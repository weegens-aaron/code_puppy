"""Tests for deterministic precedence: an owned copy suppresses the builtin.

Covers puppy-viu.2.1 (E2.1). Previously a same-named builtin and an owned
(user/project) copy BOTH loaded and fired (warn-only collision handling).
Now the owned copy fully suppresses the builtin: the builtin never imports,
never registers, and only ONE copy is the registrant.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import code_puppy.callbacks as callbacks
from code_puppy.plugins import _load_builtin_plugins, _load_user_plugins

# A minimal plugin whose register_callbacks.py registers exactly one startup
# callback, so we can count registrations and attribute ownership to a tier.
_PLUGIN_SRC = (
    "from code_puppy.callbacks import register_callback\n"
    "\n"
    "def _on_startup():\n"
    "    return None\n"
    "\n"
    'register_callback("startup", _on_startup)\n'
)


def _make_plugin(plugins_dir: Path, name: str) -> Path:
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "register_callbacks.py").write_text(_PLUGIN_SRC)
    return plugin_dir


@pytest.fixture(autouse=True)
def _isolation():
    """Keep callbacks/sys.path/sys.modules pristine across tests."""
    callbacks.clear_callbacks("startup")
    sys_path_before = list(sys.path)
    modules_before = set(sys.modules)
    yield
    callbacks.clear_callbacks("startup")
    for entry in sys.path[:]:
        if entry not in sys_path_before:
            sys.path.remove(entry)
    for name in set(sys.modules) - modules_before:
        if name.startswith("user_plugins") or name.startswith("project_plugins"):
            sys.modules.pop(name, None)


def test_builtin_suppressed_when_name_owned(tmp_path):
    """A builtin whose name is in skip_names is never imported."""
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    _make_plugin(builtin_dir, "dup")
    _make_plugin(builtin_dir, "solo")

    with patch("code_puppy.plugins.importlib.import_module") as mock_import:
        loaded = _load_builtin_plugins(builtin_dir, skip_names={"dup"})

    # 'dup' is suppressed (owned copy wins); 'solo' loads normally.
    assert loaded == ["solo"]
    mock_import.assert_called_once_with("code_puppy.plugins.solo.register_callbacks")


def test_no_skip_names_loads_every_builtin(tmp_path):
    """Default (no owned copies) still loads all builtins — no regression."""
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    _make_plugin(builtin_dir, "alpha")
    _make_plugin(builtin_dir, "beta")

    with patch("code_puppy.plugins.importlib.import_module"):
        loaded = _load_builtin_plugins(builtin_dir)

    assert sorted(loaded) == ["alpha", "beta"]


def test_owned_user_copy_is_single_registrant(tmp_path):
    """End-to-end: a user copy of a builtin name is the SOLE registrant.

    The builtin is suppressed (never imported, never registers), and exactly
    one startup callback exists afterward — owned by the user-tier copy.
    """
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    _make_plugin(builtin_dir, "shared_widget")

    user_dir = tmp_path / "user_plugins"
    user_dir.mkdir()
    _make_plugin(user_dir, "shared_widget")

    # The owned (user) tier claims the name, so the builtin is suppressed.
    owned = {"shared_widget"}

    with patch("code_puppy.plugins.importlib.import_module") as mock_import:
        builtin_loaded = _load_builtin_plugins(builtin_dir, skip_names=owned)

    # Builtin never imports -> never registers.
    mock_import.assert_not_called()
    assert builtin_loaded == []

    # The user copy actually loads and registers.
    user_loaded = _load_user_plugins(user_dir, skip_names=set())
    assert user_loaded == ["shared_widget"]

    # Exactly one registration, and it belongs to the owned copy.
    startup_cbs = callbacks.get_callbacks("startup", include_disabled=True)
    assert len(startup_cbs) == 1
    assert callbacks.get_callback_owner(startup_cbs[0]) == "shared_widget"
