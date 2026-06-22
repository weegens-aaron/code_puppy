# Spike: Cataloging the `bead-chain` Orchestrator (clone & trace lifecycle)

> **Bead:** `code_puppy_oss-8sv` · Type: spike · Priority: P2
> **Status:** research-only — documents `bead-chain` *as it exists today*. No
> code-puppy or bead-chain changes were made (and per the bead, none are
> allowed). Clone traced from `https://github.com/weegens-aaron/bead-chain`
> (`bd` variant) into a scratch dir.
> **Companion catalogs:** `/goal` (`code_puppy_oss-fzh`), `/wiggum`
> (`code_puppy_oss-68r`), `/judges` (`code_puppy_oss-80b`), synthesis
> (`code_puppy_oss-0ae`). The follow-up synthesis bead `code_puppy_oss-awj`
> consumes this catalog.

---

## 0. The headline correction (read this first)

The bead's framing calls bead-chain "the **external loop runner** that drives
code-puppy" and asks how "it **shells out to code-puppy** per bead." That mental
model is **wrong**, and correcting it is the single most important finding of
this spike:

**bead-chain is not an external process. It is a code-puppy *plugin*.** It ships
as `~/.code_puppy/plugins/bead_chain/` (a user-tier plugin), is auto-discovered
by code-puppy's plugin loader at startup, and runs *inside* the same code-puppy
process as the agent it drives. It never spawns, execs, or shells out to
code-puppy. It does not own a goal loop, a model call, or a judge.

What it actually is: **a thin queue driver that chains the `bd ready` frontier
into wiggum's existing `/goal` loop, one bead at a time.** It reuses the *exact
same* `interactive_turn_end` continuation machinery that `/wiggum` and `/goal`
ride (documented in the `/wiggum` and `/goal` catalogs). Its entire job is
queue mechanics wrapped *around* a goal engine it delegates to:

```
probe bd ready → claim → arm wiggum /goal → observe is_active() → close → repeat
```

The "shells out" intuition is half-right but aimed at the wrong target:
bead-chain *does* shell out heavily — to the **`bd` CLI** (the beads issue
tracker), via `subprocess.run`. It never shells out to code-puppy. Keep these
two subprocess surfaces straight; the whole architecture hinges on it.

> bead-chain's own docs are emphatic about this: *"This plugin is a **queue
> driver**, not a goal engine"* appears verbatim in its README, the
> `register_callbacks.py` module docstring, `AGENTS.md`, and a dedicated
> concept doc (`QueueDriverNotGoalEngine.md`). The phrase is load-bearing.

So when a previous spike closed with the reason `bead-chain: LLM judges
passed`, that close was issued by **`bead-chain`'s own `bd close` call** (in
`lifecycle.close_current_bead_success`) *after* wiggum's `/goal` judges flipped
`wiggum_state.is_active()` to `False`. bead-chain inferred "judges passed" from
that one-bit observation and ran `bd close --reason "bead-chain: LLM judges
passed"`. Nothing external grading anything; it's all one process.

---

## 1. Repo layout & entry point

bead-chain is a **dual-variant monorepo**. The same plugin ships twice:

| Variant | Targets CLI | Dir | Notes |
|---------|-------------|-----|-------|
| **`bd`** (default) | Go beads (`bd`) | `bd/` | Full feature set. The variant traced here. |
| **`br`** | beads_rust (`br`) | `br/` | Compatible subset; loses **memories** (`bd remember`/`memories`) and **gates** (`gate check`) — both degrade soft (no crash). |

Both extract to the same plugin name `bead_chain/`, so you install exactly one.
The variant dirs are near-identical (each ~the same file set + line counts);
this catalog traces `bd/`.

### Module map (the `bd/` variant)

```
bd/
├── register_callbacks.py   # ENTRY POINT — /bead-chain command, hooks, CLI flags
├── lifecycle.py            # the iteration state machine (close, pick, activate, rollup)
├── beads.py                # bd subprocess CORE (_run_bd, retries, predicates, constants)
│   ├── beads_reads.py      #   read half: ready/list/show/memories/blocker queries
│   └── beads_writes.py     #   write half: claim/revert/close/epic-rollup/gate/lint
├── prompt.py               # bead-dict → /goal prompt string (enrichment, preambles)
├── execution_hints.py      # per-bead execution_* metadata → code-puppy serial knobs
├── close_guard.py          # run_shell_command hook: block agent-issued `bd close`
└── state.py                # BeadChainState singleton (active / current_bead / counts)
```

The `beads.py` / `beads_reads.py` / `beads_writes.py` split (issue
`bead_chain-7xv`) was forced by the 600-line cap — the original `beads.py` was
~1271 lines. `beads.py` keeps the subprocess core + all classification
predicates/constants and **re-exports** both halves at the bottom of the file,
so every `from .beads import next_ready, close, ...` call site keeps working and
the test suite's `beads._run_bd` monkeypatch seam stays honoured.

### Entry point

`register_callbacks.py` registers one slash command:

```python
@register_command(name="bead-chain", usage="/bead-chain [--max=N]", category="plugin")
def handle_bead_chain_command(command: str) -> str | bool:
    ...
```

It is the same `register_callbacks.py` pattern code-puppy plugins use (see the
contributing guide). The `run_shell_command` close-guard hook is registered
eagerly at module scope; the two interactive-turn hooks are registered **lazily**
(see §3).

---

## 2. The orchestration lifecycle, end to end

This is the master loop. It is a **state machine spread across two events**: the
`/bead-chain` command (kicks off bead #1) and the `interactive_turn_end` hook
(drives every bead transition thereafter). bead-chain owns **no loop of its
own** — code-puppy's `cli_runner` continuation loop owns execution; bead-chain
just returns continuation dicts saying "run this next."

### 2.1 Engage (`handle_bead_chain_command`)

```
/bead-chain [--max=N]
```

1. **Prereq check.** If wiggum isn't loaded (`_WIGGUM_AVAILABLE == False`), bail
   loud with an actionable message — bead-chain literally cannot run without
   wiggum's `/goal` engine (`bead_chain-c87`). Defensive import, never a raw
   `ImportError`.
2. **Idempotency.** If `state.is_active()`, say "already running" and stop.
3. **Immediate ack.** Emit `bead-chain starting…` *before* any `bd` probe —
   those probes can stall, and a silent UI looks frozen.
4. **Parse `--max=N`** before touching `bd`. Invalid (`--max=abc`, `--max=0`,
   `--max=-3`, missing value) → refuse to start, claim nothing.
5. **Pick the first bead** — recovery beats fresh:
   `enforce_single_in_progress()` first (recover a stranded in-progress bead),
   else `next_ready()`. Empty queue → `No ready beads`, stop.
6. **Last-line-of-defence guards** on the chosen bead: refuse if it's an
   excluded container type (epic/milestone/gate/molecule), refuse + revert if it
   has open work-time blockers.
7. **Register hooks lazily** (`_ensure_hooks_registered`) — see §3.
8. `state.start()`, stash `max_iterations`.
9. **Claim parent epic first, then the child** (`ensure_epic_in_progress` then
   `claim`). Parent-first keeps bd's cached hierarchy views consistent. Recovery
   beads skip the claim (already in-progress).
10. **Apply execution hints** (`apply_execution_hints`) — map the bead's
    `execution_effort` / `execution_model` / `execution_agent_type` metadata onto
    code-puppy's serial knobs.
11. **Arm wiggum:** `wiggum_state.start(goal_prompt, mode="goal")`.
12. **Return the goal-prompt string.** code-puppy's `cli_runner` executes that
    return value as the user's prompt — kicking off the first `/goal` iteration.
    (Same "returned string becomes a prompt" trick `/wiggum` uses.)

### 2.2 Drive (`_on_interactive_turn_end`, fires every turn)

```python
if not state.is_active():       return None     # chain not engaged
if wiggum_state.is_active():    return None     # goal engine still cooking — yield
# else: wiggum just stopped → bead done (judges passed) or cancelled
just_closed = await asyncio.to_thread(close_current_bead_success)
if not state.is_active():       return None     # close failed → chain halted
return await asyncio.to_thread(activate_next_bead, just_closed)
```

The whole driver is a **one-bit observation of someone else's engine**: "is
wiggum still active?" If yes, get out of the way (return `None`, wiggum's own
continuation dict wins). If no, the current bead's `/goal` loop finished — close
it and advance.

The two `bd`-heavy calls (`close_current_bead_success`, `activate_next_bead`)
are offloaded to a worker thread via `asyncio.to_thread` (`bead_chain-u0b`) so
the synchronous `bd` subprocesses (up to ~45s worst-case under retries) don't
block code-puppy's interactive event loop. They are `await`ed **sequentially** —
at most one worker thread in flight — so the strict close→check→activate ordering
and the single-mutator state guarantee are preserved.

### 2.3 The handoff contract with wiggum (why hooks register *late*)

bead-chain **must** run its turn-end hook *after* wiggum's. Both plugins hook
`interactive_turn_end`; the callback list runs in registration order. wiggum
loads at startup (early). bead-chain defers its hook registration until the
**first `/bead-chain` invocation** (`_ensure_hooks_registered`, guarded by a
`_HOOKS_REGISTERED` flag), guaranteeing it's appended *after* wiggum's.

Why it matters: on each turn wiggum decides goal-complete-or-not *first*. If
incomplete, wiggum stays active and returns its own continuation dict; bead-chain
then sees `is_active() == True` and yields. If complete, wiggum stops itself and
returns `None`; bead-chain then sees `is_active() == False` and advances. If
bead-chain ran *first*, it would observe an unsettled `is_active()` and the whole
observe-after-engine contract would break. Registering hooks at import time is
explicitly called out as an anti-pattern in bead-chain's own docs.

---

## 3. How beads are discovered & read (the `bd` command surface — reads)

bead-chain **never imports a beads Python API** — beads is a Go binary and its
`--json` output is the stable contract. Every read shells out via `_run_bd`.

| Helper (`beads_reads.py`) | `bd` invocation | Used for |
|----------|-----------------|----------|
| `next_ready()` | `bd ready --exclude-type=epic,milestone,gate,molecule --json` | Head of the global ready frontier |
| `next_ready_in_epic(epic)` | `bd ready --parent=<epic> --exclude-type=… --json` | Epic-affinity sibling |
| `next_blocking_bug()` | `bd ready --type=bug --exclude-type=… --json` (then client-side `dependent_count > 0`) | Blocking-bug priority |
| `list_in_progress()` | `bd list --status=in_progress --exclude-type=… --json` | Stranded-work detection |
| `list_recoverable_strands()` | `bd list --status=in_progress,hooked --exclude-type=… --json` | Recovery tier (one call, comma-status) |
| `has_open_children(parent)` | `bd list --parent=<parent> --json` | Fan-out gate check |
| `show(id)` | `bd show <id> --json` | Full record (deps, status, metadata, design) |
| `memories()` | `bd memories --json` | Warm-start memory digest for the prompt |
| `open_blocker_ids(id)` | (reads `show()` deps) | Work-time blocker recheck |
| `is_pinned(id)` | (reads `show()` status) | Mid-flight pin guard |

**Defence-in-depth filtering.** Container types are filtered *both* server-side
(`--exclude-type`) *and* client-side (`is_excluded_type`). This is not paranoia:
the server flag has been observed to **leak epics through in production**, which
made bead-chain try to `bd close` an epic → `cannot close: N open child
issue(s)` → whole chain stalls. The case-insensitive client re-filter makes the
"never drive a container" invariant ironclad.

---

## 4. Wave sequencing & the blocking dependency graph

### 4.1 There are no "waves" — it's strictly serial, depth-1

The bead asks about "waves of ready beads mapped onto runs" and the
"concurrency model (serial vs parallel)." The answer is unambiguous:

**bead-chain is strictly serial. One bead in flight at any instant. No waves, no
fan-out, no parallel agents.** This is the *single-in-progress invariant* and it
is enforced aggressively:

- `state.current_bead` holds exactly one bead.
- `enforce_single_in_progress()` at startup: if a hard crash left *multiple*
  in-progress beads, it recovers the **head** and leaves the rest in-progress,
  to be picked up **one at a time** via the recovery tier on subsequent
  iterations. It never drives two at once.
- `execution_parallel_group` and `execution_mode` metadata hints are
  **deliberately ignored** — parallel grouping is meaningless to a serial
  driver, and the run mode is always `goal`.

So a "wave of ready beads" is processed as a **sequence**, not a batch. The
`bd ready` frontier is re-queried *after every close*, so the ready set is
recomputed each iteration rather than snapshotted into a wave.

### 4.2 The next-bead waterfall (`pick_next_bead`) — a strict 4-tier priority

After each bead closes, the next is chosen by a strict, highest-first waterfall:

| Tier | Source | Rule |
|------|--------|------|
| **0. Stranded recovery** | `list_recoverable_strands()` (`in_progress` + `hooked`) | A bead already in flight = residue from a crash/cancel. Finish it before anything new. Recovery beats *everything*. |
| **1. Blocking bug** | `next_blocking_bug()` | Any ready `bug` with `dependent_count > 0` cuts the line — fixing it unblocks downstream work. |
| **2. Epic affinity** | `next_ready_in_epic(just_closed.parent)` | If the just-closed bead had a parent epic with ready siblings, stay in that epic. Coherent commits/PRs beat queue-order optimality. |
| **3. Global ready** | `next_ready()` | Whatever `bd ready` hands us next. |

### 4.3 Respecting the blocking dependency graph

bead-chain trusts `bd ready` to filter blocked beads server-side, but layers
**belt-and-suspenders work-time blocker rechecks** at every claim/activate
boundary (the `bdboard-oals` fix: respect blocks at *claim time*, not just at
close time):

- **Blocking edge types that gate:** `blocks` and `waits-for` (`BLOCKING_DEP_TYPES`).
  `parent-child` / `discovered-from` / `related` do **not** gate.
- A blocker is satisfied **only when `closed`** — `open` / `in_progress` /
  `blocked` all still gate.
- `open_blocker_ids()` reads the full `bd show` deps and returns open blockers;
  `_reject_if_blocked()` rejects tiers 1–3; tier-0 (recovery) *reverts blocked
  strands to open* via `_unblocked_strands()` so they re-enter the queue behind
  their blockers.
- **Fan-out gates** (`waits_for: children-of(<spawner>)`) are *invisible to
  `bd blocked`* due to a beads CLI bug (`bead_chain-9sc`), so bead-chain detects
  them itself in `_has_fan_out_gate_issue` (checks the spawner has unclosed
  children via `has_open_children`) and refuses to drive a bead whose fan-out
  gate is unsatisfied.

The picker is *not allowed* to return a blocked or container bead;
`activate_next_bead` re-asserts both invariants one last time at the activation
boundary (fetching the full `bd show` record **once** and threading it into both
the blocker check and the fan-out check — call consolidation `bead_chain-lqf`).

---

## 5. Claim & close mechanics (the `bd` command surface — writes)

| Helper (`beads_writes.py`) | `bd` invocation | When |
|----------|-----------------|------|
| `claim(id)` | `bd update <id> --claim` | Atomic claim (the serializing point for the pick→claim race) |
| `revert_to_open(id)` | `bd update <id> --status=open` | Unwind a claim (cancel, close-fail, blocked strand) |
| `close(id, reason=…)` | `bd close <id> --reason "<reason>"` | After judges pass. Reason is `"bead-chain: LLM judges passed"` |
| `has_epic_in_progress()` | `bd list --type=epic --status=in_progress --json` | Decide whether to claim a parent epic |
| `close_eligible_epics()` | `bd epic close-eligible [--dry-run] --json` | Drain-time epic rollup (with recurring-molecule protection) |
| `check_gates()` | `bd gate check --json` | Empty-queue gate re-probe |
| `lint_warnings(id)` | `bd lint <id> --status all --json` | Missing-template-section warnings for the prompt |

### 5.1 Close (`close_current_bead_success`)

Runs only when wiggum has gone inactive *with* a current bead. Before the
`bd close` it applies two mid-flight guards (a bead's status can change *after*
bead-chain claimed it while it was open):

1. **Epic-leak guard** — if the current bead is somehow a container type, refuse
   to close, **revert** it (an in-progress epic is categorically broken and the
   recovery tier excludes epics, so it'd strand forever), and **stop the chain**.
2. **Mid-flight pin guard** (`is_pinned`, re-reads live status) — if a human
   pinned the bead after the claim, *respect the pin*: closing a pinned bead
   needs `--force` which bead-chain refuses to pass over a human's explicit park.
   Drop it as current, **keep trotting** (don't bump `completed_count`).

On `bd close` failure: **leave the bead in_progress** (so the next run's recovery
tier resumes it — never orphan partial work from its bead) and **stop the chain
loudly** (a close failure means something genuinely wrong). On success: bump
`completed_count`, clear `current_bead`. Either way it returns the just-closed
dict for epic-affinity routing.

### 5.2 The pick→claim race (`bead_chain-hvi`) — a known, accepted limitation

`pick_next_bead` reads the ready queue but doesn't claim; another driver (another
machine, a human in the bd UI, CI) can claim the same bead in the window before
`activate_next_bead` calls `claim()`. **Mitigation:** `bd update --claim` is
atomic at the DB layer, so at most one racer wins; the loser's `claim()` raises
`BeadsError`, bead-chain warns and **stops cleanly** rather than double-driving.
No distributed lock — the atomic claim *is* the lock; a second locking layer
would be redundant complexity (explicit YAGNI call-out in the source).

---

## 6. How it "invokes code-puppy" per bead (the real handoff)

This is the bead's core question, re-answered correctly. bead-chain does **not**
invoke code-puppy as a subprocess. Per bead it:

1. Builds a **goal-prompt string** from the bead dict (`format_bead_as_goal`).
2. Calls `wiggum_state.start(goal_prompt, mode="goal")` to arm the shared
   `WiggumState` singleton in `mode="goal"`.
3. Returns a **continuation dict** to code-puppy's `cli_runner`:
   ```python
   {"prompt": goal_prompt, "clear_context": True, "delay": 0.5, "reason": "bead_chain"}
   ```
   (Or, for bead #1, returns the prompt *string* directly from the command.)

code-puppy's continuation loop then clears context, waits 0.5s, and runs that
prompt as a normal agent turn — and wiggum's `/goal` judges take over the
work-and-verify loop for the next N turns. bead-chain is *out of the loop*
entirely until wiggum goes inactive again.

### 6.1 The goal prompt (`prompt.format_bead_as_goal`)

A richly enriched prompt — the driver's *only* contribution to the work is
*framing* it. Assembled (all enrichments soft-fail to empty so a bd blip never
strands the chain):

- **Preamble (mutually exclusive, in this order):** Recovery preamble (resuming
  a stranded bead — "assess current state before doing new work") **wins over**
  Triage-verification preamble (a bug filed+inline-fixed by a prior bead's agent
  via the bug-discovery protocol, detected by the `[bead-chain:triaged]`
  description marker) **wins over** no preamble.
- **Body:** `Complete beads issue <id>: <title>` + description.
- **`## Persistent Memories`** — `bd memories` digest (capped 12 entries / 280
  chars each), warm-starting the agent. *Policy:* surfaces bd's project-scoped
  memory only; deliberately **not** bridged to the host runtime's Kennel.
- **Issue metadata** — type, `P<priority>`, parent epic (+ title + 280-char
  description excerpt), labels.
- **`## Design`** — bd's ADR/design field (high-value for decision/spike beads).
- **`## Acceptance Criteria`** — so the agent sees the same contract the judges
  grade against (`bead_chain-2zx`).
- **`## Template Lint Warnings`** — `bd lint` missing-section warnings
  (`bead_chain-vmo`).
- **`## Related Context`** — non-gating context edges (`discovered-from`,
  `caused-by`, `validates`, `related`, `relates-to`, `tracks`).
- **Done checklist** — run linters, run tests, commit (no Claude co-author),
  `bd remember` durable insights.
- **`BUG DISCOVERY PROTOCOL`** appended to *every* prompt (file bugs, don't
  close them — judges are the only closer).

This is why the goal prompts you've seen include "LLM judges will verify
completion before this bead is closed" and the bug-discovery rubric: they are
emitted by `prompt.py`, verbatim, on every bead.

---

## 7. Config & inputs

bead-chain is nearly zero-config. Its inputs:

| Input | Source | Effect |
|-------|--------|--------|
| `--max=N` | CLI flag | Safety cap: stop after N beads closed this run. `None` = unbounded. Invalid → refuse to start. |
| `BEADS_BIN` | env var | Override the `bd` executable path. Validated (resolved to an absolute, executable file) before use — it's attacker-reachable. |
| The `bd` database | ambient (`.beads/` Dolt DB) | The entire work queue + dependency graph. |
| `execution_*` metadata | per-bead `bd` metadata | `execution_effort`→reasoning effort, `execution_model`→model, `execution_agent_type`→agent. Applied per bead, soft-fail per hint. Parallel/mode hints ignored. |
| wiggum / `/goal` | host plugin | The goal engine + judges. Hard prerequisite. |
| `judges.json` (host) | host config | The judges `/goal` consults (out of bead-chain's scope — it just reads `is_active()`). |

There is **no** `pyproject.toml`, no CI config, no server, no HTTP surface. The
test suite (`tests/`) mocks the `bd` subprocess for hermetic runs.

**Durability is explicitly *not* bead-chain's job** (ADR 0001): a drain is *not*
a session boundary. bead-chain never runs `bd dolt push/pull/export/import`.
Cross-machine sync lives in the host's `AGENTS.md` session-close protocol. An
interrupted chain's bead mutations stay in the local Dolt DB until the next
session-close pushes — documented, expected.

---

## 8. Failure, retry & partial-progress handling

### 8.1 Subprocess transport retries (`_run_bd` in `beads.py`)

- **`MAX_ATTEMPTS = 3`**, retrying **only** on `subprocess.TimeoutExpired`.
- **`DEFAULT_TIMEOUT = 15.0s`** per attempt, **exponential backoff** before each
  retry: `0.25 · 2^(n-1)` capped at `2.0s` (so 0.25, 0.5, 1.0, 2.0…).
- **Worst-case** fully-timed-out call: `15 + 0.25 + 15 + 0.5 + 15 = 45.75s`
  (`bead_chain-7b6` halved the old 91.5s budget).
- **Permanent failures are *not* retried** (fail fast): `FileNotFoundError`
  (bd not installed), `PermissionError` (not executable), and non-zero exits
  (real bd errors like "bead not found", "already closed").
- **UTF-8 forced** (`encoding="utf-8", errors="replace"`) — without it,
  `text=True` decodes with the Windows legacy code page (cp1252) and a single
  non-cp1252 byte (em-dash, box glyph) kills the pipe reader → `proc.stdout`
  comes back `None` → misleading "JSON object must be str… not NoneType". A
  real cross-platform footgun, fixed.
- **Bead-id validation** (`_validate_bead_id`): ids are pinned to
  `^[a-zA-Z0-9_.-]+$` and a leading `-` is rejected (else `bd` reads it as a
  flag) — defence against argument-confusion even though list-form `subprocess.run`
  already blocks shell injection.

### 8.2 Soft-fail vs hard-stop philosophy

Two tiers, applied consistently:

- **Hard-stop (halt the chain):** anything that compromises correctness — a
  `bd close` failure, a claim failure, a `bd ready` failure, an epic leak, a
  blocked bead reaching activation. These call `state.stop()` and bail.
- **Soft-fail (warn, keep trotting):** every *courtesy* enrichment — epic rollup,
  gate probe, `ensure_epic_in_progress`, memory digest, lint warnings, epic
  context, execution hints (per-hint). A flaky/missing/old `bd` subcommand
  (e.g. `br` lacking `memories`/`gate`) logs a warning and the chain finishes
  its drain cleanly. Losing a nicety is far less bad than stranding the queue.

### 8.3 Partial progress & recovery (the durability story)

- **Ctrl+C / cancel** → `_on_interactive_turn_cancel` fires, `state.stop()`,
  and the in-flight bead is left **`in_progress` on purpose**. The next
  `/bead-chain` run's recovery tier (tier 0) picks it up and re-prompts with the
  recovery preamble so the agent assesses on-disk state before doing new work.
  Partial work stays *paired with its bead* — no orphaning, no stranding.
- **Hard crash (SIGKILL, power loss)** → bypasses every Python handler and can
  leave multiple in-progress beads. `enforce_single_in_progress` at next startup
  recovers them one-at-a-time.
- **Close failure** → bead left in_progress, chain stopped, resumed via recovery
  next run.

### 8.4 The close-guard (premature-close protection)

`close_guard.py` registers a `run_shell_command` hook. While the chain is active,
it **blocks agent-issued `bd close` / `bd update --status=closed`** (returns
`{"blocked": True, "error_message": <reminder>}`), because the judges are the
*only* legitimate closer. The detector is regex-based but goes to real lengths to
avoid false positives: it blanks **heredoc bodies**, **quoted string literals**
(plain, double, and ANSI-C `$'...'`), and **text-flag arguments** (`-m`,
`--append-notes`, …) *before* scanning for command-boundary-anchored `bd close`.
bead-chain's *own* `bd close` calls use `subprocess.run` directly and never
traverse code-puppy's command runner, so the hook never fires on them — only
agent-issued shell commands are intercepted.

### 8.5 Empty-queue handling: gate probe → epic rollup → stop

When `pick_next_bead` returns `None`, `activate_next_bead` doesn't immediately
declare done:

1. **Gate probe** (`probe_resolved_gates` → `bd gate check`): resolvable gate
   types (`timer`/`gh:run`/`gh:pr`/`bead`) keep targets out of `bd ready` until
   the gate closes. A now-satisfied gate closing re-opens its target → re-probe
   the queue for one more iteration.
2. Still empty → **drain-time epic rollup** (`rollup_completed_epics` →
   `bd epic close-eligible`), called **once per session** (not per-bead — the
   `bead_chain-tfn` over-close fix: bd's cascade can sweep up *unrelated* epics
   if called too often). Recurring-molecule (`patrol`) epics are *protected* from
   rollup via a `--dry-run` preview + label/mol-type check.
3. `state.stop()` — "Good boy!".

---

## 9. Cross-cutting LIFECYCLE trace (the bead's stated focus)

The full bead lifecycle, selection → next wave, with the exact code path:

```
                        ┌─────────────────────────────────────────────┐
   /bead-chain  ───────▶│ handle_bead_chain_command                   │
                        │  enforce_single_in_progress() | next_ready()│  SELECTION
                        │  guards: excluded-type? blocked? recovery?  │
                        │  ensure_epic_in_progress(); claim()         │  CLAIM (bd update --claim)
                        │  apply_execution_hints()                    │
                        │  wiggum_state.start(prompt, mode="goal")    │  ARM /goal
                        │  return goal_prompt ───────────────────────┐│  INVOKE (1st turn)
                        └────────────────────────────────────────────┘│
                                                                       ▼
   ┌──────────────────────── code-puppy cli_runner runs the turn ─────────────┐
   │  wiggum /goal: agent works · self-corrects · LLM judges vote             │
   └──────────────────────────────────────────────────────────────────────────┘
                                                                       │
                        ┌──────────────────────────────────────────────▼─────┐
   each turn ──────────▶│ _on_interactive_turn_end (runs AFTER wiggum)        │
                        │  wiggum_state.is_active()?                          │  DETECT VERDICT
                        │   ├─ True  → return None (yield, /goal keeps going) │
                        │   └─ False → close_current_bead_success()           │  CLOSE (bd close --reason)
                        │              guards: epic-leak? pinned?             │
                        │              activate_next_bead(just_closed):       │
                        │                pick_next_bead() 4-tier waterfall ───┼─ RECOMPUTE READY FRONT
                        │                  tier0 recovery / tier1 bug /       │  (bd ready / list re-queried)
                        │                  tier2 epic-affinity / tier3 global │
                        │                blocked/fan-out/excluded rechecks    │
                        │                claim(); arm wiggum; return cont.dict│  NEXT WAVE (next bead)
                        │                empty? gate-probe → rollup → stop()  │
                        └────────────────────────────────────────────────────┘
```

- **Selection → claim:** atomic `bd update --claim`; parent epic claimed first.
- **Claim → invoke:** `wiggum_state.start(...)` + return prompt/continuation dict.
- **Invoke → detect verdict:** bead-chain *never grades*; it reads one bit
  (`is_active()`) that wiggum's judges flipped.
- **Detect → close-or-reopen:** judges-passed → `bd close`; cancelled → leave
  in_progress for recovery; close-fail → leave in_progress + halt.
- **Close → recompute ready front:** `bd ready`/`bd list` re-queried fresh every
  iteration (no snapshotted wave).
- **Recompute → next wave:** the 4-tier waterfall picks the next single bead;
  empty queue triggers gate-probe → epic-rollup → stop.

---

## 10. Assumptions baked into the bd CLI & code-puppy command surface

Things bead-chain *assumes*, worth knowing before any code-puppy-side change:

**About `bd`:**
- `bd ready` / `bd list` / `bd show` / `bd memories` / `bd lint` /
  `bd gate check` / `bd epic close-eligible` all accept `--json` and emit a
  stable shape. (bead-chain tolerates several output shapes for
  `epic close-eligible` and slices `{…}` out of human-prefixed gate/lint output —
  so it already defends against bd version drift here.)
- `bd update --claim` is **atomic** at the DB layer (the only concurrency
  guarantee it relies on).
- `bd ready` filters blocked + container beads server-side — but bead-chain
  re-checks both client-side because the server filter has *leaked* in prod.
- `--status` accepts a comma list (`in_progress,hooked`) → one subprocess for N
  statuses.
- `bd ready --json` *omits* a top-level `metadata` field (so execution hints
  re-fetch via `bd show`); `bd show --json` carries it.
- Closing a `pinned` bead needs `--force` (which bead-chain won't pass);
  closing an `epic` fails on open children.
- The `br` variant assumes `br` lacks `memories` and `gate` and degrades.

**About code-puppy:**
- wiggum's `/goal` mode exists, shares a `WiggumState` singleton, and exposes
  `state.start(prompt, mode="goal")` + `state.is_active()`.
- The `interactive_turn_end` hook runs callbacks **in registration order** and a
  continuation **dict** with `{"prompt", "clear_context", "delay", "reason"}` is
  honoured by `cli_runner` (the `/wiggum` contract).
- A command handler returning a **string** is executed as a prompt.
- `run_shell_command` hook can **block** a command by returning
  `{"blocked": True, ...}`.
- `code_puppy.config` exposes `set_openai_reasoning_effort` / `set_model_name` /
  `set_default_agent` (execution hints resolve these by name at call time and
  silently no-op if a setter vanishes under version drift).

These are exactly the coupling points a future code-puppy refactor of the
wiggum/goal loop would need to keep stable (the synthesis bead's concern).

---

## 11. Findings, gotchas & corrections

1. **bead-chain is a plugin, not an external runner.** (§0) The biggest framing
   correction. It runs in-process and reuses wiggum's `/goal` continuation loop;
   it shells out to **`bd`**, never to code-puppy.
2. **"Queue driver, not a goal engine"** is the load-bearing SRP boundary.
   bead-chain owns *what runs next*; wiggum owns *is the running thing done*. It
   has **zero goal-loop code** — only a one-bit `is_active()` read.
3. **Strictly serial, single-in-progress, no waves.** Parallel/mode metadata
   hints are deliberately ignored. A "wave" of ready beads is drained as a
   sequence with the frontier recomputed after every close.
4. **Lazy hook registration is load-bearing**, not an optimization — it's the
   only thing guaranteeing bead-chain observes wiggum's *settled* verdict.
5. **Doc drift in bead-chain (observation, not a code-puppy bug):**
   `__docs/Architecture.md` "API Conventions" claims *"30s timeout, 3 attempts
   with 0.5s/1.0s backoff."* The actual `beads.py` code uses
   **`DEFAULT_TIMEOUT = 15.0s`** with **exponential `0.25/0.5/1.0/2.0` backoff
   capped at 2.0s** (`bead_chain-7b6`). The prose wasn't updated when the retry
   policy was tightened. This is in the **external bead-chain repo**, which this
   bead forbids modifying — recorded here as a finding, **not** filed as a
   code_puppy bd bug (it's not a code_puppy defect).
6. **The "LLM judges passed" close reason** that closed prior spikes is a
   literal string constant in `close_current_bead_success`
   (`reason="bead-chain: LLM judges passed"`) — emitted by bead-chain's own
   `bd close` after wiggum went inactive. No external judge process.
7. **Defence-in-depth everywhere** because bd's server-side filters have leaked
   (epics, blocked beads) in prod. Client-side re-filtering is a deliberate,
   battle-scarred pattern, not redundancy.
8. **Durability is out of scope by design** (ADR 0001). A drain is not a session
   boundary; bead-chain never syncs Dolt. Interrupted chains are local-only
   until the next host session-close.

---

## 12. Appendix: file/symbol quick reference

```
bd/
├── register_callbacks.py
│   ├── handle_bead_chain_command       # /bead-chain [--max=N]  (entry point)
│   ├── _on_interactive_turn_end        # the driver: observe wiggum → close → activate
│   ├── _on_interactive_turn_cancel     # Ctrl+C → stop, leave bead in_progress
│   ├── _ensure_hooks_registered        # LAZY registration (run after wiggum)
│   └── _parse_max_iterations           # --max=N parsing (+ _PARSE_ERROR sentinel)
├── lifecycle.py
│   ├── enforce_single_in_progress      # startup invariant guard
│   ├── _unblocked_strands              # recoverable strands minus blocked (reverted)
│   ├── close_current_bead_success      # bd close (epic-leak + pin guards) → reason str
│   ├── pick_next_bead                  # 4-tier waterfall (recovery/bug/epic/global)
│   ├── _reject_if_blocked              # tier 1-3 work-time blocker recheck
│   ├── activate_next_bead              # pick → claim → arm wiggum → continuation dict
│   ├── ensure_epic_in_progress         # parent-first epic claim
│   ├── _has_fan_out_gate_issue         # children-of(...) gate detection (bd bug workaround)
│   ├── rollup_completed_epics          # once-per-session epic close (over-close fix)
│   └── probe_resolved_gates            # empty-queue gate re-probe
├── beads.py
│   ├── _run_bd                         # subprocess core: retry/backoff/utf-8/timeout
│   ├── _validate_bead_id / _bd_bin     # id + BEADS_BIN validation
│   ├── is_excluded_type / is_recurring_epic  # classification predicates
│   └── EXCLUDED_TYPES / RECOVERABLE_STATUSES / BLOCKING_DEP_TYPES / ...  # constants
├── beads_reads.py
│   └── next_ready / next_ready_in_epic / next_blocking_bug /
│       list_recoverable_strands / show / memories / open_blocker_ids / is_pinned
├── beads_writes.py
│   └── claim / revert_to_open / close / has_epic_in_progress /
│       close_eligible_epics / check_gates / lint_warnings
├── prompt.py
│   ├── format_bead_as_goal             # bead dict → /goal prompt (enrichment + preambles)
│   ├── is_triaged_bug / TRIAGE_MARKER  # bug-discovery triage-verify path
│   └── _RECOVERY_PREAMBLE / _BUG_DISCOVERY_PROTOCOL  # prompt scaffolding
├── close_guard.py
│   ├── detect_premature_close          # regex w/ heredoc/quote/flag blanking
│   └── on_run_shell_command            # run_shell_command hook → {"blocked": True}
├── execution_hints.py
│   ├── extract_execution_hints         # pure dict→dict filter
│   └── apply_execution_hints           # → config.set_* (soft-fail per hint)
└── state.py
    └── BeadChainState                  # active / current_bead / completed_count / max_iterations

External coupling points (code-puppy side):
  code_puppy.plugins.wiggum.state       # start(prompt, mode="goal"), is_active()
  code_puppy.callbacks                   # register_callback("interactive_turn_end"/...)
  code_puppy.command_line.command_registry  # register_command
  code_puppy.config                      # set_openai_reasoning_effort / set_model_name / set_default_agent
  cli_runner continuation loop           # runs the {"prompt", "clear_context", ...} dicts
```
