"""Regression tests for the goal/wiggum prerequisite gate (bead_chain-c87).

Originally bead-chain depended on a *separate* ``code_puppy.plugins.wiggum``
plugin via bare top-level imports, so when wiggum wasn't loaded the whole
module failed to import with a raw ``ImportError`` and the user saw a cryptic
loader message. The fix imported wiggum defensively, recorded its absence in
``_WIGGUM_AVAILABLE``, and made ``/bead-chain`` degrade with a friendly
warning instead of blowing up.

**Merged (bead_factory) semantics.** After the fusion (epic bead-factory-zfk)
the goal loop is *in-package* — ``chain_driver`` imports ``goal_loop`` /
``loop_state`` relatively from the same package, so the prerequisite can only
fail on a catastrophic in-package import error. ``_WIGGUM_AVAILABLE`` is
therefore ``True`` by default, and the friendly-degradation path is now a
defensive belt-and-suspenders branch rather than a routine "wiggum isn't
installed" case. The command/turn wiring lives in ``chain_driver`` (not
``register_callbacks``), so these tests drive it there.

The behavioural contract is otherwise unchanged:
  1. The friendly message still names wiggum and ``/bead-chain``.
  2. When the loop is (defensively) marked unavailable, ``/bead-chain`` bails
     early with that message and never touches ``wiggum_state`` / ``bd``.
  3. When the loop IS available the prerequisite gate is transparent — the
     command proceeds past the gate exactly as before.
"""

from __future__ import annotations

import pytest

from code_puppy.plugins.bead_factory import chain_driver as register_callbacks
from code_puppy.plugins.bead_factory import state


@pytest.fixture(autouse=True)
def _idle_chain():
    """Each test starts and ends with an idle chain."""
    state.reset()
    yield
    state.reset()


def test_loop_is_available_in_fused_package():
    """Merged semantics: the in-package goal loop is available by default.

    Pre-merge wiggum was a separate plugin that could legitimately be absent;
    post-merge ``goal_loop`` / ``loop_state`` are siblings in this very
    package, so a healthy import leaves the prerequisite satisfied. The
    degradation branch below only fires on a catastrophic import failure.
    """
    assert register_callbacks._WIGGUM_AVAILABLE is True


def test_message_is_human_readable_and_names_wiggum():
    """The friendly message must mention wiggum and /bead-chain, not a stacktrace."""
    msg = register_callbacks._WIGGUM_MISSING_MESSAGE
    assert "wiggum" in msg.lower()
    assert "bead-chain" in msg.lower()
    # No raw exception noise — it's a sentence, not a traceback fragment.
    assert "Traceback" not in msg
    assert "ModuleNotFoundError" not in msg


def test_command_degrades_gracefully_when_loop_missing(monkeypatch):
    """With the loop defensively unavailable, /bead-chain warns and returns True.

    Crucially it must bail BEFORE any ``bd`` probe or ``wiggum_state``
    dereference — so we make those explode and confirm they're never hit.
    """
    warnings: list[str] = []
    monkeypatch.setattr(register_callbacks, "_WIGGUM_AVAILABLE", False)
    monkeypatch.setattr(
        register_callbacks, "emit_warning", lambda m: warnings.append(m)
    )

    def _boom(*_a, **_k):  # pragma: no cover - asserts it's never called
        raise AssertionError("must not reach bd probe when the loop is missing")

    monkeypatch.setattr(register_callbacks, "enforce_single_in_progress", _boom)
    monkeypatch.setattr(register_callbacks, "next_ready", _boom)

    result = register_callbacks.handle_bead_chain_command("/bead-chain")

    assert result is True, "degraded command consumes the slash command"
    assert warnings == [register_callbacks._WIGGUM_MISSING_MESSAGE]
    assert not state.is_active(), "no chain should have started"


def test_gate_is_transparent_when_loop_available(monkeypatch):
    """With the loop present, the prerequisite gate doesn't short-circuit.

    We force the gate open then stop at the very next step (an empty queue),
    proving control flowed past the prerequisite check exactly as it did
    before this fix — i.e. zero behavioural change on the happy path.
    """
    monkeypatch.setattr(register_callbacks, "_WIGGUM_AVAILABLE", True)
    infos: list[str] = []
    monkeypatch.setattr(register_callbacks, "emit_info", lambda m: infos.append(m))
    monkeypatch.setattr(register_callbacks, "emit_warning", lambda m: None)
    monkeypatch.setattr(register_callbacks, "enforce_single_in_progress", lambda: None)
    monkeypatch.setattr(register_callbacks, "next_ready", lambda: None)

    result = register_callbacks.handle_bead_chain_command("/bead-chain")

    assert result is True
    # The empty-queue message proves we sailed past the prerequisite gate and
    # the "starting…" ack into the real probe path.
    assert any("No ready beads" in m for m in infos)
