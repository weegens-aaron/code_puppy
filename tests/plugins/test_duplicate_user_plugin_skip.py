"""Regression test for the user-tier skip mechanism.

Post E2.1/E2.2 the user tier is suppressed ONLY by the higher-precedence
project tier (user vs project -> project wins). A builtin can no longer
suppress a user plugin — an owned (user) copy now beats the builtin instead.
See ``code_puppy.plugins.precedence`` for the single source of truth.
"""

import sys

from code_puppy.plugins import _load_user_plugins


def test_user_plugin_skipped_when_higher_tier_owns_name(tmp_path):
    """A project-owned name suppresses the same-named user plugin loading."""
    user_plugins_dir = tmp_path / "user_plugins"
    user_plugins_dir.mkdir()

    for plugin_name in ["project_owned", "unique_user_plugin"]:
        plugin_dir = user_plugins_dir / plugin_name
        plugin_dir.mkdir()
        (plugin_dir / "register_callbacks.py").write_text("# User plugin")

    try:
        # The project tier owns "project_owned", so the loader hands it to the
        # user tier as a skip name (project wins on collision).
        loaded = _load_user_plugins(user_plugins_dir, skip_names={"project_owned"})
    finally:
        user_plugins_str = str(user_plugins_dir)
        if user_plugins_str in sys.path:
            sys.path.remove(user_plugins_str)

    assert loaded == ["unique_user_plugin"]
