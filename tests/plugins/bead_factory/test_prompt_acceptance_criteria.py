"""Unit tests for acceptance_criteria rendering in the goal prompt.

Coverage-audit gap FB-2 (``bead_chain-2zx``): ``acceptance_criteria`` is
already a key on the ``bd ready --json`` dict, but
:func:`prompt.format_bead_as_goal` historically never read it, so the LLM
judges graded completion against a contract the prompt never showed the
agent. These tests pin the present / absent behaviour and the
``_format_acceptance_criteria_block`` helper.

Imports go through the registered package (conftest sets it up) because
``prompt.py`` uses relative imports.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import prompt  # noqa: E402

_HEADING = "## Acceptance Criteria"


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
# format_bead_as_goal: present case
# --------------------------------------------------------------------------


def test_present_acceptance_criteria_is_rendered_under_heading():
    bead = _base_bead(acceptance_criteria="- foo works\n- bar is tested")
    out = prompt.format_bead_as_goal(bead)
    assert _HEADING in out
    assert "- foo works" in out
    assert "- bar is tested" in out


def test_acceptance_criteria_appears_before_done_checklist():
    bead = _base_bead(acceptance_criteria="- must pass")
    out = prompt.format_bead_as_goal(bead)
    assert out.index(_HEADING) < out.index("When you believe this is done:")


def test_acceptance_criteria_after_metadata():
    bead = _base_bead(acceptance_criteria="- must pass")
    out = prompt.format_bead_as_goal(bead)
    assert out.index("Issue metadata:") < out.index(_HEADING)


# --------------------------------------------------------------------------
# format_bead_as_goal: absent / empty cases -> prompt unchanged
# --------------------------------------------------------------------------


def test_absent_acceptance_criteria_no_heading():
    out = prompt.format_bead_as_goal(_base_bead())
    assert _HEADING not in out


def test_empty_string_acceptance_criteria_no_heading():
    out = prompt.format_bead_as_goal(_base_bead(acceptance_criteria=""))
    assert _HEADING not in out


def test_whitespace_only_acceptance_criteria_no_heading():
    out = prompt.format_bead_as_goal(_base_bead(acceptance_criteria="   \n  \t"))
    assert _HEADING not in out


def test_non_string_acceptance_criteria_no_heading():
    out = prompt.format_bead_as_goal(_base_bead(acceptance_criteria=["a", "b"]))
    assert _HEADING not in out


def test_absent_vs_present_only_differ_by_block():
    """The present prompt equals the absent prompt plus the criteria block."""
    absent = prompt.format_bead_as_goal(_base_bead())
    block = "## Acceptance Criteria\n- must pass\n\n"
    present = prompt.format_bead_as_goal(_base_bead(acceptance_criteria="- must pass"))
    assert present == absent.replace(
        "When you believe this is done:",
        block + "When you believe this is done:",
        1,
    )


# --------------------------------------------------------------------------
# _format_acceptance_criteria_block helper
# --------------------------------------------------------------------------


def test_block_helper_present():
    out = prompt._format_acceptance_criteria_block({"acceptance_criteria": "- x"})
    assert out == "## Acceptance Criteria\n- x\n\n"


def test_block_helper_absent_is_empty():
    assert prompt._format_acceptance_criteria_block({}) == ""


def test_block_helper_empty_is_empty():
    assert prompt._format_acceptance_criteria_block({"acceptance_criteria": ""}) == ""


def test_block_helper_strips_surrounding_whitespace():
    out = prompt._format_acceptance_criteria_block(
        {"acceptance_criteria": "\n  - x  \n"}
    )
    assert out == "## Acceptance Criteria\n- x\n\n"


def test_block_helper_does_not_double_existing_heading():
    """A value that already leads with the heading is not double-prefixed."""
    val = "## Acceptance Criteria\n- already headed"
    out = prompt._format_acceptance_criteria_block({"acceptance_criteria": val})
    assert out == "## Acceptance Criteria\n- already headed\n\n"
    assert out.count(_HEADING) == 1


def test_block_helper_recognizes_unhashed_heading_text():
    """A value beginning with the bare 'Acceptance Criteria' words isn't re-headed."""
    val = "Acceptance Criteria:\n- bare"
    out = prompt._format_acceptance_criteria_block({"acceptance_criteria": val})
    assert out == "Acceptance Criteria:\n- bare\n\n"


# --------------------------------------------------------------------------
# Interaction with preambles (recovery still includes criteria)
# --------------------------------------------------------------------------


def test_recovery_prompt_still_renders_criteria():
    bead = _base_bead(acceptance_criteria="- recovered done")
    out = prompt.format_bead_as_goal(bead, recovery=True)
    assert "RECOVERY MODE" in out
    assert _HEADING in out
    assert "- recovered done" in out


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
