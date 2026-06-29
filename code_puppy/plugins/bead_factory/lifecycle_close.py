"""Bead close + rollup + epic-claim helpers for the bead_factory chain.

Extracted verbatim from :mod:`lifecycle` during the bead_factory migration so
the close/rollup/gate-probe/epic-claim transitions live in one cohesive module
under the 600-line cap. Behavior is unchanged — these are the same state
transitions :mod:`lifecycle` previously owned inline. Every function here is
deliberately stateful (mutates :mod:`state`, shells out via :mod:`beads`) but
self-contained. DO NOT add hook *registration* here.
"""

from __future__ import annotations

from typing import Any

from code_puppy.messaging import (
    emit_info,
    emit_success,
    emit_warning,
)

from . import state
from .beads import BeadsError, is_excluded_type
from .beads_reads import (
    extract_parent_epic_id,
    is_pinned,
    show,
)
from .beads_writes import (
    check_gates,
    claim,
    close,
    close_eligible_epics,
    has_epic_in_progress,
    revert_to_open,
)

__all__ = [
    "close_current_bead_success",
    "rollup_completed_epics",
    "probe_resolved_gates",
    "ensure_epic_in_progress",
]


# bd refuses to close a bead that still has open blockers, surfacing a
# message containing "blocked by open issue(s)" (the "(s)" is grammatical
# pluralisation, so we key off the singular stem). bead-factory's
# :func:`beads._run_bd` wraps that stderr verbatim into the BeadsError
# string, so a substring match against ``str(exc)`` is the authoritative
# (if string-keyed) signal. We keep the match deliberately NARROW: on any
# miss we degrade to the historical halt-loudly behavior, which is safe —
# never silent.
_BLOCKED_CLOSE_MARKER: str = "blocked by open issue"


def _is_blocked_close_error(exc: BeadsError) -> bool:
    """True iff ``exc`` is bd's *recoverable* "blocked by open issues" refusal.

    This distinguishes the one **recoverable** close-failure class — a
    blocker (typically a bug filed via the Bug Discovery Protocol with
    ``--blocks=<this bead>``) is still open, so bd won't let us close —
    from every other (infra-class) BeadsError, which must still halt the
    chain loudly. Narrow by design: an unrecognised message returns
    ``False`` and the caller falls back to the safe halt path.
    """
    return _BLOCKED_CLOSE_MARKER in str(exc).lower()


def close_current_bead_success() -> dict[str, Any] | None:
    """Close the bead we were just working on, if any.

    Returns the **just-closed bead dict** (or ``None`` if there was no
    current bead) so the caller can use fields like the parent epic
    when picking the next bead — see :func:`activate_next_bead`.
    Whether the ``bd close`` call succeeded or not, the returned dict
    still represents the bead we were working on; that's the right
    signal for epic-affinity routing (we *intended* to finish that
    epic's work).

    **Close-failure handling.** If ``bd close`` raises, we split on the
    error class (*a "blocked by open issues" close failure is
    recoverable, not a chain-halt*):

      * **Recoverable — "blocked by open issue(s)".** bd refused because
        a blocker is still open, typically a bug filed *during this
        bead's own run* with ``--blocks=<this bead>`` per the Bug
        Discovery Protocol. That is a documented, self-healing state,
        not a fault. We :func:`revert_to_open` the bead, clear
        ``current_bead``, and **continue** the chain. The next
        iteration's tier-0 (``_unblocked_strands``) and tier-1
        (blocking-bug routing) machinery drives the blocker first, then
        re-drives this bead — the recovery net that already exists.
        Detected narrowly via :func:`_is_blocked_close_error`; if the
        revert itself fails, that *is* infra-class and we fall through
        to the halt path below.
      * **Infra-class — everything else** (bd outage, permission issue,
        schema drift). We **leave the bead in_progress** (reverting
        would orphan partial work — the next ``bd ready`` could hand us
        a different bead and the half-done changes would silently attach
        to no tracked work) and **stop the chain**. Staying in_progress
        means the next ``/bead-factory`` run's recovery tier picks it up
        and re-prompts with the recovery preamble, so the agent
        assesses the current state before doing anything new. Halt
        loudly rather than barreling on.

    The caller distinguishes the success vs. failure case by checking
    ``state.is_active()`` after the call: if False, the chain was
    stopped here and the caller should bail without claiming another
    bead. (A recoverable blocked-close revert leaves the chain *active*,
    so the caller proceeds to pick the next bead as usual.)
    """
    just_closed = state.get_state().current_bead
    if not just_closed:
        return None
    bead_id = state.get_state().current_bead_id or ""

    # Last-line-of-defence assertion: bead-factory must never attempt to
    # close a container bead (epic, etc.). The server-side filter and
    # the client-side filter in :func:`beads.list_in_progress` /
    # :func:`beads.next_ready` should both have caught this upstream,
    # but if *both* failed and an epic somehow reached current_bead,
    # ``bd close`` would fail with 'open child issue(s)' and halt the
    # chain anyway — we may as well refuse here with a clearer message
    # AND revert the epic so it doesn't sit incorrectly in_progress.
    #
    # Why revert here but NOT on a normal close-failure? Two reasons:
    #   1. An in_progress epic is categorically broken (epics are
    #      containers, never doable work). Leaving it stranded would
    #      silently corrupt ``bd status`` displays.
    #   2. The tier-0 recovery path in :func:`pick_next_bead` reads
    #      :func:`_unblocked_strands` (which wraps
    #      :func:`beads.list_recoverable_strands`), and that query filters epics
    #      out via ``--exclude-type=epic``. So a stranded epic would
    #      never be picked up by the recovery preamble flow — it would
    #      just sit there forever. Reverting is the only path to sanity.
    if is_excluded_type(just_closed):
        emit_warning(
            f"🚫 bead-factory refused to close {bead_id}: it's an excluded "
            f"container type ({just_closed.get('issue_type', '?')}). "
            "An upstream filter leaked an epic into the chain — this is a bug."
        )
        try:
            revert_to_open(bead_id)
            emit_info(f"🔄 reverted {bead_id} to open")
        except BeadsError as revert_exc:
            emit_warning(f"🔗 also couldn't revert {bead_id}: {revert_exc}")
        emit_warning(
            "🔗 bead-factory stopping after epic-leak detection — "
            "investigate before re-running."
        )
        state.stop()
        state.get_state().current_bead = None
        return just_closed

    # Mid-flight pin guard (FB-12 / lifecycle#1). bead-factory claims a
    # bead while it's open, but another agent/tool can flip it to
    # ``pinned`` *after* the claim. Closing a pinned bead REQUIRES
    # ``--force`` (field guide §III), which :func:`beads.close` never
    # passes — so a pinned bead reaching close() would fail and halt the
    # whole loop (same stall family as the epic-close-fail hazard). We
    # re-read the live status here and, if it's been pinned, *respect
    # the pin*: a human deliberately parked this bead to stay open
    # indefinitely, so force-closing it would override that intent.
    # Instead we drop it as the current bead and trot on — the chain
    # keeps moving and the pin stands. The bead won't be re-picked
    # (``bd ready`` and recovery both exclude ``pinned``), so this can't
    # loop. We do NOT bump_completed: nothing was closed.
    if is_pinned(bead_id):
        emit_warning(
            f"bead {bead_id} was pinned mid-flight -- respecting the pin "
            "(closing a pinned bead needs --force, which bead-factory won't "
            "do over a human's explicit park). Leaving it pinned and moving "
            "on; the chain keeps trotting."
        )
        state.get_state().current_bead = None
        return just_closed

    try:
        close(bead_id, reason="bead-factory: LLM inspectors passed")
    except BeadsError as exc:
        # Two distinct error classes hide behind one BeadsError:
        #
        #   1. RECOVERABLE — bd refused because a blocker is still open
        #      (e.g. a bug filed mid-run with --blocks=<this bead> per the
        #      Bug Discovery Protocol). This is a documented, self-healing
        #      state, NOT a fault: revert the bead to open and let the next
        #      iteration's tier-0 (_unblocked_strands) + tier-1
        #      (blocking-bug routing) machinery drive the blocker first and
        #      re-drive this bead afterwards. The chain CONTINUES.
        #
        #   2. INFRA — anything else (bd outage, permission, schema drift).
        #      Genuinely wrong: halt loudly, exactly as before.
        if _is_blocked_close_error(exc):
            emit_info(
                f"🔗 bead-factory can't close {bead_id} yet — it's blocked by an "
                "open issue (likely a bug filed during this run). This is "
                "recoverable, not a fault: reverting to open so the next "
                "iteration drives the blocker first, then re-drives this bead."
            )
            try:
                revert_to_open(bead_id)
            except BeadsError as revert_exc:
                # A failed revert IS infra-class — fall back to the safe
                # halt path rather than leaving the bead wedged in_progress.
                emit_warning(
                    f"🔗 bead-factory couldn't revert {bead_id} after a blocked "
                    f"close: {revert_exc}. Halting; investigate before "
                    "re-running."
                )
                state.stop()
                return just_closed
            emit_info(
                f"🔄 reverted {bead_id} to open — when it's re-driven, prior "
                "work may already satisfy the acceptance criteria; verify "
                "before redoing it to avoid burning tokens on a needless redo."
            )
            state.get_state().current_bead = None
            return just_closed

        emit_warning(f"🔗 bead-factory couldn't close {bead_id}: {exc}")
        # Leave the bead in_progress on purpose — see docstring.
        # The next /bead-factory run will recover it via tier-0 and
        # re-prompt with the recovery preamble so the agent assesses
        # current state (which may already satisfy the inspectors) before
        # doing any new work.
        emit_warning(
            f"🔖 Bead {bead_id} left in_progress — the next /bead-factory run "
            "will resume it with a recovery preamble. Stopping chain now; "
            "investigate the close failure before re-running."
        )
        state.stop()
        # Note: deliberately NOT clearing current_bead here. The chain
        # is stopping; the field gets cleared on the next start() call.
        return just_closed
    else:
        n = state.get_state().bump_completed()
        emit_success(f"🔗 bead-factory closed {bead_id} (#{n} completed this run)")
        state.get_state().current_bead = None
    return just_closed


def rollup_completed_epics() -> None:
    """Auto-close any epics whose children are now all complete.

    Called **once per session** when the queue is empty (drain pass in
    :func:`activate_next_bead`), NOT after every individual child close.
    This is mitigation for the over-close bug: bd's
    ``epic close-eligible`` cascade can unexpectedly close unrelated
    epics if called too frequently. By calling once-per-session, we
    dramatically reduce the surface for unintended side effects.

    bd handles cascades natively: closing epic A's last child may make
    A's parent epic B eligible too, and one call rolls both up.

    **Soft-fails by design.** Epic rollup is a courtesy cleanup, not
    bead-factory's core mission. A flaky/missing/old ``bd epic`` should
    log a warning and let the chain keep trotting — losing a rollup
    pass is way less bad than stranding the user's queue.
    """
    try:
        closed = close_eligible_epics()
    except BeadsError as exc:
        emit_warning(f"🎯 bead-factory: epic rollup failed (continuing): {exc}")
        return
    for epic in closed:
        epic_id = str(epic.get("id", "<unknown>"))
        title = str(epic.get("title", "")).strip()
        suffix = f" — {title}" if title else ""
        emit_success(f"🎯 epic {epic_id} rolled up (all children complete){suffix}")


def probe_resolved_gates() -> bool:
    """Re-evaluate open gates on an empty queue; report if any resolved.

    Called once from :func:`activate_next_bead` the moment ``bd ready``
    comes back empty, *before* the chain declares itself done. Resolvable
    gate types (``timer`` / ``gh:run`` / ``gh:pr`` / ``bead``) keep their
    target issues out of ``bd ready`` until the gate closes, and nothing
    else in bead-factory ever pokes them. So an empty queue might really be
    a queue waiting on a now-satisfied gate — we ask bd to close those
    and re-open their targets for the next pick.

    Returns ``True`` if at least one gate resolved (the caller should
    re-probe ``bd ready`` rather than stop), ``False`` otherwise.

    **Soft-fails by design.** Like :func:`rollup_completed_epics`, this
    is a courtesy nudge, not bead-factory's core mission. A flaky / missing
    / old ``bd gate`` logs a warning and returns ``False`` so the chain
    finishes its drain cleanly — losing a gate probe is far less bad than
    halting the loop.
    """
    try:
        counts = check_gates()
    except BeadsError as exc:
        emit_warning(f"⏳ bead-factory: gate check failed (continuing): {exc}")
        return False

    resolved = counts.get("resolved", 0)
    escalated = counts.get("escalated", 0)
    if resolved:
        emit_success(
            f"⏳ {resolved} gate(s) resolved on the empty-queue probe — "
            "re-opening their targets and re-checking for ready work."
        )
    if escalated:
        emit_warning(
            f"⏳ {escalated} gate(s) escalated (expired/failed) during the "
            "empty-queue probe — these need a human look."
        )
    return bool(resolved)


# ---------------------------------------------------------------------------
# Epic / bead claim helpers
# ---------------------------------------------------------------------------


def ensure_epic_in_progress(bead: dict[str, Any] | None) -> None:
    """If no epic is in_progress, claim ``bead``'s parent epic.

    Called whenever bead-factory has just claimed a new child bead — at
    chain startup and from inside :func:`activate_next_bead`. The goal
    is to give humans (and dashboards) a true "what is bead-factory
    working on" signal: the in_progress epic must be the parent of the
    bead actually in flight, not whatever happened to top ``bd ready
    --type=epic``.

    When the active bead has no parent epic, we no-op rather than
    guessing — surfacing no epic beats surfacing the wrong one.

    **Soft-fails by design.** This is a courtesy status update, not a
    gate — if bd is flaky we log and keep trotting. Never stalls the
    chain.
    """
    if not bead:
        return

    epic_id = extract_parent_epic_id(bead)
    if not epic_id:
        # Bead is a top-level item with no parent epic. Deliberately
        # do nothing rather than guess.
        return

    try:
        if has_epic_in_progress():
            return
    except BeadsError as exc:
        emit_warning(
            f"🎯 bead-factory: epic in-progress check failed (continuing): {exc}"
        )
        return

    # Try to enrich the log line with the epic's title. Pure cosmetics —
    # any failure here is silently swallowed, the claim still proceeds.
    title = ""
    try:
        epic = show(epic_id)
    except BeadsError:
        epic = None
    if epic:
        title = str(epic.get("title", "")).strip()

    try:
        claim(epic_id)
    except BeadsError as exc:
        emit_warning(
            f"🎯 bead-factory: couldn't start epic {epic_id} (continuing): {exc}"
        )
        return

    suffix = f" — {title}" if title else ""
    emit_info(f"🎯 bead-factory started epic {epic_id}{suffix}")
