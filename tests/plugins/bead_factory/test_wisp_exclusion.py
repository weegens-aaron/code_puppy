"""Regression tests: ephemeral (wisp) issues never leak onto bead-chain's queue.

Coverage-audit gap formulas#4 (bead_chain-wot): if a wisp-type bead
(heartbeat / ping / patrol / recovery) surfaced on ``bd ready`` it could
be handed to ``/build`` as if it were real code work. **Confirmed it does
not:** ``bd ready`` excludes ephemeral issues *by default* — they appear
only with the explicit ``--include-ephemeral`` flag, which bead-chain
never passes. These tests lock that invariant in two ways:

  * a fast unit check that every ready-querying helper's argv is free of
    ``--include-ephemeral`` (so no future edit silently opts in), and
  * an end-to-end proof against a real bd that a created wisp is absent
    from :func:`beads.next_ready` while a normal task is present.

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone: ``python3 -m pytest tests/`` or
``python3 tests/test_wisp_exclusion.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402

# Captured at import (before any test stubs _run_bd) so the e2e test can
# restore the genuine subprocess wrapper even when an earlier unit test
# in the same pytest session left a stub in place.
_REAL_RUN_BD = beads._run_bd


# ---------------------------------------------------------------------------
# Unit: no ready-querying helper opts into ephemerals.
# ---------------------------------------------------------------------------


def _capture_argv(payload: str = "[]"):
    """Stub _run_bd to record every argv it's called with; return the list."""
    calls: list[tuple[str, ...]] = []

    def stub(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(args)
        return payload

    beads._run_bd = stub  # type: ignore[assignment]
    return calls


def test_next_ready_never_includes_ephemeral():
    calls = _capture_argv("[]")
    beads.next_ready()
    assert calls, "next_ready should have queried bd"
    for argv in calls:
        assert "--include-ephemeral" not in argv, argv


def test_blocking_bug_scan_never_includes_ephemeral():
    calls = _capture_argv("[]")
    beads.next_blocking_bug()
    assert calls, "next_blocking_bug should have queried bd"
    for argv in calls:
        assert "--include-ephemeral" not in argv, argv


# ---------------------------------------------------------------------------
# E2E: a real wisp is absent from the plugin's ready frontier.
# ---------------------------------------------------------------------------

_ENV = {**os.environ, "BD_NON_INTERACTIVE": "1"}


def _sh(wd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bd", *args], capture_output=True, text=True, cwd=wd, env=_ENV
    )


def test_wisp_absent_from_next_ready_e2e():
    """A created ephemeral wisp must not surface via beads.next_ready()."""
    wd = tempfile.mkdtemp(prefix="bc_wisp_")
    init = _sh(wd, "init", "--non-interactive", "--prefix", "wp")
    # Restore the real subprocess wrapper — a prior unit test may have
    # left a stub on beads._run_bd within this pytest session.
    beads._run_bd = _REAL_RUN_BD  # type: ignore[assignment]
    if init.returncode != 0:
        # bd unavailable / unsupported in this env — skip rather than fail.
        print("SKIP: bd init failed:", init.stderr.strip())
        return

    _sh(wd, "create", "--type", "task", "--title", "real work", "-d", "real work")
    eph = _sh(
        wd, "create", "--type", "task", "--title", "wisp", "-d", "wisp", "--ephemeral"
    )
    if eph.returncode != 0:
        print("SKIP: this bd build can't create ephemerals:", eph.stderr.strip())
        return

    # Sanity: --include-ephemeral *does* surface the wisp (proves it exists).
    with_eph = json.loads(
        _sh(wd, "ready", "--include-ephemeral", "--json").stdout or "[]"
    )
    wisp_ids = [b["id"] for b in with_eph if "wisp" in b["id"]]
    assert wisp_ids, "expected the ephemeral wisp to exist under --include-ephemeral"

    prev = os.getcwd()
    os.chdir(wd)
    try:
        ready = beads.next_ready()
    finally:
        os.chdir(prev)

    assert ready is not None, "the normal task should be ready"
    assert "wisp" not in ready["id"], f"wisp leaked onto next_ready: {ready['id']}"
    print("PASS: wisp excluded from next_ready; got", ready["id"])


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
