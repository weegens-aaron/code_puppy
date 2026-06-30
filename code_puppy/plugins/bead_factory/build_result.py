"""Terminal-turn judgment result for the bead_factory build loop.

This module is the *output product* of a build-loop run: a small frozen
:class:`BuildResult` capturing the outcome of one terminal turn (the turn the
loop returns ``None`` on), plus a pure :func:`build_result` helper that derives
the pass/fail/abstain tallies and the aggregated remediation feedback from the
list of per-inspector :class:`~.inspector.BuildInspection` verdicts.

It is intentionally **pure and I/O-free** so it stays trivially unit-testable,
mirroring the dumb-data-box style of ``build_state.py`` and ``inspector.py``.

Design provenance: ADR ``docs/adr/0002-build-loop-judgment-result-transport.md``
(decision bead ``bead-factory-1r6``). The voting semantics match
``build_loop._run_build_inspectors`` exactly: an abstaining inspector is
excluded from the pass/fail tally but counted in ``abstained`` -- so an
abstainer is never double-counted as a pass or a fail.

The aggregated-notes feedback reuses ``build_loop._format_remediation_block``
verbatim (lazy-imported at call time) so the live build loop and this result
builder share a single source of truth for how verdicts render -- including the
exact PASS/FAIL/ABSTAIN glyphs. (We deliberately do NOT re-declare the
formatter here: this project's emoji filter strips the glyphs from any file we
write, which would silently diverge a hand-copied version from the original.)

The bottom of the file is a tiny *consume-once results sink* singleton
(``set_last`` / ``take_last`` / ``peek_last`` / ``clear``). It is the side
channel chosen in the ADR for ferrying a :class:`BuildResult` from the build
loop's terminal turn to the chain driver's close boundary (the turn-hook return
value is already overloaded as the retry/stop bit flag). Both call sites are
now wired: the build loop writes ``set_last`` at every terminal exit
(bead-factory-60e) and the chain driver reads ``take_last`` at its close
boundary (bead-factory-0sc, read-only surfacing only — it does not yet act on
the result).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .inspector import BuildInspection


class StopReason(str, Enum):
    """Why the build loop exited on its terminal turn.

    One value per real exit path in ``build_loop.on_interactive_turn_end``:

    * ``COMPLETE`` -- every voting inspector passed (``BUILD COMPLETE!``).
    * ``MAX_ITERATIONS`` -- ``loop_num >= max_iters`` (``BUILD STOPPED``).
    * ``CANCELLED`` -- ``CancelledError`` / ``KeyboardInterrupt`` (Ctrl+C).
    * ``NO_PROMPT`` -- no active build prompt at hook entry (defensive
      early-return).

    The retry path (loop incomplete, returns a continuation dict) is NOT a stop
    reason -- the loop is still running, so no terminal result is emitted on
    that turn.

    Subclasses ``str`` so the value serializes/loggable as its name and
    compares equal to the plain string -- handy for headless consumers.
    """

    COMPLETE = "complete"
    MAX_ITERATIONS = "max_iterations"
    CANCELLED = "cancelled"
    NO_PROMPT = "no_prompt"


@dataclass(frozen=True)
class BuildResult:
    """Outcome of a single build-loop terminal turn. Frozen, pure data.

    Invariants (guaranteed by :func:`build_result`):

    * ``total == passed + failed + abstained``
    * ``completed == (stop_reason is StopReason.COMPLETE)``

    ``completed`` is kept as an explicit field despite being derivable from
    ``stop_reason`` because headless consumers and the close boundary read it as
    the primary success bit without needing to reach for the enum.
    """

    completed: bool
    stop_reason: StopReason
    total: int
    passed: int
    failed: int
    abstained: int
    verdicts: tuple[BuildInspection, ...] = field(default_factory=tuple)
    aggregated_notes: str = ""
    loop_count: int = 0
    bead_id: str | None = None


def build_result(
    verdicts: list[BuildInspection],
    stop_reason: StopReason,
    *,
    loop_count: int = 0,
    bead_id: str | None = None,
) -> BuildResult:
    """Derive a :class:`BuildResult` from inspector verdicts + a stop reason.

    Pure -- no I/O, no globals read. The voting tally matches
    ``build_loop._run_build_inspectors`` exactly: abstaining inspectors are
    excluded from the pass/fail vote but counted in ``abstained``, so an
    abstainer is never double-counted as a pass or a fail. The aggregated
    feedback reuses ``build_loop._format_remediation_block`` (lazy-imported
    here to dodge a top-level cycle), the same formatter the live loop uses.
    """
    # Lazy import: build_loop is comparatively heavy (pulls in the inspector
    # stack via pydantic_ai), and we want this module to stay import-light and
    # cycle-free. The formatter is the canonical, emoji-bearing renderer -- we
    # reuse it verbatim rather than risk a divergent hand-copy.
    from .build_loop import _format_remediation_block

    total = len(verdicts)
    voting = [v for v in verdicts if not v.abstained]
    abstained = total - len(voting)
    passed = sum(1 for v in voting if v.complete)
    failed = len(voting) - passed

    return BuildResult(
        completed=stop_reason is StopReason.COMPLETE,
        stop_reason=stop_reason,
        total=total,
        passed=passed,
        failed=failed,
        abstained=abstained,
        verdicts=tuple(verdicts),
        aggregated_notes=_format_remediation_block(verdicts),
        loop_count=loop_count,
        bead_id=bead_id,
    )


# ---------------------------------------------------------------------------
# Consume-once results sink (side channel; ADR transport decision)
# ---------------------------------------------------------------------------
#
# A tiny module-level singleton ferrying ONE BuildResult from the build loop's
# terminal turn to the chain driver's close boundary. Consume-once
# (``take_last`` pops + clears) kills cross-bead staleness by design. Fail-soft
# by construction: an empty sink yields ``None`` and leaves consumers unchanged.
#
# Both call sites are wired: the build loop writes ``set_last`` at every
# terminal exit (bead-factory-60e) and the chain driver reads ``take_last`` at
# its close boundary (bead-factory-0sc).

_LAST: BuildResult | None = None


def set_last(result: BuildResult) -> None:
    """Stash the most recent terminal-turn result (overwrites any prior)."""
    global _LAST
    _LAST = result


def take_last() -> BuildResult | None:
    """Pop the stashed result, clearing the sink (consume-once)."""
    global _LAST
    result, _LAST = _LAST, None
    return result


def peek_last() -> BuildResult | None:
    """Non-destructive read of the stashed result (tests / headless)."""
    return _LAST


def clear() -> None:
    """Drop any stashed result (test isolation)."""
    global _LAST
    _LAST = None
