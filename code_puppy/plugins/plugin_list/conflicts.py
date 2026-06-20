"""Conflict reviewer for ejected-plugin sidecars (E4.3 -- puppy-viu.4.3).

Backs the ``/plugins conflicts`` subcommand. When the scoped startup sync
(:mod:`code_puppy.plugins.sync_startup`) hits a *true three-way divergence* --
the user edited their ejected copy AND upstream shipped a different change --
it refuses to clobber the user's work. Instead it drops the upstream copy into
a quarantined sidecar under ``<ejected_root>/.plugin_conflicts/<name>/`` (the
``CONFLICT`` branch of :func:`code_puppy.plugins.plugin_sync._apply_one`).

This module is the human-in-the-loop that retires those sidecars:

* ``/plugins conflicts``                     -- list every pending sidecar
* ``/plugins conflicts diff <name>``         -- unified diff (mine vs upstream)
* ``/plugins conflicts accept-upstream <n>`` -- take upstream, advance baseline
* ``/plugins conflicts keep-mine <name>``    -- keep my copy, advance baseline

Baseline semantics (the non-obvious bit)
-----------------------------------------
Both resolutions advance the installed-manifest BASE for that one plugin to the
**upstream (NEW)** hash, then delete the sidecar. That is what makes the
conflict *stay* resolved on the next launch -- it reuses the exact E3 three-way
classifier (:func:`code_puppy.plugins.plugin_sync._classify`):

* **accept-upstream**: copy sidecar -> user dir, then ``BASE := NEW``. Now
  ``CUR == NEW == BASE`` -> next sync is ``NOOP``.
* **keep-mine**: leave the user dir untouched, then ``BASE := NEW``. Now
  ``upstream_change == (NEW != BASE) == False`` and
  ``user_modified == (CUR != BASE) == True`` -> next sync is ``PRESERVE``.

Either way the plugin is never flagged as a conflict again unless a *future*
upstream release changes it anew. The atomic copy reuses
:func:`code_puppy.plugins.plugin_sync.write_plugin_dir`, so accept-upstream gets
the same L4-safe, half-write-proof swap the sync itself uses.

Everything below is pure data-gathering, deterministic filesystem mutation, and
pure string formatting -- no message bus -- so it is unit-testable in isolation.
``register_callbacks.py`` only wires the output to ``emit_*``.
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from pathlib import Path

from code_puppy.plugins.plugin_sync import (
    CONFLICT_DIRNAME,
    manifest_plugin_hashes,
    read_installed_manifest,
    write_installed_manifest,
    write_plugin_dir,
)
from code_puppy.plugins.shipped_manifest import compute_plugin_hash

# Ejected tiers that can carry sidecars, in precedence order (highest first).
_TIER_ORDER = ("project", "user")

# Files that never belong in a textual diff (build noise / binaries-ish).
_DIFF_IGNORE_DIRS = {"__pycache__"}
_DIFF_IGNORE_SUFFIXES = {".pyc", ".pyo", ".pyd"}


# ---------------------------------------------------------------------------
# tier roots (monkeypatchable seams, mirroring ejectable.py)
# ---------------------------------------------------------------------------


def _user_plugins_dir() -> Path | None:
    from code_puppy.plugins import USER_PLUGINS_DIR

    return USER_PLUGINS_DIR


def _project_plugins_dir() -> Path | None:
    from code_puppy.plugins import get_project_plugins_directory

    return get_project_plugins_directory()


def _tier_root(tier: str) -> Path | None:
    if tier == "user":
        return _user_plugins_dir()
    if tier == "project":
        return _project_plugins_dir()
    return None


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Conflict:
    """A single pending sidecar awaiting the user's verdict. Immutable."""

    name: str
    tier: str  # "user" | "project"
    ejected_root: Path
    sidecar_dir: Path  # <root>/.plugin_conflicts/<name>  (upstream copy)
    plugin_dir: Path  # <root>/<name>                    (the user's copy)
    current_hash: str | None  # CUR -- hash of the user's copy (None if absent)
    upstream_hash: str | None  # NEW -- hash of the sidecar (None if absent)
    base_hash: str | None  # BASE -- recorded installed-manifest hash

    @property
    def user_copy_present(self) -> bool:
        return self.current_hash is not None


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of a resolution attempt. ``ok`` gates success/failure messaging."""

    ok: bool
    action: str  # "accept-upstream" | "keep-mine"
    name: str
    tier: str | None
    message: str


# ---------------------------------------------------------------------------
# discovery (pure read)
# ---------------------------------------------------------------------------


def _sidecar_root(ejected_root: Path) -> Path:
    return ejected_root / CONFLICT_DIRNAME


def _iter_sidecar_names(ejected_root: Path) -> list[str]:
    """Plugin names with a pending sidecar under *ejected_root*, sorted."""
    root = _sidecar_root(ejected_root)
    if not root.is_dir():
        return []
    names = [
        item.name
        for item in root.iterdir()
        if item.is_dir() and not item.name.startswith((".", "_"))
    ]
    return sorted(names)


def _hash_if_dir(path: Path) -> str | None:
    return compute_plugin_hash(path) if path.is_dir() else None


def _describe(name: str, tier: str, ejected_root: Path) -> Conflict:
    sidecar = _sidecar_root(ejected_root) / name
    plugin_dir = ejected_root / name
    base = manifest_plugin_hashes(read_installed_manifest(ejected_root)).get(name)
    return Conflict(
        name=name,
        tier=tier,
        ejected_root=ejected_root,
        sidecar_dir=sidecar,
        plugin_dir=plugin_dir,
        current_hash=_hash_if_dir(plugin_dir),
        upstream_hash=_hash_if_dir(sidecar),
        base_hash=base,
    )


def list_conflicts() -> list[Conflict]:
    """Every pending sidecar across the user + project ejected tiers.

    Sorted by tier precedence (project first) then name, so the highest-priority
    copy -- the one actually loaded -- surfaces first.
    """
    out: list[Conflict] = []
    for tier in _TIER_ORDER:
        root = _tier_root(tier)
        if root is None or not Path(root).is_dir():
            continue
        for name in _iter_sidecar_names(Path(root)):
            out.append(_describe(name, tier, Path(root)))
    return out


def find_conflict(name: str) -> list[Conflict]:
    """All pending conflicts matching *name* (one per tier that has a sidecar)."""
    return [c for c in list_conflicts() if c.name == name]


# ---------------------------------------------------------------------------
# resolution (deterministic filesystem mutation)
# ---------------------------------------------------------------------------


def _advance_baseline(ejected_root: Path, name: str, new_hash: str) -> None:
    """Set ``BASE[name] := new_hash`` in the installed manifest, preserving rest.

    Reuses the public read/write manifest helpers so the on-disk schema and the
    package-version stamp (the E3.3 fast-path key) round-trip untouched.
    """
    manifest = read_installed_manifest(ejected_root)
    hashes = manifest_plugin_hashes(manifest)
    pkg_version = manifest.get("package_version") if manifest else None
    hashes[name] = new_hash
    write_installed_manifest(ejected_root, hashes, package_version=pkg_version)


def _remove_sidecar(conflict: Conflict) -> None:
    """Delete the sidecar dir, and the ``.plugin_conflicts`` parent if now empty."""
    shutil.rmtree(conflict.sidecar_dir, ignore_errors=True)
    parent = _sidecar_root(conflict.ejected_root)
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass  # best-effort tidy-up; a stray empty dir is harmless


def _resolved_baseline_hash(conflict: Conflict) -> str | None:
    """The hash to write as the new BASE -- the upstream (NEW) content.

    Prefers the recorded sidecar hash; recomputes from the sidecar dir if that
    snapshot is stale. Returns ``None`` only when the sidecar has vanished.
    """
    if conflict.upstream_hash is not None:
        return conflict.upstream_hash
    return _hash_if_dir(conflict.sidecar_dir)


def accept_upstream(conflict: Conflict) -> ResolveResult:
    """Adopt the upstream copy: overwrite the user's dir, then advance BASE.

    Atomic (reuses :func:`write_plugin_dir`), so an interruption leaves either
    the old copy or the new copy -- never a half-written plugin. After this the
    three-way state is ``CUR == NEW == BASE`` -> a clean ``NOOP`` next launch.
    """
    if not conflict.sidecar_dir.is_dir():
        return ResolveResult(
            ok=False,
            action="accept-upstream",
            name=conflict.name,
            tier=conflict.tier,
            message=f"No upstream sidecar found for '{conflict.name}'.",
        )
    new_hash = _resolved_baseline_hash(conflict)
    try:
        write_plugin_dir(conflict.sidecar_dir, conflict.plugin_dir)
    except Exception as exc:  # never crash the app on a bad copy
        return ResolveResult(
            ok=False,
            action="accept-upstream",
            name=conflict.name,
            tier=conflict.tier,
            message=f"Failed to apply upstream copy for '{conflict.name}': {exc}",
        )
    # BASE := NEW (recompute from the freshly written copy if the snapshot was
    # stale -- it now equals the upstream content we just wrote).
    if new_hash is None:
        new_hash = compute_plugin_hash(conflict.plugin_dir)
    _advance_baseline(conflict.ejected_root, conflict.name, new_hash)
    _remove_sidecar(conflict)
    return ResolveResult(
        ok=True,
        action="accept-upstream",
        name=conflict.name,
        tier=conflict.tier,
        message=(
            f"Accepted upstream for '{conflict.name}' ({conflict.tier} tier); "
            "your copy was replaced and the baseline advanced."
        ),
    )


def keep_mine(conflict: Conflict) -> ResolveResult:
    """Keep the user's copy untouched, but advance BASE to upstream (NEW).

    The user dir is never modified. Advancing the baseline *acknowledges* the
    upstream change so the next launch classifies this as ``PRESERVE`` (user
    owns it) rather than re-raising the conflict.
    """
    new_hash = _resolved_baseline_hash(conflict)
    if new_hash is None:
        return ResolveResult(
            ok=False,
            action="keep-mine",
            name=conflict.name,
            tier=conflict.tier,
            message=f"No upstream sidecar found for '{conflict.name}'.",
        )
    _advance_baseline(conflict.ejected_root, conflict.name, new_hash)
    _remove_sidecar(conflict)
    return ResolveResult(
        ok=True,
        action="keep-mine",
        name=conflict.name,
        tier=conflict.tier,
        message=(
            f"Kept your copy of '{conflict.name}' ({conflict.tier} tier); "
            "the upstream change was acknowledged (baseline advanced)."
        ),
    )


# ---------------------------------------------------------------------------
# diff (pure read -> string)
# ---------------------------------------------------------------------------


def _iter_diff_files(plugin_dir: Path) -> dict[str, Path]:
    """Map ``rel_posix_path -> file`` for every text-diffable file under a dir."""
    out: dict[str, Path] = {}
    if not plugin_dir.is_dir():
        return out
    for path in plugin_dir.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(plugin_dir).parts
        if any(part in _DIFF_IGNORE_DIRS for part in rel_parts):
            continue
        if path.suffix in _DIFF_IGNORE_SUFFIXES:
            continue
        out[path.relative_to(plugin_dir).as_posix()] = path
    return out


def _read_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["<binary or unreadable file>\n"]
    return text.splitlines(keepends=True)


def diff_conflict(conflict: Conflict) -> str:
    """A unified diff of the user's copy (``mine``) vs the upstream sidecar.

    Walks the union of files in both trees, so additions and deletions show up,
    not just edits. Pure: reads the filesystem and returns a string.
    """
    if not conflict.sidecar_dir.is_dir():
        return f"No upstream sidecar found for '{conflict.name}'."

    mine = _iter_diff_files(conflict.plugin_dir)
    upstream = _iter_diff_files(conflict.sidecar_dir)
    all_rel = sorted(set(mine) | set(upstream))

    chunks: list[str] = []
    for rel in all_rel:
        a_lines = _read_lines(mine[rel]) if rel in mine else []
        b_lines = _read_lines(upstream[rel]) if rel in upstream else []
        if a_lines == b_lines:
            continue
        diff = difflib.unified_diff(
            a_lines,
            b_lines,
            fromfile=f"mine/{rel}",
            tofile=f"upstream/{rel}",
        )
        chunks.append("".join(diff))

    if not chunks:
        return (
            f"No textual differences between your copy and upstream for "
            f"'{conflict.name}' (they may differ only in ignored files)."
        )
    header = (
        f"Diff for '{conflict.name}' ({conflict.tier} tier) -- mine vs upstream:\n\n"
    )
    return header + "\n".join(chunks)


# ---------------------------------------------------------------------------
# pure string formatting (no message bus)
# ---------------------------------------------------------------------------


def _mod_note(conflict: Conflict) -> str:
    if conflict.current_hash is None:
        return "your copy is missing"
    if conflict.base_hash is None:
        return "no recorded baseline"
    return (
        "you edited it" if conflict.current_hash != conflict.base_hash else "unmodified"
    )


def format_conflict_list(conflicts: list[Conflict]) -> str:
    """Render ``/plugins conflicts`` output."""
    if not conflicts:
        return (
            "No pending plugin conflicts. Your ejected plugins are all "
            "reconciled with upstream."
        )

    lines = [f"Pending plugin conflicts ({len(conflicts)})", ""]
    for c in conflicts:
        lines.append(f"   {c.name}  -> {c.tier} tier ({_mod_note(c)})")
    lines.extend(
        [
            "",
            "Resolve each one with:",
            "   /plugins conflicts diff <name>            show mine vs upstream",
            "   /plugins conflicts accept-upstream <name> take the upstream copy",
            "   /plugins conflicts keep-mine <name>       keep your edited copy",
        ]
    )
    return "\n".join(lines)
