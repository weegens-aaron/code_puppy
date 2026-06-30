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

    It also carries the active bead's *identity* (bead-factory-2mb):

    * ``bead_id`` — the id of the bead currently being built. The build loop
      re-fetches the live bead via ``bd show <bead_id>`` at inspection time
      so notes/edits appended DURING the build loop (rework feedback,
      mid-run field edits, bug-discovery notes) reach the inspectors and the
      next retry's pinned contract instead of grading the frozen claim-time
      snapshot. ``None`` disables the re-fetch (frozen-snapshot behaviour).
    * ``recovery`` — the claim-time recovery flag, stored so the live
      re-render reproduces the same mode preamble the claim-time render
      used (recovery vs triage vs ordinary). Without it a re-fetch would
      silently drop the recovery preamble on every retry.
    """

    active: bool = False
    prompt: str | None = None
    inspector_prompt: str | None = None
    bead_id: str | None = None
    recovery: bool = False
    loop_count: int = 0
    remediation_notes: str | None = None

    def start(
        self,
        prompt: str,
        *,
        inspector_prompt: str | None = None,
        bead_id: str | None = None,
        recovery: bool = False,
    ) -> None:
        self.active = True
        self.prompt = prompt
        # Inspector copy falls back to the implementor prompt so callers that
        # don't split (non-injected paths) keep the pre-462 behaviour.
        self.inspector_prompt = (
            inspector_prompt if inspector_prompt is not None else prompt
        )
        self.bead_id = bead_id
        self.recovery = recovery
        self.loop_count = 0
        self.remediation_notes = None

    def stop(self) -> None:
        self.active = False
        self.prompt = None
        self.inspector_prompt = None
        self.bead_id = None
        self.recovery = False
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


def start(
    prompt: str,
    *,
    inspector_prompt: str | None = None,
    bead_id: str | None = None,
    recovery: bool = False,
) -> None:
    _STATE.start(
        prompt,
        inspector_prompt=inspector_prompt,
        bead_id=bead_id,
        recovery=recovery,
    )


def stop() -> None:
    _STATE.stop()


def increment() -> int:
    return _STATE.increment()
