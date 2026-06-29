"""End-to-end proof for the work-time blocker gate (bdboard-oals).

Runs against a REAL bd database (like test_rollup_e2e.py). Models the
exact repro: task B depends on (is blocked by) task A, A still open.

Asserts, against real ``bd`` output:
  1. ``bd ready`` does NOT surface the blocked bead B (frontier check).
  2. :func:`beads.open_blocker_ids` reports A as B's open blocker even
     when B is in_progress (the recovery path, where ``bd list
     --status=in_progress`` would otherwise re-surface B with no
     blocker awareness).
  3. Once A is closed, B becomes unblocked (``open_blocker_ids`` empty)
     and shows up on the ready frontier.

This is the regression the bug asked for: "with B blocked by open A, a
chain run does NOT start B." Tier 0 / the claim-time gate consume
exactly the signal proven here.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402

ENV = {**os.environ, "BD_NON_INTERACTIVE": "1"}


def sh(*args, cwd):
    return subprocess.run(
        ["bd", *args], capture_output=True, text=True, cwd=cwd, env=ENV
    )


def create(issue_type, title, cwd):
    out = sh(
        "create",
        "--type",
        issue_type,
        "--title",
        title,
        "-d",
        title,
        "--json",
        cwd=cwd,
    ).stdout
    return json.loads(out)["id"]


def ready_ids(cwd):
    items = json.loads(sh("ready", "--json", cwd=cwd).stdout or "[]")
    return {b["id"] for b in items}


def main():
    workdir = tempfile.mkdtemp(prefix="bc_block_e2e_")
    sh("init", "--non-interactive", "--prefix", "test", cwd=workdir)

    a_id = create("task", "A: tighten formula", workdir)
    b_id = create("task", "B: re-pour from formula", workdir)
    # B depends on A: A blocks B (the inbound 'blocks' edge on B).
    sh("dep", "add", b_id, a_id, "-t", "blocks", cwd=workdir)

    prev = os.getcwd()
    os.chdir(workdir)
    try:
        # (1) frontier check: ready surfaces A, NOT the blocked B.
        before = ready_ids(workdir)
        assert a_id in before, f"A should be ready, got {before}"
        assert b_id not in before, f"blocked B leaked onto ready frontier: {before}"

        # (2) recovery-path check: claim B (simulate a stranded in_progress
        #     bead whose blocker is still open) and prove the gate detects it.
        sh("update", b_id, "--claim", cwd=workdir)
        blockers = beads.open_blocker_ids(b_id)
        assert blockers == [a_id], (
            f"open_blocker_ids({b_id}) should report [{a_id}] while A is open; "
            f"got {blockers}"
        )
        assert beads.is_blocked(b_id) is True

        # A itself has no blockers.
        assert beads.open_blocker_ids(a_id) == [], "A should be unblocked"

        # (3) close A -> B becomes genuinely unblocked + ready.
        sh("update", b_id, "--status=open", cwd=workdir)  # unwind the sim claim
        sh("update", a_id, "--claim", cwd=workdir)
        sh("close", a_id, "--reason", "done", cwd=workdir)
        assert beads.open_blocker_ids(b_id) == [], (
            "B should be unblocked once A is closed"
        )
        assert b_id in ready_ids(workdir), "B should be ready after A closes"
    finally:
        os.chdir(prev)

    print(
        "PASS: blocked B never on the frontier / detected as blocked even when "
        "in_progress / unblocked only after A closes."
    )


if __name__ == "__main__":
    main()
