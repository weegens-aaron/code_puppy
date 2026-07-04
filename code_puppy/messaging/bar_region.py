"""Scroll-region lifecycle for the bottom bar (mixin).

Split out of ``bottom_bar.py`` for the 600-line cap (the precedent is
``bar_painters.BarPainterMixin``): everything here is the DECSTBM
region's establish / resize / teardown machinery. The public surface,
state, and low-level plumbing stay in :class:`BottomBar`, which mixes
this in.

Resize story (one unified path):

* Every repaint lazily re-polls the terminal size (``_ensure_geometry``)
  and re-establishes on change.
* The key-listener idle tick calls :meth:`poll_resize` so resizes are
  noticed WITHOUT repaint traffic — the whole story on Windows (no
  SIGWINCH), and the missing repaint half on POSIX.
* POSIX additionally chains a SIGWINCH handler that merely invalidates
  the cached geometry (signal-safe: it never paints).

Scroll strategy on establish: when the transcript cursor is knowable
(Windows console query — ``bar_rendering.default_get_cursor_pos``), only
the content rows the reserved band would eat are scrolled away, the
cursor is re-parked at the END of the content, and on re-establishes the
transcript is bottom-anchored against the bar (the "resize hug" — see
``_establish``) so a later shrink can't strand it in scrollback. When it
isn't (POSIX, injected test streams), fall back to blindly scrolling by
the full reserved count.
"""

from __future__ import annotations

import logging
import signal
import threading

from .bar_rendering import (
    CLEAR_LINE as _CLEAR_LINE,
    CURSOR_HIDE as _CURSOR_HIDE,
    CURSOR_SHOW as _CURSOR_SHOW,
    MODKEYS_OFF as _MODKEYS_OFF,
    MODKEYS_ON as _MODKEYS_ON,
    PASTE_OFF as _PASTE_OFF,
    PASTE_ON as _PASTE_ON,
    RESET_REGION as _RESET_REGION,
    RESTORE_CURSOR as _RESTORE_CURSOR,
    SAVE_CURSOR as _SAVE_CURSOR,
)

logger = logging.getLogger(__name__)


class RegionLifecycleMixin:
    """Geometry polling + region establish/resize/teardown for ``BottomBar``."""

    # =========================================================================
    # Geometry / resize
    # =========================================================================

    def _ensure_geometry(self) -> None:
        """Re-establish the region if the terminal size changed.

        Called lazily on every repaint — plus periodically from the
        key-listener idle tick via :meth:`poll_resize` (see below).
        """
        cols, rows = self._safe_size()
        if (cols, rows) != (self._cols, self._rows):
            self._establish()

    def poll_resize(self) -> None:
        """Notice a terminal resize at IDLE (no repaint traffic).

        Geometry is re-polled lazily on every repaint, but while nothing
        repaints (idle prompt, no status updates) a resize goes unnoticed
        until the next keypress — the bar lingers painted at the OLD
        bottom (mid-screen after a maximize). The key listener calls this
        on its idle tick: the whole resize story on Windows (no SIGWINCH
        there), and the missing repaint half of it on POSIX (the SIGWINCH
        handler only invalidates; it never paints). Cheap when the size
        is unchanged; never raises.
        """
        try:
            with self._lock:
                if not self._active or self._suspend_depth > 0:
                    return
                self._ensure_geometry()
        except Exception:
            logger.debug("resize poll failed", exc_info=True)

    def _on_resize(self) -> None:
        """SIGWINCH handler body: invalidate cached geometry ONLY.

        The actual re-establish happens on the next repaint via the lazy
        ``_ensure_geometry`` poll — unifying the POSIX and Windows resize
        paths. The handler deliberately does NOT paint: an RLock is
        reentrant on its own thread, so a handler firing mid-``_establish``
        on the main thread could otherwise re-enter it and interleave
        escape writes.
        """
        # Single int store — atomic enough for a poll hint; no lock needed
        # (and taking the RLock here would defeat the point).
        self._cols = -1

    def _install_sigwinch(self) -> None:
        """Chain a SIGWINCH handler — main thread + POSIX only.

        ``signal.signal`` raises ``ValueError`` off the main thread, so
        guard explicitly; resize still works via the lazy repaint poll.
        """
        with self._lock:
            if self._sigwinch_installed:
                return
            if not hasattr(signal, "SIGWINCH"):
                return
            if threading.current_thread() is not threading.main_thread():
                return
            try:
                previous = signal.getsignal(signal.SIGWINCH)

                def _handler(signum, frame):  # pragma: no cover - signal glue
                    try:
                        self._on_resize()
                    except Exception:
                        pass
                    if callable(previous) and previous not in (
                        signal.SIG_DFL,
                        signal.SIG_IGN,
                    ):
                        try:
                            previous(signum, frame)
                        except Exception:
                            pass

                signal.signal(signal.SIGWINCH, _handler)
                self._sigwinch_installed = True
            except Exception:
                # Resize still works via the lazy repaint poll.
                logger.debug("SIGWINCH handler install failed", exc_info=True)

    # =========================================================================
    # Region establish / teardown
    # =========================================================================

    def _establish(self) -> None:
        """Set the scroll region, park the cursor inside it, paint rows."""
        cols, rows = self._safe_size()
        old_rows = self._rows
        old_reserved = self._reserved if self._region_up else 0
        self._cols, self._rows = cols, rows
        reserved = self._total_reserved()
        if rows < reserved + 1:
            # Terminal too small for a region + reserved rows; if one was
            # in effect, put the terminal back to normal and go dormant
            # (hardware cursor comes back too — no region, no pseudo-cursor).
            if self._region_up:
                parts = [_RESET_REGION]
                if self._cursor_hidden:
                    parts.append(_CURSOR_SHOW)
                    self._cursor_hidden = False
                if self._paste_armed:
                    parts.append(_PASTE_OFF)
                    self._paste_armed = False
                if self._modkeys_armed:
                    parts.append(_MODKEYS_OFF)
                    self._modkeys_armed = False
                self._write("".join(parts))
            self._region_up = False
            return
        top = rows - reserved
        # Query BEFORE composing any writes — the erase pass below moves
        # the cursor, and both scroll strategies key off where the
        # transcript content actually ENDS.
        pos = self._cursor_position()
        parts = []
        if old_reserved and old_rows > 0:
            # Re-establish after a resize: the old bar rows were painted
            # at the PREVIOUS geometry and nothing repaints over them —
            # without an explicit erase they linger as ghost duplicates
            # ("multiples of UI elements") at their old positions while
            # the fresh bar paints at the new bottom. Reset the region
            # first so the erases can reach rows outside the incoming
            # one, then blank the old reserved band (clamped to the new
            # screen height). SAVE/RESTORE keeps the transcript cursor
            # safe from the erase repositioning: without it the blind
            # branch below scrolled by ``reserved`` from the LAST CLEARED
            # row on every establish — a drag-resize fired dozens of
            # those and marched the whole transcript out of the window.
            parts.append(_RESET_REGION)
            parts.append(_SAVE_CURSOR)
            for row in range(
                max(1, old_rows - old_reserved + 1), min(old_rows, rows) + 1
            ):
                parts.append(f"\x1b[{row};1H{_CLEAR_LINE}")
            parts.append(_RESTORE_CURSOR)
        if pos is None:
            # Cursor unknowable (POSIX / injected streams): scroll
            # blindly — newlines at the content cursor push the
            # transcript up by ``reserved`` when it sits near the
            # bottom, and may over-scroll when it doesn't (the price of
            # not knowing). Cursor parks at the region bottom.
            parts += [
                # Push existing content up so the reserved rows start blank.
                "\n" * reserved,
                # DECSTBM: scrollable region = rows 1..H-reserved. Homes cursor.
                f"\x1b[1;{top}r",
                # CRITICAL: park the cursor INSIDE the scrollable area so
                # subsequent console prints scroll rather than overwriting
                # the reserved rows.
                f"\x1b[{top};1H",
            ]
        else:
            # Cursor known (Windows console query — see
            # ``default_get_cursor_pos``): scroll ONLY the content rows
            # the reserved band would otherwise eat, and park the cursor
            # back at the END of the content so the next print continues
            # right there — never a blind full-reserved scroll.
            row = min(pos[0], rows)
            col = max(1, min(pos[1], cols))
            overshoot = row - top
            if overshoot > 0:
                # Newlines at the true bottom scroll the overlap into
                # scrollback (CSI S would discard those rows instead).
                parts.append(f"\x1b[{rows};1H" + "\n" * overshoot)
                row = top
            parts.append(f"\x1b[1;{top}r")  # DECSTBM homes the cursor
            if old_reserved and old_rows > 0 and row < top:
                # RESIZE HUG: on a re-establish, bottom-anchor the
                # transcript against the bar. Leaving it top-anchored
                # with a blank band under it is a trap: a later shrink
                # (maximize -> restore) keeps the BOTTOM rows of the
                # active area — the bar and the blank band — and pushes
                # the transcript into scrollback where no escape can
                # reach it (the 'window full of nothing' artifact). SD
                # inserts blanks at the region top and discards the
                # blank gap rows at the region bottom — the transcript
                # itself is never lost, so drag-resizes can fire this
                # any number of times. First establishes skip the hug:
                # a fresh banner stays top-anchored, and the gap only
                # ever sits ABOVE content — visible exactly when there
                # isn't enough content to fill the screen.
                parts.append(f"\x1b[{top - row}T")
                row = top
            parts.append(f"\x1b[{row};{col}H")  # re-park at the content end
        if not self._cursor_hidden:
            # DECTCEM hide: the prompt row renders a pseudo-cursor; the
            # hardware cursor must not blink inside the scroll region.
            parts.insert(0, _CURSOR_HIDE)
            self._cursor_hidden = True
        if not self._paste_armed:
            # Bracketed paste while the bar owns input (Phase B).
            parts.insert(0, _PASTE_ON)
            self._paste_armed = True
        if not self._modkeys_armed:
            # modifyOtherKeys level 1: makes Shift+Enter encodable.
            parts.insert(0, _MODKEYS_ON)
            self._modkeys_armed = True
        self._region_up = True
        self._reserved = reserved
        parts.append(self._reserved_rows_seq())
        self._write("".join(parts))

    def _resize_reserved(self, old_reserved: int) -> None:
        """Grow/shrink the reserved area while the region is up.

        Caller holds the lock and guarantees the terminal size hasn't
        changed (``_ensure_geometry`` ran first). Scrollback-safe:

        * Growing (region shrinks): scroll the region content up by the
          delta first (``CSI S``), so the newly-reserved rows are blank
          instead of eating visible output.
        * Shrinking (region grows): clear the vacated reserved rows so no
          stale panel paint lingers inside the scrollable area.

        CURSOR CONTRACT: the transcript cursor is restored to wherever
        the streaming writer left it (adjusted for the grow-scroll), NOT
        parked at ``top;1``. Parking used to stomp half-typed streaming
        lines: growing scrolls the in-progress line up onto the new
        region bottom, so a blind park landed the cursor at column 1 ON
        that line and the typewriter's next chunk overwrote its head
        (the mangled-response artifact).
        """
        rows = self._rows
        new_reserved = self._total_reserved()
        if rows < new_reserved + 1:
            # Not enough room for the bigger panel — full re-establish
            # handles the dormant transition.
            self._establish()
            return
        parts = [_SAVE_CURSOR]
        delta_up = 0
        if new_reserved > old_reserved:
            # Blank the soon-to-be-reserved rows by scrolling content up.
            delta_up = new_reserved - old_reserved
            parts.append(f"\x1b[{delta_up}S")
        else:
            # Clear rows being returned to the scroll region.
            for row in range(rows - old_reserved + 1, rows - new_reserved + 1):
                parts.append(f"\x1b[{row};1H{_CLEAR_LINE}")
        top = rows - new_reserved
        parts.append(f"\x1b[1;{top}r")  # DECSTBM homes the cursor
        # Restore the transcript cursor (DECSTBM homed it). After a grow
        # the content scrolled up by ``delta_up``, so the cursor must
        # follow its line up (CUU clamps at the region top — safe).
        parts.append(_RESTORE_CURSOR)
        if delta_up:
            parts.append(f"\x1b[{delta_up}A")
        self._reserved = new_reserved
        parts.append(self._reserved_rows_seq())
        self._write("".join(parts))

    def _teardown(self) -> None:
        """Reset to a full-screen region and clear the reserved rows.

        Re-polls the terminal size: the cached geometry can be stale if
        the terminal was resized while suspended (or right before stop),
        and clearing rows computed from stale height either misses the
        real reserved rows or clears mid-screen content.
        """
        rows = self._safe_size()[1]
        reserved = self._reserved or self._total_reserved()
        parts = [_RESET_REGION]
        for row in range(max(1, rows - reserved + 1), rows + 1):
            parts.append(f"\x1b[{row};1H{_CLEAR_LINE}")
        parts.append(f"\x1b[{max(1, rows - reserved + 1)};1H")
        if self._cursor_hidden:
            # Give the hardware cursor back — every exit path (stop,
            # suspend-enter, dormant, emergency) funnels through here.
            parts.append(_CURSOR_SHOW)
            self._cursor_hidden = False
        if self._paste_armed:
            parts.append(_PASTE_OFF)
            self._paste_armed = False
        if self._modkeys_armed:
            parts.append(_MODKEYS_OFF)
            self._modkeys_armed = False
        self._write("".join(parts))
        self._region_up = False
        self._reserved = 0


__all__ = ["RegionLifecycleMixin"]
