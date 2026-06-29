"""Regression tests for bead_chain-yvc: the bug-discovery close deadlock.

When the LLM judges pass a bead ``X`` that was blocked *during its own
run* — an agent filed bug ``Y`` with ``--blocks=X`` per the Bug Discovery
Protocol — bead-chain's auto-close calls ``bd close X``, which bd refuses
with a "blocked by open issue(s)" message. The historical
``except BeadsError`` handler treated that as a fatal infra error and
called :func:`state.stop`, halting the ENTIRE chain.

That was a deadlock, not a fault: the recovery machinery (tier-0
``_unblocked_strands`` revert + tier-1 blocking-bug routing) already
exists and self-heals — but the halt fired before it ever ran.

The fix (ADR 0004) splits the close-failure handler by error class:

  * a "blocked by open issues" refusal reverts the bead to open and
    CONTINUES the chain, and
  * every other BeadsError keeps the halt-loudly behaviour, except that
  * a *failed revert* degrades back to the safe halt path.

These tests pin all four behaviours plus the narrow predicate.
"""

from __future__ import annotations

import sys

from code_puppy.plugins.bead_factory import lifecycle_close as lifecycle, state

import pytest


@pytest.fixture(autouse=True)
def _restore_state():
    """Leave the shared chain singleton pristine for the next module."""
    yield
    state.reset()


def _bead(bead_id: str, **extra) -> dict:
    b = {"id": bead_id, "issue_type": "task", "status": "in_progress", "title": "t"}
    b.update(extra)
    return b


def _engage_with_current(bead: dict):
    """Set the chain active with ``bead`` as the in-flight current bead."""
    s = state.get_state()
    s.active = True
    s.current_bead = bead
    s.completed_count = 0
    s.max_iterations = None
    return s


def _patch_common(monkeypatch):
    """Neutralise the upstream guards so the close path is reached."""
    monkeypatch.setattr(lifecycle, "is_excluded_type", lambda _b: False)
    monkeypatch.setattr(lifecycle, "is_pinned", lambda _bid: False)


# ---------------------------------------------------------------------------
# _is_blocked_close_error — the narrow predicate
# ---------------------------------------------------------------------------


def test_predicate_matches_blocked_message():
    exc = lifecycle.BeadsError(
        "`bd close X` failed (exit 1): cannot close: blocked by open issue(s): Y"
    )
    assert lifecycle._is_blocked_close_error(exc) is True


def test_predicate_is_case_insensitive():
    exc = lifecycle.BeadsError("Blocked By Open Issue(s)")
    assert lifecycle._is_blocked_close_error(exc) is True


def test_predicate_rejects_unrelated_infra_errors():
    """Anything that isn't the blocked-close refusal must NOT match — on a
    miss we degrade to the safe halt path, never silently continue."""
    for msg in (
        "`bd close X` failed (exit 1): permission denied",
        "`bd` not found on PATH — is beads installed?",
        "schema drift: unknown column",
        "blocked by a deadline",  # superficially similar, deliberately excluded
    ):
        assert lifecycle._is_blocked_close_error(lifecycle.BeadsError(msg)) is False


# ---------------------------------------------------------------------------
# The core fix — a blocked-close reverts and the chain CONTINUES
# ---------------------------------------------------------------------------


def test_blocked_close_reverts_and_chain_continues(monkeypatch):
    """ACCEPTANCE CRITERIA: a 'blocked by open issues' close failure reverts
    the bead to open and the chain CONTINUES (does NOT call state.stop)."""
    bead = _bead("X")
    s = _engage_with_current(bead)
    _patch_common(monkeypatch)

    def _raise_blocked(*_a, **_k):
        raise lifecycle.BeadsError(
            "`bd close X` failed (exit 1): blocked by open issue(s): Y"
        )

    reverted: list[str] = []
    monkeypatch.setattr(lifecycle, "close", _raise_blocked)
    monkeypatch.setattr(lifecycle, "revert_to_open", lambda bid: reverted.append(bid))

    returned = lifecycle.close_current_bead_success()

    assert reverted == ["X"], "the blocked bead must be reverted to open"
    assert s.active is True, "a recoverable blocked-close must NOT halt the chain"
    assert s.completed_count == 0, "nothing closed -> no completion bump"
    assert s.current_bead is None, "reverted bead is dropped as current"
    assert returned is bead, "returns the bead so epic-affinity routing still works"


def test_blocked_close_failed_revert_falls_back_to_halt(monkeypatch):
    """If the revert itself fails, that IS infra-class — halt the chain."""
    bead = _bead("X")
    s = _engage_with_current(bead)
    _patch_common(monkeypatch)

    def _raise_blocked(*_a, **_k):
        raise lifecycle.BeadsError("blocked by open issue(s): Y")

    def _raise_on_revert(_bid):
        raise lifecycle.BeadsError("bd outage during revert")

    monkeypatch.setattr(lifecycle, "close", _raise_blocked)
    monkeypatch.setattr(lifecycle, "revert_to_open", _raise_on_revert)

    returned = lifecycle.close_current_bead_success()

    assert s.active is False, "a failed revert must fall back to the halt path"
    assert s.completed_count == 0
    assert returned is bead


# ---------------------------------------------------------------------------
# Non-regression — every OTHER close failure still halts loudly
# ---------------------------------------------------------------------------


def test_infra_close_error_still_halts(monkeypatch):
    """A non-blocked BeadsError keeps the historical halt-loudly behaviour:
    leave in_progress (do NOT revert) and stop the chain."""
    bead = _bead("X")
    s = _engage_with_current(bead)
    _patch_common(monkeypatch)

    def _raise_infra(*_a, **_k):
        raise lifecycle.BeadsError("`bd close X` failed (exit 1): permission denied")

    reverted: list[str] = []
    monkeypatch.setattr(lifecycle, "close", _raise_infra)
    monkeypatch.setattr(lifecycle, "revert_to_open", lambda bid: reverted.append(bid))

    returned = lifecycle.close_current_bead_success()

    assert reverted == [], "infra-class failure must NOT revert (leave in_progress)"
    assert s.active is False, "an infra close failure must halt the chain"
    assert s.completed_count == 0
    # The handler doesn't itself clear current_bead on the halt path, but
    # state.stop() does as part of tearing the chain down.
    assert s.current_bead is None
    assert returned is bead


def test_clean_close_still_succeeds(monkeypatch):
    """Sanity: a normal close still closes and bumps the tally."""
    bead = _bead("N")
    s = _engage_with_current(bead)
    _patch_common(monkeypatch)

    closed: list[tuple] = []
    monkeypatch.setattr(lifecycle, "close", lambda *a, **k: closed.append((a, k)))

    returned = lifecycle.close_current_bead_success()

    assert len(closed) == 1, "a healthy bead must be closed"
    assert s.active is True
    assert s.completed_count == 1, "a real close bumps the tally"
    assert s.current_bead is None
    assert returned is bead


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
