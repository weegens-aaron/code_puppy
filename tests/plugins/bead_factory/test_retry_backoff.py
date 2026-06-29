"""Retry/backoff policy tests for ``beads._run_bd`` (bead_chain-7b6).

The old policy (``DEFAULT_TIMEOUT=30`` + fixed ``0.5/1.0`` backoffs) cost
up to 91.5s for a single fully-timed-out ``bd`` call
(``30 + 0.5 + 30 + 1.0 + 30``). With 10+ sequential calls per activation a
flaky ``bd`` binary could add minutes of pure infrastructure overhead to a
single bead.

This module locks in the reduced budget and the fail-fast taxonomy:

* transient ``TimeoutExpired`` blips are still retried with bounded,
  exponential backoff;
* known-fatal errors (binary missing, not executable, real non-zero exit)
  fail fast on the *first* attempt — no retry, no wasted wall-clock.

``beads.py`` is pure-stdlib, so this runs standalone:
``python3 -m pytest tests/test_retry_backoff.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402

# Capture the REAL _run_bd at import time. Several other test modules reassign
# the module-global ``beads._run_bd`` to a stub and never restore it, so by the
# time this suite runs in-process that attribute may point at a leftover fake.
# pytest imports every test module during collection *before* running any test,
# so grabbing the reference here guarantees we exercise the genuine function
# regardless of cross-module pollution.
_REAL_RUN_BD = beads._run_bd


def _expect_beads_error(fn, *args, **kwargs):
    """Assert ``fn(*args)`` raises BeadsError; return the exception."""
    try:
        fn(*args, **kwargs)
    except beads.BeadsError as exc:
        return exc
    raise AssertionError("expected BeadsError")


# --------------------------------------------------------------------------
# _retry_backoff: exponential with a ceiling
# --------------------------------------------------------------------------


def test_backoff_is_exponential():
    assert beads._retry_backoff(1) == 0.25
    assert beads._retry_backoff(2) == 0.5
    assert beads._retry_backoff(3) == 1.0


def test_backoff_clamped_to_ceiling():
    # Past the ceiling, every delay is capped — no unbounded growth even if
    # MAX_ATTEMPTS is bumped sky-high later.
    assert beads._retry_backoff(4) == beads._RETRY_BACKOFF_CEILING
    assert beads._retry_backoff(99) == beads._RETRY_BACKOFF_CEILING


# --------------------------------------------------------------------------
# Worst-case budget is reduced from 91.5s
# --------------------------------------------------------------------------


def test_worst_case_budget_reduced_from_91_5s():
    """The documented worst case must be well under the old 91.5s."""
    timeouts = beads.DEFAULT_TIMEOUT * beads.MAX_ATTEMPTS
    backoffs = sum(beads._retry_backoff(n) for n in range(1, beads.MAX_ATTEMPTS))
    worst_case = timeouts + backoffs
    assert worst_case < 91.5, f"worst case {worst_case}s did not shrink"
    # Pin the concrete budget so a regression in any knob is loud.
    assert worst_case == 45.75


# --------------------------------------------------------------------------
# Fakes for subprocess.run
# --------------------------------------------------------------------------


class _Spawner:
    """Replays a queue of outcomes for successive ``subprocess.run`` calls.

    Each outcome is either an exception instance (raised) or a
    ``(returncode, stdout, stderr)`` tuple (returned as a proc-like object).
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, *args, **kwargs):  # noqa: ARG002
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        rc, out, err = outcome
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _patch(monkeypatch_targets):
    """Install fakes for _bd_bin / subprocess.run / time.sleep.

    Returns the recorded sleeps list so callers can assert on backoff.
    Caller is responsible for restoring via the returned restore() fn.
    """
    sleeps: list[float] = []
    orig_run = beads.subprocess.run
    orig_sleep = beads.time.sleep
    orig_bin = beads._bd_bin

    beads.subprocess.run = monkeypatch_targets  # type: ignore[assignment]
    beads.time.sleep = lambda s: sleeps.append(s)  # type: ignore[assignment]
    beads._bd_bin = lambda: "bd"  # type: ignore[assignment]

    def restore():
        beads.subprocess.run = orig_run  # type: ignore[assignment]
        beads.time.sleep = orig_sleep  # type: ignore[assignment]
        beads._bd_bin = orig_bin  # type: ignore[assignment]

    return sleeps, restore


# --------------------------------------------------------------------------
# Transient timeouts ARE retried
# --------------------------------------------------------------------------


def test_transient_timeout_then_success():
    spawner = _Spawner(
        [
            subprocess.TimeoutExpired(cmd="bd", timeout=15.0),
            (0, "ok-payload", ""),
        ]
    )
    sleeps, restore = _patch(spawner)
    try:
        assert _REAL_RUN_BD("ready") == "ok-payload"
        assert spawner.calls == 2  # one retry happened
        assert sleeps == [0.25]  # backoff before the single retry
    finally:
        restore()


def test_all_timeouts_exhaust_attempts():
    spawner = _Spawner(
        [subprocess.TimeoutExpired(cmd="bd", timeout=15.0)] * beads.MAX_ATTEMPTS
    )
    sleeps, restore = _patch(spawner)
    try:
        exc = _expect_beads_error(_REAL_RUN_BD, "ready")
        assert "timed out" in str(exc)
        assert spawner.calls == beads.MAX_ATTEMPTS
        # One backoff sleep before each retry (not before the first attempt).
        assert sleeps == [0.25, 0.5]
    finally:
        restore()


# --------------------------------------------------------------------------
# Known-fatal errors FAIL FAST (no retry)
# --------------------------------------------------------------------------


def test_missing_binary_fails_fast():
    spawner = _Spawner([FileNotFoundError("no bd")])
    sleeps, restore = _patch(spawner)
    try:
        exc = _expect_beads_error(_REAL_RUN_BD, "ready")
        assert "not found on PATH" in str(exc)
        assert spawner.calls == 1  # no retry
        assert sleeps == []  # never slept
    finally:
        restore()


def test_permission_denied_fails_fast():
    spawner = _Spawner([PermissionError("not executable")])
    sleeps, restore = _patch(spawner)
    try:
        exc = _expect_beads_error(_REAL_RUN_BD, "ready")
        assert "permission denied" in str(exc)
        assert spawner.calls == 1  # no retry
        assert sleeps == []
    finally:
        restore()


def test_non_zero_exit_fails_fast():
    spawner = _Spawner([(1, "", "error: bead not found")])
    sleeps, restore = _patch(spawner)
    try:
        exc = _expect_beads_error(_REAL_RUN_BD, "show", "x")
        assert "exit 1" in str(exc)
        assert "bead not found" in str(exc)
        assert spawner.calls == 1  # no retry
        assert sleeps == []
    finally:
        restore()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
