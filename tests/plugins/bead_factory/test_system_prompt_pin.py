"""Tests for the compaction-protected task-contract pin (bead-factory-5wv).

``system_prompt.on_load_prompt`` is the ``load_prompt`` callback that pins the
active bead's content into the system prompt while a bead-factory chain is
running. ``BaseAgent.get_full_system_prompt()`` re-runs this fresh every turn
and ``messages[0]`` is always compaction-protected, so the contract survives
arbitrarily deep tool-call histories (epic bead-factory-cri).

Contract:
  * No chain active            -> None (strict no-op).
  * Chain active, no bead      -> None.
  * Chain active, current bead -> protected-contract header + bead content.
  * The pinned block is the *clean* content render (format_bead_content),
    NOT the scaffolding-heavy build prompt (no-duplication design, 462).
  * Failures fail-soft to None, never crash prompt assembly.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import state, system_prompt  # noqa: E402


def _base_bead(**extra) -> dict:
    bead = {
        "id": "demo-1",
        "title": "Do the thing",
        "description": "A thing that must be done.",
        "issue_type": "task",
        "priority": 1,
    }
    bead.update(extra)
    return bead


def teardown_function() -> None:
    """Keep the process-wide singleton from leaking between tests."""
    state.reset()


# --------------------------------------------------------------------------
# No-op paths
# --------------------------------------------------------------------------


def test_returns_none_when_chain_inactive():
    state.reset()
    assert system_prompt.on_load_prompt() is None


def test_returns_none_when_active_but_no_bead():
    state.start()  # active, but current_bead stays None
    assert state.is_active() is True
    assert system_prompt.on_load_prompt() is None


# --------------------------------------------------------------------------
# Active pin
# --------------------------------------------------------------------------


def test_pins_bead_content_with_protected_header_when_active():
    state.start()
    state.get_state().current_bead = _base_bead()

    out = system_prompt.on_load_prompt()
    assert out is not None
    # Protected-contract header is present.
    assert "Protected Task Contract" in out
    assert "survives context compaction" in out
    # ...and the bead's own content rides along.
    assert "Complete beads issue demo-1: Do the thing" in out
    assert "A thing that must be done." in out


def test_pin_uses_clean_content_render_not_build_scaffolding():
    """The pin must NOT duplicate the whole-project build scaffolding.

    No-duplication design (bead-factory-462): the memory digest, done-checklist
    and bug-discovery protocol live on the live build-prompt user message, not
    in the protected copy.
    """
    state.start()
    state.get_state().current_bead = _base_bead()

    out = system_prompt.on_load_prompt()
    assert out is not None
    assert "When you believe this is done:" not in out
    assert "BUG DISCOVERY PROTOCOL" not in out
    assert "## Persistent Memories" not in out


def test_fail_soft_returns_none_on_render_error(monkeypatch):
    state.start()
    state.get_state().current_bead = _base_bead()

    def _boom(_bead):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(system_prompt, "format_bead_content", _boom)
    assert system_prompt.on_load_prompt() is None


def test_registered_as_load_prompt_callback():
    """The wiring module registers the pin under the load_prompt hook.

    Registration is an *import-time* side effect of ``register_callbacks``. A
    plain ``import`` is a no-op once the module is cached in ``sys.modules``,
    so under the full suite (where an earlier test already imported it and the
    autouse callback snapshot/restore fixture in ``tests/conftest.py`` later
    rolled the registry back to a pre-import snapshot) the callback would be
    missing and this assertion would flake (bead-factory-w2y).

    Forcing ``importlib.reload`` re-executes the module-scope registration,
    which is idempotent: ``register_callback`` dedups by identity and
    ``register_command`` just overwrites its registry entry. That makes the
    wiring assertion deterministic regardless of suite ordering.
    """
    import importlib

    import code_puppy.callbacks as callbacks
    import code_puppy.plugins.bead_factory.register_callbacks as register_callbacks

    importlib.reload(register_callbacks)

    registered = callbacks._callbacks.get("load_prompt", [])
    assert system_prompt.on_load_prompt in registered
