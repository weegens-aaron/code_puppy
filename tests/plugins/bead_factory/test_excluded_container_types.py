"""Regression tests: container/handle types are excluded (bead_chain-cb5).

FB-1. ``EXCLUDED_TYPES`` originally held only ``("epic",)``. The other
bd container / handle types — ``milestone`` (anatomy#4), ``gate``
(gates#2) and ``molecule`` (swarms#1) — could surface as ready leaves
on ``bd ready``. ``next_ready()`` would then hand one to /goal as if it
were code work; the container has nothing to *do*, so ``close_guard``
refuses the close and the whole chain stalls.

The fix is a one-line edit to the :data:`beads.EXCLUDED_TYPES` tuple.
These tests pin both halves of the double filter the constant feeds:

  * **server-side** — :func:`beads._exclude_type_arg` builds the
    ``--exclude-type=...`` CLI arg, so every ``bd ready`` / ``bd list``
    query asks bd to drop the type up front; and
  * **client-side** — :func:`beads.is_excluded_type` re-filters the
    returned payload as defence-in-depth (the server flag has been
    observed to leak in the wild).

They mirror the existing epic-exclusion expectations for each new type.

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone:  ``python3 -m pytest tests/`` or
``python3 tests/test_excluded_container_types.py``.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402

# The container / handle types this bead added to the exclusion set, plus
# the original epic. Parametrising on the constant keeps these tests in
# lock-step with future one-line edits to EXCLUDED_TYPES. DRY.
CONTAINER_TYPES = ("epic", "milestone", "gate", "molecule")


def _patch_run_bd(payload: str):
    """Replace beads._run_bd with a stub returning a fixed payload."""
    beads._run_bd = lambda *a, **k: payload  # type: ignore[assignment]


def _capture_run_bd():
    """Replace beads._run_bd with a stub recording the args it was called with.

    Returns the (mutable) list that captures positional args per call, so a
    test can assert on the exact CLI it would have run.
    """
    calls: list[tuple] = []

    def _stub(*args, **kwargs):
        calls.append(args)
        return "[]"

    beads._run_bd = _stub  # type: ignore[assignment]
    return calls


# --------------------------------------------------------------------------
# The constant itself
# --------------------------------------------------------------------------


def test_excluded_types_cover_all_container_handles():
    """Acceptance: EXCLUDED_TYPES covers epic, milestone, gate, molecule."""
    for t in CONTAINER_TYPES:
        assert t in beads.EXCLUDED_TYPES, f"{t!r} missing from EXCLUDED_TYPES"


# --------------------------------------------------------------------------
# Client-side filter: is_excluded_type
# --------------------------------------------------------------------------


def test_is_excluded_type_rejects_each_container_type():
    """Each container type is flagged excluded, case-insensitively."""
    for t in CONTAINER_TYPES:
        assert beads.is_excluded_type({"id": "x", "issue_type": t}) is True, t
        # Case-insensitive: an upstream bd emitting Title-case must not leak.
        assert beads.is_excluded_type({"id": "x", "issue_type": t.upper()}) is True, t


def test_is_excluded_type_allows_real_work_types():
    """Sanity: genuine leaf work types are never excluded."""
    for t in ("task", "bug", "feature", "chore", "spike"):
        assert beads.is_excluded_type({"id": "x", "issue_type": t}) is False, t


# ------------------------------------------------------------------
# Server-side filter: _exclude_type_arg
# --------------------------------------------------------------------------


def test_exclude_type_arg_lists_every_container_type():
    """The --exclude-type CLI arg names all excluded types."""
    arg = beads._exclude_type_arg()
    assert arg.startswith("--exclude-type=")
    names = set(arg.split("=", 1)[1].split(","))
    for t in CONTAINER_TYPES:
        assert t in names, f"{t!r} not in {arg!r}"


# --------------------------------------------------------------------------
# next_ready() — both filters working together
# --------------------------------------------------------------------------


def test_next_ready_passes_exclude_type_to_bd():
    """next_ready asks bd to drop every container type server-side."""
    calls = _capture_run_bd()
    beads.next_ready()
    assert calls, "next_ready should have invoked _run_bd"
    assert beads._exclude_type_arg() in calls[0], calls[0]


def test_next_ready_drops_leaked_container_bead():
    """If bd leaks a container bead, next_ready re-filters it out."""
    for t in CONTAINER_TYPES:
        # A single leaked container bead — bd's server-side flag failed open.
        _patch_run_bd(json.dumps([{"id": "leak", "issue_type": t}]))
        assert beads.next_ready() is None, f"{t} bead leaked through next_ready"


def test_next_ready_returns_first_real_leaf_past_leaked_container():
    """A leaked container is skipped; the first real leaf is returned."""
    payload = json.dumps(
        [
            {"id": "milestone-1", "issue_type": "milestone"},
            {"id": "task-1", "issue_type": "task"},
        ]
    )
    _patch_run_bd(payload)
    picked = beads.next_ready()
    assert picked is not None and picked["id"] == "task-1", picked


# --------------------------------------------------------------------------
# next_ready_in_epic() — same double filter under a parent
# --------------------------------------------------------------------------


def test_next_ready_in_epic_drops_leaked_container_bead():
    """Per-epic ready query also re-filters leaked container beads."""
    for t in CONTAINER_TYPES:
        _patch_run_bd(json.dumps([{"id": "leak", "issue_type": t}]))
        assert beads.next_ready_in_epic("epic-1") is None, t


# --------------------------------------------------------------------------
# list_in_progress() — recovery tier must not resurrect a container bead
# --------------------------------------------------------------------------


def test_list_in_progress_drops_leaked_container_bead():
    """Stranded-work recovery never returns a leaked container bead."""
    for t in CONTAINER_TYPES:
        _patch_run_bd(json.dumps([{"id": "leak", "issue_type": t}]))
        assert beads.list_in_progress() == [], t


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
