"""Unit tests for the boilerplate-free bead-content renderer.

bead-factory-8yt (epic bead-factory-cri): the active bead's content gets
pinned into the compaction-protected system prompt, so it must carry the
bead's *own* fields and nothing else. :func:`prompt.format_bead_content`
composes the same per-block builders as
:func:`prompt.format_bead_as_build` but drops the chain scaffolding (the
persistent-memories digest, the template-lint contract, the done-checklist,
the bug-discovery protocol and the recovery/triage preambles).

Also covers :func:`prompt._format_notes_block` (the ``notes`` field
renderer used by the content render; bead-factory-8u4's spec).

Imports go through the registered package (conftest sets it up) because
``prompt.py`` uses relative imports.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import prompt  # noqa: E402


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


# --------------------------------------------------------------------------
# format_bead_content: renders the bead's own fields
# --------------------------------------------------------------------------


def test_renders_id_title_description_and_metadata():
    out = prompt.format_bead_content(_base_bead())
    assert "Complete beads issue demo-1: Do the thing" in out
    assert "A thing that must be done." in out
    assert "Issue metadata:" in out
    assert "- Type: task" in out
    assert "- Priority: P1" in out


def test_renders_labels_in_metadata():
    out = prompt.format_bead_content(_base_bead(labels=["alpha", "beta"]))
    assert "- Labels: alpha, beta" in out


def test_renders_design_acceptance_notes_related_in_order():
    bead = _base_bead(
        design="use a strategy pattern",
        acceptance_criteria="- it works",
        notes="remediation: fix the thing",
        dependencies=[{"type": "related", "depends_on_id": "x-9", "title": "ctx"}],
    )
    out = prompt.format_bead_content(bead)
    assert "## Design" in out
    assert "## Acceptance Criteria" in out
    assert "## Notes" in out
    assert "## Related Context" in out
    assert (
        out.index("## Design")
        < out.index("## Acceptance Criteria")
        < out.index("## Notes")
        < out.index("## Related Context")
    )


def test_bare_bead_has_no_empty_block_headings():
    out = prompt.format_bead_content(_base_bead())
    assert "## Design" not in out
    assert "## Acceptance Criteria" not in out
    assert "## Notes" not in out
    assert "## Related Context" not in out


def test_ends_with_single_trailing_newline():
    out = prompt.format_bead_content(_base_bead(notes="something"))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


# --------------------------------------------------------------------------
# format_bead_content: NO boilerplate / scaffolding
# --------------------------------------------------------------------------


def test_no_done_checklist():
    out = prompt.format_bead_content(_base_bead(acceptance_criteria="- x"))
    assert "When you believe this is done:" not in out


def test_no_bug_discovery_protocol():
    out = prompt.format_bead_content(_base_bead())
    assert "BUG DISCOVERY PROTOCOL" not in out
    assert "inspectors will verify" not in out.lower()


def test_no_recovery_or_triage_preamble_even_for_triaged_bug():
    bead = _base_bead(
        issue_type="bug",
        description=f"{prompt.TRIAGE_MARKER} something broke",
    )
    out = prompt.format_bead_content(bead)
    assert "RECOVERY MODE" not in out
    assert "TRIAGE VERIFICATION" not in out


def test_does_not_fetch_memory_digest(monkeypatch):
    # If content rendering tried to fetch memories it would blow up here.
    from code_puppy.plugins.bead_factory import prompt_blocks

    def _boom():  # pragma: no cover - must never be called
        raise AssertionError("format_bead_content must not fetch memories")

    monkeypatch.setattr(prompt_blocks, "memories", _boom)
    out = prompt.format_bead_content(_base_bead())
    assert "## Persistent Memories" not in out


def test_does_not_fetch_lint_warnings(monkeypatch):
    from code_puppy.plugins.bead_factory import prompt_blocks

    def _boom(_id):  # pragma: no cover - must never be called
        raise AssertionError("format_bead_content must not run bd lint")

    monkeypatch.setattr(prompt_blocks, "lint_warnings", _boom)
    out = prompt.format_bead_content(_base_bead())
    assert "## Template Lint Warnings" not in out


def test_renders_notes_field():
    out = prompt.format_bead_content(_base_bead(notes="see the rework feedback"))
    assert "## Notes" in out
    assert "see the rework feedback" in out


# --------------------------------------------------------------------------
# _format_notes_block helper (bead-factory-8u4 contract)
# --------------------------------------------------------------------------


def test_notes_helper_present():
    out = prompt._format_notes_block({"notes": "a remark"})
    assert out == "## Notes\na remark\n\n"


def test_notes_helper_absent_is_empty():
    assert prompt._format_notes_block({}) == ""


def test_notes_helper_empty_is_empty():
    assert prompt._format_notes_block({"notes": ""}) == ""


def test_notes_helper_whitespace_only_is_empty():
    assert prompt._format_notes_block({"notes": "   \n  \t"}) == ""


def test_notes_helper_non_string_is_empty():
    assert prompt._format_notes_block({"notes": ["a", "b"]}) == ""


def test_notes_helper_strips_surrounding_whitespace():
    out = prompt._format_notes_block({"notes": "\n  a remark  \n"})
    assert out == "## Notes\na remark\n\n"


def test_notes_helper_does_not_double_existing_heading():
    out = prompt._format_notes_block({"notes": "## Notes\nalready headed"})
    assert out == "## Notes\nalready headed\n\n"
    assert out.count("## Notes") == 1


# --------------------------------------------------------------------------
# Parity: content render is a substring-of-fields subset of the build prompt
# --------------------------------------------------------------------------


def test_content_fields_appear_in_full_build_prompt(monkeypatch):
    """Content blocks render identically inside the full build prompt."""
    from code_puppy.plugins.bead_factory import prompt_blocks

    # Neutralise the impure fetches so the build prompt is deterministic.
    monkeypatch.setattr(prompt_blocks, "memories", lambda: {})
    monkeypatch.setattr(prompt_blocks, "lint_warnings", lambda _id: [])

    bead = _base_bead(
        design="why", acceptance_criteria="- done", notes="steering feedback"
    )
    content = prompt.format_bead_content(bead)
    build = prompt.format_bead_as_build(bead)
    # Each non-empty content block reappears verbatim in the build prompt.
    assert "## Design\nwhy" in build
    assert "## Design\nwhy" in content
    assert "## Acceptance Criteria\n- done" in build
    assert "## Acceptance Criteria\n- done" in content
    # bead-factory-8u4: the ``notes`` block now rides the build prompt too,
    # so both the implementor and the LLM inspectors see it.
    assert "## Notes\nsteering feedback" in build
    assert "## Notes\nsteering feedback" in content


def test_notes_rendered_in_build_prompt(monkeypatch):
    """format_bead_as_build surfaces ``notes`` (bead-factory-8u4 goal)."""
    from code_puppy.plugins.bead_factory import prompt_blocks

    monkeypatch.setattr(prompt_blocks, "memories", lambda: {})
    monkeypatch.setattr(prompt_blocks, "lint_warnings", lambda _id: [])

    # Present -> a ## Notes block appears.
    build = prompt.format_bead_as_build(_base_bead(notes="see the rework feedback"))
    assert "## Notes\nsee the rework feedback\n" in build

    # Absent -> no Notes block (prompt byte-for-byte unchanged on that axis).
    bare = prompt.format_bead_as_build(_base_bead())
    assert "## Notes" not in bare


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if "monkeypatch" in fn.__code__.co_varnames:
                    continue
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {exc}")
    sys.exit(1 if failures else 0)
