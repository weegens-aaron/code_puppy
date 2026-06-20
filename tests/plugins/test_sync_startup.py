"""Tests for the startup-sync orchestrator (E3.3 — puppy-viu.3.3).

Acceptance criteria proven here:

* **Sync runs once at startup before load** — ``run_startup_sync`` plans + applies
  the scoped sync against the given ejected roots.
* **Unchanged package_version short-circuits** — a matching stamped version
  hashes/writes nothing.
* **Conflicts emit non-blocking message-bus warnings** — the conflict sink is
  invoked and the user's copy is preserved byte-for-byte.
* **User edits / user-authored plugins are never clobbered** — the scope guard
  excludes plugins that are not shipped builtins and have no baseline.
* **Never blocks** — every failure path is swallowed.
"""

from pathlib import Path

import code_puppy.plugins as plugins_pkg
from code_puppy.plugins import sync_startup
from code_puppy.plugins.plugin_sync import (
    INSTALLED_MANIFEST_FILENAME,
    CONFLICT_DIRNAME,
    read_installed_manifest,
    write_installed_manifest,
)
from code_puppy.plugins.shipped_manifest import (
    compute_plugin_hash,
    write_shipped_manifest,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_plugin(root: Path, name: str, src: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    # newline="" so disk bytes match *src* exactly (no Windows \n rewriting).
    (d / "register_callbacks.py").write_text(src, newline="")
    return d


def _source_root_with_manifest(tmp_path: Path, plugins: dict[str, str], version: str):
    """Build an in-package-style source root + its shipped manifest."""
    src = tmp_path / "source"
    src.mkdir()
    for name, body in plugins.items():
        _make_plugin(src, name, body)
    write_shipped_manifest(
        src / "_shipped_manifest.json", plugins_dir=src, package_version=version
    )
    return src


# ---------------------------------------------------------------------------
# fast-path
# ---------------------------------------------------------------------------


def test_fast_path_short_circuits_on_matching_version(tmp_path):
    """A matching stamped package_version must hash/write nothing."""
    src = _source_root_with_manifest(tmp_path, {"alpha": "v2\n"}, "1.0.0")
    ejected = tmp_path / "user"
    _make_plugin(ejected, "alpha", "v1\n")  # stale on disk
    # Baseline already stamped at the running version -> fast-path.
    write_installed_manifest(ejected, {"alpha": "whatever"}, package_version="1.0.0")

    sync_startup.run_startup_sync(
        roots=[ejected], source_root=src, package_version="1.0.0"
    )

    # Untouched: the stale v1 copy is still v1 (no upstream WRITE happened).
    assert (ejected / "alpha" / "register_callbacks.py").read_text() == "v1\n"


# ---------------------------------------------------------------------------
# clean upstream update (WRITE)
# ---------------------------------------------------------------------------


def test_clean_upstream_change_is_written(tmp_path):
    src = _source_root_with_manifest(tmp_path, {"alpha": "v2\n"}, "2.0.0")
    ejected = tmp_path / "user"
    _make_plugin(ejected, "alpha", "v1\n")
    # Baseline says BASE == old disk (user clean), older version -> sync runs.
    write_installed_manifest(
        ejected,
        {"alpha": compute_plugin_hash(ejected / "alpha")},
        package_version="1.0.0",
    )

    sync_startup.run_startup_sync(
        roots=[ejected], source_root=src, package_version="2.0.0"
    )

    assert (ejected / "alpha" / "register_callbacks.py").read_text() == "v2\n"
    manifest = read_installed_manifest(ejected)
    assert manifest["package_version"] == "2.0.0"  # baseline advanced


# ---------------------------------------------------------------------------
# scope guard — user-authored plugin untouched
# ---------------------------------------------------------------------------


def test_user_authored_plugin_never_touched(tmp_path):
    """A plugin that is NOT a shipped builtin and has no baseline is ignored."""
    src = _source_root_with_manifest(tmp_path, {"alpha": "v2\n"}, "2.0.0")
    ejected = tmp_path / "user"
    _make_plugin(ejected, "alpha", "v1\n")  # ejected builtin (in manifest)
    _make_plugin(ejected, "my_own", "mine\n")  # purely user-authored
    write_installed_manifest(
        ejected,
        {"alpha": compute_plugin_hash(ejected / "alpha")},
        package_version="1.0.0",
    )

    sync_startup.run_startup_sync(
        roots=[ejected], source_root=src, package_version="2.0.0"
    )

    # alpha got updated; my_own is byte-for-byte untouched and not in manifest.
    assert (ejected / "alpha" / "register_callbacks.py").read_text() == "v2\n"
    assert (ejected / "my_own" / "register_callbacks.py").read_text() == "mine\n"
    assert "my_own" not in read_installed_manifest(ejected)["plugins"]


# ---------------------------------------------------------------------------
# conflict — warn + preserve
# ---------------------------------------------------------------------------


def test_conflict_warns_and_preserves_user_copy(tmp_path):
    src = _source_root_with_manifest(tmp_path, {"alpha": "upstream\n"}, "2.0.0")
    ejected = tmp_path / "user"
    _make_plugin(ejected, "alpha", "my edits\n")  # CUR != BASE and != NEW
    write_installed_manifest(
        ejected, {"alpha": "original-base-hash"}, package_version="1.0.0"
    )

    warnings: list[str] = []
    sync_startup.run_startup_sync(
        roots=[ejected],
        source_root=src,
        package_version="2.0.0",
        emit_warning=warnings.append,
    )

    # User's copy is preserved...
    assert (ejected / "alpha" / "register_callbacks.py").read_text() == "my edits\n"
    # ...upstream lands in the quarantined sidecar...
    sidecar = ejected / CONFLICT_DIRNAME / "alpha" / "register_callbacks.py"
    assert sidecar.read_text() == "upstream\n"
    # ...and exactly one aggregated warning was surfaced.
    assert len(warnings) == 1
    assert "alpha" in warnings[0]


# ---------------------------------------------------------------------------
# robustness — never blocks
# ---------------------------------------------------------------------------


def test_missing_shipped_manifest_is_noop(tmp_path):
    src = tmp_path / "source"  # no manifest written
    src.mkdir()
    ejected = tmp_path / "user"
    _make_plugin(ejected, "alpha", "v1\n")

    # Must not raise and must not create an installed manifest.
    sync_startup.run_startup_sync(
        roots=[ejected], source_root=src, package_version="2.0.0"
    )
    assert not (ejected / INSTALLED_MANIFEST_FILENAME).exists()


def test_missing_root_is_noop(tmp_path):
    src = _source_root_with_manifest(tmp_path, {"alpha": "v2\n"}, "2.0.0")
    absent = tmp_path / "does_not_exist"
    sync_startup.run_startup_sync(
        roots=[absent], source_root=src, package_version="2.0.0"
    )
    assert not absent.exists()  # never created


def test_run_startup_sync_swallows_all_exceptions(monkeypatch, tmp_path):
    """A blowup inside the engine must never escape run_startup_sync."""

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sync_startup, "load_shipped_manifest", _boom)
    # Should simply return, not raise.
    sync_startup.run_startup_sync(roots=[tmp_path], source_root=tmp_path)


# ---------------------------------------------------------------------------
# idempotency — second launch fast-paths
# ---------------------------------------------------------------------------


def test_second_run_fast_paths_after_first_sync(tmp_path):
    src = _source_root_with_manifest(tmp_path, {"alpha": "v2\n"}, "2.0.0")
    ejected = tmp_path / "user"
    _make_plugin(ejected, "alpha", "v1\n")
    write_installed_manifest(
        ejected,
        {"alpha": compute_plugin_hash(ejected / "alpha")},
        package_version="1.0.0",
    )

    # First launch: applies the update, stamps version 2.0.0.
    sync_startup.run_startup_sync(
        roots=[ejected], source_root=src, package_version="2.0.0"
    )
    assert (ejected / "alpha" / "register_callbacks.py").read_text() == "v2\n"

    # Tamper after sync; a fast-pathed second run must NOT re-sync it back.
    _make_plugin(ejected, "alpha", "user touched again\n")
    sync_startup.run_startup_sync(
        roots=[ejected], source_root=src, package_version="2.0.0"
    )
    assert (
        ejected / "alpha" / "register_callbacks.py"
    ).read_text() == "user touched again\n"


# ---------------------------------------------------------------------------
# boot-spine seam — run_startup_plugin_sync runs the engine exactly once
# ---------------------------------------------------------------------------


def test_run_startup_plugin_sync_is_public_and_callable():
    """The startup-phase step is a public entry point on the plugins package."""
    assert callable(plugins_pkg.run_startup_plugin_sync)


def test_run_startup_plugin_sync_runs_engine_once_across_seams(monkeypatch):
    """The sync does real work exactly once per launch.

    It is reachable from two seams -- the explicit boot-spine call (before
    ``load_plugin_callbacks``) and the defensive call at the head of the loader.
    The ``_STARTUP_SYNC_DONE`` guard must collapse repeated calls to a single
    invocation of the underlying engine, so an ejected plugin is never synced
    twice in one process.
    """
    calls: list[tuple] = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))

    # Fresh launch: guard down, engine spied.
    monkeypatch.setattr(plugins_pkg, "_STARTUP_SYNC_DONE", False)
    monkeypatch.setattr(sync_startup, "run_startup_sync", _spy)

    # Boot-spine seam, then the loader's defensive seam, then a stray repeat.
    plugins_pkg.run_startup_plugin_sync()
    plugins_pkg.run_startup_plugin_sync()
    plugins_pkg.run_startup_plugin_sync()

    assert len(calls) == 1, "engine must run exactly once per launch"


def test_run_startup_plugin_sync_guard_set_even_on_failure(monkeypatch):
    """A hard-failing sync still flips the guard, so it never retries/loops."""

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(plugins_pkg, "_STARTUP_SYNC_DONE", False)
    monkeypatch.setattr(sync_startup, "run_startup_sync", _boom)

    # Must not raise (best-effort) and must leave the guard set.
    plugins_pkg.run_startup_plugin_sync()
    assert plugins_pkg._STARTUP_SYNC_DONE is True
