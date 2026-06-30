"""bead_factory chain driver: the bead -> build -> close -> next loop.

The queue driver handles ``--max=N`` parsing, the ``/bead-factory`` command, the
``interactive_turn_end`` / ``interactive_turn_cancel`` hook handlers, and lazy
hook registration.

This module performs **no** module-scope command/callback registration: the
plugin entry point wires ``/bead-factory`` and the close-guard hook.
``_ensure_hooks_registered`` registers the interactive-turn hooks lazily on
first command use — covering the recovery tier, single-in_progress invariant,
blocker gate, end-of-session rollup, and execution-hint mapping.

Hook ordering (explicit)
------------------------
``_ensure_hooks_registered`` makes the ordering explicit: it registers the
in-package build hooks FIRST, then the chain-driver hooks, so chain logic runs
strictly AFTER the per-turn build decision. The build loop's per-turn decision
lives in :mod:`build_loop` and its state in :mod:`build_state`.

Module layout:

  * :mod:`lifecycle` — state transitions: close, revert, invariant guard,
    next-bead waterfall, build-loop arming.
  * :mod:`beads` — thin subprocess wrapper around ``bd``.
  * :mod:`prompt` — bead-dict -> build-prompt formatting.
  * :mod:`close_guard` — shell-command hook that blocks premature
    agent-issued bead closes.
  * :mod:`state` — dumb singleton dataclass for chain state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from code_puppy.callbacks import register_callback
from code_puppy.messaging import (
    emit_info,
    emit_success,
    emit_system_message,
    emit_warning,
)

# ---------------------------------------------------------------------------
# Build loop prerequisite (in-package)
# ---------------------------------------------------------------------------
#
# bead-factory is NOT a build engine — it's a queue driver that delegates the
# LLM-inspected completion loop to the build loop. The per-turn decision lives in
# :mod:`build_loop` and its state in :mod:`build_state`; both ship inside this
# package, so we import them directly.
from . import build_loop
from . import build_state
from . import state
from .beads import BeadsError, is_excluded_type
from .beads_reads import next_ready, open_blocker_ids
from .beads_writes import claim, revert_to_open
from .execution_hints import apply_execution_hints
from .lifecycle import (
    activate_next_bead,
    close_current_bead_success,
    enforce_single_in_progress,
    ensure_epic_in_progress,
    is_recovery_bead,
)
from .prompt import build_prompts_for_arming
from .system_prompt import is_pin_active

logger = logging.getLogger(__name__)

__all__ = ["handle_bead_factory_command"]

# ---------------------------------------------------------------------------
# Lazy hook registration
# ---------------------------------------------------------------------------

_HOOKS_REGISTERED = False


def _ensure_hooks_registered() -> None:
    """Register the turn-end / cancel hooks once, in build-then-chain order.

    Ordering contract
    -----------------
    The chain driver MUST observe the build loop's per-turn decision BEFORE it
    acts: every turn the build loop (``build_loop.on_interactive_turn_end``)
    decides whether the current bead is complete (inspectors passed) or needs
    another iteration; only once the build loop has stopped does the chain
    driver (``_on_interactive_turn_end``) close the bead and claim the next
    one.

    We make the ordering explicit and deterministic: register the in-package
    build hooks FIRST, then the chain-driver hooks. ``register_callback``
    appends in call order and dedups by identity, so this guarantees the build
    decision runs strictly before the chain driver every turn — even if the
    build hooks were already registered elsewhere (a no-op dedup that simply
    preserves their earlier, still-ahead-of-us slot).
    """
    global _HOOKS_REGISTERED
    if _HOOKS_REGISTERED:
        return
    # Build loop FIRST: its per-turn decision must be observed before the chain
    # driver acts on it. Idempotent — if these were already registered, dedup
    # keeps their slot.
    register_callback("interactive_turn_end", build_loop.on_interactive_turn_end)
    register_callback("interactive_turn_cancel", build_loop.on_interactive_turn_cancel)
    register_callback("interactive_turn_end", _on_interactive_turn_end)
    register_callback("interactive_turn_cancel", _on_interactive_turn_cancel)
    _HOOKS_REGISTERED = True


# ---------------------------------------------------------------------------
# CLI flag parsing
# ---------------------------------------------------------------------------

# Sentinel returned by _parse_max_iterations to mean "the user passed an
# invalid --max value, refuse to start". We can't use None for this since
# None is a perfectly valid result (= no cap requested).
_PARSE_ERROR = object()


def _parse_max_iterations(command: str) -> int | None | object:
    """Parse ``--max=N`` or ``--max N`` from a slash-command string.

    Returns:
        * ``None`` — no ``--max`` flag present (no cap).
        * positive ``int`` — parsed cap value.
        * ``_PARSE_ERROR`` sentinel — the flag was present but the value
          was missing, non-integer, zero, or negative. A warning has
          already been emitted; caller should refuse to start.
    """
    tokens = command.split()
    raw: str | None = None
    for i, tok in enumerate(tokens):
        if tok.startswith("--max="):
            raw = tok[len("--max=") :]
            break
        if tok == "--max":
            raw = tokens[i + 1] if i + 1 < len(tokens) else ""
            break
    if raw is None:
        return None

    try:
        n = int(raw)
    except ValueError:
        emit_warning(
            f"bead-factory: --max requires a positive integer, got {raw!r}. "
            "Refusing to start."
        )
        return _PARSE_ERROR
    if n <= 0:
        emit_warning(f"bead-factory: --max must be > 0, got {n}. Refusing to start.")
        return _PARSE_ERROR
    return n


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def handle_bead_factory_command(command: str) -> str | bool:
    """Engage bead-factory: drive build across every ready bead in turn."""
    if state.is_active():
        emit_info("bead-factory is already running.")
        return True

    # Immediate ack: the bd probes below (enforce_single_in_progress,
    # next_ready, claim) can stall noticeably, so emit *something* the
    # instant the command registers — otherwise the UI looks frozen.
    emit_info("bead-factory starting…")

    # Parse --max=N before touching bd: invalid flag → bail loud,
    # don't claim anything.
    max_iterations = _parse_max_iterations(command)
    if max_iterations is _PARSE_ERROR:
        return True

    # Probe first so /bead-factory fails loud on an empty queue or broken bd.
    # Recovery check beats the ready queue: if a prior run errored mid-bead,
    # we must finish (or formally close) that one before starting new work.
    # The startup guard enforces the single-in_progress invariant by
    # auto-reverting any extras to open before we proceed.
    try:
        bead = enforce_single_in_progress()
        if bead is None:
            bead = next_ready()
    except BeadsError as exc:
        emit_warning(f"🔗 bead-factory can't reach `bd`: {exc}")
        return True

    if bead is None:
        emit_info("🦴 No ready beads — bead-factory has nothing to fetch.")
        return True

    bead_id = str(bead.get("id", ""))

    # Last-line-of-defence assertion: matches the same check in
    # :func:`lifecycle.activate_next_bead`. An upstream filter leak
    # here would arm the build loop with an epic and produce the 'cannot
    # close epic' failure we hit in prod. Refuse early.
    if is_excluded_type(bead):
        emit_warning(
            f"🚫 bead-factory refused to start with {bead_id}: it's an excluded "
            f"container type ({bead.get('issue_type', '?')}). "
            "An upstream filter leaked an epic into the chain — this is a bug."
        )
        return True

    # Last-line-of-defence assertion: matches the same check in
    # :func:`lifecycle.activate_next_bead`. ``bd ready`` filters blocked
    # beads server-side and the recovery path
    # (:func:`lifecycle.enforce_single_in_progress`) reverts+drops any
    # blocked stranded bead, so this should never fire. If it does — bd
    # version drift, or a ``blocks`` edge wired between the probe and now
    # — we refuse to start work that ``bd close`` will later reject. This
    # is the blocks-at-claim-time fix: respect work-time blocks at claim time.
    blockers = open_blocker_ids(bead_id)
    if blockers:
        emit_warning(
            f"bead-factory refused to start with {bead_id}: it has open "
            f"blocker(s) [{', '.join(blockers)}]. Respecting work-time blocks "
            "at claim time, not just at close."
        )
        if not is_recovery_bead(bead):
            try:
                revert_to_open(bead_id)
                emit_info(f"reverted {bead_id} to open")
            except BeadsError as exc:
                emit_warning(f"also couldn't revert {bead_id}: {exc}")
        return True

    recovery = is_recovery_bead(bead)
    if recovery:
        emit_warning(
            f"Recovering stranded in_progress bead {bead_id} -- "
            "agent will assess current state before doing new work."
        )

    _ensure_hooks_registered()
    state.start()
    # mypy/pyright: max_iterations is int|None here (sentinel already handled).
    state.get_state().max_iterations = max_iterations  # type: ignore[assignment]

    # Walk the hierarchy top-down: claim the parent epic FIRST, then
    # the child bead. bd's UI caches per-parent children-by-status
    # views, so flipping a leaf to in_progress under a still-open
    # parent produces a stale tree until the user navigates back to
    # the parent. Going parent-first keeps the hierarchy consistent
    # at every observable moment. Soft-fails — never blocks the chain.
    ensure_epic_in_progress(bead)

    if not recovery:
        try:
            claim(bead_id)
        except BeadsError as exc:
            emit_warning(f"🔗 bead-factory couldn't claim {bead_id}: {exc}")
            state.stop()
            return True
    # Recovery beads are already in_progress — re-claiming is at best a
    # no-op and at worst a bd error, so we skip the call entirely.

    state.get_state().current_bead = bead

    # FB-8: map the bead's recognized execution_* metadata
    # hints (effort/model/agent_type) onto code-puppy's serial knobs before
    # arming the build loop, so they shape this build pass. Soft-fails per hint and
    # is a no-op when the bead carries no recognized metadata.
    applied_hints = apply_execution_hints(bead)
    if applied_hints:
        emit_info(f"\U0001f9ea execution hints: {'; '.join(applied_hints)}")

    # De-dup arming (bead-factory-462): the inspector copy is always the
    # FULL render; the implementor copy is slimmed to scaffolding-only when
    # the bead's content is pinned into the system prompt (is_pin_active),
    # so the implementor isn't handed the same contract twice. The pin
    # guard is True here — state is active and current_bead was just set.
    build_prompt, inspector_prompt = build_prompts_for_arming(
        bead, recovery=recovery, inject_content=is_pin_active()
    )
    # Thread the bead identity + recovery flag through so the build loop can
    # re-fetch the LIVE bead at inspection time (bead-factory-2mb) and grade
    # against post-claim notes/edits rather than this frozen snapshot.
    build_state.start(
        build_prompt,
        inspector_prompt=inspector_prompt,
        bead_id=bead_id,
        recovery=recovery,
    )

    emit_success("🔗 BEAD-FACTORY ENGAGED!")
    emit_info(f"First bead: {bead_id} — {bead.get('title', '')}")
    if max_iterations is not None:
        emit_info(f"Safety cap: stopping after {max_iterations} bead(s).")
    emit_info("Will claim → build → close → repeat until `bd ready` is empty.")
    emit_info("Press Ctrl+C to halt.")
    return build_prompt


# ---------------------------------------------------------------------------
# interactive_turn_end / interactive_turn_cancel hooks
# (registered lazily by _ensure_hooks_registered)
# ---------------------------------------------------------------------------


async def _on_interactive_turn_end(
    agent: Any,
    prompt: str,
    result: Any = None,
    *,
    success: bool = True,
    error: BaseException | None = None,
) -> dict[str, Any] | None:
    """Drive the bead → build → close → next-bead loop.

    Returns None whenever the build loop should keep driving (i.e., build
    mode still active for the current bead) or when we've run out of
    beads. Returns a continuation dict only when we're handing the build
    loop a NEW bead to chew on.
    """
    del agent, prompt, result, success, error

    if not state.is_active():
        return None

    # The build loop is mid-build — let it cook. We're guaranteed to run AFTER
    # the build decision on each turn because _ensure_hooks_registered
    # registers the in-package build-loop hook ahead of this one (see its
    # docstring for the explicit ordering contract).
    if build_state.is_active():
        return None

    # The build loop just stopped — that means the bead is either complete
    # (inspectors passed) or the build loop cancelled. We can't distinguish here,
    # but interactive_turn_cancel runs for cancellation and would have
    # already stopped us; so reaching this branch with state.active
    # still True implies success.
    # close_current_bead_success() shells out to `bd`
    # (bd close / bd show / bd update) synchronously. Running it inline
    # here would block code_puppy's interactive event loop for the
    # duration of the subprocess — up to ~45s worst case under retries.
    # asyncio.to_thread() hands the blocking work to a worker thread and
    # `await` yields the loop so the UI stays responsive. We deliberately
    # `await` it to completion BEFORE the is_active() check and BEFORE
    # touching activate_next_bead below: the close→check→activate sequence
    # must stay strictly ordered (no premature parallelism), and only one
    # worker thread is ever in flight at a time, so the existing
    # single-threaded ordering and state-mutation guarantees are
    # preserved. The 15s timeout + retry/backoff live inside `bd`'s
    # _run_bd and are untouched by moving the call to a thread.
    just_closed = await asyncio.to_thread(close_current_bead_success)
    # If close-failure stopped the chain, close_current_bead_success
    # already emitted the explanation and halted state. Bow out cleanly
    # rather than barreling into activate_next_bead and claiming a new
    # bead on top of the one we couldn't close.
    if not state.is_active():
        return None
    # NOTE: Per-bead rollup removed.
    #
    # The cascade mechanism in ``bd epic close-eligible`` runs server-side:
    # closing A's last child closes A, then checks if A's parent B is now
    # eligible, closes B, checks parent C, etc. Called after EVERY bead
    # close, this cascade can unexpectedly close unrelated epics.
    #
    # Example: closing bead N in molecule-epic A triggers rollup, which
    # cascades to close A's parent (epic B), which was the last child of
    # an unrelated epic C. Closing C closes all its orphaned children,
    # including three tracking beads that had no relationship to A or N.
    #
    # Fix: Only call rollup_completed_epics() ONCE, at the end of the
    # session, when the queue is empty. See activate_next_bead() for the
    # final rollup call. This prevents multiple cascade iterations from
    # sweeping up unrelated beads. The trade-off: parent epics may not
    # close until the next session's rollup, but we gain data safety.
    #
    # Note: starting the next bead's parent epic is handled inside
    # activate_next_bead, where we actually know which bead got
    # claimed. Doing it here would be premature.
    #
    # activate_next_bead() is the other bd-heavy call in
    # this hook (pick_next_bead → bd ready/list/show, claim, gate/rollup
    # probes). Off-load it to a worker thread for the same reason as the
    # close above. It runs strictly AFTER the close has fully completed
    # and the is_active() short-circuit has passed, so the activation
    # sees the post-close state exactly as it did when both ran inline.
    return await asyncio.to_thread(activate_next_bead, just_closed)


async def _on_interactive_turn_cancel(
    prompt: str, *, reason: str = "cancelled"
) -> None:
    """Bow out on Ctrl+C; leave the in-flight bead in_progress for recovery.

    Declared ``async`` to match its sibling :func:`_on_interactive_turn_end`
    and the host's async dispatch contract: ``on_interactive_turn_cancel``
    (code_puppy/callbacks.py) is an ``async`` invoker that calls each
    callback and ``await``s the result iff it's a coroutine. A plain ``def``
    works today only because the dispatcher tolerates sync callbacks; making
    this ``async`` removes that latent coupling so the two interactive-turn
    hooks present one consistent contract. The body stays fully synchronous
    (no ``await``) — there's no I/O to suspend on.

    The bead stays **in_progress** deliberately. The next ``/bead-factory``
    run hits the recovery tier first (:func:`pick_next_bead` tier 0) and
    re-prompts the agent with :data:`prompt._RECOVERY_PREAMBLE`,
    instructing it to assess what's already on disk before doing any new
    work. This keeps partial work paired with its bead — no orphaning,
    no stranding, because the invariant guard finds it next time.

    The chain itself still stops cleanly here — we only halt the loop,
    not the bead's status. Recovery is a startup-time concern, handled
    by :func:`lifecycle.enforce_single_in_progress` /
    :func:`lifecycle.pick_next_bead` on the next run.
    """
    del prompt
    if not state.is_active():
        return
    bead_id = state.get_state().current_bead_id
    state.stop()
    emit_warning(f"🔗 bead-factory halted due to {reason}.")
    if not bead_id:
        return
    emit_system_message(
        f"🔖 Bead {bead_id} left in_progress — the next /bead-factory run "
        "will resume it with a recovery preamble so the agent assesses "
        "the current state before doing new work."
    )
