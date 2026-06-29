"""Unit tests for FB-8: execution_* metadata hints shaping the build pass.

Coverage-audit gap FB-8 (``bead_chain-9n3``, swarms#2): a bead's
free-form ``metadata`` carries an *unenforced* execution vocabulary
(``execution_effort`` / ``execution_model`` / ``execution_agent_type`` /
``execution_mode`` / ``execution_parallel_group``). bead-chain
historically read none of it. :mod:`execution_hints` now maps the three
serial-compatible keys onto code-puppy's reasoning-effort / model /
agent knobs right before the build loop is armed.

These tests pin:

* the pure :func:`extract_execution_hints` filter (recognized vs unknown
  keys, empty values, string-vs-dict metadata, garbage),
* the soft-failing :func:`apply_execution_hints` orchestrator (calls the
  right config setter, ignores unknown keys, no-ops on absent metadata,
  swallows a setter that rejects a value, re-fetches metadata via
  ``bd show`` when ``bd ready`` omitted it).

Imports go through the registered package (conftest sets it up) because
``execution_hints.py`` uses relative imports.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from code_puppy.plugins.bead_factory import execution_hints  # noqa: E402


# ---------------------------------------------------------------------------
# extract_execution_hints — the pure core
# ---------------------------------------------------------------------------


def test_extract_recognized_keys():
    meta = {
        "execution_effort": "high",
        "execution_model": "gpt-5",
        "execution_agent_type": "code-puppy",
    }
    assert execution_hints.extract_execution_hints(meta) == {
        "execution_effort": "high",
        "execution_model": "gpt-5",
        "execution_agent_type": "code-puppy",
    }


def test_extract_drops_unknown_execution_keys():
    """execution_mode / execution_parallel_group have no serial mapping."""
    meta = {
        "execution_mode": "build",
        "execution_parallel_group": "wave-1",
        "execution_effort": "low",
    }
    assert execution_hints.extract_execution_hints(meta) == {"execution_effort": "low"}


def test_extract_drops_non_execution_keys():
    meta = {"team": "platform", "execution_model": "claude"}
    assert execution_hints.extract_execution_hints(meta) == {
        "execution_model": "claude"
    }


def test_extract_drops_empty_and_whitespace_values():
    meta = {
        "execution_model": "   ",
        "execution_effort": "",
        "execution_agent_type": "x",
    }
    assert execution_hints.extract_execution_hints(meta) == {
        "execution_agent_type": "x"
    }


def test_extract_stringifies_non_string_values():
    meta = {"execution_effort": 3}
    assert execution_hints.extract_execution_hints(meta) == {"execution_effort": "3"}


def test_extract_accepts_json_string_metadata():
    """Some bd builds stringify metadata; accept that shape too."""
    raw = '{"execution_model": "gpt-5", "foo": "bar"}'
    assert execution_hints.extract_execution_hints(raw) == {"execution_model": "gpt-5"}


def test_extract_garbage_and_absent_metadata_is_empty():
    assert execution_hints.extract_execution_hints(None) == {}
    assert execution_hints.extract_execution_hints("") == {}
    assert execution_hints.extract_execution_hints("not json") == {}
    assert execution_hints.extract_execution_hints("[1, 2, 3]") == {}
    assert execution_hints.extract_execution_hints(42) == {}
    assert execution_hints.extract_execution_hints({}) == {}


# ---------------------------------------------------------------------------
# apply_execution_hints — the soft-failing orchestrator
# ---------------------------------------------------------------------------


class _Recorder:
    """Records calls so a test can assert which setter fired with what."""

    def __init__(self):
        self.calls: dict[str, str] = {}

    def make(self, key: str):
        def _setter(value):
            self.calls[key] = value

        return _setter


def _patch_setters(monkeypatch, recorder: _Recorder):
    monkeypatch.setattr(
        execution_hints.config,
        "set_openai_reasoning_effort",
        recorder.make("effort"),
        raising=False,
    )
    monkeypatch.setattr(
        execution_hints.config,
        "set_model_name",
        recorder.make("model"),
        raising=False,
    )
    monkeypatch.setattr(
        execution_hints.config,
        "set_default_agent",
        recorder.make("agent"),
        raising=False,
    )


def test_apply_maps_all_three_recognized_hints(monkeypatch):
    rec = _Recorder()
    _patch_setters(monkeypatch, rec)
    bead = {
        "id": "x-1",
        "metadata": {
            "execution_effort": "high",
            "execution_model": "gpt-5",
            "execution_agent_type": "code-puppy",
        },
    }
    applied = execution_hints.apply_execution_hints(bead)
    assert rec.calls == {"effort": "high", "model": "gpt-5", "agent": "code-puppy"}
    # The returned log strings name each applied knob + value.
    joined = "; ".join(applied)
    assert "reasoning effort -> high" in joined.replace("\u2192", "->")
    assert "model -> gpt-5" in joined.replace("\u2192", "->")
    assert "agent -> code-puppy" in joined.replace("\u2192", "->")


def test_apply_ignores_unknown_keys(monkeypatch):
    rec = _Recorder()
    _patch_setters(monkeypatch, rec)
    bead = {
        "id": "x-2",
        "metadata": {
            "execution_mode": "build",
            "execution_parallel_group": "wave-1",
            "team": "platform",
        },
    }
    assert execution_hints.apply_execution_hints(bead) == []
    assert rec.calls == {}


def test_apply_absent_metadata_no_change(monkeypatch):
    """Acceptance: absent metadata -> no change (and no bd re-fetch)."""
    rec = _Recorder()
    _patch_setters(monkeypatch, rec)

    def _boom(_bead_id):
        raise AssertionError("show() must not be called when metadata is cached")

    monkeypatch.setattr(execution_hints, "show", _boom)
    bead = {"id": "x-3", "metadata": None}
    assert execution_hints.apply_execution_hints(bead) == []
    assert rec.calls == {}


def test_apply_none_and_non_dict_bead(monkeypatch):
    rec = _Recorder()
    _patch_setters(monkeypatch, rec)
    assert execution_hints.apply_execution_hints(None) == []
    assert execution_hints.apply_execution_hints("nope") == []  # type: ignore[arg-type]
    assert rec.calls == {}


def test_apply_refetches_metadata_when_ready_omits_it(monkeypatch):
    """bd ready omits metadata; we re-fetch via bd show keyed on id."""
    rec = _Recorder()
    _patch_setters(monkeypatch, rec)

    def _fake_show(bead_id):
        assert bead_id == "x-4"
        return {"id": "x-4", "metadata": {"execution_model": "from-show"}}

    monkeypatch.setattr(execution_hints, "show", _fake_show)
    # No "metadata" key at all -> mirrors a bd ready record.
    bead = {"id": "x-4", "title": "t"}
    applied = execution_hints.apply_execution_hints(bead)
    assert rec.calls == {"model": "from-show"}
    assert applied  # something was applied


def test_apply_soft_fails_on_bad_value(monkeypatch):
    """A setter that rejects a value is logged + skipped, not raised.

    Other valid hints in the same bead still apply.
    """
    warnings: list[str] = []
    monkeypatch.setattr(
        execution_hints, "emit_warning", lambda msg: warnings.append(msg)
    )

    def _bad_effort(_value):
        raise ValueError("Invalid reasoning effort 'turbo'")

    monkeypatch.setattr(
        execution_hints.config,
        "set_openai_reasoning_effort",
        _bad_effort,
        raising=False,
    )
    rec = _Recorder()
    monkeypatch.setattr(
        execution_hints.config, "set_model_name", rec.make("model"), raising=False
    )
    monkeypatch.setattr(
        execution_hints.config,
        "set_default_agent",
        rec.make("agent"),
        raising=False,
    )

    bead = {
        "id": "x-5",
        "metadata": {"execution_effort": "turbo", "execution_model": "gpt-5"},
    }
    applied = execution_hints.apply_execution_hints(bead)
    # The bad effort was skipped; the good model still landed.
    assert rec.calls == {"model": "gpt-5"}
    assert any("turbo" in w for w in warnings)
    assert len(applied) == 1


def test_apply_missing_setter_is_silent_noop(monkeypatch):
    """If a config setter vanishes (version drift), that hint is skipped."""
    # Remove one setter entirely.
    monkeypatch.delattr(execution_hints.config, "set_model_name", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(
        execution_hints.config,
        "set_openai_reasoning_effort",
        rec.make("effort"),
        raising=False,
    )
    bead = {
        "id": "x-6",
        "metadata": {"execution_model": "gpt-5", "execution_effort": "low"},
    }
    applied = execution_hints.apply_execution_hints(bead)
    assert rec.calls == {"effort": "low"}
    assert len(applied) == 1


def test_apply_softfails_when_show_raises(monkeypatch):
    rec = _Recorder()
    _patch_setters(monkeypatch, rec)

    def _raise(_bead_id):
        raise execution_hints.BeadsError("bd down")

    monkeypatch.setattr(execution_hints, "show", _raise)
    bead = {"id": "x-7"}  # no metadata cached -> triggers re-fetch
    assert execution_hints.apply_execution_hints(bead) == []
    assert rec.calls == {}
