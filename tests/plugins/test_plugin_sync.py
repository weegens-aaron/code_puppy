"""Tests for the scoped hash-aware plugin sync engine (E3.1 — puppy-viu.3.1).

Acceptance criteria proven here:

* ``plan_update`` computes BASE/NEW/CUR three-way status with **no side
  effects** (pure: called with hash maps, touches no filesystem).
* ``apply_update`` writes **atomically** (temp + os.replace; no half-written
  dirs; conflicts never clobber the user's copy).
* **only ejected plugins are touched** (a non-ejected builtin in the shipped
  manifest is never written/considered).
* the installed manifest **round-trips** (write -> read -> identical hashes).
"""

from pathlib import Path

from code_puppy.plugins import plugin_sync as ps
from code_puppy.plugins.plugin_sync import Action
from code_puppy.plugins.shipped_manifest import compute_plugin_hash


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_plugin(root: Path, name: str, src: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    # newline="" so disk bytes match *src* exactly (Windows would rewrite \n).
    (d / "register_callbacks.py").write_text(src, newline="")
    return d


def _hash(root: Path, name: str) -> str:
    return compute_plugin_hash(root / name)


def _action(plan: ps.UpdatePlan, name: str) -> Action:
    return next(op.action for op in plan.ops if op.name == name)


# ---------------------------------------------------------------------------
# plan_update — purity + the decision table
# ---------------------------------------------------------------------------


def test_plan_update_is_pure_no_filesystem(tmp_path):
    """plan_update takes plain dicts and must not touch disk at all."""
    before = list(tmp_path.iterdir())
    plan = ps.plan_update(
        base_hashes={"a": "x"},
        new_hashes={"a": "x"},
        cur_hashes={"a": "x"},
    )
    assert plan.ops  # produced a result
    assert list(tmp_path.iterdir()) == before  # nothing written


def test_decision_table_all_rows():
    base = {
        "noop": "b",
        "update": "b",
        "preserve": "b",
        "adopt_conv": "b",
        "conflict": "b",
        "deleted": "b",
        "orphan": "b",
    }
    new = {
        "noop": "b",  # unchanged
        "update": "N",  # upstream changed
        "preserve": "b",  # upstream unchanged
        "adopt_conv": "N",  # upstream changed...
        "conflict": "N",  # upstream changed...
        # deleted / orphan: absent from NEW
        "added_write": "N",  # new to scope
        "added_adopt": "N",
        "added_conflict": "N",
    }
    cur = {
        "noop": "b",  # == base
        "update": "b",  # == base (user clean)
        "preserve": "U",  # != base (user owns)
        "adopt_conv": "N",  # == new (converged)
        "conflict": "U",  # != base and != new
        "deleted": "b",  # == base -> safe delete
        "orphan": "U",  # != base -> keep orphan
        "added_write": None,  # absent on disk
        "added_adopt": "N",  # identical already
        "added_conflict": "U",  # different
    }
    scope = set(base) | set(new) | {k for k, v in cur.items() if v is not None}
    plan = ps.plan_update(base, new, cur, scope=scope)

    assert _action(plan, "noop") == Action.NOOP
    assert _action(plan, "update") == Action.WRITE
    assert _action(plan, "preserve") == Action.PRESERVE
    assert _action(plan, "adopt_conv") == Action.ADOPT
    assert _action(plan, "conflict") == Action.CONFLICT
    assert _action(plan, "deleted") == Action.DELETE
    assert _action(plan, "orphan") == Action.KEEP_ORPHAN
    assert _action(plan, "added_write") == Action.WRITE
    assert _action(plan, "added_adopt") == Action.ADOPT
    assert _action(plan, "added_conflict") == Action.CONFLICT


def test_default_scope_excludes_unmanaged_builtins():
    """Only ejected plugins are touched: a shipped-but-never-ejected builtin
    (present in NEW only) is not even planned when scope defaults."""
    plan = ps.plan_update(
        base_hashes={"ejected": "b"},
        new_hashes={"ejected": "b", "never_ejected": "z"},
        cur_hashes={"ejected": "b"},
    )
    names = {op.name for op in plan.ops}
    assert names == {"ejected"}
    assert "never_ejected" not in names


def test_plan_ops_sorted_and_deterministic():
    plan = ps.plan_update(
        base_hashes={"c": "1", "a": "1", "b": "1"},
        new_hashes={"c": "1", "a": "1", "b": "1"},
        cur_hashes={"c": "1", "a": "1", "b": "1"},
    )
    assert [op.name for op in plan.ops] == ["a", "b", "c"]


def test_updateplan_helpers():
    plan = ps.plan_update(
        base_hashes={"u": "b", "p": "b"},
        new_hashes={"u": "N", "p": "b"},
        cur_hashes={"u": "b", "p": "U"},
    )
    assert plan.writes == ["u"]
    assert plan.by_action(Action.PRESERVE)[0].name == "p"
    assert not plan.is_noop()

    noop_plan = ps.plan_update({"a": "b"}, {"a": "b"}, {"a": "b"})
    assert noop_plan.is_noop()


# ---------------------------------------------------------------------------
# compute_current_hashes
# ---------------------------------------------------------------------------


def test_compute_current_hashes_missing_dir_is_none(tmp_path):
    _make_plugin(tmp_path, "present", "x = 1\n")
    cur = ps.compute_current_hashes(tmp_path, ["present", "absent"])
    assert cur["present"] == _hash(tmp_path, "present")
    assert cur["absent"] is None


# ---------------------------------------------------------------------------
# installed manifest round-trip
# ---------------------------------------------------------------------------


def test_installed_manifest_round_trips(tmp_path):
    hashes = {"emoji_filter": "9f2c", "theme": "1ab3"}
    path = ps.write_installed_manifest(tmp_path, hashes, package_version="1.2.3")
    assert path.name == ps.INSTALLED_MANIFEST_FILENAME

    loaded = ps.read_installed_manifest(tmp_path)
    assert loaded["package_version"] == "1.2.3"
    assert ps.manifest_plugin_hashes(loaded) == hashes


def test_read_missing_manifest_is_none(tmp_path):
    assert ps.read_installed_manifest(tmp_path) is None
    assert ps.manifest_plugin_hashes(None) == {}


def test_read_corrupt_manifest_is_none(tmp_path):
    (tmp_path / ps.INSTALLED_MANIFEST_FILENAME).write_text("{ not json")
    assert ps.read_installed_manifest(tmp_path) is None


def test_manifest_filename_is_loader_safe():
    # dot-prefixed so user/project loaders (which skip "." entries) ignore it.
    assert ps.INSTALLED_MANIFEST_FILENAME.startswith(".")
    assert ps.CONFLICT_DIRNAME.startswith(".")


# ---------------------------------------------------------------------------
# apply_update — atomicity, scope, conflict non-destruction, baseline advance
# ---------------------------------------------------------------------------


def test_apply_write_copies_upstream_and_matches_new(tmp_path):
    source = tmp_path / "src"
    ejected = tmp_path / "ejected"
    _make_plugin(source, "foo", "VERSION = 2\n")
    new_hashes = {"foo": _hash(source, "foo")}

    # foo not yet on disk -> bootstrap WRITE.
    plan = ps.plan_update({}, new_hashes, {"foo": None}, scope=["foo"])
    assert _action(plan, "foo") == Action.WRITE

    manifest = ps.apply_update(plan, ejected, source, new_hashes)

    # Written copy is byte-clean and hashes identically to NEW.
    assert (ejected / "foo" / "register_callbacks.py").read_text() == "VERSION = 2\n"
    assert _hash(ejected, "foo") == new_hashes["foo"]
    # Baseline advanced: BASE := NEW.
    assert ps.manifest_plugin_hashes(manifest)["foo"] == new_hashes["foo"]
    # No temp/backup leftovers.
    leftovers = [p.name for p in ejected.iterdir() if p.name.startswith(".tmp")]
    assert leftovers == []


def test_apply_is_idempotent(tmp_path):
    """Run twice: the second pass is a pure no-op (CUR == NEW == BASE)."""
    source = tmp_path / "src"
    ejected = tmp_path / "ejected"
    _make_plugin(source, "foo", "x = 1\n")
    new_hashes = {"foo": _hash(source, "foo")}

    plan1 = ps.plan_update({}, new_hashes, {"foo": None}, scope=["foo"])
    ps.apply_update(plan1, ejected, source, new_hashes)

    base = ps.manifest_plugin_hashes(ps.read_installed_manifest(ejected))
    cur = ps.compute_current_hashes(ejected, ["foo"])
    plan2 = ps.plan_update(base, new_hashes, cur, scope=["foo"])
    assert plan2.is_noop()
    assert _action(plan2, "foo") == Action.NOOP


def test_apply_preserves_user_modified_plugin(tmp_path):
    """User edited it, upstream unchanged -> file is left byte-for-byte."""
    source = tmp_path / "src"
    ejected = tmp_path / "ejected"
    _make_plugin(source, "foo", "ORIGINAL = 1\n")
    new_hashes = {"foo": _hash(source, "foo")}

    # User has a modified copy on disk.
    _make_plugin(ejected, "foo", "USER_EDIT = 99\n")
    base = {"foo": new_hashes["foo"]}  # BASE = what we last wrote (== upstream)
    cur = ps.compute_current_hashes(ejected, ["foo"])

    plan = ps.plan_update(base, new_hashes, cur, scope=["foo"])
    assert _action(plan, "foo") == Action.PRESERVE

    ps.apply_update(plan, ejected, source, new_hashes)
    assert (ejected / "foo" / "register_callbacks.py").read_text() == "USER_EDIT = 99\n"


def test_apply_conflict_is_non_destructive(tmp_path):
    """Both user and upstream changed -> keep user file, sidecar the upstream."""
    source = tmp_path / "src"
    ejected = tmp_path / "ejected"
    # Upstream now ships V2.
    _make_plugin(source, "foo", "UPSTREAM = 2\n")
    new_hashes = {"foo": _hash(source, "foo")}
    # User edited away from the old baseline.
    _make_plugin(ejected, "foo", "USER = 3\n")
    base = {"foo": "old_baseline_hash"}  # != cur and != new
    cur = ps.compute_current_hashes(ejected, ["foo"])

    plan = ps.plan_update(base, new_hashes, cur, scope=["foo"])
    assert _action(plan, "foo") == Action.CONFLICT

    warnings = []
    manifest = ps.apply_update(
        plan, ejected, source, new_hashes, emit_warning=warnings.append
    )

    # User's file untouched.
    assert (ejected / "foo" / "register_callbacks.py").read_text() == "USER = 3\n"
    # Upstream landed in the quarantined sidecar.
    sidecar = ejected / ps.CONFLICT_DIRNAME / "foo" / "register_callbacks.py"
    assert sidecar.read_text() == "UPSTREAM = 2\n"
    # Aggregated warning emitted, baseline advanced (BASE := NEW) so the user
    # stays flagged until they reconcile.
    assert warnings and "foo" in warnings[0]
    assert ps.manifest_plugin_hashes(manifest)["foo"] == new_hashes["foo"]


def test_apply_delete_removes_untouched_plugin(tmp_path):
    source = tmp_path / "src"
    ejected = tmp_path / "ejected"
    _make_plugin(ejected, "gone", "x = 1\n")
    base = {"gone": _hash(ejected, "gone")}  # user untouched (cur == base)
    cur = ps.compute_current_hashes(ejected, ["gone"])

    plan = ps.plan_update(base, new_hashes={}, cur_hashes=cur, scope=["gone"])
    assert _action(plan, "gone") == Action.DELETE

    manifest = ps.apply_update(plan, ejected, source, new_hashes={})
    assert not (ejected / "gone").exists()
    # Dropped from the manifest entirely.
    assert "gone" not in ps.manifest_plugin_hashes(manifest)


def test_apply_keep_orphan_when_user_modified(tmp_path):
    source = tmp_path / "src"
    ejected = tmp_path / "ejected"
    _make_plugin(ejected, "gone", "USER_EDIT = 1\n")
    base = {"gone": "old_baseline"}  # cur != base -> user modified
    cur = ps.compute_current_hashes(ejected, ["gone"])

    plan = ps.plan_update(base, new_hashes={}, cur_hashes=cur, scope=["gone"])
    assert _action(plan, "gone") == Action.KEEP_ORPHAN

    manifest = ps.apply_update(plan, ejected, source, new_hashes={})
    assert (ejected / "gone" / "register_callbacks.py").read_text() == "USER_EDIT = 1\n"
    assert "gone" not in ps.manifest_plugin_hashes(manifest)


def test_apply_only_touches_ejected_scope(tmp_path):
    """A builtin shipped but outside scope is never written to the ejected root."""
    source = tmp_path / "src"
    ejected = tmp_path / "ejected"
    _make_plugin(source, "ejected_one", "a = 1\n")
    _make_plugin(source, "untouched", "b = 2\n")
    new_hashes = {
        "ejected_one": _hash(source, "ejected_one"),
        "untouched": _hash(source, "untouched"),
    }

    plan = ps.plan_update(
        base_hashes={},
        new_hashes=new_hashes,
        cur_hashes={"ejected_one": None},
        scope=["ejected_one"],
    )
    ps.apply_update(plan, ejected, source, new_hashes)

    assert (ejected / "ejected_one").exists()
    assert not (ejected / "untouched").exists()


def test_apply_update_overwrites_existing_dir_atomically(tmp_path):
    """A clean upstream update replaces an existing (untouched) ejected dir."""
    source = tmp_path / "src"
    ejected = tmp_path / "ejected"
    # v1 currently ejected and untouched.
    _make_plugin(source, "foo", "V = 1\n")
    v1 = _hash(source, "foo")
    ps.apply_update(
        ps.plan_update({}, {"foo": v1}, {"foo": None}, scope=["foo"]),
        ejected,
        source,
        {"foo": v1},
    )
    assert (ejected / "foo" / "register_callbacks.py").read_text() == "V = 1\n"

    # Upstream ships v2; user never touched their copy -> clean WRITE.
    _make_plugin(source, "foo", "V = 2\n")
    v2 = _hash(source, "foo")
    base = ps.manifest_plugin_hashes(ps.read_installed_manifest(ejected))
    cur = ps.compute_current_hashes(ejected, ["foo"])
    plan = ps.plan_update(base, {"foo": v2}, cur, scope=["foo"])
    assert _action(plan, "foo") == Action.WRITE

    ps.apply_update(plan, ejected, source, {"foo": v2})
    assert (ejected / "foo" / "register_callbacks.py").read_text() == "V = 2\n"
    assert _hash(ejected, "foo") == v2
