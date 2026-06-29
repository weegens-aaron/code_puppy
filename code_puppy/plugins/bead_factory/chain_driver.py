"""bead_factory chain driver: the bead -> /goal -> close -> next loop.

The queue-driver *logic* migrated from bead-chain's ``register_callbacks``:
CLI ``--max=N`` parsing, the ``/bead-chain`` command handler, the
``interactive_turn_end`` / ``interactive_turn_cancel`` hook handlers, and the
lazy hook-registration helper. Behavior is identical to bead-chain — only the
home package changed (intra-plugin imports now resolve in the bead_factory
namespace).

This module intentionally performs **no** module-scope command/callback
registration: wiring the plugin entry point (registering ``/bead-chain``, the
``run_shell_command`` close-guard hook, etc.) is a dedicated downstream bead.
``_ensure_hooks_registered`` still registers the interactive-turn hooks lazily
on first command use, exactly as before, so the queue-driver behavior — the
recovery tier, single-in_progress invariant, blocker gate, end-of-session
rollup, execution-hint mapping, and the wiggum-after-us hook ordering — is
preserved unchanged.

The wiggum ``state`` prerequisite import is left pointing at
``code_puppy.plugins.wiggum`` on purpose; rewiring that cross-import to
bead_factory's own state is a separate downstream bead.

Module layout (unchanged from bead-chain):

  * :mod:`lifecycle` — state transitions: close, revert, invariant guard,
    next-bead waterfall, wiggum arming.
  * :mod:`beads` — thin subprocess wrapper around ``bd``.
  * :mod:`prompt` — bead-dict -> goal-prompt formatting.
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
# wiggum prerequisite check (bead_chain-c87)
# ---------------------------------------------------------------------------
#
# bead-chain is NOT a goal engine — it's a queue driver that delegates the
# LLM-judged completion loop to wiggum's /goal mode. wiggum is therefore a
# hard prerequisite (documented in the README). Historically this was a bare
# top-level ``from code_puppy.plugins.wiggum import state`` which, when wiggum
# wasn't loaded, raised a raw ImportError. The plugin loader caught it and the
# app survived, but the user saw a cryptic
# ``Failed to import callbacks from user plugin bead_chain: No module named
# 'code_puppy.plugins.wiggum'`` instead of an actionable message.
#
# We now import wiggum defensively: on failure we keep the module importable
# (so the loader logs nothing alarming), record the absence in
# ``_WIGGUM_AVAILABLE``, log one clear human-readable line (below, after the
# remaining imports), and make ``/bead-chain`` degrade gracefully — it tells
# the user wiggum is required rather than blowing up. When wiggum IS present
# this is a single successful import with zero behavioural change.
try:
    from code_puppy.plugins.wiggum import state as wiggum_state

    _WIGGUM_AVAILABLE = True
except ImportError:
    wiggum_state = None  # type: ignore[assignment]
    _WIGGUM_AVAILABLE = False

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
from .prompt import format_bead_as_goal

logger = logging.getLogger(__name__)

# Human-readable message shown when the wiggum prerequisite is missing. Kept
# as a module constant so the import-time log line and the runtime
# ``/bead-chain`` warning say exactly the same thing (and tests can assert it).
_WIGGUM_MISSING_MESSAGE = (
    "\U0001f517 bead-chain requires the wiggum plugin — install/enable it to "
    "use /bead-chain. bead-chain drives wiggum's /goal mode one bead at a "
    "time, so it cannot run without it."
)

if not _WIGGUM_AVAILABLE:
    # One clear line in the loader output instead of a raw ImportError
    # traceback. (bead_chain-c87)
    logger.warning(_WIGGUM_MISSING_MESSAGE)

__all__ = ["handle_bead_chain_command"]

# ---------------------------------------------------------------------------
# Lazy hook registration
# ---------------------------------------------------------------------------

_HOOKS_REGISTERED = False


def _ensure_hooks_registered() -> None:
    """Register our turn-end / cancel hooks exactly once, lazily.

    By deferring until the first /bead-chain invocation we guarantee
    wiggum (loaded at startup) is already in the callback list ahead
    of us — so wiggum's continuation-dict choice happens before we
    decide whether to grab the next bead.
    """
    global _HOOKS_REGISTERED
    if _HOOKS_REGISTERED:
        return
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
            f"🔗 bead-chain: --max requires a positive integer, got {raw!r}. "
            "Refusing to start."
        )
        return _PARSE_ERROR
    if n <= 0:
        emit_warning(f"🔗 bead-chain: --max must be > 0, got {n}. Refusing to start.")
        return _PARSE_ERROR
    return n


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def handle_bead_chain_command(command: str) -> str | bool:
    """Engage bead-chain: drive /goal across every ready bead in turn."""
    if not _WIGGUM_AVAILABLE:
        # Graceful degradation: wiggum (our /goal engine) isn't loaded, so
        # there's nothing to drive. Tell the user plainly instead of letting
        # a later ``wiggum_state`` dereference raise. (bead_chain-c87)
        emit_warning(_WIGGUM_MISSING_MESSAGE)
        return True

    if state.is_active():
        emit_info("🔗 bead-chain is already running.")
        return True

    # Immediate ack: the bd probes below (enforce_single_in_progress,
    # next_ready, claim) can stall noticeably, so emit *something* the
    # instant the command registers — otherwise the UI looks frozen.
    emit_info("🔗 bead-chain starting…")

    # Parse --max=N before touching bd: invalid flag → bail loud,
    # don't claim anything.
    max_iterations = _parse_max_iterations(command)
    if max_iterations is _PARSE_ERROR:
        return True

    # Probe first so /bead-chain fails loud on an empty queue or broken bd.
    # Recovery check beats the ready queue: if a prior run errored mid-bead,
    # we must finish (or formally close) that one before starting new work.
    # The startup guard enforces the single-in_progress invariant by
    # auto-reverting any extras to open before we proceed.
    try:
        bead = enforce_single_in_progress()
        if bead is None:
            bead = next_ready()
    except BeadsError as exc:
        emit_warning(f"🔗 bead-chain can't reach `bd`: {exc}")
        return True

    if bead is None:
        emit_info("🦴 No ready beads — bead-chain has nothing to fetch.")
        return True

    bead_id = str(bead.get("id", ""))

    # Last-line-of-defence assertion: matches the same check in
    # :func:`lifecycle.activate_next_bead`. An upstream filter leak
    # here would arm wiggum with an epic and produce the 'cannot
    # close epic' failure we hit in prod. Refuse early.
    if is_excluded_type(bead):
        emit_warning(
            f"🚫 bead-chain refused to start with {bead_id}: it's an excluded "
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
    # is the bdboard-oals fix: respect work-time blocks at claim time.
    blockers = open_blocker_ids(bead_id)
    if blockers:
        emit_warning(
            f"bead-chain refused to start with {bead_id}: it has open "
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
            emit_warning(f"🔗 bead-chain couldn't claim {bead_id}: {exc}")
            state.stop()
            return True
    # Recovery beads are already in_progress — re-claiming is at best a
    # no-op and at worst a bd error, so we skip the call entirely.

    state.get_state().current_bead = bead

    # FB-8 (bead_chain-9n3): map the bead's recognized execution_* metadata
    # hints (effort/model/agent_type) onto code-puppy's serial knobs before
    # arming wiggum, so they shape this /goal pass. Soft-fails per hint and
    # is a no-op when the bead carries no recognized metadata.
    applied_hints = apply_execution_hints(bead)
    if applied_hints:
        emit_info(f"\U0001f9ea execution hints: {'; '.join(applied_hints)}")

    goal_prompt = format_bead_as_goal(bead, recovery=recovery)
    wiggum_state.start(goal_prompt, mode="goal")

    emit_success("🔗 BEAD-CHAIN ENGAGED!")
    emit_info(f"First bead: {bead_id} — {bead.get('title', '')}")
    if max_iterations is not None:
        emit_info(f"Safety cap: stopping after {max_iterations} bead(s).")
    emit_info("Will claim → /goal → close → repeat until `bd ready` is empty.")
    emit_info("Press Ctrl+C to halt.")
    return goal_prompt


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
    """Drive the bead → /goal → close → next-bead loop.

    Returns None whenever wiggum should keep driving (i.e., goal mode
    still active for the current bead) or when we've run out of beads.
    Returns a continuation dict only when we're handing wiggum a NEW
    bead to chew on.
    """
    del agent, prompt, result, success, error

    if not state.is_active():
        return None

    # Wiggum is mid-goal — let it cook. We're guaranteed to run AFTER
    # wiggum on each turn because we registered later (see
    # _ensure_hooks_registered docstring).
    if wiggum_state.is_active():
        return None

    # Wiggum just stopped — that means the bead is either complete
    # (judges passed) or wiggum cancelled. We can't distinguish here,
    # but interactive_turn_cancel runs for cancellation and would have
    # already stopped us; so reaching this branch with state.active
    # still True implies success.
    # bead_chain-u0b: close_current_bead_success() shells out to `bd`
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
    # NOTE: Per-bead rollup removed (bead_chain-tfn fix).
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
    # bead_chain-u0b: activate_next_bead() is the other bd-heavy call in
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

    The bead stays **in_progress** deliberately. The next ``/bead-chain``
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
    emit_warning(f"🔗 bead-chain halted due to {reason}.")
    if not bead_id:
        return
    emit_system_message(
        f"🔖 Bead {bead_id} left in_progress — the next /bead-chain run "
        "will resume it with a recovery preamble so the agent assesses "
        "the current state before doing new work."
    )
