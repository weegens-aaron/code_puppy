"""Regression test for bead-factory-76s: lock the three rework-turn properties.

When inspection FAILS and the build loop sends the implementing agent back for
rework, ``build_loop.on_interactive_turn_end`` returns a continuation dict that
MUST guarantee three things for the fresh rework turn. This module pins all
three so none can silently regress:

  1. **COMPLETELY FRESH** — the continuation sets ``clear_context=True``, which
     drives cli_runner.py (~L1063) to call
     ``current_agent.clear_message_history()`` (emptying ``_message_history``)
     and rotate the autosave session. We model that mechanic faithfully with a
     real history list and assert it is empty after the clear.
  2. **INSPECTION NOTES fed back** — every inspector's PASS/FAIL/ABSTAIN + notes
     reach the rework turn. Post bead-factory-t4c they ride the bead's ``notes``
     field (appended via ``bd update --append-notes``), surfaced back through
     the live re-fetch pipeline.
  3. **LIVE BEAD NOTES present** — a note appended to the bead *after* claim time
     reaches the fresh implementor. This is what bead-factory-8u4 (render the
     notes field) + bead-factory-2mb (live re-fetch reflected in the retry
     continuation) deliver.

CRITICAL assertion level (per the bead design): we assert against the
*effective implementor input* — the re-injected system prompt
(``system_prompt.on_load_prompt``, which pins the live bead content under the
compaction epic, bead-factory-5wv) PLUS the user-message continuation prompt
(slimmed to scaffolding-only by bead-factory-462). The live notes may arrive via
*either* channel, so asserting against the combined input keeps this test valid
no matter which channel delivers them.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from code_puppy.plugins.bead_factory import build_loop, inspector_config, prompt
from code_puppy.plugins.bead_factory import build_state as bstate
from code_puppy.plugins.bead_factory import state as chain_state
from code_puppy.plugins.bead_factory import system_prompt
from code_puppy.plugins.bead_factory.inspector import BuildInspection
from code_puppy.plugins.bead_factory.inspector_config import InspectorConfig

# A note appended to the bead AFTER claim time (e.g. by bead-factory-2mb's
# mid-run edits). The claim-time render never saw it; only the live re-fetch can.
LIVE_NOTE = "LIVE-NOTE-76s: refactor the soft-fail branch before retrying"

# The actionable remediation an inspector emits on a FAIL verdict. Post-t4c this
# is appended to the bead's notes field, then surfaced via the live re-fetch.
REMEDIATION_NOTE = "tests still failing in widget.py"

BEAD_ID = "bead-factory-76s"


class FakeImplementor:
    """Stand-in for the implementor agent carrying a REAL message history.

    cli_runner clears context by calling ``clear_message_history()`` when a
    continuation sets ``clear_context=True`` (cli_runner.py ~L1063), so we model
    that mechanic faithfully: a real list the build loop can read AND that we can
    prove is emptied after the clear (the "completely fresh" guarantee).

    Deliberately does NOT expose ``get_pydantic_agent`` so the inspector-model
    fallback in ``_resolve_inspectors`` routes through ``get_model_name`` — the
    same shape a non-pydantic agent presents in production.
    """

    def __init__(self, history: list) -> None:
        self._message_history = list(history)

    def get_message_history(self) -> list:
        return self._message_history

    def get_model_name(self) -> str:
        return "fallback-model"

    def clear_message_history(self) -> None:
        # Mirrors BaseAgent.clear_message_history: the history is emptied.
        self._message_history = []


@pytest.fixture
def isolated_inspectors():
    """Force the inspector registry into a throwaway file per test."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "inspectors.json")
        with patch.object(inspector_config, "INSPECTORS_FILE", path):
            yield path


@pytest.fixture(autouse=True)
def _reset_state():
    """Pristine chain + build state around every test (no cross-test leakage)."""
    chain_state.get_state().reset()
    bstate.get_state().stop()
    yield
    chain_state.get_state().reset()
    bstate.get_state().stop()


@pytest.fixture(autouse=True)
def _pure_prompt_fetchers():
    """Keep prompt rendering pure + fast — no bd shell-outs for the memory
    digest, template lint, or epic-metadata enrichment. These soft-fail to empty
    in production anyway; stubbing them keeps the test deterministic and quick.
    """
    with (
        patch.object(prompt, "_fetch_lint_warnings", return_value=[]),
        patch.object(prompt, "_fetch_memory_digest", return_value={}),
        patch.object(prompt, "_format_epic_metadata_lines", return_value=[]),
    ):
        yield


def _live_bead() -> dict:
    """A ``bd show``-shaped bead carrying a post-claim note (LIVE_NOTE)."""
    return {
        "id": BEAD_ID,
        "title": "Regression-test the rework turn",
        "description": "lock the three rework-turn guarantees",
        "issue_type": "task",
        "priority": 1,
        "notes": LIVE_NOTE,
    }


async def _drive_incomplete_rework_turn(agent: FakeImplementor, live_bead: dict):
    """Arm the chain/build state and run ONE incomplete-inspection turn.

    Returns the continuation dict the build loop hands back to cli_runner. Leaves
    ``chain_state.current_bead`` pointing at whatever the live re-fetch refreshed
    it to, so the caller can reconstruct the effective implementor input
    (system pin + user-message continuation).
    """

    def fake_show(_bead_id):
        return live_bead

    def fake_append_notes(_bead_id, block):
        # Mirror ``bd update --append-notes``: the block lands on the bead so a
        # subsequent ``bd show`` reflects it — exactly how t4c remediation
        # feedback reaches the fresh implementor through the live pipeline.
        existing = live_bead.get("notes", "")
        live_bead["notes"] = (existing + "\n\n" + block).strip()

    async def fake_inspect(**_kwargs):
        return BuildInspection(
            inspector_name="checker",
            complete=False,
            notes=REMEDIATION_NOTE,
            raw_response="",
        )

    # Arm the chain with a STALE claim-time snapshot (no notes) so the pin is
    # active (active + current_bead set) and the implementor user-message is
    # slimmed to scaffolding-only (bead-factory-462). The live note is therefore
    # genuinely absent at claim time and can only arrive via the re-fetch.
    claim_snapshot = {**live_bead, "notes": ""}
    chain_state.start()
    chain_state.get_state().current_bead = claim_snapshot

    impl_prompt, insp_prompt = prompt.build_prompts_for_arming(
        claim_snapshot, inject_content=True
    )
    bstate.start(impl_prompt, inspector_prompt=insp_prompt, bead_id=BEAD_ID)

    inspector_config.add_inspector(InspectorConfig(name="checker", model="m"))

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspect),
        patch.object(build_loop, "display_inspector"),
        patch("code_puppy.plugins.bead_factory.beads.show", side_effect=fake_show),
        patch(
            "code_puppy.plugins.bead_factory.beads.append_notes",
            side_effect=fake_append_notes,
        ),
    ):
        return await build_loop.on_interactive_turn_end(
            agent=agent,
            prompt=impl_prompt,
            result=MagicMock(output="my attempt"),
        )


@pytest.mark.asyncio
async def test_incomplete_continuation_is_clear_context_and_build(isolated_inspectors):
    """Acceptance criterion: the incomplete-inspection continuation dict carries
    ``clear_context=True`` and ``reason == 'build'``."""
    agent = FakeImplementor(history=["m1", "m2"])

    continuation = await _drive_incomplete_rework_turn(agent, _live_bead())

    assert continuation is not None
    assert continuation["clear_context"] is True
    assert continuation["reason"] == "build"


@pytest.mark.asyncio
async def test_rework_turn_is_completely_fresh(isolated_inspectors):
    """Property #1: clear_context drives the implementor history to EMPTY.

    We model the cli_runner mechanic (cli_runner.py ~L1063): on a continuation
    with ``clear_context=True`` it calls ``clear_message_history()``. Here we
    prove (a) the continuation requests the clear and (b) the clear empties the
    history — together: a completely fresh rework turn.
    """
    agent = FakeImplementor(history=["msg-a", "msg-b", "msg-c"])
    assert agent.get_message_history(), "precondition: history is non-empty"

    continuation = await _drive_incomplete_rework_turn(agent, _live_bead())

    assert continuation is not None
    assert continuation["clear_context"] is True

    # Replay exactly what cli_runner does when clear_context is set.
    if continuation.get("clear_context"):
        agent.clear_message_history()

    assert agent.get_message_history() == [], (
        "rework turn must start completely fresh — history not cleared"
    )


@pytest.mark.asyncio
async def test_rework_turn_carries_inspection_and_live_notes(isolated_inspectors):
    """Properties #2 and #3: the inspection remediation notes AND a post-claim
    live bead note both reach the EFFECTIVE implementor input on the rework turn.

    Effective input = re-injected system prompt (the pinned live bead content,
    bead-factory-5wv) + the user-message continuation prompt (slimmed to
    scaffolding-only, bead-factory-462). We assert against the COMBINED input so
    the test stays valid no matter which channel delivers the notes.
    """
    agent = FakeImplementor(history=["m1"])
    live_bead = _live_bead()

    continuation = await _drive_incomplete_rework_turn(agent, live_bead)

    assert continuation is not None

    # Reconstruct what the fresh implementor EFFECTIVELY receives next turn:
    # the re-assembled system prompt pin (reads chain_state.current_bead, which
    # the live re-fetch refreshed to the live bead) + the user-message prompt.
    system_pin = system_prompt.on_load_prompt() or ""
    effective_input = system_pin + "\n\n" + continuation["prompt"]

    # Property #3: the post-claim live note reaches the fresh implementor.
    assert LIVE_NOTE in effective_input, (
        "live bead note appended post-claim did not reach the rework turn"
    )
    # Property #2: the inspector's remediation note reaches the fresh implementor.
    assert REMEDIATION_NOTE in effective_input, (
        "inspection remediation notes did not reach the rework turn"
    )

    # And the pin really was refreshed to the live bead (not the stale snapshot)
    # so the live content rides the compaction-protected system prompt.
    assert chain_state.get_state().current_bead is live_bead
