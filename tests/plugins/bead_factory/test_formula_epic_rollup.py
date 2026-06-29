"""Regression tests for formula-derived epic rollup (bead_chain-0kx).

The reported bug suspected that three-segment *formula* epic ids
(``bdboard-mol-isk``) were not matched by rollup logic that assumed
two-segment ids (``bdboard-isk``), so formula epics never rolled up.

Investigation found the bead-chain plugin does **no** id-segment
parsing at all — rollup is fully delegated to ``bd epic
close-eligible`` (see :func:`beads.close_eligible_epics`). bd 1.0.4
rolls up two- and three-segment ids identically. These tests lock that
in at the parser layer so any future regression that *did* start
treating ids structurally (e.g. a misguided ``id.split("-")`` filter)
would fail loudly here.

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone: ``python3 -m pytest tests/`` or
``python3 tests/test_formula_epic_rollup.py``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402


def _patch_run_bd(payload: str):
    """Replace beads._run_bd with a stub returning a fixed payload."""
    beads._run_bd = lambda *a, **k: payload  # type: ignore[assignment]


# A mix of two-segment (standard) and three-segment (formula) ids in
# every shape bd is known to emit. The parser must treat them
# identically: id structure is opaque to bead-chain.


def test_formula_id_string_shape_bd_104():
    """bd 1.0.4 {"closed": [ids]} must keep three-segment formula ids."""
    _patch_run_bd(
        '{"closed": ["bdboard-isk", "bdboard-mol-isk"], '
        '"count": 2, "schema_version": 1}'
    )
    result = beads.close_eligible_epics()
    assert [e["id"] for e in result] == ["bdboard-isk", "bdboard-mol-isk"], result


def test_formula_id_bare_list_of_dicts():
    """Older bd: bare list of dicts; formula id survives untouched."""
    _patch_run_bd(
        '[{"id": "bdboard-isk", "title": "Standard"}, '
        '{"id": "bdboard-mol-isk", "title": "Formula"}]'
    )
    result = beads.close_eligible_epics()
    assert [e["id"] for e in result] == ["bdboard-isk", "bdboard-mol-isk"], result
    assert result[1]["title"] == "Formula"


def test_formula_id_enveloped_shape():
    """{"epic": {...}} envelope unwraps with a three-segment id intact."""
    _patch_run_bd('{"closed": [{"epic": {"id": "bdboard-mol-isk", "title": "F"}}]}')
    result = beads.close_eligible_epics()
    assert result == [{"id": "bdboard-mol-isk", "title": "F"}], result


def test_multi_segment_ids_not_truncated():
    """Even longer multi-segment ids (deep formula nesting) pass through.

    Guards against a hypothetical 'normalise to first two segments'
    regression: the full id must be preserved verbatim.
    """
    _patch_run_bd('{"closed": ["a-b-c-d-e", "x-mol-y-z"]}')
    result = beads.close_eligible_epics()
    assert [e["id"] for e in result] == ["a-b-c-d-e", "x-mol-y-z"], result


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
