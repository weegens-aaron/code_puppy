"""Detect agent attempts to close a bead while bead-chain is in flight.

bead-chain delegates the close decision to wiggum's LLM judges: a bead
is only closed once the judges agree the goal is satisfied, via
:func:`bd close` invoked by the plugin itself (see ``beads.close``).

If an agent shells out to ``bd close`` (or ``bd update <id>
--status=closed``) mid-run, it short-circuits that contract and closes
the bead without any verdict. This module spots the bypass so the
``run_shell_command`` hook can block it with a reminder.

Pure functions, no side effects, trivially testable — kept in its own
module so :mod:`register_callbacks` doesn't grow regex baggage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from code_puppy.messaging import emit_warning

from . import state

__all__ = [
    "CloseGuardMatch",
    "detect_premature_close",
    "on_run_shell_command",
]


@dataclass(frozen=True)
class CloseGuardMatch:
    """Result of a premature-close detection."""

    pattern_name: str
    description: str


# Tokens that legitimately precede a fresh command in a shell pipeline
# or chain. Anchoring to one of these (or start-of-string) prevents
# false positives like ``echo "run: bd close cpp-1"`` from triggering
# the guard — a plain space is **not** a command boundary, so a bd
# token inside a quoted string won't match. Same boundary set as
# ``force_push_guard.detector``, deliberately, for consistency. We do
# not try to support env-var prefixes (``FOO=bar bd close ...``) — they
# blur the line between quoted text and real invocations, and they
# aren't a pattern agents reach for in practice. YAGNI.
_COMMAND_BOUNDARY = r"(?:^|&&|\|\||;|\|)\s*"

# Quoted-segment matcher used to blank out shell string literals before
# the boundary scan. Three flavours, ordered so the longest/most-specific
# prefix wins at each position:
#   * ANSI-C ``$'...'`` (``bead_chain-khg``) — honours backslash escapes,
#     so ``\'`` does NOT end the string. Must come first so the ``$``
#     prefix is consumed as a unit; otherwise the plain single-quote alt
#     below would match ``'a\'`` and stop at the *escaped* quote, leaving
#     the real string tail exposed to the boundary scan (a ``; bd close``
#     inside the literal would then false-positive at the ``;``).
#   * plain ``'...'`` — fully literal, no escapes (shell single quotes).
#   * double ``"..."`` — honours backslash escapes.
# We replace each quoted run — quotes included — with same-length
# whitespace so that:
#   * a ``bd close`` line *inside* a quoted arg (e.g. a git commit
#     message body) is no longer at a real command boundary, and
#   * a genuine ``bd close`` on its own line *outside* quotes still is.
# This is what lets us keep ``re.MULTILINE`` (so newline-separated
# commands are caught) without the false-positive in ``bead_chain-21d``.
_QUOTED_SEGMENT_RE = re.compile(
    r"""(?:\$'(?:\\.|[^'\\])*'|'[^']*'|"(?:\\.|[^"\\])*")""", re.DOTALL
)


def _blank_quoted(command: str) -> str:
    """Replace quoted string literals with equal-length whitespace.

    Keeps overall length/offsets stable (handy for debugging) while
    ensuring text *inside* quotes can never satisfy ``_COMMAND_BOUNDARY``.
    Newlines inside a quoted run become spaces, so an embedded
    ``\nbd close`` no longer looks like a fresh command; newlines
    *outside* quotes are untouched and still act as separators.

    Handles plain ``'...'``, double ``"..."`` *and* ANSI-C ``$'...'``
    quoting (``bead_chain-khg``); the latter honours backslash escapes
    so an escaped quote (``\'``) doesn't prematurely end the literal.
    """
    return _QUOTED_SEGMENT_RE.sub(lambda m: " " * len(m.group(0)), command)


# ---------------------------------------------------------------------------
# Heredoc-body blanking (bead_chain-khg)
# ---------------------------------------------------------------------------
#
# A heredoc body is literal text fed to a command's stdin, e.g.
#
#     cat <<EOF
#     bd close cpp-1
#     EOF
#
# Without blanking, the newline before ``bd close`` looks like a real
# command boundary under ``re.MULTILINE`` and false-positives. Quote
# blanking doesn't help — heredoc bodies aren't quoted. So we blank the
# body (newlines preserved, offsets stable) up to — but not including —
# the terminator line.
#
# Supported openers: ``<<EOF``, ``<< EOF``, ``<<-EOF`` (tab-stripped
# terminator), and quoted delimiters ``<<'EOF'`` / ``<<"EOF"``. We run
# this BEFORE quote-blanking so the raw terminator line is intact and
# reliably found; a ``<<EOF`` that lives *inside* quotes simply has no
# matching terminator line and is left alone (see conservative fallback).
#
# Conservative fallback (anti-false-negative): if no terminator line is
# found, we do NOT blank anything. A real ``bd close`` slipping through
# (false negative — a bead closed without judges) is strictly worse than
# a spurious block (false positive — a mild annoyance), so when in doubt
# we leave the text scannable.
_HEREDOC_OPENER_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")


def _find_heredoc_terminator(
    command: str, start: int, delim: str, dash: bool
) -> int | None:
    """Return the offset of the terminator line for a heredoc, or None.

    Scans line-by-line from ``start``; a line equal to ``delim`` ends the
    body. With ``<<-`` (``dash``), leading tabs on the terminator are
    stripped before comparison, matching shell semantics.
    """
    pos = start
    while pos <= len(command):
        nl = command.find("\n", pos)
        line_end = nl if nl != -1 else len(command)
        line = command[pos:line_end]
        candidate = line.lstrip("\t") if dash else line
        if candidate == delim:
            return pos
        if nl == -1:
            return None
        pos = nl + 1
    return None


def _blank_heredocs(command: str) -> str:
    """Blank heredoc bodies with equal-length whitespace (newlines kept).

    Runs before :func:`_blank_quoted`. Only blanks a body when a matching
    terminator line is found; otherwise leaves the text untouched so a
    genuine ``bd close`` can never hide behind a malformed heredoc.
    """
    if "<<" not in command:
        return command
    result = list(command)
    for opener in _HEREDOC_OPENER_RE.finditer(command):
        dash = command[opener.start() + 2 : opener.start() + 3] == "-"
        delim = opener.group(2)
        nl = command.find("\n", opener.end())
        if nl == -1:
            continue  # single-line: no body to blank
        body_start = nl + 1
        term_start = _find_heredoc_terminator(command, body_start, delim, dash)
        if term_start is None:
            continue  # conservative: no terminator → leave scannable
        for i in range(body_start, term_start):
            if result[i] != "\n":
                result[i] = " "
    return "".join(result)


# ---------------------------------------------------------------------------
# Flag-argument blanking (bead_chain-4hy)
# ---------------------------------------------------------------------------
#
# When the ``on_run_shell_command`` hook receives a command, shell-level
# quote processing may already have occurred — the runtime parses the
# agent's JSON tool-call, extracts the ``command`` string, and hands it
# to the hook *without* surrounding quotes. So a command like:
#
#     bd update foo --append-notes "text about bd close"
#
# arrives as:
#
#     bd update foo --append-notes text about bd close
#
# ``_blank_quoted`` finds nothing to blank, and the regex then matches
# ``bd close`` inside what was originally argument text.
#
# Fix: after blanking quoted strings, blank the *values* of known text-
# consuming flags (``--append-notes``, ``--description``, ``-m``, etc.)
# up to the next shell separator (``&&``, ``||``, ``;``, ``|``,
# newline). This makes the detector immune to argument text regardless
# of whether quotes survived transit.

# Flags whose value is free-form text that may contain bd-like tokens.
# Long flags use ``--`` prefix (unambiguous). Short ``-m`` needs a
# lookbehind to avoid matching inside longer flags like ``--some-m``.
_TEXT_FLAG_RE = re.compile(
    r"(?:"
    r"--(?:append-notes|notes|description|title|message)\b"
    r"|(?<!\S)-m\b"
    r")(?:=|\s+)",
)

# Shell separators that end a flag's argument value.
_SHELL_SEP_RE = re.compile(r"(?:&&|\|\||[;\n|])")


def _blank_flag_args(command: str) -> str:
    """Blank values of text-consuming flags up to the next shell separator.

    Like :func:`_blank_quoted`, replaces with equal-length whitespace to
    keep offsets stable. Designed to run *after* ``_blank_quoted`` so it
    catches the case where quotes were stripped before the hook received
    the command (``bead_chain-4hy``).
    """
    result = list(command)
    for match in _TEXT_FLAG_RE.finditer(command):
        value_start = match.end()
        sep_match = _SHELL_SEP_RE.search(command, value_start)
        value_end = sep_match.start() if sep_match else len(command)
        for i in range(value_start, value_end):
            result[i] = " "
    return "".join(result)


# Optional path prefix (``/usr/local/bin/``, ``./``, ``$BEADS_BIN/``...).
# Anything non-whitespace ending in a slash is fine; the basename has to
# be exactly ``bd``.
_BD_INVOCATION = r"(?:\S*/)?bd"

# Match ``bd close [...]`` — any subcommand-style invocation of close
# regardless of trailing flags or bead id.
_BD_CLOSE_RE = re.compile(
    rf"{_COMMAND_BOUNDARY}{_BD_INVOCATION}\s+close\b", re.MULTILINE
)

# Match ``bd update <id> --status=closed`` or ``bd update <id> --status
# closed``. We restrict the gap between ``update`` and ``--status`` to
# the same command (no shell separators) so a later chained command
# doesn't get blamed on the earlier ``bd update --claim``.
_BD_UPDATE_STATUS_CLOSED_RE = re.compile(
    rf"{_COMMAND_BOUNDARY}{_BD_INVOCATION}\s+update\b[^|;&]*?"
    r"--status[=\s]+closed\b",
    re.MULTILINE,
)


def detect_premature_close(command: str) -> CloseGuardMatch | None:
    """Return a :class:`CloseGuardMatch` if ``command`` would close a bead.

    Returns ``None`` for unrelated commands and for legitimate
    ``bd update --claim`` / ``--status=in_progress`` calls. The check
    is intentionally lenient about *which* bead is being closed: while
    bead-chain is active, the agent has no business closing any bead
    — that's the chain's job.
    """
    # Cheap pre-filter: skip regex work entirely when the command can't
    # possibly invoke bd. ``"bd"`` appears in plenty of unrelated
    # strings, but it's a small enough set to be worth the savings.
    if "bd" not in command:
        return None

    # Blank out heredoc bodies first (``bead_chain-khg``): their literal
    # text isn't quoted, so a ``bd close`` line inside ``<<EOF ... EOF``
    # would otherwise look like a fresh command at a newline boundary.
    #
    # Then blank quoted string literals so text inside an argument
    # (e.g. a multi-line git commit message that happens to start a line
    # with "bd close", or an ANSI-C ``$'...'`` literal) can never be
    # mistaken for a real command at a boundary. Real, unquoted
    # invocations are unaffected. See ``bead_chain-21d`` for the
    # re.MULTILINE false-positive this guards, and ``bead_chain-khg``
    # for the ANSI-C / heredoc edge cases.
    #
    # Finally blank text-consuming flag arguments (--append-notes, -m,
    # etc.) to handle the case where quotes were stripped before the hook
    # received the command (``bead_chain-4hy``).
    scannable = _blank_flag_args(_blank_quoted(_blank_heredocs(command)))

    if _BD_CLOSE_RE.search(scannable):
        return CloseGuardMatch(
            pattern_name="bd close",
            description="Direct `bd close` bypasses the LLM judges.",
        )
    if _BD_UPDATE_STATUS_CLOSED_RE.search(scannable):
        return CloseGuardMatch(
            pattern_name="bd update --status=closed",
            description=("Setting status=closed on a bead bypasses the LLM judges."),
        )
    return None


# ---------------------------------------------------------------------------
# run_shell_command hook
# ---------------------------------------------------------------------------
#
# Lives here (next to the detector) rather than in ``register_callbacks``
# because the two are one cohesive guard: change one and you'll almost
# certainly want to glance at the other. The hook is registered by
# ``register_callbacks`` at module scope — there's no ordering
# dependency on any other plugin, and the early ``state.is_active()``
# check makes it a cheap no-op when the chain isn't running.


async def on_run_shell_command(
    context: Any,
    command: str,
    cwd: str | None = None,
    timeout: int = 60,
) -> dict[str, Any] | None:
    """Block premature `bd close` / `bd update --status=closed` calls.

    Returns ``None`` (allow) unless **both** conditions hold:

      * bead-chain is currently active, AND
      * the command would close a bead.

    In that case returns a ``{"blocked": True, ...}`` dict whose
    ``error_message`` is surfaced verbatim to the agent as the shell
    command's error output — a teachable moment reminding the agent
    the LLM judges are the only legitimate closer.
    """
    del context, cwd, timeout

    if not state.is_active():
        return None

    match = detect_premature_close(command)
    if match is None:
        return None

    current = state.get_state().current_bead_id or "the active bead"
    reminder = (
        f"🛑 bead-chain blocked `{match.pattern_name}`.\n"
        f"  {match.description}\n"
        f"  bead-chain is currently driving bead {current} through "
        f"wiggum's /goal mode. The bead will be closed automatically "
        f"once the LLM judges sign off — do NOT close it yourself.\n"
        f"  Keep working on the task. If you believe the bead is "
        f"complete, summarize what you did and let the judges decide."
    )
    emit_warning(reminder)
    return {
        "blocked": True,
        "reasoning": f"Premature close attempted ({match.pattern_name})",
        "error_message": reminder,
    }
