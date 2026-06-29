"""Unit tests for the persistent-memory digest in the build prompt.

Coverage-audit gap FB-6 (``bead_chain-ndt``): bead-chain bridged none of
bd's memory layer, so every bead started cold. ``format_bead_as_build``
now folds ``bd memories`` into a ``## Persistent Memories`` block and the
done-checklist nudges ``bd remember``. These tests pin:

* the pure :func:`prompt._format_memory_digest_block` helper (present /
  absent / capping / truncation / pathological cases),
* the impure :func:`prompt._fetch_memory_digest` soft-fail contract,
* the wiring in :func:`prompt.format_bead_as_build` (placement + the
  ``bd remember`` checklist step),
* the :func:`beads.memories` parser.

Imports go through the registered package (conftest sets it up) because
``prompt.py`` uses relative imports.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import beads, prompt, prompt_blocks  # noqa: E402

_HEADING = "## Persistent Memories"
_CHECKLIST_NUDGE = "bd remember"


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


def _stub_memories(monkeypatch, value):
    """Force ``format_bead_as_build`` to see exactly ``value`` memories."""
    monkeypatch.setattr(prompt, "_fetch_memory_digest", lambda: value)


# --------------------------------------------------------------------------
# _format_memory_digest_block helper (pure)
# --------------------------------------------------------------------------


def test_block_present_renders_each_memory_as_bullet():
    out = prompt._format_memory_digest_block(
        {"auth-jwt": "auth uses JWT not sessions", "race": "run tests with -race"}
    )
    assert out.startswith(_HEADING)
    assert "- auth-jwt: auth uses JWT not sessions" in out
    assert "- race: run tests with -race" in out
    assert out.endswith("\n\n")


def test_block_absent_is_empty():
    assert prompt._format_memory_digest_block({}) == ""


def test_block_non_dict_is_empty():
    assert prompt._format_memory_digest_block(None) == ""  # type: ignore[arg-type]
    assert prompt._format_memory_digest_block(["a", "b"]) == ""  # type: ignore[arg-type]


def test_block_caps_entry_count():
    many = {f"k{i}": f"insight {i}" for i in range(50)}
    out = prompt._format_memory_digest_block(many)
    bullets = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(bullets) == prompt_blocks._MEMORY_DIGEST_MAX_ENTRIES


def test_block_truncates_long_insight():
    long = "x" * 1000
    out = prompt._format_memory_digest_block({"big": long})
    # The excerpt is capped well under the raw length.
    assert "x" * (prompt_blocks._MEMORY_EXCERPT_LIMIT + 50) not in out
    assert "…" in out


def test_block_all_empty_insights_yields_nothing():
    # Whitespace-only values truncate to nothing -> no bullets -> "".
    assert prompt._format_memory_digest_block({"a": "   ", "b": "\n\t"}) == ""


def test_block_preserves_insertion_order():
    out = prompt._format_memory_digest_block({"zzz": "last", "aaa": "first"})
    assert out.index("zzz") < out.index("aaa")


# --------------------------------------------------------------------------
# _fetch_memory_digest soft-fail contract (impure)
# --------------------------------------------------------------------------


def test_fetch_soft_fails_to_empty_on_beads_error(monkeypatch):
    def boom():
        raise beads.BeadsError("bd exploded")

    monkeypatch.setattr(prompt_blocks, "memories", boom)
    assert prompt._fetch_memory_digest() == {}


def test_fetch_passes_through_memories(monkeypatch):
    monkeypatch.setattr(prompt_blocks, "memories", lambda: {"k": "v"})
    assert prompt._fetch_memory_digest() == {"k": "v"}


# --------------------------------------------------------------------------
# format_bead_as_build wiring
# --------------------------------------------------------------------------


def test_build_includes_memory_block_when_memories_exist(monkeypatch):
    _stub_memories(monkeypatch, {"gotcha": "dolt phantom DBs hide in 3 places"})
    out = prompt.format_bead_as_build(_base_bead())
    assert _HEADING in out
    assert "dolt phantom DBs hide in 3 places" in out


def test_build_omits_memory_block_when_no_memories(monkeypatch):
    _stub_memories(monkeypatch, {})
    out = prompt.format_bead_as_build(_base_bead())
    assert _HEADING not in out


def test_memory_block_appears_before_metadata(monkeypatch):
    _stub_memories(monkeypatch, {"k": "v"})
    out = prompt.format_bead_as_build(_base_bead())
    assert out.index(_HEADING) < out.index("Issue metadata:")


def test_memory_block_appears_after_description(monkeypatch):
    _stub_memories(monkeypatch, {"k": "v"})
    out = prompt.format_bead_as_build(_base_bead())
    assert out.index("A thing that must be done.") < out.index(_HEADING)


def test_done_checklist_nudges_bd_remember(monkeypatch):
    _stub_memories(monkeypatch, {})
    out = prompt.format_bead_as_build(_base_bead())
    assert _CHECKLIST_NUDGE in out
    # The nudge sits inside the done-checklist, after the commit step.
    assert out.index("When you believe this is done:") < out.index(_CHECKLIST_NUDGE)


def test_recovery_prompt_still_includes_memory_block(monkeypatch):
    _stub_memories(monkeypatch, {"k": "warm context"})
    out = prompt.format_bead_as_build(_base_bead(), recovery=True)
    assert "RECOVERY MODE" in out
    assert _HEADING in out
    assert "warm context" in out


# --------------------------------------------------------------------------
# beads.memories parser
# --------------------------------------------------------------------------


def test_memories_parses_object_and_strips_metadata(monkeypatch):
    payload = '{"auth-jwt": "uses JWT", "race": "use -race", "schema_version": 1}'
    monkeypatch.setattr(beads, "_run_bd", lambda *a, **k: payload)
    out = beads.memories()
    assert out == {"auth-jwt": "uses JWT", "race": "use -race"}
    assert "schema_version" not in out


def test_memories_drops_non_string_and_empty_values(monkeypatch):
    payload = '{"good": "keep me", "num": 5, "blank": "   ", "list": ["x"]}'
    monkeypatch.setattr(beads, "_run_bd", lambda *a, **k: payload)
    assert beads.memories() == {"good": "keep me"}


def test_memories_empty_output_is_empty_dict(monkeypatch):
    monkeypatch.setattr(beads, "_run_bd", lambda *a, **k: "")
    assert beads.memories() == {}


def test_memories_non_json_raises(monkeypatch):
    monkeypatch.setattr(beads, "_run_bd", lambda *a, **k: "not json {")
    try:
        beads.memories()
    except beads.BeadsError:
        pass
    else:
        raise AssertionError("expected BeadsError on non-JSON output")


def test_memories_non_object_payload_raises(monkeypatch):
    monkeypatch.setattr(beads, "_run_bd", lambda *a, **k: "[1, 2, 3]")
    try:
        beads.memories()
    except beads.BeadsError:
        pass
    else:
        raise AssertionError("expected BeadsError on non-object payload")


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
