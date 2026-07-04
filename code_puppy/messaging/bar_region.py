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

Placement strategy (the "floating bar"): when the transcript cursor is
knowable (Windows console query — ``bar_rendering.default_get_cursor_pos``)
the reserved band anchors DIRECTLY UNDER the content and descends as the
transcript grows, docking at the true bottom once content fills the
screen. Why: any blank band between content and a bar painted at the
far bottom is a trap — a shrink (maximize -> restore) keeps the bottom
rows of the active area and strands the transcript in scrollback, while
scrolling the content down to close the gap (the earlier "resize hug")
inserts blank bands that permanently fracture scrollback. With the band
hugging the content from below, everything under it stays blank, and
terminals trim trailing blanks on shrink — resizes keep the transcript
visible AND contiguous. When the cursor isn't knowable (POSIX, injected
test streams), fall back to the classic bottom-docked bar with blind
scrolling by the full reserved count.
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

#: Blank runway kept between the content end and the floating band, so
#: bursts of output have room to print before the descent tick moves the
#: band down (region-bottom scrolling while free rows sit below the bar
#: would look broken). Small enough that a shrink losing ONLY runway
#: rows to the trailing-blank trim never clips content.
FLOAT_HEADROOM = 8


class RegionLifecycleMixin:
    """Geometry polling + region establish/resize/teardown for ``BottomBar``."""

    # =========================================================================
    # Geometry / resize
    # =========================================================================

    def _ensure_geometry(self) -> None:
        """Re-establish on terminal resize OR when the bar must descend.

        Called lazily on every repaint — plus periodically from the
        key-listener idle tick via :meth:`poll_resize` (see below).
        Descent: while the bar floats above the true bottom, content
        reaching the region bottom means it needs room — re-establish
        moves the band down (see ``_establish``'s anchor math). The
        cursor query is a cheap kernel call and only fires while
        actually floating (never on POSIX — no query there, no float).
        """
        cols, rows = self._safe_size()
        if (cols, rows) != (self._cols, self._rows):
            self._establish()
            return
        if self._region_up and 0 < self._anchor < rows:
            pos = self._cursor_position()
            if pos is not None and pos[0] >= self._anchor - self._reserved:
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
            self._anchor = 0
            return
        old_anchor = (
            min(self._anchor, rows) if (self._region_up and self._anchor) else 0
        )
        # Query BEFORE composing any writes — the erase pass below moves
        # the cursor, and the anchor math keys off where the transcript
        # content actually ENDS.
        pos = self._cursor_position()
        parts = []
        if pos is None:
            # Cursor unknowable (POSIX / injected streams): classic
            # bottom-docked bar with a blind scroll — newlines at the
            # content cursor push the transcript up by ``reserved`` when
            # it sits near the bottom, and may over-scroll when it
            # doesn't (the price of not knowing).
            anchor = rows
            top = anchor - reserved
            if old_reserved and old_rows > 0:
                # Erase the old band at its recorded position (clamped to
                # the new height) so it can't linger as a ghost. SAVE/
                # RESTORE keeps the transcript cursor safe from the erase
                # repositioning: without it this branch scrolled by
                # ``reserved`` from the LAST CLEARED row on every
                # establish — a drag-resize fired dozens of those and
                # marched the whole transcript out of the window.
                band = old_anchor or old_rows
                parts.append(_RESET_REGION)
                parts.append(_SAVE_CURSOR)
                for row in range(max(1, band - old_reserved + 1), min(band, rows) + 1):
                    parts.append(f"\x1b[{row};1H{_CLEAR_LINE}")
                parts.append(_RESTORE_CURSOR)
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
            # Cursor known (Windows console query): FLOAT the band right
            # under the content. The anchor (band's bottom row) never
            # rises within a session and grows by ``reserved`` when the
            # content has filled its region (the descent), docking at
            # the true bottom for long transcripts. No blank band ever
            # sits between content and bar, so shrinks can't strand the
            # transcript (terminals trim trailing blanks) and nothing
            # needs to scroll content around (no scrollback fractures).
            row = min(pos[0], rows)
            col = max(1, min(pos[1], cols))
            desired = row + reserved + FLOAT_HEADROOM
            if old_anchor and old_reserved:
                # Never lift the band off rows it already owns — the
                # anchor is monotonic within a session (only a smaller
                # terminal forces it back up, via the min() below).
                desired = max(desired, old_anchor)
            anchor = min(rows, desired)
            top = anchor - reserved
            if old_reserved and old_rows > 0:
                # Erase the old band. The float invariant keeps it a
                # fixed distance under the content (runway + band), so
                # erase RELATIVE TO THE QUERIED CURSOR — stored
                # coordinates go stale when the terminal reveals
                # scrollback on a grow; the cursor never does. The span
                # also stretches to the recorded anchor for band
                # shrinks; over-erased rows are runway blanks or get
                # repainted below.
                erase_to = min(
                    rows,
                    max(old_anchor, row + old_reserved + FLOAT_HEADROOM),
                )
                parts.append(_RESET_REGION)
                parts.append(_SAVE_CURSOR)
                for r in range(row + 1, erase_to + 1):
                    parts.append(f"\x1b[{r};1H{_CLEAR_LINE}")
                parts.append(_RESTORE_CURSOR)
            overshoot = row - top
            if overshoot > 0:
                # Content taller than the docked region: newlines at the
                # true bottom scroll the overlap into scrollback (CSI S
                # would discard those rows instead).
                parts.append(f"\x1b[{rows};1H" + "\n" * overshoot)
                row = top
            parts += [
                f"\x1b[1;{top}r",  # DECSTBM homes the cursor…
                f"\x1b[{row};{col}H",  # …so re-park at the content end.
            ]
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
        self._anchor = anchor
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
        old_anchor = min(self._anchor or rows, rows)
        floating = old_anchor < rows
        parts = [_SAVE_CURSOR]
        delta_up = 0
        if floating:
            # Floating band: keep the region top FIXED (the band stays
            # glued to the content) and move the band's BOTTOM edge —
            # growth extends into the blank rows below (no scroll at
            # all); shrink vacates the band's own bottom rows.
            top = old_anchor - old_reserved
            anchor = top + new_reserved
            if anchor > rows:
                # Ran out of blank rows below — scroll the region up for
                # the remainder, exactly like the docked path.
                delta_up = anchor - rows
                parts.append(f"\x1b[{delta_up}S")
                anchor = rows
                top = anchor - new_reserved
            elif new_reserved < old_reserved:
                for row in range(anchor + 1, old_anchor + 1):
                    parts.append(f"\x1b[{row};1H{_CLEAR_LINE}")
        else:
            # Docked band (classic): bottom edge stays at the screen
            # bottom; the region's top edge gives/takes the rows.
            anchor = rows
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
        self._anchor = anchor
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
        anchor = min(self._anchor or rows, rows)
        band_top = max(1, anchor - reserved + 1)
        parts = [_RESET_REGION]
        for row in range(band_top, anchor + 1):
            parts.append(f"\x1b[{row};1H{_CLEAR_LINE}")
        # Park where the band was — right under the content when the
        # band floats, so e.g. the shell prompt on exit lands adjacent
        # to the transcript instead of at the bottom of a blank screen.
        parts.append(f"\x1b[{band_top};1H")
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
        self._anchor = 0


__all__ = ["RegionLifecycleMixin"]
