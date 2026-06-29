"""Unit tests for `bd gate check` summary parsing (bead_chain-x3g / FB-3).

bead-chain probes resolvable gates on an empty queue via
:func:`beads.check_gates`. The parser has to survive a real bd 1.0.x
quirk: ``bd gate check --json`` prints a human-readable summary line
*before* the JSON object even under ``--json``::

    Checked 3 gates: 1 resolved, 0 escalated, 0 errors
    {"checked": 3, "resolved": 1, "escalated": 0, "errors": 0, ...}

So a naive ``json.loads(stdout)`` blows up. These tests pin the
slice-from-first-brace behaviour and the all-zero soft-degrade paths.

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone: ``python3 -m pytest tests/`` or
``python3 tests/test_gate_check_parsing.py``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


def _patch_run_bd(payload: str):
    """Replace beads._run_bd with a stub returning a fixed payload."""
    beads._run_bd = lambda *a, **k: payload  # type: ignore[assignment]


# The real bd 1.0.x shape: leading human line, blank line, then JSON.
_BD_104_RESOLVED = (
    "\nChecked 3 gates: 1 resolved, 0 escalated, 0 errors\n"
    '{\n  "checked": 3,\n  "dry_run": false,\n  "errors": 0,\n'
    '  "escalated": 0,\n  "resolved": 1,\n  "schema_version": 1\n}\n'
)

_BD_104_EMPTY = (
    "\nChecked 0 gates: 0 resolved, 0 escalated, 0 errors\n"
    '{\n  "checked": 0,\n  "dry_run": false,\n  "errors": 0,\n'
    '  "escalated": 0,\n  "resolved": 0,\n  "schema_version": 1\n}\n'
)


def test_leading_human_line_is_skipped_and_counts_parsed():
    """The real bd quirk: human line before JSON must not break parsing."""
    _patch_run_bd(_BD_104_RESOLVED)
    counts = beads.check_gates()
    assert counts["resolved"] == 1, counts
    assert counts["checked"] == 3, counts
    assert counts["escalated"] == 0, counts
    assert counts["errors"] == 0, counts


def test_zero_resolved_empty_queue_shape():
    """No gates resolved -> resolved count is 0 (caller will not re-probe)."""
    _patch_run_bd(_BD_104_EMPTY)
    counts = beads.check_gates()
    assert counts["resolved"] == 0, counts
    assert counts["checked"] == 0, counts


def test_pure_json_without_leading_line():
    """Future bd that drops the human line still parses cleanly."""
    _patch_run_bd('{"checked": 2, "resolved": 2, "escalated": 0, "errors": 0}')
    counts = beads.check_gates()
    assert counts == {"checked": 2, "resolved": 2, "escalated": 0, "errors": 0}


def test_escalated_count_is_surfaced():
    """Escalated (expired/failed) gates are reported alongside resolved."""
    _patch_run_bd(
        "Checked 4 gates: 1 resolved, 2 escalated, 0 errors\n"
        '{"checked": 4, "resolved": 1, "escalated": 2, "errors": 0}'
    )
    counts = beads.check_gates()
    assert counts["resolved"] == 1, counts
    assert counts["escalated"] == 2, counts


def test_missing_keys_default_to_zero():
    """A partial payload must not KeyError — absent keys read as 0."""
    _patch_run_bd('{"checked": 5}')
    counts = beads.check_gates()
    assert counts == {"checked": 5, "resolved": 0, "escalated": 0, "errors": 0}


def test_nonint_values_degrade_to_zero():
    """Defensive: a stringy count never leaks a non-int into the caller."""
    _patch_run_bd('{"checked": 1, "resolved": "lots", "escalated": null}')
    counts = beads.check_gates()
    assert counts["resolved"] == 0, counts
    assert counts["escalated"] == 0, counts


def test_non_json_degrades_to_all_zero():
    """Unparseable-but-successful output -> all zeros, never raises."""
    _patch_run_bd("not json at all")
    assert beads.check_gates() == {
        "checked": 0,
        "resolved": 0,
        "escalated": 0,
        "errors": 0,
    }


def test_empty_output_degrades_to_all_zero():
    """Blank stdout -> all zeros (no first brace to slice)."""
    _patch_run_bd("")
    assert beads.check_gates()["resolved"] == 0


def test_non_dict_json_degrades_to_all_zero():
    """A JSON list (not the expected object) -> all zeros, no crash."""
    _patch_run_bd("[1, 2, 3]")
    assert beads.check_gates() == {
        "checked": 0,
        "resolved": 0,
        "escalated": 0,
        "errors": 0,
    }


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
