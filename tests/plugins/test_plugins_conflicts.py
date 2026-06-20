"""Tests for the conflict reviewer (E4.3 -- puppy-viu.4.3).

Acceptance criteria proven here:

* ``/plugins conflicts`` lists pending ``.plugin_conflicts/<name>`` sidecars.
* ``diff`` renders a unified diff of mine vs upstream.
* ``accept-upstream`` overwrites the user's copy with the sidecar AND advances
  the installed-manifest baseline; the sidecar is removed.
* ``keep-mine`` leaves the user's copy untouched but still advances the baseline
  (acknowledging the upstream change); the sidecar is removed.
* Resolving makes the next three-way classification a clean NOOP/PRESERVE.

All logic is pure data-gathering + deterministic filesystem mutation, so these
drive ``conflicts`` directly against synthetic tier roots -- no message bus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_puppy.plugins.plugin_list import conflicts as cf
from code_puppy.plugins.plugin_sync import (
    CONFLICT_DIRNAME,
    Action,
    manifest_plugin_hashes,
    plan_update,
    read_installed_manifest,
    write_installed_manifest,
)
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


def _make_sidecar(root: Path, name: str, body: str) -> Path:
    """Drop an upstream sidecar exactly where the sync engine would."""
    return _make_plugin(root / CONFLICT_DIRNAME, name, body)


@pytest.fixture
def tiers(tmp_path, monkeypatch):
    """Synthetic user + project ejected roots wired into the reviewer."""
    user = tmp_path / "user"
    project = tmp_path / "project"
    user.mkdir()
    project.mkdir()
    monkeypatch.setattr(cf, "_user_plugins_dir", lambda: user)
    monkeypatch.setattr(cf, "_project_plugins_dir", lambda: project)
    return {"user": user, "project": project}


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_no_conflicts_when_no_sidecars(tiers):
    _make_plugin(tiers["user"], "alpha", "v1\n")
    assert cf.list_conflicts() == []


def test_lists_sidecars_across_tiers_precedence_first(tiers):
    _make_plugin(tiers["user"], "alpha", "mine-u\n")
    _make_sidecar(tiers["user"], "alpha", "upstream-u\n")
    _make_plugin(tiers["project"], "beta", "mine-p\n")
    _make_sidecar(tiers["project"], "beta", "upstream-p\n")

    conflicts = cf.list_conflicts()
    names_tiers = [(c.name, c.tier) for c in conflicts]
    # project precedence comes first.
    assert names_tiers == [("beta", "project"), ("alpha", "user")]


def test_describe_populates_three_hashes(tiers):
    _make_plugin(tiers["user"], "alpha", "mine\n")
    _make_sidecar(tiers["user"], "alpha", "upstream\n")
    write_installed_manifest(tiers["user"], {"alpha": "old-base"}, package_version="1")

    (c,) = cf.list_conflicts()
    assert c.current_hash == compute_plugin_hash(tiers["user"] / "alpha")
    assert c.upstream_hash == compute_plugin_hash(
        tiers["user"] / CONFLICT_DIRNAME / "alpha"
    )
    assert c.base_hash == "old-base"


# ---------------------------------------------------------------------------
# accept-upstream
# ---------------------------------------------------------------------------


def test_accept_upstream_overwrites_and_advances_baseline(tiers):
    _make_plugin(tiers["user"], "alpha", "mine\n")
    _make_sidecar(tiers["user"], "alpha", "UPSTREAM CONTENT\n")
    write_installed_manifest(tiers["user"], {"alpha": "old-base"}, package_version="9")

    (c,) = cf.find_conflict("alpha")
    upstream_hash = c.upstream_hash
    result = cf.accept_upstream(c)

    assert result.ok is True
    # User copy now holds the upstream content.
    body = (tiers["user"] / "alpha" / "register_callbacks.py").read_text(newline="")
    assert body == "UPSTREAM CONTENT\n"
    # Baseline advanced to the upstream (NEW) hash.
    base = manifest_plugin_hashes(read_installed_manifest(tiers["user"]))
    assert base["alpha"] == upstream_hash
    # package_version stamp preserved.
    assert read_installed_manifest(tiers["user"])["package_version"] == "9"
    # Sidecar gone, and the empty .plugin_conflicts dir cleaned up.
    assert not (tiers["user"] / CONFLICT_DIRNAME / "alpha").exists()
    assert not (tiers["user"] / CONFLICT_DIRNAME).exists()


def test_accept_upstream_then_sync_is_noop(tiers):
    _make_plugin(tiers["user"], "alpha", "mine\n")
    _make_sidecar(tiers["user"], "alpha", "v2\n")
    write_installed_manifest(tiers["user"], {"alpha": "old-base"}, package_version="9")

    cf.accept_upstream(cf.find_conflict("alpha")[0])

    # Reconstruct the three-way state after resolving.
    base = manifest_plugin_hashes(read_installed_manifest(tiers["user"]))
    cur = compute_plugin_hash(tiers["user"] / "alpha")
    new = base["alpha"]  # upstream became the baseline
    plan = plan_update({"alpha": base["alpha"]}, {"alpha": new}, {"alpha": cur})
    assert plan.ops[0].action == Action.NOOP


def test_accept_upstream_missing_sidecar_fails(tiers):
    _make_plugin(tiers["user"], "alpha", "mine\n")
    write_installed_manifest(tiers["user"], {"alpha": "b"}, package_version="1")
    # Hand-build a Conflict whose sidecar does not exist.
    c = cf._describe("alpha", "user", tiers["user"])
    result = cf.accept_upstream(c)
    assert result.ok is False
    assert "No upstream sidecar" in result.message


# ---------------------------------------------------------------------------
# keep-mine
# ---------------------------------------------------------------------------


def test_keep_mine_preserves_copy_and_advances_baseline(tiers):
    _make_plugin(tiers["user"], "alpha", "MY EDITS\n")
    _make_sidecar(tiers["user"], "alpha", "upstream\n")
    write_installed_manifest(tiers["user"], {"alpha": "old-base"}, package_version="5")

    (c,) = cf.find_conflict("alpha")
    upstream_hash = c.upstream_hash
    result = cf.keep_mine(c)

    assert result.ok is True
    # User copy untouched.
    body = (tiers["user"] / "alpha" / "register_callbacks.py").read_text(newline="")
    assert body == "MY EDITS\n"
    # Baseline advanced to upstream (NEW) -> acknowledges the upstream change.
    base = manifest_plugin_hashes(read_installed_manifest(tiers["user"]))
    assert base["alpha"] == upstream_hash
    assert not (tiers["user"] / CONFLICT_DIRNAME / "alpha").exists()


def test_keep_mine_then_sync_is_preserve(tiers):
    _make_plugin(tiers["user"], "alpha", "MY EDITS\n")
    _make_sidecar(tiers["user"], "alpha", "upstream\n")
    write_installed_manifest(tiers["user"], {"alpha": "old-base"}, package_version="5")

    cf.keep_mine(cf.find_conflict("alpha")[0])

    base = manifest_plugin_hashes(read_installed_manifest(tiers["user"]))
    cur = compute_plugin_hash(tiers["user"] / "alpha")
    new = base["alpha"]  # upstream is now the baseline
    plan = plan_update({"alpha": base["alpha"]}, {"alpha": new}, {"alpha": cur})
    # user_modified=True, upstream_change=False -> PRESERVE.
    assert plan.ops[0].action == Action.PRESERVE


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_shows_changed_lines(tiers):
    _make_plugin(tiers["user"], "alpha", "line one\nMINE\nline three\n")
    _make_sidecar(tiers["user"], "alpha", "line one\nUPSTREAM\nline three\n")

    (c,) = cf.find_conflict("alpha")
    text = cf.diff_conflict(c)

    assert "mine/register_callbacks.py" in text
    assert "upstream/register_callbacks.py" in text
    assert "-MINE" in text
    assert "+UPSTREAM" in text


def test_diff_detects_added_file(tiers):
    _make_plugin(tiers["user"], "alpha", "base\n")
    side = _make_sidecar(tiers["user"], "alpha", "base\n")
    (side / "extra.py").write_text("brand new\n", newline="")

    (c,) = cf.find_conflict("alpha")
    text = cf.diff_conflict(c)
    assert "upstream/extra.py" in text
    assert "+brand new" in text


def test_diff_missing_sidecar_message(tiers):
    _make_plugin(tiers["user"], "alpha", "x\n")
    c = cf._describe("alpha", "user", tiers["user"])
    assert "No upstream sidecar" in cf.diff_conflict(c)


# ---------------------------------------------------------------------------
# formatter
# ---------------------------------------------------------------------------


def test_format_empty():
    assert "No pending plugin conflicts" in cf.format_conflict_list([])


def test_format_lists_with_actions(tiers):
    _make_plugin(tiers["user"], "alpha", "mine\n")
    _make_sidecar(tiers["user"], "alpha", "upstream\n")
    write_installed_manifest(tiers["user"], {"alpha": "old"}, package_version="1")

    text = cf.format_conflict_list(cf.list_conflicts())
    assert "Pending plugin conflicts (1)" in text
    assert "alpha" in text
    assert "you edited it" in text
    assert "accept-upstream" in text
    assert "keep-mine" in text
    assert "diff" in text
