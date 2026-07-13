"""Capture Code Puppy's interactive prompt_toolkit menus as terminal frames.

Menus (``/set``, model picker, ``/add_model`` …) are prompt_toolkit apps that
repaint with cursor control, so a raw ANSI dump is unusable. This spawns the
real CLI in a pseudo-terminal, drives it to the target menu, then replays the
full output stream through the ``pyte`` terminal emulator to reconstruct the
exact on-screen grid (characters + colors). That snapshot is rendered to SVG
via Rich, matching what a real terminal shows.

Unlike render_capture.py this launches the actual CLI, so it needs the package
installed and a (dummy is fine) model configured; it never makes a network call
because we only open a menu, not run a turn.

Usage:
    python tools/menu_capture.py --command /set
    python tools/menu_capture.py --command /set --rows 40 --cols 110 \
        --keys "down,down" --out tools/render_out_menu
"""

from __future__ import annotations

import argparse
import os
import pathlib
import tempfile
import time

import pexpect
import pyte
from rich.console import Console
from rich.text import Text

_CONFIG = (
    "[puppy]\npuppy_name = Probe\nowner_name = Tester\n"
    "auto_save_session = false\nmodel = dummy\nenable_dbos = false\n"
)
_MODELS = (
    '{"dummy":{"type":"custom_openai","provider":"x","name":"x",'
    '"custom_endpoint":{"url":"http://localhost:1/v1","api_key":"x"},'
    '"context_length":8000}}'
)

# Named keys -> the escape sequence pexpect should send.
_KEYS = {
    "down": "\x1b[B",
    "up": "\x1b[A",
    "left": "\x1b[D",
    "right": "\x1b[C",
    "enter": "\r",
    "tab": "\t",
    "esc": "\x1b",
    "space": " ",
}


def _make_home() -> pathlib.Path:
    home = pathlib.Path(tempfile.mkdtemp(prefix="cp_menu_"))
    (home / ".code_puppy").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "code_puppy").mkdir(parents=True, exist_ok=True)
    (home / ".code_puppy" / "puppy.cfg").write_text(_CONFIG, encoding="utf-8")
    (home / ".config" / "code_puppy" / "puppy.cfg").write_text(
        _CONFIG, encoding="utf-8"
    )
    (home / ".code_puppy" / "extra_models.json").write_text(_MODELS, encoding="utf-8")
    return home


def _spawn(home: pathlib.Path, rows: int, cols: int) -> pexpect.spawn:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CODE_PUPPY_SKIP_TUTORIAL"] = "1"
    env["DBOS_LOG_LEVEL"] = "ERROR"
    env["COLORTERM"] = "truecolor"
    env["TERM"] = "xterm-256color"
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "PYTHONPATH",
    ):
        env.pop(var, None)
    return pexpect.spawn(
        "code-puppy",
        args=["-i"],
        encoding="utf-8",
        timeout=30,
        env=env,
        dimensions=(rows, cols),
    )


def _drain(child: pexpect.spawn, screen: pyte.Screen, stream: pyte.Stream) -> None:
    """Feed everything currently readable from the child into the emulator."""
    while True:
        try:
            chunk = child.read_nonblocking(size=4096, timeout=0.4)
        except (pexpect.TIMEOUT, pexpect.EOF):
            break
        if chunk:
            stream.feed(chunk)


def _pyte_color_to_rich(color: str, *, default: str | None) -> str | None:
    """Map a pyte color token to a Rich color string."""
    if color == "default":
        return default
    if len(color) == 6 and all(c in "0123456789abcdefABCDEF" for c in color):
        return f"#{color}"
    return color  # named ansi color (e.g. 'red', 'brightblue')


def _screen_to_text(screen: pyte.Screen) -> Text:
    """Render a pyte screen buffer to a Rich Text with per-cell styles."""
    out = Text()
    for row in range(screen.lines):
        line = screen.buffer[row]
        for col in range(screen.columns):
            char = line[col]
            style_parts = []
            fg = _pyte_color_to_rich(char.fg, default=None)
            bg = _pyte_color_to_rich(char.bg, default=None)
            if char.reverse:
                fg, bg = bg or "black", fg or "white"
            if fg:
                style_parts.append(fg)
            if bg:
                style_parts.append(f"on {bg}")
            if char.bold:
                style_parts.append("bold")
            if char.italics:
                style_parts.append("italic")
            if char.underscore:
                style_parts.append("underline")
            out.append(char.data or " ", style=" ".join(style_parts) or None)
        out.append("\n")
    return out


def capture_menu(
    command: str, keys: list[str], rows: int, cols: int, out_dir: pathlib.Path
) -> pathlib.Path:
    home = _make_home()
    child = _spawn(home, rows, cols)
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    try:
        child.expect([">>> ", "Enter your coding task", "Interactive Mode"], timeout=30)
        _drain(child, screen, stream)
        child.send(command + "\r")
        time.sleep(1.0)
        _drain(child, screen, stream)
        for key in keys:
            child.send(_KEYS.get(key, key))
            time.sleep(0.3)
            _drain(child, screen, stream)
    finally:
        if child.isalive():
            child.terminate(force=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    import io

    console = Console(
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=cols,
        file=io.StringIO(),
    )
    console.print(_screen_to_text(screen), end="")
    svg_path = out_dir / "frame.svg"
    (out_dir / "frame.txt").write_text("\n".join(screen.display), encoding="utf-8")
    svg_path.write_text(
        console.export_svg(title=f"code-puppy {command}", clear=False),
        encoding="utf-8",
    )
    return svg_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default="/set", help="menu command to open")
    parser.add_argument(
        "--keys",
        default="",
        help="comma-separated keys to send after opening (e.g. down,down,enter)",
    )
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--cols", type=int, default=110)
    parser.add_argument(
        "--out", type=pathlib.Path, default=pathlib.Path("tools/render_out_menu")
    )
    args = parser.parse_args()

    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    svg_path = capture_menu(args.command, keys, args.rows, args.cols, args.out)
    print(f"Wrote {svg_path}")
    print(f"Wrote {svg_path.with_name('frame.txt')}")


if __name__ == "__main__":
    main()
