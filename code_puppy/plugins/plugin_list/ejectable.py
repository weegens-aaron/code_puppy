"""Discoverability helpers for the opt-in eject surface (E4.2 — puppy-viu.4.2).

This module backs two ``/plugins`` subcommands:

* ``/plugins list-ejectable`` — which builtin plugins are *eligible* to eject,
  and which have already been ejected.
* ``/plugins show <name>`` — a per-plugin report: which tier(s) it lives in,
  which copy actually wins, whether it has been ejected, and (for an ejected
  copy) whether the user has modified it since.

It is the cheap patch for the recommendation's one weak column —
discoverability — flagged in ADR-001 / 27g.4. The eject *action* itself (E4.1)
is separate; this module is read-only: it inspects the three plugin tiers and
reuses the **same** hash engine the scoped sync uses (E3.1/E3.2), so the
modification verdict here is identical to the one the startup sync would reach
(newline-normalized, L4-safe).

Everything here is pure data-gathering + pure string formatting (no message
bus), so it is unit-testable in isolation; ``register_callbacks.py`` only wires
the output to ``emit_*``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from code_puppy.plugins.plugin_sync import (
    manifest_plugin_hashes,
    read_installed_manifest,
)
from code_puppy.plugins.shipped_manifest import (
    compute_plugin_hash,
    iter_builtin_plugin_dirs,
    load_shipped_manifest,
)

# Tiers in *precedence order* (highest wins). Mirrors the loader: an owned
# project copy beats a user copy beats the builtin.
_TIER_ORDER = ("project", "user", "builtin")

# Modification verdicts for an ejected copy.
MOD_UNMODIFIED = "unmodified"
MOD_MODIFIED = "modified"
MOD_UNKNOWN = "unknown"
MOD_NA = "n/a"  # not ejected -> modification is not a meaningful question


def get_builtin_plugins_dir() -> Path:
    """The in-package canonical plugins directory (``code_puppy/plugins``).

    This file lives at ``code_puppy/plugins/plugin_list/ejectable.py``; two
    parents up is the builtin plugins root — the canonical copy the lazy-hybrid
    keeps in the wheel.
    """
    return Path(__file__).resolve().parent.parent


def _user_plugins_dir() -> Path | None:
    from code_puppy.plugins import USER_PLUGINS_DIR

    return USER_PLUGINS_DIR


def _project_plugins_dir() -> Path | None:
    from code_puppy.plugins import get_project_plugins_directory

    return get_project_plugins_directory()


def _tier_root(tier: str) -> Path | None:
    if tier == "builtin":
        return get_builtin_plugins_dir()
    if tier == "user":
        return _user_plugins_dir()
    if tier == "project":
        return _project_plugins_dir()
    return None


def _has_plugin(root: Path | None, name: str) -> bool:
    """True if *root* physically holds a loadable plugin dir named *name*.

    Matches the loader's "what counts as a plugin dir" rule (an entry-point
    file is present), so a dot/underscore sidecar is never mistaken for one.
    """
    if root is None:
        return False
    plugin_dir = root / name
    if not plugin_dir.is_dir():
        return False
    return (plugin_dir / "register_callbacks.py").exists() or (
        plugin_dir / "__init__.py"
    ).exists()


def builtin_names() -> set[str]:
    """Every builtin plugin name shipped in the wheel."""
    return {p.name for p in iter_builtin_plugin_dirs(get_builtin_plugins_dir())}


def _shipped_hash(name: str) -> str | None:
    """The NEW (shipped) hash for *name*, falling back to a live recompute.

    Prefers the build-time ``_shipped_manifest.json`` (the L4-normalized truth).
    A bare source checkout has no manifest, so we recompute from the canonical
    builtin dir with the very same primitive — identical result, just slower.
    """
    manifest = load_shipped_manifest(get_builtin_plugins_dir())
    shipped = manifest_plugin_hashes(manifest)
    cached = shipped.get(name)
    if cached is not None:
        return cached
    builtin_dir = get_builtin_plugins_dir() / name
    if builtin_dir.is_dir():
        return compute_plugin_hash(builtin_dir)
    return None


def _modification_status(name: str, ejected_root: Path) -> str:
    """Has the user changed their ejected copy of *name*?

    Uses BASE/CUR exactly like the sync engine: ``user_modified == (CUR != BASE)``
    where BASE is the recorded installed-manifest hash. When no baseline was
    ever recorded (a hand-dropped copy with no manifest), we fall back to
    comparing CUR against the shipped NEW hash — equal means "pristine copy",
    different means "modified".
    """
    plugin_dir = ejected_root / name
    if not plugin_dir.is_dir():
        return MOD_NA
    cur = compute_plugin_hash(plugin_dir)

    base = manifest_plugin_hashes(read_installed_manifest(ejected_root)).get(name)
    if base is not None:
        return MOD_MODIFIED if cur != base else MOD_UNMODIFIED

    shipped = _shipped_hash(name)
    if shipped is None:
        return MOD_UNKNOWN
    return MOD_MODIFIED if cur != shipped else MOD_UNMODIFIED


@dataclass(frozen=True)
class PluginStatus:
    """A read-only snapshot of one plugin across the three tiers."""

    name: str
    present_tiers: tuple[str, ...] = field(default_factory=tuple)  # precedence order
    is_builtin: bool = False
    ejected_tier: str | None = None  # user/project copy of a builtin, if any
    modification: str = MOD_NA
    ejectable: bool = False  # a builtin that is not already ejected

    @property
    def loaded_tier(self) -> str | None:
        """The precedence winner — the copy the loader would actually run."""
        return self.present_tiers[0] if self.present_tiers else None

    @property
    def is_ejected(self) -> bool:
        return self.ejected_tier is not None

    @property
    def exists(self) -> bool:
        return bool(self.present_tiers)


def describe(name: str) -> PluginStatus:
    """Build the cross-tier status for a single plugin *name*.

    Works for any name: a pristine builtin, an ejected builtin (builtin +
    user/project copy), or a purely user/project-authored plugin that was never
    a builtin. A name that exists nowhere returns ``exists == False``.
    """
    present = tuple(t for t in _TIER_ORDER if _has_plugin(_tier_root(t), name))
    is_builtin = "builtin" in present

    # The ejected tier is the highest-precedence *owned* (non-builtin) copy of a
    # builtin. A user/project plugin that is not a builtin is authored, not
    # ejected.
    ejected_tier = None
    if is_builtin:
        for tier in ("project", "user"):
            if tier in present:
                ejected_tier = tier
                break

    modification = MOD_NA
    if ejected_tier is not None:
        modification = _modification_status(name, _tier_root(ejected_tier))

    return PluginStatus(
        name=name,
        present_tiers=present,
        is_builtin=is_builtin,
        ejected_tier=ejected_tier,
        modification=modification,
        ejectable=is_builtin and ejected_tier is None,
    )


def list_ejectable() -> list[PluginStatus]:
    """Status for every builtin plugin, sorted by name.

    Each entry is a builtin candidate for eject; ``ejectable`` is ``True`` when
    it has not already been ejected. Already-ejected builtins are still listed
    (with their tier + modification status) so the user sees the full picture.
    """
    return [describe(name) for name in sorted(builtin_names())]


# ---------------------------------------------------------------------------
# Pure string formatting (no message bus) — kept here so it is unit-testable.
# ---------------------------------------------------------------------------


def format_list_ejectable(statuses: list[PluginStatus]) -> str:
    """Render ``/plugins list-ejectable`` output."""
    if not statuses:
        return "No builtin plugins found."

    available = [s for s in statuses if s.ejectable]
    ejected = [s for s in statuses if s.is_ejected]

    lines: list[str] = ["Ejectable builtin plugins", ""]

    lines.append(f"Available to eject ({len(available)}):")
    if available:
        lines.extend(f"   {s.name}" for s in available)
    else:
        lines.append("   (none - every builtin is already ejected)")

    if ejected:
        lines.extend(["", f"Already ejected ({len(ejected)}):"])
        for s in ejected:
            note = "modified" if s.modification == MOD_MODIFIED else s.modification
            lines.append(f"   {s.name}  -> {s.ejected_tier} ({note})")

    lines.extend(
        [
            "",
            "Use /plugins show <name> for details, or /plugins eject <name> to eject.",
        ]
    )
    return "\n".join(lines)


def format_show(status: PluginStatus) -> str:
    """Render ``/plugins show <name>`` output."""
    if not status.exists:
        return (
            f"Plugin '{status.name}' not found in any tier "
            "(builtin, user, or project).\n"
            "Use /plugins list-ejectable to see builtin plugins."
        )

    tiers = ", ".join(status.present_tiers) if status.present_tiers else "(none)"
    lines = [
        f"Plugin: {status.name}",
        "",
        f"   Present in tier(s): {tiers}",
        f"   Loaded from:        {status.loaded_tier} (precedence winner)",
        f"   Builtin:            {'yes' if status.is_builtin else 'no'}",
    ]

    if status.is_builtin:
        if status.is_ejected:
            lines.append(f"   Ejected:            yes -> {status.ejected_tier} tier")
            lines.append(f"   Modification:       {status.modification}")
        else:
            lines.append("   Ejected:            no (running from the wheel)")
            lines.append("   Ejectable:          yes - /plugins eject " + status.name)
    else:
        lines.append(
            "   Ejected:            n/a (not a builtin; user/project-authored)"
        )

    return "\n".join(lines)
