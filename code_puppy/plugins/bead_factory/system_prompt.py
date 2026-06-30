"""Pin the active bead's contract into the compaction-protected system prompt.

The chain hands the bead to the implementor as a *user* message (the build
prompt rendered by :func:`prompt.format_bead_as_build`). On long tool-call
histories, code_puppy's compaction (``agents/_compaction.py``) summarizes or
truncates everything except the system message (``messages[0]``) and a recent
token-bounded tail. So the contract the implementor is graded against can
silently evaporate out from under it (epic bead-factory-cri).

The fix (bead-factory-5wv) is plugin-only and rides on a property of
``BaseAgent.get_full_system_prompt()``: it re-runs ``callbacks.on_load_prompt()``
fresh every run, and ``messages[0]`` is *always* compaction-protected. So a
``load_prompt`` fragment is pinned into the protected system prompt and survives
arbitrarily deep histories.

We emit only :func:`prompt.format_bead_content` — the bead's *own* fields, no
whole-project scaffolding — under a clear "protected task contract" header.
Keeping the boilerplate (memory digest, lint, bug-discovery protocol, done
checklist) out of the protected copy avoids duplicating it into every turn's
system prompt (the no-duplication design, bead-factory-462): that scaffolding
still rides the live build-prompt user message.

Fail-soft: any hiccup returns ``None`` (skip) rather than crashing prompt
assembly — a missing contract degrades to today's behaviour, it never bricks
the agent.
"""

from __future__ import annotations

from code_puppy.messaging.bus import emit_debug

from . import state
from .prompt import format_bead_content

__all__ = ["on_load_prompt", "is_pin_active"]

# Header wrapping the pinned bead so the implementor knows this block is the
# durable contract it's graded against — and that it intentionally outlives
# the rest of the (compactable) history. Kept as a module constant so the
# wording lives in one place.
_PROTECTED_CONTRACT_HEADER: str = (
    "## Protected Task Contract (bead-factory)\n"
    "\n"
    "The following is the active bead you are implementing. It is pinned into\n"
    "your system prompt so it survives context compaction across long\n"
    "tool-call histories — it is the contract the LLM inspectors grade you\n"
    "against. Treat it as authoritative even if earlier conversation has been\n"
    "summarized away.\n"
    "\n"
)


def is_pin_active() -> bool:
    """True when the ``load_prompt`` pin will inject the active bead's content.

    Mirrors the guard in :func:`on_load_prompt`: a bead-factory chain must be
    active *and* carry a current bead. This is the single predicate the arm
    sites consult (bead-factory-462) to decide whether the implementor's
    user-message build prompt can be slimmed to scaffolding-only — slimming
    is safe exactly when the bead's content is guaranteed to ride the
    compaction-protected system prompt instead.

    Fail-soft: any hiccup returns ``False`` (assume not pinned → render the
    full prompt) rather than risking a contract that exists in neither place.
    """
    try:
        return state.is_active() and state.get_state().current_bead is not None
    except Exception:  # noqa: BLE001 — never crash prompt assembly.
        return False


def on_load_prompt() -> str | None:
    """``load_prompt`` hook — pin the active bead's contract while a chain runs.

    Returns the bead-content render under the protected-contract header when a
    bead-factory chain is active and has a current bead; otherwise ``None`` so
    the fragment is dropped (``callbacks.on_load_prompt`` filters ``None``).

    Outside a chain this is a strict no-op: no chain, no pin. The render is
    near-pure (only the soft-failing epic-context fetch inside
    :func:`format_bead_content`), so this stays cheap to re-run every turn.
    """
    try:
        if not is_pin_active():
            return None
        bead = state.get_state().current_bead
        return _PROTECTED_CONTRACT_HEADER + format_bead_content(bead)
    except Exception as exc:  # noqa: BLE001 — never crash prompt assembly.
        emit_debug(f"[bead_factory] load_prompt pin skipped: {exc!r}")
        return None
