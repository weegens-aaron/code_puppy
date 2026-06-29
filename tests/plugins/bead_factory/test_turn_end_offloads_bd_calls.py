"""Regression tests for bead_chain-u0b: the ``interactive_turn_end`` hook
must run its blocking ``bd`` subprocess work OFF the event-loop thread.

``_on_interactive_turn_end`` drives the close -> next-bead loop, and both
``close_current_bead_success`` and ``activate_next_bead`` shell out to
``bd`` synchronously (up to ~45s worst case under retries). Running them
inline on code_puppy's single interactive event loop would freeze the UI
for the duration. The fix wraps each call in ``asyncio.to_thread`` so the
loop stays responsive.

These tests verify the three behaviours that matter:

  1. **Off-loop execution.** Both lifecycle calls run on a *different*
     thread than the event loop (the asyncio.to_thread contract).
  2. **Preserved ordering.** ``close_current_bead_success`` completes
     fully *before* ``activate_next_bead`` starts (no premature
     parallelism), and the continuation dict it returns flows through.
  3. **Short-circuit intact.** If the close stops the chain (close
     failure path), ``activate_next_bead`` is never called.
"""

from __future__ import annotations

import asyncio
import threading

from code_puppy.plugins.bead_factory import chain_driver as register_callbacks, state


def _run_hook():
    """Invoke the async turn-end hook exactly as the host dispatcher does."""
    return asyncio.run(
        register_callbacks._on_interactive_turn_end(
            agent=object(),
            prompt="p",
            result=None,
            success=True,
            error=None,
        )
    )


def test_idle_hook_is_a_noop_and_touches_no_bd(monkeypatch):
    """When the chain isn't active the hook returns None without bd work."""
    state.stop()

    called = {"close": False, "activate": False}

    def _close():
        called["close"] = True
        return None

    def _activate(just_closed):
        called["activate"] = True
        return None

    monkeypatch.setattr(register_callbacks, "close_current_bead_success", _close)
    monkeypatch.setattr(register_callbacks, "activate_next_bead", _activate)

    assert _run_hook() is None
    assert called == {"close": False, "activate": False}


def test_bd_calls_run_off_the_event_loop_thread(monkeypatch):
    """close + activate must each execute on a worker thread, not the loop."""
    loop_thread = threading.get_ident()
    seen = {}

    bead = {"id": "bead_chain-1", "title": "x"}
    continuation = {"prompt": "next", "clear_context": True}

    def _close():
        seen["close_thread"] = threading.get_ident()
        return bead

    def _activate(just_closed):
        seen["activate_thread"] = threading.get_ident()
        seen["activate_arg"] = just_closed
        return continuation

    monkeypatch.setattr(register_callbacks, "close_current_bead_success", _close)
    monkeypatch.setattr(register_callbacks, "activate_next_bead", _activate)

    st = state.get_state()
    st.start()
    st.current_bead = bead

    result = _run_hook()

    # The continuation dict flows straight through.
    assert result is continuation
    # The just-closed bead is threaded from close -> activate.
    assert seen["activate_arg"] is bead
    # Both blocking calls ran OFF the event-loop thread (to_thread contract).
    assert seen["close_thread"] != loop_thread
    assert seen["activate_thread"] != loop_thread


def test_ordering_close_completes_before_activate_starts(monkeypatch):
    """Strict close -> activate ordering: no premature parallelism."""
    order = []

    bead = {"id": "bead_chain-2", "title": "y"}

    def _close():
        order.append("close-start")
        order.append("close-end")
        return bead

    def _activate(just_closed):
        order.append("activate-start")
        return {"prompt": "n"}

    monkeypatch.setattr(register_callbacks, "close_current_bead_success", _close)
    monkeypatch.setattr(register_callbacks, "activate_next_bead", _activate)

    st = state.get_state()
    st.start()
    st.current_bead = bead

    _run_hook()

    assert order == ["close-start", "close-end", "activate-start"]


def test_short_circuit_when_close_stops_the_chain(monkeypatch):
    """If close halts the chain, activate_next_bead must NOT be called."""
    activate_called = {"hit": False}

    def _close():
        # Simulate the close-failure / epic-leak path: it stops the chain.
        state.stop()
        return {"id": "bead_chain-3"}

    def _activate(just_closed):
        activate_called["hit"] = True
        return {"prompt": "should-not-happen"}

    monkeypatch.setattr(register_callbacks, "close_current_bead_success", _close)
    monkeypatch.setattr(register_callbacks, "activate_next_bead", _activate)

    st = state.get_state()
    st.start()
    st.current_bead = {"id": "bead_chain-3", "title": "z"}

    result = _run_hook()

    assert result is None
    assert activate_called["hit"] is False, "activate must be skipped on stop"
