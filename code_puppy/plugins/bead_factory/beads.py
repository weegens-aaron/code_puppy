"""Thin subprocess wrapper around the ``bd`` CLI.

We intentionally shell out instead of importing any beads Python API:
beads is a Go binary, and its JSON output is its stable contract. This
keeps the plugin dependency-free and lets users on any bd version play.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

__all__ = [
    # Exception
    "BeadsError",
    # Bead classification predicates
    "is_recurring_epic",
    "is_excluded_type",
    "is_blocked",
    "is_pinned",
    "has_open_children",
    "has_epic_in_progress",
    # Queue / waterfall reads
    "next_ready",
    "next_in_progress",
    "next_ready_in_epic",
    "next_blocking_bug",
    "list_in_progress",
    "list_recoverable_strands",
    # Bead introspection
    "show",
    "memories",
    "extract_parent_epic_id",
    "open_blocker_ids",
    "lint_warnings",
    # State mutations
    "claim",
    "revert_to_open",
    "close",
    # Epic / gate housekeeping
    "close_eligible_epics",
    "check_gates",
    # Public configuration constants
    "DEFAULT_TIMEOUT",
    "DEFAULT_BD_BIN",
    "MAX_ATTEMPTS",
    "EXCLUDED_TYPES",
    "RECURRING_MOL_TYPES",
    "RECURRING_EPIC_LABELS",
    "BLOCKING_DEP_TYPES",
    "SATISFIED_BLOCKER_STATUSES",
    "IN_PROGRESS_STATUS",
    "HOOKED_STATUS",
    "PINNED_STATUS",
    "RECOVERABLE_STATUSES",
    "BLOCKING_BUG_TYPES",
    "PARENT_EPIC_KEY",
]

DEFAULT_TIMEOUT = 15.0
DEFAULT_BD_BIN = "bd"

# Bead ids are attacker-reachable strings: they come from bd's own JSON
# output, but bead-factory also accepts them from CLI args and recovery
# flows where a crafted value could slip through. List-form
# ``subprocess.run`` already blocks *shell* injection, but a value
# bristling with flags/whitespace/dashes can still confuse bd's own
# argument parser. We pin ids to the shape bd actually emits and reject
# anything else loudly. See :func:`_validate_bead_id`.
_BEAD_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

# Retry policy for transient `bd` timeouts.
#
# bd talks to a sqlite database that can briefly contend on locks
# (concurrent agents, cold-cache opens, the daemon flushing, etc.).
# Stranding the entire chain on a single blip is way worse than trying
# again, so we retry on ``subprocess.TimeoutExpired`` only. Permanent
# failures — ``FileNotFoundError`` (bd not installed), ``PermissionError``
# (bd present but not executable), and non-zero exits (real bd errors
# like 'bead not found', 'already closed') — are NOT retried; those are
# fatal and retrying just delays the truth (and burns wall-clock).
#
# WORST-CASE BUDGET (bead_chain-7b6): the old policy (DEFAULT_TIMEOUT=30,
# backoffs 0.5/1.0) cost up to 91.5s per call (30 + 0.5 + 30 + 1.0 + 30).
# With 10+ sequential calls per activation a flaky bd binary could add
# minutes of pure infra overhead to a single bead. The current policy
# (DEFAULT_TIMEOUT=15, exponential backoff capped at 2.0s) caps a fully
# timed-out call at:
#     15 + 0.25 + 15 + 0.5 + 15 = 45.75s  (about half the old worst case).
#
# Kept as module constants per YAGNI: if someone needs env-var knobs
# we add them later (5-line follow-up). Doing both up front overcommits.
MAX_ATTEMPTS: int = 3  # initial try + up to (MAX_ATTEMPTS - 1) retries
# Exponential backoff applied BEFORE each retry: delay(n) = BASE * 2**(n-1),
# capped at CEILING so a flaky bd binary can never stack arbitrarily long
# sleeps. ``n`` is the 1-based retry index (the initial attempt never waits).
# With the defaults the pre-retry sleeps are 0.25s, 0.50s, 1.00s, 2.00s, ...
# — long enough to let a sqlite lock clear, short enough not to feel like a
# hang. See :func:`_retry_backoff`.
_RETRY_BACKOFF_BASE: float = 0.25
_RETRY_BACKOFF_CEILING: float = 2.0


def _retry_backoff(attempt: int) -> float:
    """Exponential pre-retry delay (seconds) for retry index ``attempt``.

    ``attempt`` is the 1-based retry number — the 0th attempt is the
    initial try and never waits, so callers only invoke this for
    ``attempt >= 1``. Delay grows as ``BASE * 2 ** (attempt - 1)`` and is
    clamped to :data:`_RETRY_BACKOFF_CEILING` so the worst-case retry
    budget stays bounded no matter how high :data:`MAX_ATTEMPTS` climbs.
    """
    return min(_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), _RETRY_BACKOFF_CEILING)


# Bead types that /bead-factory must never try to drive directly.
# These are container / handle types: they organise or gate *other*
# work and have no code work of their own, so handing one to the build loop
# produces a bead that can't be completed — close_guard then refuses
# the close and the whole chain stalls.
#
#   * 'epic'      — container of child issues (anatomy); drive children
#   * 'milestone' — container/handle (anatomy#4)
#   * 'gate'      — gating handle (gates#2)
#   * 'molecule'  — swarm container/handle (swarms#1)
#
# Extend this tuple if other purely-organizational/handle types appear.
# One-line change, by design — :func:`_exclude_type_arg` builds the
# server-side ``--exclude-type`` arg from this tuple and
# :func:`is_excluded_type` re-filters client-side. DRY.
EXCLUDED_TYPES: tuple[str, ...] = ("epic", "milestone", "gate", "molecule")

# Molecule types whose live epic must SURVIVE rollup. A poured ``patrol``
# molecule is a *recurring* monitoring loop: once its current children
# close, its epic is eligible for ``bd epic close-eligible`` — but closing
# it would defeat the recurrence (coverage-audit gap formulas#2,
# bead_chain-wot). We refuse to auto-close epics tagged as one of these.
# Matched case-insensitively against a ``mol_type``-style field. Extend
# this tuple if other recurring molecule types appear — one-line change.
RECURRING_MOL_TYPES: tuple[str, ...] = ("patrol",)

# Field names that *might* carry a molecule's type. bd 1.0.4 does NOT
# surface mol-type on ``bd show`` / ``epic close-eligible --json`` (verified
# the hard way — see bead_chain-wot), so today the *label* contract below
# is the real signal; these keys are forward-compat for a bd that starts
# emitting the type directly (checked both top-level and inside metadata).
_MOL_TYPE_KEYS: tuple[str, ...] = ("mol_type", "mol-type", "molecule_type")

# Labels that mark an epic as a recurring molecule that must outlive its
# children. This is the *documented contract* for tagging a patrol/
# recurring molecule today (the audit's "template label" path): pour the
# molecule with one of these labels and rollup will leave its epic open.
# Matched case-insensitively against the epic's ``labels`` list. Kept as a
# small, extensible tuple mirroring :data:`EXCLUDED_TYPES`. DRY.
RECURRING_EPIC_LABELS: tuple[str, ...] = ("patrol", "mol-type:patrol", "recurring")


def _mol_type_matches(container: Any) -> bool:
    """True if ``container`` (a dict) carries a recurring mol-type field."""
    if not isinstance(container, dict):
        return False
    for key in _MOL_TYPE_KEYS:
        if str(container.get(key, "")).strip().lower() in RECURRING_MOL_TYPES:
            return True
    return False


def is_recurring_epic(bead: dict[str, Any] | None) -> bool:
    """True if ``bead`` is a recurring molecule epic rollup must NOT close.

    Two independent signals, either of which protects the epic:

    1. **mol-type field** equals a value in :data:`RECURRING_MOL_TYPES`
       (e.g. ``patrol``) — checked both top-level and inside a nested
       ``metadata`` dict. Forward-compat: bd 1.0.4 doesn't emit this yet.
    2. **label marker** — one of the epic's ``labels`` (case-insensitive)
       is in :data:`RECURRING_EPIC_LABELS`. This is the signal that
       actually fires today: tag a poured patrol molecule's epic with a
       ``patrol`` label and rollup leaves it open for re-pour.

    None/missing/non-dict input is treated as 'not recurring' (safe
    default: an ordinary epic with no marker rolls up as before — we only
    *withhold* closure when a recurring marker is positively present).
    """
    if not isinstance(bead, dict):
        return False
    if _mol_type_matches(bead) or _mol_type_matches(bead.get("metadata")):
        return True
    raw = bead.get("labels")
    if isinstance(raw, list):
        labels = {str(item).strip().lower() for item in raw}
        if labels & {lbl.lower() for lbl in RECURRING_EPIC_LABELS}:
            return True
    return False


def is_excluded_type(bead: dict[str, Any] | None) -> bool:
    """True if ``bead`` is a container type bead-factory must never drive.

    Defence-in-depth companion to the server-side ``--exclude-type``
    filter we pass to ``bd``. The CLI flag *should* keep epics out of
    our queries, but — verified the hard way in prod — it sometimes
    leaks an epic through anyway (bd version drift, JSON casing
    differences, etc.). Filtering client-side as well makes the
    invariant ironclad: even if every server-side filter failed open,
    we still refuse to treat epics as drivable work.

    The check is case-insensitive on ``issue_type`` so an upstream
    bd that suddenly emits ``"Epic"`` instead of ``"epic"`` doesn't
    silently start leaking. None/missing/non-dict input is treated as
    'not excluded' (safer: a bead with a busted shape can still be
    surfaced for the caller to handle, rather than vanish silently).
    """
    if not isinstance(bead, dict):
        return False
    issue_type = str(bead.get("issue_type", "")).strip().lower()
    return issue_type in EXCLUDED_TYPES


# Inbound dependency-edge types that gate work until the other bead
# closes. ``blocks`` is the canonical hard block; ``waits-for`` is the
# generic fan-out edge, which gates identically. ``parent-child`` /
# ``discovered-from`` / ``related`` do NOT gate and are excluded. Tuple
# so a new blocking type stays a one-line edit. (The molecule
# ``waits_for: children-of(...)`` *field* is a different mechanism, see
# :func:`lifecycle._has_fan_out_gate_issue`.) Rationale:
# ``__docs/Features/WorkTimeBlockerGate.md``.
BLOCKING_DEP_TYPES: tuple[str, ...] = ("blocks", "waits-for")

# A blocker is satisfied only once closed (open / in_progress / blocked
# all still gate). Compared case-insensitively in :func:`open_blocker_ids`.
SATISFIED_BLOCKER_STATUSES: frozenset[str] = frozenset({"closed"})


# bd lifecycle status strings bead-factory reasons about by name. The full
# set lives in `bd statuses`; we only name the ones the chain actually
# inspects so a typo can't silently break a status comparison.
IN_PROGRESS_STATUS: str = "in_progress"
HOOKED_STATUS: str = "hooked"
PINNED_STATUS: str = "pinned"

# Statuses for *stranded in-flight work* the chain must recover:
# ``in_progress`` plus ``hooked`` (real partial work another tool flipped
# after we claimed it, invisible to both ``bd ready`` and an
# in_progress-only query). Frozen states are deliberately excluded —
# ``blocked`` is modelled via the edge graph (reverted, not recovered)
# and ``pinned`` / ``deferred`` are human-parked (``pinned`` handled at
# close-time, see :func:`is_pinned`). Full rationale:
# ``__docs/Flows/StrandedBeadRecovery.md``.
RECOVERABLE_STATUSES: tuple[str, ...] = (IN_PROGRESS_STATUS, HOOKED_STATUS)


# Issue types that count as 'bugs' for the blocking-bug priority pass.
# A blocking bug (type in here AND dependent_count > 0) jumps the queue
# ahead of every other selection rule because fixing it unblocks more
# work. Keeping this as a tuple constant makes adding a sibling type
# (e.g. 'regression') a one-line change. DRY.
BLOCKING_BUG_TYPES: tuple[str, ...] = ("bug",)


def _exclude_type_arg() -> str:
    """Return the ``--exclude-type=...`` CLI arg for EXCLUDED_TYPES.

    DRY helper: this exact arg string is needed by every function that
    queries ``bd ready`` or ``bd list``. Centralising it here means a
    new excluded type is a one-line edit to :data:`EXCLUDED_TYPES`.
    """
    return f"--exclude-type={','.join(EXCLUDED_TYPES)}"


# Key on a bd-ready bead dict that names the bead's parent epic, if any.
#
# ``bd ready --json`` surfaces the parent as a top-level ``"parent"``
# field (string id) on each child bead, alongside the more verbose
# ``dependencies`` array. We pick the top-level field as canonical
# because it's a one-key lookup and lines up with bd's own
# ``--parent=<id>`` filter on ``bd ready`` / ``bd list``.
PARENT_EPIC_KEY: str = "parent"

# Legacy/fallback keys checked by :func:`extract_parent_epic_id` after
# ``PARENT_EPIC_KEY``. Order is most-likely-first. Keep this list short —
# every entry is a subprocess-free dict lookup, but extras add noise.
_PARENT_EPIC_FALLBACK_KEYS: tuple[str, ...] = ("parent_id", "epic_id")


def _bd_bin() -> str:
    """Return the validated ``bd`` executable path to invoke.

    Honors the ``BEADS_BIN`` environment variable so users with a
    non-standard install location can override the default ``bd``
    lookup on ``PATH``. An unset or empty value falls back to ``bd``.

    Security: ``BEADS_BIN`` is an attacker-reachable env var — anyone
    who can set it could otherwise redirect *every* bd call to an
    arbitrary binary. Before trusting it we:

      1. resolve it to an absolute path (via ``PATH`` if it's a bare
         command name, else from the given path), and
      2. verify the resolved target is a real, executable file.

    A bad override raises :class:`BeadsError` with a clear message
    rather than silently exec'ing junk or falling back. The unset case
    is *not* validated here — bd-on-PATH is resolved by ``subprocess``
    itself and a missing bd already surfaces a clear error in
    :func:`_run_bd`.
    """
    override = os.environ.get("BEADS_BIN")
    if not override:
        return DEFAULT_BD_BIN
    return _validate_beads_bin(override)


def _validate_beads_bin(override: str) -> str:
    """Resolve + verify a ``BEADS_BIN`` override to an absolute executable.

    Accepts either a bare command name (resolved via ``PATH``) or a
    path (resolved to absolute). Raises :class:`BeadsError` if the
    target can't be found or isn't an executable file.
    """
    # A bare name (no path separator) is resolved against PATH so
    # `BEADS_BIN=bd-dev` keeps working like a normal command lookup.
    if os.sep in override or (os.altsep and os.altsep in override):
        resolved = os.path.abspath(os.path.expanduser(override))
    else:
        found = shutil.which(override)
        if found is None:
            raise BeadsError(
                f"BEADS_BIN={override!r} not found on PATH "
                "(set it to an absolute path or an executable on PATH)"
            )
        resolved = os.path.abspath(found)

    if not os.path.isfile(resolved):
        raise BeadsError(
            f"BEADS_BIN={override!r} (resolved to {resolved!r}) is not a file"
        )
    if not os.access(resolved, os.X_OK):
        raise BeadsError(
            f"BEADS_BIN={override!r} (resolved to {resolved!r}) is not executable"
        )
    return resolved


def _validate_bead_id(bead_id: str) -> str:
    """Return ``bead_id`` unchanged if it matches the safe-id shape.

    Bead ids are passed straight to the ``bd`` binary as subprocess
    args. List-form ``subprocess.run`` stops shell injection, but a
    crafted id (leading dash → looks like a flag; whitespace, NUL,
    shell metachars) can still confuse bd's own argument parsing. We
    accept only the shape bd actually emits — ``[a-zA-Z0-9_.-]`` — and
    raise :class:`BeadsError` on anything else instead of failing
    silently downstream.

    Empty ids are rejected here: the public entry points already short
    -circuit falsy ids *before* calling this, so reaching it with an
    empty string is a programming error worth surfacing.

    A leading ``-`` is rejected even though the regex char class allows
    it mid-id: an id like ``--force`` matches ``[a-zA-Z0-9_.-]+`` but
    bd would parse it as a *flag*, not an id — the exact argument-
    confusion this guard exists to stop.
    """
    if not isinstance(bead_id, str) or not _BEAD_ID_RE.match(bead_id):
        raise BeadsError(
            f"invalid bead id {bead_id!r}: must match {_BEAD_ID_RE.pattern} "
            "(letters, digits, '_', '.', '-')"
        )
    if bead_id.startswith("-"):
        raise BeadsError(
            f"invalid bead id {bead_id!r}: must not start with '-' "
            "(bd would read it as a flag, not an id)"
        )
    return bead_id


class BeadsError(RuntimeError):
    """Raised when the ``bd`` CLI fails, is missing, or returns junk."""


def _parse_json_list(raw: str, context: str) -> list[Any]:
    """Parse JSON from bd output, expecting a list.

    DRY helper for the repeated pattern:
      1. Parse JSON (raise BeadsError on decode failure)
      2. Validate it's a list (raise BeadsError if not)
      3. Return the list for caller to filter

    Args:
        raw: Raw stdout from a bd command.
        context: Human-readable command description for error messages,
                 e.g. "bd ready --json" or "bd list --status=in_progress".

    Raises:
        BeadsError: On non-JSON output or non-list payload.
    """
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        snippet = raw[:200].replace("\n", " ")
        raise BeadsError(f"`{context}` returned non-JSON: {snippet!r}") from exc

    if not isinstance(items, list):
        raise BeadsError(
            f"`{context}` returned non-list payload: {type(items).__name__}"
        )
    return items


def _run_bd(*args: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run ``bd <args>`` and return stdout, or raise :class:`BeadsError`.

    Transient timeouts are retried per :data:`MAX_ATTEMPTS` with
    exponential backoff (:func:`_retry_backoff`). Non-zero exits,
    missing-binary (:class:`FileNotFoundError`) and not-executable
    (:class:`PermissionError`) errors are fatal and surfaced on the
    first failure — they're not transient, so we fail fast.
    """
    bd = _bd_bin()
    last_timeout: subprocess.TimeoutExpired | None = None

    for attempt in range(MAX_ATTEMPTS):
        if attempt > 0:
            time.sleep(_retry_backoff(attempt))

        try:
            proc = subprocess.run(
                [bd, *args],
                capture_output=True,
                text=True,
                # Force UTF-8 decoding. Without an explicit encoding,
                # ``text=True`` decodes subprocess output using
                # ``locale.getpreferredencoding()``, which on Windows is the
                # legacy code page (cp1252). ``bd``/git emit UTF-8 (em-dashes,
                # smart quotes, box-drawing glyphs, etc.), so a single
                # non-cp1252 byte kills the pipe-reader thread with a
                # UnicodeDecodeError; ``proc.stdout`` then comes back ``None``
                # and surfaces downstream as a misleading
                # "the JSON object must be str, bytes or bytearray, not
                # NoneType". ``errors="replace"`` guarantees a rogue byte can
                # never crash the chain.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            # Permanent — retrying won't make bd appear.
            raise BeadsError(f"`{bd}` not found on PATH — is beads installed?") from exc
        except PermissionError as exc:
            # Permanent — bd exists but isn't executable. Retrying won't
            # change the file mode, so fail fast instead of burning the
            # whole retry budget on a guaranteed-fatal error.
            raise BeadsError(
                f"`{bd}` is not executable (permission denied) — check its file mode"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            last_timeout = exc
            continue  # try again after backoff

        if proc.returncode != 0:
            # Real bd error (bead not found, already closed, etc.).
            # Permanent — surface immediately so callers can react.
            cmd = " ".join((bd, *args))
            stderr = (proc.stderr or proc.stdout or "").strip()
            raise BeadsError(f"`{cmd}` failed (exit {proc.returncode}): {stderr}")

        return proc.stdout

    # Exhausted MAX_ATTEMPTS — every one of them timed out.
    cmd = " ".join((bd, *args))
    raise BeadsError(
        f"`{cmd}` timed out after {timeout}s on each of {MAX_ATTEMPTS} attempts"
    ) from last_timeout


# ---------------------------------------------------------------------------
# Facade re-exports (bead_chain-7xv split)
# ---------------------------------------------------------------------------
# The read/query waterfall and the mutation + epic/gate/lint housekeeping were
# carved out of this once-monolithic module (1271 lines, ~2x the 600-line cap)
# into :mod:`beads_reads` and :mod:`beads_writes`. We re-import their public
# names *here*, at the bottom, so every existing call site keeps working
# verbatim:
#
#   * ``from .beads import next_ready, close, ...`` (the package consumers),
#   * flat ``import beads; beads.close_eligible_epics()`` (the test suite),
#   * and — crucially — monkeypatching: tests stub ``beads._run_bd`` /
#     ``beads._parse_json_list`` / ``beads.show`` by attribute assignment on
#     *this* module, and the moved functions resolve those three seams through
#     the live ``beads`` module object at call time, so the stubs are honoured.
#
# This import MUST stay at the end: the submodules do ``from . import beads``
# and read core symbols (``_run_bd``, the predicates, the constants) defined
# above, so the core must be fully bound before we trigger their import. The
# try/except mirrors the submodules so this loads both as a real package
# (runtime) and flat under bare pytest. The names below are part of the public
# API declared in ``__all__`` above, so they are intentional re-exports.
try:  # package context
    from .beads_reads import (  # noqa: F401
        extract_parent_epic_id,
        has_open_children,
        is_blocked,
        is_pinned,
        list_in_progress,
        list_recoverable_strands,
        memories,
        next_blocking_bug,
        next_in_progress,
        next_ready,
        next_ready_in_epic,
        open_blocker_ids,
        show,
    )
    from .beads_writes import (  # noqa: F401
        check_gates,
        claim,
        close,
        close_eligible_epics,
        has_epic_in_progress,
        lint_warnings,
        revert_to_open,
    )
except ImportError:  # flat context (bare ``import beads`` under pytest)
    from beads_reads import (  # type: ignore[no-redef]  # noqa: F401
        extract_parent_epic_id,
        has_open_children,
        is_blocked,
        is_pinned,
        list_in_progress,
        list_recoverable_strands,
        memories,
        next_blocking_bug,
        next_in_progress,
        next_ready,
        next_ready_in_epic,
        open_blocker_ids,
        show,
    )
    from beads_writes import (  # type: ignore[no-redef]  # noqa: F401
        check_gates,
        claim,
        close,
        close_eligible_epics,
        has_epic_in_progress,
        lint_warnings,
        revert_to_open,
    )
