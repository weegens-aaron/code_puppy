"""Floating bar + idle resize poll (the Windows resize fixes).

The reserved band FLOATS directly under the content (plus a small blank
runway) when the transcript cursor is knowable, descending as content
grows and docking at the true bottom for long transcripts. Why: a bar
painted at the far bottom with a blank band above it is a resize trap —
a shrink (maximize -> restore) keeps the bottom rows of the active area
and strands the transcript in scrollback, while scrolling content around
to close the gap fractures scrollback with inserted blanks. With the
band hugging content from below, everything under it is blank, and
terminals trim trailing blanks on shrink: resizes keep the transcript
visible AND contiguous.

POSIX / injected streams (no cursor query) keep the classic
bottom-docked band with deterministic blind scrolling.
"""

import io
import re

from code_puppy.messaging.bar_region import FLOAT_HEADROOM
from code_puppy.messaging.bar_rendering import RESET_REGION
from code_puppy.messaging.bottom_bar import RESERVED_ROWS, BottomBar

_SCROLL_DOWN = re.compile(r"\x1b\[\d+T")  # CSI Ps T (SD)


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


def _bar(rows=40, cursor=None):
    """Bar with an injected cursor provider (``cursor`` may be a callable)."""
    tty = FakeTTY()
    size = MutableSize(80, rows)
    provider = cursor if callable(cursor) or cursor is None else (lambda: cursor)
    bar = BottomBar(stream=tty, get_size=size, get_cursor_pos=provider)
    return bar, tty, size


# Idle bar: empty status -> reserved = margin + prompt = 2.
_RES = RESERVED_ROWS


# =========================================================================
# Floating establish
# =========================================================================


def test_band_floats_under_short_content():
    # 40 rows, content ends at (5, 10): the band anchors at content +
    # runway, NOT the screen bottom — everything below stays blank so a
    # shrink can only trim blanks.
    bar, tty, _ = _bar(cursor=(5, 10))
    bar.start()
    out = drain(tty)
    anchor = 5 + _RES + FLOAT_HEADROOM
    assert bar._anchor == anchor
    assert f"\x1b[1;{anchor - _RES}r" in out  # region ends at the band
    assert f"\x1b[{anchor};1H" in out  # prompt row painted at the anchor
    assert "\x1b[40;1H" not in out  # nothing painted at the far bottom
    assert "\x1b[5;10H" in out  # cursor re-parked at the content end
    assert "\n" * _RES not in out  # no blind scroll


def test_band_docks_when_content_fills_screen():
    # Content at the bottom row: float clamps to the screen bottom and
    # the overlap scrolls into scrollback via newlines (never CSI S).
    bar, tty, _ = _bar(rows=24, cursor=(24, 1))
    bar.start()
    out = drain(tty)
    assert bar._anchor == 24
    assert "\x1b[24;1H" + "\n" * 2 in out  # overshoot-only scroll
    assert "\x1b[1;22r" in out
    assert "\x1b[22;1H" in out


def test_grow_keeps_band_adjacent_no_split_no_scroll():
    # Maximize: the band STAYS with the content instead of jumping to
    # the new bottom — no gap trap, no SD, no transcript motion at all.
    bar, tty, size = _bar(rows=24, cursor=(5, 1))
    bar.start()
    anchor = bar._anchor
    drain(tty)
    size.rows = 60
    bar.poll_resize()
    out = drain(tty)
    assert bar._anchor == anchor  # monotonic: didn't move
    assert _SCROLL_DOWN.search(out) is None  # no hug/SD
    assert "\n" not in out.replace(f"\x1b[1;{anchor - _RES}r", "")  # no scroll
    assert "\x1b[5;1H" in out  # still parked at the content end


def test_content_growth_descends_the_band():
    # Content eats its runway -> the descent tick moves the band down
    # (a fresh runway) without any size change.
    pos = {"row": 5}
    bar, tty, _ = _bar(cursor=lambda: (pos["row"], 1))
    bar.start()
    old_anchor = bar._anchor
    drain(tty)
    pos["row"] = old_anchor - _RES  # content reached the region bottom
    bar.poll_resize()  # same size — pure descent
    out = drain(tty)
    assert bar._anchor == pos["row"] + _RES + FLOAT_HEADROOM
    assert bar._anchor > old_anchor
    assert f"\x1b[1;{bar._anchor - _RES}r" in out


def test_descent_erases_the_old_band():
    # The band's old rows sit ABOVE the new position after a descent —
    # without an erase they'd linger as ghost chrome mid-transcript.
    pos = {"row": 5}
    bar, tty, _ = _bar(cursor=lambda: (pos["row"], 1))
    bar.start()
    old_anchor = bar._anchor
    drain(tty)
    pos["row"] = old_anchor - _RES
    bar.poll_resize()
    out = drain(tty)
    for row in (old_anchor - 1, old_anchor):  # old margin + prompt rows
        assert f"\x1b[{row};1H\x1b[2K" in out


def test_cursor_provider_failure_falls_back_to_blind_dock():
    def exploding():
        raise OSError("no console")

    bar, tty, _ = _bar(rows=24, cursor=exploding)
    bar.start()
    out = drain(tty)
    assert bar._anchor == 24  # classic dock
    assert "\n" * _RES in out  # classic blind scroll
    assert "\x1b[22;1H" in out


def test_injected_stream_without_provider_stays_blind():
    # Tests (and POSIX) keep the deterministic docked path.
    bar, tty, _ = _bar(rows=24, cursor=None)
    bar.start()
    assert "\n" * _RES in drain(tty)
    assert bar._anchor == 24


# =========================================================================
# Floating band grow/shrink (status row materializing etc.)
# =========================================================================


def test_floating_band_grows_downward_without_scrolling():
    bar, tty, _ = _bar(cursor=(5, 1))
    bar.start()
    top = bar._anchor - _RES
    drain(tty)
    bar.set_status("tokens: 42")  # reserved 2 -> 3
    out = drain(tty)
    assert bar._anchor == top + 3  # bottom edge extended into the blanks
    assert f"\x1b[1;{top}r" in out  # region top UNCHANGED
    assert "\x1b[1S" not in out  # no scroll needed — rows below were blank


def test_floating_band_shrinks_from_its_bottom_edge():
    bar, tty, _ = _bar(cursor=(5, 1))
    bar.start()
    bar.set_status("tokens: 42")
    grown_anchor = bar._anchor
    drain(tty)
    bar.set_status("")  # status collapses: reserved 3 -> 2
    out = drain(tty)
    assert bar._anchor == grown_anchor - 1
    assert f"\x1b[{grown_anchor};1H\x1b[2K" in out  # vacated bottom row cleared


# =========================================================================
# Idle resize poll
# =========================================================================


def test_poll_resize_reestablishes_on_size_change():
    bar, tty, size = _bar(rows=24)
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
    size.rows = 60
    bar.poll_resize()  # never started
    assert drain(tty) == ""


def test_poll_resize_suspended_is_noop():
    bar, tty, size = _bar()
    bar.start()
    with bar.suspended():
        drain(tty)
        size.rows = 60
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


# =========================================================================
# Teardown parks under the content
# =========================================================================


def test_teardown_parks_cursor_at_band_top():
    # Exit with a floating band: the shell prompt should land right
    # under the transcript, not at the bottom of a blank screen.
    bar, tty, _ = _bar(cursor=(5, 1))
    bar.start()
    band_top = bar._anchor - _RES + 1
    drain(tty)
    bar.stop()
    out = drain(tty)
    assert f"\x1b[{band_top};1H" in out
    assert bar._anchor == 0
