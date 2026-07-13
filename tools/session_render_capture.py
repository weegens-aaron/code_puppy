"""Capture the fresh-vs-resumed rendering difference.

The same conversation renders through two different code paths:

* **Fresh / live** — thinking via the streaming handler's THINKING banner, tool
  activity via the message renderer's rich banners + colored diff, and the
  answer via termflow streaming (event_stream_handler.py + rich_renderer.py).
* **Resumed** — replayed by ``display_resumed_history`` (command_line/autosave_menu.py):
  tool activity collapses to flat dim ``Tool Call:`` / ``Tool Result:`` lines
  (the diff is gone, results truncated), thinking merges into the answer's
  markdown block, user turns gain a ``> **bold**`` banner, and the whole thing
  is wrapped in ``Session Resumed`` rules.

This drives ONE shared scenario through both paths, exports each frame, and (if
Pillow is available) stitches them into a single labeled comparison PNG.

Usage:
    python tools/session_render_capture.py
    python tools/session_render_capture.py --width 100 --out tools/render_out_session
"""

from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path

from rich.console import Console
from rich.text import Text

# ---- shared scenario --------------------------------------------------------

USER_TEXT = "add exponential backoff retry to get()"
THINKING_TEXT = (
    "The user wants a retry helper. I'll wrap get() with exponential backoff "
    "and jitter, then edit http_utils.py."
)
ANSWER_MD = (
    "## Done\n\n"
    "Added **exponential backoff** to `get()`:\n\n"
    "- Retries up to **4** times\n"
    "- Delays: `2s, 4s, 8s, 16s`\n"
    "- Jitter via `random.uniform`\n"
)
DIFF_LINES = [
    ("context", "import time"),
    ("add", "import random"),
    ("context", ""),
    ("remove", "def get(url):"),
    ("add", "def get(url, retries=4):"),
]


def _new_console(width: int) -> Console:
    return Console(
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        file=io.StringIO(),
    )


def _frame_from_ansi(raw: str, out_dir: Path, width: int, title: str) -> Path:
    console = _new_console(width)
    console.print(Text.from_ansi(raw), end="")
    out_dir.mkdir(parents=True, exist_ok=True)
    svg = out_dir / "frame.svg"
    (out_dir / "frame.txt").write_text(console.export_text(clear=False), "utf-8")
    svg.write_text(console.export_svg(title=title, clear=False), "utf-8")
    return svg


# ---- history for the resume path -------------------------------------------


def _build_history():
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        ThinkingPart,
        ToolCallPart,
        ToolReturnPart,
    )
    from pydantic_ai.messages import (
        UserPromptPart,
    )

    diff_text = "\n".join(
        ("+ " if t == "add" else "- " if t == "remove" else "  ") + c
        for t, c in DIFF_LINES
    )
    return [
        ModelRequest(parts=[SystemPromptPart(content="You are Code Puppy.")]),
        ModelRequest(parts=[UserPromptPart(content=USER_TEXT)]),
        ModelResponse(
            parts=[
                ThinkingPart(content=THINKING_TEXT),
                ToolCallPart(
                    tool_name="edit_file",
                    args={"path": "code_puppy/http_utils.py", "op": "modify"},
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="edit_file",
                    content=f"Modified code_puppy/http_utils.py\n{diff_text}",
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content=ANSWER_MD)]),
    ]


# ---- fresh / live capture ---------------------------------------------------


def _run_stream(console: Console, parts) -> None:
    """Render (kind, text) parts through the real streaming handler."""
    from pydantic_ai import PartDeltaEvent, PartEndEvent, PartStartEvent
    from pydantic_ai.messages import (
        TextPart,
        TextPartDelta,
        ThinkingPart,
        ThinkingPartDelta,
    )

    from code_puppy.agents import event_stream_handler as esh

    esh.make_smooth_termflow_writer = lambda target: None
    esh.make_thinking_smoother = lambda console: None
    esh.get_output_level = lambda: "high"
    esh.set_streaming_console(console)

    events = []
    for index, (kind, text) in enumerate(parts):
        if kind == "thinking":
            events.append(PartStartEvent(index=index, part=ThinkingPart(content="")))
            events.append(
                PartDeltaEvent(index=index, delta=ThinkingPartDelta(content_delta=text))
            )
            events.append(PartEndEvent(index=index, part=ThinkingPart(content=text)))
        else:
            events.append(PartStartEvent(index=index, part=TextPart(content="")))
            for chunk in text.splitlines(keepends=True):
                events.append(
                    PartDeltaEvent(
                        index=index, delta=TextPartDelta(content_delta=chunk)
                    )
                )
            events.append(PartEndEvent(index=index, part=TextPart(content=text)))

    async def _gen():
        for event in events:
            yield event

    asyncio.run(esh.event_stream_handler(None, _gen()))


def capture_live(out_dir: Path, width: int) -> Path:
    from code_puppy.messaging import rich_renderer as _rr
    from code_puppy.messaging.bus import MessageBus
    from code_puppy.messaging.messages import DiffLine, DiffMessage
    from code_puppy.messaging.rich_renderer import RichConsoleRenderer

    _rr.get_output_level = lambda: "high"
    console = _new_console(width)

    # The user's prompt echoes plainly (no banner in live chat).
    console.print(f"[dim]> [/dim]{USER_TEXT}")

    # Thinking streams under a THINKING banner.
    _run_stream(console, [("thinking", THINKING_TEXT)])

    # The edit tool renders a rich EDIT FILE banner + colored diff.
    renderer = RichConsoleRenderer(bus=MessageBus(), console=console)
    renderer._render_sync(
        DiffMessage(
            path="code_puppy/http_utils.py",
            operation="modify",
            diff_lines=[
                DiffLine(line_number=i + 10, type=t, content=c)
                for i, (t, c) in enumerate(DIFF_LINES)
            ],
        )
    )

    # The answer streams under an AGENT RESPONSE banner via termflow.
    _run_stream(console, [("text", ANSWER_MD)])

    return _frame_from_ansi(console.file.getvalue(), out_dir, width, "fresh session")


# ---- resumed capture --------------------------------------------------------


def capture_resume(out_dir: Path, width: int) -> Path:
    import rich.console as _rc

    from code_puppy.command_line.autosave_menu import display_resumed_history

    rec = _new_console(width)
    original = _rc.Console
    # display_resumed_history does `console = Console()` internally; hand it our
    # recording console so we can capture (and keep colors, which a non-tty
    # default Console would strip).
    _rc.Console = lambda *a, **k: rec
    try:
        display_resumed_history(_build_history(), num_messages=10)
    finally:
        _rc.Console = original

    return _frame_from_ansi(rec.file.getvalue(), out_dir, width, "resumed session")


# ---- stitch -----------------------------------------------------------------


_FONT_PATHS = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _label_font(size: int):
    from PIL import ImageFont

    for path in _FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _stitch(live_png: Path, resume_png: Path, out_png: Path) -> bool:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return False

    label_h, pad, gap = 60, 20, 28
    font = _label_font(34)
    live, resume = Image.open(live_png), Image.open(resume_png)
    width = max(live.width, resume.width) + pad * 2
    height = (label_h + live.height) + gap + (label_h + resume.height) + pad * 2
    canvas = Image.new("RGB", (width, height), (13, 13, 16))
    draw = ImageDraw.Draw(canvas)

    y = pad
    for label, color, img in (
        ("● FRESH SESSION — live render", (120, 200, 255), live),
        ("● RESUMED SESSION — display_resumed_history", (200, 160, 255), resume),
    ):
        draw.text((pad, y + 12), label, fill=color, font=font)
        y += label_h
        canvas.paste(img, (pad, y))
        y += img.height + gap
    canvas.save(out_png)
    return True


def _rasterize(svg: Path) -> Path:
    """Rasterize an SVG to PNG using tools/svg_to_png.py's Chromium helper."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from svg_to_png import _find_chrome

    from playwright.sync_api import sync_playwright

    png = svg.with_suffix(".png")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_find_chrome())
        page = browser.new_page(device_scale_factor=2)
        page.goto(svg.resolve().as_uri())
        page.query_selector("svg").screenshot(path=str(png))
        browser.close()
    return png


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=98)
    parser.add_argument("--out", type=Path, default=Path("tools/render_out_session"))
    parser.add_argument(
        "--no-png",
        action="store_true",
        help="only write SVGs; skip Chromium rasterize + stitch",
    )
    args = parser.parse_args()

    live_svg = capture_live(args.out / "live", args.width)
    resume_svg = capture_resume(args.out / "resume", args.width)
    print(f"Wrote {live_svg}")
    print(f"Wrote {resume_svg}")

    if args.no_png:
        return
    live_png = _rasterize(live_svg)
    resume_png = _rasterize(resume_svg)
    comparison = args.out / "comparison.png"
    if _stitch(live_png, resume_png, comparison):
        print(f"Wrote {comparison}")
    else:
        print("Pillow unavailable; wrote separate PNGs only")


if __name__ == "__main__":
    main()
