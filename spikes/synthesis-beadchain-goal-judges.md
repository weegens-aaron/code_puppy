# Spike: Synthesis — how `bead-chain` drives code-puppy `/goal` + LLM judge verdicts

> **Bead:** `code_puppy_oss-awj` · Type: spike · Priority: P2
> **Status:** research-only SYNTHESIS — documents the integration boundary between
> the external `bead-chain` orchestrator and code-puppy's judge-driven `/goal`
> retry loop *as it exists today*. Makes **no** behavioral code changes.
> **Inputs (all closed / present):**
> - `bead-chain` catalog — `code_puppy_oss-8sv` → [`bead-chain-catalog.md`](./bead-chain-catalog.md)
> - `/goal` `/wiggum` `/judges` synthesis — `code_puppy_oss-0ae` → [`synthesis-context-prompt-tools.md`](./synthesis-context-prompt-tools.md)
> - Live wiggum plugin: `code_puppy/plugins/wiggum/{register_callbacks,judge,judge_config,state}.py`
> - Core continuation loop: `code_puppy/cli_runner.py` (the `while True:` block ~L961)

This doc answers the bead's one load-bearing question:

> **Where exactly does "LLM judges passed" become `bd close --reason`?**
> i.e. the precise seam where a judge verdict crosses from code-puppy's `/goal`
> engine to bead-chain's `bd close` call.

---

## 0. The headline (read this first)

**There is no process boundary.** The bead's framing asks whether the verdict
crosses via *"exit code, stdout marker, or bd state."* The answer is **none of
those.** It crosses as a single **in-process Python boolean** —
`WiggumState.active`, an attribute on one module-level singleton
(`code_puppy/plugins/wiggum/state.py`) — read directly by bead-chain's
`interactive_turn_end` hook in the *same* Python process, on the *same* callback
bus, microseconds after wiggum's hook flipped it.

This is the direct corollary of the `bead-chain` catalog's headline correction
(§0 there): **bead-chain is a code-puppy plugin, not an external loop runner.**
It never spawns code-puppy, never reads a code-puppy exit code, never parses
code-puppy stdout. Both wiggum and bead-chain register the **same hook name**
(`interactive_turn_end`) and both run, in registration order, inside core's one
continuation loop. The "seam" is therefore not an IPC channel at all — it's a
**shared in-memory flag plus a guaranteed callback ordering.**

```
              ┌─────────────────── ONE code-puppy process ───────────────────┐
              │                                                               │
  judges vote │   wiggum hook:  state.stop()  ──flips──▶  WiggumState.active  │
   complete   │                                              = False         │
              │                                                 │            │
              │                                                 ▼            │
              │   bead-chain hook:  if not wiggum_state.is_active():         │
              │                         bd close --reason "...judges passed" │
              └───────────────────────────────────────────────────────────────┘
                         ▲                                       │
                         │ subprocess.run(["bd", ...])           │ subprocess.run(["bd","close",...])
                         └──────────────── bd CLI (Dolt DB) ─────┘
```

The **only** subprocess boundary in the whole round-trip is between bead-chain
and the **`bd` CLI** (the beads tracker). code-puppy's verdict never traverses
it; `bd close` is the *consequence* of the verdict, issued by bead-chain after
it reads the in-process flag.

---

## 1. The round trip, step by step (with exact symbols)

The full loop the bead asks to map, with the precise file/function/line where
each hop happens. Two plugins, one core loop, one `bd` CLI.

| # | Actor | What happens | Exact code site |
|---|-------|--------------|-----------------|
| 1 | **bead-chain** | Picks a bead off the `bd ready` frontier, claims it | `lifecycle.pick_next_bead` → `beads_writes.claim` (`bd update --claim`) |
| 2 | **bead-chain** | Builds a `/goal` prompt from the bead dict | `prompt.format_bead_as_goal` |
| 3 | **bead-chain** | Arms the **shared** wiggum singleton in goal mode | `wiggum_state.start(goal_prompt, mode="goal")` |
| 4 | **bead-chain** | Hands the prompt back to core to run | returns prompt string (bead #1) or `{"prompt", "clear_context": True, "delay": 0.5, "reason": "bead_chain"}` continuation dict |
| 5 | **core** | Runs the prompt as an agent turn; on completion fires every `interactive_turn_end` callback | `cli_runner.py` `while True:` loop → `on_interactive_turn_end(...)` |
| 6 | **wiggum** | (runs FIRST) fans out judges in parallel | `register_callbacks._on_interactive_turn_end` → `_run_goal_judges` → `asyncio.gather(judge_goal …)` |
| 7 | **judges** | Each judge = ephemeral read-only `pydantic_ai.Agent`, returns `GoalJudgeOutput{complete, notes}` → normalized to `GoalJudgement` | `judge.py::judge_goal`, structured `ToolOutput` named `goal_judgement` |
| 8 | **wiggum** | Tallies: `all(v.complete for v in voting)` over **non-abstaining** judges | `_run_goal_judges` → `voting = [v for v in verdicts if not v.abstained]` |
| 9 | **wiggum** | If complete → prints ` GOAL COMPLETE!`, calls `state.stop()`, returns `None` | `_on_interactive_turn_end` `if complete:` branch |
|  | **— THE SEAM —** | `WiggumState.active` is now `False`. This boolean *is* the verdict. | `state.py::WiggumState.stop()` sets `active = False` |
| 11 | **bead-chain** | (runs AFTER wiggum) its hook reads `wiggum_state.is_active()` → `False` | `bead_chain/register_callbacks._on_interactive_turn_end` |
| 12 | **bead-chain** | Closes the bead | `lifecycle.close_current_bead_success` → `beads_writes.close(id, reason="bead-chain: LLM judges passed")` → `subprocess.run(["bd","close",id,"--reason",...])` |
| 13 | **bead-chain** | Picks the next bead (4-tier waterfall), back to step 1 | `lifecycle.activate_next_bead` |

The verdict-to-`bd close` distance is **two hook invocations on one callback
list and one attribute read** — no serialization, no marshalling, no channel.

---

## 2. The exact seam, zoomed all the way in

### 2.1 Where "judges passed" is *computed* (code-puppy side)

`code_puppy/plugins/wiggum/register_callbacks.py::_run_goal_judges`:

```python
voting = [v for v in verdicts if not v.abstained]
if not voting:
    all_complete = False           # all-abstain → treated as incomplete
else:
    all_complete = all(v.complete for v in voting)   # STRICT UNANIMITY
```

This is the completion rule the persisted memory `goal-completion-rule-and-cap`
describes: **strict unanimity of non-abstaining judges.** Abstainers
(model-not-in-config, endpoint error, plumbing crash) get *no vote*
(`judge.py` returns `GoalJudgement(abstained=True)` on those paths).

### 2.2 Where the verdict *becomes the flag* (the seam itself)

Same file, `_on_interactive_turn_end`:

```python
if complete:
    _display_llm_judge(" GOAL COMPLETE!", final=True)
    state.stop()        # ← WiggumState.active = False.  THIS IS THE WHOLE SIGNAL.
    return None
```

`state.stop()` (`state.py`) sets `active = False`. That single assignment is the
entire cross-plugin signal. No return value carries it (wiggum returns `None`);
no exit code, no stdout token. The bit lives on the `_STATE` singleton that
**both** plugins import.

### 2.3 Where the flag *becomes `bd close`* (bead-chain side)

`bead_chain/register_callbacks._on_interactive_turn_end` (traced in the
bead-chain catalog §2.2):

```python
if not state.is_active():       return None     # bead-chain not engaged
if wiggum_state.is_active():    return None     # /goal still cooking → yield
# else: wiggum just went inactive → close this bead
just_closed = await asyncio.to_thread(close_current_bead_success)
...
```

`close_current_bead_success` (`lifecycle.py`) calls
`beads_writes.close(bead_id, reason="bead-chain: LLM judges passed")`, which is
the **only** place that literal string is produced. It is a hardcoded constant on
*bead-chain's* side — code-puppy never emits it.

### 2.4 Why the ordering is guaranteed (the load-bearing detail)

Core's continuation loop picks **the first dict** returned by any hook:

```python
# cli_runner.py
continuation_requests = await on_interactive_turn_end(current_agent, ...)
continuation = next((r for r in continuation_requests if isinstance(r, dict)), None)
```

`on_interactive_turn_end` runs callbacks **in registration order** and collects
their returns into a list. So:

- **wiggum registers at startup** (its `register_callbacks` runs on plugin load).
- **bead-chain registers its turn-end hook LAZILY** — only on the first
  `/bead-chain` invocation (`_ensure_hooks_registered`, bead-chain catalog §2.3)
  — guaranteeing it is appended **after** wiggum's.

That ordering is the entire correctness argument for the seam: wiggum **decides
and flips the flag first**; bead-chain **observes the settled flag second**. If
bead-chain ran first it would read a stale/unsettled `is_active()` and the
observe-after-engine contract collapses. The lazy registration is not an
optimization — it is the synchronization primitive.

When `/goal` is **incomplete**, the symmetry holds the other way: wiggum stays
`active`, returns its own continuation dict (`reason: "goal"`), and that dict —
being first in the list — wins; bead-chain sees `is_active() == True` and returns
`None`. So **at most one plugin ever returns a dict per turn**, and the `next(...)`
arbitration never actually has to choose between two competing dicts. The flag is
the mutex.

---

## 3. Configuration map — what's set where, on each side

The bead asks where *judges, models, and `goal_max_iterations`* are configured on
each side of the seam. They live almost entirely on the **code-puppy side**;
bead-chain contributes only per-bead *overrides* and an *outer* cap.

### 3.1 code-puppy side (the `/goal` engine + judges)

| Knob | Source | Default / rule | Code site |
|------|--------|----------------|-----------|
| **Judge roster** (who votes) | `judges.json` (`$DATA_DIR/judges.json`), edited via `/judges` TUI | zero-config → one synthetic `default` judge (`get_enabled_judges_or_default`) | `judge_config.py` |
| **Judge models** | per-judge `JudgeConfig.model`; default judge uses the **implementor's** model | `_resolve_judges` computes the fallback model | `judge_config.py`, `register_callbacks._resolve_judges` |
| **Judge prompts** | per-judge `JudgeConfig.prompt` or `DEFAULT_JUDGE_PROMPT` | strict, read-only, "never modify files" | `judge_config.py::DEFAULT_JUDGE_PROMPT` |
| **Completion rule** | hardcoded | strict unanimity of non-abstainers; all-abstain ⇒ incomplete | `_run_goal_judges` |
| **`goal_max_iterations`** | `/set goal_max_iterations=<int>` → `config.get_value` | default `10`, clamped `[1, 1000]` | `register_callbacks._get_goal_max_iterations` |
| **Judge request cap** | hardcoded | `UsageLimits(request_limit=200)` per judge | `judge.py::GOAL_JUDGE_REQUEST_LIMIT` |
| **Judge tools** | implementor's tools ∩ `_READ_ONLY_TOOLS` + `inspect_goal_history` | shell IS allowed (judges run tests) | `judge.py::_READ_ONLY_TOOLS` |

### 3.2 bead-chain side (the queue driver)

| Knob | Source | Effect | Code site |
|------|--------|--------|-----------|
| **`--max=N`** | `/bead-chain --max=N` flag | OUTER cap: stop after N **beads closed** (≠ goal iterations) | `register_callbacks._parse_max_iterations`, `state.max_iterations` |
| **`execution_model`** | per-bead `bd` metadata | overrides the implementor model for that bead → `config.set_model_name` | `execution_hints.apply_execution_hints` |
| **`execution_effort`** | per-bead `bd` metadata | reasoning effort → `config.set_openai_reasoning_effort` | `execution_hints` |
| **`execution_agent_type`** | per-bead `bd` metadata | implementor agent → `config.set_default_agent` | `execution_hints` |
| **`BEADS_BIN`** | env var | path to the `bd` binary | `beads._bd_bin` |

**Key separation of concerns:** bead-chain configures *which model implements*
the bead (via `execution_*` hints) but has **zero say over the judges.** Judge
identity, model, prompt, and the unanimity rule are entirely code-puppy/`judges.json`
territory. bead-chain reads exactly one bit of judge output (`is_active()`); it
cannot see *which* judge passed, *how many*, or *why*.

### 3.3 The two caps are independent and easy to confuse

There are **two separate iteration ceilings**, one per side, and they count
different things:

```
/bead-chain --max=3                 ← OUTER: at most 3 BEADS closed this run
   └─ bead #1  /goal (≤ goal_max_iterations retries)   ← INNER: per-bead judge retries
   └─ bead #2  /goal (≤ goal_max_iterations retries)
   └─ bead #3  /goal (≤ goal_max_iterations retries)
```

`goal_max_iterations` (default 10) bounds the judge-retry loop **within one
bead**. `--max=N` bounds **how many beads** the chain drains. Neither knows about
the other. A `--max=3` run can burn up to `3 × goal_max_iterations` agent turns.

---

## 4. THE headline finding — the one-bit signal is lossy (verdict ambiguity)

This is the most important *new* finding of this synthesis, and it falls out
directly from "the seam is a single boolean."

**`WiggumState.active == False` collapses three distinct `/goal` outcomes into one
indistinguishable bit.** All three call `state.stop()`:

| Real outcome | wiggum code path | flag after |
|--------------|------------------|------------|
| Judges voted complete  | `if complete: ... state.stop()` | `active = False` |
| **Hit `goal_max_iterations`** without passing  | `if loop_num >= max_iters: ... state.stop()` | `active = False` |
| Manual `/goal_stop` or Ctrl+C  | `handle_wiggum_stop_command` / cancel hook → `state.stop()` | `active = False` |

bead-chain's hook sees **the same `is_active() == False`** in all three cases and,
for the non-cancel paths, unconditionally closes the bead with
`--reason "bead-chain: LLM judges passed"`.

**Consequence:** a bead that **exhausted its iteration budget without the judges
ever passing** gets closed as *"LLM judges passed"* — a **false-positive close.**
The Ctrl+C/cancel path is covered (bead-chain's `_on_interactive_turn_cancel`
stops the chain), but the **max-iterations-exhausted** path is a normal turn-end,
not a cancel, so it slips straight through to a mislabeled `bd close`.

### Why it happens

The `/goal` engine has no notion of an *exit status* — only "am I still looping?"
There is no `WiggumState.outcome` enum, no `last_verdict` field, no return
payload from `state.stop()`. An external observer (bead-chain) literally **cannot
tell success from exhaustion** through the public surface it's given
(`is_active()`).

### Where the fix belongs (and why it isn't filed as a code_puppy bug here)

- The *mislabeled reason string* is produced on **bead-chain's** side (an external
  repo this bead is not permitted to modify), so it's not a code_puppy defect to
  file against this tree.
- The *root enabler* — `/goal` exposing only a one-bit `is_active()` with no
  distinguishable terminal status — **is** a code-puppy-side observation. It's the
  natural seed for a follow-up (see §6, REC-1). It is the central subject of this
  spike, not an unrelated incidental bug, so per the synthesis-spike convention
  (mirroring `code_puppy_oss-0ae`) it's documented as a roadmap seed rather than
  spun out as a separate bead mid-spike.

---

## 5. Secondary findings & confirmations

1. **Judges are NOT sandboxed** (`goal-judges-shell-not-sandboxed`): confirmed in
   `judge.py::_READ_ONLY_TOOLS` — `agent_run_shell_command` is on the allow-list,
   so a `/goal` judge driven by a bead-chain bead can run arbitrary shell (e.g.
   the test suite) to verify completion. The "read-only" guarantee is
   prompt-deep (`DEFAULT_JUDGE_PROMPT` says "never modify files"), not
   sandbox-deep. Across the seam this means *bead-chain-driven judges have full
   shell* — relevant if the chain runs unattended.

2. **Zero-config still votes** (`judges-config-system`): if `judges.json` has no
   enabled judges, `get_enabled_judges_or_default` synthesizes an ephemeral
   `default` judge on the implementor's model. So a bead-chain run never stalls
   for "no judges configured" — there is always ≥1 voter, hence
   `_run_goal_judges`' `if not judges: return False, "No judge agents
   configured.", []` is **dead code** (also flagged in the prior synthesis,
   CHORE-1).

3. **`remediation_notes` round-trips through the shared singleton, not the seam.**
   On an *incomplete* verdict wiggum writes `state.get_state().remediation_notes =
   notes` AND folds the same notes into the next prompt. bead-chain never reads
   `remediation_notes` — it's purely intra-wiggum. The seam carries *only*
   `is_active()`; the judge *content* never reaches bead-chain. (So bead-chain
   logs/reasons cannot explain *why* a bead failed — by design.)

4. **The close-guard prevents the agent from forging the verdict.** While the
   chain is active, `close_guard.py` (a `run_shell_command` hook) blocks
   *agent-issued* `bd close`. The judges are the only legitimate closer, and the
   only path to `bd close` is bead-chain's own `subprocess.run` *after* the flag
   flips. The implementor agent cannot short-circuit the seam by shelling out
   `bd close` itself. (bead-chain catalog §8.4.)

5. **`execution_model` vs judge model can silently diverge.** bead-chain's
   `execution_model` hint sets the *implementor* model per bead, but the **default
   judge** uses whatever the implementor's model resolves to at judge time
   (`_resolve_judges` → `get_pydantic_agent().model.model_name`). So a per-bead
   `execution_model` override *also* moves the default judge's model — but any
   *explicitly configured* judge in `judges.json` is pinned to its own
   `JudgeConfig.model` and ignores the hint. Mixed rosters → mixed behavior. Worth
   knowing when reasoning about "which model graded this bead."

---

## 6. Recommendations (follow-up bead seeds — nothing committed)

Mirroring the prior synthesis's roadmap style. These are decomposition seeds, not
work orders.

| # | Type | Seed | Rolls up |
|---|------|------|----------|
| **REC-1** | feature | **Give `/goal` a distinguishable terminal status.** Add a `WiggumState.last_outcome` enum (`complete` / `exhausted` / `cancelled`) set right before each `state.stop()`, exposed via a `get_last_outcome()` read. Lets *any* observer (bead-chain or future plugins) tell success from exhaustion without guessing. | §4 (the false-positive close) |
| **REC-2** | decision | **Should an exhausted `/goal` count as a bead failure?** Today it's invisibly closed as "passed." Decide the contract: close-as-failed? leave in_progress for recovery? requires REC-1 first. | §4 |
| **REC-3** | chore | **Remove dead `if not judges:` branch** in `_run_goal_judges` (unreachable; default judge always synthesized). Same finding as prior synthesis CHORE-1. | §5.2 |
| **REC-4** | decision | **Judge sandboxing for unattended chains.** bead-chain runs `/goal` autonomously; judges have full shell. Decide whether autonomous-mode judges need a tighter allow-list. Ties to prior synthesis DEC-2. | §5.1 |
| **REC-5** | doc | **Document the two-cap model** (`--max` vs `goal_max_iterations`) in user-facing `/goal` + bead-chain help — they're trivially confused. | §3.3 |

REC-1 is the spine: it's the smallest change that removes the §4 ambiguity and
unblocks REC-2.

---

## 7. TL;DR for the next reader

- **The seam is not IPC — it's a shared boolean.** "LLM judges passed" crosses
  from `/goal` to `bd close` as `WiggumState.active = False`, read in-process by
  bead-chain's `interactive_turn_end` hook. No exit code, no stdout marker, no bd
  state read-back. bead-chain shells out only to **`bd`**, never to code-puppy.
- **Ordering is the synchronization primitive.** wiggum (startup) flips the flag
  first; bead-chain (lazy-registered) observes it second. The flag is also the
  mutex — at most one plugin returns a continuation dict per turn.
- **Config split:** judges/models/prompts/unanimity/`goal_max_iterations` are
  **code-puppy/`judges.json`** territory; bead-chain owns only `--max=N` (an
  *outer* bead cap) and per-bead `execution_*` model/effort/agent overrides. The
  two caps are independent and count different things.
- **Headline gotcha:** the one-bit signal is **lossy.** `complete`,
  `max-iterations-exhausted`, and `manual-stop` all collapse to
  `active == False`, so a bead that ran out of retries without passing is closed
  as *"LLM judges passed"* — a false-positive. Root cause is code-puppy-side
  (`/goal` has no terminal status); the mislabel surfaces on the
  not-modifiable-here bead-chain side. Seed: REC-1.
- **Judges have full shell** even when driven unattended by a chain; the
  read-only guarantee is prompt-deep, not sandbox-deep.
