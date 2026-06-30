"""LLM inspector helpers for build mode.

An inspector is a small, read-only pydantic_ai Agent that examines the
implementor's latest response (and optionally its message history) and
returns a structured verdict: complete/not + remediation notes.

Each inspector has its own model and prompt -- see ``inspector_config.py`` for
persistence. The build loop fans these out in parallel; see
``build_loop._run_build_inspectors``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext, ToolOutput, UsageLimits

from code_puppy.agents._history import stringify_part
from code_puppy.agents.agent_manager import load_agent
from code_puppy.model_factory import ModelFactory, make_model_settings
from code_puppy.model_utils import prepare_prompt_for_model
from code_puppy.tools.subagent_context import subagent_context

from .inspector_config import DEFAULT_INSPECTOR_PROMPT, InspectorConfig

# Default cap on per-inspector pydantic_ai requests. Inspectors are read-only
# and shouldn't ever need this many round-trips -- if one does, it's almost
# certainly looping. 200 leaves plenty of headroom for inspection-heavy
# verification (file reads + grep + a shell test run) without letting a
# runaway inspector burn the whole build loop's token budget.
BUILD_INSPECTOR_REQUEST_LIMIT = 200

_READ_ONLY_TOOLS = {
    "list_files",
    "read_file",
    "grep",
    "agent_run_shell_command",
    "load_image_for_analysis",
    "list_agents",
    "invoke_agent",
}


class BuildInspectionOutput(BaseModel):
    """Structured verdict from a build inspector."""

    complete: bool = Field(
        description="True only when the build is verifiably complete."
    )
    notes: str = Field(
        description="Brief rationale plus remediation notes if incomplete."
    )


@dataclass(frozen=True)
class BuildInspection:
    """Final, normalized verdict surfaced to build-loop callers.

    ``abstained`` means the inspector couldn't produce a verdict for an
    infrastructure reason -- model endpoint 404, auth failure, network
    timeout, misconfigured model, etc. Abstaining inspectors are excluded
    from the all-complete tally; they neither pass nor fail the build.
    """

    inspector_name: str
    complete: bool
    notes: str
    raw_response: str
    abstained: bool = False


def _read_only_tools_for_implementor(agent_config: Any) -> list[str]:
    return [
        tool for tool in agent_config.get_available_tools() if tool in _READ_ONLY_TOOLS
    ]


def _format_message(message: Any, index: int) -> str:
    part_lines = [stringify_part(part) for part in getattr(message, "parts", [])]
    text = "\n".join(line for line in part_lines if line)
    return f"[{index}] {message.__class__.__name__}:\n{text or '(empty message)'}"


def _latest_instructions(messages: list[Any]) -> str | None:
    """Most recent non-empty message-level ``instructions``, or ``None``.

    bead-factory-c9n: the active bead's contract is pinned into the
    implementor's SYSTEM PROMPT (bead-factory-5wv). pydantic_ai delivers the
    system prompt as the message-level ``instructions`` field on a
    ``ModelRequest`` -- it is NOT a message *part*. Because
    :func:`_format_message` only walks ``.parts``, the pinned contract would
    otherwise be invisible to an inspector reading the implementor history
    via ``inspect_build_history`` -- and the user-message build prompt is
    slimmed to scaffolding-only while the pin is active (bead-factory-462),
    so the bead content isn't in the parts either. Surfacing the latest
    instructions is what guarantees the protected bead content is ALWAYS
    present in what the inspector reads here, even after the implementor's
    history has been compacted/truncated.

    We take the *latest* non-empty block: pydantic_ai re-attaches the
    (freshly re-rendered) instructions to the newest request each turn, so
    the last one is the current contract; walking back also tolerates
    histories where older requests carry stale or absent instructions.
    """
    for message in reversed(messages):
        instructions = getattr(message, "instructions", None)
        if instructions:
            return str(instructions)
    return None


def _format_protected_contract(
    instructions: str | None, *, max_chars: int
) -> str | None:
    """Render the pinned system instructions as a labelled, budgeted block."""
    if not instructions:
        return None
    text = instructions
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(instructions truncated)…"
    return "[protected task contract — pinned system instructions]\n" + text


def _message_search_text(message: Any) -> str:
    """Lowercased searchable text for a message: its parts AND instructions.

    Including ``instructions`` (the pinned contract) means an inspector can
    query for a term that lives only in the protected bead content and still
    select the message that carries it (bead-factory-c9n).
    """
    parts_text = "\n".join(
        stringify_part(part) for part in getattr(message, "parts", [])
    )
    instructions = getattr(message, "instructions", None) or ""
    return f"{parts_text}\n{instructions}".lower()


def _format_history_window(
    messages: list[Any],
    *,
    query: str | None = None,
    limit: int = 20,
    max_chars: int = 12000,
) -> str:
    if not messages:
        return "(no implementor message history captured)"

    # bead-factory-c9n: ALWAYS surface the pinned contract (the system-prompt
    # instructions) so the protected bead content is present in what the
    # inspector reads -- even when the user-message build prompt was slimmed
    # to scaffolding-only (bead-factory-462) and even when older history was
    # compacted away. This is independent of the query: the contract is the
    # spec the inspector grades against, so it is never filtered out.
    contract = _format_protected_contract(
        _latest_instructions(messages), max_chars=max_chars
    )

    normalized_query = (query or "").strip().lower()
    indexed = list(enumerate(messages))
    if normalized_query:
        indexed = [
            (idx, msg)
            for idx, msg in indexed
            if normalized_query in _message_search_text(msg)
        ]

    selected = indexed[-max(1, min(limit, 100)) :]
    chunks: list[str] = []
    total = 0
    for idx, message in selected:
        block = _format_message(message, idx)
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 500:
                chunks.append(block[:remaining])
            break
        chunks.append(block)
        total += len(block)

    window = (
        "\n\n---\n\n".join(chunks)
        if chunks
        else "(no matching implementor history messages)"
    )
    sections = [section for section in (contract, window) if section]
    return "\n\n===\n\n".join(sections)


def _register_build_history_tool(inspector_agent: Agent, messages: list[Any]) -> None:
    @inspector_agent.tool
    async def inspect_build_history(
        context: RunContext[None],
        query: str | None = None,
        limit: int = 20,
    ) -> str:
        """Inspect the build implementor's read-only message history.

        Args:
            query: Optional case-insensitive substring to search for in message parts.
            limit: Maximum number of recent matching messages to return, capped at 100.
        """
        del context
        return _format_history_window(messages, query=query, limit=limit)


def _strip_thinking_settings(model_settings: dict) -> None:
    """Remove thinking-related settings that conflict with ToolOutput.

    Anthropic models reject thinking + tool output at the same time, and other
    providers don't mind missing keys. We strip:
      * `anthropic_thinking` (Anthropic / Bedrock / Azure Foundry Claude / claude_code)
      * `thinking` (GLM-4.7 / GLM-5 style)
      * `thinking_enabled` / `thinking_level` (Gemini)
      * `extra_body.output_config` (effort knob that only matters with adaptive thinking)
    """
    for key in ("anthropic_thinking", "thinking", "thinking_enabled", "thinking_level"):
        model_settings.pop(key, None)

    extra_body = model_settings.get("extra_body")
    if isinstance(extra_body, dict):
        extra_body.pop("output_config", None)
        if not extra_body:
            model_settings.pop("extra_body", None)


def _inspector_user_prompt(
    build: str,
    response: str | None,
    error: BaseException | None,
) -> str:
    error_text = f"\nRUN ERROR:\n{error}\n" if error else ""
    response_text = response or "(no response captured)"
    return f"""\
Inspect whether this coding build is verifiably complete.

BUILD:
{build}

LATEST AGENT RESPONSE:
{response_text}
{error_text}
You have a read-only `inspect_build_history` tool for the build implementor's transcript.
Use it when the latest response is not enough to confidently verify completion.
"""


async def inspect_build(
    *,
    inspector_config: InspectorConfig,
    implementor_agent: Any,
    build: str,
    response: str | None,
    error: BaseException | None,
    history: list[Any] | None = None,
) -> BuildInspection:
    """Run a single fresh, read-only inspector against the implementor's turn.

    Args:
        inspector_config: The inspector's persisted configuration (name, model, prompt).
        implementor_agent: The build implementor's agent -- used only to discover
            which read-only tools should be available to the inspector.
        build: The original build prompt.
        response: The implementor's most recent textual response (or None).
        error: Any exception raised on the implementor's latest turn.
        history: The implementor's message history (read-only).
    """
    from code_puppy import plugins

    plugins.load_plugin_callbacks()

    model_name = inspector_config.model
    models_config = ModelFactory.load_config()
    if model_name not in models_config:
        # Misconfigured model is an infrastructure issue, not a real
        # verdict -- abstain so we don't block the build loop on it.
        return BuildInspection(
            inspector_name=inspector_config.name,
            complete=False,
            notes=(f"model {model_name!r} not present in the model config"),
            raw_response="",
            abstained=True,
        )

    model = ModelFactory.get_model(model_name, models_config)
    inspector_instructions = inspector_config.prompt or DEFAULT_INSPECTOR_PROMPT
    user_prompt = _inspector_user_prompt(build, response, error)
    prepared = prepare_prompt_for_model(
        model_name,
        inspector_instructions,
        user_prompt,
        prepend_system_to_user=True,
    )

    model_settings = make_model_settings(model_name)
    _strip_thinking_settings(model_settings)

    inspector_agent = Agent(
        model=model,
        instructions=prepared.instructions,
        output_type=ToolOutput(
            BuildInspectionOutput,
            name="build_inspection",
            description="Verdict for the build implementor's latest iteration.",
        ),
        retries=3,
        model_settings=model_settings,
    )

    # Give the inspector the same read-only tools the implementor has, so it
    # can poke at files/run tests/etc to verify completion.
    from code_puppy.tools import register_tools_for_agent

    try:
        implementor_config = load_agent(implementor_agent.name)
        read_only_tools = _read_only_tools_for_implementor(implementor_config)
    except Exception:
        read_only_tools = []

    register_tools_for_agent(
        inspector_agent,
        read_only_tools,
        model_name=model_name,
    )
    _register_build_history_tool(inspector_agent, list(history or []))

    # Run the inspector inside a sub-agent context so:
    #   * its tool-call banners (read_file, grep, shell, etc.) are suppressed
    #   * its reasoning/agent-response chatter doesn't litter the build loop UI
    # The plumbing for this already exists in rich_renderer and tools/display --
    # they check is_subagent() + get_subagent_verbose() and skip rendering.
    try:
        with subagent_context(f"inspector:{inspector_config.name}"):
            result = await inspector_agent.run(
                prepared.user_prompt,
                usage_limits=UsageLimits(request_limit=BUILD_INSPECTOR_REQUEST_LIMIT),
            )
        output = result.output
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Propagate cancellation -- the orchestrator handles cleanup.
        raise
    except Exception as exc:
        # Any other exception during the run is an infrastructure problem
        # (HTTP 4xx/5xx, network timeout, auth failure, vendor SDK bug...).
        # Abstain rather than fail -- the inspector couldn't render a verdict,
        # so it shouldn't get a vote.
        return BuildInspection(
            inspector_name=inspector_config.name,
            complete=False,
            notes=f"endpoint error ({type(exc).__name__}): {exc}",
            raw_response="",
            abstained=True,
        )

    if hasattr(output, "model_dump_json"):
        raw = output.model_dump_json()
        complete = output.complete
        notes = output.notes
    else:
        raw = json.dumps(output)
        complete = bool(output.get("complete"))
        notes = str(output.get("notes", ""))

    return BuildInspection(
        inspector_name=inspector_config.name,
        complete=complete,
        notes=notes,
        raw_response=raw,
    )
