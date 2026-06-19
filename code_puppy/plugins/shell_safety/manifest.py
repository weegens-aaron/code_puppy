"""Declarative load predicate for the shell_safety plugin.

shell_safety only earns its keep when the user has delegated command review
to the agent -- i.e. when ``safety_permission_level`` is ``"none"`` or
``"low"``. At higher permission levels the user reviews every command by hand,
so the automated risk assessor is redundant and we skip loading it.

This predicate replaces the old hardcoded
``if plugin_name == "shell_safety"`` branch that used to live in the plugin
loader. By moving the gate into the plugin's own manifest, the conditional-load
logic travels *with* the plugin and keeps working identically across the
builtin, user, and project tiers -- so shell_safety can be externalized out of
the wheel without orphaning its gate (closes externalization liability L3).

The loader imports this module in isolation purely to read ``should_load``; it
must NOT register callbacks or carry side effects.
"""

from code_puppy.config import get_safety_permission_level

# Safety permission levels at which the automated shell-safety assessor adds
# value (the user is NOT manually reviewing every command at these levels).
_ACTIVE_LEVELS = ("none", "low")


def should_load() -> bool:
    """Return True only when the shell_safety assessor should be active."""
    return get_safety_permission_level() in _ACTIVE_LEVELS
