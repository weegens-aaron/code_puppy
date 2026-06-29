"""Unit tests for the template-lint-warnings block in the goal prompt.

Coverage-audit gap FB-5 (``bead_chain-vmo``): bead-chain drove beads off
``bd ready`` without ever consulting the template contract, so a bead
that lost its ``## Acceptance Criteria`` to a ``--graph`` import would be
graded by the LLM judges against a section the prompt never showed was
missing. ``format_bead_as_goal`` now runs ``bd lint <id>`` on the claim
path and folds the missing sections into a ``## Template Lint Warnings``
block. These tests pin:

* the pure :func:`prompt._format_lint_warnings_block` helper,
* the impure :func:`prompt._fetch_lint_warnings` soft-fail contract,
* the wiring in :func:`prompt.format_bead_as_goal` (present / absent /
  placement / preamble interaction).

Imports go through the registered package (conftest sets it up) because
``prompt.py`` uses relative imports.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import beads, prompt, prompt_blocks  # noqa: E402

_HEADING = "## Template Lint Warnings"


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


def _stub_lint(monkeypatch, value):
    """Force ``format_bead_as_goal`` to see exactly ``value`` warnings."""
    monkeypatch.setattr(prompt, "_fetch_lint_warnings", lambda _bead_id: value)


# --------------------------------------------------------------------------
# _format_lint_warnings_block helper (pure)
# --------------------------------------------------------------------------


def test_block_helper_present():
    out = prompt._format_lint_warnings_block(["## Acceptance Criteria"])
    assert out.startswith(_HEADING)
    assert "- ## Acceptance Criteria" in out
    assert out.endswith("\n\n")


def test_block_helper_multiple_bullets():
    out = prompt._format_lint_warnings_block(
        ["## Steps to Reproduce", "## Acceptance Criteria"]
    )
    assert "- ## Steps to Reproduce" in out
    assert "- ## Acceptance Criteria" in out


def test_block_helper_empty_is_empty():
    assert prompt._format_lint_warnings_block([]) == ""


# --------------------------------------------------------------------------
# _fetch_lint_warnings soft-fail contract
# --------------------------------------------------------------------------


def test_fetch_passes_through_warnings(monkeypatch):
    monkeypatch.setattr(prompt_blocks, "lint_warnings", lambda _id: ["## X"])
    assert prompt._fetch_lint_warnings("demo-1") == ["## X"]


def test_fetch_soft_fails_on_beads_error(monkeypatch):
    def _boom(_id):
        raise beads.BeadsError("no lint subcommand on this bd build")

    monkeypatch.setattr(prompt_blocks, "lint_warnings", _boom)
    assert prompt._fetch_lint_warnings("demo-1") == []


# --------------------------------------------------------------------------
# format_bead_as_goal: present case
# --------------------------------------------------------------------------


def test_present_warnings_render_under_heading(monkeypatch):
    _stub_lint(monkeypatch, ["## Acceptance Criteria"])
    out = prompt.format_bead_as_goal(_base_bead())
    assert _HEADING in out
    assert "- ## Acceptance Criteria" in out


def test_warnings_appear_before_done_checklist(monkeypatch):
    _stub_lint(monkeypatch, ["## Acceptance Criteria"])
    out = prompt.format_bead_as_goal(_base_bead())
    assert out.index(_HEADING) < out.index("When you believe this is done:")


def test_warnings_after_acceptance_block(monkeypatch):
    """Acceptance (what's present) leads; lint (what's missing) follows."""
    _stub_lint(monkeypatch, ["## Acceptance Criteria"])
    bead = _base_bead(acceptance_criteria="- partial criteria")
    out = prompt.format_bead_as_goal(bead)
    assert out.index("## Acceptance Criteria") < out.index(_HEADING)


# --------------------------------------------------------------------------
# format_bead_as_goal: absent / clean -> prompt unchanged
# --------------------------------------------------------------------------


def test_clean_bead_no_heading(monkeypatch):
    _stub_lint(monkeypatch, [])
    out = prompt.format_bead_as_goal(_base_bead())
    assert _HEADING not in out


def test_clean_vs_warned_only_differ_by_block(monkeypatch):
    _stub_lint(monkeypatch, [])
    clean = prompt.format_bead_as_goal(_base_bead())

    _stub_lint(monkeypatch, ["## Acceptance Criteria"])
    warned = prompt.format_bead_as_goal(_base_bead())

    block = prompt._format_lint_warnings_block(["## Acceptance Criteria"])
    assert warned == clean.replace(
        "When you believe this is done:",
        block + "When you believe this is done:",
        1,
    )


# --------------------------------------------------------------------------
# Interaction with preambles (recovery still surfaces warnings)
# --------------------------------------------------------------------------


def test_recovery_prompt_still_surfaces_warnings(monkeypatch):
    _stub_lint(monkeypatch, ["## Acceptance Criteria"])
    out = prompt.format_bead_as_goal(_base_bead(), recovery=True)
    assert "RECOVERY MODE" in out
    assert _HEADING in out


if __name__ == "__main__":
    # Minimal monkeypatch shim so this runs without pytest. Records and
    # restores touched attributes after each test so a stub from one test
    # never leaks into the next (pytest's real monkeypatch does this).
    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def restore(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo.clear()

    failures = 0
    for fn_name, fn in sorted(globals().items()):
        if fn_name.startswith("test_") and callable(fn):
            mp = _MP()
            try:
                if "monkeypatch" in fn.__code__.co_varnames:
                    fn(mp)
                else:
                    fn()
                print(f"PASS {fn_name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {fn_name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {fn_name}: {exc}")
            finally:
                mp.restore()
    sys.exit(1 if failures else 0)
