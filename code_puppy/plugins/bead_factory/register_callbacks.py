"""bead_factory plugin entry point: wire every command + callback.

This is the single thin entry point the plugin loader imports. It does no real
work itself -- it imports the migrated submodules and registers their slash
commands and lifecycle callbacks. All behavior lives in the submodules
(:mod:`commands`, :mod:`build_loop`, :mod:`chain_driver`, :mod:`close_guard`).

User-facing surface:

  * ``/inspectors``                 -- configure build-mode LLM inspectors
  * ``/bead-factory`` ``[--max=N]`` -- drive the build loop across ready beads

bead_factory is driven solely via ``/bead-factory`` plus the ``/inspectors``
pane (epic bead-factory-ak6).

Hook ordering
-------------
The build-loop ``interactive_turn_end`` / ``interactive_turn_cancel`` hooks are
registered lazily by the chain driver — its sole registrar — ahead of its own
turn hooks on first ``/bead-factory`` use
(:func:`chain_driver._ensure_hooks_registered`), in build-then-chain order, so
the build decision is always observed before the chain acts.

The close guard no-ops unless the chain (:mod:`state`) is active.
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
    description="Configure build-mode LLM inspectors (TUI)",
    usage="/inspectors",
    category="plugin",
)(commands.handle_inspectors_command)

register_command(
    name="bead-factory",
    description="Drive build-mode verification across every ready bead in turn",
    usage="/bead-factory [--max=N]",
    category="plugin",
)(chain_driver.handle_bead_factory_command)

# ---------------------------------------------------------------------------
# Lifecycle callbacks
# ---------------------------------------------------------------------------
# The build turn hooks are registered lazily by the chain driver
# (_ensure_hooks_registered) on first /bead-factory use -- see the module
# docstring for the build-then-chain ordering contract.
#
# Close guard: no-ops unless the chain (state) is active.
register_callback("run_shell_command", close_guard.on_run_shell_command)
