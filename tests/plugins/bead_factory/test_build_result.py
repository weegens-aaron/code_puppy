"""Unit tests for ``bead_factory.build_result`` (bead-factory-ush).

Pure-logic coverage for the :class:`BuildResult` dataclass and the
:func:`build_result` builder: tally derivation (no double-counting abstainers),
the ``completed == COMPLETE`` invariant, ``total == passed+failed+abstained``,
aggregated-notes parity with the live ``build_loop`` formatter, frozenness, and
the consume-once sink semantics.
"""

from __future__ import annotations

import pytest

from code_puppy.plugins.bead_factory import build_result as br
from code_puppy.plugins.bead_factory.build_loop import _format_remediation_block
from code_puppy.plugins.bead_factory.build_result import (
    BuildResult,
    StopReason,
    build_result,
)
from code_puppy.plugins.bead_factory.inspector import BuildInspection


def _verdict(name, *, complete=True, abstained=False, notes=""):
    return BuildInspection(
        inspector_name=name,
        complete=complete,
        notes=notes,
        raw_response="",
        abstained=abstained,
    )


def test_tally_excludes_abstainers_from_vote():
    verdicts = [
        _verdict("a", complete=True),
        _verdict("b", complete=False),
        _verdict("c", abstained=True),
        _verdict("d", abstained=True, complete=True),  # complete ignored when abstained
    ]
    r = build_result(verdicts, StopReason.MAX_ITERATIONS)
    assert r.total == 4
    assert r.passed == 1
    assert r.failed == 1
    assert r.abstained == 2
    # No double-counting: an abstainer is neither a pass nor a fail.
    assert r.total == r.passed + r.failed + r.abstained


def test_completed_iff_stop_reason_complete():
    verdicts = [_verdict("a", complete=True)]
    assert build_result(verdicts, StopReason.COMPLETE).completed is True
    for reason in (
        StopReason.MAX_ITERATIONS,
        StopReason.CANCELLED,
        StopReason.NO_PROMPT,
    ):
        assert build_result(verdicts, reason).completed is False


def test_aggregated_notes_match_live_formatter():
    verdicts = [
        _verdict("a", complete=True, notes="looks good"),
        _verdict("b", complete=False, notes="missing tests\nfix the thing"),
        _verdict("c", abstained=True, notes="endpoint 404"),
    ]
    r = build_result(verdicts, StopReason.MAX_ITERATIONS)
    assert r.aggregated_notes == _format_remediation_block(verdicts)


def test_empty_verdicts_zero_tally():
    r = build_result([], StopReason.NO_PROMPT)
    assert (r.total, r.passed, r.failed, r.abstained) == (0, 0, 0, 0)
    assert r.aggregated_notes == ""
    assert r.completed is False


def test_loop_count_and_bead_id_passthrough():
    r = build_result(
        [_verdict("a")],
        StopReason.COMPLETE,
        loop_count=7,
        bead_id="bead-factory-xyz",
    )
    assert r.loop_count == 7
    assert r.bead_id == "bead-factory-xyz"


def test_verdicts_stored_as_tuple():
    verdicts = [_verdict("a"), _verdict("b", complete=False)]
    r = build_result(verdicts, StopReason.COMPLETE)
    assert isinstance(r.verdicts, tuple)
    assert r.verdicts == tuple(verdicts)


def test_result_is_frozen():
    r = build_result([_verdict("a")], StopReason.COMPLETE)
    with pytest.raises(Exception):
        r.passed = 99  # type: ignore[misc]


def test_stop_reason_is_str_enum():
    assert StopReason.CANCELLED == "cancelled"
    assert StopReason.COMPLETE.value == "complete"


def test_build_result_dataclass_field_set():
    # Lock the agreed field set so a drift is loud.
    expected = {
        "completed",
        "stop_reason",
        "total",
        "passed",
        "failed",
        "abstained",
        "verdicts",
        "aggregated_notes",
        "loop_count",
        "bead_id",
    }
    assert {f.name for f in BuildResult.__dataclass_fields__.values()} == expected


# --- consume-once sink ------------------------------------------------------


def test_sink_consume_once():
    br.clear()
    assert br.take_last() is None
    r = build_result([_verdict("a")], StopReason.COMPLETE)
    br.set_last(r)
    assert br.peek_last() is r  # non-destructive
    assert br.peek_last() is r
    assert br.take_last() is r  # pops + clears
    assert br.take_last() is None
    br.clear()


def test_sink_overwrites_prior():
    br.clear()
    first = build_result([_verdict("a")], StopReason.COMPLETE)
    second = build_result([_verdict("b", complete=False)], StopReason.MAX_ITERATIONS)
    br.set_last(first)
    br.set_last(second)
    assert br.take_last() is second
    br.clear()
