"""Goal-inspectors TUI: list view, add/edit flows, and main loop.

Part of the bead_factory inspectors menu, ported from the core goal-judges
menu (judge -> inspector rename, config repointed at inspectors.json). The
add/edit form and display helpers live in :mod:`inspectors_menu_form`; this
module owns the split-panel list view, the add/edit orchestration flows, and
the main TUI loop. Split out of a single module purely to honor the 600-line
cap -- the two halves are cohesive (form vs. list-loop).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame

from code_puppy.command_line.pagination import (
    ensure_visible_page,
    get_page_bounds,
    get_page_for_index,
    get_total_pages,
)
from code_puppy.messaging import emit_info, emit_success, emit_warning
from code_puppy.plugins.bead_factory.inspector_config import (
    InspectorConfig,
    add_inspector,
    delete_inspector,
    load_inspectors,
    toggle_inspector,
    update_inspector,
)
from code_puppy.tools.command_runner import set_awaiting_user_input

from .inspectors_menu_form import _run_inspector_form, _sanitize, _wrap

PAGE_SIZE = 10

# ---------------------------------------------------------------------------
# Panel rendering for the list view
# ---------------------------------------------------------------------------


def _render_menu(
    inspectors: list[InspectorConfig],
    page: int,
    selected_idx: int,
) -> list:
    lines = []
    total_pages = get_total_pages(len(inspectors), PAGE_SIZE)
    start, end = get_page_bounds(page, len(inspectors), PAGE_SIZE)

    lines.append(("bold", "Goal Inspectors"))
    lines.append(("fg:ansibrightblack", f" (Page {page + 1}/{max(total_pages, 1)})"))
    lines.append(("", "\n\n"))

    if not inspectors:
        lines.append(("fg:yellow", "  No inspectors configured."))
        lines.append(("", "\n"))
        lines.append(("fg:ansibrightblack", "  Press "))
        lines.append(("fg:ansigreen bold", "N"))
        lines.append(("fg:ansibrightblack", " to add one."))
        lines.append(("", "\n\n"))
    else:
        for i in range(start, end):
            inspector = inspectors[i]
            is_selected = i == selected_idx
            marker = "▶ " if is_selected else "  "
            row_style = "fg:ansigreen bold" if is_selected else ""
            enabled_glyph = "[on] " if inspector.enabled else "[off]"
            enabled_style = (
                "fg:ansigreen" if inspector.enabled else "fg:ansibrightblack"
            )

            lines.append((row_style or "fg:ansigreen", marker))
            lines.append((enabled_style, enabled_glyph + " "))
            lines.append((row_style, _sanitize(inspector.name)))
            lines.append(("fg:ansibrightblack", "  "))
            lines.append(("fg:ansiyellow", _sanitize(inspector.model)))
            lines.append(("", "\n"))

    lines.append(("", "\n"))
    lines.append(("fg:ansibrightblack", "  ↑↓ "))
    lines.append(("", "Navigate\n"))
    lines.append(("fg:ansibrightblack", "  ←→ "))
    lines.append(("", "Page\n"))
    lines.append(("fg:ansigreen", "  N "))
    lines.append(("", "New inspector\n"))
    lines.append(("fg:ansigreen", "  Enter "))
    lines.append(("", "Edit (or E)\n"))
    lines.append(("fg:ansibrightblack", "  T "))
    lines.append(("", "Toggle enabled\n"))
    lines.append(("fg:ansibrightred", "  D "))
    lines.append(("", "Delete\n"))
    lines.append(("fg:ansibrightblack", "  Esc "))
    lines.append(("", "Close (or Ctrl+C)"))
    return lines


def _render_preview(inspector: Optional[InspectorConfig]) -> list:
    lines = []
    lines.append(("dim cyan", " INSPECTOR DETAILS"))
    lines.append(("", "\n\n"))

    if inspector is None:
        lines.append(("fg:yellow", "  No inspector selected."))
        lines.append(("", "\n"))
        return lines

    lines.append(("bold", "Name: "))
    lines.append(("", _sanitize(inspector.name)))
    lines.append(("", "\n\n"))

    lines.append(("bold", "Model: "))
    lines.append(("fg:ansiyellow", _sanitize(inspector.model)))
    lines.append(("", "\n\n"))

    lines.append(("bold", "Enabled: "))
    if inspector.enabled:
        lines.append(("fg:ansigreen", "yes"))
    else:
        lines.append(("fg:ansibrightblack", "no"))
    lines.append(("", "\n\n"))

    lines.append(("bold", "Prompt:"))
    lines.append(("", "\n"))
    for wrapped in _wrap(inspector.prompt or "", width=58):
        lines.append(("fg:ansibrightblack", wrapped or " "))
        lines.append(("", "\n"))

    return lines


# ---------------------------------------------------------------------------
# Add / edit handlers (invoked between TUI sessions)
# ---------------------------------------------------------------------------


async def _add_inspector_flow() -> Optional[str]:
    form = await _run_inspector_form(title="New Inspector")
    if not form.saved:
        emit_info("Cancelled.")
        return None
    try:
        add_inspector(
            InspectorConfig(
                name=form.name,
                model=form.model,
                prompt=form.prompt,
                enabled=True,
            )
        )
    except ValueError as exc:
        emit_warning(str(exc))
        return None
    emit_success(f"Added inspector {form.name!r} → {form.model}")
    return form.name


async def _edit_inspector_flow(current: InspectorConfig) -> Optional[str]:
    form = await _run_inspector_form(
        title=f"Edit Inspector — {current.name}",
        initial_name=current.name,
        initial_model=current.model,
        initial_prompt=current.prompt,
    )
    if not form.saved:
        emit_info("Cancelled.")
        return current.name

    try:
        update_inspector(
            current.name,
            new_name=form.name if form.name != current.name else None,
            model=form.model if form.model != current.model else None,
            prompt=form.prompt if form.prompt != current.prompt else None,
        )
    except ValueError as exc:
        emit_warning(str(exc))
        return current.name
    emit_success(f"Updated inspector {form.name!r}")
    return form.name


# ---------------------------------------------------------------------------
# Main TUI loop
# ---------------------------------------------------------------------------


async def interactive_inspectors_menu() -> None:
    """Open the goal-inspectors TUI. Returns when the user closes the menu."""
    registry = load_inspectors()
    inspectors = list(registry.inspectors)

    selected_idx = [0]
    current_page = [0]
    pending_action: list[Optional[str]] = [None]
    pending_target: list[Optional[str]] = [None]

    def refresh(select_name: Optional[str] = None) -> None:
        nonlocal inspectors
        registry = load_inspectors()
        inspectors = list(registry.inspectors)
        if not inspectors:
            selected_idx[0] = 0
            current_page[0] = 0
            return
        if select_name:
            for i, j in enumerate(inspectors):
                if j.name == select_name:
                    selected_idx[0] = i
                    break
            else:
                selected_idx[0] = min(selected_idx[0], len(inspectors) - 1)
        else:
            selected_idx[0] = min(selected_idx[0], len(inspectors) - 1)
        current_page[0] = get_page_for_index(selected_idx[0], PAGE_SIZE)

    def current_inspector() -> Optional[InspectorConfig]:
        if 0 <= selected_idx[0] < len(inspectors):
            return inspectors[selected_idx[0]]
        return None

    menu_control = FormattedTextControl(text="")
    preview_control = FormattedTextControl(text="")

    def update_display() -> None:
        menu_control.text = _render_menu(inspectors, current_page[0], selected_idx[0])
        preview_control.text = _render_preview(current_inspector())

    menu_window = Window(
        content=menu_control, wrap_lines=False, width=Dimension(weight=40)
    )
    preview_window = Window(
        content=preview_control, wrap_lines=True, width=Dimension(weight=60)
    )
    menu_frame = Frame(menu_window, width=Dimension(weight=40), title="Inspectors")
    preview_frame = Frame(preview_window, width=Dimension(weight=60), title="Preview")
    root = VSplit([menu_frame, preview_frame])

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        if selected_idx[0] > 0:
            selected_idx[0] -= 1
            current_page[0] = ensure_visible_page(
                selected_idx[0], current_page[0], len(inspectors), PAGE_SIZE
            )
            update_display()

    @kb.add("down")
    def _(event):
        if selected_idx[0] < len(inspectors) - 1:
            selected_idx[0] += 1
            current_page[0] = ensure_visible_page(
                selected_idx[0], current_page[0], len(inspectors), PAGE_SIZE
            )
            update_display()

    @kb.add("left")
    def _(event):
        if current_page[0] > 0:
            current_page[0] -= 1
            selected_idx[0] = current_page[0] * PAGE_SIZE
            update_display()

    @kb.add("right")
    def _(event):
        total = get_total_pages(len(inspectors), PAGE_SIZE)
        if current_page[0] < total - 1:
            current_page[0] += 1
            selected_idx[0] = current_page[0] * PAGE_SIZE
            update_display()

    @kb.add("n")
    def _(event):
        pending_action[0] = "add"
        event.app.exit()

    # Enter edits the highlighted inspector — it's the obvious "act on this row"
    # gesture in a list view. 'E' is kept as an alias for muscle memory.
    @kb.add("enter")
    @kb.add("e")
    def _(event):
        inspector = current_inspector()
        if inspector:
            pending_action[0] = "edit"
            pending_target[0] = inspector.name
            event.app.exit()

    @kb.add("t")
    def _(event):
        inspector = current_inspector()
        if inspector:
            pending_action[0] = "toggle"
            pending_target[0] = inspector.name
            event.app.exit()

    @kb.add("d")
    def _(event):
        inspector = current_inspector()
        if inspector:
            pending_action[0] = "delete"
            pending_target[0] = inspector.name
            event.app.exit()

    # Esc and Ctrl+C both close the menu. Esc is the natural "I'm done"
    # gesture; Ctrl+C is the universal escape hatch. eager=False (the
    # default) is fine here because the list view has no Esc-chord, so
    # there's nothing to wait for — prompt_toolkit fires the handler as
    # soon as the chord timeout expires (immediate in practice).
    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        pending_action[0] = "close"
        event.app.exit()

    layout = Layout(root)
    app = Application(
        layout=layout, key_bindings=kb, full_screen=False, mouse_support=False
    )

    set_awaiting_user_input(True)
    sys.stdout.write("\033[?1049h")
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    await asyncio.sleep(0.05)

    try:
        while True:
            pending_action[0] = None
            pending_target[0] = None
            update_display()
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

            await app.run_async()

            action = pending_action[0]
            target = pending_target[0]

            if action in (None, "close", "cancel"):
                break

            if action == "add":
                new_name = await _add_inspector_flow()
                refresh(select_name=new_name)
                continue

            if not target:
                continue

            if action == "edit":
                inspector = next((j for j in inspectors if j.name == target), None)
                if inspector:
                    new_name = await _edit_inspector_flow(inspector)
                    refresh(select_name=new_name or target)
                continue

            if action == "toggle":
                new_state = toggle_inspector(target)
                if new_state is None:
                    emit_warning(f"No inspector named {target!r}.")
                else:
                    emit_info(
                        f"{target!r} is now {'enabled' if new_state else 'disabled'}"
                    )
                refresh(select_name=target)
                continue

            if action == "delete":
                if delete_inspector(target):
                    emit_success(f"Deleted inspector {target!r}")
                else:
                    emit_warning(f"No inspector named {target!r}.")
                refresh()
                continue

    finally:
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()
        set_awaiting_user_input(False)

    emit_info("✓ Exited inspectors menu")
