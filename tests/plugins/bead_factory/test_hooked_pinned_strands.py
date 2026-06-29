"""Regression tests for bead_chain-0q9 (FB-12): hooked/pinned strands.

Two lifecycle holes this bead closes:

  * **lifecycle#2 (hooked strands invisible to recovery).** Recovery
    historically queried only ``bd list --status=in_progress``, so a
    bead flipped to ``hooked`` mid-flight by another agent/tool was
    invisible to BOTH ``bd ready`` (hooked is out of the ready frontier)
    AND the recovery tier -- stranded work no run ever resumed. The fix
    widens the stranded-work query to :data:`beads.RECOVERABLE_STATUSES`
    (``in_progress`` + ``hooked``) via
    :func:`beads.list_recoverable_strands`, and teaches
    :func:`lifecycle.is_recovery_bead` to treat ``hooked`` as recovery
    (re-drive, don't re-claim).

  * **lifecycle#1 (pinned-close halt).** Closing a ``pinned`` bead needs
    ``--force``, which :func:`beads.close` never passes. A bead pinned
    *after* bead-chain claimed it would fail at close() and halt the
    loop. The fix re-reads the live status in
    :func:`lifecycle.close_current_bead_success` via
    :func:`beads.is_pinned` and *respects the pin*: it skips the close,
    drops the bead as current, and keeps the chain trotting -- never
    halting and never force-closing a human's deliberate park.

The pure-beads half imports ``beads`` flat (no code_puppy needed); the
lifecycle half imports the registered package (conftest wires it up).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


# ---------------------------------------------------------------------------
# beads.RECOVERABLE_STATUSES -- the constant the whole fix keys off
# ---------------------------------------------------------------------------


def test_recoverable_statuses_include_in_progress_and_hooked():
    """The recovery rollup must cover in_progress AND hooked (not pinned)."""
    assert "in_progress" in beads.RECOVERABLE_STATUSES
    assert "hooked" in beads.RECOVERABLE_STATUSES
    # pinned is frozen/deliberate -- never auto-recovered (would fight a
    # human's park and risk a re-pick loop). It's handled at close-time.
    assert "pinned" not in beads.RECOVERABLE_STATUSES
    assert "deferred" not in beads.RECOVERABLE_STATUSES


# ---------------------------------------------------------------------------
# beads.list_recoverable_strands -- merges per-status queries, dedupes
# ---------------------------------------------------------------------------


def _patch_run_bd_by_status(mapping: dict[str, list[dict]]):
    """Stub beads._run_bd for the consolidated comma-status ``bd list`` call.

    bead_chain-lqf collapsed the per-status fan-out into a single
    ``bd list --status=a,b`` spawn. ``mapping`` still maps each status
    to the beads bd would return for it; the stub splits the requested
    ``--status=a,b`` arg on commas and concatenates the per-status
    lists (in the requested order, mirroring bd's own behaviour) so the
    client-side dedup + ordering logic is exercised. Each bead inherits
    its keying status (so the client-side sort has a ``status`` field to
    work with) unless it already declares one.
    """

    def _stub(*args, **kwargs):
        for arg in args:
            if isinstance(arg, str) and arg.startswith("--status="):
                statuses = arg[len("--status=") :].split(",")
                out: list[dict] = []
                for status in statuses:
                    for bead in mapping.get(status, []):
                        bead = {"status": status, **bead}
                        out.append(bead)
                return json.dumps(out)
        return "[]"

    beads._run_bd = _stub  # type: ignore[assignment]


def test_list_recoverable_strands_surfaces_hooked_bead():
    """A bead hooked mid-flight is no longer invisible to recovery."""
    _patch_run_bd_by_status(
        {
            "in_progress": [{"id": "A", "issue_type": "task"}],
            "hooked": [{"id": "H", "issue_type": "task"}],
        }
    )
    ids = [b["id"] for b in beads.list_recoverable_strands()]
    assert ids == ["A", "H"], "in_progress leads, hooked follows"


def test_list_recoverable_strands_single_subprocess_call(monkeypatch):
    """bead_chain-lqf: N recoverable statuses cost exactly ONE bd spawn."""
    calls: list[tuple] = []

    def _counting_stub(*args, **kwargs):
        calls.append(args)
        return "[]"

    monkeypatch.setattr(beads, "_run_bd", _counting_stub)
    beads.list_recoverable_strands()
    assert len(calls) == 1, f"expected 1 bd list call, got {len(calls)}: {calls}"
    # The single call must carry every recoverable status comma-joined.
    status_args = [
        a for a in calls[0] if isinstance(a, str) and a.startswith("--status=")
    ]
    assert status_args, "no --status arg passed"
    requested = set(status_args[0][len("--status=") :].split(","))
    assert requested == set(beads.RECOVERABLE_STATUSES)


def test_list_recoverable_strands_orders_in_progress_first():
    """Client-side sort restores in_progress-before-hooked from one call."""
    # bd returns hooked first (its own sort) -- we must reorder.
    _patch_run_bd_by_status(
        {
            "hooked": [{"id": "H", "issue_type": "task"}],
            "in_progress": [{"id": "A", "issue_type": "task"}],
        }
    )
    ids = [b["id"] for b in beads.list_recoverable_strands()]
    assert ids == ["A", "H"], "in_progress must sort ahead of hooked"


def test_list_recoverable_strands_dedupes_across_statuses():
    """A bead echoed under two statuses is returned once (one-at-a-time)."""
    dup = {"id": "D", "issue_type": "task"}
    _patch_run_bd_by_status({"in_progress": [dup], "hooked": [dup]})
    ids = [b["id"] for b in beads.list_recoverable_strands()]
    assert ids == ["D"], f"duplicate id must be de-duped, got {ids}"


def test_list_recoverable_strands_drops_leaked_container_bead():
    """Even a hooked epic leak is filtered (defence-in-depth, like ready)."""
    _patch_run_bd_by_status({"hooked": [{"id": "leak", "issue_type": "epic"}]})
    assert beads.list_recoverable_strands() == []


def test_list_recoverable_strands_empty_when_nothing_stranded():
    """No strands in any recoverable status -> empty list (clean slate)."""
    _patch_run_bd_by_status({})
    assert beads.list_recoverable_strands() == []


# ---------------------------------------------------------------------------
# beads.is_pinned -- live re-read of a bead's status
# ---------------------------------------------------------------------------


def _patch_show_status(status: str):
    """Stub beads.show() to return a bead record carrying ``status``.

    We patch :func:`beads.show` directly (the seam :func:`beads.is_pinned`
    actually calls) rather than the underlying ``_run_bd`` -- other test
    modules rebind ``beads.show`` at module scope, so stubbing the lower
    layer wouldn't survive their pollution.
    """
    beads.show = lambda _id: {"id": "X", "status": status}  # type: ignore[assignment]


def test_is_pinned_true_for_pinned_bead():
    _patch_show_status("pinned")
    assert beads.is_pinned("X") is True


def test_is_pinned_false_for_in_progress_bead():
    _patch_show_status("in_progress")
    assert beads.is_pinned("X") is False


def test_is_pinned_case_insensitive():
    _patch_show_status("Pinned")
    assert beads.is_pinned("X") is True


def test_is_pinned_soft_fails_to_false_on_bd_error():
    """A bd blip must not block a legitimate close -- soft-fail to False."""

    def _boom(*a, **k):
        raise beads.BeadsError("bd exploded")

    beads.show = _boom  # type: ignore[assignment]
    assert beads.is_pinned("X") is False


def test_is_pinned_false_on_empty_id():
    assert beads.is_pinned("") is False


# ---------------------------------------------------------------------------
# Lifecycle half -- needs code_puppy (conftest registers the package)
# ---------------------------------------------------------------------------

from code_puppy.plugins.bead_factory import (  # noqa: E402
    lifecycle,
    lifecycle_close,
    state,
)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_state():
    """Leave the shared chain singleton pristine for the next module."""
    yield
    state.reset()


def _bead(bead_id: str, **extra) -> dict:
    b = {"id": bead_id, "issue_type": "task", "status": "open", "title": "t"}
    b.update(extra)
    return b


# --- is_recovery_bead now treats hooked as recovery --------------------------


def test_is_recovery_bead_true_for_in_progress():
    assert lifecycle.is_recovery_bead(_bead("A", status="in_progress")) is True


def test_is_recovery_bead_true_for_hooked():
    """A hooked strand is recovered (re-driven), not treated as fresh."""
    assert lifecycle.is_recovery_bead(_bead("H", status="hooked")) is True


def test_is_recovery_bead_false_for_open():
    assert lifecycle.is_recovery_bead(_bead("O", status="open")) is False


def test_is_recovery_bead_case_insensitive():
    assert lifecycle.is_recovery_bead(_bead("H", status="Hooked")) is True


# --- pick_next_bead recovers a hooked strand (tier 0) ------------------------


def test_pick_next_bead_recovers_hooked_strand(monkeypatch):
    """A hooked stranded bead is surfaced by the recovery tier."""
    hooked = _bead("H", status="hooked")
    ready = _bead("R")
    monkeypatch.setattr(lifecycle, "list_recoverable_strands", lambda: [hooked])
    monkeypatch.setattr(lifecycle, "open_blocker_ids", lambda _bid, _bead=None: [])
    monkeypatch.setattr(lifecycle, "next_blocking_bug", lambda: None)
    monkeypatch.setattr(lifecycle, "next_ready", lambda: ready)
    picked = lifecycle.pick_next_bead(None)
    assert picked is hooked, "hooked strand must win the recovery tier"


# --- close respects a mid-flight pin without halting ------------------------


def _engage_with_current(bead: dict):
    """Set the chain active with ``bead`` as the in-flight current bead."""
    s = state.get_state()
    s.active = True
    s.current_bead = bead
    s.completed_count = 0
    s.max_iterations = None
    return s


def test_close_respects_mid_flight_pin_and_keeps_trotting(monkeypatch):
    """A bead pinned mid-flight is left pinned; the chain does NOT halt."""
    bead = _bead("P", status="in_progress")
    s = _engage_with_current(bead)

    closed_calls: list[str] = []
    monkeypatch.setattr(lifecycle_close, "is_pinned", lambda _bid: True)
    monkeypatch.setattr(
        lifecycle_close, "close", lambda *a, **k: closed_calls.append(a)
    )

    returned = lifecycle_close.close_current_bead_success()

    assert closed_calls == [], "must NOT close a pinned bead (would need --force)"
    assert s.active is True, "respecting a pin must not halt the chain"
    assert s.completed_count == 0, "nothing was closed -> no completion bump"
    assert s.current_bead is None, "pinned bead is dropped as current"
    assert returned is bead, "returns the bead so epic-affinity routing works"


def test_close_proceeds_normally_when_not_pinned(monkeypatch):
    """Sanity: an unpinned bead still closes and bumps the tally."""
    bead = _bead("N", status="in_progress")
    s = _engage_with_current(bead)

    closed_calls: list[tuple] = []
    monkeypatch.setattr(lifecycle_close, "is_pinned", lambda _bid: False)
    monkeypatch.setattr(
        lifecycle_close, "close", lambda *a, **k: closed_calls.append((a, k))
    )

    returned = lifecycle_close.close_current_bead_success()

    assert len(closed_calls) == 1, "unpinned bead must be closed"
    assert s.active is True
    assert s.completed_count == 1, "a real close bumps the tally"
    assert s.current_bead is None
    assert returned is bead


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
