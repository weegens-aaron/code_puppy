"""Persistent bottom prompt bar via a terminal scroll region (DECSTBM).

Reserves the bottom rows of the terminal (3 base rows + up to
``PANEL_MAX_ROWS`` sub-agent panel rows):

    rows H-2-n..H-3  sub-agent panel (n = 0..4 rows, via set_panel_lines)
    row  H-2         status line (token/context info, via set_status)
    row  H-1         prompt line  (the always-available input line)
    row  H           blank margin

The scrollable region is rows ``1 .. H-3-n``, so
all existing streaming output — termflow markdown, thinking stream, tool
token-count lines — keeps working unmodified: it simply scrolls *inside*
the region while the reserved rows stay put.

Design rules:

* **NOT Rich Live.** All escape writes go directly to ``sys.__stdout__``
  with an immediate flush; Rich never sees them.
* **TTY-only.** If stdout is not a TTY (pipes, CI, ``-p`` headless mode),
  every method is a silent no-op.
* **Thread-safe.** A single reentrant lock guards all state + writes; the
  SIGWINCH handler runs on the main thread and re-enters safely.
* **Cursor stays inside the region.** After establishing the region the
  cursor is parked at the bottom of the scrollable area so subsequent
  console prints scroll correctly instead of stomping the reserved rows.
  The prompt line therefore renders its own *pseudo* cursor (a
  reverse-video cell) rather than parking the real cursor outside the
  region.
* **Resize.** One unified path: every repaint lazily re-polls the
  terminal size (``_ensure_geometry``) and re-establishes on change.
  POSIX additionally gets a chained SIGWINCH handler that merely
  invalidates the cached geometry so the next repaint picks it up — the
  handler itself never paints (signal-safe by construction).

Suspension mirrors the refcount pattern in
``code_puppy.agents._key_listeners.suspended_key_listener``: wrap any code
that needs the full screen (prompt_toolkit menus, ``ask_user_question``
TUI, shell commands) in :meth:`BottomBar.suspended`.
"""

from __future__ import annotations

import atexit
import logging
import sys
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, TextIO, Tuple

from .bar_painters import PROMPT_MAX_ROWS, BarPainterMixin
from .bar_region import RegionLifecycleMixin
from .bar_rendering import (
    CURSOR_SHOW as _CURSOR_SHOW,
    MODKEYS_OFF as _MODKEYS_OFF,
    PASTE_OFF as _PASTE_OFF,
    default_get_cursor_pos as _default_get_cursor_pos,
    default_get_size as _default_get_size,
    sanitize as _sanitize,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

#: Minimum rows reserved at the bottom of the screen (top margin +
#: prompt). The status row (below the prompt) adds one more, but only
#: while it has content — an empty status row isn't reserved at all.
RESERVED_ROWS = 2

#: Maximum extra rows for the sub-agent panel (above the status row).
PANEL_MAX_ROWS = 4

#: Maximum rows for the completion popup (directly BELOW the prompt).
POPUP_MAX_ROWS = 6

SizeProvider = Callable[[], Tuple[int, int]]
CursorPosProvider = Callable[[], Optional[Tuple[int, int]]]


# =============================================================================
# BottomBar
# =============================================================================


class BottomBar(RegionLifecycleMixin, BarPainterMixin):
    """Scroll-region manager for the persistent bottom prompt.

    Use the module-level singleton via :func:`get_bottom_bar` in app code;
    direct construction is for tests (inject ``stream`` / ``get_size``).
    """

    def __init__(
        self,
        stream: Optional[TextIO] = None,
        get_size: Optional[SizeProvider] = None,
        get_cursor_pos: Optional[CursorPosProvider] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._stream = stream
        self._get_size = get_size or _default_get_size
        self._get_cursor_pos = get_cursor_pos
        self._active = False  # user-facing started state
        self._region_up = False  # is DECSTBM currently in effect?
        self._suspend_depth = 0
        self._rows = 0
        self._cols = 0
        # Bottom row of the reserved band. Docked = screen bottom; the
        # cursor-aware path FLOATS it directly under the content (see
        # bar_region's module docstring). 0 while no region is up.
        self._anchor = 0
        self._status = ""
        self._status_prefix = ""  # animated spinner slot (puppy_spinner)
        self._status_suffix = ""  # trailing slot (steer_queue's '(N queued)')
        self._panel_lines: list[str] = []
        self._popup_lines: list[str] = []  # completion popup (over panel)
        self._popup_selected = -1
        self._reserved = 0  # reserved-row count while the region is up
        self._paste_armed = False  # bracketed paste (ESC[?2004h) state
        self._modkeys_armed = False  # xterm modifyOtherKeys level 1
        self._prompt_prefix = ""
        self._prompt_prefix_sgrs: list[str] = []  # per-char prefix colors
        self._prompt_buffer = ""
        self._prompt_cursor = 0
        self._sigwinch_installed = False
        self._atexit_registered = False
        # DECTCEM state: the hardware cursor is hidden while the region
        # is up (the prompt row paints a reverse-video pseudo-cursor;
        # without hiding, a second "rogue" cursor blinks wherever
        # streaming output last wrote inside the region).
        self._cursor_hidden = False

    # =========================================================================
    # Public API
    # =========================================================================

    def start(self) -> None:
        """Establish the scroll region and paint the reserved rows.

        Idempotent; silent no-op when stdout isn't a TTY.
        """
        if not self._is_tty():
            return
        with self._lock:
            if self._active:
                return
            self._active = True
            if self._suspend_depth == 0:
                self._establish()
        self._install_sigwinch()
        self._register_atexit()

    def stop(self) -> None:
        """Reset the scroll region and clear the reserved rows.

        Fully restores normal terminal state (``ESC[r`` + cleared rows).
        Idempotent; silent no-op when inactive or not a TTY.
        """
        with self._lock:
            if not self._active:
                return
            self._active = False
            if self._region_up:
                self._teardown()

    def is_active(self) -> bool:
        """True between :meth:`start` and :meth:`stop`."""
        with self._lock:
            return self._active

    def set_status(self, text: str) -> None:
        """Update the status line (bottom row, BELOW the prompt).

        Text is control-character-stripped and truncated to the terminal
        width. Cached even while inactive/suspended so the next repaint
        shows the latest value. Setting the first non-empty value
        materializes the row (the region shrinks by one); clearing both
        slots collapses it again.
        """
        with self._lock:
            self._status = text or ""
            self._sync_reserved(self._status_seq)

    def set_status_prefix(self, text: str) -> None:
        """Update the spinner slot painted BEFORE the status text.

        Separate from :meth:`set_status` so an animation (the puppy
        spinner plugin) and the token-context writer never stomp each
        other — each owns its own slot on the shared status row.
        """
        with self._lock:
            self._status_prefix = text or ""
            self._sync_reserved(self._status_seq)

    def set_status_suffix(self, text: str) -> None:
        """Update the trailing slot painted AFTER the status text.

        Third slot on the shared row (prefix=spinner, status=tokens,
        suffix=queue count) — same no-stomping contract as the others.
        """
        with self._lock:
            self._status_suffix = text or ""
            self._sync_reserved(self._status_seq)

    def set_panel_lines(self, lines: Optional[list]) -> None:
        """Set the sub-agent panel rows (above the status row).

        Accepts up to ``PANEL_MAX_ROWS`` rows (extra lines are dropped),
        each either a plain string (sanitized here, painted dim) or a
        ``rich.text.Text`` (kept styled; content sanitized per-segment at
        paint time -- see ``bar_rendering.render_styled_line``).
        An empty list collapses the panel rows and returns them to the
        scroll region. Growing/shrinking re-establishes the region with
        the appropriate row clears so scrollback isn't corrupted.
        While the completion popup is open it takes the panel's place;
        the panel restores automatically when the popup closes.
        """
        from rich.text import Text

        cleaned = [
            line.copy() if isinstance(line, Text) else _sanitize(str(line))
            for line in (lines or [])
        ][:PANEL_MAX_ROWS]
        with self._lock:
            self._panel_lines = cleaned
            self._sync_reserved(self._panel_seq)

    def set_popup_lines(self, lines: Optional[list], selected: int = -1) -> None:
        """Set the completion-popup rows (directly BELOW the prompt block
        — the prompt slides up to make room, IDE-dropdown style).

        Up to ``POPUP_MAX_ROWS`` rows; the ``selected`` index renders in
        the brand accent. While non-empty the popup takes precedence
        over the sub-agent panel (cached and restored on close).
        """
        cleaned = [_sanitize(str(line)) for line in (lines or [])][:POPUP_MAX_ROWS]
        with self._lock:
            self._popup_lines = cleaned
            self._popup_selected = selected
            self._sync_reserved(self._popup_seq)

    def get_panel_lines(self) -> list:
        """Return a copy of the current sub-agent panel lines."""
        with self._lock:
            return list(self._panel_lines)

    def set_prompt_text(
        self, prefix: str, buffer: str, cursor_pos: int, prefix_sgrs=None
    ) -> None:
        """Repaint the prompt row (row ``H-1``) with a visible cursor.

        The *real* terminal cursor must stay inside the scroll region so
        streaming output keeps scrolling correctly, so the cursor is
        rendered as a reverse-video cell at ``cursor_pos`` instead.
        ``prefix_sgrs``: per-char SGR codes for the prefix (out-of-band
        — in-band escapes would be sanitized away).
        """
        with self._lock:
            self._prompt_prefix = prefix or ""
            self._prompt_prefix_sgrs = list(prefix_sgrs or [])
            self._prompt_buffer = buffer or ""
            self._prompt_cursor = max(0, cursor_pos)
            self._sync_reserved(self._prompt_seq)

    def _sync_reserved(self, painter) -> None:
        """Repaint (or resize the reserved area) after a state change.

        Caller holds the lock. ``self._reserved`` is the authoritative
        on-screen count: if the desired total differs, grow/shrink the
        region; otherwise ``painter`` paints just the changed block. A
        geometry change re-establishes everything and needs neither.
        """
        if not self._region_up:
            # Dormant (terminal was too small)? A grown terminal is only
            # noticed here — try to wake. Never while inactive/suspended.
            if not self._active or self._suspend_depth > 0:
                return
            self._ensure_geometry()
            if not self._region_up:
                return
            self._write(painter())
            return
        self._ensure_geometry()  # re-establishes fully on size change
        if not self._region_up:
            return
        if self._reserved != self._total_reserved():
            self._resize_reserved(self._reserved)
        else:
            self._write(painter())

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Reentrant context manager releasing the full screen.

        Resets the region and clears the reserved rows on the outermost
        enter, then re-establishes the region + repaints on the outermost
        exit. Needed around prompt_toolkit menus, the ``ask_user_question``
        TUI, and interactive shell commands.
        """
        if not self._is_tty():
            yield
            return
        with self._lock:
            self._suspend_depth += 1
            if self._suspend_depth == 1 and self._region_up:
                self._teardown()
        try:
            yield
        finally:
            with self._lock:
                self._suspend_depth -= 1
                if self._suspend_depth == 0 and self._active:
                    self._establish()

    # =========================================================================
    # Emergency restore (abnormal exit)
    # =========================================================================

    def _register_atexit(self) -> None:
        """Register the last-resort restore hook once, on first start().

        Mirrors the precedent in ``plugins/theme/osc_palette.py`` — the
        user's terminal must never stay bricked with a stale scroll
        region because Code Puppy died without a clean ``stop()``.
        """
        with self._lock:
            if self._atexit_registered:
                return
            try:
                atexit.register(self._emergency_restore)
                self._atexit_registered = True
            except Exception:
                logger.debug("atexit registration failed", exc_info=True)

    def _emergency_restore(self) -> None:
        """Reset the region + re-show the cursor on abnormal exit.

        Region reset only fires if bookkeeping says it's still up, but
        the cursor-show is UNCONDITIONAL (TTY permitting): a visible
        cursor is always the safe failure mode, even if ``_region_up``
        tracking is somehow off. Never raises — this runs during
        interpreter shutdown.
        """
        try:
            with self._lock:
                if self._region_up:
                    self._teardown()  # includes cursor-show + paste-off
                elif self._is_tty():
                    self._write(_CURSOR_SHOW + _PASTE_OFF + _MODKEYS_OFF)
                    self._cursor_hidden = False
                    self._paste_armed = False
                    self._modkeys_armed = False
        except Exception:
            pass  # interpreter is dying; nothing sane left to do

    # =========================================================================
    # Low-level plumbing
    # =========================================================================

    def _resolve_stream(self) -> Optional[TextIO]:
        if self._stream is not None:
            return self._stream
        return getattr(sys, "__stdout__", None)

    def _is_tty(self) -> bool:
        stream = self._resolve_stream()
        if stream is None:
            return False
        try:
            return bool(stream.isatty())
        except Exception:
            return False

    def _safe_size(self) -> Tuple[int, int]:
        try:
            cols, rows = self._get_size()
            return max(1, int(cols)), max(1, int(rows))
        except Exception:
            return 80, 24

    def _cursor_position(self) -> Optional[Tuple[int, int]]:
        """Transcript cursor ``(row, col)`` — or ``None`` when unknowable.

        Injectable for tests; the default only queries the REAL console
        and only when the bar is actually writing to it — an injected
        stream isn't the console, so tests keep the deterministic blind
        fallback unless they inject a provider.
        """
        if self._get_cursor_pos is not None:
            try:
                return self._get_cursor_pos()
            except Exception:
                return None
        if self._stream is not None:
            return None
        return _default_get_cursor_pos()

    def _write(self, seq: str) -> None:
        """Write escapes straight to the stream with a flush; never raise."""
        if not seq:
            return
        stream = self._resolve_stream()
        if stream is None:
            return
        try:
            stream.write(seq)
            stream.flush()
        except Exception:
            pass


# =============================================================================
# Module-level singleton
# =============================================================================

_bottom_bar: Optional[BottomBar] = None
_bottom_bar_lock = threading.Lock()


def get_bottom_bar() -> BottomBar:
    """Get or lazily create the global BottomBar singleton."""
    global _bottom_bar
    with _bottom_bar_lock:
        if _bottom_bar is None:
            _bottom_bar = BottomBar()
        return _bottom_bar


def reset_bottom_bar() -> None:
    """Reset the global BottomBar (for testing)."""
    global _bottom_bar
    with _bottom_bar_lock:
        if _bottom_bar is not None:
            try:
                _bottom_bar.stop()
            except Exception:
                pass
        _bottom_bar = None


__all__ = [
    "PANEL_MAX_ROWS",
    "POPUP_MAX_ROWS",
    "PROMPT_MAX_ROWS",
    "RESERVED_ROWS",
    "BottomBar",
    "get_bottom_bar",
    "reset_bottom_bar",
]
