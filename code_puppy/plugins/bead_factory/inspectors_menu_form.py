"""Add/edit form + display helpers for the goal-inspectors TUI.

Ported into the bead_factory plugin from the core goal-judges menu
(judge -> inspector rename, config repointed at inspectors.json). The
list view, add/edit flows, and main loop live in :mod:`inspectors_menu`.

Original menu help:

A split-panel list (left = inspectors, right = preview), with an in-TUI
form for adding/editing — no $EDITOR popout. Everything happens in
prompt_toolkit, so the UX stays inside the terminal session.

List view keys:
  N           add new inspector (opens form)
  Enter / E   edit selected inspector (opens form)
  T           toggle enabled
  D           delete selected
  Esc / Ctrl+C  close menu

Form view keys:
  Tab / Shift+Tab     cycle between Name ↔ Model ↔ Prompt
  ↑ / ↓               (when Model is focused) select model
  ←→ / PgUp PgDn      (when Model is focused) page through models
  Home / End          (when Model is focused) jump to first / last model
  Ctrl+S              save
  Esc / Ctrl+C        cancel
"""

from __future__ import annotations

import sys
import unicodedata

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea

from code_puppy.command_line.model_picker_completion import load_model_names
from code_puppy.command_line.pagination import (
    ensure_visible_page,
    get_page_bounds,
    get_page_for_index,
    get_total_pages,
)
from code_puppy.messaging import emit_warning
from code_puppy.plugins.bead_factory.inspector_config import (
    DEFAULT_INSPECTOR_PROMPT,
    validate_name,
)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _sanitize(text: str) -> str:
    """Strip characters that mess with prompt_toolkit width calculations."""
    safe = (
        "Lu",
        "Ll",
        "Lt",
        "Lm",
        "Lo",
        "Nd",
        "Nl",
        "No",
        "Pc",
        "Pd",
        "Ps",
        "Pe",
        "Pi",
        "Pf",
        "Po",
        "Zs",
        "Sm",
        "Sc",
        "Sk",
    )
    cleaned = "".join(c for c in text if unicodedata.category(c) in safe)
    return " ".join(cleaned.split())


def _wrap(text: str, width: int) -> list[str]:
    """Crude word-wrap for the preview panel."""
    out: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            out.append("")
            continue
        words = line.split()
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) > width:
                out.append(current)
                current = word
            else:
                current += " " + word
        if current:
            out.append(current)
    return out


# ---------------------------------------------------------------------------
# Model list (inline paginated picker rendered as a tabbable form section)
# ---------------------------------------------------------------------------

MODEL_PAGE_SIZE = 8  # rows of models visible at once in the form section


def _load_available_models() -> list[str]:
    """Return the list of model names, or [] if loading fails."""
    try:
        return load_model_names() or []
    except Exception as exc:
        emit_warning(f"Failed to load models: {exc}")
        return []


def _render_model_list(
    models: list[str],
    selected_idx: int,
    page: int,
    *,
    focused: bool,
) -> list:
    """Render the inline paginated model list with a selection marker."""
    lines: list = []

    if not models:
        lines.append(("fg:yellow", "  No models available."))
        lines.append(("", "\n"))
        lines.append(
            (
                "fg:ansibrightblack",
                "  Configure models first — see /model in the main CLI.",
            )
        )
        return lines

    total_pages = get_total_pages(len(models), MODEL_PAGE_SIZE)
    start, end = get_page_bounds(page, len(models), MODEL_PAGE_SIZE)

    # Header: (Page x/y, focused indicator)
    if focused:
        lines.append(("fg:ansigreen bold", "▼ "))
    else:
        lines.append(("fg:ansibrightblack", "  "))
    lines.append(
        (
            "fg:ansibrightblack",
            f"Page {page + 1}/{max(total_pages, 1)}   "
            f"(↑↓ to move, ←→ / PgUp PgDn to page)\n",
        )
    )

    for i in range(start, end):
        is_sel = i == selected_idx
        name = _sanitize(models[i])
        if is_sel and focused:
            lines.append(("fg:ansigreen bold", "  ▶ "))
            lines.append(("fg:ansigreen bold", name))
        elif is_sel:
            lines.append(("fg:ansiyellow", "  · "))
            lines.append(("fg:ansiyellow", name))
        else:
            lines.append(("", "    "))
            lines.append(("", name))
        lines.append(("", "\n"))

    return lines


# ---------------------------------------------------------------------------
# In-TUI form for add/edit
# ---------------------------------------------------------------------------


class _FormResult:
    """Mutable struct so closures in key bindings can mutate."""

    def __init__(self) -> None:
        self.saved: bool = False
        self.cancelled: bool = False
        self.name: str = ""
        self.model: str = ""
        self.prompt: str = ""


async def _run_inspector_form(
    *,
    title: str,
    initial_name: str = "",
    initial_model: str = "",
    initial_prompt: str = DEFAULT_INSPECTOR_PROMPT,
) -> _FormResult:
    """Render the add/edit form with three tabbable sections:

        1. Name   — single-line TextArea
        2. Model  — inline paginated list (focusable Window)
        3. Prompt — multiline TextArea

    Tab / Shift+Tab cycles between sections. When Model is focused, the
    arrow keys move the selection; ←→ / PgUp PgDn page-jump. Whatever's
    highlighted IS the selected model — no separate "confirm" gesture.
    """
    from prompt_toolkit.filters import Condition

    result = _FormResult()
    status_line = [""]

    # ---- Model list state ----
    models = _load_available_models()
    # Find the index of the initial model, or default to 0.
    try:
        model_idx = [models.index(initial_model)] if initial_model in models else [0]
    except ValueError:
        model_idx = [0]
    model_page = [get_page_for_index(model_idx[0], MODEL_PAGE_SIZE) if models else 0]

    def current_model() -> str:
        if not models:
            return ""
        return models[max(0, min(model_idx[0], len(models) - 1))]

    # ---- Widgets ----
    name_area = TextArea(
        text=initial_name,
        multiline=False,
        wrap_lines=False,
        focusable=True,
        height=1,
    )
    prompt_area = TextArea(
        text=initial_prompt,
        multiline=True,
        wrap_lines=True,
        focusable=True,
        scrollbar=True,
        height=Dimension(min=6, weight=55),
    )

    model_control = FormattedTextControl(
        text="",
        focusable=True,
        show_cursor=False,
    )
    model_window = Window(
        content=model_control,
        wrap_lines=False,
        # +2 rows for the header line and padding.
        height=Dimension(min=MODEL_PAGE_SIZE + 2, max=MODEL_PAGE_SIZE + 2),
    )

    status_control = FormattedTextControl(text="")
    help_control = FormattedTextControl(text="")
    status_window = Window(content=status_control, height=1)
    help_window = Window(content=help_control, height=1)

    # Layout: stacked frames, Name fixed-height, Model fixed-height,
    # Prompt takes the rest of the column.
    model_frame = Frame(model_window, title="Model")
    root = HSplit(
        [
            Frame(name_area, title="Name", height=3),
            model_frame,
            Frame(prompt_area, title="Prompt (multiline)"),
            status_window,
            help_window,
        ]
    )

    # ---- Renderers ----
    def is_model_focused() -> bool:
        try:
            return app.layout.current_window is model_window
        except Exception:
            return False

    def refresh() -> None:
        model_control.text = _render_model_list(
            models,
            model_idx[0],
            model_page[0],
            focused=is_model_focused(),
        )
        if status_line[0]:
            status_control.text = [("fg:ansired", status_line[0])]
        else:
            # Hint at the bottom of the form: show current model selection.
            current = current_model() or "(no models available)"
            status_control.text = [
                ("fg:ansibrightblack", "Selected model: "),
                ("fg:ansiyellow", _sanitize(current)),
            ]
        help_control.text = [
            ("fg:ansibrightblack", "  Tab "),
            ("", "next field    "),
            ("fg:ansigreen", "  ↑↓ "),
            ("", "select model    "),
            ("fg:ansigreen", "  Ctrl+S "),
            ("", "save    "),
            ("fg:ansibrightred", "  Esc/Ctrl+C "),
            ("", "cancel"),
        ]

    # ---- Keybindings ----
    kb = KeyBindings()

    # Cycle order: name → model → prompt → name ...
    _focus_cycle = [name_area, model_window, prompt_area]

    def _focus_index() -> int:
        try:
            cur = app.layout.current_window
        except Exception:
            return 0
        for i, item in enumerate(_focus_cycle):
            target = item.window if hasattr(item, "window") else item
            if cur is target:
                return i
        return 0

    def _focus(item) -> None:
        # TextArea.focus works on the TextArea; Window is focused directly.
        app.layout.focus(item)

    @kb.add("tab")
    def _(event):
        nxt = (_focus_index() + 1) % len(_focus_cycle)
        _focus(_focus_cycle[nxt])
        refresh()

    @kb.add("s-tab")
    def _(event):
        prev = (_focus_index() - 1) % len(_focus_cycle)
        _focus(_focus_cycle[prev])
        refresh()

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        result.cancelled = True
        event.app.exit()

    @kb.add("c-s")
    def _(event):
        name = name_area.text.strip()
        prompt_text = prompt_area.text
        err = validate_name(name)
        if err:
            status_line[0] = err
            refresh()
            return
        chosen_model = current_model()
        if not chosen_model:
            status_line[0] = "No models available — cannot save."
            refresh()
            return
        if not prompt_text.strip():
            status_line[0] = "Prompt cannot be empty."
            refresh()
            return
        result.saved = True
        result.name = name
        result.model = chosen_model
        result.prompt = prompt_text
        event.app.exit()

    # ---- Model-list navigation (only fires when the model window is focused) ----
    model_focused = Condition(is_model_focused)

    @kb.add("up", filter=model_focused)
    def _(event):
        if not models:
            return
        if model_idx[0] > 0:
            model_idx[0] -= 1
            model_page[0] = ensure_visible_page(
                model_idx[0], model_page[0], len(models), MODEL_PAGE_SIZE
            )
            status_line[0] = ""
            refresh()

    @kb.add("down", filter=model_focused)
    def _(event):
        if not models:
            return
        if model_idx[0] < len(models) - 1:
            model_idx[0] += 1
            model_page[0] = ensure_visible_page(
                model_idx[0], model_page[0], len(models), MODEL_PAGE_SIZE
            )
            status_line[0] = ""
            refresh()

    def _page_jump(delta: int) -> None:
        if not models:
            return
        total = get_total_pages(len(models), MODEL_PAGE_SIZE)
        new_page = max(0, min(model_page[0] + delta, total - 1))
        if new_page == model_page[0]:
            return
        model_page[0] = new_page
        # Snap selection to the first item on the new page.
        model_idx[0] = new_page * MODEL_PAGE_SIZE
        status_line[0] = ""
        refresh()

    @kb.add("left", filter=model_focused)
    @kb.add("pageup", filter=model_focused)
    def _(event):
        _page_jump(-1)

    @kb.add("right", filter=model_focused)
    @kb.add("pagedown", filter=model_focused)
    def _(event):
        _page_jump(1)

    @kb.add("home", filter=model_focused)
    def _(event):
        if not models:
            return
        model_idx[0] = 0
        model_page[0] = 0
        status_line[0] = ""
        refresh()

    @kb.add("end", filter=model_focused)
    def _(event):
        if not models:
            return
        model_idx[0] = len(models) - 1
        model_page[0] = get_page_for_index(model_idx[0], MODEL_PAGE_SIZE)
        status_line[0] = ""
        refresh()

    # ---- App ----
    layout = Layout(root)
    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )
    layout.focus(name_area)

    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    refresh()
    await app.run_async()

    return result
