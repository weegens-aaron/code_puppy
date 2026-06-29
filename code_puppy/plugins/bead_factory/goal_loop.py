"""Goal/loop orchestration for the bead_factory plugin.

Relocated from the former ``wiggum`` plugin's ``register_callbacks.py``. This
module holds the *importable logic* only -- the iteration-cap reader, the
parallel inspector orchestration, the remediation-note formatting, and the
turn-end/turn-cancel drivers that power the goal and loop modes. It does NOT
register any slash commands or callbacks; wiring is the entry-point module's
job.

Renames applied (pure rename, zero behavior change):
  * the verifier vocabulary becomes "inspectors"
    (``_run_goal_inspectors``, ``_run_single_inspector``, etc.)
  * the iteration-cap config key becomes ``bf_goal_max_iterations``
    (read via ``get_value``)
  * the banner key/label become ``bf_inspector`` / ``INSPECTOR``
    (see ``banner.py``)
"""

from __future__ import annotations

import asyncio
from typing import Any

from code_puppy.config import get_value
from code_puppy.messaging import (
    emit_info,
    emit_system_message,
    emit_warning,
)

from . import loop_state as state
from .banner import display_inspector
from .inspector import GoalInspection, inspect_goal
from .inspector_config import (
    InspectorConfig,
    get_enabled_inspectors_or_default,
    load_inspectors,
)

# Default cap on goal-mode iterations. Override per-user with:
#   /set bf_goal_max_iterations=<int>
# Clamped to [1, 1000] in get_goal_max_iterations to avoid pathological values.
GOAL_MAX_ITERATIONS_DEFAULT = 10
GOAL_MAX_ITERATIONS_FLOOR = 1
GOAL_MAX_ITERATIONS_CEILING = 1000


def get_goal_max_iterations() -> int:
    """Read the configured goal iteration cap, with sane fallbacks."""
    val = get_value("bf_goal_max_iterations")
    try:
        n = int(val) if val else GOAL_MAX_ITERATIONS_DEFAULT
    except (ValueError, TypeError):
        n = GOAL_MAX_ITERATIONS_DEFAULT
    return max(GOAL_MAX_ITERATIONS_FLOOR, min(n, GOAL_MAX_ITERATIONS_CEILING))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_prompt(command: str) -> str:
    """Pull the prompt text out of a ``/command <prompt>`` invocation."""
    parts = command.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def emit_configured_inspectors_summary() -> None:
    """Show the user exactly which inspectors the goal loop will fan out to.

    Previously we just told users to run /inspectors, which left people
    wondering if they'd configured anything at all. Now we list the
    enabled inspectors (and warn about disabled ones) so the state of the
    world is obvious before the loop kicks off.
    """
    registry = load_inspectors()
    enabled = registry.enabled()
    disabled = [i for i in registry.inspectors if not i.enabled]

    if enabled:
        emit_info(f"Configured inspectors ({len(enabled)} enabled):")
        for inspector in enabled:
            emit_info(f"  - {inspector.name} ({inspector.model})")
        if disabled:
            disabled_names = ", ".join(i.name for i in disabled)
            emit_info(f"  (disabled: {disabled_names})")
    else:
        emit_info(
            "No inspectors configured — falling back to a single default "
            "inspector using the implementor's model."
        )
    emit_info("Run /inspectors to add, edit, enable, or disable inspectors.")


def _response_text(result: Any) -> str | None:
    return str(getattr(result, "output", "")) if result is not None else None


def _resolve_inspectors(implementor_agent: Any) -> list[InspectorConfig]:
    """Pick the inspector set for this goal iteration.

    If the user has configured inspectors via ``/inspectors`` we use those.
    Otherwise we fall back to a single ``default`` inspector that uses the
    implementor agent's model and the standard goal-inspector prompt.
    """
    fallback_model = getattr(
        implementor_agent.get_pydantic_agent().model
        if hasattr(implementor_agent, "get_pydantic_agent")
        else None,
        "model_name",
        None,
    )
    if not fallback_model:
        # The agent object exposes its model name via get_model_name() in most
        # places; fall back to that if the pydantic_agent shape differs.
        try:
            fallback_model = implementor_agent.get_model_name()
        except Exception:
            fallback_model = "code-puppy"

    return get_enabled_inspectors_or_default(str(fallback_model))


def _format_remediation_block(verdicts: list[GoalInspection]) -> str:
    """Build the remediation-notes string that feeds the next iteration."""
    lines: list[str] = []
    for v in verdicts:
        if v.abstained:
            status = "⚠️  ABSTAIN"
        else:
            status = "✅ PASS" if v.complete else "❌ FAIL"
        lines.append(f"[{v.inspector_name}] {status}")
        if v.notes:
            for note_line in v.notes.strip().splitlines():
                lines.append(f"  {note_line}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Inspector orchestration (parallel)
# ---------------------------------------------------------------------------


async def _run_single_inspector(
    inspector_config: InspectorConfig,
    *,
    implementor_agent: Any,
    goal: str,
    response: str | None,
    error: BaseException | None,
    history: list[Any],
) -> GoalInspection:
    """Run a single inspector. No I/O -- callers handle display before/after.

    We intentionally do NOT print here: ``_run_goal_inspectors`` runs many of
    these in parallel via ``asyncio.gather``, and concurrent calls into the
    rich Console (which does \\r line-clearing tricks) interleave and
    overwrite each other. Display is serialized at the orchestrator level.
    """
    try:
        return await inspect_goal(
            inspector_config=inspector_config,
            implementor_agent=implementor_agent,
            goal=goal,
            response=response,
            error=error,
            history=history,
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:
        # inspect_goal() already catches model exceptions and returns an
        # abstain-verdict. Anything that escapes here is an unexpected bug
        # in OUR plumbing -- still abstain so one bad inspector can't block
        # the goal loop.
        from code_puppy.error_logging import log_error

        log_error(exc, context=f"Goal inspector failed ({inspector_config.name})")
        return GoalInspection(
            inspector_name=inspector_config.name,
            complete=False,
            notes=f"inspector crashed: {type(exc).__name__}: {exc}",
            raw_response="",
            abstained=True,
        )


def _inspector_roster_line(inspectors: list[InspectorConfig]) -> str:
    """e.g. 'judy (gpt-5.4), joe-brown (claude-sonnet-4.5)'."""
    return ", ".join(f"{i.name} ({i.model})" for i in inspectors)


async def _run_goal_inspectors(
    *,
    agent: Any,
    goal: str,
    result: Any,
    error: BaseException | None,
) -> tuple[bool, str, list[GoalInspection]]:
    """Run every enabled inspector in parallel.

    Returns ``(all_complete, formatted_notes, verdicts)``.

    ``all_complete`` is True when every inspector reports ``complete=True``.
    The inspector's own ``complete=True`` IS the "no remediation needed"
    signal -- any rationale notes alongside it are just for visibility and
    don't block completion.
    """
    inspectors = _resolve_inspectors(agent)
    if not inspectors:
        return False, "No inspector agents configured.", []

    history = list(agent.get_message_history())
    response_text = _response_text(result)

    # Announce up front so the user knows we're firing N inspectors in parallel.
    if len(inspectors) == 1:
        display_inspector(
            f"Asking inspector {_inspector_roster_line(inspectors)} "
            "if the goal is complete..."
        )
    else:
        display_inspector(
            f"Running {len(inspectors)} inspectors in parallel: "
            f"{_inspector_roster_line(inspectors)}"
        )

    try:
        verdicts: list[GoalInspection] = await asyncio.gather(
            *(
                _run_single_inspector(
                    inspector,
                    implementor_agent=agent,
                    goal=goal,
                    response=response_text,
                    error=error,
                    history=history,
                )
                for inspector in inspectors
            )
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Display the banner so the user sees WHY the panel bailed, then
        # re-raise. The caller (on_interactive_turn_end) catches at the
        # plugin boundary so the REPL stays alive. Letting cancellation
        # propagate here is what stops the goal loop cleanly -- if we
        # returned a sentinel tuple instead, the caller would treat it as
        # "goal incomplete" and request another retry.
        display_inspector("⛔ Inspectors cancelled (Ctrl+C). Stopping goal loop.")
        raise

    # Now serialize the per-inspector verdicts so all banners actually show up.
    for v in verdicts:
        if v.abstained:
            glyph = "⚠️  ABSTAIN"
        else:
            glyph = "✅ PASS" if v.complete else "❌ FAIL"
        summary = v.notes.strip().splitlines()[0] if v.notes.strip() else "(no notes)"
        display_inspector(f"  [{v.inspector_name}] {glyph} — {summary}")

    # Abstaining inspectors (endpoint errors, misconfigured models, etc.) are
    # excluded from the tally -- they don't get a vote because they couldn't
    # actually render one. The goal completes when every NON-abstaining
    # inspector says PASS. If every inspector abstained, we can't decide --
    # treat that as incomplete with a clear warning.
    voting = [v for v in verdicts if not v.abstained]
    if not voting:
        all_complete = False
        if verdicts:
            display_inspector(
                "⚠️  All inspectors abstained — cannot determine completion."
            )
    else:
        all_complete = all(v.complete for v in voting)

    notes = _format_remediation_block(verdicts)
    return all_complete, notes, verdicts


# ---------------------------------------------------------------------------
# Turn-end / turn-cancel drivers (power the goal and loop modes)
# ---------------------------------------------------------------------------


async def on_interactive_turn_end(
    agent: Any,
    prompt: str,
    result: Any = None,
    *,
    success: bool = True,
    error: BaseException | None = None,
) -> dict[str, Any] | None:
    """Ask the CLI to continue while loop/goal mode is active."""
    del prompt, success
    goal_prompt = state.get_prompt()
    if not goal_prompt:
        state.stop()
        return None

    loop_num = state.increment()
    if state.is_goal_mode():
        try:
            complete, notes, _verdicts = await _run_goal_inspectors(
                agent=agent,
                goal=goal_prompt,
                result=result,
                error=error,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Belt-and-suspenders: _run_goal_inspectors already swallows these
            # but we never want a stray Ctrl+C to escape the plugin and
            # take down the whole REPL.
            display_inspector("⛔ Goal loop cancelled (Ctrl+C).")
            state.stop()
            return None
        if complete:
            # Per-inspector verdicts were already shown by _run_goal_inspectors
            # -- no need to re-dump the notes block here.
            display_inspector("✅ GOAL COMPLETE!", final=True)
            state.stop()
            return None

        max_iters = get_goal_max_iterations()
        if loop_num >= max_iters:
            display_inspector(
                f"🛑 GOAL STOPPED — Hit max iterations ({max_iters}). "
                f"Raise the cap with /set bf_goal_max_iterations=<int>.",
                final=True,
            )
            state.stop()
            return None

        state.get_state().remediation_notes = notes
        display_inspector(
            f"❌ GOAL INCOMPLETE — Retrying! (Loop #{loop_num}/{max_iters})",
            final=True,
        )
        return {
            "prompt": f"{goal_prompt}\n\nInspector remediation notes:\n{notes}",
            "clear_context": True,
            "delay": 0.5,
            "reason": "goal",
        }

    if error is not None:
        emit_warning(f"\n🍩 WIGGUM RETRYING AFTER ERROR! (Loop #{loop_num})")
        emit_system_message(f"Previous run failed: {error}")
    else:
        emit_warning(f"\n🍩 WIGGUM RELOOPING! (Loop #{loop_num})")

    emit_system_message(f"Re-running prompt: {goal_prompt}")
    return {
        "prompt": goal_prompt,
        "clear_context": True,
        "delay": 0.5,
        "reason": "wiggum",
    }


def on_interactive_turn_cancel(prompt: str, *, reason: str = "cancelled") -> None:
    del prompt
    if state.is_active():
        state.stop()
        emit_warning(f"🍩 Wiggum/goal loop stopped due to {reason}")
