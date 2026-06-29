"""Regression tests for bead_chain-i0v (FB-10): generic ``waits-for`` edges.

The hole this bead closes:

  :func:`beads.open_blocker_ids` historically honoured **only** ``blocks``
  edges (``BLOCKING_DEP_TYPES = ("blocks",)``), and
  :func:`lifecycle._has_fan_out_gate_issue` only matched the molecule
  ``waits_for: children-of(...)`` *field* marker. So a **generic**
  ``waits-for`` edge -- the kind created by
  ``bd dep add B A --type=waits-for`` (or ``--waits-for=A``), which lands
  in the inbound ``dependencies`` array with
  ``dependency_type == "waits-for"`` -- was honoured *only* by ``bd ready``
  server-side. The recovery tier bypasses ``bd ready`` (it reads
  ``bd list --status=in_progress``), so a stranded in_progress bead
  re-gated by a generic ``waits-for`` would be re-driven straight into a
  close-time refusal: the exact bdboard-oals failure class, for a second
  gating edge type.

The fix adds ``"waits-for"`` to :data:`beads.BLOCKING_DEP_TYPES`, so the
whole blocker machinery (``open_blocker_ids`` -> the recovery tier's
:func:`lifecycle._unblocked_strands`, and the claim-time guards) now treats
it identically to ``blocks``.

The pure-beads half imports ``beads`` flat (no code_puppy needed); the
recovery-tier half imports the registered package (conftest wires it up).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


def _patch_show(bead: dict | None):
    """Stub beads.show() to return a fixed bead record."""
    beads.show = lambda _id: bead  # type: ignore[assignment]


def _dep(dep_id: str, status: str, dep_type: str) -> dict:
    """Build a `bd show`-shaped inbound dependency entry."""
    return {"id": dep_id, "status": status, "dependency_type": dep_type}


# ---------------------------------------------------------------------------
# beads.BLOCKING_DEP_TYPES -- the constant the whole fix keys off
# ---------------------------------------------------------------------------


def test_blocking_dep_types_includes_blocks_and_waits_for():
    """Both hard work-time edge types gate; soft edges are excluded."""
    assert "blocks" in beads.BLOCKING_DEP_TYPES
    assert "waits-for" in beads.BLOCKING_DEP_TYPES
    # Soft / structural edges must NOT gate work.
    assert "parent-child" not in beads.BLOCKING_DEP_TYPES
    assert "related" not in beads.BLOCKING_DEP_TYPES
    assert "discovered-from" not in beads.BLOCKING_DEP_TYPES


# ---------------------------------------------------------------------------
# beads.open_blocker_ids -- now honours generic waits-for edges
# ---------------------------------------------------------------------------


def test_open_waits_for_dep_is_reported():
    """An open generic `waits-for` dependency gates work -> id surfaces."""
    _patch_show({"id": "x", "dependencies": [_dep("a-1", "open", "waits-for")]})
    assert beads.open_blocker_ids("x") == ["a-1"]
    assert beads.is_blocked("x") is True


def test_in_progress_waits_for_still_gates():
    """A `waits-for` target that's in_progress (not closed) still gates."""
    _patch_show({"id": "x", "dependencies": [_dep("a-1", "in_progress", "waits-for")]})
    assert beads.open_blocker_ids("x") == ["a-1"]


def test_closed_waits_for_is_satisfied():
    """A closed `waits-for` dependency no longer gates -> unblocked."""
    _patch_show({"id": "x", "dependencies": [_dep("a-1", "closed", "waits-for")]})
    assert beads.open_blocker_ids("x") == []
    assert beads.is_blocked("x") is False


def test_case_insensitive_waits_for_type():
    """bd version drift: 'Waits-For' / 'OPEN' casing must not leak past."""
    _patch_show({"id": "x", "dependencies": [_dep("a-1", "OPEN", "Waits-For")]})
    assert beads.open_blocker_ids("x") == ["a-1"]


def test_mixed_blocks_and_waits_for_both_count():
    """Both hard edge types surface together; soft edges drop out."""
    _patch_show(
        {
            "id": "x",
            "dependencies": [
                _dep("b-1", "open", "blocks"),  # hard block -> counts
                _dep("w-1", "open", "waits-for"),  # waits-for -> counts
                _dep("w-2", "closed", "waits-for"),  # satisfied -> dropped
                _dep("epic-1", "open", "parent-child"),  # parent -> dropped
                _dep("r-1", "open", "related"),  # advisory -> dropped
            ],
        }
    )
    assert beads.open_blocker_ids("x") == ["b-1", "w-1"]
    assert beads.is_blocked("x") is True


# ---------------------------------------------------------------------------
# Recovery-tier path -- needs code_puppy (conftest registers the package)
#
# This is the acceptance-critical test: a stranded in_progress bead gated by
# a generic waits-for must be REVERTED to open and dropped from the workable
# set -- never re-driven. We exercise the REAL open_blocker_ids through the
# real _unblocked_strands(), patching only the bd-touching seams.
# ---------------------------------------------------------------------------

from code_puppy.plugins.bead_factory import beads as pkg_beads  # noqa: E402
from code_puppy.plugins.bead_factory import lifecycle  # noqa: E402

# NB: the package-registered beads module (what lifecycle's open_blocker_ids
# resolves ``show`` against) is a DIFFERENT module object than the flat
# ``beads`` imported at the top of this file. The recovery-tier tests must
# patch ``pkg_beads.show`` for the real open_blocker_ids to see our stub.


def _strand(bead_id: str) -> dict:
    return {"id": bead_id, "issue_type": "task", "status": "in_progress"}


def test_recovery_tier_reverts_waits_for_gated_strand(monkeypatch):
    """A stranded in_progress bead gated by a generic waits-for is reverted.

    The whole point of FB-10: the recovery tier bypasses ``bd ready``, so it
    must mirror its blocking semantics. A strand whose only blocker is a
    still-open ``waits-for`` edge must be reverted to open and NOT returned
    as workable (which would re-drive it into a close-time refusal).
    """
    strand = _strand("S")

    # The recovery tier enumerates this one stranded bead...
    monkeypatch.setattr(lifecycle, "list_recoverable_strands", lambda: [strand])
    # ...and the REAL open_blocker_ids re-fetches it via beads.show and sees
    # an open generic waits-for edge -> must classify it as blocked.
    monkeypatch.setattr(
        pkg_beads,
        "show",
        lambda _id: {"id": "S", "dependencies": [_dep("G", "open", "waits-for")]},
    )

    reverted: list[str] = []
    monkeypatch.setattr(lifecycle, "revert_to_open", lambda bid: reverted.append(bid))

    workable = lifecycle._unblocked_strands()

    assert workable == [], "a waits-for-gated strand must NOT be re-driven"
    assert reverted == ["S"], "the gated strand must be reverted to open"


def test_recovery_tier_keeps_strand_with_satisfied_waits_for(monkeypatch):
    """Sanity: a strand whose waits-for is closed stays workable (recovered)."""
    strand = _strand("S")

    monkeypatch.setattr(lifecycle, "list_recoverable_strands", lambda: [strand])
    monkeypatch.setattr(
        pkg_beads,
        "show",
        lambda _id: {"id": "S", "dependencies": [_dep("G", "closed", "waits-for")]},
    )

    reverted: list[str] = []
    monkeypatch.setattr(lifecycle, "revert_to_open", lambda bid: reverted.append(bid))

    workable = lifecycle._unblocked_strands()

    assert workable == [strand], "a satisfied waits-for must not block recovery"
    assert reverted == [], "nothing to revert when the gate is satisfied"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
