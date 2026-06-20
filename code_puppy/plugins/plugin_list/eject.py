"""Opt-in eject action for builtin plugins (E4.1 -- puppy-viu.4.1).

Backs the ``/plugins eject <name>`` subcommand -- the *action* the three E4.2/
E4.3 read-only commands (``list-ejectable`` / ``show`` / ``conflicts``) keep
advertising. It copies a builtin plugin (the canonical copy kept in the wheel)
out to the user tier on demand and records its content-hash *baseline* in the
ejected root's installed manifest, so that:

* the ejected copy **wins the tier collision** and suppresses the builtin on the
  next launch (the loader's precedence rule in :mod:`code_puppy.plugins`), and
* the scoped startup sync (:mod:`code_puppy.plugins.sync_startup`) classifies it
  as ``NOOP`` -- ``BASE == NEW == CUR`` -- so it is never clobbered or re-written
  until a *future* upstream release actually changes it.

Dependency clusters (closes L5)
-------------------------------
A builtin may import *another* builtin with an **absolute** cross-plugin import
(``from code_puppy.plugins.other import x``) -- per the contributing convention,
cross-plugin deps stay absolute. Ejecting such a plugin *alone* would leave a
half-relocated slice: you could edit the plugin but not the sibling it leans on.

So eject is **cluster-aware**: it computes the whole **dependency cluster** --
the transitive closure of a plugin's outgoing cross-plugin builtin imports --
and *refuses a partial eject*. If you ask to eject ``A`` while it still imports a
**non-ejected** sibling ``B``, the eject is **refused with a clear message** and
an explicit opt-in to eject the whole cluster (``cluster=True`` /
``/plugins eject A --cluster``). Only when every sibling is already ejected (or
when you opt into the cluster) does the eject proceed -- so the ejected slice is
always a self-contained, editable unit and we never externalize a *subset* of a
cluster (closes liability L5). A standalone plugin (no cross-plugin imports)
ejects normally, no opt-in required.

Everything here is pure data-gathering + deterministic filesystem mutation +
pure string formatting -- no message bus -- so it is unit-testable in isolation.
``register_callbacks.py`` only wires the result to ``emit_*``. Tier roots are
reused from :mod:`ejectable` (one source of truth) and the atomic copy + the
manifest helpers are reused from :mod:`code_puppy.plugins.plugin_sync` (DRY).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from code_puppy.plugins.plugin_sync import (
    manifest_plugin_hashes,
    read_installed_manifest,
    write_installed_manifest,
    write_plugin_dir,
)
from code_puppy.plugins.shipped_manifest import compute_plugin_hash
from code_puppy.plugins.sync_startup import _current_package_version

from . import ejectable

# The two owned tiers an eject can target. ``user`` is the default (applies to
# every project); ``project`` pins the copy to the current repo.
_EJECT_TIERS = ("user", "project")

_CROSS_PLUGIN_PREFIX = "code_puppy.plugins."


# ---------------------------------------------------------------------------
# dependency-cluster discovery (pure read)
# ---------------------------------------------------------------------------


def _plugin_name_from_module(module: str | None) -> str | None:
    """Extract ``<name>`` from a ``code_puppy.plugins.<name>[...]`` module path.

    Returns ``None`` for any module that is not a cross-plugin reference, so a
    core import (``code_puppy.messaging``) or a stdlib import is never mistaken
    for a plugin dependency.
    """
    if not module or not module.startswith(_CROSS_PLUGIN_PREFIX):
        return None
    rest = module[len(_CROSS_PLUGIN_PREFIX) :]
    head = rest.split(".", 1)[0]
    return head or None


def _cross_plugin_deps(
    plugin_dir: Path, builtins: set[str], self_name: str
) -> set[str]:
    """Builtin plugin names *plugin_dir* imports via an absolute cross-plugin path.

    AST-scans every ``.py`` under the plugin for ``from code_puppy.plugins.X
    import ...`` and ``import code_puppy.plugins.X`` where ``X`` is a *different*
    builtin. A self-referential import is ignored (those are relative by
    convention anyway), as is anything that is not a known builtin.
    """
    deps: set[str] = set()
    for py in plugin_dir.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue  # never let one unparseable file abort cluster discovery
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                name = _plugin_name_from_module(node.module)
                if name and name in builtins and name != self_name:
                    deps.add(name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = _plugin_name_from_module(alias.name)
                    if name and name in builtins and name != self_name:
                        deps.add(name)
    return deps


def resolve_cluster(name: str) -> list[str]:
    """The dependency cluster to eject when the user asks for *name*, sorted.

    The transitive closure of *name*'s outgoing cross-plugin builtin imports --
    i.e. *name* plus everything it (directly or indirectly) leans on. A plugin
    with no cross-plugin imports clusters to just itself. A name that is not a
    builtin returns an empty list (nothing ejectable).
    """
    builtins = ejectable.builtin_names()
    if name not in builtins:
        return []

    builtin_root = ejectable.get_builtin_plugins_dir()
    seen: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for dep in _cross_plugin_deps(builtin_root / current, builtins, current):
            if dep not in seen:
                stack.append(dep)
    return sorted(seen)


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EjectResult:
    """Outcome of an eject attempt. ``ok`` gates success/failure messaging."""

    ok: bool
    name: str
    target_tier: str
    cluster: tuple[str, ...] = field(default_factory=tuple)
    ejected: tuple[str, ...] = field(default_factory=tuple)  # newly written
    skipped: tuple[str, ...] = field(default_factory=tuple)  # already present
    # Non-ejected siblings that forced a refusal (empty unless ``refused``).
    requires_cluster: tuple[str, ...] = field(default_factory=tuple)
    # True when the eject was refused *because* of a partial cluster -- distinct
    # from a hard error (unknown plugin, bad target). Lets the CLI nudge with a
    # warning + the opt-in hint instead of a flat error.
    refused: bool = False
    message: str = ""


# ---------------------------------------------------------------------------
# the eject action (deterministic filesystem mutation)
# ---------------------------------------------------------------------------


def _tier_root(tier: str) -> Path | None:
    if tier == "user":
        return ejectable._user_plugins_dir()
    if tier == "project":
        return ejectable._project_plugins_dir()
    return None


def _record_baselines(root: Path, names: list[str]) -> None:
    """Record ``BASE := NEW`` for each freshly ejected plugin, preserving the rest.

    Reuses the shipped (NEW) hash so the next startup sync sees
    ``BASE == NEW == CUR`` -> ``NOOP``. Any other recorded baselines round-trip
    untouched -- we only *add* the newly ejected names.

    The ``package_version`` stamp is set to the **running** wheel version
    (``code_puppy.__version__`` via :func:`_current_package_version`) -- the same
    value the startup sync's fast-path compares against
    (``base_manifest.package_version == running_version``). Stamping it here lets
    the very next restart short-circuit the scoped sync instead of doing a full
    hash pass (closes puppy-0gg). On a *first* eject the existing manifest is
    absent, so the old ``manifest.get(...)`` fell back to ``None`` and defeated
    the fast-path for one restart; reusing the exact fast-path source of truth
    keeps the two ends from drifting. We fall back to any existing stamp only if
    version detection itself fails (defensive).
    """
    manifest = read_installed_manifest(root)
    hashes = manifest_plugin_hashes(manifest)
    pkg_version = _current_package_version()
    if pkg_version is None and manifest:
        pkg_version = manifest.get("package_version")
    for name in names:
        base = ejectable._shipped_hash(name)
        if base is None:
            # No shipped manifest and no builtin dir hash -> fall back to the
            # copy we just wrote, guaranteeing CUR == BASE (still unmodified).
            base = compute_plugin_hash(root / name)
        hashes[name] = base
    write_installed_manifest(root, hashes, package_version=pkg_version)


def eject(name: str, *, target: str = "user", cluster: bool = False) -> EjectResult:
    """Eject *name* to the *target* tier, refusing partial clusters (closes L5).

    Copies each not-yet-ejected cluster member out of the wheel with the atomic,
    L4-safe :func:`write_plugin_dir` swap, then records their baselines. Members
    already present in the target tier are **skipped, never clobbered** -- the
    user's copy and edits are sacred; we only complete the cluster.

    If *name* still absolute-imports a **non-ejected** sibling and *cluster* is
    ``False``, the eject is **refused** (``ok=False, refused=True``) with a clear
    message + the opt-in hint -- we never strand a half-relocated slice. Pass
    ``cluster=True`` (``/plugins eject <name> --cluster``) to eject the whole
    cluster in one go. A standalone plugin -- or one whose siblings are all
    already ejected -- ejects normally regardless of *cluster*.
    """
    target = target.lower()
    if target not in _EJECT_TIERS:
        return EjectResult(
            ok=False,
            name=name,
            target_tier=target,
            message=f"Unknown eject target '{target}'. Use 'user' or 'project'.",
        )

    builtins = ejectable.builtin_names()
    if name not in builtins:
        status = ejectable.describe(name)
        if status.exists:
            msg = (
                f"'{name}' is not a builtin plugin (it lives in the "
                f"{status.loaded_tier} tier); only builtins can be ejected."
            )
        else:
            msg = (
                f"Plugin '{name}' not found in any tier. "
                "Use /plugins list-ejectable to see what can be ejected."
            )
        return EjectResult(ok=False, name=name, target_tier=target, message=msg)

    root = _tier_root(target)
    if root is None:
        return EjectResult(
            ok=False,
            name=name,
            target_tier=target,
            message=(
                f"No {target} plugins directory is available to eject into. "
                "Create .code_puppy/plugins/ to opt into project plugins."
            ),
        )
    root = Path(root)

    cluster_members = resolve_cluster(name)
    builtin_root = ejectable.get_builtin_plugins_dir()

    # Refuse a *partial* eject: if a sibling this plugin absolute-imports is not
    # already present in the target tier, ejecting *name* alone would strand a
    # half-relocated cluster. Demand the explicit cluster opt-in instead.
    pending_siblings = sorted(
        member
        for member in cluster_members
        if member != name and not ejectable._has_plugin(root, member)
    )
    if pending_siblings and not cluster:
        return EjectResult(
            ok=False,
            name=name,
            target_tier=target,
            cluster=tuple(cluster_members),
            requires_cluster=tuple(pending_siblings),
            refused=True,
            message=_refusal_message(name, target, cluster_members, pending_siblings),
        )

    ejected: list[str] = []
    skipped: list[str] = []
    for member in cluster_members:
        if ejectable._has_plugin(root, member):
            skipped.append(member)  # already ejected here -> leave it alone
            continue
        try:
            write_plugin_dir(builtin_root / member, root / member)
        except Exception as exc:  # never crash the app on a bad copy
            return EjectResult(
                ok=False,
                name=name,
                target_tier=target,
                cluster=tuple(cluster_members),
                ejected=tuple(ejected),
                skipped=tuple(skipped),
                message=f"Failed to eject '{member}': {exc}",
            )
        ejected.append(member)

    if ejected:
        _record_baselines(root, ejected)

    return EjectResult(
        ok=True,
        name=name,
        target_tier=target,
        cluster=tuple(cluster_members),
        ejected=tuple(ejected),
        skipped=tuple(skipped),
        message=_summary(name, target, cluster_members, ejected, skipped),
    )


# ---------------------------------------------------------------------------
# pure string formatting (no message bus)
# ---------------------------------------------------------------------------


def _refusal_message(
    name: str,
    target: str,
    cluster: list[str],
    pending_siblings: list[str],
) -> str:
    """Render the partial-cluster refusal: what's blocking + the opt-in hint."""
    siblings = ", ".join(pending_siblings)
    plural = "sibling" if len(pending_siblings) == 1 else "siblings"
    target_flag = "" if target == "user" else f" {target}"
    return (
        f"Refusing to eject '{name}' on its own: it absolute-imports the "
        f"non-ejected {plural} {siblings}. Ejecting it alone would strand a "
        "half-relocated cluster -- you could edit "
        f"'{name}' but not {siblings}.\n"
        f"Eject the whole dependency cluster ({', '.join(cluster)}) instead:\n"
        f"   /plugins eject {name}{target_flag} --cluster"
    )


def _summary(
    name: str,
    target: str,
    cluster: list[str],
    ejected: list[str],
    skipped: list[str],
) -> str:
    if not ejected:
        return (
            f"Nothing to do: '{name}' (and its cluster) is already ejected to the "
            f"{target} tier. Use /plugins show {name} for its status."
        )
    if len(ejected) == 1 and not skipped:
        return (
            f"Ejected '{ejected[0]}' to the {target} tier. It now overrides the "
            "builtin and will load on the next launch."
        )
    cluster_note = ""
    if len(cluster) > 1:
        cluster_note = f" (dependency cluster of '{name}': {', '.join(cluster)})"
    return (
        f"Ejected {len(ejected)} plugin(s) to the {target} tier{cluster_note}: "
        f"{', '.join(ejected)}."
    )


def format_eject_result(result: EjectResult) -> str:
    """Render the full ``/plugins eject`` report, including any skipped members."""
    lines = [result.message]
    if result.ok and result.skipped:
        lines.append("Already ejected (left untouched): " + ", ".join(result.skipped))
    if result.ok and result.ejected:
        lines.append(
            "Edit them under ~/.code_puppy/plugins/ (user) or "
            ".code_puppy/plugins/ (project); restart to load your copy."
        )
    return "\n".join(lines)
