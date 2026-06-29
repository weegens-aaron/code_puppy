"""Slash-command handlers for the bead_factory loop/goal subsystem.

Thin entry points relocated from the former ``wiggum`` plugin's
``register_callbacks`` command handlers and renamed to the committed
bead_factory clean-break command names (``/bf-loop``, ``/bf-goal`` + aliases,
``/bf-stop``, ``/inspectors``). Each handler delegates to the importable logic
in :mod:`goal_loop`, :mod:`loop_state`, :mod:`banner`, and
:mod:`inspectors_menu`; the plugin entry point (:mod:`register_callbacks`)
only wires these to the command registry.

Behavior is identical to the wiggum originals -- only the command names, the
``bf_goal_max_iterations`` config key, the ``bf_inspector`` banner, and the
"judges" -> "inspectors" vocabulary differ.
"""

from __future__ import annotations

import asyncio

from code_puppy.messaging import emit_info, emit_success, emit_warning

from . import banner, goal_loop
from . import loop_state as state


def handle_loop_command(command: str) -> str | bool:
    """Start bf-loop mode and execute the prompt immediately."""
    prompt = goal_loop.extract_prompt(command)
    if not prompt:
        emit_warning("Usage: /bf-loop <prompt>")
        emit_info("Example: /bf-loop say hello world")
        emit_info("Press Ctrl+C or run /bf-stop to stop the loop.")
        return True

    state.start(prompt, mode="wiggum")
    emit_success("🍩 BF-LOOP MODE ACTIVATED!")
    emit_info(f"Prompt: {prompt}")
    emit_info("The agent will re-loop this prompt after each completion.")
    emit_info("Press Ctrl+C or run /bf-stop to stop the loop.")
    return prompt


def handle_goal_command(command: str) -> str | bool:
    """Start bf-goal mode and execute the prompt immediately."""
    prompt = goal_loop.extract_prompt(command)
    if not prompt:
        emit_warning("Usage: /bf-goal <prompt>  (aliases: /bf-kibble, /bf-chow)")
        emit_info("Example: /bf-goal make tests pass for the auth flow")
        emit_info("Press Ctrl+C or run /bf-stop to stop the loop.")
        return True

    state.start(prompt, mode="goal")
    banner.display_banner_message(
        "GOAL MODE", "🎯 ACTIVATED!", banner_name="bf_inspector"
    )
    emit_info(f"Goal: {prompt}")
    emit_info(
        "After each iteration, every enabled inspector will verify "
        "completion in parallel."
    )
    emit_info(
        f"Max iterations: {goal_loop.get_goal_max_iterations()} "
        f"(change with /set bf_goal_max_iterations=<int>)"
    )
    goal_loop.emit_configured_inspectors_summary()
    return prompt


def handle_stop_command(command: str) -> bool:
    """Stop bf-loop/goal mode."""
    del command
    if state.is_active():
        state.stop()
        emit_success("🍩 bf-loop/goal mode stopped!")
    else:
        emit_info("bf-loop/goal mode is not active.")
    return True


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
