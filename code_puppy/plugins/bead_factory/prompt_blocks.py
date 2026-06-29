"""Bead -> build-prompt block builders.

The pure (or soft-failing) helpers that turn a ``bd ready``-shaped bead dict
into the individual prompt *sections* :func:`prompt.format_bead_as_build`
assembles. Split out of :mod:`prompt` during the bead_factory migration to
keep both files under the 600-line plugin cap and to give the prompt-shape
tests one obvious target. Behavior is unchanged.

The only impure helpers are the ``_fetch_*`` functions, which shell out to
``bd`` and soft-fail (to ``None`` / ``{}`` / ``[]``) so the rest of the
formatter stays deterministic.
"""

from __future__ import annotations

from typing import Any

from .beads import BeadsError
from .beads_reads import extract_parent_epic_id, memories, show
from .beads_writes import lint_warnings


# Char cap for the epic-description excerpt injected into the build prompt.
# Big enough to convey purpose, small enough that ten chained beads under
# the same epic don't blow the LLM's context budget on duplicate prose.
_EPIC_EXCERPT_LIMIT: int = 280

# --- bd memory layer <-> host Kennel policy (coverage-audit gap FB-6) -------
#
# POLICY (one line): bead-factory surfaces *bd's* project-scoped memory
# layer (``bd remember``/``bd memories``, which travels with the Dolt DB)
# into the build prompt and nudges agents to write back to it; it does NOT
# bridge to the host runtime's Kennel — the two are deliberately separate
# (bd memories = this project's shared facts; Kennel = the host agent's
# cross-repo diary), and coupling them would tie bead-factory to a
# host-specific backend. We document the split rather than bridge it.
#
# Caps for the persistent-memory digest injected into the build prompt.
# Memories are high-signal but unbounded over a project's life; we cap
# both the count and per-entry length so a long-lived bd DB can't blow
# the LLM context budget. Newest-by-bd-order entries win the slots.
_MEMORY_DIGEST_MAX_ENTRIES: int = 12
_MEMORY_EXCERPT_LIMIT: int = 280


def _first_paragraph_excerpt(text: str, *, limit: int = _EPIC_EXCERPT_LIMIT) -> str:
    """Return the first paragraph of ``text``, truncated to ``limit`` chars.

    Splits on the first blank line (``\\n\\n``) to grab just the lede, then
    word-boundary-truncates with an ellipsis if still too long. Empty /
    None-ish input → ``""``. Pure function, trivially testable.
    """
    if not text:
        return ""
    paragraph = text.split("\n\n", 1)[0].strip()
    if len(paragraph) <= limit:
        return paragraph
    cut = paragraph[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _fetch_epic_context(epic_id: str) -> tuple[str, str] | None:
    """Return ``(title, excerpt)`` for an epic, or ``None`` if unavailable.

    Soft-fails by design: any :class:`BeadsError` (bd missing, timeout,
    bead not found, garbage JSON) yields ``None`` and lets the caller
    fall back to a minimal "Parent epic: <id>" line. Epic context is a
    nice-to-have for the LLM — we never want it to crash the build
    prompt or stall the chain.
    """
    try:
        epic = show(epic_id)
    except BeadsError:
        return None
    if not epic:
        return None
    title = str(epic.get("title", "")).strip()
    excerpt = _first_paragraph_excerpt(str(epic.get("description", "")))
    return title, excerpt


def _fetch_memory_digest() -> dict[str, str]:
    """Return bd's persistent memories as ``{key: insight}``, or ``{}``.

    Soft-fails by design (same rationale as :func:`_fetch_epic_context`):
    any :class:`BeadsError` — bd missing, timeout, this bd build lacking
    a ``memories`` subcommand, garbage JSON — yields ``{}`` so the build
    prompt renders without a memory block rather than crashing the
    chain. The memory digest is a warm-start nicety, never a hard
    dependency.
    """
    try:
        return memories()
    except BeadsError:
        return {}


def _fetch_lint_warnings(bead_id: str) -> list[str]:
    """Return ``bd lint`` missing-section warnings for a bead, or ``[]``.

    Soft-fails by design (same rationale as :func:`_fetch_memory_digest`):
    any :class:`BeadsError` — bd missing, timeout, this bd build lacking a
    ``lint`` subcommand, garbage JSON — yields ``[]`` so the build prompt
    renders without a lint block rather than crashing the chain. Running
    the lint at prompt-build time means the *claim path* (which builds the
    build prompt immediately after ``bd update --claim``) always consults
    the template contract — coverage-audit gap FB-5.
    """
    try:
        return lint_warnings(bead_id)
    except BeadsError:
        return []


def _format_memory_digest_block(mems: dict[str, str]) -> str:
    """Render a ``## Persistent Memories`` prompt section, or ``""``.

    Bridges bd's memory layer into the build prompt so a freshly-spawned
    working agent starts warm — it sees the project's durable insights
    (architecture decisions, gotchas, prior-bead learnings) the same way
    a human running ``bd prime`` would (coverage-audit gap FB-6).

    Contract:

    * Non-empty dict → a block beginning with the literal ``## Persistent
      Memories`` heading, one ``- key: excerpt`` bullet per memory
      (capped at :data:`_MEMORY_DIGEST_MAX_ENTRIES`, each excerpt
      truncated to :data:`_MEMORY_EXCERPT_LIMIT`), then a trailing blank
      line so it slots between prompt sections.
    * Empty / non-dict → ``""`` (prompt byte-for-byte unchanged).

    Pure function, trivially testable — the impure fetch lives in
    :func:`_fetch_memory_digest`.
    """
    if not isinstance(mems, dict) or not mems:
        return ""
    lines = [
        "## Persistent Memories",
        "Durable project knowledge from bd's memory layer (`bd memories`). "
        "Treat as background context — verify before relying on it:",
    ]
    for key, insight in list(mems.items())[:_MEMORY_DIGEST_MAX_ENTRIES]:
        text = _first_paragraph_excerpt(str(insight), limit=_MEMORY_EXCERPT_LIMIT)
        if not text:
            continue
        lines.append(f"- {key}: {text}")
    # All entries truncated to nothing (pathological) -> emit nothing.
    if len(lines) == 2:
        return ""
    return "\n".join(lines) + "\n\n"


def _format_epic_metadata_lines(bead: dict[str, Any]) -> list[str]:
    """Build the ``Parent epic: ...`` metadata lines for the build prompt.

    Returns ``[]`` when the bead has no parent epic, so the caller can
    blindly ``extend()`` without conditionals.

    Three outcomes:

    * no parent epic → ``[]``
    * parent epic found and fetched → ``['- Parent epic: id — title',
      '  > excerpt']`` (the excerpt line is omitted if blank)
    * parent epic known but ``bd show`` failed → ``['- Parent epic: id']``
      (we still tell the LLM this bead is part of a larger effort)
    """
    epic_id = extract_parent_epic_id(bead)
    if not epic_id:
        return []

    context = _fetch_epic_context(epic_id)
    if context is None:
        return [f"- Parent epic: {epic_id}"]

    title, excerpt = context
    label = f"{epic_id} — {title}" if title else epic_id
    lines = [f"- Parent epic: {label}"]
    if excerpt:
        lines.append(f"  > {excerpt}")
    return lines


def _format_labels_line(bead: dict[str, Any]) -> list[str]:
    """Return a ``- Labels: a, b, c`` metadata line, or ``[]`` when absent.

    ``labels`` is a list of strings on the ``bd ready --json`` record
    bead-factory already hands to :func:`format_bead_as_build` (verified
    present on this bd build — coverage-audit gap FB-7, anatomy #3), but
    the formatter historically never read it. Labels are the bead's
    cross-cutting tags (e.g. ``bead-factory``, ``prompt``, ``security``) —
    cheap, high-signal context for framing the work.

    Returns a single-element list so the caller can blindly ``extend()``
    the metadata block, matching :func:`_format_epic_metadata_lines`.

    Contract:

    * Non-empty list of stringy labels → ``['- Labels: a, b, c']``
      (each label stripped; empties/whitespace-only entries dropped).
    * Missing / empty / non-list / all-empty → ``[]`` (prompt unchanged).

    Pure function, trivially testable.
    """
    raw = bead.get("labels")
    if not isinstance(raw, (list, tuple)):
        return []
    labels = [str(item).strip() for item in raw if str(item).strip()]
    if not labels:
        return []
    return [f"- Labels: {', '.join(labels)}"]


# Non-gating, context-bearing edge types bead-factory surfaces in the build
# prompt (coverage-audit gap FB-11; dependency#2).
#
# These six edges carry *context* the working agent (and the LLM inspectors)
# otherwise can't see: provenance (``discovered-from``), causal bug links
# (``caused-by``), validating tests (``validates``), and plain related
# work (``related`` / ``relates-to`` / ``tracks``). The field guide
# classifies all six as Informational — they do NOT gate readiness, and
# bead-factory deliberately keeps it that way (see
# :data:`beads.BLOCKING_DEP_TYPES`). Surfacing them here is purely about
# context; gating behaviour is untouched.
#
# Mapping value is the human-readable gloss prefixed to the target id in
# the rendered block. Insertion order also defines the *display* order so
# the most causally-load-bearing edges (provenance / cause / validation)
# lead. Adding a future context edge is a one-line edit. DRY.
_CONTEXT_EDGE_GLOSSES: dict[str, str] = {
    "discovered-from": "Discovered while working on",
    "caused-by": "Caused by",
    "validates": "Validates",
    "related": "Related to",
    "relates-to": "Relates to",
    "tracks": "Tracks",
}


def _edge_type(dep: dict[str, Any]) -> str:
    """Return a dependency edge's lowercased type, shape-agnostic.

    bd reports edges with two different field names depending on the
    command: ``bd ready``/``bd list`` records carry ``type``, while
    ``bd show`` records carry ``dependency_type``. We accept either so
    this formatter works regardless of which shape upstream hands us.
    """
    raw = dep.get("type") or dep.get("dependency_type") or ""
    return str(raw).strip().lower()


def _edge_target_id(dep: dict[str, Any]) -> str:
    """Return the id of the bead an edge points at, shape-agnostic.

    ``bd ready``/``bd list`` name the far end ``depends_on_id``; the
    ``bd show`` dependency records inline the related bead and name its
    id ``id``. Prefer the explicit ``depends_on_id`` so we never mistake
    a ``bd show`` edge's own id for its target.
    """
    raw = dep.get("depends_on_id") or dep.get("id") or ""
    return str(raw).strip()


def _format_related_context_block(bead: dict[str, Any]) -> str:
    """Return a ``## Related Context`` prompt section, or ``""`` when absent.

    Folds the bead's *non-gating* context edges — ``discovered-from``,
    ``caused-by``, ``validates``, ``related``, ``relates-to``,
    ``tracks`` (see :data:`_CONTEXT_EDGE_GLOSSES`) — into a short block
    so the working agent (and the LLM inspectors) can see the bead's
    provenance, causal bug link, validating test, and related work
    instead of working blind (coverage-audit gap FB-11). The block opens
    with a one-line caveat making explicit these links are background,
    not blockers.

    Reads the ``dependencies`` array that ``bd ready --json`` already
    hands :func:`format_bead_as_build`. Each edge is rendered
    ``- <gloss> <target-id>`` (with ``: <title>`` appended when the edge
    record carries one, as ``bd show`` records do). Entries are emitted
    grouped by :data:`_CONTEXT_EDGE_GLOSSES` insertion order, then in the
    order they appear within the array; duplicate ``(type, target)``
    pairs are dropped.

    Contract:

    * At least one recognised context edge → a block beginning with the
      ``## Related Context`` heading, the caveat line, the edge lines,
      then a trailing blank line so it slots between prompt sections.
    * No ``dependencies`` / no *context* edges (only gating/structural
      edges like ``blocks`` / ``parent-child``) / non-list / malformed →
      ``""`` (prompt byte-for-byte unchanged).

    Gating behaviour is untouched: this helper never inspects or alters
    readiness — it is pure presentation. Pure function, trivially
    testable.
    """
    deps = bead.get("dependencies")
    if not isinstance(deps, (list, tuple)):
        return ""

    # Collect (edge_type -> list of "target[: title]" lines), de-duped.
    seen: set[tuple[str, str]] = set()
    by_type: dict[str, list[str]] = {}
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        edge_type = _edge_type(dep)
        if edge_type not in _CONTEXT_EDGE_GLOSSES:
            continue
        target = _edge_target_id(dep)
        if not target:
            continue
        key = (edge_type, target)
        if key in seen:
            continue
        seen.add(key)
        title = str(dep.get("title", "")).strip()
        suffix = f": {title}" if title else ""
        by_type.setdefault(edge_type, []).append(f"{target}{suffix}")

    if not by_type:
        return ""

    lines = [
        "## Related Context",
        "These links are non-gating background (provenance, causal bug "
        "links, validating tests, related work) — they do NOT block this "
        "bead:",
    ]
    for edge_type, gloss in _CONTEXT_EDGE_GLOSSES.items():
        for entry in by_type.get(edge_type, []):
            lines.append(f"- {gloss} {entry}")
    return "\n".join(lines) + "\n\n"


def _format_design_block(bead: dict[str, Any]) -> str:
    """Return a ``## Design`` prompt section, or ``""`` when absent.

    ``design`` is bd's ADR/design-rationale field — the conventional home
    for ``decision``- and ``spike``-type beads (coverage-audit gap FB-7,
    anatomy #2). Unlike ``acceptance_criteria``/``labels``, bd *omits*
    the key entirely when it's unset, so this helper soft-defaults via
    ``.get`` and renders only when a non-empty string is present.

    Contract (mirrors :func:`_format_acceptance_criteria_block`):

    * Non-empty ``design`` → a block beginning with the literal
      ``## Design`` heading, then the design text, then a trailing blank
      line so it slots between prompt sections.
    * Missing / empty / whitespace-only / non-string → ``""`` (prompt
      byte-for-byte unchanged).
    * If the stored value already leads with a ``Design`` heading we
      don't double it up — the value is emitted as-is.

    Pure function, trivially testable.
    """
    raw = bead.get("design", "")
    if not isinstance(raw, str):
        return ""
    design = raw.strip()
    if not design:
        return ""
    heading = "## Design"
    if design.lstrip("# ").lower().startswith("design"):
        body = design
    else:
        body = f"{heading}\n{design}"
    return f"{body}\n\n"


def _format_acceptance_criteria_block(bead: dict[str, Any]) -> str:
    """Return a ``## Acceptance Criteria`` prompt section, or ``""`` if absent.

    ``acceptance_criteria`` is already a key on the ``bd ready --json``
    record bead-factory hands to :func:`format_bead_as_build`, but the
    formatter historically never read it — so the LLM inspectors verified
    completion against a contract the prompt never showed the agent
    (coverage-audit gap FB-2).

    Contract:

    * Non-empty ``acceptance_criteria`` → a block beginning with the
      literal ``## Acceptance Criteria`` heading, then the criteria text,
      then a trailing blank line so it slots between prompt sections.
    * Missing / empty / whitespace-only / non-string → ``""`` (the
      prompt is byte-for-byte unchanged, preserving old behaviour).
    * If the stored value *already* leads with the ``## Acceptance
      Criteria`` heading (some beads embed it in the field text), we
      don't double it up — the value is emitted as-is under the blank
      line.

    Pure function, trivially testable.
    """
    raw = bead.get("acceptance_criteria", "")
    if not isinstance(raw, str):
        return ""
    criteria = raw.strip()
    if not criteria:
        return ""
    heading = "## Acceptance Criteria"
    if criteria.lstrip("# ").lower().startswith("acceptance criteria"):
        body = criteria
    else:
        body = f"{heading}\n{criteria}"
    return f"{body}\n\n"


def _format_lint_warnings_block(warnings: list[str]) -> str:
    """Return a ``## Template Lint Warnings`` block, or ``""`` when clean.

    Renders the missing-section names ``bd lint`` reported for this bead
    (coverage-audit gap FB-5). Where
    :func:`_format_acceptance_criteria_block` shows the agent *what's
    present*, this block shows *what the template contract says is
    missing* — the two pair up so the agent (and the LLM inspectors, who
    grade against the same contract) aren't blind to a section a
    ``--graph`` import silently dropped.

    Contract:

    * Non-empty ``warnings`` → a ``## Template Lint Warnings`` heading,
      a one-line explanation, a bullet per missing section, then a
      trailing blank line so it slots between prompt sections.
    * Empty list → ``""`` (the prompt is byte-for-byte unchanged,
      preserving old behaviour and producing nothing for clean beads).

    Pure function, trivially testable.
    """
    if not warnings:
        return ""
    bullets = "\n".join(f"- {w}" for w in warnings)
    return (
        "## Template Lint Warnings\n"
        "`bd lint` flagged this bead as missing recommended section(s) for "
        "its issue type. The LLM inspectors grade completion against these "
        "contracts, so treat each missing section as part of the work: "
        "satisfy it (or its intent) before you consider the bead done.\n"
        f"{bullets}\n\n"
    )
