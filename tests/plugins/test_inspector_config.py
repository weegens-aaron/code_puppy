"""Tests for the persisted inspector config registry."""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from unittest.mock import patch

from code_puppy.plugins.bead_factory import inspector_config
from code_puppy.plugins.bead_factory.inspector_config import (
    DEFAULT_INSPECTOR_PROMPT,
    InspectorConfig,
    add_inspector,
    delete_inspector,
    get_enabled_inspectors_or_default,
    load_inspectors,
    save_inspectors,
    toggle_inspector,
    update_inspector,
    validate_name,
    InspectorRegistry,
)


@pytest.fixture
def isolated_inspectors_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "inspectors.json")
        with patch.object(inspector_config, "INSPECTORS_FILE", path):
            yield path


def test_validate_name_accepts_simple_identifiers():
    assert validate_name("abc") is None
    assert validate_name("abc-123") is None
    assert validate_name("abc_def_42") is None


def test_validate_name_rejects_bad_names():
    assert validate_name("") is not None
    assert validate_name("has space") is not None
    assert validate_name("with.dot") is not None
    assert validate_name("x" * 65) is not None  # too long


def test_load_inspectors_returns_empty_when_no_file(isolated_inspectors_file):
    registry = load_inspectors()
    assert isinstance(registry, InspectorRegistry)
    assert registry.inspectors == []


def test_load_inspectors_returns_empty_on_garbage_json(isolated_inspectors_file):
    with open(isolated_inspectors_file, "w", encoding="utf-8") as f:
        f.write("not json {{{")
    assert load_inspectors().inspectors == []


def test_load_inspectors_skips_invalid_entries(isolated_inspectors_file):
    with open(isolated_inspectors_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "inspectors": [
                    {"name": "ok", "model": "x", "prompt": "p", "enabled": True},
                    {"name": "bad name", "model": "x"},  # invalid name
                    {"name": "no-model", "model": ""},  # missing model
                    {"name": "ok", "model": "y"},  # duplicate
                ]
            },
            f,
        )
    registry = load_inspectors()
    assert [j.name for j in registry.inspectors] == ["ok"]


def test_add_and_load_round_trip(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="alpha", model="gpt-5.4", prompt="p1"))
    add_inspector(
        InspectorConfig(name="beta", model="claude", prompt="p2", enabled=False)
    )

    reg = load_inspectors()
    assert reg.names() == ["alpha", "beta"]
    assert reg.find("alpha").model == "gpt-5.4"
    assert reg.find("beta").enabled is False
    assert reg.enabled() == [reg.find("alpha")]


def test_add_inspector_rejects_duplicate(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m"))
    with pytest.raises(ValueError, match="already exists"):
        add_inspector(InspectorConfig(name="x", model="other"))


def test_add_inspector_rejects_invalid_name(isolated_inspectors_file):
    with pytest.raises(ValueError, match="must be"):
        add_inspector(InspectorConfig(name="bad name", model="m"))


def test_add_inspector_rejects_empty_model(isolated_inspectors_file):
    with pytest.raises(ValueError, match="Model must not be empty"):
        add_inspector(InspectorConfig(name="x", model=""))


def test_update_inspector_changes_fields(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m1", prompt="p1", enabled=True))

    update_inspector("x", model="m2", prompt="p2", enabled=False)

    inspector = load_inspectors().find("x")
    assert inspector.model == "m2"
    assert inspector.prompt == "p2"
    assert inspector.enabled is False


def test_update_inspector_rename(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m"))
    update_inspector("x", new_name="y")
    names = load_inspectors().names()
    assert "y" in names and "x" not in names


def test_update_inspector_rename_conflict(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m"))
    add_inspector(InspectorConfig(name="y", model="m"))
    with pytest.raises(ValueError, match="already exists"):
        update_inspector("x", new_name="y")


def test_update_inspector_invalid_rename(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m"))
    with pytest.raises(ValueError, match="must be"):
        update_inspector("x", new_name="bad name")


def test_update_inspector_missing(isolated_inspectors_file):
    with pytest.raises(ValueError, match="No inspector"):
        update_inspector("nope", model="m")


def test_update_inspector_rejects_empty_model(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m"))
    with pytest.raises(ValueError, match="Model must not be empty"):
        update_inspector("x", model="")


def test_update_inspector_resets_empty_prompt_to_default(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m", prompt="custom"))
    update_inspector("x", prompt="")
    assert load_inspectors().find("x").prompt == DEFAULT_INSPECTOR_PROMPT


def test_delete_inspector(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m"))
    add_inspector(InspectorConfig(name="y", model="m"))
    assert delete_inspector("x") is True
    assert load_inspectors().names() == ["y"]
    assert delete_inspector("nope") is False


def test_toggle_inspector(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m", enabled=True))
    assert toggle_inspector("x") is False
    assert load_inspectors().find("x").enabled is False
    assert toggle_inspector("x") is True
    assert load_inspectors().find("x").enabled is True
    assert toggle_inspector("nope") is None


def test_get_enabled_inspectors_or_default_uses_configured(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="a", model="m1"))
    add_inspector(InspectorConfig(name="b", model="m2", enabled=False))

    inspectors = get_enabled_inspectors_or_default("fallback-model")
    assert [j.name for j in inspectors] == ["a"]


def test_get_enabled_inspectors_or_default_falls_back(isolated_inspectors_file):
    # no inspectors configured
    inspectors = get_enabled_inspectors_or_default("fallback-model")
    assert len(inspectors) == 1
    assert inspectors[0].name == "default"
    assert inspectors[0].model == "fallback-model"
    assert inspectors[0].enabled is True
    assert inspectors[0].prompt == DEFAULT_INSPECTOR_PROMPT


def test_get_enabled_inspectors_or_default_falls_back_when_all_disabled(
    isolated_inspectors_file,
):
    add_inspector(InspectorConfig(name="x", model="m", enabled=False))
    inspectors = get_enabled_inspectors_or_default("fallback-model")
    assert len(inspectors) == 1
    assert inspectors[0].name == "default"


def test_save_inspectors_is_atomic(isolated_inspectors_file):
    add_inspector(InspectorConfig(name="x", model="m"))
    # No leftover tmp file
    assert not os.path.exists(isolated_inspectors_file + ".tmp")


def test_save_inspectors_creates_parent_dir(tmp_path):
    nested = tmp_path / "nested" / "deeper" / "inspectors.json"
    with patch.object(inspector_config, "INSPECTORS_FILE", str(nested)):
        save_inspectors(
            InspectorRegistry(inspectors=[InspectorConfig(name="x", model="m")])
        )
        assert nested.exists()
