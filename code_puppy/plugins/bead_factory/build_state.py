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
    """Tiny state container. No behavior soup, please and thank you.

    Holds TWO build-prompt copies (bead-factory-462):

    * ``prompt`` — the implementor-facing build prompt. Slimmed to
      scaffolding-only when the bead's content is pinned into the system
      prompt (bead-factory-5wv), so the implementor isn't handed the same
      instruction twice. This is what ``get_prompt()`` returns and what the
      build-loop continuation re-sends.
    * ``inspector_prompt`` — the FULL compose (content + scaffolding). The
      inspector is a raw pydantic_ai agent with no system-prompt injection,
      so it must carry the bead's content inline. Defaults to ``prompt``
      when not split (non-injected paths render the full prompt for both).
    """

    active: bool = False
    prompt: str | None = None
    inspector_prompt: str | None = None
    loop_count: int = 0
    remediation_notes: str | None = None

    def start(self, prompt: str, *, inspector_prompt: str | None = None) -> None:
        self.active = True
        self.prompt = prompt
        # Inspector copy falls back to the implementor prompt so callers that
        # don't split (non-injected paths) keep the pre-462 behaviour.
        self.inspector_prompt = (
            inspector_prompt if inspector_prompt is not None else prompt
        )
        self.loop_count = 0
        self.remediation_notes = None

    def stop(self) -> None:
        self.active = False
        self.prompt = None
        self.inspector_prompt = None
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


def get_inspector_prompt() -> str | None:
    """Full content+scaffolding prompt for the inspector (bead-factory-462).

    The inspector never receives the ``load_prompt`` system pin, so it reads
    this FULL copy rather than the (possibly slimmed) implementor ``prompt``.
    """
    return _STATE.inspector_prompt if _STATE.active else None


def start(prompt: str, *, inspector_prompt: str | None = None) -> None:
    _STATE.start(prompt, inspector_prompt=inspector_prompt)


def stop() -> None:
    _STATE.stop()


def increment() -> int:
    return _STATE.increment()
