"""State for the bead-factory plugin.

Mirrors a tiny-singleton pattern. Behavior lives in
``chain_driver.py`` and ``lifecycle.py`` — this module is a dumb data box.

Thread-safety (known limitation)
--------------------------------
The singleton is **not** thread-safe: ``_STATE`` is a bare module-level
instance with no lock around its mutators. This is deliberate and safe in
practice. bead-factory's coordinating hooks (command / turn-end /
turn-cancel) all fire on code_puppy's single interactive event loop, and
they never run concurrently with one another.

Note (bead_chain-u0b): ``_on_interactive_turn_end`` now off-loads its
``bd`` subprocess work to a worker thread via ``asyncio.to_thread`` so the
event loop stays responsive. That work — ``close_current_bead_success``
then ``activate_next_bead`` — mutates this box from the worker thread.
There is still no contended writer: the two calls are ``await``ed
*sequentially* (at most one worker thread is ever in flight), and the
turn-end hook does not re-enter while its own thread is running, so the
state has exactly one mutator at any instant. If bead-factory ever fans
these calls out concurrently, or grows an independent background thread
that mutates state, this box must gain a lock (or move to per-run
dependency injection). Until then, a lock would be pure YAGNI ceremony.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ChainState",
    "get_state",
    "is_active",
    "reset",
    "start",
    "stop",
]


@dataclass
class ChainState:
    """Whether the chain is engaged, and what bead it's currently chewing on.

    We hold the **full bead dict**, not just its id, so callers can peek
    at fields like the parent epic without having to round-trip through
    ``bd show``. Code that only needs the id can use the
    :pyattr:`current_bead_id` convenience property.
    """

    active: bool = False
    current_bead: dict[str, Any] | None = None
    completed_count: int = 0
    # Optional safety brake: stop the chain after this many beads have
    # been completed in the current run. None = no cap (run forever).
    # Set by /bead-factory --max=N; reset to None on stop().
    max_iterations: int | None = None

    @property
    def current_bead_id(self) -> str | None:
        """Convenience accessor for ``current_bead['id']`` (or ``None``).

        Pure read-only — to set the active bead, assign to
        :pyattr:`current_bead` directly with the bd-ready dict. This keeps
        the rename surgical: callers that only need the id stay unchanged.
        """
        if self.current_bead is None:
            return None
        bead_id = self.current_bead.get("id")
        return str(bead_id) if bead_id is not None else None

    def start(self) -> None:
        self.active = True
        self.current_bead = None
        # completed_count is reset on every fresh start() so each
        # /bead-factory run reports its own tally.
        self.completed_count = 0

    def stop(self) -> None:
        self.active = False
        self.current_bead = None
        # Always clear the cap so the next run starts at "no cap"
        # unless explicitly re-armed via --max=N.
        self.max_iterations = None

    def bump_completed(self) -> int:
        self.completed_count += 1
        return self.completed_count

    def reset(self) -> None:
        """Return this box to its just-constructed state (factory reset).

        This exists primarily for **test isolation**: because the module
        owns a single process-wide ``_STATE``, mutations leak between
        tests unless something puts it back to defaults. A teardown
        fixture calling ``get_state().reset()`` guarantees the next test
        starts pristine.

        Unlike :meth:`stop` (which disengages the chain but deliberately
        *preserves* ``completed_count`` so the end-of-run rollup can read
        the final tally), ``reset`` also zeroes the tally — it is a full
        factory reset, not a chain disengage. Keep the two distinct.
        """
        self.active = False
        self.current_bead = None
        self.completed_count = 0
        self.max_iterations = None


_STATE = ChainState()


def get_state() -> ChainState:
    return _STATE


def is_active() -> bool:
    return _STATE.active


def start() -> None:
    _STATE.start()


def stop() -> None:
    _STATE.stop()


def reset() -> None:
    """Factory-reset the singleton. Thin shortcut for test teardown."""
    _STATE.reset()
