"""Regression test for bead_chain-221: per-test isolation of beads globals.

Several legacy test modules stub ``beads._run_bd`` / ``beads._parse_json_list``
by direct attribute assignment and never restore them. The autouse
``_restore_beads_module_globals`` fixture in ``conftest.py`` snapshots and
restores those globals around every test, so a stub set by one test can never
leak into the next. These two tests prove that: the first deliberately
pollutes the globals, the second asserts the real implementations are back.

``beads.py`` is pure-stdlib, so this runs standalone under
``python3 -m pytest tests/``.

## Steps to Reproduce
1. In test A: ``beads._run_bd = lambda *a, **k: "stubbed"`` (never restored).
2. In test B (same or a later module): call the *real* ``beads._run_bd``.
3. Before the fix, B saw the leftover lambda and failed in the full suite
   while passing in isolation. After the fix, the autouse guard restores
   the real function between tests.

## Acceptance Criteria
- A test that clobbers ``beads._run_bd`` / ``beads._parse_json_list`` does
  not leak its stub into the following test.
- The real implementations are restored after each test, so the full suite
  matches isolated runs.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


def test_clobbering_run_bd_globals_is_contained():
    """Pollute the globals the way legacy modules do; the guard cleans up."""
    beads._run_bd = lambda *a, **k: "stubbed"  # type: ignore[assignment]
    beads._parse_json_list = lambda *a, **k: ["stub"]  # type: ignore[assignment]
    assert beads._run_bd() == "stubbed"
    assert beads._parse_json_list() == ["stub"]


def test_globals_restored_after_clobbering_test():
    """The autouse fixture must have restored the real implementations."""
    assert beads._run_bd.__name__ == "_run_bd", beads._run_bd
    assert beads._parse_json_list.__name__ == "_parse_json_list", beads._parse_json_list
