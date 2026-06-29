"""End-to-end regression test for bead_chain-tfn: over-close bug.

This test verifies the fix for the bug where closing a molecule's beads
triggered a cascade that unintentionally closed **unrelated** parentless
beads (bead_chain-7at, bead_chain-t1z, bead_chain-h03).

The fix: calling rollup_completed_epics() once per session (at drain pass)
instead of after every individual bead close dramatically reduces the risk
of unintended closes via bd's server-side cascade.

Test scenario (Criterion 3 from bead_chain-tfn audit):
  1. Create N unrelated parentless beads (bug, spike, chore types).
  2. Create a molecule: an epic with children (simulating diataxis-generate).
  3. Claim the epic and all its children.
  4. Close all children (simulating molecule finalization).
  5. Call rollup_completed_epics() (the drain-pass cleanup that closes
     eligible epics).
  6. Assert:
     - The epic IS closed (rollup worked).
     - The N unrelated parentless beads are STILL OPEN (no over-close).

This validates that the once-per-session rollup strategy prevents
unrelated beads from being swept up by the cascade.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402

# bd init/create can prompt without this; e2e must stay non-interactive.
ENV = {**os.environ, "BD_NON_INTERACTIVE": "1"}


def sh(*args: str, cwd: str) -> subprocess.CompletedProcess[str]:
    """Run a bd command in a workspace directory."""
    return subprocess.run(
        ["bd", *args], capture_output=True, text=True, cwd=cwd, env=ENV
    )


def create(issue_type: str, title: str, cwd: str) -> str:
    """Create a bead and return its ID."""
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


def statuses(cwd: str) -> dict[str, Any]:
    """Return a dict of bead ID -> status for all beads in the workspace."""
    items = json.loads(sh("list", "--json", cwd=cwd).stdout or "[]")
    return {b["id"]: b["status"] for b in items}


def test_over_close_e2e() -> None:
    """E2E regression test for bead_chain-tfn: unrelated beads aren't over-closed."""
    # We rely on the real ``_run_bd`` here, not a mock left behind by an
    # earlier test. The autouse ``_restore_beads_module_globals`` fixture in
    # conftest.py (bead_chain-221) already snapshots and restores
    # ``beads._run_bd`` / ``beads._parse_json_list`` around every test, so the
    # real implementations are guaranteed to be in place by the time this runs.
    #
    # We deliberately do NOT ``importlib.reload(beads)`` to achieve that:
    # since bead_chain-7xv split the read/write halves into ``beads_reads`` /
    # ``beads_writes`` (imported by beads.py's facade), reloading only ``beads``
    # re-defines ``BeadsError`` to a fresh class object while the *cached*
    # submodules keep catching the old one — so a soft-failing ``except
    # BeadsError`` would stop matching. The conftest guard makes the reload
    # redundant anyway.
    # Create a temporary bd workspace.
    workdir = tempfile.mkdtemp(prefix="bc_over_close_e2e_")
    sh("init", "--non-interactive", "--prefix", "test", cwd=workdir)

    # Step 1: Create N unrelated parentless beads (tracking beads).
    # These should NEVER be closed by the molecule rollup.
    unrelated_beads = [
        create("bug", "Unrelated P0 bug", workdir),  # like bead_chain-7at
        create("spike", "Unrelated P1 spike", workdir),  # like bead_chain-t1z
        create("chore", "Unrelated P2 chore", workdir),  # like bead_chain-h03
    ]
    print(
        f"Created {len(unrelated_beads)} unrelated parentless beads: {unrelated_beads}"
    )

    # Step 2: Create a molecule (epic + children).
    # This simulates diataxis-generate or any multi-bead epic task.
    epic_id = create("epic", "Molecule Epic", workdir)
    child_ids = [
        create("task", "Molecule child 1", workdir),
        create("task", "Molecule child 2", workdir),
        create("task", "Molecule child 3 (finalize)", workdir),
    ]
    print(f"Created molecule: epic {epic_id} with {len(child_ids)} children")

    # Add parent-child relationships.
    for child_id in child_ids:
        sh("dep", "add", child_id, epic_id, "-t", "parent-child", cwd=workdir)

    # Step 3: Claim the epic and all children (simulating bead-chain driving them).
    sh("update", epic_id, "--claim", cwd=workdir)
    for child_id in child_ids:
        sh("update", child_id, "--claim", cwd=workdir)

    # Step 4: Close all children (simulating completion).
    for child_id in child_ids:
        sh("close", child_id, "--reason", "molecule complete", cwd=workdir)
    print("Closed all molecule children")

    # Step 5: Call the real rollup function (what happens at drain pass).
    prev_cwd = os.getcwd()
    os.chdir(workdir)
    try:
        closed_epics = beads.close_eligible_epics()
    finally:
        os.chdir(prev_cwd)

    print(f"Rollup closed epics: {[e['id'] for e in closed_epics]}")

    # Step 6: Verify the results.
    after = statuses(workdir)
    print(f"Bead statuses after rollup: {after}")

    # Assert: the epic IS closed (rollup worked).
    closed_epic_ids = {e["id"] for e in closed_epics}
    assert epic_id in closed_epic_ids, (
        f"Epic {epic_id} should be closed by rollup; got {closed_epic_ids}"
    )

    # Assert: the N unrelated beads are STILL OPEN (no over-close).
    for bead_id in unrelated_beads:
        assert bead_id in after, (
            f"Unrelated bead {bead_id} disappeared (was closed unintentionally)"
        )
        assert after[bead_id] == "open", (
            f"Unrelated bead {bead_id} status is {after[bead_id]}, "
            f"should be 'open' (over-close bug!)"
        )

    print(
        f"✓ PASS: Molecule closed, epic rolled up, "
        f"{len(unrelated_beads)} unrelated beads remain open."
    )


if __name__ == "__main__":
    test_over_close_e2e()
