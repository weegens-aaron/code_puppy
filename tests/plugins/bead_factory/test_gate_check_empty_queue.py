"""Empty-queue gate probe behaviour (bead_chain-x3g / FB-3).

bead-chain never ran ``bd gate check``, so resolvable timer / gh:run /
gh:pr / bead gates kept their *targets* out of ``bd ready`` and the
chain stopped short of ready-pending-poll work. The fix wires a
:func:`lifecycle.probe_resolved_gates` call into the empty-queue branch
of :func:`lifecycle.activate_next_bead`, *before* the drain pass.

These tests pin all three acceptance criteria:

  1. On an empty queue, ``bd gate check`` runs once before the chain
     declares done.
  2. Newly-resolved gates re-open their targets for the next iteration
     (the chain re-probes ``bd ready`` and keeps trotting).
  3. A gate-check failure warns and never halts the loop (soft-fail).

Requires ``code_puppy`` on the path (lifecycle imports its messaging +
build state); ``conftest.py`` registers the plugin as a package.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from code_puppy.plugins.bead_factory import (
    lifecycle,
    lifecycle_close,
    state,
)
from code_puppy.plugins.bead_factory.beads import BeadsError
from code_puppy.plugins.bead_factory import build_state


def _bead(bead_id: str, **extra) -> dict:
    b = {"id": bead_id, "issue_type": "task", "status": "open", "title": "t"}
    b.update(extra)
    return b


@pytest.fixture(autouse=True)
def _fresh_state():
    """Each test starts with an engaged, uncapped chain at zero tally."""
    s = state.get_state()
    s.active = True
    s.current_bead = None
    s.completed_count = 0
    s.max_iterations = None
    yield
    state.reset()


def _stub_activation_surface(monkeypatch):
    """Neutralise the post-pick side effects so we test only the branch.

    Returns a dict of call-tracking lists the tests can assert against.
    """
    calls: dict[str, list] = {"rollup": [], "claim": [], "build": []}
    monkeypatch.setattr(lifecycle, "open_blocker_ids", lambda _bid, _bead=None: [])
    monkeypatch.setattr(
        lifecycle,
        "_fan_out_gate_verdict",
        lambda _bid, _bead=None: SimpleNamespace(blocked=False, mode_known=True),
    )
    monkeypatch.setattr(lifecycle, "ensure_epic_in_progress", lambda _b: None)
    monkeypatch.setattr(lifecycle, "claim", lambda bid: calls["claim"].append(bid))
    monkeypatch.setattr(lifecycle, "format_bead_as_build", lambda *_a, **_k: "BUILD")
    monkeypatch.setattr(
        lifecycle, "rollup_completed_epics", lambda: calls["rollup"].append(True)
    )
    monkeypatch.setattr(
        build_state, "start", lambda *a, **k: calls["build"].append((a, k))
    )
    return calls


def test_empty_queue_runs_gate_check_once_then_stops(monkeypatch):
    """Criterion 1: gate check runs once before the chain declares done.

    Queue is empty and no gate resolves -> probe runs exactly once, the
    drain pass fires, and the chain stops cleanly.
    """
    gate_calls: list[bool] = []

    def fake_check_gates():
        gate_calls.append(True)
        return {"checked": 0, "resolved": 0, "escalated": 0, "errors": 0}

    monkeypatch.setattr(lifecycle, "pick_next_bead", lambda _jc: None)
    monkeypatch.setattr(lifecycle_close, "check_gates", fake_check_gates)
    calls = _stub_activation_surface(monkeypatch)

    result = lifecycle.activate_next_bead(None)

    assert result is None, "no work -> no continuation"
    assert gate_calls == [True], "gate check must run exactly once"
    assert calls["rollup"] == [True], "drain pass should still run"
    assert state.get_state().active is False, "chain should stop"


def test_resolved_gate_reopens_target_and_chain_continues(monkeypatch):
    """Criterion 2: a resolved gate re-opens its target for the next pick.

    First ``bd ready`` is empty; the gate check resolves one gate; the
    re-probe now sees the freshly-unblocked target and the chain claims
    it and keeps going instead of stopping.
    """
    target = _bead("TARGET")
    pick_results = [None, target]
    monkeypatch.setattr(lifecycle, "pick_next_bead", lambda _jc: pick_results.pop(0))
    monkeypatch.setattr(
        lifecycle_close,
        "check_gates",
        lambda: {"checked": 1, "resolved": 1, "escalated": 0, "errors": 0},
    )
    calls = _stub_activation_surface(monkeypatch)

    result = lifecycle.activate_next_bead(None)

    assert result is not None, "resolved gate should re-open work"
    assert result["reason"] == "bead_chain"
    assert calls["claim"] == ["TARGET"], "the re-opened target gets claimed"
    assert calls["rollup"] == [], "we did NOT declare done -> no drain pass"
    assert state.get_state().active is True, "chain keeps trotting"


def test_resolved_gate_but_still_no_ready_falls_through_to_drain(monkeypatch):
    """A gate resolves but its target is still blocked -> clean drain.

    Re-probe still returns None (e.g. target has another open blocker),
    so the chain falls through to the drain pass and stops — no infinite
    loop, no crash.
    """
    monkeypatch.setattr(lifecycle, "pick_next_bead", lambda _jc: None)
    monkeypatch.setattr(
        lifecycle_close,
        "check_gates",
        lambda: {"checked": 1, "resolved": 1, "escalated": 0, "errors": 0},
    )
    calls = _stub_activation_surface(monkeypatch)

    result = lifecycle.activate_next_bead(None)

    assert result is None
    assert calls["rollup"] == [True], "drain pass runs when re-probe is empty"
    assert state.get_state().active is False


def test_gate_check_failure_soft_fails_and_drains(monkeypatch):
    """Criterion 3: a gate-check failure warns and never halts the loop.

    ``check_gates`` raising BeadsError must NOT propagate — the probe
    swallows it, the drain pass runs, and the chain stops gracefully.
    """
    monkeypatch.setattr(lifecycle, "pick_next_bead", lambda _jc: None)

    def boom():
        raise BeadsError("bd gate check exploded")

    monkeypatch.setattr(lifecycle_close, "check_gates", boom)
    calls = _stub_activation_surface(monkeypatch)

    # The whole point: no exception escapes.
    result = lifecycle.activate_next_bead(None)

    assert result is None
    assert calls["rollup"] == [True], "soft-fail still drains normally"
    assert state.get_state().active is False


def test_probe_returns_true_only_when_resolved_positive(monkeypatch):
    """Unit-level: probe_resolved_gates maps resolved>0 -> True."""
    monkeypatch.setattr(
        lifecycle_close, "check_gates", lambda: {"resolved": 2, "escalated": 0}
    )
    assert lifecycle.probe_resolved_gates() is True

    monkeypatch.setattr(
        lifecycle_close, "check_gates", lambda: {"resolved": 0, "escalated": 3}
    )
    assert lifecycle.probe_resolved_gates() is False


def test_probe_soft_fails_to_false_on_error(monkeypatch):
    """Unit-level: a BeadsError yields False, never raises."""

    def boom():
        raise BeadsError("nope")

    monkeypatch.setattr(lifecycle_close, "check_gates", boom)
    assert lifecycle.probe_resolved_gates() is False
