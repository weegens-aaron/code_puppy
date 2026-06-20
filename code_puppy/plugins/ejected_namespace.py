"""Make an ejected builtin the *live* copy for absolute imports too (puppy-4sy).

Background
----------
When a builtin plugin is ejected to the user/project tier, the tier loader
imports the ejected copy under a synthetic namespace (``user_plugins.<name>`` /
``project_plugins.<name>``) and *suppresses* the wheel builtin so it never
registers callbacks. That is enough to make the ejected copy win for the
plugin's own **relative** imports (``from . import x``) — the case the loader's
``_ensure_plugin_package`` already covers.

It is **not** enough, though, for an **absolute** import of the plugin's
submodules. Core modules and sibling plugins legitimately reach into a plugin
by its canonical dotted path::

    from code_puppy.plugins.claude_code_oauth.utils import refresh_access_token
    from code_puppy.plugins.ollama_setup.completer import OllamaSetupCompleter

Python resolves ``code_puppy.plugins.<name>`` against the **real** package on
disk (the pristine wheel copy), so those imports loaded the *un-ejected* files —
and a user's edits to the ejected copy were silently ignored after a restart.
That is the runtime symptom reported in puppy-4sy: "the edit is not reflected;
code puppy behaves as if the original plugin is in effect."

The fix
-------
After the tiers are loaded, :func:`alias_ejected_builtins` rebinds the canonical
``code_puppy.plugins.<name>`` entry in ``sys.modules`` to the **already-loaded
ejected modules**. Because the owned-tier package's ``__path__`` already points
at the ejected directory, this makes *every* form of import resolve to the live,
edited copy:

* already-imported submodules are aliased object-for-object (so there is exactly
  ONE module instance — no double execution, hence no double callback
  registration when something re-imports ``...register_callbacks``), and
* not-yet-imported submodules load lazily from the ejected directory via the
  shared package's ``__path__``.

Precedence (project > user) is honored: a project-tier eject wins over a
user-tier copy of the same name, matching the loader's tier-collision policy.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# The canonical package every builtin plugin lives under in the wheel. Aliasing
# a submodule of this is exactly what makes an ejected copy "the live copy" for
# absolute imports issued by core or by a sibling plugin.
_BUILTIN_PLUGINS_NS = "code_puppy.plugins"

# Synthetic owned-tier namespaces, in *descending* precedence order so the
# higher-precedence tier (project) is aliased first and a user copy of the same
# name can never clobber it.
_OWNED_TIERS: tuple[tuple[str, str], ...] = (
    ("project", "project_plugins"),
    ("user", "user_plugins"),
)

__all__ = ["alias_ejected_builtins"]


def alias_ejected_builtins(
    loaded: dict[str, list[str]],
    builtin_names: set[str],
) -> list[str]:
    """Alias every ejected builtin onto its canonical ``code_puppy.plugins.<name>``.

    Args:
        loaded: The loader's per-tier result — a dict with (at least) ``user``
            and ``project`` keys mapping to the list of plugin names loaded from
            that tier.
        builtin_names: The set of shipped builtin plugin names. A loaded owned
            plugin counts as an *ejected builtin* only if its name is in here;
            a purely user/project-authored plugin is left alone.

    Returns:
        The list of plugin names that were aliased (for logging/tests). Safe and
        idempotent: never raises, and a name is aliased at most once (highest
        precedence tier wins).
    """
    aliased: list[str] = []
    seen: set[str] = set()

    for tier_key, tier_ns in _OWNED_TIERS:
        for name in loaded.get(tier_key, []) or []:
            if name in seen or name not in builtin_names:
                # Already claimed by a higher tier, or not an ejected builtin.
                continue
            try:
                if _alias_subtree(f"{tier_ns}.{name}", f"{_BUILTIN_PLUGINS_NS}.{name}"):
                    aliased.append(name)
                    seen.add(name)
            except Exception as exc:  # never let an alias hiccup block startup
                logger.warning(
                    "ejected_namespace: failed to alias '%s' from %s tier (%s)",
                    name,
                    tier_key,
                    exc,
                )

    if aliased:
        logger.debug(
            "ejected_namespace: ejected builtins now live at canonical path: %s",
            ", ".join(sorted(aliased)),
        )
    return aliased


def _alias_subtree(owned_pkg: str, canonical_pkg: str) -> bool:
    """Point ``canonical_pkg.*`` at the already-loaded ``owned_pkg.*`` modules.

    Copies the package module object and every loaded submodule from the owned
    namespace onto the canonical one, sharing the SAME objects so there is no
    re-execution. Returns ``True`` if the owned package was present and aliased.
    """
    owned_root = sys.modules.get(owned_pkg)
    if owned_root is None:
        # The owned copy never actually loaded (e.g. an import error). Nothing
        # to alias — leave the wheel builtin resolution untouched.
        return False

    prefix = owned_pkg + "."
    # Snapshot first: we mutate sys.modules while iterating.
    for mod_name in list(sys.modules):
        if mod_name == owned_pkg:
            sys.modules[canonical_pkg] = owned_root
        elif mod_name.startswith(prefix):
            suffix = mod_name[len(owned_pkg) :]  # e.g. ".register_callbacks"
            sys.modules[canonical_pkg + suffix] = sys.modules[mod_name]
    return True
