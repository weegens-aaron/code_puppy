"""Slash-command handlers for the bead_factory inspectors subsystem.

Thin entry point for the ``/inspectors`` TUI command. bead_factory is driven
solely via ``/bead-factory`` (the chain driver) plus the ``/inspectors`` pane
(epic bead-factory-ak6). The handler delegates to :mod:`inspectors_menu`; the
plugin entry point (:mod:`register_callbacks`) only wires it to the command
registry.
"""

from __future__ import annotations

import asyncio

from code_puppy.messaging import emit_warning


def handle_inspectors_command(command: str) -> bool:
    """Open the goal-inspectors TUI."""
    del command
    import concurrent.futures

    from .inspectors_menu import interactive_inspectors_menu

    # The menu is async; run it in a fresh event loop on a worker thread so
    # we don't collide with whatever loop the CLI is using.
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(lambda: asyncio.run(interactive_inspectors_menu()))
            future.result(timeout=600)
    except concurrent.futures.TimeoutError:
        emit_warning("Inspectors menu timed out.")
    except Exception as exc:
        emit_warning(f"Inspectors menu error: {exc}")
    return True
