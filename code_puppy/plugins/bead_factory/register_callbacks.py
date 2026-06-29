"""bead_factory plugin entry point: wire every command + callback.

This is the single thin entry point the plugin loader imports. It does no real
work itself -- it imports the migrated submodules and registers their slash
commands and lifecycle callbacks. All behavior lives in the submodules
(:mod:`commands`, :mod:`goal_loop`, :mod:`chain_driver`, :mod:`close_guard`).

Clean-break naming (decision bead-factory-vka) -- none of these collide with
the still-loaded ``wiggum`` or ``bead-chain`` plugins:

  * ``/inspectors``                 (was ``/judges``)
  * ``/bead-factory`` ``[--max=N]`` (was ``/bead-chain``)

The iteration-cap config key (``bf_goal_max_iterations``) and the inspector
banner (key ``bf_inspector`` / label ``INSPECTOR``) are likewise namespaced in
the submodules so ``/set`` and banners never clash with wiggum.

The standalone ``/bf-goal`` / ``/bf-loop`` / ``/bf-stop`` commands have been
retired -- bead_factory is now driven solely via ``/bead-factory`` plus the
``/inspectors`` pane (epic bead-factory-ak6).

Hook ordering
-------------
The goal/loop ``interactive_turn_end`` / ``interactive_turn_cancel`` hooks are
NO LONGER registered here at startup. The chain driver is now their sole
registrar: it registers them lazily, ahead of its own turn hooks, on first
``/bead-factory`` use (:func:`chain_driver._ensure_hooks_registered`), in
goal-then-chain order, so the goal decision is always observed before the chain
acts.

The close guard no-ops unless the chain (:mod:`state`) is active, so it never
double-blocks alongside bead-chain's own close guard.
"""

from __future__ import annotations

from code_puppy.callbacks import register_callback
from code_puppy.command_line.command_registry import register_command

from . import chain_driver, close_guard, commands

# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
# register_command is a decorator; applying it to the already-defined submodule
# handlers keeps this module to wiring only.

register_command(
    name="inspectors",
    description="Configure goal-mode LLM inspectors (TUI)",
    usage="/inspectors",
    category="plugin",
)(commands.handle_inspectors_command)

register_command(
    name="bead-factory",
    description="Drive goal-mode verification across every ready bead in turn",
    usage="/bead-factory [--max=N]",
    category="plugin",
)(chain_driver.handle_bead_chain_command)

# ---------------------------------------------------------------------------
# Lifecycle callbacks
# ---------------------------------------------------------------------------
# The goal/loop turn hooks are registered lazily by the chain driver
# (_ensure_hooks_registered) on first /bead-factory use -- see the module
# docstring for the goal-then-chain ordering contract.
#
# Close guard: no-ops unless the chain (state) is active, so it never
# double-blocks alongside bead-chain's own close guard.
register_callback("run_shell_command", close_guard.on_run_shell_command)
