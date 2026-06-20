"""Wire the scoped sync engine into process startup (E3.3 — bead puppy-viu.3.3).

This is the thin **orchestration** layer that runs :mod:`plugin_sync` once per
launch, *before* the idempotent plugin load (see ``load_plugin_callbacks`` in
:mod:`code_puppy.plugins`). The pure policy lives in :mod:`plugin_sync`; this
module only supplies the real-world inputs — which roots are ejected, what the
current package version is, and where conflict warnings go — and guarantees the
whole thing **never blocks startup**.

Why run *before* the load
-------------------------
The whole point of the scoped sync is that an ejected plugin's reconciled copy
is what gets imported *this* launch. So the sync has to land on disk before any
tier is imported. ``load_plugin_callbacks`` is itself idempotent (guarded by
``_PLUGINS_LOADED``), so calling this at the very top of that function gives us
the "once per launch, before load" guarantee for free.

The three acceptance guarantees, by construction
------------------------------------------------
* **Runs once at startup before load** — invoked from the top of
  ``load_plugin_callbacks`` ahead of tier resolution.
* **Unchanged ``package_version`` short-circuits** — :func:`_sync_one_root`
  bails the moment the installed manifest's stamped version matches the running
  wheel, so a normal relaunch hashes nothing.
* **Conflicts emit non-blocking message-bus warnings** — the aggregated
  conflict notice is routed through :func:`code_puppy.messaging.emit_warning`;
  every failure path is swallowed so a sync hiccup can never take down boot.

Scope guard (only ejected plugins are touched)
----------------------------------------------
The defining risk of running sync over a user's plugin dir is clobbering a
plugin they *authored themselves* (which merely happens to live in the same
directory as an ejected builtin). :func:`_root_scope` closes that: the scope is
``set(BASE) | (on-disk names ∩ shipped builtin names)``. A user-authored plugin
whose name is **not** a shipped builtin and has **no** recorded baseline is
never in scope — never hashed, never written, never deleted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Mapping

from code_puppy.plugins.plugin_sync import (
    apply_update,
    compute_current_hashes,
    manifest_plugin_hashes,
    plan_update,
    read_installed_manifest,
)
from code_puppy.plugins.shipped_manifest import (
    iter_builtin_plugin_dirs,
    load_shipped_manifest,
)

logger = logging.getLogger(__name__)

__all__ = ["run_startup_sync"]


def run_startup_sync(
    *,
    roots: Iterable[Path] | None = None,
    source_root: Path | None = None,
    package_version: str | None = None,
    emit_warning: Callable[[str], None] | None = None,
) -> None:
    """Reconcile every ejected root with upstream. Best-effort, never blocking.

    Args:
        roots: The ejected/overlaid roots to sync (user tier, project tier).
            When ``None`` they are discovered lazily from the loader's own
            ``USER_PLUGINS_DIR`` + project plugins dir, so the single source of
            truth for those paths stays in :mod:`code_puppy.plugins`.
        source_root: The in-package canonical plugins dir (WRITE/CONFLICT copy
            from here). Defaults to this module's own package directory.
        package_version: The running wheel version, stamped into the installed
            manifest and compared for the fast-path. Defaults to
            ``code_puppy.__version__``.
        emit_warning: Sink for the aggregated conflict notice. Defaults to the
            message bus (:func:`code_puppy.messaging.emit_warning`); falls back
            to the module logger if the bus is unavailable.

    This wrapper swallows *all* exceptions: a failed sync must degrade to "run
    the builtins straight from the wheel", never to a crashed boot.
    """
    try:
        source_root = (
            Path(source_root) if source_root else Path(__file__).resolve().parent
        )
        new_manifest = load_shipped_manifest(source_root)
        if not new_manifest:
            # Bare source checkout that was never packaged: nothing shipped to
            # reconcile against, so there is nothing to do. Not an error.
            logger.debug("plugin_sync: no shipped manifest; skipping startup sync")
            return

        new_hashes = manifest_plugin_hashes(new_manifest)
        pkg_version = package_version or _current_package_version()
        warn = _resolve_emit_warning(emit_warning)
        target_roots = list(roots) if roots is not None else _default_ejected_roots()

        for root in target_roots:
            _sync_one_root(Path(root), source_root, new_hashes, pkg_version, warn)
    except Exception as exc:  # never let a sync failure block startup
        logger.warning("plugin_sync: startup sync aborted (%s)", exc, exc_info=True)


def _sync_one_root(
    ejected_root: Path,
    source_root: Path,
    new_hashes: Mapping[str, str],
    package_version: str | None,
    emit_warning: Callable[[str], None] | None,
) -> None:
    """Plan + apply the scoped sync for a single ejected root.

    Isolated per root so one unreadable/unwritable tier can never abort the
    other. Honors the ``package_version`` fast-path and the ejected-only scope
    guard described in the module docstring.
    """
    if not ejected_root.is_dir():
        return  # tier not present -> nothing was ever ejected here

    base_manifest = read_installed_manifest(ejected_root)

    # Fast-path: we already synced this exact wheel into this root. A normal
    # relaunch (no upgrade) hits this and hashes nothing.
    if (
        base_manifest is not None
        and base_manifest.get("package_version") == package_version
    ):
        logger.debug(
            "plugin_sync: %s already synced for version %s; fast-path skip",
            ejected_root,
            package_version,
        )
        return

    base_hashes = manifest_plugin_hashes(base_manifest)
    on_disk = _scan_ejected_names(ejected_root)
    scope = _root_scope(base_hashes, new_hashes, on_disk)
    if not scope:
        # Nothing ejected here (the dir holds only user-authored plugins, or is
        # empty). Touch nothing — not even a manifest stamp.
        return

    cur_hashes = compute_current_hashes(ejected_root, scope)
    plan = plan_update(base_hashes, new_hashes, cur_hashes, scope=scope)
    apply_update(
        plan,
        ejected_root,
        source_root,
        new_hashes,
        package_version=package_version,
        emit_warning=emit_warning,
    )


def _root_scope(
    base_hashes: Mapping[str, str],
    new_hashes: Mapping[str, str],
    on_disk_names: Iterable[str],
) -> set[str]:
    """The ejected-only scope for one root. **The user-plugin safety guard.**

    A plugin is in scope iff it has a recorded baseline (we ejected/managed it
    before) OR it physically exists on disk *and* is the name of a shipped
    builtin (a hand-dropped ejected copy). A purely user-authored plugin —
    name not in the shipped manifest, no baseline — is deliberately excluded,
    so the sync can never hash, overwrite, or delete it.
    """
    shipped = set(new_hashes)
    return set(base_hashes) | (set(on_disk_names) & shipped)


def _scan_ejected_names(ejected_root: Path) -> set[str]:
    """Plugin directory names physically present under *ejected_root*.

    Reuses :func:`iter_builtin_plugin_dirs` so the "what counts as a plugin
    dir" rule (entry-point file present; ``_``/``.`` prefixes skipped) stays in
    one place — the dot-prefixed manifest and ``.plugin_conflicts`` sidecar are
    excluded for free.
    """
    return {p.name for p in iter_builtin_plugin_dirs(ejected_root)}


def _default_ejected_roots() -> list[Path]:
    """Discover the user + project ejected roots from the loader.

    Imported lazily to avoid a module-import cycle with
    :mod:`code_puppy.plugins` (which imports *this* module at the top of
    ``load_plugin_callbacks``). Keeps the path definitions DRY — the loader
    remains the single source of truth for where plugins live.
    """
    from code_puppy.plugins import USER_PLUGINS_DIR, get_project_plugins_directory

    roots: list[Path] = [USER_PLUGINS_DIR]
    project_dir = get_project_plugins_directory()
    if project_dir is not None:
        roots.append(project_dir)
    return roots


def _resolve_emit_warning(
    emit_warning: Callable[[str], None] | None,
) -> Callable[[str], None] | None:
    """Default the conflict sink to the message bus, gracefully.

    Returns the caller's sink unchanged when provided. Otherwise routes to
    :func:`code_puppy.messaging.emit_warning`; if that import fails (e.g. a
    stripped-down environment), returns ``None`` so :func:`apply_update` falls
    back to its logger — conflicts are surfaced *somewhere*, never swallowed.
    """
    if emit_warning is not None:
        return emit_warning
    try:
        from code_puppy.messaging import emit_warning as bus_emit_warning

        return bus_emit_warning
    except Exception:  # pragma: no cover - defensive import guard
        return None


def _current_package_version() -> str | None:
    """Best-effort running wheel version for the fast-path / manifest stamp."""
    try:
        from code_puppy import __version__

        return __version__
    except Exception:  # pragma: no cover - defensive import guard
        return None
