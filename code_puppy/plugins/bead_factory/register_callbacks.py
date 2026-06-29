"""bead_factory plugin entry point: wire every command + callback.

This is the single thin entry point the plugin loader imports. It does no real
work itself -- it imports the migrated submodules and registers their slash
commands and lifecycle callbacks. All behavior lives in the submodules
(:mod:`commands`, :mod:`goal_loop`, :mod:`chain_driver`, :mod:`close_guard`).

Clean-break naming (decision bead-factory-vka) -- none of these collide with
the still-loaded ``wiggum`` or ``bead-chain`` plugins:

  * ``/bf-loop``                                 (was wiggum's ``/wiggum``)
  * ``/bf-goal`` + ``/bf-kibble`` / ``/bf-chow`` (was ``/goal`` / ``/kibble`` / ``/chow``)
  * ``/bf-stop`` + ``/bf-ws``                    (was ``/wiggum_stop`` / ``/ws`` / ...)
  * ``/inspectors``                              (was ``/judges``)
  * ``/bead-factory`` ``[--max=N]``              (was ``/bead-chain``)

The iteration-cap config key (``bf_goal_max_iterations``) and the inspector
banner (key ``bf_inspector`` / label ``INSPECTOR``) are likewise namespaced in
the submodules so ``/set`` and banners never clash with wiggum.

Hook ordering
-------------
The goal/loop ``interactive_turn_end`` / ``interactive_turn_cancel`` hooks are
registered here at startup so ``/bf-goal`` and ``/bf-loop`` work without the
chain driver. The chain driver registers its OWN turn hooks lazily on first
``/bead-factory`` use (:func:`chain_driver._ensure_hooks_registered`), in
goal-then-chain order, so the goal decision is always observed before the chain
acts. Because ``register_callback`` dedups by identity, the chain driver's
re-registration of the goal hooks is a no-op that simply preserves their
earlier, still-ahead-of-the-chain slot.

Every co-registered callback no-ops unless bead_factory's own state is active:
the goal hooks bail when :mod:`loop_state` is idle, and the close guard bails
when :mod:`state` (the chain state) is idle -- so loading alongside wiggum +
bead-chain never double-drives a loop or double-blocks a close.
"""

from __future__ import annotations

from code_puppy.callbacks import register_callback
from code_puppy.command_line.command_registry import register_command

from . import chain_driver, close_guard, commands, goal_loop

# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
# register_command is a decorator; applying it to the already-defined submodule
# handlers keeps this module to wiring only.

register_command(
    name="bf-loop",
    description="Loop mode: re-run the same prompt when the agent finishes 🍩",
    usage="/bf-loop <prompt>",
    category="plugin",
)(commands.handle_loop_command)

register_command(
    name="bf-goal",
    description="Retry a task until all LLM inspectors say it is complete 🎯",
    usage="/bf-goal <prompt>",
    # /bf-kibble and /bf-chow are puppy-themed aliases for /bf-goal.
    aliases=["bf-kibble", "bf-chow"],
    category="plugin",
)(commands.handle_goal_command)

register_command(
    name="bf-stop",
    description="Stop bead_factory loop/goal mode",
    usage="/bf-stop",
    aliases=["bf-ws"],
    category="plugin",
)(commands.handle_stop_command)

register_command(
    name="inspectors",
    description="Configure goal-mode LLM inspectors (TUI)",
    usage="/inspectors",
    category="plugin",
)(commands.handle_inspectors_command)

register_command(
    name="bead-factory",
    description="Drive /bf-goal across every ready bead in turn 🔗",
    usage="/bead-factory [--max=N]",
    category="plugin",
)(chain_driver.handle_bead_chain_command)

# ---------------------------------------------------------------------------
# Lifecycle callbacks
# ---------------------------------------------------------------------------
# Goal/loop turn hooks at startup so /bf-goal and /bf-loop work standalone. The
# chain driver lazily registers these again (a dedup no-op) ahead of its own
# hooks on first /bead-factory use -- see the module docstring for the
# goal-then-chain ordering contract.
register_callback("interactive_turn_end", goal_loop.on_interactive_turn_end)
register_callback("interactive_turn_cancel", goal_loop.on_interactive_turn_cancel)

# Close guard: no-ops unless the chain (state) is active, so it never
# double-blocks alongside bead-chain's own close guard.
register_callback("run_shell_command", close_guard.on_run_shell_command)
