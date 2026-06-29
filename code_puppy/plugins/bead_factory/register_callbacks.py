"""bead_factory plugin entry point (scaffold stub -- no behavior yet).

The plugin loader auto-discovers this module and imports it at startup, which is
all this stub needs to do for now: exist and import cleanly.

The actual command + callback registration (the unified entry point wiring the
loop/goal commands, the inspector orchestration, and the bead-chain driver) is
wired up by the downstream migration beads. Until then this module
intentionally registers nothing.
"""

from __future__ import annotations
