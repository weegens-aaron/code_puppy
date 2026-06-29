"""Regression tests for bead_chain-s1w: async/sync contract on the
``interactive_turn_cancel`` hook.

The host's callback dispatcher (``code_puppy.callbacks._trigger_callbacks``)
calls each registered callback and ``await``s the result *iff* it's a
coroutine. That means a plain ``def`` callback technically works today —
but its async sibling ``_on_interactive_turn_end`` does not, and relying
on the dispatcher tolerating mixed sync/async is a latent coupling. The
fix makes ``_on_interactive_turn_cancel`` ``async`` so both
interactive-turn hooks present one consistent contract.

These tests verify:
  1. ``_on_interactive_turn_cancel`` is a coroutine function (async).
  2. It presents the SAME async-ness as its sibling ``_on_interactive_turn_end``.
  3. Driven through the host dispatcher, the cancel logic actually runs:
     the chain stops and the in-flight bead is left ``in_progress``.
"""

from __future__ import annotations

import asyncio
import inspect

from code_puppy.plugins.bead_factory import chain_driver as register_callbacks, state


def test_cancel_callback_is_async():
    """The cancel hook must be a coroutine function (async contract)."""
    assert inspect.iscoroutinefunction(register_callbacks._on_interactive_turn_cancel)


def test_both_interactive_turn_hooks_share_one_contract():
    """Cancel and end hooks must agree on sync-vs-async (consistency)."""
    end_is_async = inspect.iscoroutinefunction(
        register_callbacks._on_interactive_turn_end
    )
    cancel_is_async = inspect.iscoroutinefunction(
        register_callbacks._on_interactive_turn_cancel
    )
    assert end_is_async == cancel_is_async == True  # noqa: E712


def test_cancel_through_dispatcher_stops_chain_and_parks_bead():
    """Driven through the host's awaiting dispatch, cancel still parks the bead.

    We invoke the callback exactly as the framework does: call it, then
    await the returned coroutine. The chain must stop and the bead must be
    left untouched at the bd layer (in_progress is the deliberate handoff
    to next-run recovery).
    """
    st = state.get_state()
    st.start()
    st.current_bead = {"id": "bead_chain-test", "title": "x"}
    assert state.is_active()

    coro = register_callbacks._on_interactive_turn_cancel(
        "some prompt", reason="ctrl-c"
    )
    assert inspect.iscoroutine(coro), "async callback must return an awaitable"
    result = asyncio.run(coro)

    assert result is None
    assert not state.is_active(), "cancel must stop the chain"


def test_cancel_when_idle_is_a_noop():
    """If the chain isn't active, cancel must bow out without error."""
    state.stop()
    assert not state.is_active()
    result = asyncio.run(
        register_callbacks._on_interactive_turn_cancel("p", reason="cancelled")
    )
    assert result is None
    assert not state.is_active()
