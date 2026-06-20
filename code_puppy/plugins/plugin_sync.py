"""Scoped, hash-aware plugin sync engine (E3.1 — bead puppy-viu.3.1).

This module implements 27g.2's **BASE / NEW / CUR** three-way model, *scoped to
the ejected/overlaid slice only* (the lazy-hybrid from ADR-001 / 27g.4). It does
two things and nothing else:

* :func:`plan_update` — a **pure** planning pass. Given three hash maps (no
  filesystem access of its own) it returns an ordered, side-effect-free
  :class:`UpdatePlan`. This is what makes the policy unit-testable in isolation.
* :func:`apply_update` — the **atomic** executor. It copies upstream plugin
  directories into place with a temp-dir + ``os.replace`` swap, drops conflict
  sidecars *without* touching the user's copy, and rewrites the installed
  manifest **last** so an interrupted run self-heals on the next pass.

Granularity
-----------
E3.2's shipped manifest (:mod:`code_puppy.plugins.shipped_manifest`) hashes each
plugin at **directory** granularity — one ``sha256-nl`` per plugin dir. This
engine mirrors that: a "managed path" is a *whole plugin directory*, keyed by
plugin name. So the 12-row file-level decision table from
``docs/PLUGIN_HASH_AWARE_UPDATES.md`` collapses onto plugin-directory units:

============================  =========================================
Three-way state               Action (per plugin dir)
============================  =========================================
BASE==NEW==CUR                NOOP
upstream changed, user clean  WRITE   (deliver upstream)
user changed, upstream clean  PRESERVE (keep the user's copy)
both -> same content (CUR==NEW)  ADOPT  (no write; advance baseline)
both -> different (true 3-way)   CONFLICT (sidecar; never clobber)
new to scope, disk absent     WRITE   (bootstrap eject)
new to scope, CUR==NEW        ADOPT   (adopt identical copy)
new to scope, CUR!=NEW        CONFLICT
no longer shipped, CUR==BASE  DELETE
no longer shipped, CUR!=BASE  KEEP_ORPHAN (preserve user work)
============================  =========================================

Scope — *only ejected plugins are touched*
------------------------------------------
The sync never reaches outside the ejected set. By default the scope is
``set(base) | {names present on disk}`` — i.e. exactly the plugins we have a
recorded baseline for or that physically live in the ejected root. The full
shipped manifest (every builtin) is consulted **only** for the NEW hashes of
names already in scope, so a plugin that was never ejected is never written,
deleted, or even considered. Callers may pass an explicit ``scope`` to narrow it
further.

Newline-normalized hashing (L4) comes for free: CUR is computed with
:func:`code_puppy.plugins.shipped_manifest.compute_plugin_hash`, the very same
primitive the build-time NEW hashes use, so a Linux-built wheel and a
Windows-edited checkout agree on identical content.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping

from code_puppy.plugins.shipped_manifest import (
    HASH_ALGORITHM,
    MANIFEST_VERSION,
    compute_plugin_hash,
)

logger = logging.getLogger(__name__)

__all__ = [
    "INSTALLED_MANIFEST_FILENAME",
    "CONFLICT_DIRNAME",
    "Action",
    "PluginOp",
    "UpdatePlan",
    "plan_update",
    "apply_update",
    "compute_current_hashes",
    "read_installed_manifest",
    "write_installed_manifest",
    "manifest_plugin_hashes",
]

# The installed (BASE) manifest lives in the ejected root. The leading dot keeps
# it out of the user/project plugin loaders, which skip dot-prefixed entries —
# so it is never mistaken for a plugin directory.
INSTALLED_MANIFEST_FILENAME = ".code_puppy_plugins_manifest.json"

# Conflict sidecars (upstream copies written beside a user-modified plugin) are
# quarantined under this dot-prefixed dir for the same loader-safety reason.
CONFLICT_DIRNAME = ".plugin_conflicts"

# Build noise that must never be copied into an ejected plugin — keeps a freshly
# written CUR byte-identical (after newline-normalization) to NEW so the very
# next sync is a guaranteed no-op.
_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "*.pyd", ".DS_Store"
)


class Action(str, Enum):
    """The resolved action for a single managed plugin directory.

    ``str`` mixin so ops serialize/compare readably in logs and tests.
    """

    NOOP = "noop"  # nothing changed anywhere
    WRITE = "write"  # deliver upstream (clean update or bootstrap add)
    ADOPT = "adopt"  # disk already equals NEW; no write, just advance baseline
    PRESERVE = "preserve"  # user owns it, upstream unchanged; leave alone
    CONFLICT = "conflict"  # true 3-way; keep user copy, drop upstream sidecar
    DELETE = "delete"  # upstream dropped it, user untouched; remove
    KEEP_ORPHAN = "keep_orphan"  # upstream dropped it, user modified; preserve


# Actions whose plugin still belongs in the advanced installed manifest.
# DELETE / KEEP_ORPHAN intentionally fall out of the manifest (the latter
# becomes an untracked user file we never touch again).
_BASELINE_RETAINED = frozenset(
    {Action.NOOP, Action.WRITE, Action.ADOPT, Action.PRESERVE, Action.CONFLICT}
)


@dataclass(frozen=True)
class PluginOp:
    """One resolved decision for one plugin directory. Immutable by design."""

    name: str
    action: Action
    base_hash: str | None  # BASE (installed manifest), None if newly in scope
    new_hash: str | None  # NEW (shipped manifest), None if no longer shipped
    cur_hash: str | None  # CUR (live on disk), None if the dir is absent


@dataclass(frozen=True)
class UpdatePlan:
    """The full, ordered, side-effect-free result of :func:`plan_update`."""

    ops: tuple[PluginOp, ...] = field(default_factory=tuple)

    def by_action(self, action: Action) -> list[PluginOp]:
        return [op for op in self.ops if op.action == action]

    @property
    def conflicts(self) -> list[str]:
        return [op.name for op in self.ops if op.action == Action.CONFLICT]

    @property
    def writes(self) -> list[str]:
        return [op.name for op in self.ops if op.action == Action.WRITE]

    @property
    def deletes(self) -> list[str]:
        return [op.name for op in self.ops if op.action == Action.DELETE]

    def is_noop(self) -> bool:
        """True when applying this plan would touch nothing on disk.

        ADOPT does not write files (disk already matches NEW); it only advances
        the baseline, so it counts as a no-op for *disk* purposes.
        """
        return all(
            op.action
            in (Action.NOOP, Action.ADOPT, Action.PRESERVE, Action.KEEP_ORPHAN)
            for op in self.ops
        )


def plan_update(
    base_hashes: Mapping[str, str],
    new_hashes: Mapping[str, str],
    cur_hashes: Mapping[str, str | None],
    scope: Iterable[str] | None = None,
) -> UpdatePlan:
    """Resolve the three-way status for every in-scope plugin. **Pure.**

    Args:
        base_hashes: BASE — plugin name -> hash from the installed manifest
            (what we last wrote to the ejected root). Empty/missing == bootstrap.
        new_hashes: NEW — plugin name -> hash from the shipped manifest (this
            release). May contain *every* builtin; only in-scope names are read.
        cur_hashes: CUR — plugin name -> live on-disk hash, or ``None`` when the
            ejected directory is absent. Computed by :func:`compute_current_hashes`.
        scope: The ejected/overlaid set to consider. When ``None`` it defaults to
            ``set(base_hashes) | {names with a non-None cur hash}`` — i.e. only
            plugins we already manage. This is the guard that keeps the sync from
            ever touching a non-ejected builtin.

    Returns:
        An :class:`UpdatePlan`. No filesystem access, no writes — safe to call,
        inspect, log, or discard.
    """
    if scope is None:
        scope = set(base_hashes) | {
            name for name, h in cur_hashes.items() if h is not None
        }
    else:
        scope = set(scope)

    ops: list[PluginOp] = []
    for name in sorted(scope):
        b = base_hashes.get(name)
        n = new_hashes.get(name)
        cur = cur_hashes.get(name)
        ops.append(PluginOp(name, _classify(b, n, cur), b, n, cur))

    return UpdatePlan(ops=tuple(ops))


def _classify(b: str | None, n: str | None, cur: str | None) -> Action:
    """Map a single (BASE, NEW, CUR) triple to its :class:`Action`.

    This is the literal encoding of the collapsed decision table in this
    module's docstring — the one place the policy lives.
    """
    # ---- no longer shipped (NEW absent) ----
    if n is None:
        if cur is None:
            return Action.NOOP  # already gone
        if cur == b:
            return Action.DELETE  # upstream removed, user untouched
        return Action.KEEP_ORPHAN  # upstream removed, user modified -> preserve

    # ---- newly in scope (no recorded BASE) ----
    if b is None:
        if cur is None:
            return Action.WRITE  # bootstrap: write upstream
        if cur == n:
            return Action.ADOPT  # identical already on disk
        return Action.CONFLICT  # user authored something different

    # ---- present in both BASE and NEW ----
    user_modified = cur != b
    upstream_change = n != b
    if not upstream_change and not user_modified:
        return Action.NOOP
    if upstream_change and not user_modified:
        return Action.WRITE  # clean upstream update
    if not upstream_change and user_modified:
        return Action.PRESERVE  # user owns it
    if cur == n:
        return Action.ADOPT  # both converged on the same content
    return Action.CONFLICT  # true 3-way divergence


def compute_current_hashes(
    ejected_root: Path,
    scope: Iterable[str],
) -> dict[str, str | None]:
    """Compute live CUR hashes for the named plugins under *ejected_root*.

    Returns ``{name: hash}`` with ``None`` for any plugin whose directory is
    absent. Uses the shipped-manifest primitive so CUR is newline-normalized
    identically to NEW (the L4 mitigation). This is the only place plan inputs
    touch the filesystem — :func:`plan_update` itself stays pure.
    """
    ejected_root = Path(ejected_root)
    out: dict[str, str | None] = {}
    for name in scope:
        plugin_dir = ejected_root / name
        out[name] = compute_plugin_hash(plugin_dir) if plugin_dir.is_dir() else None
    return out


def apply_update(
    plan: UpdatePlan,
    ejected_root: Path,
    source_root: Path,
    new_hashes: Mapping[str, str],
    *,
    package_version: str | None = None,
    emit_warning: Callable[[str], None] | None = None,
) -> dict:
    """Execute *plan* atomically, then rewrite the installed manifest **last**.

    Args:
        plan: The plan from :func:`plan_update`.
        ejected_root: The on-disk root holding the ejected plugin directories.
        source_root: The in-package canonical plugins dir (the lazy-hybrid keeps
            the original copy in the wheel); WRITE/CONFLICT copy from here.
        new_hashes: NEW hashes used to advance the baseline (BASE := NEW).
        package_version: Stamped into the installed manifest for the E3.3
            fast-path short-circuit.
        emit_warning: Optional sink for the aggregated conflict notice. Falls
            back to the module logger so this works in non-TTY/CI runs.

    Returns:
        The freshly written installed-manifest dict.

    Safety: each plugin dir is staged in a sibling temp dir and swapped in with
    ``os.replace``; the manifest is written only after every plugin op succeeds,
    so an interruption leaves a recomputable CUR and the next run converges.
    """
    ejected_root = Path(ejected_root)
    source_root = Path(source_root)
    ejected_root.mkdir(parents=True, exist_ok=True)

    conflicts: list[str] = []
    for op in plan.ops:
        try:
            _apply_one(op, ejected_root, source_root)
        except Exception as exc:  # never crash the app on a single bad plugin
            logger.error(
                "plugin_sync: failed to apply %s for '%s': %s",
                op.action.value,
                op.name,
                exc,
            )
            continue
        if op.action == Action.CONFLICT:
            conflicts.append(op.name)

    if conflicts:
        msg = (
            f"{len(conflicts)} ejected plugin(s) had conflicting changes; "
            f"upstream copies were written under '{CONFLICT_DIRNAME}/'. "
            f"Affected: {', '.join(sorted(conflicts))}."
        )
        (emit_warning or logger.warning)(msg)

    # Advance the baseline LAST: BASE := NEW for every retained plugin.
    advanced = {
        op.name: new_hashes.get(op.name, op.new_hash)
        for op in plan.ops
        if op.action in _BASELINE_RETAINED and new_hashes.get(op.name) is not None
    }
    write_installed_manifest(ejected_root, advanced, package_version=package_version)
    return _build_installed_manifest(advanced, package_version)


def _apply_one(op: PluginOp, ejected_root: Path, source_root: Path) -> None:
    """Perform the single filesystem side effect for *op* (or none)."""
    target = ejected_root / op.name
    if op.action == Action.WRITE:
        _write_plugin_dir(source_root / op.name, target)
    elif op.action == Action.CONFLICT:
        # NON-DESTRUCTIVE: leave the user's dir byte-for-byte; drop the upstream
        # version into the quarantined sidecar for the E4 conflicts reviewer.
        sidecar = ejected_root / CONFLICT_DIRNAME / op.name
        _write_plugin_dir(source_root / op.name, sidecar)
    elif op.action == Action.DELETE:
        shutil.rmtree(target, ignore_errors=True)
    # NOOP / ADOPT / PRESERVE / KEEP_ORPHAN: leave the disk exactly as-is.


def _write_plugin_dir(src: Path, target: Path) -> None:
    """Atomically replace *target* with a clean copy of *src*.

    Copies into a sibling temp dir (same filesystem), then swaps with
    ``os.replace`` so a reader never sees a half-written plugin directory.
    """
    if not src.is_dir():
        raise FileNotFoundError(f"source plugin dir not found: {src}")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staged = (
        Path(tempfile.mkdtemp(prefix=f".tmp_{target.name}_", dir=parent)) / target.name
    )
    try:
        shutil.copytree(src, staged, ignore=_COPY_IGNORE)
        _atomic_replace_tree(staged, target)
    finally:
        # Clean up the temp wrapper dir whether or not the swap happened.
        shutil.rmtree(staged.parent, ignore_errors=True)


def _atomic_replace_tree(staged: Path, target: Path) -> None:
    """Swap *staged* into *target*, moving any existing target aside first.

    ``os.replace`` cannot overwrite a non-empty directory on Windows, so we move
    the old target to a backup, drop the new one in, then delete the backup. On
    failure the backup is restored — the target is never left missing.
    """
    backup: Path | None = None
    if target.exists():
        backup = target.with_name(
            f"{target.name}.bak_{os.getpid()}_{os.urandom(4).hex()}"
        )
        os.replace(target, backup)
    try:
        os.replace(staged, target)
    except Exception:
        if backup is not None and backup.exists():
            os.replace(backup, target)  # restore the original
        raise
    if backup is not None and backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


# ---------------------------------------------------------------------------
# Installed (BASE) manifest — read / write / round-trip
# ---------------------------------------------------------------------------


def _build_installed_manifest(
    plugin_hashes: Mapping[str, str],
    package_version: str | None,
) -> dict:
    """Assemble the installed-manifest dict (same schema as the shipped one)."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "algorithm": HASH_ALGORITHM,
        "package_version": package_version,
        "plugins": dict(sorted(plugin_hashes.items())),
    }


def manifest_plugin_hashes(manifest: Mapping | None) -> dict[str, str]:
    """Extract the ``{name: hash}`` mapping from a manifest dict.

    Tolerant of ``None`` (missing manifest -> empty mapping, i.e. bootstrap) and
    of a malformed ``plugins`` value, so callers never have to special-case it.
    """
    if not manifest:
        return {}
    plugins = manifest.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    return {str(k): str(v) for k, v in plugins.items()}


def read_installed_manifest(ejected_root: Path) -> dict | None:
    """Read the installed (BASE) manifest from *ejected_root*.

    Returns the parsed dict, or ``None`` when it is absent or corrupt — callers
    treat ``None`` as "no baseline yet" (bootstrap), never as an error.
    """
    path = Path(ejected_root) / INSTALLED_MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("plugin_sync: corrupt installed manifest at %s (%s)", path, exc)
        return None


def write_installed_manifest(
    ejected_root: Path,
    plugin_hashes: Mapping[str, str],
    package_version: str | None = None,
) -> Path:
    """Atomically write the installed (BASE) manifest into *ejected_root*.

    Writes to a temp file in the same directory and ``os.replace``-s it into
    place so a reader never sees a truncated manifest. Returns the written path.
    """
    ejected_root = Path(ejected_root)
    ejected_root.mkdir(parents=True, exist_ok=True)
    path = ejected_root / INSTALLED_MANIFEST_FILENAME
    manifest = _build_installed_manifest(plugin_hashes, package_version)
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        prefix=".tmp_manifest_", dir=str(ejected_root), suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path
