"""Tests for the plugin_list plugin (/plugins slash command)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from code_puppy.plugins.plugin_list.register_callbacks import (
    _build_output,
    _custom_help,
    _format_plugin_list,
    _handle_custom_command,
)

# Patch targets live on the source module because _build_output() uses
# lazy imports: ``from code_puppy.plugins import …``.
_PLUGINS_MOD = "code_puppy.plugins"
_PLUGINS_CONFIG_MOD = "code_puppy.plugins.config"


# ── Unit tests for helpers ────────────────────────────────────────────────


class TestFormatPluginList:
    def test_empty_list(self):
        assert _format_plugin_list([], set()) == "  (none)"

    def test_single_plugin(self):
        result = _format_plugin_list(["shell_safety"], set())
        assert "shell_safety" in result

    def test_multiple_sorted(self):
        result = _format_plugin_list(["zebra", "alpha", "mid"], set())
        lines = result.split("\n")
        assert len(lines) == 3
        assert "alpha" in lines[0]
        assert "mid" in lines[1]
        assert "zebra" in lines[2]

    def test_disabled_shown(self):
        result = _format_plugin_list(["alpha", "beta"], {"beta"})
        lines = result.split("\n")
        assert "(disabled)" not in lines[0]  # alpha
        assert "(disabled)" in lines[1]  # beta


class TestBuildOutput:
    def test_all_tiers_populated(self):
        loaded = {
            "builtin": ["shell_safety", "agent_skills"],
            "user": ["my_tool"],
            "project": ["repo_guard"],
        }
        with (
            patch(
                f"{_PLUGINS_MOD}.get_loaded_plugins",
                return_value=loaded,
            ),
            patch(
                f"{_PLUGINS_MOD}.get_project_plugins_directory",
                return_value=Path("/tmp/proj/.code_puppy/plugins"),
            ),
            patch(
                f"{_PLUGINS_CONFIG_MOD}.get_disabled_plugins",
                return_value=set(),
            ),
        ):
            output = _build_output()
            assert "Loaded Plugins" in output
            assert "Builtin (" in output
            assert "agent_skills" in output
            assert "shell_safety" in output
            assert "User (~/.code_puppy/plugins/):" in output
            assert "my_tool" in output
            assert "Project (/tmp/proj/.code_puppy/plugins/):" in output
            assert "repo_guard" in output

    def test_empty_tiers_show_none(self):
        loaded = {"builtin": ["one"], "user": [], "project": []}
        with (
            patch(
                f"{_PLUGINS_MOD}.get_loaded_plugins",
                return_value=loaded,
            ),
            patch(
                f"{_PLUGINS_MOD}.get_project_plugins_directory",
                return_value=None,
            ),
            patch(
                f"{_PLUGINS_CONFIG_MOD}.get_disabled_plugins",
                return_value=set(),
            ),
        ):
            output = _build_output()
            lines = output.split("\n")
            user_idx = next(
                i for i, line in enumerate(lines) if line.startswith("User")
            )
            project_idx = next(
                i for i, line in enumerate(lines) if line.startswith("Project")
            )
            assert lines[user_idx + 1].strip() == "(none)"
            assert lines[project_idx + 1].strip() == "(none)"

    def test_project_path_placeholder_when_no_dir(self):
        loaded = {"builtin": [], "user": [], "project": []}
        with (
            patch(
                f"{_PLUGINS_MOD}.get_loaded_plugins",
                return_value=loaded,
            ),
            patch(
                f"{_PLUGINS_MOD}.get_project_plugins_directory",
                return_value=None,
            ),
            patch(
                f"{_PLUGINS_CONFIG_MOD}.get_disabled_plugins",
                return_value=set(),
            ),
        ):
            output = _build_output()
            assert "<CWD>/.code_puppy/plugins/" in output


# ── Slash command tests ───────────────────────────────────────────────────


class TestHandleCustomCommand:
    def test_unrelated_command_returns_none(self):
        assert _handle_custom_command("/foo", "foo") is None
        assert _handle_custom_command("/help", "help") is None

    def test_bare_plugins_launches_tui(self):
        with patch(
            "code_puppy.plugins.plugin_list.plugins_menu.run_plugins_menu",
        ) as mock_menu:
            result = _handle_custom_command("/plugins", "plugins")
            assert result is True
            mock_menu.assert_called_once()

    def test_plugins_list_returns_text(self):
        loaded = {"builtin": ["a"], "user": [], "project": []}
        with (
            patch(
                f"{_PLUGINS_MOD}.get_loaded_plugins",
                return_value=loaded,
            ),
            patch(
                f"{_PLUGINS_MOD}.get_project_plugins_directory",
                return_value=None,
            ),
            patch(
                f"{_PLUGINS_CONFIG_MOD}.get_disabled_plugins",
                return_value=set(),
            ),
            patch(
                "code_puppy.messaging.emit_info",
            ) as mock_emit,
        ):
            result = _handle_custom_command("/plugins list", "plugins")
            assert result is True
            mock_emit.assert_called_once()
            assert "Loaded Plugins" in mock_emit.call_args[0][0]


class TestEjectSubcommands:
    def test_list_ejectable_routes_and_emits(self):
        with patch("code_puppy.messaging.emit_info") as mock_emit:
            result = _handle_custom_command("/plugins list-ejectable", "plugins")
            assert result is True
            mock_emit.assert_called_once()
            # Runs against the real builtin tier -> header is always present.
            assert "Ejectable builtin plugins" in mock_emit.call_args[0][0]

    def test_show_requires_a_name(self):
        with patch("code_puppy.messaging.emit_error") as mock_err:
            result = _handle_custom_command("/plugins show", "plugins")
            assert result is True
            mock_err.assert_called_once()
            assert "Usage" in mock_err.call_args[0][0]

    def test_show_routes_and_emits(self):
        with patch("code_puppy.messaging.emit_info") as mock_emit:
            result = _handle_custom_command("/plugins show plugin_list", "plugins")
            assert result is True
            mock_emit.assert_called_once()
            assert "Plugin: plugin_list" in mock_emit.call_args[0][0]

    def test_unknown_subcommand_mentions_new_commands(self):
        with patch("code_puppy.messaging.emit_error") as mock_err:
            result = _handle_custom_command("/plugins bogus", "plugins")
            assert result is True
            mock_err.assert_called_once()
            msg = mock_err.call_args[0][0]
            assert "list-ejectable" in msg
            assert "show" in msg
            assert "conflicts" in msg
            assert "eject" in msg


class TestEjectActionSubcommand:
    def test_eject_requires_a_name(self):
        with patch("code_puppy.messaging.emit_error") as mock_err:
            result = _handle_custom_command("/plugins eject", "plugins")
            assert result is True
            mock_err.assert_called_once()
            assert "Usage" in mock_err.call_args[0][0]

    def test_eject_routes_and_emits_success(self):
        from code_puppy.plugins.plugin_list.eject import EjectResult

        ok = EjectResult(
            ok=True,
            name="alpha",
            target_tier="user",
            cluster=("alpha",),
            ejected=("alpha",),
            skipped=(),
            message="Ejected 'alpha' to the user tier.",
        )
        with (
            patch(
                "code_puppy.plugins.plugin_list.eject.eject",
                return_value=ok,
            ) as mock_eject,
            patch("code_puppy.messaging.emit_success") as mock_ok,
        ):
            result = _handle_custom_command("/plugins eject alpha", "plugins")
            assert result is True
            mock_eject.assert_called_once_with("alpha", target="user")
            mock_ok.assert_called_once()
            assert "alpha" in mock_ok.call_args[0][0]

    def test_eject_passes_target_tier(self):
        from code_puppy.plugins.plugin_list.eject import EjectResult

        ok = EjectResult(
            ok=True,
            name="alpha",
            target_tier="project",
            cluster=("alpha",),
            ejected=("alpha",),
            skipped=(),
            message="done",
        )
        with (
            patch(
                "code_puppy.plugins.plugin_list.eject.eject",
                return_value=ok,
            ) as mock_eject,
            patch("code_puppy.messaging.emit_success"),
        ):
            _handle_custom_command("/plugins eject alpha project", "plugins")
            mock_eject.assert_called_once_with("alpha", target="project")

    def test_eject_failure_emits_error(self):
        from code_puppy.plugins.plugin_list.eject import EjectResult

        bad = EjectResult(
            ok=False,
            name="ghost",
            target_tier="user",
            message="Plugin 'ghost' not found in any tier.",
        )
        with (
            patch(
                "code_puppy.plugins.plugin_list.eject.eject",
                return_value=bad,
            ),
            patch("code_puppy.messaging.emit_error") as mock_err,
        ):
            result = _handle_custom_command("/plugins eject ghost", "plugins")
            assert result is True
            mock_err.assert_called_once()
            assert "not found" in mock_err.call_args[0][0]


class TestConflictsSubcommand:
    def test_bare_conflicts_lists(self):
        with (
            patch(
                "code_puppy.plugins.plugin_list.conflicts.list_conflicts",
                return_value=[],
            ),
            patch("code_puppy.messaging.emit_info") as mock_emit,
        ):
            result = _handle_custom_command("/plugins conflicts", "plugins")
            assert result is True
            mock_emit.assert_called_once()
            assert "No pending plugin conflicts" in mock_emit.call_args[0][0]

    def test_unknown_conflicts_action_errors(self):
        with patch("code_puppy.messaging.emit_error") as mock_err:
            result = _handle_custom_command("/plugins conflicts bogus", "plugins")
            assert result is True
            mock_err.assert_called_once()
            assert "Unknown conflicts action" in mock_err.call_args[0][0]

    def test_action_requires_a_name(self):
        with patch("code_puppy.messaging.emit_error") as mock_err:
            result = _handle_custom_command(
                "/plugins conflicts accept-upstream", "plugins"
            )
            assert result is True
            mock_err.assert_called_once()
            assert "Usage" in mock_err.call_args[0][0]

    def test_no_matching_conflict_errors(self):
        with (
            patch(
                "code_puppy.plugins.plugin_list.conflicts.find_conflict",
                return_value=[],
            ),
            patch("code_puppy.messaging.emit_error") as mock_err,
        ):
            result = _handle_custom_command(
                "/plugins conflicts keep-mine ghost", "plugins"
            )
            assert result is True
            mock_err.assert_called_once()
            assert "No pending conflict" in mock_err.call_args[0][0]

    def test_accept_upstream_routes_and_emits_success(self):
        from code_puppy.plugins.plugin_list.conflicts import Conflict, ResolveResult

        fake = Conflict(
            name="alpha",
            tier="user",
            ejected_root=Path("/tmp"),
            sidecar_dir=Path("/tmp/.plugin_conflicts/alpha"),
            plugin_dir=Path("/tmp/alpha"),
            current_hash="c",
            upstream_hash="n",
            base_hash="b",
        )
        ok = ResolveResult(True, "accept-upstream", "alpha", "user", "done!")
        with (
            patch(
                "code_puppy.plugins.plugin_list.conflicts.find_conflict",
                return_value=[fake],
            ),
            patch(
                "code_puppy.plugins.plugin_list.conflicts.accept_upstream",
                return_value=ok,
            ) as mock_accept,
            patch("code_puppy.messaging.emit_success") as mock_ok,
        ):
            result = _handle_custom_command(
                "/plugins conflicts accept-upstream alpha", "plugins"
            )
            assert result is True
            mock_accept.assert_called_once_with(fake)
            mock_ok.assert_called_once()
            assert "done!" in mock_ok.call_args[0][0]


class TestCustomHelp:
    def test_returns_plugins_entry(self):
        entries = _custom_help()
        assert len(entries) == 1
        cmd, desc = entries[0]
        assert cmd == "plugins"
        assert "plugin" in desc.lower()
