"""Remote render-capture harness for Code Puppy's terminal output.

Drives a scripted "conversation" through the *real* RichConsoleRenderer against
a Rich Console in record mode, then exports the resulting terminal frame to SVG
(and plain text). This lets rendering changes be observed remotely — no live
LLM, no interactive terminal required. The fixtures below stand in for a real
model stream; swap/extend them to reproduce whatever rendering case you're
working on.

Usage:
    python tools/render_capture.py                # default fixture -> out dir
    python tools/render_capture.py --width 120    # override terminal width
    python tools/render_capture.py --out /some/dir --title "diff case"

Output:
    <out>/frame.svg   Faithful terminal frame (colors, panels, markdown)
    <out>/frame.txt   Plain-text version of the same frame
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

from rich.console import Console

from code_puppy.messaging import rich_renderer as _rr
from code_puppy.messaging.bus import MessageBus
from code_puppy.messaging.messages import (
    AgentReasoningMessage,
    DiffLine,
    DiffMessage,
    MessageLevel,
    ShellOutputMessage,
    ShellStartMessage,
    TextMessage,
)
from code_puppy.messaging.rich_renderer import RichConsoleRenderer


def default_fixture():
    """A representative sequence of messages a real turn would emit.

    Exercises the main render paths: status text, agent thinking, a shell
    command + its output, a file diff, and a markdown agent response.
    """
    return [
        TextMessage(level=MessageLevel.INFO, text="Loaded agent 'code-puppy' 🐶"),
        AgentReasoningMessage(
            reasoning="The user wants a retry helper. I'll add it to http_utils "
            "and wire exponential backoff.",
            next_steps="Edit http_utils.py, then run the tests.",
        ),
        ShellStartMessage(command="pytest tests/test_http_utils.py -q", cwd="/repo"),
        ShellOutputMessage(
            command="pytest tests/test_http_utils.py -q",
            stdout="....\n4 passed in 0.42s",
            stderr="",
            exit_code=0,
            duration_seconds=0.42,
        ),
        DiffMessage(
            path="code_puppy/http_utils.py",
            operation="modify",
            diff_lines=[
                DiffLine(line_number=10, type="context", content="import time"),
                DiffLine(line_number=11, type="add", content="import random"),
                DiffLine(line_number=12, type="context", content=""),
                DiffLine(line_number=13, type="remove", content="def get(url):"),
                DiffLine(
                    line_number=13,
                    type="add",
                    content="def get(url, retries=4):",
                ),
            ],
        ),
        # NOTE: AgentResponseMessage is a deliberate no-op in the renderer —
        # the final assistant text/markdown streams through
        # event_stream_handler.py (termflow) on a separate console. This
        # harness covers the *message renderer* surface; see the module
        # docstring for the streaming path.
        TextMessage(level=MessageLevel.SUCCESS, text="All tests passing ✅"),
    ]


def _force_output_level(level: str) -> None:
    """Pin the renderer's density level without touching global config.

    The renderer imported these getters by name at module load, so patching
    them here controls suppression/collapse without writing to puppy.cfg
    (which may not exist in a fresh/remote container).
    """
    _rr.get_output_level = lambda: level
    if level == "high":
        _rr.get_suppress_informational_messages = lambda: False
        _rr.get_suppress_thinking_messages = lambda: False


def capture(messages, out_dir: Path, width: int, title: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    # record=True captures rendered segments regardless of `file`; route the
    # live echo to a throwaway buffer so running the harness stays quiet.
    console = Console(
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        file=io.StringIO(),
    )
    renderer = RichConsoleRenderer(bus=MessageBus(), console=console)
    for msg in messages:
        renderer._render_sync(msg)

    svg_path = out_dir / "frame.svg"
    txt_path = out_dir / "frame.txt"
    # export_* clears the record buffer by default; keep it so both succeed.
    txt_path.write_text(console.export_text(clear=False), encoding="utf-8")
    svg_path.write_text(
        console.export_svg(title=title, clear=False), encoding="utf-8"
    )
    return svg_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=100, help="terminal columns")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tools/render_out"),
        help="output directory",
    )
    parser.add_argument("--title", default="code-puppy", help="SVG window title")
    parser.add_argument(
        "--output-level",
        default="high",
        choices=["low", "medium", "high"],
        help="density level; 'high' renders every path without suppression",
    )
    args = parser.parse_args()

    _force_output_level(args.output_level)
    svg_path = capture(default_fixture(), args.out, args.width, args.title)
    print(f"Wrote {svg_path}")
    print(f"Wrote {svg_path.with_name('frame.txt')}")


if __name__ == "__main__":
    main()
