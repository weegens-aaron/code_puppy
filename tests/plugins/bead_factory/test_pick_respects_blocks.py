"""Regression tests: bead-chain respects work-time blocks (bdboard-oals).

The bug: the chain claimed and fully executed beads BEYOND the
``bd ready`` frontier -- it ignored ``blocks`` deps until ``bd close``
refused. These tests pin the fix at the *selection* layer
(:func:`lifecycle.pick_next_bead`):

  * a blocked candidate from any tier is never returned, and
  * a blocked *stranded* in_progress bead (the recovery tier, which
    reads ``bd list --status=<recoverable>`` and so bypasses the ready
    frontier) is reverted to open and never re-driven.

Requires ``code_puppy`` on the path (lifecycle imports its messaging +
wiggum state); ``conftest.py`` registers the plugin as a package.
"""

from __future__ import annotations

from code_puppy.plugins.bead_factory import beads, lifecycle


def _bead(bead_id: str, **extra) -> dict:
    b = {"id": bead_id, "issue_type": "task", "status": "open"}
    b.update(extra)
    return b


def _install(
    monkeypatch,
    *,
    blocked_ids=(),
    in_progress=(),
    ready=None,
    blocking_bug=None,
    epic_sibling=None,
):
    """Wire the beads.* selection surface that pick_next_bead calls."""
    blocked = set(blocked_ids)
    blocker_fn = lambda bid, _bead=None: [f"{bid}-blocker"] if bid in blocked else []  # noqa: E731
    monkeypatch.setattr(beads, "open_blocker_ids", blocker_fn)
    monkeypatch.setattr(lifecycle, "open_blocker_ids", blocker_fn)

    monkeypatch.setattr(
        lifecycle, "list_recoverable_strands", lambda: list(in_progress)
    )
    monkeypatch.setattr(lifecycle, "next_blocking_bug", lambda: blocking_bug)
    monkeypatch.setattr(lifecycle, "next_ready_in_epic", lambda _e: epic_sibling)
    monkeypatch.setattr(lifecycle, "next_ready", lambda: ready)

    reverted: list[str] = []
    monkeypatch.setattr(lifecycle, "revert_to_open", lambda bid: reverted.append(bid))
    return reverted


def test_blocked_global_ready_bead_is_not_returned(monkeypatch):
    """If `bd ready` ever leaks a blocked bead, pick_next_bead refuses it."""
    b = _bead("B")
    _install(monkeypatch, blocked_ids={"B"}, ready=b)
    assert lifecycle.pick_next_bead(None) is None


def test_unblocked_global_ready_bead_is_returned(monkeypatch):
    """Sanity: an unblocked ready bead flows straight through."""
    a = _bead("A")
    _install(monkeypatch, ready=a)
    assert lifecycle.pick_next_bead(None) is a


def test_blocked_blocking_bug_is_skipped_falls_through(monkeypatch):
    """A blocked 'blocking bug' candidate is skipped; we fall to ready (A)."""
    bug = _bead("BUG", issue_type="bug")
    a = _bead("A")
    _install(monkeypatch, blocked_ids={"BUG"}, blocking_bug=bug, ready=a)
    assert lifecycle.pick_next_bead(None) is a


def test_blocked_epic_sibling_is_skipped_falls_through(monkeypatch):
    """A blocked epic-affinity sibling is skipped; we fall to ready (A)."""
    sib = _bead("SIB")
    a = _bead("A")
    just_closed = _bead("DONE", parent="epic-1")
    _install(monkeypatch, blocked_ids={"SIB"}, epic_sibling=sib, ready=a)
    assert lifecycle.pick_next_bead(just_closed) is a


def test_blocked_stranded_in_progress_is_reverted_not_redriven(monkeypatch):
    """T core repro: a blocked in_progress bead is reverted, never re-driven.

    With B blocked-by-open-A left in_progress, the chain must NOT recover
    B; it reverts B to open and picks the genuinely ready leaf A instead.
    """
    b = _bead("B", status="in_progress")
    a = _bead("A")
    reverted = _install(monkeypatch, blocked_ids={"B"}, in_progress=[b], ready=a)
    picked = lifecycle.pick_next_bead(None)
    assert picked is a, f"expected ready leaf A, got {picked}"
    assert reverted == ["B"], f"blocked stranded B must be reverted, got {reverted}"


def test_unblocked_stranded_in_progress_is_recovered(monkeypatch):
    """An unblocked stranded bead is still legitimately recovered (tier 0)."""
    b = _bead("B", status="in_progress")
    a = _bead("A")
    reverted = _install(monkeypatch, in_progress=[b], ready=a)
    picked = lifecycle.pick_next_bead(None)
    assert picked is b, "unblocked stranded bead should be recovered first"
    assert reverted == [], "an unblocked recovery bead must not be reverted"


def test_enforce_single_evicts_blocked_recovery(monkeypatch):
    """Startup guard reverts a blocked stranded bead and returns nothing."""
    b = _bead("B", status="in_progress")
    reverted = _install(monkeypatch, blocked_ids={"B"}, in_progress=[b])
    assert lifecycle.enforce_single_in_progress() is None
    assert reverted == ["B"]
