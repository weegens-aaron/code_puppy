"""Unit tests for the fan-out gate (bead_chain-9sc).

``lifecycle.py`` implements :func:`_has_fan_out_gate_issue` to detect
fan-out gates (beads with ``waits_for: children-of(spawner_id)`` where
the spawner has unclosed children). These tests pin the logic against
the gate-detection model.

**Context:** Beads with ``waits_for: children-of(...)`` are invisible to
both ``bd ready`` and ``bd blocked`` in the beads CLI (upstream bug),
so bead-chain must detect them at claim time and refuse to drive them.

These tests import directly from ``lifecycle.py`` via conftest setup.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import beads
from code_puppy.plugins.bead_factory import fan_out_gate as lifecycle  # noqa: E402


def _patch_show(bead: dict | None):
    """Stub beads.show() to return a fixed bead record."""
    lifecycle.show = lambda _id: bead  # type: ignore[assignment]


def _patch_run_bd_and_parse(issues: list[dict]):
    """Stub beads._run_bd() and beads._parse_json_list() to return a bead list.

    This simulates ``bd list --json`` returning the given issues.
    """
    import json

    raw_json = json.dumps(issues)

    def mock_run_bd(*args, **kwargs):  # noqa: ARG001
        return raw_json

    def mock_parse_json(raw, context):  # noqa: ARG001
        return json.loads(raw)

    beads._run_bd = mock_run_bd  # type: ignore[assignment]
    beads._parse_json_list = mock_parse_json  # type: ignore[assignment]


def test_no_waits_for_field_is_unblocked():
    """A bead with no waits_for field has no gate issue."""
    _patch_show({"id": "x", "status": "open"})
    _patch_run_bd_and_parse([])
    assert lifecycle._has_fan_out_gate_issue("x") is False


def test_waits_for_non_children_of_format_is_unblocked():
    """A bead with waits_for in a different format has no gate issue."""
    _patch_show({"id": "x", "waits_for": "other-format"})
    _patch_run_bd_and_parse([])
    assert lifecycle._has_fan_out_gate_issue("x") is False


def test_waits_for_malformed_children_of_is_unblocked():
    """A malformed waits_for children-of(...) soft-fails to unblocked."""
    # Missing closing paren
    _patch_show({"id": "x", "waits_for": "children-of(spawner_id"})
    _patch_run_bd_and_parse([])
    assert lifecycle._has_fan_out_gate_issue("x") is False

    # Empty spawner id
    _patch_show({"id": "x", "waits_for": "children-of()"})
    _patch_run_bd_and_parse([])
    assert lifecycle._has_fan_out_gate_issue("x") is False


def test_spawner_has_unclosed_children_is_blocked():
    """A bead gated on spawner with unclosed children has a gate issue."""
    _patch_show(
        {
            "id": "finalize",
            "status": "open",
            "waits_for": "children-of(discover)",
        }
    )
    # Spawner exists and has an unclosed child
    _patch_run_bd_and_parse(
        [
            {"id": "discover", "status": "closed"},
            {"id": "discover.1", "parent": "discover", "status": "open"},
        ]
    )
    assert lifecycle._has_fan_out_gate_issue("finalize") is True


def test_spawner_has_multiple_unclosed_children_is_blocked():
    """Multiple unclosed children -> still blocked (any one blocks)."""
    _patch_show(
        {
            "id": "finalize",
            "waits_for": "children-of(discover)",
        }
    )
    _patch_run_bd_and_parse(
        [
            {"id": "discover", "status": "closed"},
            {"id": "discover.1", "parent": "discover", "status": "open"},
            {"id": "discover.2", "parent": "discover", "status": "open"},
            {"id": "discover.3", "parent": "discover", "status": "in_progress"},
        ]
    )
    assert lifecycle._has_fan_out_gate_issue("finalize") is True


def test_all_children_closed_is_unblocked():
    """When all children are closed, gate is satisfied; no gate issue."""
    _patch_show(
        {
            "id": "finalize",
            "waits_for": "children-of(discover)",
        }
    )
    _patch_run_bd_and_parse(
        [
            {"id": "discover", "status": "closed"},
            {"id": "discover.1", "parent": "discover", "status": "closed"},
            {"id": "discover.2", "parent": "discover", "status": "closed"},
        ]
    )
    assert lifecycle._has_fan_out_gate_issue("finalize") is False


def test_no_children_means_gate_satisfied():
    """If spawner exists but has no children, gate is satisfied."""
    _patch_show(
        {
            "id": "finalize",
            "waits_for": "children-of(discover)",
        }
    )
    # Only the spawner, no children listed
    _patch_run_bd_and_parse([{"id": "discover", "status": "closed"}])
    assert lifecycle._has_fan_out_gate_issue("finalize") is False


def test_spawner_does_not_exist_soft_fails():
    """If spawner can't be found, soft-fail to unblocked."""
    _patch_show(
        {
            "id": "finalize",
            "waits_for": "children-of(nonexistent)",
        }
    )

    # Stub show() to raise BeadsError when called with nonexistent
    def mock_show(bead_id):
        if bead_id == "finalize":
            return {"id": "finalize", "waits_for": "children-of(nonexistent)"}
        if bead_id == "nonexistent":
            raise beads.BeadsError("not found")
        return None

    lifecycle.show = mock_show  # type: ignore[assignment]
    _patch_run_bd_and_parse([])

    # Should soft-fail to False (assume gate is satisfied)
    assert lifecycle._has_fan_out_gate_issue("finalize") is False


def test_bead_does_not_exist_soft_fails():
    """If the gated bead itself doesn't exist, soft-fail to unblocked."""
    # Mock show to return None
    lifecycle.show = lambda _id: None  # type: ignore[assignment]
    _patch_run_bd_and_parse([])

    assert lifecycle._has_fan_out_gate_issue("missing") is False


def test_empty_bead_id_soft_fails():
    """Empty bead_id soft-fails to unblocked (no gate issue)."""
    _patch_show({"id": "", "waits_for": "children-of(x)"})
    _patch_run_bd_and_parse([])
    assert lifecycle._has_fan_out_gate_issue("") is False


def test_case_sensitivity_in_children_of_format():
    """Verify children-of() format is case-sensitive (exact match)."""
    _patch_show(
        {
            "id": "finalize",
            "waits_for": "CHILDREN-OF(discover)",  # uppercase
        }
    )
    _patch_run_bd_and_parse([{"id": "discover", "status": "closed"}])
    # Should NOT match because format check is case-sensitive
    assert lifecycle._has_fan_out_gate_issue("finalize") is False


def test_waits_for_as_non_string_type():
    """waits_for as non-string (e.g., dict) soft-fails."""
    _patch_show(
        {
            "id": "x",
            "waits_for": {"type": "children-of", "spawner": "y"},  # dict, not string
        }
    )
    _patch_run_bd_and_parse([])
    assert lifecycle._has_fan_out_gate_issue("x") is False


def test_gate_unblock_transition():
    """Regression test: gate becomes satisfied when last child closes.

    This test captures the unblock transition: we query the current state
    and find the gate satisfied because all children are now closed.
    """
    # Initial state: finalize waits on discover, discover.1 and discover.2 are open
    _patch_show(
        {
            "id": "finalize",
            "waits_for": "children-of(discover)",
        }
    )
    _patch_run_bd_and_parse(
        [
            {"id": "discover", "status": "closed"},
            {"id": "discover.1", "parent": "discover", "status": "open"},
            {"id": "discover.2", "parent": "discover", "status": "open"},
        ]
    )
    assert lifecycle._has_fan_out_gate_issue("finalize") is True

    # Transition: discover.1 and discover.2 both close
    _patch_show(
        {
            "id": "finalize",
            "waits_for": "children-of(discover)",
        }
    )
    _patch_run_bd_and_parse(
        [
            {"id": "discover", "status": "closed"},
            {"id": "discover.1", "parent": "discover", "status": "closed"},
            {"id": "discover.2", "parent": "discover", "status": "closed"},
        ]
    )
    assert lifecycle._has_fan_out_gate_issue("finalize") is False


def test_gate_check_uses_scoped_parent_query():
    """Regression (bead_chain-ygs): the gate check must scope its query.

    The old implementation fetched the ENTIRE issue database
    (``bd list --json`` with no filter) and scanned it client-side —
    O(total issues) work to answer a question about one spawner's
    children. The fix routes through ``beads.has_open_children``, which
    issues a ``bd list --parent=<spawner> --json`` so the filter happens
    server-side. This locks in that contract by asserting the argv.
    """
    captured: list[tuple[str, ...]] = []

    def mock_run_bd(*args, **kwargs):  # noqa: ARG001
        captured.append(args)
        import json

        return json.dumps(
            [{"id": "discover.1", "parent": "discover", "status": "open"}]
        )

    def mock_parse_json(raw, context):  # noqa: ARG001
        import json

        return json.loads(raw)

    _patch_show({"id": "finalize", "waits_for": "children-of(discover)"})
    beads._run_bd = mock_run_bd  # type: ignore[assignment]
    beads._parse_json_list = mock_parse_json  # type: ignore[assignment]

    assert lifecycle._has_fan_out_gate_issue("finalize") is True
    # Exactly one bd query, and it is the SCOPED parent filter — never a
    # bare ``bd list --json`` full-database scan.
    assert captured == [("list", "--parent=discover", "--json")]


# ---------------------------------------------------------------------------
# FB-13 (bead_chain-y0s): mode-aware fan-out gates + unknown-mode no-revert
# ---------------------------------------------------------------------------


def test_fan_out_mode_unknown_when_no_mode_surfaced():
    """Today's bd (mode write-only) ⇒ mode resolves to None (unknown)."""
    bead = {"id": "finalize", "waits_for": "children-of(discover)"}
    assert lifecycle._fan_out_gate_mode(bead) is None


def test_fan_out_mode_reads_top_level_key():
    """Once bd surfaces a top-level mode key, it is honored (any/all)."""
    any_bead = {"waits_for": "children-of(d)", "waits_for_gate": "any-children"}
    all_bead = {"waits_for": "children-of(d)", "waits_for_gate": "all-children"}
    assert lifecycle._fan_out_gate_mode(any_bead) == lifecycle._FAN_OUT_MODE_ANY
    assert lifecycle._fan_out_gate_mode(all_bead) == lifecycle._FAN_OUT_MODE_ALL


def test_fan_out_mode_tolerates_spelling_drift():
    """`any`, `any_children`, `ANY-CHILDREN` all normalize to any-children."""
    for raw in ("any", "any_children", "ANY-CHILDREN", " Any-Child "):
        assert lifecycle._normalize_fan_out_mode(raw) == lifecycle._FAN_OUT_MODE_ANY
    for raw in ("all", "all_children", "ALL-CHILDREN"):
        assert lifecycle._normalize_fan_out_mode(raw) == lifecycle._FAN_OUT_MODE_ALL
    # Garbage / non-string ⇒ unknown, never a guess.
    assert lifecycle._normalize_fan_out_mode("sometimes") is None
    assert lifecycle._normalize_fan_out_mode({"x": 1}) is None


def test_fan_out_mode_reads_dependency_edge():
    """If bd surfaces the mode on a dependency entry, that is honored too."""
    bead = {
        "waits_for": "children-of(d)",
        "dependencies": [{"type": "waits-for", "gate": "any-children"}],
    }
    assert lifecycle._fan_out_gate_mode(bead) == lifecycle._FAN_OUT_MODE_ANY


def test_any_children_satisfied_after_first_child_closes():
    """any-children: one closed child ⇒ gate satisfied even with open siblings."""
    _patch_show({"id": "discover", "status": "in_progress"})
    _patch_run_bd_and_parse(
        [
            {"id": "discover.1", "parent": "discover", "status": "closed"},
            {"id": "discover.2", "parent": "discover", "status": "open"},
        ]
    )
    bead = {
        "id": "finalize",
        "waits_for": "children-of(discover)",
        "waits_for_gate": "any-children",
    }
    verdict = lifecycle._fan_out_gate_verdict("finalize", bead)
    assert verdict.blocked is False
    assert verdict.mode_known is True


def test_any_children_unsatisfied_when_no_child_closed():
    """any-children: zero closed children ⇒ gate still unsatisfied (blocked)."""
    _patch_show({"id": "discover", "status": "in_progress"})
    _patch_run_bd_and_parse(
        [
            {"id": "discover.1", "parent": "discover", "status": "open"},
            {"id": "discover.2", "parent": "discover", "status": "in_progress"},
        ]
    )
    bead = {
        "id": "finalize",
        "waits_for": "children-of(discover)",
        "waits_for_gate": "any-children",
    }
    verdict = lifecycle._fan_out_gate_verdict("finalize", bead)
    assert verdict.blocked is True
    assert verdict.mode_known is True


def test_unknown_mode_blocks_but_marks_revert_unsafe():
    """Unknown mode + open child ⇒ blocked (conservative) but mode_known False.

    This is the FB-13 core: with the mode invisible we still refuse to
    drive (blocked), but flag the revert as unsafe so the caller does not
    strand a possibly-ready any-children waiter at ``open``.
    """
    _patch_show({"id": "discover", "status": "in_progress"})
    _patch_run_bd_and_parse(
        [
            {"id": "discover.1", "parent": "discover", "status": "closed"},
            {"id": "discover.2", "parent": "discover", "status": "open"},
        ]
    )
    bead = {"id": "finalize", "waits_for": "children-of(discover)"}
    verdict = lifecycle._fan_out_gate_verdict("finalize", bead)
    assert verdict.blocked is True
    assert verdict.mode_known is False


def test_all_children_mode_known_allows_revert():
    """Explicit all-children + open child ⇒ blocked AND mode_known (revert ok)."""
    _patch_show({"id": "discover", "status": "in_progress"})
    _patch_run_bd_and_parse(
        [{"id": "discover.1", "parent": "discover", "status": "open"}]
    )
    bead = {
        "id": "finalize",
        "waits_for": "children-of(discover)",
        "waits_for_gate": "all-children",
    }
    verdict = lifecycle._fan_out_gate_verdict("finalize", bead)
    assert verdict.blocked is True
    assert verdict.mode_known is True


def test_has_fan_out_gate_issue_still_bool_for_unknown_all_children():
    """Back-compat: the bool wrapper keeps all-children semantics by default."""
    _patch_show({"id": "discover", "status": "in_progress"})
    _patch_run_bd_and_parse(
        [{"id": "discover.1", "parent": "discover", "status": "open"}]
    )
    bead = {"id": "finalize", "waits_for": "children-of(discover)"}
    assert lifecycle._has_fan_out_gate_issue("finalize", bead) is True


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
            except Exception as exc:
                failures += 1
                print(f"ERROR {name}: {exc}")
    sys.exit(1 if failures else 0)
