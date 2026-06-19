"""Regression tests for puppy-viu.1.1 — shared namespace-package helper.

These tests prove the acceptance criterion: a user-tier plugin that uses a
relative import (``from . import x``) now loads, because the user tier builds
a real parent package via the shared ``_ensure_plugin_package`` helper — the
same helper the project tier already used. This closes liability L1 (user
plugin relative imports raised ``ModuleNotFoundError``).
"""

import sys

import pytest

from code_puppy.plugins import (
    _ensure_plugin_package,
    _load_user_plugins,
)


@pytest.fixture
def clean_sys_path():
    """Snapshot/restore sys.path and the synthetic namespace packages."""
    before_path = list(sys.path)
    before_mods = set(sys.modules)
    yield
    sys.path[:] = before_path
    for name in set(sys.modules) - before_mods:
        # Drop any synthetic packages we registered during the test so each
        # test starts cold (the loader short-circuits if pkg already present).
        if name.startswith(("user_plugins", "project_plugins")):
            sys.modules.pop(name, None)


def _make_relative_import_plugin(plugins_dir, name: str, marker: str):
    """Create a plugin whose register_callbacks does ``from . import state``."""
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "state.py").write_text(f"VALUE = {marker!r}\n")
    (plugin_dir / "register_callbacks.py").write_text(
        "from . import state\n"
        "import sys\n"
        # Stash proof that the relative import resolved so the test can assert.
        f"sys.modules.setdefault('_viu_proof', type(sys)('_viu_proof'))\n"
        f"sys.modules['_viu_proof'].{name} = state.VALUE\n"
    )
    return plugin_dir


def test_user_plugin_relative_import_loads(tmp_path, clean_sys_path):
    """A user plugin using ``from . import state`` loads without error."""
    user_plugins_dir = tmp_path / "user_plugins"
    user_plugins_dir.mkdir()
    _make_relative_import_plugin(user_plugins_dir, "reltest", marker="user-wins")

    sys.modules.pop("_viu_proof", None)

    loaded = _load_user_plugins(user_plugins_dir)

    assert "reltest" in loaded
    # The relative import actually executed and resolved the sibling module.
    assert sys.modules["_viu_proof"].reltest == "user-wins"
    # The shared helper registered a real parent package for the user tier.
    assert "user_plugins.reltest" in sys.modules


def test_shared_helper_builds_namespace_for_both_tiers(tmp_path, clean_sys_path):
    """One helper builds the parent package for whatever namespace it's given."""
    plug = tmp_path / "shared"
    plug.mkdir()
    (plug / "sibling.py").write_text("X = 1\n")

    # User tier
    assert _ensure_plugin_package("user_plugins", plug, "shared") is False
    assert "user_plugins" in sys.modules
    assert "user_plugins.shared" in sys.modules
    assert sys.modules["user_plugins.shared"].__path__ == [str(plug)]

    # Project tier — same helper, different namespace
    assert _ensure_plugin_package("project_plugins", plug, "shared") is False
    assert "project_plugins.shared" in sys.modules


def test_helper_executes_init_when_present(tmp_path, clean_sys_path):
    """When a plugin ships __init__.py, the helper executes it (returns True)."""
    plug = tmp_path / "withinit"
    plug.mkdir()
    (plug / "__init__.py").write_text("MARKER = 'inited'\n")

    assert _ensure_plugin_package("user_plugins", plug, "withinit") is True
    assert sys.modules["user_plugins.withinit"].MARKER == "inited"
