"""Regression tests for puppy-4sy — ejected edits applied via absolute imports.

The bug: a builtin ejected to the user/project tier won the loader's *relative*
imports, but an ABSOLUTE ``import code_puppy.plugins.<name>.x`` (issued by core
or a sibling plugin) still resolved to the pristine wheel copy. So edits to an
ejected plugin that core imports absolutely were silently ignored after restart.

The fix (:func:`alias_ejected_builtins`): after the tiers load, the ejected
copy's already-loaded modules are aliased onto the canonical
``code_puppy.plugins.<name>`` namespace, so EVERY import form resolves to the
live edited copy.
"""

import importlib
import sys

import pytest

from code_puppy.plugins import _load_user_plugins
from code_puppy.plugins.ejected_namespace import alias_ejected_builtins


@pytest.fixture
def clean_modules():
    before = set(sys.modules)
    before_path = list(sys.path)
    yield
    sys.path[:] = before_path
    for name in set(sys.modules) - before:
        if name.startswith(("user_plugins", "project_plugins")) or (
            name.startswith("code_puppy.plugins.") and name.split(".")[2] == "ejtest"
        ):
            sys.modules.pop(name, None)
    # Also drop any aliased canonical names we created.
    for name in set(sys.modules) - before:
        if name.startswith("code_puppy.plugins.ejtest"):
            sys.modules.pop(name, None)


def _make_plugin(plugins_dir, name, sibling_value):
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "sibling.py").write_text(
        f"VALUE = {sibling_value!r}\n", encoding="utf-8"
    )
    # register_callbacks relatively imports the sibling, mirroring a real plugin.
    (plugin_dir / "register_callbacks.py").write_text(
        "from . import sibling\n_LOADED = sibling.VALUE\n", encoding="utf-8"
    )
    return plugin_dir


def test_ejected_builtin_aliased_to_canonical_namespace(tmp_path, clean_modules):
    """An ejected builtin resolves to the edited copy via the canonical path."""
    user_dir = tmp_path / "user_plugins"
    user_dir.mkdir()
    _make_plugin(user_dir, "ejtest", "EDITED-EJECTED")

    loaded = _load_user_plugins(user_dir, skip_names=set())
    assert "ejtest" in loaded

    # 'ejtest' is treated as an ejected builtin.
    aliased = alias_ejected_builtins({"user": loaded, "project": []}, {"ejtest"})
    assert aliased == ["ejtest"]

    # Absolute import (the path core/sibling plugins use) now sees the edit.
    mod = importlib.import_module("code_puppy.plugins.ejtest.sibling")
    assert mod.VALUE == "EDITED-EJECTED"

    # Same object as the tier copy: no duplicate module, no double execution.
    assert (
        sys.modules["code_puppy.plugins.ejtest.register_callbacks"]
        is sys.modules["user_plugins.ejtest.register_callbacks"]
    )


def test_user_authored_plugin_not_aliased(tmp_path, clean_modules):
    """A plugin that is NOT a shipped builtin is left on its tier namespace."""
    user_dir = tmp_path / "user_plugins"
    user_dir.mkdir()
    _make_plugin(user_dir, "ejtest", "mine")

    loaded = _load_user_plugins(user_dir, skip_names=set())
    # builtin_names is empty -> 'ejtest' is purely authored, not an eject.
    aliased = alias_ejected_builtins({"user": loaded, "project": []}, set())

    assert aliased == []
    assert "code_puppy.plugins.ejtest" not in sys.modules
    assert "code_puppy.plugins.ejtest.sibling" not in sys.modules


def test_project_tier_wins_over_user(tmp_path, clean_modules):
    """When both tiers eject the same name, the project copy is the live one."""
    user_dir = tmp_path / "user_plugins"
    user_dir.mkdir()
    proj_dir = tmp_path / "project_plugins"
    proj_dir.mkdir()
    _make_plugin(user_dir, "ejtest", "USER")
    _make_plugin(proj_dir, "ejtest", "PROJECT")

    # Simulate the loader: project loaded under its own namespace; user too
    # (in practice the user copy is skipped, but precedence must hold either way).
    _load_user_plugins(user_dir, skip_names=set())
    # Load the project copy under the project namespace by hand.
    from code_puppy.plugins import _load_project_plugins

    _load_project_plugins(proj_dir, builtin_names=set(), user_names=set())

    aliased = alias_ejected_builtins(
        {"user": ["ejtest"], "project": ["ejtest"]}, {"ejtest"}
    )
    assert aliased == ["ejtest"]

    mod = importlib.import_module("code_puppy.plugins.ejtest.sibling")
    assert mod.VALUE == "PROJECT"


def test_alias_is_best_effort_on_missing_module(clean_modules):
    """A name with no loaded owned package is skipped, never raises."""
    aliased = alias_ejected_builtins({"user": ["never_loaded"], "project": []}, set())
    assert aliased == []
