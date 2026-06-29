"""Unit tests for the non-gating ``## Related Context`` block (FB-11).

Coverage-audit gap FB-11 (``bead_chain-n57``, dependency#2): the six
context-bearing edges (``related``, ``relates-to``, ``tracks``,
``discovered-from``, ``caused-by``, ``validates``) are never surfaced in
the goal prompt, so the working agent (and the LLM judges) are blind to
the bead's provenance, causal bug link, validating test and related
work. These tests pin the present/absent rendering of
:func:`prompt._format_related_context_block` and verify that **gating
behaviour is untouched** (blocks / waits-for / parent-child never appear
in the block).

The dependency-edge records come in two upstream shapes: ``bd ready`` /
``bd list`` use ``type`` + ``depends_on_id``; ``bd show`` uses
``dependency_type`` + ``id`` (+ inline ``title``). Both are exercised.

Imports go through the registered package (conftest sets it up) because
``prompt.py`` uses relative imports.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import prompt  # noqa: E402

_HEADING = "## Related Context"


def _base_bead(**extra) -> dict:
    bead = {
        "id": "demo-1",
        "title": "Do the thing",
        "description": "A thing that must be done.",
        "issue_type": "task",
        "priority": 1,
    }
    bead.update(extra)
    return bead


def _ready_edge(target: str, edge_type: str, **extra) -> dict:
    """A ``bd ready``/``bd list``-shaped inbound edge record."""
    dep = {"issue_id": "demo-1", "depends_on_id": target, "type": edge_type}
    dep.update(extra)
    return dep


def _show_edge(target: str, edge_type: str, **extra) -> dict:
    """A ``bd show``-shaped inbound edge record (carries title)."""
    dep = {"id": target, "dependency_type": edge_type}
    dep.update(extra)
    return dep


# --------------------------------------------------------------------------
# format_bead_as_goal: present cases
# --------------------------------------------------------------------------


def test_present_context_edges_render_block():
    bead = _base_bead(
        dependencies=[
            _ready_edge("demo-9", "discovered-from"),
            _ready_edge("demo-8", "caused-by"),
            _ready_edge("demo-7", "validates"),
            _ready_edge("demo-6", "related"),
        ]
    )
    out = prompt.format_bead_as_goal(bead)
    assert _HEADING in out
    assert "- Discovered while working on demo-9" in out
    assert "- Caused by demo-8" in out
    assert "- Validates demo-7" in out
    assert "- Related to demo-6" in out


def test_block_appears_after_acceptance_before_checklist():
    bead = _base_bead(
        acceptance_criteria="- it works",
        dependencies=[_ready_edge("demo-2", "related")],
    )
    out = prompt.format_bead_as_goal(bead)
    assert out.index("## Acceptance Criteria") < out.index(_HEADING)
    assert out.index(_HEADING) < out.index("When you believe this is done:")


def test_block_caveat_states_non_gating():
    bead = _base_bead(dependencies=[_ready_edge("demo-2", "caused-by")])
    out = prompt.format_bead_as_goal(bead)
    assert "do NOT block this" in out.lower() or "do not block this" in out.lower()


def test_show_shape_edges_render_with_title():
    bead = _base_bead(
        dependencies=[_show_edge("demo-5", "validates", title="The test bead")]
    )
    out = prompt.format_bead_as_goal(bead)
    assert "- Validates demo-5: The test bead" in out


# --------------------------------------------------------------------------
# format_bead_as_goal: gating / structural edges are NOT surfaced
# --------------------------------------------------------------------------


def test_blocking_edges_not_in_block():
    bead = _base_bead(
        dependencies=[
            _ready_edge("demo-b", "blocks"),
            _ready_edge("demo-w", "waits-for"),
            _ready_edge("demo-p", "parent-child"),
        ]
    )
    out = prompt.format_bead_as_goal(bead)
    # None of these are context edges -> no block at all.
    assert _HEADING not in out
    assert "demo-b" not in out
    assert "demo-w" not in out


def test_mixed_edges_only_context_surfaced():
    bead = _base_bead(
        dependencies=[
            _ready_edge("demo-b", "blocks"),
            _ready_edge("demo-r", "related"),
            _ready_edge("demo-p", "parent-child"),
        ]
    )
    out = prompt.format_bead_as_goal(bead)
    assert "- Related to demo-r" in out
    assert "demo-b" not in out
    assert "demo-p" not in out


def test_until_and_supersedes_not_surfaced():
    # Informational but out-of-scope edges must not leak into the block.
    bead = _base_bead(
        dependencies=[
            _ready_edge("demo-u", "until"),
            _ready_edge("demo-s", "supersedes"),
        ]
    )
    out = prompt.format_bead_as_goal(bead)
    assert _HEADING not in out


# --------------------------------------------------------------------------
# format_bead_as_goal: absent / malformed -> prompt unchanged
# --------------------------------------------------------------------------


def test_absent_dependencies_no_block():
    out = prompt.format_bead_as_goal(_base_bead())
    assert _HEADING not in out


def test_empty_dependencies_no_block():
    out = prompt.format_bead_as_goal(_base_bead(dependencies=[]))
    assert _HEADING not in out


def test_non_list_dependencies_no_block():
    out = prompt.format_bead_as_goal(_base_bead(dependencies="related"))
    assert _HEADING not in out


def test_malformed_edge_entries_skipped():
    bead = _base_bead(
        dependencies=[
            "not-a-dict",
            {"type": "related"},  # no target id
            {"depends_on_id": "demo-x"},  # no type
            _ready_edge("demo-ok", "related"),
        ]
    )
    out = prompt.format_bead_as_goal(bead)
    assert "- Related to demo-ok" in out
    assert out.count("\n- ") >= 1


# --------------------------------------------------------------------------
# Gating behaviour is unchanged: the block never touches readiness.
# This is a structural guarantee — the helper only reads, never calls bd.
# --------------------------------------------------------------------------


def test_helper_is_pure_no_blocks_key_consulted():
    # A bead with NO context edges yields no block regardless of any
    # gating-related fields present on the dict.
    bead = _base_bead(
        dependencies=[_ready_edge("demo-b", "blocks")],
        blocked=True,
    )
    assert prompt._format_related_context_block(bead) == ""


# --------------------------------------------------------------------------
# _format_related_context_block helper
# --------------------------------------------------------------------------


def test_helper_absent_is_empty():
    assert prompt._format_related_context_block({}) == ""


def test_helper_orders_by_gloss_then_appearance():
    # discovered-from glosses lead, related trails (per _CONTEXT_EDGE_GLOSSES).
    bead = {
        "dependencies": [
            _ready_edge("r1", "related"),
            _ready_edge("d1", "discovered-from"),
        ]
    }
    out = prompt._format_related_context_block(bead)
    assert out.index("Discovered while working on d1") < out.index("Related to r1")


def test_helper_dedupes_same_type_and_target():
    bead = {
        "dependencies": [
            _ready_edge("dup", "related"),
            _ready_edge("dup", "related"),
        ]
    }
    out = prompt._format_related_context_block(bead)
    assert out.count("Related to dup") == 1


def test_helper_handles_all_six_context_types():
    bead = {
        "dependencies": [
            _ready_edge("a", "discovered-from"),
            _ready_edge("b", "caused-by"),
            _ready_edge("c", "validates"),
            _ready_edge("d", "related"),
            _ready_edge("e", "relates-to"),
            _ready_edge("f", "tracks"),
        ]
    }
    out = prompt._format_related_context_block(bead)
    assert "Discovered while working on a" in out
    assert "Caused by b" in out
    assert "Validates c" in out
    assert "Related to d" in out
    assert "Relates to e" in out
    assert "Tracks f" in out


def test_helper_edge_type_case_insensitive():
    bead = {"dependencies": [_ready_edge("x", "Discovered-From")]}
    out = prompt._format_related_context_block(bead)
    assert "Discovered while working on x" in out


def test_helper_block_ends_with_blank_line():
    bead = {"dependencies": [_ready_edge("x", "related")]}
    out = prompt._format_related_context_block(bead)
    assert out.endswith("\n\n")


# --------------------------------------------------------------------------
# Backward-compat + preamble interaction
# --------------------------------------------------------------------------


def test_no_context_edges_prompt_byte_for_byte_unchanged():
    plain = prompt.format_bead_as_goal(_base_bead())
    with_gating = prompt.format_bead_as_goal(
        _base_bead(dependencies=[_ready_edge("demo-b", "blocks")])
    )
    assert plain == with_gating


def test_recovery_prompt_still_renders_related_context():
    bead = _base_bead(dependencies=[_ready_edge("demo-9", "discovered-from")])
    out = prompt.format_bead_as_goal(bead, recovery=True)
    assert "RECOVERY MODE" in out
    assert "- Discovered while working on demo-9" in out


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
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {exc}")
    sys.exit(1 if failures else 0)
