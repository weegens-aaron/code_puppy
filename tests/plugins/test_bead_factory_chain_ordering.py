"""Tests for bead_factory's intra-package wiring + turn-end hook ordering.

These lock in the contract rewired by bead-factory-7h4:

* The chain driver references the *in-package* build state
  (``build_state``) — there is no cross-import of ``code_puppy.plugins.wiggum``.
* The build turn-end hook is registered STRICTLY BEFORE the chain
  driver's, so chain logic always runs after the per-turn build decision.
* Cancellation leaves the in-flight bead ``in_progress`` for recovery (the
  cancel handler never reverts/closes the bead in ``bd``).
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

import code_puppy.callbacks as cb
from code_puppy.plugins.bead_factory import chain_driver as cd
from code_puppy.plugins.bead_factory import build_loop
from code_puppy.plugins.bead_factory import lifecycle
from code_puppy.plugins.bead_factory import build_state


# ---------------------------------------------------------------------------
# Intra-package wiring (no more cross-plugin import)
# ---------------------------------------------------------------------------


def test_no_cross_import_of_wiggum_plugin():
    """No bead_factory module may *import* code_puppy.plugins.wiggum.

    Doc comments referencing the old path for historical context are fine;
    an actual ``import``/``from`` statement is not.
    """
    pkg_dir = Path(cd.__file__).parent
    offenders: list[str] = []
    for py in pkg_dir.glob("*.py"):
        for raw in py.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue  # comment line — historical reference, allowed
            if (
                "import code_puppy.plugins.wiggum" in line
                or "from code_puppy.plugins.wiggum" in line
            ):
                offenders.append(f"{py.name}: {line}")
    assert not offenders, f"stale wiggum cross-imports found: {offenders}"


def test_chain_driver_and_lifecycle_share_in_package_build_state():
    """Both modules' ``build_state`` ref IS the in-package build_state."""
    assert cd.build_state is build_state
    assert lifecycle.build_state is build_state
    # build_loop drives the very same singleton, so arming from the chain
    # side is observed by the build side.
    assert build_loop.state is build_state


# ---------------------------------------------------------------------------
# Turn-end hook ordering: build decision BEFORE chain driver
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_turn_hooks():
    """Isolate the interactive-turn callback lists for an ordering test."""
    before_end = cb.get_callbacks("interactive_turn_end", include_disabled=True)
    before_cancel = cb.get_callbacks("interactive_turn_cancel", include_disabled=True)
    cb.clear_callbacks("interactive_turn_end")
    cb.clear_callbacks("interactive_turn_cancel")
    cd._HOOKS_REGISTERED = False
    try:
        yield
    finally:
        cb.clear_callbacks("interactive_turn_end")
        cb.clear_callbacks("interactive_turn_cancel")
        for f in before_end:
            cb.register_callback("interactive_turn_end", f)
        for f in before_cancel:
            cb.register_callback("interactive_turn_cancel", f)
        cd._HOOKS_REGISTERED = False


def test_build_hook_registered_before_chain_hook(clean_turn_hooks):
    cd._ensure_hooks_registered()

    end_cbs = cb.get_callbacks("interactive_turn_end", include_disabled=True)
    cancel_cbs = cb.get_callbacks("interactive_turn_cancel", include_disabled=True)

    assert build_loop.on_interactive_turn_end in end_cbs
    assert cd._on_interactive_turn_end in end_cbs
    assert end_cbs.index(build_loop.on_interactive_turn_end) < end_cbs.index(
        cd._on_interactive_turn_end
    ), "build turn-end hook must run BEFORE the chain driver"

    assert cancel_cbs.index(build_loop.on_interactive_turn_cancel) < cancel_cbs.index(
        cd._on_interactive_turn_cancel
    ), "build cancel hook must run BEFORE the chain driver"


def test_ensure_hooks_is_idempotent(clean_turn_hooks):
    cd._ensure_hooks_registered()
    cd._ensure_hooks_registered()  # second call must not duplicate

    end_cbs = cb.get_callbacks("interactive_turn_end", include_disabled=True)
    assert end_cbs.count(build_loop.on_interactive_turn_end) == 1
    assert end_cbs.count(cd._on_interactive_turn_end) == 1


def test_ordering_holds_when_entry_point_registered_build_first(clean_turn_hooks):
    """Mirror the plugin entry point wiring the build hook at startup.

    dedup must keep the build hook in its earlier (ahead-of-us) slot.
    """
    cb.register_callback("interactive_turn_end", build_loop.on_interactive_turn_end)
    cd._ensure_hooks_registered()

    end_cbs = cb.get_callbacks("interactive_turn_end", include_disabled=True)
    assert end_cbs.count(build_loop.on_interactive_turn_end) == 1
    assert end_cbs.index(build_loop.on_interactive_turn_end) < end_cbs.index(
        cd._on_interactive_turn_end
    )


def test_chain_driver_defers_while_build_is_active(monkeypatch):
    """Behavioural proof the chain runs AFTER the build decision.

    While the build loop is still active for the current bead, the
    chain driver must bow out (return None) instead of closing the bead.
    """
    cd.state.get_state().active = True
    cd.state.get_state().current_bead = {"id": "bead-x"}
    build_state.start("do the thing")
    try:
        result = asyncio.run(
            cd._on_interactive_turn_end(agent=object(), prompt="p", result=None)
        )
        assert result is None
    finally:
        build_state.stop()
        cd.state.reset()


# ---------------------------------------------------------------------------
# Cancellation leaves the in-flight bead in_progress for recovery
# ---------------------------------------------------------------------------


def test_cancel_leaves_bead_in_progress(monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(cd, "emit_warning", lambda m, *a, **k: messages.append(m))
    monkeypatch.setattr(
        cd, "emit_system_message", lambda m, *a, **k: messages.append(m)
    )
    # If the cancel handler ever tried to revert/close the bead, these would
    # be called — they must NOT be (the bead stays in_progress).
    revert_calls: list[str] = []
    monkeypatch.setattr(
        importlib.import_module("code_puppy.plugins.bead_factory.beads_writes"),
        "revert_to_open",
        lambda bid: revert_calls.append(bid),
    )

    cd.state.get_state().active = True
    cd.state.get_state().current_bead = {"id": "bead-7h4"}
    try:
        asyncio.run(cd._on_interactive_turn_cancel("prompt", reason="ctrl-c"))
    finally:
        cd.state.reset()

    # Chain disengaged in memory…
    assert cd.state.is_active() is False
    # …but the bead was never reverted/closed: it stays in_progress in bd.
    assert revert_calls == []
    # …and the user is told it's left in_progress for the recovery preamble.
    joined = " ".join(messages)
    assert "bead-7h4" in joined
    assert "in_progress" in joined
