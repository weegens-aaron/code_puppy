"""End-to-end proof for bdboard-rzxb against a real bd database.

Simulates the bead-chain final-bead path:
  * epic + single child, epic marked in_progress (as bead-chain does),
  * child closed (as beads.close() would after judges pass),
  * then the REAL beads.close_eligible_epics() runs (what
    rollup_completed_epics / the new end-of-run drain call).

Asserts the rollup BOTH closes the epic AND reports it (non-empty
return) -- the silent-rollup bug returned [] here pre-fix.
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


def create(issue_type, title, cwd):
    out = sh(
        "create", "--type", issue_type, "--title", title, "-d", title, "--json", cwd=cwd
    ).stdout
    return json.loads(out)["id"]


def statuses(cwd):
    items = json.loads(sh("list", "--json", cwd=cwd).stdout or "[]")
    return {b["id"]: b["status"] for b in items}


def main():
    workdir = tempfile.mkdtemp(prefix="bc_e2e_")
    sh("init", "--non-interactive", "--prefix", "test", cwd=workdir)

    eid = create("epic", "E2E Epic", workdir)
    cid = create("task", "E2E only child", workdir)
    sh("dep", "add", cid, eid, "-t", "parent-child", cwd=workdir)
    sh("update", eid, "--claim", cwd=workdir)  # bead-chain marks epic in_progress
    sh("update", cid, "--claim", cwd=workdir)
    sh("close", cid, "--reason", "judges passed", cwd=workdir)

    # Run the REAL plugin function from inside the workspace dir.
    prev = os.getcwd()
    os.chdir(workdir)
    try:
        closed = beads.close_eligible_epics()
    finally:
        os.chdir(prev)

    after = statuses(workdir)
    print("closed (reported by rollup):", closed)
    print("epic status after rollup:", after.get(eid, "<gone-from-open-list>"))

    closed_ids = [e["id"] for e in closed]
    assert eid in closed_ids, (
        f"rollup did NOT report epic {eid} (silent-rollup bug); got {closed}"
    )
    assert eid not in after, f"epic {eid} still open after rollup: {after}"
    print("PASS: final-child close rolls up AND reports the epic in the same pass.")


if __name__ == "__main__":
    main()
