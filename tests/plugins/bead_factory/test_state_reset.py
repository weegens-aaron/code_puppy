"""Tests for ``state.reset()`` — the test-isolation factory reset.

bead_chain-2yk: the chain state is a process-wide singleton, so any
mutation in one test leaks into the next unless something puts it back to
defaults. ``reset()`` is that something. These tests pin its contract:

  1. It returns *every* field to its just-constructed default.
  2. The module-level ``reset()`` shortcut delegates to the instance.
  3. It differs from ``stop()`` exactly in that it also zeroes the tally
     (``stop`` deliberately preserves ``completed_count`` for the rollup).
"""

from __future__ import annotations

from code_puppy.plugins.bead_factory import state


def _dirty_the_singleton() -> state.ChainState:
    """Scribble non-default values onto the shared singleton."""
    s = state.get_state()
    s.active = True
    s.current_bead = {"id": "bead_chain-xyz", "title": "scratch"}
    s.completed_count = 7
    s.max_iterations = 3
    return s


def test_reset_returns_all_fields_to_defaults():
    """Every field must match a freshly-constructed instance."""
    s = _dirty_the_singleton()
    s.reset()

    fresh = state.ChainState()
    assert s.active == fresh.active is False
    assert s.current_bead == fresh.current_bead is None
    assert s.completed_count == fresh.completed_count == 0
    assert s.max_iterations == fresh.max_iterations is None


def test_module_level_reset_delegates_to_instance():
    """``state.reset()`` must scrub the same shared singleton."""
    _dirty_the_singleton()
    state.reset()

    s = state.get_state()
    assert s.active is False
    assert s.current_bead is None
    assert s.completed_count == 0
    assert s.max_iterations is None


def test_reset_zeroes_tally_unlike_stop():
    """reset() clears completed_count; stop() deliberately keeps it.

    This guards the documented distinction: stop() leaves the tally so the
    end-of-run epic rollup can read the final 'closed N this run' count,
    whereas reset() is a full factory reset for test isolation.
    """
    s = _dirty_the_singleton()
    s.stop()
    assert s.active is False
    assert s.completed_count == 7, "stop() must NOT touch the tally"

    s.reset()
    assert s.completed_count == 0, "reset() must zero the tally"


def test_reset_leaves_singleton_pristine_for_isolation():
    """After reset the singleton is indistinguishable from a fresh one."""
    _dirty_the_singleton()
    state.reset()
    assert not state.is_active()
    assert state.get_state().current_bead_id is None
