"""Tests for the bead_factory loop/goal slash-command handlers.

Migrated from the former wiggum plugin's command tests. Clean-break command
names (decision bead-factory-vka): ``/bf-goal`` (+ ``/bf-kibble`` / ``/bf-chow``
aliases), the ``bf_inspector`` banner, and the judges -> inspectors vocabulary.
The command/goal logic lives in ``commands.py`` + ``goal_loop.py``;
``register_callbacks.py`` only wires it to the registry.

Emoji literals are written as ``\\U...`` escapes so the emoji_filter plugin hook
can't strip them out of the source (it rewrites create_file string args).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_puppy.plugins.bead_factory import banner, commands, goal_loop
from code_puppy.plugins.bead_factory import loop_state as state


def _empty_registry():
    """A stand-in inspector registry with nothing configured."""

    class _Reg:
        inspectors: list = []

        def enabled(self):
            return []

    return _Reg()


def test_goal_command_uses_banner_for_activation():
    info_messages: list[str] = []
    with (
        patch.object(banner, "display_banner_message") as mock_banner,
        patch.object(commands, "emit_info", side_effect=info_messages.append),
        patch.object(goal_loop, "emit_info", side_effect=info_messages.append),
        patch.object(goal_loop, "load_inspectors", return_value=_empty_registry()),
        patch.object(state, "start") as mock_start,
    ):
        result = commands.handle_goal_command("/bf-goal say hi")

    assert result == "say hi"
    mock_start.assert_called_once_with("say hi", mode="goal")
    mock_banner.assert_called_once_with(
        "GOAL MODE",
        "\U0001f3af ACTIVATED!",
        banner_name="bf_inspector",
    )
    # Goal: ..., "After each iteration...", plus the inspectors-summary block
    # (no inspectors configured + the /inspectors hint).
    assert any(msg.startswith("Goal:") for msg in info_messages)
    assert any("After each iteration" in msg for msg in info_messages)
    assert any("No inspectors configured" in msg for msg in info_messages)
    assert any("/inspectors" in msg for msg in info_messages)


def test_display_inspector_uses_shared_banner_helper():
    with patch.object(banner, "display_banner_message") as mock_banner:
        banner.display_inspector("done", "notes", final=True)

    mock_banner.assert_called_once_with(
        "INSPECTOR",
        "done",
        banner_name="bf_inspector",
        details="notes",
        final=True,
    )


def test_inspectors_command_invokes_menu():
    class _ExecCtx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn):
            class _F:
                def result(self_inner, timeout=None):
                    fn()
                    return None

            return _F()

    with (
        patch.object(commands, "asyncio") as mock_asyncio,
        patch("concurrent.futures.ThreadPoolExecutor", return_value=_ExecCtx()),
        patch(
            "code_puppy.plugins.bead_factory.inspectors_menu."
            "interactive_inspectors_menu",
            new_callable=MagicMock,
        ) as mock_menu,
    ):
        mock_asyncio.run.return_value = None
        result = commands.handle_inspectors_command("/inspectors")

    assert result is True
    # asyncio.run() was called with the menu coroutine, and the menu function
    # was referenced (it's the thing passed to asyncio.run).
    assert mock_asyncio.run.called
    mock_menu.assert_called_once()


# ---------------------------------------------------------------------------
# /bf-goal aliases: /bf-kibble, /bf-chow
# ---------------------------------------------------------------------------


def test_goal_command_has_puppy_themed_aliases():
    """`/bf-kibble` and `/bf-chow` must resolve to the same CommandInfo as
    `/bf-goal` so behaviour is identical -- if they ever diverge, that's a bug.
    """
    from code_puppy.command_line.command_registry import get_all_commands

    # Importing the plugin entry point triggers register_command side-effects.
    import code_puppy.plugins.bead_factory.register_callbacks  # noqa: F401

    cmds = get_all_commands()
    goal = cmds.get("bf-goal")
    assert goal is not None, "/bf-goal command should be registered"
    assert "bf-kibble" in goal.aliases
    assert "bf-chow" in goal.aliases

    # Both aliases resolve to the SAME CommandInfo object, not a copy.
    assert cmds.get("bf-kibble") is goal
    assert cmds.get("bf-chow") is goal


def test_prompt_extraction_is_command_word_agnostic():
    """extract_prompt drops the first whitespace-delimited token, so the same
    prompt is extracted regardless of which alias the user typed."""
    expected = "make the tests pass for the auth flow"
    assert goal_loop.extract_prompt(f"/bf-goal {expected}") == expected
    assert goal_loop.extract_prompt(f"/bf-kibble {expected}") == expected
    assert goal_loop.extract_prompt(f"/bf-chow {expected}") == expected


def test_usage_hint_mentions_aliases():
    """`/bf-goal` with no prompt advertises the aliases so they're discoverable."""
    captured: list[str] = []

    with (
        patch.object(
            commands, "emit_warning", side_effect=lambda m, *a, **k: captured.append(m)
        ),
        patch.object(
            commands, "emit_info", side_effect=lambda m, *a, **k: captured.append(m)
        ),
    ):
        result = commands.handle_goal_command("/bf-goal")

    assert result is True  # command consumed, no prompt to dispatch
    joined = " ".join(captured)
    assert "/bf-kibble" in joined
    assert "/bf-chow" in joined
