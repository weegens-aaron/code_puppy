"""Unit tests for design + labels rendering in the build prompt.

Coverage-audit gap FB-7 (``bead_chain-432``): ``labels`` is already a key
on the ``bd ready --json`` dict and ``design`` is bd's ADR/design-rationale
field, but :func:`prompt.format_bead_as_build` historically read neither
(anatomy #2/#3). These tests pin the present / absent behaviour and the
``_format_design_block`` / ``_format_labels_line`` helpers.

Imports go through the registered package (conftest sets it up) because
``prompt.py`` uses relative imports.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import prompt  # noqa: E402

_DESIGN_HEADING = "## Design"


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
# format_bead_as_build: labels present case
# --------------------------------------------------------------------------


def test_present_labels_rendered_in_metadata():
    bead = _base_bead(labels=["bead-chain", "prompt", "remediation"])
    out = prompt.format_bead_as_build(bead)
    assert "- Labels: bead-chain, prompt, remediation" in out


def test_labels_line_is_inside_metadata_block():
    bead = _base_bead(labels=["alpha"])
    out = prompt.format_bead_as_build(bead)
    assert out.index("Issue metadata:") < out.index("- Labels: alpha")
    assert out.index("- Labels: alpha") < out.index("When you believe this is done:")


# --------------------------------------------------------------------------
# format_bead_as_build: labels absent / empty cases -> prompt unchanged
# --------------------------------------------------------------------------


def test_absent_labels_no_line():
    out = prompt.format_bead_as_build(_base_bead())
    assert "- Labels:" not in out


def test_empty_list_labels_no_line():
    out = prompt.format_bead_as_build(_base_bead(labels=[]))
    assert "- Labels:" not in out


def test_whitespace_only_labels_no_line():
    out = prompt.format_bead_as_build(_base_bead(labels=["  ", "\t"]))
    assert "- Labels:" not in out


def test_non_list_labels_no_line():
    out = prompt.format_bead_as_build(_base_bead(labels="bead-chain"))
    assert "- Labels:" not in out


# --------------------------------------------------------------------------
# _format_labels_line helper
# --------------------------------------------------------------------------


def test_labels_helper_present():
    assert prompt._format_labels_line({"labels": ["a", "b"]}) == ["- Labels: a, b"]


def test_labels_helper_strips_and_drops_empties():
    out = prompt._format_labels_line({"labels": ["  a  ", "", "  ", "b"]})
    assert out == ["- Labels: a, b"]


def test_labels_helper_absent_is_empty_list():
    assert prompt._format_labels_line({}) == []


def test_labels_helper_tuple_accepted():
    assert prompt._format_labels_line({"labels": ("x", "y")}) == ["- Labels: x, y"]


def test_labels_helper_coerces_non_string_entries():
    assert prompt._format_labels_line({"labels": [1, 2]}) == ["- Labels: 1, 2"]


# --------------------------------------------------------------------------
# format_bead_as_build: design present case
# --------------------------------------------------------------------------


def test_present_design_rendered_under_heading():
    bead = _base_bead(design="Use a strategy pattern; see ADR-3.")
    out = prompt.format_bead_as_build(bead)
    assert _DESIGN_HEADING in out
    assert "Use a strategy pattern; see ADR-3." in out


def test_design_appears_after_metadata_and_before_done_checklist():
    bead = _base_bead(design="some rationale")
    out = prompt.format_bead_as_build(bead)
    assert out.index("Issue metadata:") < out.index(_DESIGN_HEADING)
    assert out.index(_DESIGN_HEADING) < out.index("When you believe this is done:")


def test_design_appears_before_acceptance_criteria():
    bead = _base_bead(design="rationale", acceptance_criteria="- it works")
    out = prompt.format_bead_as_build(bead)
    assert out.index(_DESIGN_HEADING) < out.index("## Acceptance Criteria")


# --------------------------------------------------------------------------
# format_bead_as_build: design absent / empty cases -> prompt unchanged
# --------------------------------------------------------------------------


def test_absent_design_no_heading():
    out = prompt.format_bead_as_build(_base_bead())
    assert _DESIGN_HEADING not in out


def test_empty_string_design_no_heading():
    out = prompt.format_bead_as_build(_base_bead(design=""))
    assert _DESIGN_HEADING not in out


def test_whitespace_only_design_no_heading():
    out = prompt.format_bead_as_build(_base_bead(design="   \n  \t"))
    assert _DESIGN_HEADING not in out


def test_non_string_design_no_heading():
    out = prompt.format_bead_as_build(_base_bead(design={"k": "v"}))
    assert _DESIGN_HEADING not in out


# --------------------------------------------------------------------------
# _format_design_block helper
# --------------------------------------------------------------------------


def test_design_helper_present():
    out = prompt._format_design_block({"design": "rationale"})
    assert out == "## Design\nrationale\n\n"


def test_design_helper_absent_is_empty():
    assert prompt._format_design_block({}) == ""


def test_design_helper_empty_is_empty():
    assert prompt._format_design_block({"design": ""}) == ""


def test_design_helper_strips_surrounding_whitespace():
    out = prompt._format_design_block({"design": "\n  rationale  \n"})
    assert out == "## Design\nrationale\n\n"


def test_design_helper_does_not_double_existing_heading():
    val = "## Design\nalready headed"
    out = prompt._format_design_block({"design": val})
    assert out == "## Design\nalready headed\n\n"
    assert out.count(_DESIGN_HEADING) == 1


def test_design_helper_recognizes_unhashed_heading_text():
    val = "Design notes:\n- bare"
    out = prompt._format_design_block({"design": val})
    assert out == "Design notes:\n- bare\n\n"


# --------------------------------------------------------------------------
# Backward-compat: prompts with neither field are byte-for-byte unchanged
# --------------------------------------------------------------------------


def test_neither_field_present_adds_nothing():
    out = prompt.format_bead_as_build(_base_bead())
    assert _DESIGN_HEADING not in out
    assert "- Labels:" not in out


def test_both_fields_compose_in_order():
    bead = _base_bead(
        labels=["x"],
        design="why",
        acceptance_criteria="- done",
    )
    out = prompt.format_bead_as_build(bead)
    # Labels (metadata) -> Design -> Acceptance -> checklist
    assert (
        out.index("- Labels: x")
        < out.index(_DESIGN_HEADING)
        < out.index("## Acceptance Criteria")
        < out.index("When you believe this is done:")
    )


# --------------------------------------------------------------------------
# Interaction with preambles (recovery still includes design + labels)
# --------------------------------------------------------------------------


def test_recovery_prompt_still_renders_design_and_labels():
    bead = _base_bead(design="recovered rationale", labels=["lbl"])
    out = prompt.format_bead_as_build(bead, recovery=True)
    assert "RECOVERY MODE" in out
    assert _DESIGN_HEADING in out
    assert "- Labels: lbl" in out


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
