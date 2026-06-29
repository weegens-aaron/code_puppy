"""Unit tests for `bd lint <id>` warning parsing (bead_chain-vmo / FB-5).

bead-chain consults the template contract on the claim path via
:func:`beads.lint_warnings`. ``bd lint --json`` emits::

    {
      "total": 1,
      "issues": 1,
      "results": [
        {"id": "x", "title": "...", "type": "task",
         "missing": ["## Acceptance Criteria"], "warnings": 1}
      ]
    }

and ``results`` is ``null`` for a clean bead. These tests pin the
slice-from-first-brace parse, the id-filtering, and the empty/soft-
degrade paths.

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone: ``python3 -m pytest tests/`` or
``python3 tests/test_lint_warnings_parsing.py``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


def _patch_run_bd(payload: str):
    """Replace beads._run_bd with a stub returning a fixed payload."""
    beads._run_bd = lambda *a, **k: payload  # type: ignore[assignment]


# The real bd 1.0.x clean shape: results is null.
_BD_CLEAN = '{\n  "total": 0,\n  "issues": 0,\n  "results": null\n}\n'

# A task missing its Acceptance Criteria section.
_BD_TASK_MISSING_AC = (
    '{\n  "total": 1,\n  "issues": 1,\n  "results": [\n'
    '    {\n      "id": "demo-1",\n      "title": "Do the thing",\n'
    '      "type": "task",\n      "missing": [\n'
    '        "## Acceptance Criteria"\n      ],\n      "warnings": 1\n'
    "    }\n  ]\n}\n"
)


def test_clean_bead_returns_empty_list():
    """results: null -> no warnings."""
    _patch_run_bd(_BD_CLEAN)
    assert beads.lint_warnings("demo-1") == []


def test_missing_section_is_surfaced():
    _patch_run_bd(_BD_TASK_MISSING_AC)
    assert beads.lint_warnings("demo-1") == ["## Acceptance Criteria"]


def test_multiple_missing_sections_preserved_in_order():
    _patch_run_bd(
        '{"results": [{"id": "b", "missing": '
        '["## Steps to Reproduce", "## Acceptance Criteria"]}]}'
    )
    assert beads.lint_warnings("b") == [
        "## Steps to Reproduce",
        "## Acceptance Criteria",
    ]


def test_only_matching_id_is_returned():
    """A result for another bead must never be attributed to this one."""
    _patch_run_bd(
        '{"results": ['
        '{"id": "other", "missing": ["## Success Criteria"]},'
        '{"id": "mine", "missing": ["## Acceptance Criteria"]}'
        "]}"
    )
    assert beads.lint_warnings("mine") == ["## Acceptance Criteria"]


def test_no_matching_id_returns_empty():
    _patch_run_bd('{"results": [{"id": "other", "missing": ["## X"]}]}')
    assert beads.lint_warnings("mine") == []


def test_leading_human_line_is_skipped():
    """Defensive: a human line before the JSON must not break parsing."""
    _patch_run_bd(
        "Linted 1 issue: 1 warning\n"
        '{"results": [{"id": "b", "missing": ["## Acceptance Criteria"]}]}'
    )
    assert beads.lint_warnings("b") == ["## Acceptance Criteria"]


def test_blank_and_whitespace_sections_dropped():
    _patch_run_bd('{"results": [{"id": "b", "missing": ["", "   ", "## Real"]}]}')
    assert beads.lint_warnings("b") == ["## Real"]


def test_missing_field_absent_returns_empty():
    _patch_run_bd('{"results": [{"id": "b"}]}')
    assert beads.lint_warnings("b") == []


def test_missing_field_null_returns_empty():
    _patch_run_bd('{"results": [{"id": "b", "missing": null}]}')
    assert beads.lint_warnings("b") == []


def test_non_json_degrades_to_empty():
    _patch_run_bd("not json at all")
    assert beads.lint_warnings("b") == []


def test_empty_output_degrades_to_empty():
    _patch_run_bd("")
    assert beads.lint_warnings("b") == []


def test_non_dict_json_degrades_to_empty():
    _patch_run_bd("[1, 2, 3]")
    assert beads.lint_warnings("b") == []


def test_non_list_results_degrades_to_empty():
    _patch_run_bd('{"results": {"id": "b"}}')
    assert beads.lint_warnings("b") == []


def test_non_dict_result_item_skipped():
    _patch_run_bd('{"results": ["junk", {"id": "b", "missing": ["## X"]}]}')
    assert beads.lint_warnings("b") == ["## X"]


def test_infra_failure_raises_beads_error():
    """Unlike parse-junk (soft []), an infra failure raises so the prompt
    layer can decide to soft-fail. Mirrors check_gates' contract."""

    def _boom(*a, **k):
        raise beads.BeadsError("bd not found")

    beads._run_bd = _boom  # type: ignore[assignment]
    try:
        beads.lint_warnings("b")
    except beads.BeadsError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected BeadsError to propagate")


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
