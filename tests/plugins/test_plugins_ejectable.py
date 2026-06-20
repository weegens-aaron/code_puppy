"""Tests for the eject discoverability helpers (E4.2 — puppy-viu.4.2).

Acceptance criteria proven here:

* ``/plugins list-ejectable`` lists builtins eligible for eject (and flags the
  ones already ejected).
* ``/plugins show <name>`` reports tier, ejected state, and modification status.

All logic is pure (tier inspection + the shared E3 hash engine), so these tests
drive ``ejectable`` directly with synthetic tier roots — no message bus, no real
plugins package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_puppy.plugins.plugin_list import ejectable
from code_puppy.plugins.plugin_sync import write_installed_manifest
from code_puppy.plugins.shipped_manifest import compute_plugin_hash


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _make_plugin(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    # newline="" so disk bytes match *body* exactly (no Windows rewriting).
    (d / "register_callbacks.py").write_text(body, newline="")
    return d


@pytest.fixture
def tiers(tmp_path, monkeypatch):
    """Three synthetic tier roots wired into ejectable's lookups.

    Returns a dict with ``builtin`` / ``user`` / ``project`` Paths.
    """
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    builtin.mkdir()
    user.mkdir()
    project.mkdir()

    monkeypatch.setattr(ejectable, "get_builtin_plugins_dir", lambda: builtin)
    monkeypatch.setattr(ejectable, "_user_plugins_dir", lambda: user)
    monkeypatch.setattr(ejectable, "_project_plugins_dir", lambda: project)

    return {"builtin": builtin, "user": user, "project": project}


# ---------------------------------------------------------------------------
# describe() — single plugin status
# ---------------------------------------------------------------------------


def test_pristine_builtin_is_ejectable(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")

    s = ejectable.describe("alpha")

    assert s.is_builtin is True
    assert s.ejectable is True
    assert s.is_ejected is False
    assert s.ejected_tier is None
    assert s.modification == ejectable.MOD_NA
    assert s.present_tiers == ("builtin",)
    assert s.loaded_tier == "builtin"


def test_ejected_unmodified_via_baseline(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")
    _make_plugin(tiers["user"], "alpha", "v1\n")
    # Record BASE == current on-disk hash -> CUR == BASE -> unmodified.
    write_installed_manifest(
        tiers["user"],
        {"alpha": compute_plugin_hash(tiers["user"] / "alpha")},
        package_version="1.0.0",
    )

    s = ejectable.describe("alpha")

    assert s.is_ejected is True
    assert s.ejected_tier == "user"
    assert s.ejectable is False  # already ejected
    assert s.modification == ejectable.MOD_UNMODIFIED
    assert s.loaded_tier == "user"  # user beats builtin


def test_ejected_modified_via_baseline(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")
    _make_plugin(tiers["user"], "alpha", "I edited this\n")
    # BASE is the original hash; CUR now differs -> modified.
    write_installed_manifest(
        tiers["user"], {"alpha": "stale-original-base-hash"}, package_version="1.0.0"
    )

    s = ejectable.describe("alpha")

    assert s.modification == ejectable.MOD_MODIFIED
    assert s.ejected_tier == "user"


def test_ejected_no_manifest_falls_back_to_shipped_hash(tiers):
    """A hand-dropped copy (no installed manifest) compares CUR vs shipped NEW."""
    _make_plugin(tiers["builtin"], "alpha", "v1\n")
    # Identical copy, no manifest -> falls back to shipped hash -> unmodified.
    _make_plugin(tiers["user"], "alpha", "v1\n")
    assert ejectable.describe("alpha").modification == ejectable.MOD_UNMODIFIED

    # Now make the user copy differ -> modified (still no manifest).
    _make_plugin(tiers["user"], "alpha", "changed\n")
    assert ejectable.describe("alpha").modification == ejectable.MOD_MODIFIED


def test_project_wins_over_user_and_builtin(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")
    _make_plugin(tiers["user"], "alpha", "v1\n")
    _make_plugin(tiers["project"], "alpha", "v1\n")

    s = ejectable.describe("alpha")

    assert s.present_tiers == ("project", "user", "builtin")
    assert s.loaded_tier == "project"
    assert s.ejected_tier == "project"  # highest-precedence owned copy


def test_user_authored_plugin_is_not_ejectable(tiers):
    _make_plugin(tiers["user"], "my_own", "mine\n")  # not a builtin

    s = ejectable.describe("my_own")

    assert s.is_builtin is False
    assert s.ejectable is False
    assert s.is_ejected is False
    assert s.modification == ejectable.MOD_NA
    assert s.present_tiers == ("user",)


def test_unknown_plugin_reports_not_found(tiers):
    s = ejectable.describe("ghost")
    assert s.exists is False
    assert s.present_tiers == ()
    assert s.loaded_tier is None


# ---------------------------------------------------------------------------
# list_ejectable() — the catalogue
# ---------------------------------------------------------------------------


def test_list_ejectable_enumerates_all_builtins_sorted(tiers):
    _make_plugin(tiers["builtin"], "zebra", "z\n")
    _make_plugin(tiers["builtin"], "alpha", "a\n")
    _make_plugin(tiers["builtin"], "mid", "m\n")
    # Eject one of them.
    _make_plugin(tiers["user"], "mid", "m\n")
    write_installed_manifest(
        tiers["user"],
        {"mid": compute_plugin_hash(tiers["user"] / "mid")},
        package_version="1.0.0",
    )

    statuses = ejectable.list_ejectable()

    assert [s.name for s in statuses] == ["alpha", "mid", "zebra"]
    by_name = {s.name: s for s in statuses}
    assert by_name["alpha"].ejectable is True
    assert by_name["zebra"].ejectable is True
    assert by_name["mid"].ejectable is False  # already ejected
    assert by_name["mid"].is_ejected is True


def test_list_ejectable_ignores_user_only_plugins(tiers):
    _make_plugin(tiers["builtin"], "alpha", "a\n")
    _make_plugin(tiers["user"], "user_thing", "u\n")  # not a builtin

    names = [s.name for s in ejectable.list_ejectable()]
    assert names == ["alpha"]  # user_thing is not a builtin, never ejectable


# ---------------------------------------------------------------------------
# formatters — pure string rendering
# ---------------------------------------------------------------------------


def test_format_list_ejectable_sections(tiers):
    _make_plugin(tiers["builtin"], "alpha", "a\n")
    _make_plugin(tiers["builtin"], "beta", "b\n")
    _make_plugin(tiers["user"], "beta", "edited\n")
    write_installed_manifest(tiers["user"], {"beta": "old"}, package_version="1.0.0")

    text = ejectable.format_list_ejectable(ejectable.list_ejectable())

    assert "Available to eject (1)" in text
    assert "alpha" in text
    assert "Already ejected (1)" in text
    assert "beta" in text
    assert "modified" in text
    assert "/plugins eject" in text


def test_format_list_ejectable_empty():
    assert "No builtin plugins" in ejectable.format_list_ejectable([])


def test_format_show_pristine_builtin(tiers):
    _make_plugin(tiers["builtin"], "alpha", "a\n")
    text = ejectable.format_show(ejectable.describe("alpha"))
    assert "Plugin: alpha" in text
    assert "Builtin:" in text
    assert "Ejectable:" in text
    assert "/plugins eject alpha" in text


def test_format_show_ejected(tiers):
    _make_plugin(tiers["builtin"], "alpha", "a\n")
    _make_plugin(tiers["user"], "alpha", "edited\n")
    write_installed_manifest(tiers["user"], {"alpha": "old"}, package_version="1.0.0")

    text = ejectable.format_show(ejectable.describe("alpha"))
    assert "Ejected:" in text
    assert "user" in text
    assert "Modification:" in text
    assert ejectable.MOD_MODIFIED in text


def test_format_show_not_found(tiers):
    text = ejectable.format_show(ejectable.describe("ghost"))
    assert "not found" in text
