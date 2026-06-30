"""Tests for the parallel-inspectors orchestration in bead_factory/build_loop."""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from code_puppy.plugins.bead_factory import build_loop, inspector_config
from code_puppy.plugins.bead_factory.inspector import BuildInspection
from code_puppy.plugins.bead_factory.inspector_config import InspectorConfig


@pytest.fixture
def isolated_inspectors():
    """Force the inspector registry to live in a tmp file for each test."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "inspectors.json")
        with patch.object(inspector_config, "INSPECTORS_FILE", path):
            yield path


def _fake_agent(name: str = "code-puppy", history: list | None = None):
    agent = MagicMock()
    agent.name = name
    agent.get_message_history = MagicMock(return_value=history or [])
    agent.get_model_name = MagicMock(return_value="fallback-model")
    return agent


def _verdict(name: str, *, complete: bool, notes: str = "") -> BuildInspection:
    return BuildInspection(
        inspector_name=name, complete=complete, notes=notes, raw_response=""
    )


@pytest.mark.asyncio
async def test_run_build_inspectors_parallel_all_pass(isolated_inspectors):
    """Two inspectors both pass with no notes => build complete."""
    inspector_config.add_inspector(InspectorConfig(name="alpha", model="m1"))
    inspector_config.add_inspector(InspectorConfig(name="beta", model="m2"))

    call_log: list[str] = []

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        call_log.append(inspector_config.name)
        await asyncio.sleep(0.01)
        return _verdict(inspector_config.name, complete=True, notes="")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        all_complete, notes, verdicts = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert all_complete is True
    # Per-inspector PASS headers always included for visibility in the success banner.
    assert "[alpha] ✅ PASS" in notes
    assert "[beta] ✅ PASS" in notes
    assert {v.inspector_name for v in verdicts} == {"alpha", "beta"}
    assert set(call_log) == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_run_build_inspectors_one_fails_means_incomplete(isolated_inspectors):
    inspector_config.add_inspector(InspectorConfig(name="alpha", model="m1"))
    inspector_config.add_inspector(InspectorConfig(name="beta", model="m2"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        if inspector_config.name == "alpha":
            return _verdict("alpha", complete=True, notes="")
        return _verdict("beta", complete=False, notes="tests are failing")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        all_complete, notes, verdicts = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert all_complete is False
    assert "[alpha] ✅ PASS" in notes
    assert "[beta] ❌ FAIL" in notes
    assert "tests are failing" in notes
    assert len(verdicts) == 2


@pytest.mark.asyncio
async def test_passing_inspector_with_rationale_notes_still_completes(
    isolated_inspectors,
):
    """complete=True is the 'no remediation needed' signal.

    Notes alongside a passing verdict are rationale (e.g., "Yes, the response
    satisfies the build because..."), not remediation. They must not block
    completion — otherwise a verbose inspector keeps the loop going forever.
    """
    inspector_config.add_inspector(InspectorConfig(name="chatty", model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        return _verdict(
            "chatty",
            complete=True,
            notes="Yes, the response 'Hello there!' satisfies 'say hi'.",
        )

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        all_complete, notes, _ = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert all_complete is True
    assert "satisfies" in notes  # rationale still appears in the display string


@pytest.mark.asyncio
async def test_inspector_exception_becomes_abstaining_verdict(isolated_inspectors):
    """A crashed inspector abstains — doesn't get a vote, doesn't block build."""
    inspector_config.add_inspector(InspectorConfig(name="crashy", model="m"))

    async def fake_inspector_build(**_kwargs):
        raise RuntimeError("kaboom")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
        patch("code_puppy.error_logging.log_error"),
    ):
        all_complete, notes, verdicts = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    # Only inspector abstained → no voters → can't decide → incomplete.
    assert all_complete is False
    assert verdicts[0].abstained is True
    assert "kaboom" in notes
    assert "ABSTAIN" in notes


@pytest.mark.asyncio
async def test_default_inspector_used_when_none_configured(isolated_inspectors):
    """No inspectors configured => synthesize a 'default' inspector w/ implementor's model."""
    seen_names: list[str] = []

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        seen_names.append(inspector_config.name)
        return _verdict(inspector_config.name, complete=True, notes="")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        all_complete, notes, _ = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert seen_names == ["default"]
    assert all_complete is True


@pytest.mark.asyncio
async def test_inspectors_run_in_parallel_not_serial(isolated_inspectors):
    """Three slow inspectors should finish in ~one slow-inspector interval, not three."""
    for n in ("a", "b", "c"):
        inspector_config.add_inspector(InspectorConfig(name=n, model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        await asyncio.sleep(0.2)
        return _verdict(inspector_config.name, complete=True, notes="")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        all_complete, _, _ = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )
        elapsed = loop.time() - t0

    assert all_complete is True
    # Serial would be ~0.6s; parallel should be ~0.2s. Allow generous slack.
    assert elapsed < 0.45, f"inspectors ran serially? elapsed={elapsed}"


@pytest.mark.asyncio
async def test_remediation_notes_format(isolated_inspectors):
    inspector_config.add_inspector(InspectorConfig(name="a", model="m"))
    inspector_config.add_inspector(InspectorConfig(name="b", model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        if inspector_config.name == "a":
            return _verdict("a", complete=False, notes="missing tests\nadd them")
        return _verdict("b", complete=True, notes="")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        _, notes, _ = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert "[a] ❌ FAIL" in notes
    assert "  missing tests" in notes  # indented
    assert "  add them" in notes
    assert "[b] ✅ PASS" in notes


@pytest.mark.asyncio
async def test_turn_end_feeds_remediation_notes_to_next_iteration(isolated_inspectors):
    """When build is incomplete, the next iteration's prompt must include the notes."""
    from code_puppy.plugins.bead_factory import build_state as state

    state.start("fix the bug")

    inspector_config.add_inspector(InspectorConfig(name="checker", model="m"))

    async def fake_inspector_build(**_kwargs):
        return _verdict("checker", complete=False, notes="bug still present in foo.py")

    try:
        with (
            patch.object(build_loop, "inspect_build", new=fake_inspector_build),
            patch.object(build_loop, "display_inspector"),
        ):
            next_request = await build_loop.on_interactive_turn_end(
                agent=_fake_agent(),
                prompt="fix the bug",
                result=MagicMock(output="I tried"),
            )
    finally:
        state.stop()

    # No bead_id was set, so the append soft-fails and we fall back to
    # inlining the notes (bead-factory-t4c soft-fail contract).
    assert next_request is not None
    assert next_request["reason"] == "build"
    assert next_request["clear_context"] is True
    assert "fix the bug" in next_request["prompt"]
    assert "bug still present in foo.py" in next_request["prompt"]
    assert "Inspector remediation notes" in next_request["prompt"]


@pytest.mark.asyncio
async def test_turn_end_appends_remediation_to_active_bead(isolated_inspectors):
    """bead-factory-t4c: with a live bead_id, the remediation block is APPENDED
    to the bead's notes (delimited per loop) instead of inlined."""
    from code_puppy.plugins.bead_factory import beads
    from code_puppy.plugins.bead_factory import build_state as state

    state.start("fix the bug", bead_id="bf-1")
    inspector_config.add_inspector(InspectorConfig(name="checker", model="m"))

    async def fake_inspector_build(**_kwargs):
        return _verdict("checker", complete=False, notes="bug still present in foo.py")

    append_mock = MagicMock()
    try:
        with (
            patch.object(build_loop, "inspect_build", new=fake_inspector_build),
            patch.object(build_loop, "display_inspector"),
            patch.object(beads, "append_notes", new=append_mock),
            patch.object(
                build_loop,
                "_refresh_build_prompts",
                return_value=("RERENDERED build prompt", "inspector prompt"),
            ),
        ):
            next_request = await build_loop.on_interactive_turn_end(
                agent=_fake_agent(),
                prompt="fix the bug",
                result=MagicMock(output="I tried"),
            )
    finally:
        state.stop()

    # The block was appended to the ACTIVE bead, delimited per loop.
    append_mock.assert_called_once()
    called_bead_id, called_block = append_mock.call_args.args
    assert called_bead_id == "bf-1"
    assert called_block.startswith("### Inspection remediation (loop #1)")
    assert "bug still present in foo.py" in called_block

    # Happy path: the bead is the single source — NO inline concat.
    assert next_request is not None
    assert next_request["reason"] == "build"
    assert "Inspector remediation notes" not in next_request["prompt"]
    assert next_request["prompt"] == "RERENDERED build prompt"


@pytest.mark.asyncio
async def test_turn_end_stops_loop_on_full_success(isolated_inspectors):
    from code_puppy.plugins.bead_factory import build_state as state

    state.start("ship it")

    inspector_config.add_inspector(InspectorConfig(name="checker", model="m"))

    async def fake_inspector_build(**_kwargs):
        return _verdict("checker", complete=True, notes="")

    try:
        with (
            patch.object(build_loop, "inspect_build", new=fake_inspector_build),
            patch.object(build_loop, "display_inspector"),
        ):
            next_request = await build_loop.on_interactive_turn_end(
                agent=_fake_agent(),
                prompt="ship it",
                result=MagicMock(output="done"),
            )
    finally:
        # state should already be stopped by the success path
        if state.is_active():
            state.stop()

    assert next_request is None
    assert state.is_active() is False


@pytest.mark.asyncio
async def test_inspectors_running_in_parallel_dont_share_failure_state(
    isolated_inspectors,
):
    """One inspector crashing must not prevent the other from returning a verdict.

    Crashed/erroring inspectors ABSTAIN — they're excluded from the tally and
    don't block build completion (see the 'endpoint errors should abstain'
    bug fix). So crashy + ok=PASS means the build completes.
    """
    inspector_config.add_inspector(InspectorConfig(name="ok", model="m"))
    inspector_config.add_inspector(InspectorConfig(name="crashy", model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        if inspector_config.name == "crashy":
            raise RuntimeError("boom")
        await asyncio.sleep(0.05)
        return _verdict("ok", complete=True, notes="")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
        patch("code_puppy.error_logging.log_error"),
    ):
        all_complete, _, verdicts = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    by_name = {v.inspector_name: v for v in verdicts}
    assert by_name["ok"].complete is True
    assert by_name["crashy"].abstained is True  # crashed → abstain
    assert all_complete is True  # crashy excluded, ok passed → build complete


@pytest.mark.asyncio
async def test_display_serialized_after_parallel_run(isolated_inspectors):
    """Every inspector's per-verdict banner must be displayed exactly once.

    Regression test for the 'I only saw Judy's output' bug: when banners
    were emitted concurrently from inside _run_single_inspector, their writes
    interleaved and one overwrote the other. Now we emit per-inspector banners
    AFTER asyncio.gather() finishes, so each inspector gets its own line.
    """
    inspector_config.add_inspector(InspectorConfig(name="judy", model="m1"))
    inspector_config.add_inspector(InspectorConfig(name="joe-brown", model="m2"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        await asyncio.sleep(0.01)
        return _verdict(inspector_config.name, complete=True, notes="rationale")

    seen_messages: list[str] = []

    def capture_display(msg, *_, **__):
        seen_messages.append(msg)

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector", side_effect=capture_display),
    ):
        await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    # 1 announcement + 1 per-inspector verdict banner each = 3 total
    assert len(seen_messages) == 3
    # The announcement mentions both inspectors by name
    assert "judy" in seen_messages[0] and "joe-brown" in seen_messages[0]
    # Per-inspector verdicts both appear in the post-gather output
    joined = "\n".join(seen_messages[1:])
    assert "[judy]" in joined
    assert "[joe-brown]" in joined
    assert joined.count("✅ PASS") == 2


@pytest.mark.asyncio
async def test_single_inspector_uses_singular_phrasing(isolated_inspectors):
    """When there's only one inspector, the announcement banner is more natural."""
    inspector_config.add_inspector(InspectorConfig(name="solo", model="m"))

    async def fake_inspector_build(**_kwargs):
        return _verdict("solo", complete=True, notes="")

    seen: list[str] = []

    def capture(msg, *_, **__):
        seen.append(msg)

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector", side_effect=capture),
    ):
        await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    # Singular phrasing: "Asking inspector..." not "Running N inspectors..."
    assert seen[0].lower().startswith("asking inspector")
    assert "solo" in seen[0]


@pytest.mark.asyncio
async def test_all_pass_with_rationale_completes(isolated_inspectors):
    """Two inspectors both pass with rationale notes — should still complete."""
    inspector_config.add_inspector(InspectorConfig(name="a", model="m"))
    inspector_config.add_inspector(InspectorConfig(name="b", model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        return _verdict(
            inspector_config.name,
            complete=True,
            notes=f"{inspector_config.name} approves with light reasoning.",
        )

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        all_complete, _, verdicts = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert all_complete is True
    assert all(v.complete for v in verdicts)


@pytest.mark.asyncio
async def test_brackets_in_inspector_name_are_preserved_in_display(isolated_inspectors):
    """Regression: Rich was eating [joe-brown] because brackets parse as markup.

    We rely on _display_banner_message passing markup=False on the message.
    Here we test the orchestrator's output strings to make sure the format
    string itself contains the [name] payload — that's what gets handed to
    the display helper.
    """
    inspector_config.add_inspector(InspectorConfig(name="joe-brown", model="m"))
    inspector_config.add_inspector(InspectorConfig(name="judy", model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        return _verdict(inspector_config.name, complete=True, notes="ok")

    captured: list[str] = []

    def capture(msg, *_, **__):
        captured.append(msg)

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector", side_effect=capture),
    ):
        await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    # Both inspector names with brackets must appear in the per-inspector display lines.
    per_inspector = captured[1:]
    assert any("[joe-brown]" in m for m in per_inspector), per_inspector
    assert any("[judy]" in m for m in per_inspector), per_inspector


@pytest.mark.asyncio
async def test_final_complete_banner_omits_notes_body(isolated_inspectors):
    """When the build completes, the final banner must NOT re-dump per-inspector notes.

    Per-inspector lines are already shown by _run_build_inspectors; dumping the full
    notes block again into the final '✅ BUILD COMPLETE!' banner was the
    'output shown twice' bug.
    """
    from code_puppy.plugins.bead_factory import build_state as state

    state.start("say hi")
    inspector_config.add_inspector(InspectorConfig(name="judy", model="m"))

    async def fake_inspector_build(**_kwargs):
        return _verdict("judy", complete=True, notes="some long rationale here")

    captured_args: list[tuple[tuple, dict]] = []

    def capture(*args, **kwargs):
        captured_args.append((args, kwargs))

    try:
        with (
            patch.object(build_loop, "inspect_build", new=fake_inspector_build),
            patch.object(build_loop, "display_inspector", side_effect=capture),
        ):
            await build_loop.on_interactive_turn_end(
                agent=_fake_agent(),
                prompt="say hi",
                result=MagicMock(output="hi"),
            )
    finally:
        if state.is_active():
            state.stop()

    # Find the final "✅ BUILD COMPLETE!" call
    completion_calls = [
        (args, kwargs)
        for args, kwargs in captured_args
        if args and "BUILD COMPLETE" in args[0]
    ]
    assert len(completion_calls) == 1
    args, kwargs = completion_calls[0]
    # The "details" argument (positional[1] or kwargs['details']) must NOT
    # contain the per-inspector notes block — that was the duplication bug.
    details = kwargs.get("details") if len(args) < 2 else args[1]
    assert details is None or "some long rationale here" not in str(details)


@pytest.mark.asyncio
async def test_inspector_runs_inside_subagent_context(isolated_inspectors):
    """inspect_build must wrap inspector_agent.run() in subagent_context.

    This is what suppresses the inspector's tool-call banners and intermediate
    chatter (read_file, grep, agent reasoning, etc.) in the build-loop UI.
    """
    from code_puppy.plugins.bead_factory import inspector as inspector_module
    from code_puppy.tools.subagent_context import is_subagent

    inspector_config.add_inspector(InspectorConfig(name="checker", model="fake-model"))

    saw_subagent_flag: list[bool] = []

    async def fake_run(_user_prompt, **_kwargs):
        # When the real inspector_agent.run() executes, is_subagent() should be True.
        # Accept arbitrary kwargs (e.g. usage_limits) so this stub keeps
        # working as inspect_build grows new pydantic_ai run options.
        saw_subagent_flag.append(is_subagent())

        class _R:
            output = inspector_module.BuildInspectionOutput(complete=True, notes="ok")

        return _R()

    # Stub everything heavy: model load, prompt prep, tool registration.
    with (
        patch.object(
            inspector_module.ModelFactory,
            "load_config",
            return_value={"fake-model": {}},
        ),
        patch.object(
            inspector_module.ModelFactory, "get_model", return_value=MagicMock()
        ),
        patch.object(inspector_module, "make_model_settings", return_value={}),
        patch.object(
            inspector_module,
            "prepare_prompt_for_model",
            return_value=MagicMock(instructions="i", user_prompt="u"),
        ),
        patch.object(
            inspector_module,
            "load_agent",
            return_value=MagicMock(get_available_tools=lambda: []),
        ),
        patch("code_puppy.tools.register_tools_for_agent"),
        patch.object(inspector_module, "Agent") as mock_agent_cls,
    ):
        mock_agent = MagicMock()
        mock_agent.run = fake_run
        mock_agent_cls.return_value = mock_agent

        verdict = await inspector_module.inspect_build(
            inspector_config=InspectorConfig(name="checker", model="fake-model"),
            implementor_agent=_fake_agent(),
            build="g",
            response="r",
            error=None,
            history=[],
        )

    assert saw_subagent_flag == [True], (
        "inspector_agent.run() must execute inside subagent_context"
    )
    # After the context exits, we're back in main-agent context.
    assert is_subagent() is False
    assert verdict.complete is True


@pytest.mark.asyncio
async def test_abstaining_inspector_excluded_from_tally(isolated_inspectors):
    """One abstaining + one PASS = build complete (abstain excluded)."""
    inspector_config.add_inspector(InspectorConfig(name="passy", model="m"))
    inspector_config.add_inspector(InspectorConfig(name="busted", model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        if inspector_config.name == "busted":
            return BuildInspection(
                inspector_name="busted",
                complete=False,
                notes="endpoint error (NotFoundError): 404 model_not_found",
                raw_response="",
                abstained=True,
            )
        return _verdict("passy", complete=True, notes="all good")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        all_complete, notes, _ = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert all_complete is True
    assert "ABSTAIN" in notes
    assert "model_not_found" in notes


@pytest.mark.asyncio
async def test_abstaining_inspector_with_failing_other_still_incomplete(
    isolated_inspectors,
):
    """Abstain + FAIL = incomplete (abstain ignored, FAIL still counts)."""
    inspector_config.add_inspector(InspectorConfig(name="busted", model="m"))
    inspector_config.add_inspector(InspectorConfig(name="strict", model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        if inspector_config.name == "busted":
            return BuildInspection(
                inspector_name="busted",
                complete=False,
                notes="endpoint error",
                raw_response="",
                abstained=True,
            )
        return _verdict("strict", complete=False, notes="tests still failing")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector"),
    ):
        all_complete, notes, _ = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert all_complete is False
    assert "tests still failing" in notes


@pytest.mark.asyncio
async def test_all_abstain_means_cannot_decide(isolated_inspectors):
    """When every inspector abstains, we can't decide \u2192 incomplete with a warning."""
    inspector_config.add_inspector(InspectorConfig(name="a", model="m"))
    inspector_config.add_inspector(InspectorConfig(name="b", model="m"))

    async def fake_inspector_build(*, inspector_config, **_kwargs):
        return BuildInspection(
            inspector_name=inspector_config.name,
            complete=False,
            notes="endpoint error",
            raw_response="",
            abstained=True,
        )

    seen: list[str] = []

    def capture(msg, *_, **__):
        seen.append(msg)

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector", side_effect=capture),
    ):
        all_complete, _, _ = await build_loop._run_build_inspectors(
            agent=_fake_agent(),
            build="g",
            result=None,
            error=None,
        )

    assert all_complete is False
    # User-facing warning when nobody could vote
    assert any("All inspectors abstained" in m for m in seen), seen


@pytest.mark.asyncio
async def test_cancellation_during_gather_shows_banner_and_propagates(
    isolated_inspectors,
):
    """Ctrl+C inside _run_build_inspectors: show a visible banner, then re-raise.

    Letting cancellation propagate out of _run_build_inspectors is what stops the
    build loop cleanly — if we swallowed it and returned 'incomplete', the
    caller would just request another retry. The OUTER plugin boundary
    (_on_interactive_turn_end) catches it so the REPL stays alive.
    """
    inspector_config.add_inspector(InspectorConfig(name="slow", model="m"))

    async def fake_inspector_build(**_kwargs):
        await asyncio.sleep(10)
        return _verdict("slow", complete=True, notes="")

    with (
        patch.object(build_loop, "inspect_build", new=fake_inspector_build),
        patch.object(build_loop, "display_inspector") as mock_display,
    ):
        task = asyncio.create_task(
            build_loop._run_build_inspectors(
                agent=_fake_agent(),
                build="g",
                result=None,
                error=None,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    display_calls = [c.args[0] for c in mock_display.call_args_list]
    assert any("cancel" in m.lower() for m in display_calls), display_calls


@pytest.mark.asyncio
async def test_turn_end_swallows_cancellation_and_stops_build_mode(isolated_inspectors):
    """Same regression at the higher level: _on_interactive_turn_end must
    catch cancellation, stop build mode, and return None (no continuation).
    """
    from code_puppy.plugins.bead_factory import build_state as state

    state.start("do a thing")
    inspector_config.add_inspector(InspectorConfig(name="slow", model="m"))

    async def fake_inspector_build(**_kwargs):
        await asyncio.sleep(10)
        return _verdict("slow", complete=True, notes="")

    try:
        with (
            patch.object(build_loop, "inspect_build", new=fake_inspector_build),
            patch.object(build_loop, "display_inspector"),
        ):
            task = asyncio.create_task(
                build_loop.on_interactive_turn_end(
                    agent=_fake_agent(),
                    prompt="do a thing",
                    result=MagicMock(output="trying"),
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()

            try:
                next_request = await task
            except asyncio.CancelledError:
                pytest.fail("Cancellation escaped _on_interactive_turn_end")
    finally:
        if state.is_active():
            state.stop()

    assert next_request is None
    # Build mode was stopped as a side-effect.
    assert state.is_active() is False
