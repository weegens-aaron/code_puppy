"""Unit tests for the work-time blocker gate (bdboard-oals).

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone:
    ``python3 -m pytest tests/test_blocker_gate.py``
    ``python3 tests/test_blocker_gate.py``

The bug: bead-chain claimed and fully executed a bead whose ``blocks``
dependencies were still open, only tripping at ``bd close`` ("blocked
by open issues"). The fix gives the chain a claim-time blocker check so
a bead with open ``DEPENDS ON`` edges is never driven.

These tests pin :func:`beads.open_blocker_ids` against the real
``bd show --json`` dependency shape (full dep dicts carrying ``status``
and ``dependency_type``).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


def _patch_show(bead: dict | None):
    """Stub beads.show() to return a fixed bead record."""
    beads.show = lambda _id: bead  # type: ignore[assignment]


def _dep(dep_id: str, status: str, dep_type: str = "blocks") -> dict:
    """Build a `bd show`-shaped inbound dependency entry."""
    return {"id": dep_id, "status": status, "dependency_type": dep_type}


def test_open_blocks_dep_is_reported():
    """An open `blocks` dependency gates work -> id surfaces."""
    _patch_show({"id": "x", "dependencies": [_dep("a-1", "open")]})
    assert beads.open_blocker_ids("x") == ["a-1"]
    assert beads.is_blocked("x") is True


def test_in_progress_blocker_still_gates():
    """A blocker that is in_progress (not closed) still gates."""
    _patch_show({"id": "x", "dependencies": [_dep("a-1", "in_progress")]})
    assert beads.open_blocker_ids("x") == ["a-1"]


def test_closed_blocker_is_satisfied():
    """A closed `blocks` dependency no longer gates -> unblocked."""
    _patch_show({"id": "x", "dependencies": [_dep("a-1", "closed")]})
    assert beads.open_blocker_ids("x") == []
    assert beads.is_blocked("x") is False


def test_parent_child_edge_is_not_a_blocker():
    """parent-child (the parent epic) must NOT count as a work-time blocker."""
    _patch_show({"id": "x", "dependencies": [_dep("epic-1", "open", "parent-child")]})
    assert beads.open_blocker_ids("x") == []


def test_non_blocking_edge_types_ignored():
    """discovered-from / related edges never gate work."""
    _patch_show(
        {
            "id": "x",
            "dependencies": [
                _dep("a-1", "open", "discovered-from"),
                _dep("a-2", "open", "related"),
            ],
        }
    )
    assert beads.open_blocker_ids("x") == []


def test_mixed_deps_returns_only_open_blocks():
    """Real-world mix (the bdboard-h4eq shape): only open `blocks` count."""
    _patch_show(
        {
            "id": "h4eq",
            "dependencies": [
                _dep("30vz", "open"),  # open blocker -> counts
                _dep("ilmb", "closed"),  # satisfied -> dropped
                _dep("irrn", "closed"),  # satisfied -> dropped
                _dep("uiwu", "open", "parent-child"),  # parent epic -> dropped
            ],
        }
    )
    assert beads.open_blocker_ids("h4eq") == ["30vz"]
    assert beads.is_blocked("h4eq") is True


def test_case_insensitive_type_and_status():
    """bd version drift: 'Blocks' / 'OPEN' casing must not leak past the gate."""
    _patch_show({"id": "x", "dependencies": [_dep("a-1", "OPEN", "Blocks")]})
    assert beads.open_blocker_ids("x") == ["a-1"]


def test_no_deps_is_unblocked():
    """Bead with no dependencies array / empty list is ready."""
    _patch_show({"id": "x", "dependencies": []})
    assert beads.open_blocker_ids("x") == []
    _patch_show({"id": "x"})
    assert beads.open_blocker_ids("x") == []


def test_empty_id_and_missing_bead_softfail():
    """Empty id or a vanished bead -> [] (not-blocked), never a crash."""
    assert beads.open_blocker_ids("") == []
    _patch_show(None)
    assert beads.open_blocker_ids("x") == []


def test_bd_error_softfails_to_unblocked():
    """A transient bd failure must NOT strand the chain -> treat as unblocked.

    The close-time guard remains the final safety net; a blip here
    should degrade gracefully, not halt.
    """

    def _boom(_id):
        raise beads.BeadsError("transient bd timeout")

    beads.show = _boom  # type: ignore[assignment]
    assert beads.open_blocker_ids("x") == []
    assert beads.is_blocked("x") is False


def test_blank_blocker_ids_are_dropped():
    """Defensive: a dep with an empty id never produces a phantom blocker."""
    _patch_show({"id": "x", "dependencies": [_dep("", "open"), _dep("real-1", "open")]})
    assert beads.open_blocker_ids("x") == ["real-1"]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
