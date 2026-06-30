"""bead-factory-0sc: the chain driver surfaces the build loop's terminal-turn
result at its close boundary.

The build loop stashes a :class:`~build_result.BuildResult` via the
consume-once sink at every terminal exit (bead-factory-60e). This bead wires
``chain_driver._on_interactive_turn_end`` to *read* that result the moment the
build loop has stopped and just before ``close_current_bead_success`` -- so the
per-bead outcome (abstain count + remediation feedback) is available at the
close boundary for any future consumer.

These tests pin the three acceptance criteria:

  1. **Retrievable via the chosen transport.** The result set on the sink is
     consumed (``take_last``) at the close boundary -- the sink is drained
     afterward, so no stale verdict can leak into the next bead's close.
  2. **Read-only + fail-soft.** A missing/None result degrades gracefully (the
     close path runs exactly as before) and no exception escapes.
  3. **No new control-flow decisions.** The continuation dict still flows
     straight through and the close -> activate ordering is untouched.
"""

from __future__ import annotations

import asyncio

from code_puppy.plugins.bead_factory import build_result
from code_puppy.plugins.bead_factory import chain_driver, state
from code_puppy.plugins.bead_factory.build_result import StopReason


def _run_hook():
    return asyncio.run(
        chain_driver._on_interactive_turn_end(
            agent=object(),
            prompt="p",
            result=None,
            success=True,
            error=None,
        )
    )


def _wire_close_activate(monkeypatch, *, bead, continuation):
    monkeypatch.setattr(chain_driver, "close_current_bead_success", lambda: bead)
    monkeypatch.setattr(
        chain_driver, "activate_next_bead", lambda just_closed: continuation
    )


def test_close_boundary_consumes_the_sink(monkeypatch):
    """A stashed result is drained at the close boundary (consume-once)."""
    build_result.clear()
    bead = {"id": "bead-factory-1", "title": "x"}
    continuation = {"prompt": "next"}
    _wire_close_activate(monkeypatch, bead=bead, continuation=continuation)

    res = build_result.build_result([], StopReason.COMPLETE, bead_id="bead-factory-1")
    build_result.set_last(res)

    st = state.get_state()
    st.start()
    st.current_bead = bead

    result = _run_hook()

    # Control flow is unchanged: the continuation dict flows straight through.
    assert result is continuation
    # The sink was drained at the close boundary -- nothing leaks to next bead.
    assert build_result.peek_last() is None


def test_empty_sink_degrades_gracefully(monkeypatch):
    """No stashed result must not change the close path or raise."""
    build_result.clear()
    bead = {"id": "bead-factory-2", "title": "y"}
    continuation = {"prompt": "next"}
    _wire_close_activate(monkeypatch, bead=bead, continuation=continuation)

    st = state.get_state()
    st.start()
    st.current_bead = bead

    result = _run_hook()

    assert result is continuation
    assert build_result.peek_last() is None


def test_read_failure_is_fail_soft(monkeypatch):
    """A take_last hiccup is swallowed -- the chain closes the bead as usual."""
    build_result.clear()
    bead = {"id": "bead-factory-3", "title": "z"}
    continuation = {"prompt": "next"}
    _wire_close_activate(monkeypatch, bead=bead, continuation=continuation)

    def _boom():
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(build_result, "take_last", _boom)

    st = state.get_state()
    st.start()
    st.current_bead = bead

    # Must not raise; close/next proceeds normally.
    result = _run_hook()
    assert result is continuation


def test_helper_returns_the_result_then_clears():
    """The helper retrieves the BuildResult and drains the sink (consume-once)."""
    build_result.clear()
    res = build_result.build_result(
        [], StopReason.MAX_ITERATIONS, bead_id="bead-factory-4"
    )
    build_result.set_last(res)

    got = chain_driver._consume_terminal_build_result()

    assert got is res
    assert build_result.peek_last() is None
    # Idempotent on an empty sink.
    assert chain_driver._consume_terminal_build_result() is None
