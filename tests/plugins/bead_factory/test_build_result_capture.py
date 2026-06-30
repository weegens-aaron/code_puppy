"""Capture-at-exit coverage for the build loop (bead-factory-60e).

Each terminal exit of ``build_loop.on_interactive_turn_end`` -- and
``on_interactive_turn_cancel`` -- must record a :class:`BuildResult` via the
consume-once sink with the matching ``stop_reason`` and the live verdict tally,
WITHOUT changing control flow (every path still calls ``state.stop()`` and
returns the same value it returned before) and fail-soft (a capture hiccup
degrades to today's behaviour, never raises).

We monkeypatch ``_run_build_inspectors`` so each exit branch can be driven
deterministically without spinning up real inspector models.
"""

from __future__ import annotations

import asyncio

import pytest

from code_puppy.plugins.bead_factory import build_loop
from code_puppy.plugins.bead_factory import build_result as br
from code_puppy.plugins.bead_factory import build_state as state
from code_puppy.plugins.bead_factory.build_result import StopReason
from code_puppy.plugins.bead_factory.inspector import BuildInspection


def _verdict(name, *, complete=True, abstained=False, notes=""):
    return BuildInspection(
        inspector_name=name,
        complete=complete,
        notes=notes,
        raw_response="",
        abstained=abstained,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset both the build-state singleton and the result sink each test."""
    state.stop()
    br.clear()
    yield
    state.stop()
    br.clear()


def _stub_inspectors(monkeypatch, *, returns=None, raises=None):
    async def _fake(*_a, **_k):
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(build_loop, "_run_build_inspectors", _fake)


def _run_turn_end(agent=object()):
    return asyncio.run(build_loop.on_interactive_turn_end(agent, "prompt"))


# --- NO_PROMPT --------------------------------------------------------------


def test_no_prompt_captures_no_prompt_result():
    # state is inactive -> get_prompt() returns None -> early return.
    assert not state.is_active()
    result = _run_turn_end()
    assert result is None  # control flow unchanged
    captured = br.take_last()
    assert captured is not None
    assert captured.stop_reason is StopReason.NO_PROMPT
    assert captured.completed is False
    assert captured.total == 0
    assert captured.loop_count == 0


# --- COMPLETE ---------------------------------------------------------------


def test_complete_captures_complete_result_with_verdicts(monkeypatch):
    state.start("build prompt", bead_id=None)
    verdicts = [_verdict("a", complete=True), _verdict("b", complete=True)]
    _stub_inspectors(monkeypatch, returns=(True, "notes", verdicts))

    result = _run_turn_end()
    assert result is None  # complete -> stop -> None
    assert not state.is_active()  # control flow unchanged: stopped

    captured = br.take_last()
    assert captured is not None
    assert captured.stop_reason is StopReason.COMPLETE
    assert captured.completed is True
    assert captured.total == 2
    assert captured.passed == 2
    assert captured.loop_count == 1
    assert captured.verdicts == tuple(verdicts)


# --- MAX_ITERATIONS ---------------------------------------------------------


def test_max_iterations_captures_max_iterations_result(monkeypatch):
    state.start("build prompt", bead_id=None)
    monkeypatch.setattr(build_loop, "get_build_max_iterations", lambda: 1)
    verdicts = [_verdict("a", complete=False, notes="nope")]
    _stub_inspectors(monkeypatch, returns=(False, "notes", verdicts))

    result = _run_turn_end()
    assert result is None  # max iters -> stop -> None
    assert not state.is_active()

    captured = br.take_last()
    assert captured is not None
    assert captured.stop_reason is StopReason.MAX_ITERATIONS
    assert captured.completed is False
    assert captured.total == 1
    assert captured.failed == 1
    assert captured.loop_count == 1


# --- CANCELLED (inspectors raise) -------------------------------------------


def test_inspectors_cancelled_captures_cancelled_result(monkeypatch):
    state.start("build prompt", bead_id=None)
    _stub_inspectors(monkeypatch, raises=asyncio.CancelledError())

    result = _run_turn_end()
    assert result is None  # cancelled -> stop -> None
    assert not state.is_active()

    captured = br.take_last()
    assert captured is not None
    assert captured.stop_reason is StopReason.CANCELLED
    assert captured.completed is False
    assert captured.total == 0
    assert captured.loop_count == 1


# --- retry path does NOT capture (loop still running) -----------------------


def test_retry_path_records_no_terminal_result(monkeypatch):
    state.start("build prompt", bead_id=None)
    monkeypatch.setattr(build_loop, "get_build_max_iterations", lambda: 5)
    verdicts = [_verdict("a", complete=False, notes="more work")]
    _stub_inspectors(monkeypatch, returns=(False, "notes", verdicts))

    result = _run_turn_end()
    # Retry path returns a continuation dict, not None.
    assert isinstance(result, dict)
    assert result["reason"] == "build"
    # No terminal result on a non-terminal turn.
    assert br.peek_last() is None


# --- on_interactive_turn_cancel ---------------------------------------------


def test_turn_cancel_captures_cancelled_result():
    state.start("build prompt", bead_id=None)
    state.increment()
    state.increment()  # pretend we got two loops deep

    build_loop.on_interactive_turn_cancel("prompt", reason="ctrl-c")
    assert not state.is_active()  # control flow unchanged: stopped

    captured = br.take_last()
    assert captured is not None
    assert captured.stop_reason is StopReason.CANCELLED
    assert captured.completed is False
    assert captured.loop_count == 2


def test_turn_cancel_when_idle_captures_nothing():
    assert not state.is_active()
    build_loop.on_interactive_turn_cancel("prompt")
    assert br.peek_last() is None


# --- fail-soft --------------------------------------------------------------


def test_capture_failure_does_not_break_loop(monkeypatch):
    """A capture hiccup must degrade to today's behaviour, never raise."""
    state.start("build prompt", bead_id=None)
    verdicts = [_verdict("a", complete=True)]
    _stub_inspectors(monkeypatch, returns=(True, "notes", verdicts))

    def _boom(_result):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(br, "set_last", _boom)

    # Loop must still stop + return None despite the capture blowing up.
    result = _run_turn_end()
    assert result is None
    assert not state.is_active()


def test_capture_failure_in_cancel_does_not_break_loop(monkeypatch):
    state.start("build prompt", bead_id=None)

    def _boom(_result):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(br, "set_last", _boom)

    # Must not raise; loop still stops.
    build_loop.on_interactive_turn_cancel("prompt", reason="ctrl-c")
    assert not state.is_active()
