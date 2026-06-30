"""De-duplication of the bead instruction (bead-factory-462, epic ...cri).

Once the bead's CONTENT is pinned into the compaction-protected system prompt
(bead-factory-5wv via ``system_prompt.on_load_prompt``), the implementor would
ALSO receive the full bead through its user-message build prompt -- the same
instruction twice on the initial prompt. The fix makes the bead content
single-source:

* :func:`prompt.format_bead_scaffolding` renders ONLY the chain scaffolding
  (mode preamble, pinned-contract pointer, memories digest, lint, done
  checklist, bug-discovery protocol) and NONE of the bead's own fields.
* :func:`prompt.build_prompts_for_arming` returns ``(implementor, inspector)``:
  the inspector copy is ALWAYS the FULL compose (it's a raw pydantic_ai agent
  with no system-prompt injection -- the CRITICAL invariant), while the
  implementor copy is slimmed when ``inject_content`` is True.
* :func:`system_prompt.is_pin_active` is the gate the arm sites consult.

Imports go through the registered package (conftest sets it up) because
``prompt.py`` uses relative imports.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import pytest  # noqa: E402
from code_puppy.plugins.bead_factory import prompt, state, system_prompt  # noqa: E402


def _base_bead(**extra) -> dict:
    bead = {
        "id": "demo-1",
        "title": "Do the thing",
        "description": "A thing that must be done.",
        "issue_type": "task",
        "priority": 1,
        "design": "Because reasons.",
        "acceptance_criteria": "- it works",
        "notes": "some rework feedback",
    }
    bead.update(extra)
    return bead


@pytest.fixture(autouse=True)
def _no_shellouts(monkeypatch):
    """Keep the scaffolding render deterministic -- no ``bd`` subprocesses."""
    monkeypatch.setattr(prompt, "_fetch_memory_digest", lambda: {})
    monkeypatch.setattr(prompt, "_fetch_lint_warnings", lambda _id: [])
    yield
    state.reset()


# --------------------------------------------------------------------------
# format_bead_scaffolding: scaffolding only, NO bead fields
# --------------------------------------------------------------------------


def test_scaffolding_drops_bead_own_fields():
    bead = _base_bead()
    out = prompt.format_bead_scaffolding(bead)
    # None of the bead's own content fields may appear.
    assert bead["title"] not in out
    assert bead["description"] not in out
    assert bead["design"] not in out
    assert "it works" not in out
    assert "rework feedback" not in out
    # Structural field headers are gone too.
    assert "## Design" not in out
    assert "## Acceptance Criteria" not in out
    assert "## Notes" not in out
    assert "Issue metadata:" not in out


def test_scaffolding_keeps_pointer_and_boilerplate():
    out = prompt.format_bead_scaffolding(_base_bead())
    assert "pinned in your system prompt" in out
    assert "When you believe this is done:" in out
    assert "BUG DISCOVERY PROTOCOL" in out


def test_scaffolding_recovery_preamble():
    out = prompt.format_bead_scaffolding(_base_bead(), recovery=True)
    assert "RECOVERY MODE" in out


def test_scaffolding_triage_preamble():
    bead = _base_bead(
        issue_type="bug",
        description=f"{prompt.TRIAGE_MARKER} broke",
    )
    out = prompt.format_bead_scaffolding(bead)
    assert "TRIAGE VERIFICATION" in out


# --------------------------------------------------------------------------
# build_prompts_for_arming: implementor slim, inspector ALWAYS full
# --------------------------------------------------------------------------


def test_arming_injected_slims_implementor_only():
    bead = _base_bead()
    implementor, inspector = prompt.build_prompts_for_arming(bead, inject_content=True)
    # Acceptance criterion: implementor does NOT repeat the bead's own fields.
    assert bead["description"] not in implementor
    assert bead["design"] not in implementor
    assert "it works" not in implementor
    assert "rework feedback" not in implementor
    # CRITICAL invariant: inspector copy stays FULL (carries the content).
    # Since bead-factory-8u4, format_bead_as_build also renders the ``notes``
    # field, so the inspector (full compose) now surfaces it too -- while the
    # slimmed implementor (asserted above) still omits it.
    assert bead["description"] in inspector
    assert bead["design"] in inspector
    assert "it works" in inspector
    assert "rework feedback" in inspector
    # Both still carry the shared scaffolding.
    assert "BUG DISCOVERY PROTOCOL" in implementor
    assert "BUG DISCOVERY PROTOCOL" in inspector


def test_arming_not_injected_keeps_implementor_full():
    bead = _base_bead()
    implementor, inspector = prompt.build_prompts_for_arming(bead, inject_content=False)
    # No pin -> implementor must still see the whole bead.
    assert bead["description"] in implementor
    assert bead["design"] in implementor
    assert implementor == inspector


# --------------------------------------------------------------------------
# is_pin_active: the gate the arm sites consult
# --------------------------------------------------------------------------


def test_is_pin_active_requires_active_chain_and_bead():
    state.reset()
    assert system_prompt.is_pin_active() is False

    s = state.get_state()
    s.active = True
    s.current_bead = None
    assert system_prompt.is_pin_active() is False, "active but no bead -> no pin"

    s.current_bead = _base_bead()
    assert system_prompt.is_pin_active() is True
