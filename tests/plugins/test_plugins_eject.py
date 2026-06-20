"""Tests for the eject *action* (E4.1 -- puppy-viu.4.1).

Acceptance criteria proven here:

* ``/plugins eject <name>`` copies the plugin to the user dir, records its
  baseline hash, and the ejected copy would load + suppress the builtin on the
  next launch.
* A plugin with a **non-ejected** cross-plugin sibling is *refused* (closes L5
  -- no partial ejects) with a clear message + the ``--cluster`` opt-in; passing
  ``cluster=True`` ejects the whole **dependency cluster**.
* A standalone plugin -- or one whose siblings are all already ejected -- ejects
  normally with no opt-in.
* Already-ejected members are skipped, never clobbered; non-builtins are
  refused.

All logic is pure (tier inspection + the shared E3 hash engine + an AST scan),
so these tests drive ``eject`` directly with synthetic tier roots -- no message
bus, no real plugins package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_puppy.plugins.plugin_list import eject, ejectable
from code_puppy.plugins.plugin_sync import (
    Action,
    compute_current_hashes,
    manifest_plugin_hashes,
    plan_update,
    read_installed_manifest,
)
from code_puppy.plugins.shipped_manifest import compute_plugin_hash


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _make_plugin(root: Path, name: str, body: str = "v1\n") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    # newline="" so disk bytes match *body* exactly (no Windows rewriting).
    (d / "register_callbacks.py").write_text(body, newline="")
    return d


@pytest.fixture
def tiers(tmp_path, monkeypatch):
    """Three synthetic tier roots wired into ejectable's lookups.

    eject reuses ejectable's tier seams, so patching ejectable steers eject too.
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
# resolve_cluster() -- dependency-cluster discovery
# ---------------------------------------------------------------------------


def test_cluster_is_self_when_no_cross_plugin_imports(tiers):
    _make_plugin(tiers["builtin"], "alpha", "x = 1\n")
    assert eject.resolve_cluster("alpha") == ["alpha"]


def test_cluster_follows_from_import(tiers):
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")
    assert eject.resolve_cluster("alpha") == ["alpha", "beta"]


def test_cluster_follows_plain_import(tiers):
    _make_plugin(tiers["builtin"], "alpha", "import code_puppy.plugins.beta\n")
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")
    assert eject.resolve_cluster("alpha") == ["alpha", "beta"]


def test_cluster_is_transitive(tiers):
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(
        tiers["builtin"], "beta", "from code_puppy.plugins.gamma import other\n"
    )
    _make_plugin(tiers["builtin"], "gamma", "other = 1\n")
    assert eject.resolve_cluster("alpha") == ["alpha", "beta", "gamma"]


def test_cluster_ignores_core_and_self_imports(tiers):
    body = (
        "from code_puppy.messaging import emit_info\n"
        "from code_puppy.plugins.alpha.sub import helper\n"  # self -> ignored
    )
    _make_plugin(tiers["builtin"], "alpha", body)
    assert eject.resolve_cluster("alpha") == ["alpha"]


def test_cluster_handles_cycles(tiers):
    _make_plugin(tiers["builtin"], "alpha", "from code_puppy.plugins.beta import b\n")
    _make_plugin(tiers["builtin"], "beta", "from code_puppy.plugins.alpha import a\n")
    assert eject.resolve_cluster("alpha") == ["alpha", "beta"]


def test_cluster_ignores_unknown_plugin_names(tiers):
    # References a name that is not a builtin -> not pulled into the cluster.
    _make_plugin(tiers["builtin"], "alpha", "from code_puppy.plugins.ghost import x\n")
    assert eject.resolve_cluster("alpha") == ["alpha"]


def test_cluster_empty_for_non_builtin(tiers):
    _make_plugin(tiers["user"], "mine", "x = 1\n")
    assert eject.resolve_cluster("mine") == []


# ---------------------------------------------------------------------------
# eject() -- the action
# ---------------------------------------------------------------------------


def test_eject_copies_and_records_baseline(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")

    result = eject.eject("alpha")

    assert result.ok is True
    assert result.ejected == ("alpha",)
    assert result.target_tier == "user"
    # Copied to the user dir.
    assert (tiers["user"] / "alpha" / "register_callbacks.py").exists()
    # Baseline recorded == the on-disk content hash.
    base = manifest_plugin_hashes(read_installed_manifest(tiers["user"]))
    assert base["alpha"] == compute_plugin_hash(tiers["user"] / "alpha")


def test_ejected_copy_suppresses_builtin_next_launch(tiers):
    """describe() (the loader's precedence view) now reports the user copy wins."""
    _make_plugin(tiers["builtin"], "alpha", "v1\n")

    eject.eject("alpha")

    status = ejectable.describe("alpha")
    assert status.is_ejected is True
    assert status.ejected_tier == "user"
    assert status.loaded_tier == "user"  # user beats builtin
    assert status.modification == ejectable.MOD_UNMODIFIED


def test_eject_makes_next_sync_a_noop(tiers):
    """BASE == NEW == CUR after eject -> the startup sync would do nothing."""
    _make_plugin(tiers["builtin"], "alpha", "v1\n")

    eject.eject("alpha")

    # NEW (shipped) hashes -- no shipped manifest here, so compute from builtin.
    new_hashes = {"alpha": compute_plugin_hash(tiers["builtin"] / "alpha")}
    base_hashes = manifest_plugin_hashes(read_installed_manifest(tiers["user"]))
    cur = compute_current_hashes(tiers["user"], ["alpha"])
    plan = plan_update(base_hashes, new_hashes, cur, scope=["alpha"])

    assert [op.action for op in plan.ops] == [Action.NOOP]


def test_eject_stamps_running_package_version(tiers):
    """Eject stamps the running wheel version, not None (closes puppy-0gg).

    On a *first* eject the installed manifest is absent, so the old code stamped
    ``package_version=None`` -- which made the startup sync fast-path
    (``base.package_version == running_version``) miss for one restart and do a
    needless full hash pass. The freshly written manifest must carry the same
    version the fast-path checks against.
    """
    _make_plugin(tiers["builtin"], "alpha", "v1\n")

    eject.eject("alpha")

    manifest = read_installed_manifest(tiers["user"])
    assert manifest["package_version"] == eject._current_package_version()
    assert manifest["package_version"] is not None


def test_eject_refuses_partial_cluster(tiers):
    """Ejecting a plugin with a non-ejected sibling is refused, not auto-pulled."""
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")

    result = eject.eject("alpha")

    assert result.ok is False
    assert result.refused is True
    assert result.requires_cluster == ("beta",)
    assert result.ejected == ()
    # Nothing was written -- not even alpha.
    assert not (tiers["user"] / "alpha").exists()
    assert not (tiers["user"] / "beta").exists()
    assert read_installed_manifest(tiers["user"]) is None
    assert "--cluster" in result.message
    assert "beta" in result.message


def test_eject_cluster_opt_in_pulls_dependency(tiers):
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")

    result = eject.eject("alpha", cluster=True)

    assert result.ok is True
    assert result.refused is False
    assert set(result.ejected) == {"alpha", "beta"}
    assert (tiers["user"] / "alpha" / "register_callbacks.py").exists()
    assert (tiers["user"] / "beta" / "register_callbacks.py").exists()
    base = manifest_plugin_hashes(read_installed_manifest(tiers["user"]))
    assert "alpha" in base and "beta" in base


def test_eject_proceeds_when_sibling_already_ejected(tiers):
    """No refusal when every imported sibling is already present in the tier."""
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")
    _make_plugin(tiers["user"], "beta", "thing = 1\n")  # sibling already ejected

    result = eject.eject("alpha")

    assert result.ok is True
    assert result.refused is False
    assert result.ejected == ("alpha",)
    assert result.skipped == ("beta",)


def test_eject_standalone_plugin_needs_no_opt_in(tiers):
    """A plugin with no cross-plugin imports ejects normally without --cluster."""
    _make_plugin(tiers["builtin"], "alpha", "x = 1\n")

    result = eject.eject("alpha")

    assert result.ok is True
    assert result.refused is False
    assert result.ejected == ("alpha",)


def test_eject_skips_already_ejected_member_without_clobbering(tiers):
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")
    # User already has an *edited* beta -- it must survive untouched.
    _make_plugin(tiers["user"], "beta", "I edited beta\n")

    result = eject.eject("alpha")

    assert result.ok is True
    assert result.ejected == ("alpha",)
    assert result.skipped == ("beta",)
    # The edited copy is preserved byte-for-byte.
    assert (tiers["user"] / "beta" / "register_callbacks.py").read_text(
        newline=""
    ) == "I edited beta\n"


def test_eject_idempotent_when_already_ejected(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")
    eject.eject("alpha")

    again = eject.eject("alpha")

    assert again.ok is True
    assert again.ejected == ()
    assert again.skipped == ("alpha",)
    assert "already ejected" in again.message.lower()


def test_eject_to_project_tier(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")

    result = eject.eject("alpha", target="project")

    assert result.ok is True
    assert result.target_tier == "project"
    assert (tiers["project"] / "alpha" / "register_callbacks.py").exists()
    base = manifest_plugin_hashes(read_installed_manifest(tiers["project"]))
    assert "alpha" in base


def test_eject_rejects_non_builtin(tiers):
    _make_plugin(tiers["user"], "mine", "x = 1\n")

    result = eject.eject("mine")

    assert result.ok is False
    assert "not a builtin" in result.message
    # Nothing was written to a baseline.
    assert read_installed_manifest(tiers["user"]) is None


def test_eject_rejects_unknown_plugin(tiers):
    result = eject.eject("ghost")
    assert result.ok is False
    assert "not found" in result.message


def test_eject_rejects_unknown_target(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")
    result = eject.eject("alpha", target="bogus")
    assert result.ok is False
    assert "Unknown eject target" in result.message


def test_eject_preserves_existing_baselines(tiers):
    """Ejecting beta must not drop alpha's previously recorded baseline."""
    _make_plugin(tiers["builtin"], "alpha", "a\n")
    _make_plugin(tiers["builtin"], "beta", "b\n")
    eject.eject("alpha")
    eject.eject("beta")

    base = manifest_plugin_hashes(read_installed_manifest(tiers["user"]))
    assert "alpha" in base and "beta" in base


# ---------------------------------------------------------------------------
# formatters -- pure string rendering
# ---------------------------------------------------------------------------


def test_format_eject_result_single(tiers):
    _make_plugin(tiers["builtin"], "alpha", "v1\n")
    text = eject.format_eject_result(eject.eject("alpha"))
    assert "Ejected 'alpha'" in text
    assert "restart" in text.lower()


def test_format_eject_result_cluster_with_skip(tiers):
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")
    _make_plugin(tiers["user"], "beta", "edited\n")

    text = eject.format_eject_result(eject.eject("alpha"))

    assert "alpha" in text
    assert "Already ejected" in text
    assert "beta" in text


def test_format_eject_result_failure(tiers):
    text = eject.format_eject_result(eject.eject("ghost"))
    assert "not found" in text


def test_format_eject_result_refusal(tiers):
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")

    result = eject.eject("alpha")
    text = eject.format_eject_result(result)

    assert "Refusing to eject 'alpha'" in text
    assert "beta" in text
    assert "/plugins eject alpha --cluster" in text


def test_format_eject_result_refusal_to_project_keeps_target_flag(tiers):
    _make_plugin(
        tiers["builtin"], "alpha", "from code_puppy.plugins.beta import thing\n"
    )
    _make_plugin(tiers["builtin"], "beta", "thing = 1\n")

    result = eject.eject("alpha", target="project")
    text = eject.format_eject_result(result)

    assert "/plugins eject alpha project --cluster" in text
