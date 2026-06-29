"""Bead lifecycle helpers — the state-transition brain of bead-factory.

This module owns the *state transitions*: how to close, revert, enforce
the single-in_progress invariant, pick the next bead, and arm the build
loop for the next iteration. The companion :mod:`chain_driver` module
owns the *wiring*: slash-command registration, hook registration,
the hook handlers themselves, CLI flag parsing.

Functions here are deliberately stateful (they mutate :mod:`state` and
shell out via :mod:`beads`) but each is self-contained — same input
state + same bd database → same output. That makes them safe to call
from any hook handler in any order without coupling to specific call
sites.

DO NOT add hook *registration* here. Hooks live in :mod:`chain_driver`
so contributors have one obvious place to discover what bead-factory
listens to.
"""

from __future__ import annotations

from typing import Any

from code_puppy.messaging import (
    emit_info,
    emit_success,
    emit_warning,
)

from . import state

try:
    # bead-factory is a queue driver that delegates the LLM-judged completion
    # loop to the build loop. Post-merge that loop lives IN THIS
    # package — its state is the in-package :mod:`build_state` module (no more
    # cross-import of ``code_puppy.plugins.wiggum``). We keep the defensive
    # import so this module still imports cleanly if build_state is somehow
    # broken: chain_driver gates every code path that would actually call
    # build_state behind an availability check, so a None here is never
    # dereferenced. (bead_chain-c87)
    from . import build_state
except ImportError:  # pragma: no cover - exercised via chain_driver
    build_state = None  # type: ignore[assignment]
from .beads import BeadsError, RECOVERABLE_STATUSES, is_excluded_type
from .beads_reads import (
    extract_parent_epic_id,
    list_recoverable_strands,
    next_blocking_bug,
    next_ready,
    next_ready_in_epic,
    open_blocker_ids,
    show,
)
from .beads_writes import (
    claim,
    revert_to_open,
)
from .execution_hints import apply_execution_hints
from .fan_out_gate import _fan_out_gate_verdict
from .lifecycle_close import (
    close_current_bead_success,
    ensure_epic_in_progress,
    probe_resolved_gates,
    rollup_completed_epics,
)
from .prompt import format_bead_as_build


__all__ = [
    "is_recovery_bead",
    "enforce_single_in_progress",
    "close_current_bead_success",
    "rollup_completed_epics",
    "probe_resolved_gates",
    "ensure_epic_in_progress",
    "pick_next_bead",
    "activate_next_bead",
]

# Statuses that mark a picked bead as *already in flight* — i.e. residue
# from a prior run that crashed/cancelled before the LLM judges could
# rule. A bead in any of these was claimed (or hooked) but not closed,
# so bead-factory *recovers* it (re-drives with the recovery preamble)
# rather than re-claiming. Sourced from :data:`beads.RECOVERABLE_STATUSES`
# so the recovery query and the recovery-vs-fresh decision can never
# drift apart. See :func:`is_recovery_bead`.
_RECOVERY_STATUSES: frozenset[str] = frozenset(s.lower() for s in RECOVERABLE_STATUSES)


def is_recovery_bead(bead: dict[str, Any] | None) -> bool:
    """True if ``bead`` was already in flight when bead-factory picked it.

    The deliberate one-bead-at-a-time discipline means we should never
    see an in_progress (or hooked) bead at chain-start or between
    iterations — if we do, it's residue from a prior crashed/cancelled
    run, or a strand another agent left mid-flight. Centralised here so
    the recovery-mode signal stays consistent across both the startup
    path and the mid-chain pick path. DRY.

    Membership-tests against :data:`_RECOVERY_STATUSES` (case-insensitive)
    so a bead flipped to ``hooked`` mid-flight is recovered — not
    re-claimed as if it were fresh (FB-12 / lifecycle#2).
    """
    if not bead:
        return False
    return str(bead.get("status", "")).strip().lower() in _RECOVERY_STATUSES


# ---------------------------------------------------------------------------
# Startup invariant guard
# ---------------------------------------------------------------------------


def _unblocked_strands() -> list[dict[str, Any]]:
    """List the stranded in-flight non-epic beads that are *actually workable*.

    Enumerates every recoverable status (in_progress **and** hooked —
    see :data:`beads.RECOVERABLE_STATUSES`) via
    :func:`beads.list_recoverable_strands`, so a bead flipped to
    ``hooked`` mid-flight is no longer invisible to recovery (FB-12 /
    lifecycle#2).

    A stranded bead with open ``blocks`` dependencies must **never** be
    re-driven — that is the bdboard-oals bug: the recovery tier bypasses
    the ready frontier, so a bead claimed-while-ready and later
    re-blocked would get run to completion and only trip at ``bd
    close``. We refuse to perpetuate that: any blocked stranded bead is
    **reverted to open** (so it re-enters the queue behind its blockers)
    and dropped from the workable set.

    Eviction is best-effort — if the revert itself fails we log and still
    drop the bead from the workable list, so the chain never picks it up
    this pass regardless.

    Raises :class:`BeadsError` from the underlying ``bd list`` so callers
    keep the same soft-fail contract they had with
    :func:`beads.list_recoverable_strands`.
    """
    items = list_recoverable_strands()
    workable: list[dict[str, Any]] = []
    for bead in items:
        bead_id = str(bead.get("id", ""))
        blockers = open_blocker_ids(bead_id)
        if blockers:
            emit_warning(
                f"bead-factory: stranded in_progress bead {bead_id} is blocked "
                f"by open issue(s) [{', '.join(blockers)}] -- refusing to re-drive "
                "it and reverting to open (work-time blocks must be respected, "
                "not just at close-time)."
            )
            try:
                revert_to_open(bead_id)
                emit_info(f"reverted blocked {bead_id} to open")
            except BeadsError as exc:
                emit_warning(
                    f"also couldn't revert {bead_id} (still dropping it from "
                    f"this pass): {exc}"
                )
            continue
        workable.append(bead)
    return workable


def enforce_single_in_progress() -> dict[str, Any] | None:
    """Pick the head in_progress bead for recovery; leave the rest alone.

    The chain's contract is *one bead at a time*. Multiple in_progress
    beads should be impossible if the cancel hook and close-failure
    paths do their job — but hard crashes (SIGKILL, power loss, OS
    reboot) bypass every Python-level handler, and old sessions may
    have left residue.

    Behavior:

      * Zero in_progress beads → return ``None`` (clean slate; startup
        will pick a fresh ready bead).
      * One in_progress bead → return it (normal recovery).
      * More than one → return the head, **leave the rest in_progress**,
        and emit a warning so the user knows. The extras will be
        recovered one-at-a-time on subsequent iterations within this
        same run via :func:`pick_next_bead`'s tier-0 recovery branch.
        This preserves the work-paired-with-its-bead invariant: every
        in_progress bead represents real partial work on disk that the
        agent must assess via the recovery preamble before doing more.

    Beads with open work-time blockers are filtered out (and reverted
    to open) by :func:`_unblocked_strands` before any of the above
    — a blocked stranded bead is never recovered/re-driven (bdboard-oals).

    Soft-fails by design: a bd outage here shouldn't block the chain
    from running. If listing fails we emit a warning and return
    ``None``, letting the normal startup probe handle whatever it can
    see.
    """
    try:
        items = _unblocked_strands()
    except BeadsError as exc:
        emit_warning(
            f"🔗 bead-factory: couldn't enumerate in_progress beads ({exc}); "
            "continuing without invariant check."
        )
        return None

    if not items:
        return None
    if len(items) == 1:
        return items[0]

    head = items[0]
    extras = items[1:]
    extra_ids = [str(b.get("id", "?")) for b in extras]
    head_id = str(head.get("id", "?"))
    emit_warning(
        f"⚠️ bead-factory: found {len(items)} in_progress beads (residue from "
        f"a hard crash or pre-fix session). Recovering {head_id} first; "
        f"the rest will be picked up one-at-a-time via the recovery tier: "
        f"{', '.join(extra_ids)}"
    )
    return head


# ---------------------------------------------------------------------------
# Next-bead waterfall + activation
# ---------------------------------------------------------------------------


def pick_next_bead(
    just_closed: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Choose the next bead via a strict four-tier waterfall.

    Priority order (highest first):

    0. **Stranded in_progress bead.** If any non-epic bead is already
       in_progress, a previous run errored or was cancelled before the
       judges could close it. Recovery beats every other rule — the
       one-bead-at-a-time discipline means there can only be one in
       flight, so we must finish (or formally close) this one before
       starting anything new.
    1. **Blocking bug.** Any ready bug with ``dependent_count > 0`` —
       fixing it unblocks downstream work, so it always cuts the line.
    2. **Epic affinity.** If ``just_closed`` had a parent epic and that
       epic still has ready siblings, claim one of those. Coherent
       commits and PRs beat queue-order optimality (the 'finish what
       you start' rule).
    3. **Global ready queue.** Whatever bd hands us next.

    Beads with open work-time blockers are never returned: tier 0
    reverts+drops blocked stranded beads via :func:`_unblocked_strands`,
    and tiers 1-3 (which come from ``bd ready`` and so *should* already
    be unblocked) get a belt-and-suspenders :func:`beads.is_blocked`
    recheck — defence-in-depth against bd version drift, mirroring the
    epic ``--exclude-type`` filter. This is the bdboard-oals fix: the
    chain respects blocks at claim/start time, not just at close.

    Raises :class:`BeadsError` on infrastructure failure so the caller
    can stop the chain cleanly.

    .. note:: **Pick-then-activate race (bead_chain-hvi).** The bead this
       returns is read from the ready queue, not yet claimed. Another
       agent can claim it in the window before
       :func:`activate_next_bead` calls ``claim()``. That race is a known,
       accepted limitation; see the ``KNOWN RACE`` comment at the
       ``claim()`` call site for the window, the BeadsError mitigation,
       and why a distributed lock is not warranted.
    """
    workable = _unblocked_strands()
    if workable:
        stranded = workable[0]
        bead_id = str(stranded.get("id", "<unknown>"))
        emit_warning(
            f"bead-factory: found stranded in_progress bead {bead_id} -- "
            "recovering before picking new work."
        )
        return stranded

    blocking = next_blocking_bug()
    if blocking is not None and not _reject_if_blocked(blocking, "blocking bug"):
        bead_id = str(blocking.get("id", "<unknown>"))
        emit_info(f"bead-factory: blocking bug detected -> prioritising {bead_id}")
        return blocking

    epic_id = extract_parent_epic_id(just_closed)
    if epic_id:
        sibling = next_ready_in_epic(epic_id)
        if sibling is not None and not _reject_if_blocked(sibling, "epic affinity"):
            emit_info(f"bead-factory: epic affinity -> staying inside {epic_id}")
            return sibling

    nxt = next_ready()
    if nxt is not None and _reject_if_blocked(nxt, "global ready"):
        return None
    return nxt


def _reject_if_blocked(bead: dict[str, Any] | None, tier: str) -> bool:
    """True (and warn) if ``bead`` has open work-time blockers.

    Defence-in-depth for the non-recovery tiers, which source beads
    from ``bd ready`` (server-side blocker-filtered) and so should
    never be blocked. If one ever is — bd version drift, a ``blocks``
    edge wired between the ``ready`` query and now — we refuse to drive
    it rather than barrel into the close-time failure (bdboard-oals).
    """
    if not bead:
        return False
    bead_id = str(bead.get("id", ""))
    blockers = open_blocker_ids(bead_id)
    if not blockers:
        return False
    emit_warning(
        f"bead-factory: {tier} candidate {bead_id} has open blocker(s) "
        f"[{', '.join(blockers)}] -- refusing to claim it (bd ready leaked a "
        "blocked bead; respecting work-time blocks anyway)."
    )
    return True


def activate_next_bead(
    just_closed: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Pick the next ready bead, claim it, arm the build loop.

    If ``just_closed`` is provided and had a parent epic, we prefer the
    next ready bead under that same epic before falling back to the
    global ``bd ready`` queue — see :func:`pick_next_bead`.

    Returns the continuation dict for the runner, or ``None`` if we
    ran out of beads / hit an infrastructure error / hit the
    ``--max=N`` safety cap (in which case we've already stopped
    ourselves and emitted a message).
    """
    # Safety brake: stop before we even look at the queue if the
    # next activation would push us past the user-set cap. We check
    # *before* picking a bead so we don't waste a `bd ready` call.
    # NB: we do NOT close the current bead here — judges already
    # closed it in the previous turn (via close_current_bead_success)
    # before this iteration began.
    s = state.get_state()
    if s.max_iterations is not None and s.completed_count + 1 > s.max_iterations:
        emit_success(
            f"🛑 bead-factory: --max={s.max_iterations} cap reached "
            f"(closed {s.completed_count} bead(s) this run). Stopping. Good boy!"
        )
        state.stop()
        return None

    try:
        bead = pick_next_bead(just_closed)
    except BeadsError as exc:
        emit_warning(f"🔗 bead-factory stopping — `bd ready` failed: {exc}")
        state.stop()
        return None

    if bead is None:
        # Empty-queue gate probe (bead_chain-x3g / FB-3): before we declare
        # the chain done, ask bd to re-evaluate every open gate. Resolvable
        # gate types (timer / gh:run / gh:pr / bead) keep their targets out
        # of `bd ready` until the gate closes, and nothing else in
        # bead-factory pokes them — so an "empty" queue might just be waiting
        # on a now-satisfied gate. If any gate resolves, its target re-opens
        # and we re-probe the ready queue for one more iteration. Soft-fails
        # (see probe_resolved_gates) so a flaky `bd gate` never halts us.
        if probe_resolved_gates():
            try:
                bead = pick_next_bead(just_closed)
            except BeadsError as exc:
                emit_warning(f"🔗 bead-factory stopping — `bd ready` failed: {exc}")
                state.stop()
                return None

    if bead is None:
        # Drain pass: at session end, sweep any epics whose final child we
        # just closed. Per bead_chain-tfn (over-close bug fix), we call
        # rollup_completed_epics() ONLY HERE at the end of a session
        # (when the queue is empty), NOT after every individual bead close.
        #
        # Rationale: bd's ``epic close-eligible`` command runs a server-side
        # cascade: closing A's last child closes A, then checks if A's parent
        # B is now eligible, closes B, checks parent C, etc. When called
        # per-bead (after EVERY close), this cascade can unexpectedly close
        # unrelated epics that happen to have no open children.
        #
        # Fix: Calling it once per session limits the cascade to a single
        # pass at the end. This is mitigation (not prevention) — the cascade
        # still exists in bd, but is called far less frequently, reducing the
        # surface for unintended side effects. Parent epics may close one
        # session later, but data safety is preserved.
        #
        # See chain_driver._on_interactive_turn_end for the detailed
        # explanation and the call-site of the per-bead rollup removal.
        rollup_completed_epics()
        emit_success(
            f"bead-factory: no more ready beads. "
            f"Closed {state.get_state().completed_count} this run. Good boy!"
        )
        state.stop()
        return None

    # Last-line-of-defence assertion: the picker is *not allowed* to
    # return a container bead (epic). All four tiers filter epics out
    # both server-side (``--exclude-type=epic``) and client-side via
    # :func:`is_excluded_type`. If one slipped through anyway, refuse
    # to arm the build loop with it — driving the build loop at an epic causes the
    # 'cannot close epic: N open child issue(s)' failure we hit in
    # prod, and halts the chain after wasted token spend.
    if is_excluded_type(bead):
        bead_id = str(bead.get("id", "<unknown>"))
        emit_warning(
            f"🚫 bead-factory refused to activate {bead_id}: it's an excluded "
            f"container type ({bead.get('issue_type', '?')}). "
            "An upstream filter leaked an epic into the chain — this is a bug."
        )
        state.stop()
        return None

    bead_id = str(bead.get("id", ""))
    recovery = is_recovery_bead(bead)

    # Call consolidation (bead_chain-lqf): both the work-time blocker
    # guard and the fan-out gate guard below need this bead's FULL
    # ``bd show`` record (the ``bd ready`` / ``bd list`` dict the picker
    # handed us lacks per-dependency status and the ``waits_for`` field).
    # We fetch it ONCE here and thread it into both checks rather than
    # letting each spawn its own identical ``bd show``. One fresh read at
    # the activation boundary preserves the mid-flight-mutation safety
    # (pinned/re-blocked detection) the two guards were written for, at
    # one subprocess instead of two. Soft-fails to ``None`` so the
    # guards fall back to their own fetch / safe defaults on a bd blip.
    try:
        full_bead = show(bead_id)
    except BeadsError:
        full_bead = None

    # Last-line-of-defence assertion: the picker is *not allowed* to
    # return a bead with open work-time blockers. Tier 0 reverts+drops
    # them; tiers 1-3 reject them via :func:`_reject_if_blocked`. If one
    # still reached here (e.g. a ``blocks`` edge wired in the moment
    # between pick and activate), refuse to claim/drive it rather than
    # running blocked work that ``bd close`` will later reject. This is
    # the bdboard-oals fix mirrored at the activation boundary. Recovery
    # beads are exempt from the revert path here (they were already
    # blocker-filtered in :func:`_unblocked_strands`); we just stop
    # if somehow one is blocked, leaving it in_progress for inspection.
    blockers = open_blocker_ids(bead_id, full_bead)
    if blockers:
        emit_warning(
            f"bead-factory refused to activate {bead_id}: it has open "
            f"blocker(s) [{', '.join(blockers)}]. Respecting work-time blocks "
            "at claim time, not just at close. Stopping chain."
        )
        if not recovery:
            try:
                revert_to_open(bead_id)
                emit_info(f"reverted {bead_id} to open")
            except BeadsError as exc:
                emit_warning(f"also couldn't revert {bead_id}: {exc}")
        state.stop()
        return None

    # WORKAROUND (bead_chain-9sc): Check for unsatisfied fan-out gates.
    # Beads with waits_for: children-of(...) are invisible to bd blocked,
    # so we detect and refuse to claim them here. Reuses ``full_bead``
    # fetched above (bead_chain-lqf) so we don't re-spawn ``bd show``.
    fan_out = _fan_out_gate_verdict(bead_id, full_bead)
    if fan_out.blocked:
        emit_warning(
            f"bead-factory refused to activate {bead_id}: it has an unsatisfied "
            "fan-out gate (waits_for: children-of(...) with unclosed spawned "
            "children). Stopping chain to avoid driving work that isn't ready yet."
        )
        # FB-13 (bead_chain-y0s): only revert when bd actually surfaced the
        # aggregation mode. When the mode is unknown, the gate *might* be
        # ``any-children`` and already satisfied — reverting would strand
        # that otherwise-ready waiter at ``open``. So we still stop the
        # chain (conservative refusal) but leave the bead in_progress for a
        # human to inspect, rather than wrongly flipping it back.
        if not recovery:
            if fan_out.mode_known:
                try:
                    revert_to_open(bead_id)
                    emit_info(f"reverted {bead_id} to open")
                except BeadsError as exc:
                    emit_warning(f"also couldn't revert {bead_id}: {exc}")
            else:
                emit_info(
                    f"leaving {bead_id} in_progress (fan-out aggregation mode "
                    "unknown — skipping revert so an any-children waiter that "
                    "is already ready is not stranded at open)"
                )
        state.stop()
        return None

    # Walk the hierarchy top-down: claim the parent epic FIRST, then
    # the child bead. bd's UI caches per-parent children-by-status
    # views, so flipping a leaf to in_progress under a still-open
    # parent produces a stale tree until the user navigates back to
    # the parent. Going parent-first keeps the hierarchy consistent
    # at every observable moment. Soft-fails internally — never
    # blocks the chain.
    ensure_epic_in_progress(bead)

    if not recovery:
        # KNOWN RACE — pick-then-activate (bead_chain-hvi):
        # There is an unavoidable window between pick_next_bead() reading
        # the ready queue (`bd ready` / `bd list`) and this claim() call
        # flipping the bead to in_progress. In that gap a *different* agent
        # — another bead-factory on another machine, a human in the bd UI,
        # CI — can claim the very same bead. pick/claim is read-then-write,
        # not a single atomic compare-and-swap, so two drivers can both see
        # the bead "ready" and race for it.
        #
        # MITIGATION (sufficient, by design): the claim is the serializing
        # point. bd's `update --claim` is atomic at the database layer, so
        # at most one racer wins; the loser's claim() raises BeadsError
        # (the bead is no longer claimable in the state it expected). We
        # catch that here, warn, and stop the chain cleanly rather than
        # double-driving a bead someone else owns. No work is lost or
        # duplicated — the winner drives it, the loser backs off. Worst
        # case is one wasted `bd ready` + `bd show` round-trip on the loser.
        #
        # WHY NO DISTRIBUTED LOCK: closing the window entirely would need a
        # cross-process/cross-machine lock (lease, advisory lock, CAS token)
        # spanning pick→claim. That's a large amount of distributed-systems
        # machinery (lock store, lease renewal, crash-recovery for orphaned
        # locks) to defend against a sub-second window whose only failure
        # mode is already handled gracefully by the claim-fails-→-stop path.
        # The race is rare (it requires two drivers targeting the same bead
        # in the same instant), self-healing (the loser simply stops), and
        # harmless (no corruption). YAGNI: the atomic claim *is* the lock we
        # need; a second locking layer would be redundant complexity.
        try:
            claim(bead_id)
        except BeadsError as exc:
            emit_warning(f"🔗 bead-factory couldn't claim {bead_id}: {exc} — stopping.")
            state.stop()
            return None
    # Recovery beads are already in_progress — skip the redundant claim
    # call (see handle_bead_factory_command for the same rationale).

    state.get_state().current_bead = bead

    # FB-8 (bead_chain-9n3): apply the bead's recognized execution_*
    # metadata hints (effort/model/agent_type) to the serial drive before
    # arming the build loop. Soft-fails per hint; no-op when none are present.
    applied_hints = apply_execution_hints(bead)
    if applied_hints:
        emit_info(f"\U0001f9ea execution hints: {'; '.join(applied_hints)}")

    build_prompt = format_bead_as_build(bead, recovery=recovery)

    # Hand the wheel to the build loop for the next N turns.
    build_state.start(build_prompt)

    action = "recovered" if recovery else "claimed"
    emit_info(f"🔗 bead-factory {action} {bead_id} — {bead.get('title', '')}")
    return {
        "prompt": build_prompt,
        "clear_context": True,
        "delay": 0.5,
        "reason": "bead_chain",
    }
