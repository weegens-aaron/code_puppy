"""Tests for the bead_factory ``/inspectors`` slash-command handler.

Migrated from the former wiggum plugin's command tests. The standalone
``/bf-goal`` / ``/bf-loop`` / ``/bf-stop`` commands have been retired
(epic bead-factory-ak6), so only the ``/inspectors`` menu handler and the
shared inspector banner helper are exercised here. The command logic lives in
``commands.py``; ``register_callbacks.py`` only wires it to the registry.

Emoji literals are written as ``\\U...`` escapes so the emoji_filter plugin hook
can't strip them out of the source (it rewrites create_file string args).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_puppy.plugins.bead_factory import banner, commands


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
