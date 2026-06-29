"""Banned-term grep-gate for the ``bead_factory`` plugin source.

This is the *final enforcement gate* for epic ``bead-factory-881`` (dissolve
the inherited ``goal`` / ``wiggum`` / ``judge`` / ``bead-chain`` vocabulary).
It is deliberately a **text scan** -- it greps every ``.py`` file under
``code_puppy/plugins/bead_factory`` and fails on any banned term. That makes
it the blunt, last-line backstop that complements the surgical, AST-based
cross-import guard in ``test_bead_factory_chain_ordering.py``: the import
guard proves *behaviour* (no sibling-plugin imports), this gate proves the
*words* never creep back into docstrings, comments, banners or prose.

Canonical vocabulary rules live in
``docs/adr/0001-bead-factory-canonical-vocabulary.md``. The bans:

* ``wiggum``           -- a dissolved source plugin; zero legitimate uses.
* ``judge`` / ``judges`` -- renamed to ``inspector(s)``; zero legitimate uses.
* ``bead-chain`` / ``bead_chain`` -- the OLD plugin *name*; renamed to
  ``bead-factory``. (The generic word ``chain`` -- ``chain_driver.py``,
  "bead chain mechanism" -- is explicitly KEPT and is not banned.)
* ``goal`` *as loop vocabulary* -- the loop verb is now ``build``. Plain
  English ("the bead's stated goal", "the goal is to ...") is fine; only
  loop-flavoured identifiers/banners (``goal_loop``, ``GoalInspection``,
  ``GOAL MODE``, ``goal mode``, ``bf_goal_*`` ...) are banned.
"""

from __future__ import annotations

import re
from pathlib import Path

import code_puppy.plugins.bead_factory as pkg

# Directory whose .py source is gated. Resolved off the imported package so
# the test follows the code if the package is ever relocated.
_PKG_DIR = Path(pkg.__file__).parent


def _source_files() -> list[Path]:
    """Every tracked-ish ``.py`` source file in the package.

    ``__pycache__`` is skipped -- stale ``.pyc`` artefacts from the
    pre-rename world (``goal_loop.pyc``, ``loop_state.pyc``) are not source
    and must never trip the gate.
    """
    return [p for p in sorted(_PKG_DIR.rglob("*.py")) if "__pycache__" not in p.parts]


# --- Banned patterns --------------------------------------------------------
#
# Each entry: (human label, compiled regex). Regexes are case-insensitive
# unless they encode case on purpose (the ALL-CAPS banner forms).

_HARD_BANS: list[tuple[str, re.Pattern[str]]] = [
    # Dissolved source plugin -- no survivors (provenance docstrings were
    # scrubbed in bead-factory-qmx; the alias was removed in bead-factory-89g).
    ("wiggum", re.compile(r"wiggum", re.IGNORECASE)),
    # judges -> inspectors. Bare-word boundary so we never flag, say,
    # "prejudge" inside an unrelated dependency -- but there are none here.
    ("judge", re.compile(r"\bjudges?\b", re.IGNORECASE)),
    # OLD plugin NAME only. ``bead-chain`` / ``bead_chain`` -> ``bead-factory``.
    # The generic standalone word ``chain`` is intentionally NOT matched.
    ("bead-chain (plugin name)", re.compile(r"bead[-_]chain", re.IGNORECASE)),
]

# 'goal' is banned ONLY as loop vocabulary. These shapes are what the rename
# (bead-factory-89g) replaced with ``build``; plain-English "goal" is allowed.
_GOAL_LOOP_VOCAB = re.compile(
    r"""
    (?:_goal\w*)        # _goal, _goal_inspectors, run_goal...
    | (?:\bgoal_\w+)    # goal_loop, goal_state, goal_max, goal_mode...
    | (?:\bGoal[A-Z]\w*)  # GoalInspection, GoalInspectionOutput (CamelCase)
    | (?:\bbf_goal\w*)  # bf_goal_max_iterations config key
    | (?:\bgoal[\s-]+(?:mode|loop|iteration))  # "goal mode" / "goal-loop"
    | (?:GOAL\s+(?:MODE|COMPLETE|INCOMPLETE|STOPPED))  # banner all-caps
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _scan(pattern: re.Pattern[str]) -> list[str]:
    """Return ``file:line: text`` hits for ``pattern`` across the package."""
    hits: list[str] = []
    for path in _source_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                rel = path.relative_to(_PKG_DIR)
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_no_hard_banned_terms():
    """``wiggum`` / ``judge`` / ``bead-chain`` must not appear anywhere."""
    offenders: list[str] = []
    for label, pattern in _HARD_BANS:
        for hit in _scan(pattern):
            offenders.append(f"[{label}] {hit}")
    assert not offenders, (
        "Banned vocabulary found in bead_factory source -- see "
        "docs/adr/0001-bead-factory-canonical-vocabulary.md:\n" + "\n".join(offenders)
    )


def test_no_goal_loop_vocabulary():
    """``goal`` must not survive as loop vocabulary (the verb is ``build``)."""
    offenders = _scan(_GOAL_LOOP_VOCAB)
    assert not offenders, (
        "Loop-vocabulary 'goal' found -- rename to 'build' "
        "(plain-English 'goal' is fine, loop identifiers/banners are not):\n"
        + "\n".join(offenders)
    )


def test_generic_goal_prose_is_allowed():
    """Guard the guard: plain-English 'goal' must NOT trip the loop-vocab gate.

    Without this, a future tightening of ``_GOAL_LOOP_VOCAB`` could start
    flagging legitimate prose like "the bead's stated goal" and force a
    pointless reword. These strings model the allowed shapes.
    """
    allowed = [
        "the bead's stated goal, file it as a bd bead",
        "completing the current bead's stated goal.",
        "finish the original goal, and present both",
        "The goal is to give humans a true signal",
    ]
    for line in allowed:
        assert not _GOAL_LOOP_VOCAB.search(line), (
            f"loop-vocab gate wrongly flagged generic prose: {line!r}"
        )


def test_scan_actually_finds_files():
    """Sanity: the gate is pointed at real source, not an empty set."""
    files = _source_files()
    assert files, "no .py source discovered under bead_factory"
    names = {p.name for p in files}
    # Spot-check a couple of known modules so a mis-resolved path is loud.
    assert "build_loop.py" in names
    assert "chain_driver.py" in names
