"""Tests for bead-factory-2mb: re-fetch the LIVE bead at inspection time.

``build_state.prompt`` / ``inspector_prompt`` are frozen at claim time, so
notes/edits appended to the bead DURING the build loop never reach the
inspectors -- they grade stale context. ``build_loop._refresh_build_prompts``
re-fetches the live bead via ``bd show`` and re-renders through the
notes-aware formatter, soft-failing to the frozen snapshot on any bd error.

These tests verify:
  1. BuildState carries the threaded bead identity (bead_id + recovery).
  2. A live re-fetch surfaces post-claim notes to BOTH prompts and refreshes
     the compaction-protected pin (ChainState.current_bead).
  3. Any bd error / empty payload / missing id soft-fails to the frozen pair.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from code_puppy.plugins.bead_factory import build_loop
from code_puppy.plugins.bead_factory import build_state as bstate
from code_puppy.plugins.bead_factory import state as chain_state


@pytest.fixture(autouse=True)
def _reset_state():
    """Pristine chain + build state around every test."""
    chain_state.get_state().reset()
    bstate.get_state().stop()
    yield
    chain_state.get_state().reset()
    bstate.get_state().stop()


def _bead(notes: str = "") -> dict:
    bead = {
        "id": "bead-factory-2mb",
        "title": "Re-fetch the live bead",
        "description": "do the thing",
        "issue_type": "feature",
        "priority": 1,
    }
    if notes:
        bead["notes"] = notes
    return bead


def test_build_state_carries_bead_identity():
    """start() threads bead_id + recovery; stop() clears them."""
    bstate.start(
        "impl",
        inspector_prompt="insp",
        bead_id="bead-factory-2mb",
        recovery=True,
    )
    st = bstate.get_state()
    assert st.bead_id == "bead-factory-2mb"
    assert st.recovery is True

    bstate.stop()
    assert st.bead_id is None
    assert st.recovery is False


def test_refresh_surfaces_live_notes_and_refreshes_pin():
    """A live re-fetch renders post-claim notes and refreshes the pin."""
    # Arm the chain so is_pin_active() is True (active + current_bead set).
    chain_state.start()
    chain_state.get_state().current_bead = _bead()  # stale snapshot
    bstate.start(
        "frozen-impl",
        inspector_prompt="frozen-insp",
        bead_id="bead-factory-2mb",
    )

    live = _bead(notes="REWORK: tighten the soft-fail path")

    with patch("code_puppy.plugins.bead_factory.beads.show", return_value=live) as show:
        impl, insp = build_loop._refresh_build_prompts(
            frozen_implementor="frozen-impl",
            frozen_inspector="frozen-insp",
        )

    show.assert_called_once_with("bead-factory-2mb")
    # Inspector copy is the FULL render and now carries the live notes.
    assert "REWORK: tighten the soft-fail path" in insp
    # The pin (read fresh each turn) was refreshed to the live bead.
    assert chain_state.get_state().current_bead is live
    # Neither copy is the frozen string anymore.
    assert impl != "frozen-impl"
    assert insp != "frozen-insp"


def test_refresh_soft_fails_to_frozen_on_bd_error():
    """Any bd error returns the frozen claim-time pair untouched."""
    from code_puppy.plugins.bead_factory.beads import BeadsError

    chain_state.start()
    chain_state.get_state().current_bead = _bead()
    bstate.start(
        "frozen-impl",
        inspector_prompt="frozen-insp",
        bead_id="bead-factory-2mb",
    )

    with patch(
        "code_puppy.plugins.bead_factory.beads.show",
        side_effect=BeadsError("bd exploded"),
    ):
        impl, insp = build_loop._refresh_build_prompts(
            frozen_implementor="frozen-impl",
            frozen_inspector="frozen-insp",
        )

    assert impl == "frozen-impl"
    assert insp == "frozen-insp"


def test_refresh_soft_fails_on_empty_payload():
    """An empty/None bd payload also falls back to the frozen pair."""
    chain_state.start()
    bstate.start(
        "frozen-impl",
        inspector_prompt="frozen-insp",
        bead_id="bead-factory-2mb",
    )

    with patch("code_puppy.plugins.bead_factory.beads.show", return_value=None):
        impl, insp = build_loop._refresh_build_prompts(
            frozen_implementor="frozen-impl",
            frozen_inspector="frozen-insp",
        )

    assert (impl, insp) == ("frozen-impl", "frozen-insp")


def test_refresh_is_a_noop_without_bead_id():
    """No threaded bead id => no bd call, frozen pair returned verbatim."""
    bstate.start("frozen-impl", inspector_prompt="frozen-insp")  # bead_id=None

    with patch("code_puppy.plugins.bead_factory.beads.show") as show:
        impl, insp = build_loop._refresh_build_prompts(
            frozen_implementor="frozen-impl",
            frozen_inspector="frozen-insp",
        )

    show.assert_not_called()
    assert (impl, insp) == ("frozen-impl", "frozen-insp")
