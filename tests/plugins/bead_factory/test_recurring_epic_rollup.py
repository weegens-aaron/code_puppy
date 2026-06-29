"""Regression tests for recurring (patrol) molecule rollup protection.

Coverage-audit gap formulas#2 (bead_chain-wot): a poured ``patrol``
molecule is a *recurring* monitor. When its current children close, its
epic becomes eligible for ``bd epic close-eligible`` — but auto-closing
it defeats the recurrence. ``bd epic close-eligible`` has no exclude
flag, so :func:`beads.close_eligible_epics` now **previews** the eligible
set with ``--dry-run`` first and refuses to close any epic
:func:`beads.is_recurring_epic` flags (via ``mol-type`` field or a
recurring label), closing only the safe ones individually.

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone: ``python3 -m pytest tests/`` or
``python3 tests/test_recurring_epic_rollup.py``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


# ---------------------------------------------------------------------------
# is_recurring_epic — the detection predicate
# ---------------------------------------------------------------------------


def test_label_marks_recurring_epic():
    """An epic carrying a recurring label is protected (case-insensitive)."""
    for label in ("patrol", "PATROL", "mol-type:patrol", "recurring"):
        assert beads.is_recurring_epic({"id": "e", "labels": [label]}) is True, label


def test_mol_type_field_marks_recurring_epic():
    """A future bd surfacing mol_type=patrol is honoured (top-level + metadata)."""
    assert beads.is_recurring_epic({"id": "e", "mol_type": "patrol"}) is True
    assert beads.is_recurring_epic({"id": "e", "mol-type": "PATROL"}) is True
    assert (
        beads.is_recurring_epic({"id": "e", "metadata": {"mol_type": "patrol"}}) is True
    )


def test_ordinary_epic_is_not_recurring():
    """No marker → rolls up as before (we only *withhold* on a positive marker)."""
    assert (
        beads.is_recurring_epic({"id": "e", "labels": ["audit", "code-health"]})
        is False
    )
    assert beads.is_recurring_epic({"id": "e"}) is False
    assert beads.is_recurring_epic({"id": "e", "labels": []}) is False


def test_recurring_epic_handles_bad_input():
    """None / non-dict / weird shapes never raise; default to not-recurring."""
    assert beads.is_recurring_epic(None) is False
    assert beads.is_recurring_epic("nope") is False  # type: ignore[arg-type]
    assert (
        beads.is_recurring_epic({"id": "e", "labels": "patrol"}) is False
    )  # str, not list


# ---------------------------------------------------------------------------
# close_eligible_epics — the partition behaviour
# ---------------------------------------------------------------------------


class _BdRecorder:
    """Arg-aware ``_run_bd`` stub: dry-run returns a fixed preview, real
    close-eligible returns its own payload, and every ``close`` is logged
    so a test can assert *which* epics got closed individually."""

    def __init__(self, dry_run_payload: str, bulk_payload: str = '{"closed": []}'):
        self.dry_run_payload = dry_run_payload
        self.bulk_payload = bulk_payload
        self.closed_ids: list[str] = []
        self.bulk_called = False

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        if args[:2] == ("epic", "close-eligible"):
            if "--dry-run" in args:
                return self.dry_run_payload
            self.bulk_called = True
            return self.bulk_payload
        if args and args[0] == "close":
            self.closed_ids.append(args[1])
            return ""
        return ""


def _install(rec: _BdRecorder):
    beads._run_bd = rec  # type: ignore[assignment]


def test_no_recurring_uses_bulk_cascade():
    """No patrol epic eligible → fast path: bd's native bulk close runs."""
    rec = _BdRecorder(
        dry_run_payload='[{"epic": {"id": "e1", "labels": ["x"]}}]',
        bulk_payload='{"closed": ["e1"], "count": 1}',
    )
    _install(rec)
    result = beads.close_eligible_epics()
    assert rec.bulk_called is True, (
        "should use the bulk cascade when nothing is protected"
    )
    assert rec.closed_ids == [], "must not close epics individually on the fast path"
    assert [e["id"] for e in result] == ["e1"]


def test_recurring_epic_is_skipped_others_closed_individually():
    """Patrol epic eligible → bulk is bypassed; only the safe epic closes."""
    rec = _BdRecorder(
        dry_run_payload=(
            '[{"epic": {"id": "patrol-1", "labels": ["patrol"]}},'
            ' {"epic": {"id": "work-1", "labels": ["feature"]}}]'
        )
    )
    _install(rec)
    result = beads.close_eligible_epics()
    assert rec.bulk_called is False, (
        "must NOT run the bulk cascade when a patrol epic is eligible"
    )
    assert rec.closed_ids == ["work-1"], "only the non-recurring epic should be closed"
    assert [e["id"] for e in result] == ["work-1"]
    assert all(e["id"] != "patrol-1" for e in result), (
        "patrol epic must not be reported closed"
    )


def test_recurring_close_failure_is_soft():
    """A per-epic close failure doesn't strand the rest of the rollup."""
    rec = _BdRecorder(
        dry_run_payload=(
            '[{"epic": {"id": "patrol-1", "labels": ["patrol"]}},'
            ' {"epic": {"id": "boom"}}, {"epic": {"id": "ok"}}]'
        )
    )

    real_call = rec.__call__

    def flaky(*args, **kwargs):  # noqa: ANN002, ANN003
        if args and args[0] == "close" and args[1] == "boom":
            raise beads.BeadsError("simulated close failure")
        return real_call(*args, **kwargs)

    beads._run_bd = flaky  # type: ignore[assignment]
    result = beads.close_eligible_epics()
    ids = [e["id"] for e in result]
    assert "ok" in ids, "a sibling close failure must not block the others"
    assert "boom" not in ids, "the failed close must not be reported as closed"
    assert "patrol-1" not in ids, "patrol epic stays protected"


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
