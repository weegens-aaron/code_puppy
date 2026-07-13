# Remote render-capture harness

Tools for observing Code Puppy's **terminal rendering** without a live LLM or an
interactive terminal — built for working on rendering changes remotely (edit →
capture → view a PNG → adjust). Fixtures stand in for a real model stream, so
captures are deterministic and reproducible.

## Setup (fresh session)

```bash
pip install -e . --ignore-installed PyJWT   # deps; PyJWT flag avoids a distro conflict
pip install pyte pexpect                     # or: install the dev dependency group
```

`playwright` ships as a main dependency; the pre-installed Chromium is found
automatically by `svg_to_png.py` (override with `CHROME_PATH`).

## The three render surfaces

| Surface | Code under test | Tool |
|---|---|---|
| **Message renderer** — banners, tool calls, diffs, reasoning, shell, status | `code_puppy/messaging/rich_renderer.py` | `render_capture.py --surface message` |
| **Streaming assistant text** — markdown/code answer typing out, thinking blocks | `code_puppy/agents/event_stream_handler.py` + `termflow` | `render_capture.py --surface stream` |
| **Interactive menus** — `/set`, model picker, `/add_model`, etc. | `code_puppy/command_line/*` (prompt_toolkit) | `menu_capture.py --command /set` |

> Note: the final assistant response is a **no-op** in the message renderer
> (`rich_renderer.py`) — it streams via `event_stream_handler.py`. Use
> `--surface stream` for that text.

### Comparison capture: fresh vs resumed

`session_render_capture.py` renders one shared conversation through **both** the
live path and the resume path (`display_resumed_history` in
`command_line/autosave_menu.py`) and stitches a labeled before/after PNG. Useful
because the two paths diverge sharply — resume flattens tool calls/diffs to dim
one-liners and merges thinking into the answer block.

```bash
python tools/session_render_capture.py        # -> tools/render_out_session/comparison.png
python tools/session_render_capture.py --no-png   # SVGs only, skip Chromium
```

## Usage

```bash
# Message renderer surface -> tools/render_out/frame.{svg,txt}
python tools/render_capture.py --surface message

# Streaming markdown surface -> tools/render_out/frame.{svg,txt}
python tools/render_capture.py --surface stream

# A menu (drives the real CLI in a pty, snapshots via pyte)
python tools/menu_capture.py --command /set
python tools/menu_capture.py --command /set --keys down,down   # move selection first

# Rasterize any frame.svg to a crisp PNG for phone/remote viewing
python tools/svg_to_png.py tools/render_out/frame.svg
```

Common flags: `--width` (columns), `--out` (output dir), `--output-level
{low,medium,high}` (density; `high` renders every path without suppression).
`menu_capture.py` also takes `--rows`/`--cols` and `--keys` (comma-separated:
`down,up,enter,tab,esc,space`, or any literal characters).

## How to iterate on a rendering change

1. Edit the renderer (e.g. a banner color in `rich_renderer.py`, markdown
   styling in the `termflow` path, or a panel in a `command_line/*` menu).
2. Re-run the matching capture command above.
3. `svg_to_png.py` the resulting `frame.svg` and look at the PNG.
4. Adjust and repeat.

To reproduce a *specific* case, edit the fixtures: `message_fixture()` /
`stream_events()` in `render_capture.py`, or point `menu_capture.py` at a
different `--command`.

## How it works (why these approaches)

- **Message & stream surfaces** render through the real renderers into a Rich
  `Console(record=True)`, exported with `export_svg()`. The stream surface
  additionally re-parses termflow's raw ANSI (termflow writes straight to
  `console.file`, bypassing Rich's record buffer) and disables smooth
  (typewriter) streaming so output is synchronous and deterministic.
- **Menu surface** can't be reconstructed from Rich alone — prompt_toolkit apps
  repaint with cursor control. So the real CLI is spawned in a pty, driven to
  the menu, and its full output stream is replayed through the `pyte` terminal
  emulator to rebuild the exact on-screen grid (characters + colors), which is
  then rendered to SVG. No network call is made — only a menu is opened.

Generated frames land in `tools/render_out*/` and are gitignored.
