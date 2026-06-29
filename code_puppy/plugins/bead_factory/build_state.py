"""State for the bead_factory build continuation policy.

This is a dumb data box; the build orchestration lives in ``build_loop.py``.

Note: this is intentionally a *separate* module from bead_factory's
``state.py`` (which holds bead-factory's ``ChainState``). The two singletons
never collide because they live in different modules with different classes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BuildState:
    """Tiny state container. No behavior soup, please and thank you."""

    active: bool = False
    prompt: str | None = None
    loop_count: int = 0
    remediation_notes: str | None = None

    def start(self, prompt: str) -> None:
        self.active = True
        self.prompt = prompt
        self.loop_count = 0
        self.remediation_notes = None

    def stop(self) -> None:
        self.active = False
        self.prompt = None
        self.loop_count = 0
        self.remediation_notes = None

    def increment(self) -> int:
        self.loop_count += 1
        return self.loop_count


_STATE = BuildState()


def get_state() -> BuildState:
    return _STATE


def is_active() -> bool:
    return _STATE.active


def get_prompt() -> str | None:
    return _STATE.prompt if _STATE.active else None


def start(prompt: str) -> None:
    _STATE.start(prompt)


def stop() -> None:
    _STATE.stop()


def increment() -> int:
    return _STATE.increment()
