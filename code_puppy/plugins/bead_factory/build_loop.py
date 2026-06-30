"""Build-loop orchestration for the bead_factory plugin.

This module holds the *importable logic* only -- the iteration-cap reader, the
parallel inspector orchestration, the remediation-note formatting, and the
turn-end/turn-cancel drivers that power the build loop. It does NOT register
any slash commands or callbacks; wiring is the entry-point module's job.

Conventions:
  * the build-completion verifiers are "inspectors"
    (``_run_build_inspectors``, ``_run_single_inspector``, etc.)
  * the iteration-cap config key is ``bf_build_max_iterations``
    (read via ``get_value``)
  * the banner key/label are ``bf_inspector`` / ``INSPECTOR``
    (see ``banner.py``)
"""

from __future__ import annotations

import asyncio
from typing import Any

from code_puppy.config import get_value
from code_puppy.messaging import (
    emit_warning,
)
from code_puppy.messaging.bus import emit_debug

from . import build_state as state
from . import state as chain_state
from .banner import display_inspector
from .inspector import BuildInspection, inspect_build
from .inspector_config import (
    InspectorConfig,
    get_enabled_inspectors_or_default,
)
from .prompt import build_prompts_for_arming
from .system_prompt import is_pin_active

# Default cap on build-loop iterations. Override per-user with:
#   /set bf_build_max_iterations=<int>
# Clamped to [1, 1000] in get_build_max_iterations to avoid pathological values.
BUILD_MAX_ITERATIONS_DEFAULT = 10
BUILD_MAX_ITERATIONS_FLOOR = 1
BUILD_MAX_ITERATIONS_CEILING = 1000


def get_build_max_iterations() -> int:
    """Read the configured build iteration cap, with sane fallbacks."""
    val = get_value("bf_build_max_iterations")
    try:
        n = int(val) if val else BUILD_MAX_ITERATIONS_DEFAULT
    except (ValueError, TypeError):
        n = BUILD_MAX_ITERATIONS_DEFAULT
    return max(BUILD_MAX_ITERATIONS_FLOOR, min(n, BUILD_MAX_ITERATIONS_CEILING))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _response_text(result: Any) -> str | None:
    return str(getattr(result, "output", "")) if result is not None else None


def _resolve_inspectors(implementor_agent: Any) -> list[InspectorConfig]:
    """Pick the inspector set for this build iteration.

    If the user has configured inspectors via ``/inspectors`` we use those.
    Otherwise we fall back to a single ``default`` inspector that uses the
    implementor agent's model and the standard build-inspector prompt.
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


def _format_remediation_block(verdicts: list[BuildInspection]) -> str:
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


def _refresh_build_prompts(
    *, frozen_implementor: str, frozen_inspector: str
) -> tuple[str, str]:
    """Re-render the (implementor, inspector) prompts from the LIVE bead.

    bead-factory-2mb: ``build_state.prompt`` / ``inspector_prompt`` are frozen
    at claim time, so notes/edits appended to the bead DURING the build loop
    (inspection rework feedback, mid-run field edits, bug-discovery notes)
    never reach the inspectors — they grade stale context. Here we re-fetch
    the live bead via ``bd show <bead_id>`` and re-render through the
    notes-aware formatter (:func:`build_prompts_for_arming`), so BOTH the
    inspectors and the agent's next retry iteration see current state.

    As a side effect we refresh the compaction-protected pin
    (``ChainState.current_bead``, read fresh each turn by
    ``system_prompt.on_load_prompt``) so the implementor's *next* retry turn —
    whose user-message build prompt is slimmed to scaffolding-only while the
    pin is active — is graded against the live contract too, not the frozen
    one.

    Soft-fails to the frozen claim-time snapshot on ANY error (no bead id,
    bd missing/timeout/non-zero, empty payload, render hiccup): a stale
    prompt is strictly better than a crashed build loop.
    """
    bead_id = state.get_state().bead_id
    if not bead_id:
        return frozen_implementor, frozen_inspector
    try:
        from . import beads

        live_bead = beads.show(bead_id)
        if not live_bead:
            return frozen_implementor, frozen_inspector
        # Refresh the pinned contract so the NEXT retry turn's system prompt
        # carries the live bead (the pin reads current_bead fresh each turn).
        chain_state.get_state().current_bead = live_bead
        return build_prompts_for_arming(
            live_bead,
            recovery=state.get_state().recovery,
            inject_content=is_pin_active(),
        )
    except Exception as exc:  # noqa: BLE001 — never crash the build loop.
        emit_debug(f"[bead_factory] live bead re-fetch failed: {exc!r}")
        return frozen_implementor, frozen_inspector


# ---------------------------------------------------------------------------
# Inspector orchestration (parallel)
# ---------------------------------------------------------------------------


async def _run_single_inspector(
    inspector_config: InspectorConfig,
    *,
    implementor_agent: Any,
    build: str,
    response: str | None,
    error: BaseException | None,
    history: list[Any],
) -> BuildInspection:
    """Run a single inspector. No I/O -- callers handle display before/after.

    We intentionally do NOT print here: ``_run_build_inspectors`` runs many of
    these in parallel via ``asyncio.gather``, and concurrent calls into the
    rich Console (which does \\r line-clearing tricks) interleave and
    overwrite each other. Display is serialized at the orchestrator level.
    """
    try:
        return await inspect_build(
            inspector_config=inspector_config,
            implementor_agent=implementor_agent,
            build=build,
            response=response,
            error=error,
            history=history,
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:
        # inspect_build() already catches model exceptions and returns an
        # abstain-verdict. Anything that escapes here is an unexpected bug
        # in OUR plumbing -- still abstain so one bad inspector can't block
        # the build loop.
        from code_puppy.error_logging import log_error

        log_error(exc, context=f"Build inspector failed ({inspector_config.name})")
        return BuildInspection(
            inspector_name=inspector_config.name,
            complete=False,
            notes=f"inspector crashed: {type(exc).__name__}: {exc}",
            raw_response="",
            abstained=True,
        )


def _inspector_roster_line(inspectors: list[InspectorConfig]) -> str:
    """e.g. 'judy (gpt-5.4), joe-brown (claude-sonnet-4.5)'."""
    return ", ".join(f"{i.name} ({i.model})" for i in inspectors)


async def _run_build_inspectors(
    *,
    agent: Any,
    build: str,
    result: Any,
    error: BaseException | None,
) -> tuple[bool, str, list[BuildInspection]]:
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
            "if the build is complete..."
        )
    else:
        display_inspector(
            f"Running {len(inspectors)} inspectors in parallel: "
            f"{_inspector_roster_line(inspectors)}"
        )

    try:
        verdicts: list[BuildInspection] = await asyncio.gather(
            *(
                _run_single_inspector(
                    inspector,
                    implementor_agent=agent,
                    build=build,
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
        # propagate here is what stops the build loop cleanly -- if we
        # returned a sentinel tuple instead, the caller would treat it as
        # "build incomplete" and request another retry.
        display_inspector("⛔ Inspectors cancelled (Ctrl+C). Stopping build loop.")
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
    # actually render one. The build completes when every NON-abstaining
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
# Turn-end / turn-cancel drivers (power the build loop)
# ---------------------------------------------------------------------------


async def on_interactive_turn_end(
    agent: Any,
    prompt: str,
    result: Any = None,
    *,
    success: bool = True,
    error: BaseException | None = None,
) -> dict[str, Any] | None:
    """Ask the CLI to continue while the build loop is active."""
    del prompt, success
    build_prompt = state.get_prompt()
    if not build_prompt:
        state.stop()
        return None

    # The inspector is a raw pydantic_ai agent with no system-prompt pin, so
    # it gets the FULL content+scaffolding copy (bead-factory-462). The
    # implementor continuation below re-sends the (possibly slimmed) copy,
    # since its bead content rides the pinned system prompt.
    inspector_frozen = state.get_inspector_prompt() or build_prompt

    # bead-factory-2mb: re-fetch the LIVE bead so notes/edits appended during
    # this build loop (inspection rework feedback, mid-run edits,
    # bug-discovery notes) reach the inspectors AND the next retry's pinned
    # contract — not the frozen claim-time snapshot. Soft-fails to the frozen
    # copies on any bd error.
    build_prompt, inspector_build = _refresh_build_prompts(
        frozen_implementor=build_prompt,
        frozen_inspector=inspector_frozen,
    )

    loop_num = state.increment()
    try:
        complete, notes, _verdicts = await _run_build_inspectors(
            agent=agent,
            build=inspector_build,
            result=result,
            error=error,
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Belt-and-suspenders: _run_build_inspectors already swallows these
        # but we never want a stray Ctrl+C to escape the plugin and
        # take down the whole REPL.
        display_inspector("Build loop cancelled (Ctrl+C).")
        state.stop()
        return None
    if complete:
        # Per-inspector verdicts were already shown by _run_build_inspectors
        # -- no need to re-dump the notes block here.
        display_inspector("BUILD COMPLETE!", final=True)
        state.stop()
        return None

    max_iters = get_build_max_iterations()
    if loop_num >= max_iters:
        display_inspector(
            f"BUILD STOPPED — Hit max iterations ({max_iters}). "
            f"Raise the cap with /set bf_build_max_iterations=<int>.",
            final=True,
        )
        state.stop()
        return None

    state.get_state().remediation_notes = notes
    display_inspector(
        f"BUILD INCOMPLETE — Retrying! (Loop #{loop_num}/{max_iters})",
        final=True,
    )
    return {
        "prompt": f"{build_prompt}\n\nInspector remediation notes:\n{notes}",
        "clear_context": True,
        "delay": 0.5,
        "reason": "build",
    }


def on_interactive_turn_cancel(prompt: str, *, reason: str = "cancelled") -> None:
    del prompt
    if state.is_active():
        state.stop()
        emit_warning(f"Build loop stopped due to {reason}")
