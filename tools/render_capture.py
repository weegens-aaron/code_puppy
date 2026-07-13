"""Remote render-capture harness for Code Puppy's terminal output.

Drives scripted, deterministic fixtures through Code Puppy's *real* renderers
and exports the resulting terminal frame to SVG + plain text. This lets terminal
rendering changes be observed remotely — no live LLM, no interactive terminal.
The fixtures stand in for a real model stream ("mock the stream"); swap/extend
them to reproduce whatever rendering case you're working on.

Two render surfaces are covered via ``--surface``:

* ``message`` — the RichConsoleRenderer (rich_renderer.py): banners, tool calls,
  diffs, reasoning, shell output, status panels.
* ``stream``  — the streaming assistant text (event_stream_handler.py + termflow):
  the answer typing out as markdown, plus thinking blocks. Driven by a mocked
  async stream of pydantic-ai part events.

Usage:
    python tools/render_capture.py                       # message surface
    python tools/render_capture.py --surface stream      # streaming markdown
    python tools/render_capture.py --width 120 --out /tmp/frames

Output (in <out>):
    frame.svg   Faithful terminal frame (colors, panels, markdown)
    frame.txt   Plain-text version of the same frame

Rasterize to PNG for phone viewing with the pre-installed Chromium — see
tools/README_render_capture.md.
"""

from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path

from rich.console import Console
from rich.text import Text

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


# =============================================================================
# Shared helpers
# =============================================================================


def _new_console(width: int) -> Console:
    """A truecolor, terminal-forcing console whose output goes to a buffer."""
    return Console(
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        file=io.StringIO(),
    )


def _write_frame(console: Console, out_dir: Path, title: str) -> Path:
    """Export a recording console's frame to SVG + text."""
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / "frame.svg"
    # export_* clears the record buffer by default; keep it so both succeed.
    (out_dir / "frame.txt").write_text(
        console.export_text(clear=False), encoding="utf-8"
    )
    svg_path.write_text(console.export_svg(title=title, clear=False), encoding="utf-8")
    return svg_path


def _write_frame_from_ansi(raw: str, out_dir: Path, width: int, title: str) -> Path:
    """Re-parse a raw ANSI stream into a recording console, then export.

    Needed for the streaming surface: termflow writes ANSI straight to
    ``console.file``, bypassing Rich's record buffer, so we capture the raw
    bytes and re-render them faithfully here.
    """
    console = _new_console(width)
    console.print(Text.from_ansi(raw), end="")
    return _write_frame(console, out_dir, title)


# =============================================================================
# Surface 1: message renderer (rich_renderer.py)
# =============================================================================


def message_fixture():
    """A representative sequence of messages a real turn would emit."""
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
                    line_number=13, type="add", content="def get(url, retries=4):"
                ),
            ],
        ),
        TextMessage(level=MessageLevel.SUCCESS, text="All tests passing ✅"),
    ]


def _force_output_level(level: str) -> None:
    """Pin the renderers' density level without touching global config.

    Both renderer modules imported these getters by name at import time, so
    patching them here controls suppression/collapse without writing to
    puppy.cfg (which may not exist in a fresh/remote container).
    """
    _rr.get_output_level = lambda: level
    if level == "high":
        _rr.get_suppress_informational_messages = lambda: False
        _rr.get_suppress_thinking_messages = lambda: False


def capture_message(out_dir: Path, width: int, title: str) -> Path:
    console = _new_console(width)
    renderer = RichConsoleRenderer(bus=MessageBus(), console=console)
    for msg in message_fixture():
        renderer._render_sync(msg)
    return _write_frame(console, out_dir, title)


# =============================================================================
# Surface 2: streaming assistant text (event_stream_handler.py + termflow)
# =============================================================================


def stream_events():
    """A mocked pydantic-ai part-event stream: a thinking block then markdown.

    Chunked mid-word on purpose so the termflow line-buffering / block
    finalization paths are exercised the same way a live stream hits them.
    """
    from pydantic_ai import PartDeltaEvent, PartEndEvent, PartStartEvent
    from pydantic_ai.messages import (
        TextPart,
        TextPartDelta,
        ThinkingPart,
        ThinkingPartDelta,
    )

    thinking = ["I'll add exponential ", "backoff with jitter ", "to get()."]
    markdown = [
        "## Done\n\n",
        "Added **exponential backoff** to `get()`:\n\n",
        "- Retries up to **4** times\n",
        "- Delays: `2s, 4s, 8s, 16s`\n",
        "- Jitter via `random.uniform`\n\n",
        "```python\n",
        "for attempt in range(retries):\n",
        "    try:\n",
        "        return _do_get(url)\n",
        "    except TransientError:\n",
        "        time.sleep(2 ** attempt)\n",
        "```\n",
    ]

    events = [PartStartEvent(index=0, part=ThinkingPart(content=""))]
    for chunk in thinking:
        events.append(
            PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=chunk))
        )
    events.append(PartEndEvent(index=0, part=ThinkingPart(content="".join(thinking))))

    events.append(PartStartEvent(index=1, part=TextPart(content="")))
    for chunk in markdown:
        events.append(PartDeltaEvent(index=1, delta=TextPartDelta(content_delta=chunk)))
    events.append(PartEndEvent(index=1, part=TextPart(content="".join(markdown))))
    return events


def capture_stream(out_dir: Path, width: int, title: str, level: str) -> Path:
    from code_puppy.agents import event_stream_handler as esh

    # Deterministic capture: disable smooth (typewriter) streaming so output is
    # synchronous, and pin output level so thinking is not suppressed.
    esh.make_smooth_termflow_writer = lambda target: None
    esh.make_thinking_smoother = lambda console: None
    esh.get_output_level = lambda: level

    console = _new_console(width)
    esh.set_streaming_console(console)

    async def _gen():
        for event in stream_events():
            yield event

    asyncio.run(esh.event_stream_handler(None, _gen()))

    # termflow wrote raw ANSI to console.file; re-parse it into a fresh frame.
    raw = console.file.getvalue()
    return _write_frame_from_ansi(raw, out_dir, width, title)


# =============================================================================
# CLI
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--surface",
        default="message",
        choices=["message", "stream"],
        help="which render surface to capture",
    )
    parser.add_argument("--width", type=int, default=100, help="terminal columns")
    parser.add_argument(
        "--out", type=Path, default=Path("tools/render_out"), help="output directory"
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
    if args.surface == "message":
        svg_path = capture_message(args.out, args.width, args.title)
    else:
        svg_path = capture_stream(args.out, args.width, args.title, args.output_level)

    print(f"Wrote {svg_path}")
    print(f"Wrote {svg_path.with_name('frame.txt')}")


if __name__ == "__main__":
    main()
