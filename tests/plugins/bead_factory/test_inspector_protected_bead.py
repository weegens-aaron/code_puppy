"""Regression tests for bead-factory-c9n: inspectors keep the bead contract.

Epic bead-factory-cri protects the active bead's content from the
implementor's context compaction. There are TWO independent paths by which an
LLM inspector must still see the *full* bead requirements, and this module
locks BOTH so the "inspectors keep the bead contract" requirement can never
silently regress:

1. **Fresh full prompt in the inspector's USER prompt.** The inspector is a
   raw pydantic_ai agent with no ``load_prompt`` system pin, so it always
   receives the FULL compose (content + scaffolding) inline -- re-rendered
   from the live bead each inspection (bead-factory-462 + 2mb). This is
   independent of the implementor's history and therefore immune to the
   implementor's compaction. Even when the implementor's own build prompt is
   slimmed to scaffolding-only (because its content rides the pinned system
   prompt), the inspector still gets the whole bead.

2. **Protected contract visible in the implementor HISTORY.** The bead is
   pinned into the implementor's system prompt (bead-factory-5wv), which
   pydantic_ai delivers as the message-level ``instructions`` field on a
   ``ModelRequest`` -- NOT a message part. The inspector's read-only
   ``inspect_build_history`` tool must therefore surface ``instructions`` (it
   previously only walked ``.parts``, so the pinned contract was invisible --
   the gap this bead closed).
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from code_puppy.plugins.bead_factory import build_loop, inspector, inspector_config
from code_puppy.plugins.bead_factory import build_state as bstate
from code_puppy.plugins.bead_factory.inspector_config import InspectorConfig
from code_puppy.plugins.bead_factory.prompt import build_prompts_for_arming

# A bead whose body carries a string that appears ONLY in the bead content,
# never in the chain scaffolding -- so we can assert presence/absence cleanly.
_CONTRACT_NEEDLE = "ACCEPTANCE-NEEDLE-must-handle-empty-input"
_BEAD = {
    "id": "bead-factory-c9n",
    "title": "Verify inspector coverage of the protected bead",
    "description": "A distinctive description body.",
    "issue_type": "task",
    "priority": 2,
    "acceptance_criteria": _CONTRACT_NEEDLE,
}


@pytest.fixture
def isolated_inspectors():
    """Force the inspector registry into a per-test tmp file."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "inspectors.json")
        with patch.object(inspector_config, "INSPECTORS_FILE", path):
            yield path


@pytest.fixture(autouse=True)
def _reset_build_state():
    bstate.get_state().stop()
    yield
    bstate.get_state().stop()


def _fake_agent(history: list | None = None):
    agent = MagicMock()
    agent.name = "code-puppy"
    agent.get_message_history = MagicMock(return_value=history or [])
    agent.get_model_name = MagicMock(return_value="fallback-model")
    return agent


# ---------------------------------------------------------------------------
# Claim 1: the inspector gets the FULL bead inline, fresh, immune to compaction
# ---------------------------------------------------------------------------


def test_arming_gives_inspector_full_bead_even_when_implementor_slimmed():
    """When the content is pinned, the implementor prompt is slimmed but the
    inspector prompt still carries the full bead content."""
    implementor, inspector_prompt = build_prompts_for_arming(_BEAD, inject_content=True)

    # Implementor user-message build prompt is slimmed -> no bead content.
    assert _CONTRACT_NEEDLE not in implementor
    # Inspector ALWAYS gets the full compose inline -> bead content present.
    assert _CONTRACT_NEEDLE in inspector_prompt


def test_inspector_user_prompt_embeds_the_full_build():
    """The build string handed to an inspector is embedded verbatim in its
    user prompt, so the bead requirements ride the inspector's own request."""
    user_prompt = inspector._inspector_user_prompt(
        build=f"FULL BUILD with {_CONTRACT_NEEDLE}",
        response="some response",
        error=None,
    )
    assert _CONTRACT_NEEDLE in user_prompt


@pytest.mark.asyncio
async def test_turn_end_feeds_full_inspector_prompt_not_slimmed(isolated_inspectors):
    """The build loop must hand the FULL inspector copy (with bead content) to
    the inspectors -- never the slimmed implementor copy.

    Regression guard for the 462 split: ``get_inspector_prompt()`` is the full
    compose; ``get_prompt()`` is the slimmed one. The inspector must see the
    former.
    """
    slimmed = "SCAFFOLDING ONLY -- contract is pinned elsewhere"
    full = f"FULL bead compose containing {_CONTRACT_NEEDLE}"
    # bead_id=None -> _refresh_build_prompts soft-returns the frozen pair
    # (no bd call), so the frozen inspector copy is what reaches the inspector.
    bstate.start(slimmed, inspector_prompt=full, bead_id=None)
    inspector_config.add_inspector(InspectorConfig(name="checker", model="m"))

    captured: dict[str, str] = {}

    async def fake_inspect_build(*, build, inspector_config, **_kwargs):
        captured["build"] = build
        return inspector.BuildInspection(
            inspector_name=inspector_config.name,
            complete=True,
            notes="",
            raw_response="",
        )

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspect_build),
        patch.object(build_loop, "display_inspector"),
    ):
        await build_loop.on_interactive_turn_end(
            agent=_fake_agent(),
            prompt=slimmed,
            result=MagicMock(output="done"),
        )

    assert _CONTRACT_NEEDLE in captured["build"]
    assert captured["build"] == full
    assert "SCAFFOLDING ONLY" not in captured["build"]


# ---------------------------------------------------------------------------
# Claim 2: the pinned contract (system instructions) is visible in the history
# the inspector reads via inspect_build_history
# ---------------------------------------------------------------------------


def _slimmed_pinned_history() -> list:
    """The shape the implementor history takes once the pin is active:

    the user-message build prompt is slimmed to scaffolding-only, and the full
    bead contract rides the message-level ``instructions`` (system prompt).
    """
    return [
        ModelRequest(
            parts=[UserPromptPart(content="SCAFFOLDING ONLY user build prompt")],
            instructions=f"## Protected Task Contract\n{_CONTRACT_NEEDLE}",
        ),
        ModelResponse(parts=[TextPart(content="working on it")]),
    ]


def test_history_window_surfaces_pinned_contract():
    """The pinned contract (in instructions) must appear even though the
    user-message build prompt has been slimmed to scaffolding-only."""
    out = inspector._format_history_window(_slimmed_pinned_history())
    assert _CONTRACT_NEEDLE in out
    # And the slimmed user prompt still shows too -- we add, never replace.
    assert "SCAFFOLDING ONLY" in out


def test_history_window_surfaces_contract_even_with_nonmatching_query():
    """A query that matches nothing in the message parts must NOT hide the
    protected contract -- it's the spec the inspector grades against."""
    out = inspector._format_history_window(
        _slimmed_pinned_history(), query="totally-absent-substring-zzz"
    )
    assert _CONTRACT_NEEDLE in out


def test_history_window_query_matches_contract_text():
    """An inspector can query for a term living only in the pinned contract and
    still select the carrying message (instructions are searchable)."""
    out = inspector._format_history_window(
        _slimmed_pinned_history(), query=_CONTRACT_NEEDLE
    )
    assert _CONTRACT_NEEDLE in out
    # The carrying ModelRequest was selected (not the '(no matching...)' miss).
    assert "no matching implementor history messages" not in out


def test_latest_instructions_picks_most_recent_nonempty():
    """We take the latest non-empty instructions: pydantic_ai re-attaches the
    freshly re-rendered contract to the newest request each turn."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="x")], instructions="STALE"),
        ModelRequest(parts=[UserPromptPart(content="y")], instructions=None),
        ModelRequest(parts=[UserPromptPart(content="z")], instructions="CURRENT"),
    ]
    assert inspector._latest_instructions(messages) == "CURRENT"


def test_history_window_no_instructions_has_no_contract_block():
    """No pin -> no contract block: behaviour for non-bead-factory runs is
    unchanged (no spurious header)."""
    messages = [ModelRequest(parts=[UserPromptPart(content="just a user msg")])]
    out = inspector._format_history_window(messages)
    assert "protected task contract" not in out.lower()
    assert "just a user msg" in out


def test_contract_block_is_budget_capped():
    """An enormous pinned contract is truncated, not allowed to blow the
    inspector's context budget unbounded."""
    huge = "BEAD-" + ("y" * 50_000)
    messages = [ModelRequest(parts=[UserPromptPart(content="u")], instructions=huge)]
    out = inspector._format_history_window(messages, max_chars=1000)
    assert "instructions truncated" in out
    # The block is bounded near max_chars, not the full 50k.
    assert len(out) < 5000


@pytest.mark.asyncio
async def test_history_tool_returns_pinned_contract_via_inspect_build_history():
    """End-to-end-ish: the registered ``inspect_build_history`` tool returns a
    view that contains the pinned contract.

    This wires the real tool registration to a real pydantic_ai Agent and
    pulls the registered function out of its toolset, then invokes it -- the
    exact call path an inspector model takes.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    agent = Agent(model=TestModel())
    inspector._register_build_history_tool(agent, _slimmed_pinned_history())

    # Find the registered tool's underlying function across pydantic_ai shapes.
    func = None
    toolset = getattr(agent, "_function_toolset", None) or getattr(
        agent, "_function_tools", None
    )
    tools = getattr(toolset, "tools", toolset)
    if isinstance(tools, dict):
        entry = tools.get("inspect_build_history")
        func = getattr(entry, "function", entry)
    assert func is not None, "inspect_build_history tool was not registered"

    out = await func(None)
    assert _CONTRACT_NEEDLE in out
