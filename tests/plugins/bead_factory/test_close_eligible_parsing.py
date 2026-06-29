"""Regression tests for epic-rollup parsing (bdboard-rzxb).

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone:  ``python3 -m pytest tests/`` or ``python3 tests/test_close_eligible_parsing.py``.

The bug: bd 1.0.4's ``epic close-eligible --json`` emits
``{"closed": ["id", ...], "count": N}`` — a list of bare **string ids**.
The old parser filtered with ``isinstance(item, dict)``, dropping every
string, so rollups closed epics *silently* (empty return → no log line).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


def _patch_run_bd(monkeypatch_value: str):
    """Replace beads._run_bd with a stub returning a fixed payload."""
    beads._run_bd = lambda *a, **k: monkeypatch_value  # type: ignore[assignment]


def test_bd_104_string_id_shape_is_parsed():
    """bd 1.0.4: {"closed": ["abc-1"], "count": 1} -> [{"id": "abc-1"}]."""
    _patch_run_bd('{"closed": ["abc-1", "abc-2"], "count": 2, "schema_version": 1}')
    result = beads.close_eligible_epics()
    assert [e["id"] for e in result] == ["abc-1", "abc-2"], result


def test_bare_list_of_dicts_shape():
    """Older bd: a bare top-level list of epic dicts."""
    _patch_run_bd('[{"id": "x-1", "title": "Epic One"}, {"id": "x-2"}]')
    result = beads.close_eligible_epics()
    assert [e["id"] for e in result] == ["x-1", "x-2"], result
    assert result[0]["title"] == "Epic One"


def test_enveloped_epic_shape_is_unwrapped():
    """Some shapes wrap each closed epic as {"epic": {...}}."""
    _patch_run_bd('{"closed": [{"epic": {"id": "y-1", "title": "Wrapped"}}]}')
    result = beads.close_eligible_epics()
    assert result == [{"id": "y-1", "title": "Wrapped"}], result


def test_empty_and_nonjson_are_silent_noops():
    """No eligible epics / unparseable output -> [] (no crash, no log spam)."""
    _patch_run_bd("")
    assert beads.close_eligible_epics() == []
    _patch_run_bd("not json at all")
    assert beads.close_eligible_epics() == []
    _patch_run_bd('{"closed": [], "count": 0}')
    assert beads.close_eligible_epics() == []


def test_blank_string_ids_are_dropped():
    """Defensive: empty/whitespace ids never produce a phantom epic dict."""
    _patch_run_bd('{"closed": ["", "  ", "real-1"]}')
    result = beads.close_eligible_epics()
    assert [e["id"] for e in result] == ["real-1"], result


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
