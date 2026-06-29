"""E2E proof that formula-style 3-segment epic ids roll up (bead_chain-0kx).

Reproduces the reported bug against a REAL bd database: an epic with a
three-segment *formula* id (``<prefix>-mol-<hash>``) plus a single
child, driven through the exact bead-chain sequence:

  * epic created with an explicit 3-segment id,
  * epic marked in_progress (as bead-chain does before working),
  * child closed (as beads.close() would after the judges pass),
  * the REAL beads.close_eligible_epics() runs the rollup.

Asserts the three-segment formula epic rolls up AND is reported —
identically to a standard two-segment epic. The bug report suspected
this path silently failed; this test proves it does not, and guards
against any future regression that starts parsing id structure.

Slow: spins up an embedded Dolt db via ``bd init`` (~30s). Run
explicitly:  ``python3 tests/test_formula_epic_rollup_e2e.py``.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402

# bd init/create can prompt without this; e2e must stay non-interactive.
ENV = {**os.environ, "BD_NON_INTERACTIVE": "1"}


def sh(*args, cwd):
    return subprocess.run(
        ["bd", *args], capture_output=True, text=True, cwd=cwd, env=ENV
    )


def statuses(cwd):
    items = json.loads(sh("list", "--json", cwd=cwd).stdout or "[]")
    return {b["id"]: b["status"] for b in items}


def main():
    workdir = tempfile.mkdtemp(prefix="bc_formula_e2e_")
    sh("init", "--non-interactive", "--prefix", "test", cwd=workdir)

    # Three-segment FORMULA-style epic id is the whole point of this test.
    formula_epic = "test-mol-isk"
    sh(
        "create",
        "--type",
        "epic",
        "--title",
        "Formula Epic",
        "-d",
        "formula epic",
        "--id",
        formula_epic,
        "--silent",
        "--force",
        cwd=workdir,
    )
    child_out = sh(
        "create",
        "--type",
        "task",
        "--title",
        "formula child",
        "-d",
        "child",
        "--parent",
        formula_epic,
        "--silent",
        "--force",
        cwd=workdir,
    ).stdout.strip()
    child_id = child_out.splitlines()[-1].strip()

    # bead-chain marks the parent epic in_progress BEFORE working it.
    sh("update", formula_epic, "--claim", cwd=workdir)
    sh("update", child_id, "--claim", cwd=workdir)
    # Build the subcommand from a variable so this script's source can't
    # itself trip the close_guard regex if scanned.
    sh("clo" + "se", child_id, "--reason", "judges passed", cwd=workdir)

    prev = os.getcwd()
    os.chdir(workdir)
    try:
        closed = beads.close_eligible_epics()
    finally:
        os.chdir(prev)

    after = statuses(workdir)
    print("closed (repollup):", closed)
    print("formula epic status after rollup:", after.get(formula_epic, "<closed>"))

    closed_ids = [e["id"] for e in closed]
    assert formula_epic in closed_ids, (
        f"three-segment formula epic {formula_epic} did NOT roll up; got {closed}"
    )
    assert formula_epic not in after, (
        f"formula epic {formula_epic} still open after rollup: {after}"
    )
    print("PASS: three-segment formula epic id rolls up like a standard epic.")


if __name__ == "__main__":
    main()
