"""Cursor-aware establish + idle resize poll (the Windows resize fixes).

Three live symptoms, one root: ``_establish`` scrolled blindly and only
ran on repaint traffic.

* Blind ``"\\n" * reserved`` scrolled the transcript up on EVERY
  re-establish — a drag-resize fired dozens and marched the content out
  of the window (growing blank gap between output and prompt).
* The cursor parked at the region bottom, so new output printed with a
  huge artificial gap after a maximize.
* No repaint at idle meant a resize wasn't noticed until the next
  keypress — the bar lingered painted mid-screen ("original placement
  still painted").

With a cursor-position provider (Windows console query in production;
injected here) establish scrolls ONLY the overshoot and re-parks at the
content end; ``poll_resize`` lets the key-listener idle tick notice
resizes without repaint traffic.
"""

import io

from code_puppy.messaging.bar_rendering import RESET_REGION
from code_puppy.messaging.bottom_bar import RESERVED_ROWS, BottomBar


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class MutableSize:
    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows

    def __call__(self):
        return (self.cols, self.rows)


def drain(stream):
    value = stream.getvalue()
    stream.truncate(0)
    stream.seek(0)
    return value


def _bar(rows=24, cursor=None):
    """Bar with an injected cursor provider (``cursor`` may be a callable)."""
    tty = FakeTTY()
    size = MutableSize(80, rows)
    provider = cursor if callable(cursor) or cursor is None else (lambda: cursor)
    bar = BottomBar(stream=tty, get_size=size, get_cursor_pos=provider)
    return bar, tty, size


# =========================================================================
# Cursor-aware establish
# =========================================================================


def test_known_cursor_above_region_parks_at_content_end():
    # 24 rows, empty status -> reserved=2, top=22. Content ends at (5, 10):
    # nothing to scroll; the next print must continue exactly there.
    bar, tty, _ = _bar(cursor=(5, 10))
    bar.start()
    out = drain(tty)
    assert "\n" * RESERVED_ROWS not in out  # no blind scroll
    assert "\x1b[1;22r" in out
    assert "\x1b[5;10H" in out  # parked at the content end


def test_known_cursor_in_reserved_band_scrolls_only_overshoot():
    # Content ends on the bottom row (24): the reserved band would eat
    # rows 23-24, so scroll exactly 2 -- newlines at the true bottom so
    # the overlap goes to scrollback -- and park at the region bottom.
    bar, tty, _ = _bar(cursor=(24, 1))
    bar.start()
    out = drain(tty)
    assert "\x1b[24;1H" + "\n" * 2 in out
    assert "\x1b[1;22r" in out
    assert "\x1b[22;1H" in out


def test_known_cursor_establish_is_scroll_idempotent():
    # Re-establish with an unchanged in-region cursor must not scroll:
    # this is what stops a drag-resize from marching the transcript away.
    calls = []

    def provider():
        calls.append(True)
        return (5, 1)

    bar, tty, size = _bar(cursor=provider)
    bar.start()
    drain(tty)
    size.rows = 30
    bar.set_prompt_text("> ", "hi", 2)  # repaint notices the resize
    out = drain(tty)
    assert "\n" not in out.replace("\x1b[1;28r", "")  # zero scroll anywhere
    assert "\x1b[5;1H" in out  # still parked at the content end
    assert len(calls) == 2  # one query per establish


def test_cursor_provider_failure_falls_back_to_blind_scroll():
    def exploding():
        raise OSError("no console")

    bar, tty, _ = _bar(cursor=exploding)
    bar.start()
    out = drain(tty)
    assert "\n" * RESERVED_ROWS in out  # classic blind path
    assert "\x1b[22;1H" in out


def test_injected_stream_without_provider_stays_blind():
    # Tests (and POSIX) keep the deterministic blind path.
    bar, tty, _ = _bar(cursor=None)
    bar.start()
    assert "\n" * RESERVED_ROWS in drain(tty)


# =========================================================================
# Idle resize poll
# =========================================================================


def test_poll_resize_reestablishes_on_size_change():
    bar, tty, size = _bar()
    bar.start()
    drain(tty)
    size.rows = 40
    bar.poll_resize()  # no repaint traffic needed
    out = drain(tty)
    assert RESET_REGION in out  # old band erased
    assert f"\x1b[1;{40 - bar._reserved}r" in out


def test_poll_resize_unchanged_size_writes_nothing():
    bar, tty, _ = _bar()
    bar.start()
    drain(tty)
    bar.poll_resize()
    assert drain(tty) == ""


def test_poll_resize_inactive_is_noop():
    bar, tty, size = _bar()
    size.rows = 40
    bar.poll_resize()  # never started
    assert drain(tty) == ""


def test_poll_resize_suspended_is_noop():
    bar, tty, size = _bar()
    bar.start()
    with bar.suspended():
        drain(tty)
        size.rows = 40
        bar.poll_resize()
        assert drain(tty) == ""


def test_poll_resize_wakes_dormant_bar_when_terminal_grows():
    # Terminal too small at start -> dormant; growing it must wake the
    # bar via the idle poll (no keypress required).
    bar, tty, size = _bar(rows=2)
    bar.start()
    assert bar._region_up is False
    drain(tty)
    size.rows = 24
    bar.poll_resize()
    assert bar._region_up is True
    assert "\x1b[1;22r" in drain(tty)
