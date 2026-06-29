"""Mutating ``bd`` calls + epic/gate/lint housekeeping for bead-chain.

The write half of the original monolithic ``beads.py`` (bead_chain-7xv):
state mutations (``claim`` / ``revert_to_open`` / ``close``) plus the
epic-rollup, gate-probe and lint-warning housekeeping that close the
chain's loops. Its sibling :mod:`beads_reads` owns the read/query
waterfall, and :mod:`beads` keeps the shared subprocess core
(``_run_bd``, the predicates, the constants) and re-exports both halves
so existing ``from .beads import ...`` call sites and the flat
``import beads`` test suite keep working unchanged.

Monkeypatch contract: like :mod:`beads_reads`, the ``_run_bd`` seam is
resolved through the live ``beads`` module object (``_beads._run_bd``) at
call time so a test that stubs ``beads._run_bd`` is honoured. Pure
predicates and constants no test patches are imported directly.
"""

from __future__ import annotations

import json
from typing import Any

try:  # package context: code_puppy.plugins.bead_chain.beads_writes
    from . import beads as _beads
    from .beads import BeadsError, _validate_bead_id, is_recurring_epic
except ImportError:  # flat context: bare ``import beads_writes`` under pytest
    import beads as _beads  # type: ignore[no-redef]
    from beads import (  # type: ignore[no-redef]
        BeadsError,
        _validate_bead_id,
        is_recurring_epic,
    )

__all__ = [
    "claim",
    "revert_to_open",
    "close",
    "has_epic_in_progress",
    "close_eligible_epics",
    "check_gates",
    "lint_warnings",
]


def claim(bead_id: str) -> None:
    """Claim a bead as in-progress for the current actor."""
    _validate_bead_id(bead_id)
    _beads._run_bd("update", bead_id, "--claim")


def revert_to_open(bead_id: str) -> None:
    """Push a claimed bead back to ``open``, re-entering the ready queue.

    The clean inverse of :func:`claim`. Used by bead-chain to unwind
    the in_progress state when:

    * the user cancels a chain (Ctrl+C / runtime cancel) — work isn't
      complete, but the bead shouldn't sit claimed forever, and
    * ``bd close`` fails on judge-passed completion — the bead is
      still legitimately not-done; keeping it claimed would leak into
      the next run's recovery flow.

    Wraps ``bd update <id> --status=open``. This mirrors the syntax we
    already guard against in :mod:`close_guard` (``--status=closed``),
    so we're confident the flag name is canonical bd. Raises
    :class:`BeadsError` on infrastructure failure so callers can decide
    whether to soft-fail or escalate.
    """
    _validate_bead_id(bead_id)
    _beads._run_bd("update", bead_id, "--status=open")


def close(bead_id: str, *, reason: str | None = None) -> None:
    """Close a bead with an optional reason note."""
    _validate_bead_id(bead_id)
    args = ["close", bead_id]
    if reason:
        args.extend(["--reason", reason])
    _beads._run_bd(*args)


def has_epic_in_progress() -> bool:
    """Return ``True`` if at least one epic is currently in_progress.

    Wraps ``bd list --type=epic --status=in_progress --json``. Used to
    decide whether bead-chain needs to start a new epic or if one is
    already being tracked as active.
    """
    raw = _beads._run_bd("list", "--type=epic", "--status=in_progress", "--json")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        # Can't parse → assume nothing is in progress (safe default:
        # worst case we start one that's already started, which --claim
        # handles idempotently).
        return False

    if isinstance(items, list):
        return len(items) > 0
    return False


def close_eligible_epics() -> list[dict[str, Any]]:
    """Close every epic whose children are all complete; return the closed ones.

    **Conservative approach (bead_chain-tfn fix):** The original cascade
    mechanism in ``bd epic close-eligible`` was too aggressive, sweeping up
    unrelated epics and their children when closing a set of molecule beads.

    The fix is simple but effective: call ``bd epic close-eligible`` once,
    but DISABLE the iteration loop. bd's cascade closes A → checks if parent
    B is now eligible → closes B → checks parent C, etc. This cascade can
    unexpectedly pull in unrelated epics that happen to have no open children.

    By calling close-eligible only once per session (at the end of a drain
    pass in :func:`lifecycle.activate_next_bead`), we limit the scope: only
    epics that were eligible *at that moment* are closed. Subsequent runs
    will handle parent eligibility if needed. This sacrifices one-shot
    cascading for data safety.

    Idempotent: a no-op when no epics are eligible. Return value always
    contains dicts with at least an ``id`` key.

    Older / unexpected bd versions may emit non-JSON output even with
    ``--json``; in that case the rollup *still happened*, we just can't
    enumerate what got closed. We return ``[]`` rather than raise: an
    unparseable success is functionally equivalent to "nothing got
    closed" for the caller (it just means quieter logs). Real failures
    (bd missing, non-zero exit) still raise :class:`BeadsError` so
    callers can decide whether to soft-fail or escalate.

    The returned list always contains **dicts** with at least an ``id``
    key, regardless of which shape bd emitted. Several shapes are
    tolerated so we don't break across bd schema tweaks:

      * bd 1.0.4 wraps a list of bare **string ids** under ``closed``:
        ``{"closed": ["abc-1", "abc-2"], "count": 2}``. Each id is
        normalised to ``{"id": "abc-1"}`` so callers can uniformly do
        ``epic.get("id")`` / ``epic.get("title")``.
      * Older bd emits a bare top-level list of epic dicts.
      * Some shapes wrap each closed epic as ``{"epic": {...}}``; we
        unwrap to the inner dict.

    **Recurring-molecule protection (bead_chain-wot / formulas#2):** a
    poured ``patrol`` molecule is a *recurring* monitor — closing its
    epic when its current children finish would kill the recurrence.
    ``bd epic close-eligible`` has no exclude flag, so we can't tell it
    "skip this epic". Instead we **preview** the eligible set with a
    non-destructive ``--dry-run`` first (:func:`_preview_close_eligible`)
    and check each candidate via :func:`is_recurring_epic`:

      * No recurring epic eligible → fast path: run bd's native one-shot
        cascade (preserves the bead_chain-tfn once-per-session
        behaviour and every existing rollup test).
      * ≥1 recurring epic eligible → we must NOT run the bulk close (it
        would sweep the patrol epic too). Close each *non*-recurring
        candidate individually and leave the recurring ones open for
        their next pour.
    """
    candidates = _preview_close_eligible()
    if not any(is_recurring_epic(epic) for epic in candidates):
        # Common case: nothing to protect. Let bd cascade natively.
        return _bulk_close_eligible()
    # A recurring (patrol) epic is in the eligible set — bypass the bulk
    # cascade and close only the safe ones, one by one.
    return _close_non_recurring(candidates)


def _preview_close_eligible() -> list[dict[str, Any]]:
    """Return the epics ``bd epic close-eligible`` *would* close, non-destructively.

    Wraps ``bd epic close-eligible --dry-run --json``. bd emits a list of
    ``{"epic": {...full record incl labels...}, "eligible_for_close":
    true}`` envelopes; :func:`_normalise_closed_epic` unwraps each to the
    inner epic dict (so :func:`is_recurring_epic` sees its ``labels``).
    Returns ``[]`` on empty / unparseable output — same silent-success
    contract as the live close path.
    """
    raw = _beads._run_bd("epic", "close-eligible", "--dry-run", "--json").strip()
    return _parse_close_eligible_payload(raw)


def _bulk_close_eligible() -> list[dict[str, Any]]:
    """Run the destructive ``bd epic close-eligible`` and return what closed."""
    raw = _beads._run_bd("epic", "close-eligible", "--json").strip()
    return _parse_close_eligible_payload(raw)


def _close_non_recurring(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Close every non-recurring epic in ``candidates`` one at a time.

    Used only when a recurring (patrol) epic is in the eligible set, so
    we can't trust bd's bulk cascade not to sweep it up. Recurring epics
    are skipped (left open for re-pour). Per-epic close failures are
    swallowed — a single stubborn epic must not strand the rest of the
    rollup; the next session's pass retries it.
    """
    closed: list[dict[str, Any]] = []
    for epic in candidates:
        if is_recurring_epic(epic):
            continue
        epic_id = str(epic.get("id", "")).strip()
        if not epic_id:
            continue
        try:
            close(epic_id, reason="all children complete (bead-chain rollup)")
        except BeadsError:
            # Soft-fail this one; rollup is courtesy cleanup, not core.
            continue
        closed.append(epic)
    return closed


def _parse_close_eligible_payload(raw: str) -> list[dict[str, Any]]:
    """Normalise any ``epic close-eligible`` JSON shape into epic dicts.

    Shared by the dry-run preview and the live close so both speak the
    same dialect. See :func:`close_eligible_epics` for the tolerated
    shapes. Empty / non-JSON / unexpected payloads degrade to ``[]``.
    """
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Rollup ran (or dry-run produced no parseable list); treat as
        # silent success — see close_eligible_epics docstring.
        return []

    if isinstance(payload, list):
        items: Any = payload
    elif isinstance(payload, dict):
        # bd 1.0.4: {"closed": [...ids...]}. Older/alt: {"epics": [...]}.
        items = payload.get("closed") or payload.get("epics") or []
    else:
        return []

    return [_normalise_closed_epic(item) for item in items if _is_closed_epic(item)]


def _is_closed_epic(item: Any) -> bool:
    """True if ``item`` is a usable closed-epic entry (non-empty str or dict)."""
    if isinstance(item, str):
        return bool(item.strip())
    return isinstance(item, dict)


def _normalise_closed_epic(item: Any) -> dict[str, Any]:
    """Coerce a close-eligible entry into a ``{"id": ..., ...}`` dict.

    bd's ``epic close-eligible --json`` is inconsistent across versions:
    1.0.4 returns bare string ids under ``closed``; older builds return
    epic dicts; some wrap each as ``{"epic": {...}}``. Callers only need
    ``id`` (and optionally ``title``) for log lines, so we flatten every
    shape to a plain dict here. Centralised so the rollup logger in
    :mod:`lifecycle` never has to branch on bd's output shape.
    """
    if isinstance(item, str):
        return {"id": item.strip()}
    # dict: unwrap a nested {"epic": {...}} envelope if present.
    inner = item.get("epic")
    if isinstance(inner, dict):
        return inner
    return item


# Summary keys bd emits from ``gate check --json``. Centralised so the
# parser and its zero-default fallback stay in lock-step.
_GATE_COUNT_KEYS: tuple[str, ...] = ("checked", "resolved", "escalated", "errors")


def check_gates() -> dict[str, int]:
    """Evaluate all open gates, close the resolved ones, return the counts.

    Wraps ``bd gate check --json``. Resolvable gate types — ``timer``,
    ``gh:run``, ``gh:pr``, ``bead`` — keep their *target* issues out of
    ``bd ready`` until the gate closes. bead-chain never polls these on
    its own, so a gate that has *become* satisfied can sit closeable-but-
    open and strand its target, stopping the chain short of ready-
    pending-poll work. Asking bd to re-evaluate every open gate closes
    the satisfied ones, which re-opens their targets for the next
    ``bd ready`` pick.

    Returns the summary counts ``{"checked", "resolved", "escalated",
    "errors"}`` (any missing key defaults to 0). ``resolved > 0`` means
    at least one gate closed this pass — the caller should re-probe the
    ready queue rather than declare the chain done.

    Raises :class:`BeadsError` on infrastructure failure (bd missing,
    non-zero exit) — same contract as :func:`close_eligible_epics`, so
    the caller can soft-fail. Unparseable-but-successful output degrades
    to all-zero counts rather than raising: a courtesy probe shouldn't
    halt the chain over a log-format quirk.
    """
    raw = _beads._run_bd("gate", "check", "--json")
    return _parse_gate_check_summary(raw)


def _parse_gate_check_summary(raw: str) -> dict[str, int]:
    """Extract ``{checked,resolved,escalated,errors}`` from bd's output.

    bd 1.0.x prints a human-readable summary line *before* the JSON
    object even under ``--json`` (e.g. ``Checked 3 gates: 1 resolved,
    0 escalated, 0 errors`` then ``{...}``), so we slice from the first
    ``{`` to the last ``}`` rather than parsing the whole payload. Any
    non-JSON / non-dict / missing-key situation degrades to zeros so a
    courtesy gate probe never raises on a log-format quirk.
    """
    zeros = {key: 0 for key in _GATE_COUNT_KEYS}
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return zeros
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return zeros
    if not isinstance(payload, dict):
        return zeros
    summary: dict[str, int] = {}
    for key in _GATE_COUNT_KEYS:
        value = payload.get(key, 0)
        summary[key] = value if isinstance(value, int) else 0
    return summary


def lint_warnings(bead_id: str) -> list[str]:
    """Return ``bd lint`` template-contract warnings for one bead.

    Wraps ``bd lint <id> --status all --json``. ``bd lint`` checks an
    issue for the *recommended* sections its type requires (e.g. a
    ``task`` should carry ``## Acceptance Criteria``; an ``epic`` should
    carry ``## Success Criteria``) and reports the missing ones. The
    coverage audit (FB-5, ``bead_chain-vmo``) found bead-chain drove
    beads straight off ``bd ready`` without ever consulting this
    contract, so a bead that lost its ``## Acceptance Criteria`` to a
    ``--graph`` import would be graded by the LLM judges against a
    section the agent was never shown was missing. Surfacing the lint
    output into the build prompt closes that blind spot (pairs with FB-2,
    which renders the criteria that *are* present).

    The ``--status all`` flag is belt-and-suspenders: a claimed bead is
    ``in_progress``, and ``bd lint``'s default filter is ``open``. On
    this bd build an explicit issue id already bypasses the status
    filter, but a future build that honoured it would silently skip the
    very bead we just claimed — ``--status all`` guarantees the lint
    runs regardless of the bead's current status.

    Returns the list of missing-section names for ``bead_id`` (e.g.
    ``['## Acceptance Criteria']``), or ``[]`` when the bead is clean.

    Raises :class:`BeadsError` on infrastructure failure (bd missing,
    non-zero exit) — same contract as :func:`check_gates`, so the
    prompt layer can soft-fail to ``[]`` when this bd build lacks the
    ``lint`` subcommand. Unparseable-but-successful output degrades to
    ``[]`` rather than raising: a courtesy lint shouldn't halt the
    chain over a log-format quirk.
    """
    _validate_bead_id(bead_id)
    raw = _beads._run_bd("lint", bead_id, "--status", "all", "--json")
    return _parse_lint_missing(raw, bead_id)


def _parse_lint_missing(raw: str, bead_id: str) -> list[str]:
    """Extract ``bead_id``'s missing-section names from ``bd lint`` output.

    ``bd lint --json`` emits ``{"total", "issues", "results": [...]}``
    where each result is ``{"id", "title", "type", "missing": [...],
    "warnings": N}``. We slice from the first ``{`` to the last ``}``
    (mirroring :func:`_parse_gate_check_summary`) in case bd prefixes a
    human-readable line, then pull the ``missing`` list off the result
    whose ``id`` matches ``bead_id``. Filtering by id is defensive — an
    explicit id should only ever return that one result, but we never
    want to attribute another bead's warnings to this one. Any
    non-JSON / non-dict / missing-key situation degrades to ``[]``.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    missing: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")) != bead_id:
            continue
        for section in item.get("missing", []) or []:
            text = str(section).strip()
            if text:
                missing.append(text)
    return missing
