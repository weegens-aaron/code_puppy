"""Molecule fan-out gate aggregation mode (FB-13).

Split out of :mod:`lifecycle` so this self-contained gate logic lives in one
cohesive module; behavior is unchanged. :func:`lifecycle.activate_next_bead`
calls
:func:`_fan_out_gate_verdict` to decide whether a candidate bead's fan-out
gate is satisfied before claiming it.

A ``waits_for: children-of(spawner)`` gate can aggregate its spawned children
two ways:

* ``all-children`` — satisfied only once EVERY child is closed.
* ``any-children`` — satisfied the moment the FIRST child closes.

bd accepts ``--waits-for-gate {all-children,any-children}`` at *write* time
but, through at least bd 1.0.5, does NOT surface the chosen mode in
``bd show --json`` / ``bd dep list`` — the mode is write-only. So today
:func:`_fan_out_gate_mode` resolves to ``None`` (unknown) in practice; the
plumbing here *honors* the mode the instant bd starts exposing it, with no
further change.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from .beads import BeadsError
from .beads_reads import has_closed_children, has_open_children, show


_FAN_OUT_MODE_ALL = "all-children"
_FAN_OUT_MODE_ANY = "any-children"

# Top-level ``bd show`` record keys that *might* carry the aggregation mode
# once bd surfaces it. Ordered most-likely-first; every one is a cheap
# dict lookup, so listing a few candidate spellings costs nothing and
# future-proofs against bd's eventual field name.
_FAN_OUT_MODE_KEYS: tuple[str, ...] = (
    "waits_for_gate",
    "waits_for_mode",
    "fan_out_mode",
    "gate_mode",
)

# Keys to probe inside each ``dependencies`` array entry, in case bd
# surfaces the mode on the dependency edge rather than the bead.
_FAN_OUT_DEP_MODE_KEYS: tuple[str, ...] = (
    "waits_for_gate",
    "gate",
    "gate_mode",
    "mode",
    "aggregation",
)


def _normalize_fan_out_mode(raw: Any) -> str | None:
    """Map a raw mode token to a canonical mode constant, or ``None``.

    Tolerant of spelling drift (``any`` / ``any-children`` / ``any_child``)
    so we honor whatever shape bd eventually emits. Anything unrecognised
    (including non-strings) reads as ``None`` — unknown, never a guess.
    """
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower().replace("_", "-")
    if token in ("any", "any-child", "any-children"):
        return _FAN_OUT_MODE_ANY
    if token in ("all", "all-child", "all-children"):
        return _FAN_OUT_MODE_ALL
    return None


def _fan_out_gate_mode(bead: dict[str, Any] | None) -> str | None:
    """Resolve a fan-out gate's aggregation mode from a ``bd show`` record.

    Returns ``_FAN_OUT_MODE_ALL``, ``_FAN_OUT_MODE_ANY``, or ``None``
    (unknown). Checks the plausible top-level keys first, then any
    per-edge ``dependencies`` entries. Today (bd ≤ 1.0.5) the mode is
    write-only and this returns ``None`` for every real bead — that's the
    expected, documented state, not a bug. The verdict layer treats
    ``None`` as 'do not revert' so an otherwise-ready *any-children*
    waiter is never wrongly flipped back to open.
    """
    if not bead:
        return None
    for key in _FAN_OUT_MODE_KEYS:
        mode = _normalize_fan_out_mode(bead.get(key))
        if mode is not None:
            return mode
    deps = bead.get("dependencies")
    if isinstance(deps, list):
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            for key in _FAN_OUT_DEP_MODE_KEYS:
                mode = _normalize_fan_out_mode(dep.get(key))
                if mode is not None:
                    return mode
    return None


class _FanOutGateVerdict(NamedTuple):
    """Outcome of evaluating a bead's molecule fan-out gate.

    ``blocked``
        The gate is unsatisfied, so the bead must not be driven yet.
    ``mode_known``
        bd surfaced the aggregation mode, so a revert-to-open is safe.
        When the mode is unknown we *refuse* (stop) but deliberately
        *skip the revert*: an unknown gate might be ``any-children`` and
        already satisfied, and reverting would strand that ready waiter
        at ``open`` (FB-13 acceptance criterion #1).
    """

    blocked: bool
    mode_known: bool


# Canonical 'no gate / nothing to do' verdict. ``mode_known=True`` is
# inert here (no revert happens when ``blocked`` is False) but keeps the
# 'unknown ⇒ skip revert' signal meaningful only for real, blocked gates.
_NO_FAN_OUT_GATE = _FanOutGateVerdict(blocked=False, mode_known=True)


def _fan_out_gate_verdict(
    bead_id: str, bead: dict[str, Any] | None = None
) -> _FanOutGateVerdict:
    """Evaluate ``bead_id``'s molecule fan-out gate, honoring its mode.

    Beads with ``waits_for: children-of(spawner)`` are invisible to
    ``bd blocked`` (upstream bd bug), so bead-factory evaluates
    the gate itself at claim time. The verdict honors the aggregation
    mode (FB-13):

    * **any-children** — unsatisfied only while *no* child has closed yet;
      satisfied the moment the first child closes.
    * **all-children** — unsatisfied while *any* child is still open
      (the historic, hardcoded behavior).
    * **unknown** (bd doesn't surface the mode) — evaluated with the
      conservative all-children rule for the *block* decision, but flagged
      ``mode_known=False`` so the caller skips the destructive revert.

    Call consolidation: pass an already-fetched
    ``bd show`` record as ``bead`` to avoid a redundant spawn. The
    spawner lookup is always a separate ``bd show`` — a different bead.

    Soft-fails to :data:`_NO_FAN_OUT_GATE` (not blocked) on any bd blip or
    malformed input, preserving the gate-detection path's fail-safe-open
    discipline.
    """
    if not bead_id:
        return _NO_FAN_OUT_GATE

    if bead is None:
        try:
            bead = show(bead_id)
        except BeadsError:
            # Can't determine gate status; assume no gate issue.
            return _NO_FAN_OUT_GATE
    if not bead:
        return _NO_FAN_OUT_GATE

    # Check for waits_for field.
    waits_for = bead.get("waits_for")
    if not waits_for or not isinstance(waits_for, str):
        return _NO_FAN_OUT_GATE

    # Check if it's a fan-out gate (children-of format).
    if not waits_for.startswith("children-of(") or not waits_for.endswith(")"):
        return _NO_FAN_OUT_GATE

    # Extract spawner ID.
    try:
        spawner_id = waits_for[len("children-of(") : -1].strip()
        if not spawner_id:
            return _NO_FAN_OUT_GATE
    except (ValueError, IndexError):
        return _NO_FAN_OUT_GATE

    # Confirm the spawner exists before querying its children.
    try:
        spawner = show(spawner_id)
    except BeadsError:
        # Can't determine; assume gate is satisfied.
        return _NO_FAN_OUT_GATE
    if not spawner:
        return _NO_FAN_OUT_GATE

    mode = _fan_out_gate_mode(bead)

    if mode == _FAN_OUT_MODE_ANY:
        # Satisfied the moment the first child closes. ``has_closed_children``
        # scopes the query to this one spawner (``bd list --parent=<id>``).
        blocked = not has_closed_children(spawner_id)
        return _FanOutGateVerdict(blocked=blocked, mode_known=True)

    # all-children OR unknown: unsatisfied iff the spawner still has an
    # unclosed child. ``has_open_children`` scopes the query to this one
    # spawner and soft-fails to False (gate satisfied) on infra error.
    blocked = has_open_children(spawner_id)
    return _FanOutGateVerdict(blocked=blocked, mode_known=(mode == _FAN_OUT_MODE_ALL))


def _has_fan_out_gate_issue(bead_id: str, bead: dict[str, Any] | None = None) -> bool:
    """True if ``bead_id`` has an unsatisfied fan-out gate.

    Thin bool wrapper over :func:`_fan_out_gate_verdict` (kept for its
    long-standing call sites and unit tests). The revert decision lives
    in the verdict's ``mode_known`` flag; callers that must decide whether
    to revert should use :func:`_fan_out_gate_verdict` directly.
    """
    return _fan_out_gate_verdict(bead_id, bead).blocked
