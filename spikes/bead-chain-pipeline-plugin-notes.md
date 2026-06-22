# bead-chain pipeline plugin — design notes

> Running capture of design thoughts for a new plugin that owns the full
> bead-chain pipeline. Each section is one thought, grounded in current code.
> Companion canvas: `spikes/possible-flow.canvas`.

---

## Thought 1 — Judge system: great for `/goal`, insufficient for bead-chain (no dynamic adjustment)

**Claim captured:** The current judge system works well for `/goal` but falls
short for bead-chain because there is no way to dynamically adjust the judges.

### Code backing (current judge system)

- A judge is a **static persisted record** `(name, model, prompt, enabled)` —
  `code_puppy/plugins/wiggum/judge_config.py` `JudgeConfig`. Source of truth is a
  single global file `JUDGES_FILE = DATA_DIR/judges.json` (`judge_config.py`).
- `/goal` picks the roster via `_resolve_judges(agent)` —
  `register_callbacks.py:241`, called inside `_run_goal_judges` at
  `register_callbacks.py:351`. It defers to `get_enabled_judges_or_default(...)`
  (`register_callbacks.py:263` → `judge_config.py:231`), which calls
  `load_judges()` and returns enabled judges or one synthetic `default`.
- The roster IS re-read fresh each iteration (load → filter), but the **only
  mutation path is the human-driven `/judges` TUI**
  (`code_puppy/command_line/judges_menu.py`). There is no programmatic API for
  the pipeline itself to add/remove/swap/reweight judges mid-run.
- The roster is **global, not per-task/per-bead** — one `judges.json` for
  everything. No notion of "these judges for this bead, those for that bead."
- Completion policy is **hard-coded strict unanimity** of non-abstaining judges
  (`_run_goal_judges` docstring, `register_callbacks.py:335+`; catalog
  §7.6/§8). No quorum, no weights, no per-bead policy knob.

### Where it falls short for bead-chain (implications of the above)

1. **No per-bead judge panels** — bead-chain processes a graph of heterogeneous
   beads, but the judge roster is one global static list.
2. **No runtime/programmatic adjustment** — judges can only change between
   iterations if a *human* edits `judges.json` via the TUI; the pipeline can't
   compose a panel from bead metadata.
3. **No policy flexibility** — unanimity is fixed; bead-chain can't set per-bead
   quorum/weighting.

### Tie to canvas

Node `9a9f4039725633f8` proposes *per-bead* agent + judge-panel creation with
prompt/tool/skill/mcp assignment — i.e. the dynamic, composed-per-bead panel
that today's static `judges.json` model cannot express.

---

## Thought 2 — Same problem on the implementor side: `/goal` & `/wiggum` can't adjust agent details at all

**Claim captured:** There's a parallel gap for the implementor agent — `/goal`
and `/wiggum` provide no way to adjust the agent's details (model, prompt,
tools, skills, MCP) at all. The agent is frozen at loop start.

### Code backing (implementor agent is frozen)

- The loop is driven by the `interactive_turn_end` hook
  `_on_interactive_turn_end(agent, prompt, result, ...)` —
  `register_callbacks.py:422` (registered at file bottom,
  `register_callback("interactive_turn_end", _on_interactive_turn_end)`). The
  `agent` is **passed in by core**; the plugin never constructs, reassigns, or
  reconfigures it.
- The continuation contract is a plain dict with only
  `{prompt, clear_context, delay, reason}` — `register_callbacks.py:476-481`
  (goal) and `:489-494` (wiggum). **No agent, model, prompt-config, toolset, or
  MCP field exists in the return shape.** The next iteration reruns on whatever
  agent core re-passes.
- Per-iteration the only thing that changes is the *user prompt string*: goal
  appends judge remediation notes (`f"{goal_prompt}\n\nJudge remediation
  notes:\n{notes}"`, `:477`); wiggum reuses `goal_prompt` verbatim (`:490`).
  Everything about the agent itself is untouched.
- Catalog confirms the same conclusion across both modes: "Agent reassigned
  between iterations? **No — frozen at loop start**" for both `/wiggum` and
  `/goal`; model = "agent's configured model"; system prompt = "agent's full
  prompt, rebuilt per run from the *same* agent"
  (`spikes/synthesis-context-prompt-tools.md` §2.1, §2.2).
- There is **no shared abstraction** for "make me an agent with model M, prompt
  P, toolset T" — the only place anything composes an agent on the fly is the
  judge side (`judge.py:236` `judge_agent = Agent(...)`, hand-rolled), and that
  path is judge-only and not reusable by the implementor
  (`synthesis-context-prompt-tools.md` §2.3 D1).

### Symmetry with Thought 1

- **Thought 1:** judges are a global static roster, not adjustable per-bead/at
  runtime by the pipeline.
- **Thought 2:** the implementor agent is likewise fixed for the whole loop —
  no per-bead/per-iteration adjustment of model, prompt, tools, skills, or MCP.
- Both sides lack a **runtime agent-composition** capability. A bead-chain
  pipeline plugin that wants per-bead staffing (canvas node
  `9a9f4039725633f8`: agent + judge panel with prompt/tool/skill/mcp
  assignment) gets **no** support from either the implementor or judge path as
  they exist today.

### Tie to canvas

Reinforces node `9a9f4039725633f8` — the "per bead agent ... creation with
prompt, tool (incl. bespoke via helios), skill, and mcp assignment to agent and
judges" covers BOTH sides: the agent (Thought 2) and the judge panel
(Thought 1).

---

## Thought 3 — When staffing implementors and judges, leverage `universal_constructor` to forge any missing tools

**Claim captured:** During per-bead creation of implementors and judges, the
`universal_constructor` (UC) should be used to create any tools the
agent/judge needs to complete its task when those tools don't already exist.

### Code backing (what UC is and how tools attach today)

- UC is a tool-forging subsystem: `code_puppy/plugins/universal_constructor/`
  (`registry.py`, `sandbox.py`, `models.py`). The `universal_constructor` tool
  exposes actions `create` / `call` / `list` / `update` / `info`
  (`agent_helios.py:62-67`).
- Tools UC creates are **Python files persisted on disk** under `USER_UC_DIR`,
  discovered by `UCRegistry.scan()` which rglobs `*.py`, reads each file's
  `TOOL_META`, and registers it by `full_name` (namespace + name)
  (`registry.py` `scan` / `_load_tool_file`). "The tools you create persist
  forever ... available across all sessions" (`agent_helios.py` system prompt).
- **Helios** is the dedicated UC agent: `HeliosAgent.get_available_tools()`
  returns `universal_constructor` + file/shell tools (`agent_helios.py:24-34`).
  It is gated by `UC_AGENT_NAMES = frozenset(["helios"])` (`config.py:91`) and
  the global `get_universal_constructor_enabled()` flag
  (`agent_manager.py:357`, `:509`).
- **Any agent can be granted UC-created tools**, not just Helios. A JSON agent's
  `get_available_tools()` pulls enabled UC tools from the registry and includes
  them as `uc:<full_name>` if listed in its config
  (`json_agent.py:127-151`). `agent_creator_agent.py:548-549` likewise appends
  `universal_constructor` to its toolset when UC is enabled.

### Why this matters for the pipeline (the gap to close)

- The per-bead staffing flow (Thoughts 1 & 2 — composing an implementor + judge
  panel with assigned tools) currently has **no step that detects a missing
  capability and fills it**. UC tools only exist if a human/Helios already
  forged them; `UCRegistry` only *discovers* what's already on disk
  (`registry.scan`). There is no "need tool X → create tool X → attach to this
  agent" loop.
- The proposal: at staffing time, if a bead's task requires a tool not present
  in the built-in `TOOL_REGISTRY` nor in the UC registry, invoke UC (via Helios
  or the `universal_constructor` tool directly) to **forge the missing tool**,
  then attach it to the implementor and/or relevant judges before they run.
- Because UC tools persist and are globally discoverable
  (`registry.list_tools`), a tool forged for one bead becomes reusable by later
  beads — the pipeline accretes capability over a run.

### Open questions / decisions (flagged, not resolved)

- **Trigger:** who decides a tool is "missing" — the staffing step, the
  implementor mid-run, or a judge that needs a verification tool? (Judges are
  currently read-only-ish; granting them tool-creation crosses the sandbox line
  noted in `synthesis-context-prompt-tools.md` §3.3 G4.)
- **Enablement gate:** UC is globally toggleable
  (`get_universal_constructor_enabled`). If the pipeline depends on UC, behavior
  when it's disabled must be defined.
- **Persistence scope:** UC writes to `USER_UC_DIR` (user-global), not
  per-project/per-bead. A pipeline forging many bespoke tools pollutes the
  global namespace unless scoped.

### Tie to canvas

Directly realizes node `9a9f4039725633f8`'s phrase "tool (including bespoke via
helios)" — "bespoke via helios" = UC-forged tools. Helios is the
`universal_constructor` agent (`agent_helios.py`).

---

## Thought 4 — Durability/observability gaps: the one-bit seam closes beads on incomplete judge runs, and a mis-wired bug can halt the chain

> Evidence note: bead-chain's own source is an external repo
> (`~/.code_puppy/plugins/bead_chain/`, `bead-chain-catalog.md` §0). bead-chain
> behavior below is cited from the spike catalogs; the wiggum-side seam is
> verifiable in-tree.

**Claim captured (four linked parts):**
1. bead-chain was built to be durable against crashes, but the goal/judge loop
   gives no proper insight into the process.
2. Beads get closed even when the judge process didn't actually complete.
3. There's guidance to keep work flowing — file a bug, unblock, and still get a
   proper look at the bug later.
4. But if the bug is wired wrong, the current bead can't be closed.

### Part 1 & 2 — The seam is one lossy bit; that's why incomplete runs still close

- The entire bead-chain ↔ goal handoff is a **single boolean**:
  `_on_interactive_turn_end` returns early while `wiggum_state.is_active()`, and
  treats `is_active() == False` as "bead done → close"
  (`bead-chain-catalog.md` §2.2; `synthesis-beadchain-goal-judges.md` §2.3).
- **`state.stop()` collapses THREE distinct outcomes into that one bit**, all
  verifiable in-tree in `code_puppy/plugins/wiggum/register_callbacks.py`:
  - judges complete → `state.stop()` (`register_callbacks.py:458` area, after
    `"✅ GOAL COMPLETE!"`);
  - **max-iterations exhausted** → `if loop_num >= max_iters: ... state.stop()`
    (`register_callbacks.py:463-468`);
  - cancel / `/goal_stop` → `_on_interactive_turn_cancel` → `state.stop()`
    (`register_callbacks.py:497-501`).
- There is **no exit-status surface** — no `WiggumState.outcome` enum, no
  `last_verdict`, no return payload from `state.stop()`. "An external observer
  (bead-chain) literally cannot tell success from exhaustion"
  (`synthesis-beadchain-goal-judges.md` §4).
- **Consequence (the false-positive close):** a bead that **burned its iteration
  budget without the judges ever passing** is a normal turn-end (not a cancel),
  so it slips straight through to `bd close --reason "bead-chain: LLM judges
  passed"` — mislabeled as success (`synthesis-beadchain-goal-judges.md` §4).
  The Ctrl+C/cancel path *is* covered (`_on_interactive_turn_cancel` stops the
  chain); the **exhausted** path is not.
- **Why no insight:** judge *content* never crosses the seam. `remediation_notes`
  round-trips intra-wiggum only (`state.get_state().remediation_notes`,
  `register_callbacks.py:471`); bead-chain reads only `is_active()`, so its
  logs/reasons "cannot explain *why* a bead failed — by design"
  (`synthesis-beadchain-goal-judges.md` §5.3).
- **Durability that DOES exist** (so the gap is specifically observability, not
  recovery): single-in-progress invariant + tier-0 stranded-recovery
  (`bead-chain-catalog.md` §4.1–4.2), and on `bd close` failure bead-chain
  **leaves the bead `in_progress` and stops the chain loudly** so the next run's
  recovery tier resumes it (`bead-chain-catalog.md` §5.1).

### Part 3 — The "file a bug, unblock, look later" mechanism

- A **BUG DISCOVERY PROTOCOL** is appended to *every* goal prompt: "file bugs,
  don't close them — judges are the only closer" (`bead-chain-catalog.md` §6.1).
  The close-guard enforces this: while the chain is active, `close_guard.py`
  blocks *agent-issued* `bd close` (`synthesis-beadchain-goal-judges.md` §5.4).
- Filed bugs re-enter the queue with priority via the waterfall **tier 1
  (blocking bug)**: any ready `bug` with `dependent_count > 0` cuts the line
  (`bead-chain-catalog.md` §4.2).
- "Get a proper look later" = the **Triage-verification preamble**: a bug filed
  + inline-fixed by a prior bead's agent is re-surfaced for verification,
  detected via the `[bead-chain:triaged]` description marker
  (`bead-chain-catalog.md` §6.1; `is_triaged_bug` / `TRIAGE_MARKER` §11 map).

### Part 4 — A mis-wired bug halts the chain (VERIFIED against bead-chain source at `../../bead-chain`)

**The protocol's *intended* wiring is close-safe.** The blocking-bug rubric tells
the agent to run `bd create --type=bug ... --blocks=<current-bead-id>
--priority=1` (`prompt.py:508-510`; `BugDiscoveryProtocol.md:147`). The filed
bug **stays open** while the judges close the current bead
(`HandleBugsDuringWork.md` Step 4 WARNING; `BugDiscoveryProtocol.md` mermaid
"present work (NEVER self-close)" -> "Judges->>Judges: close bead N", lines
78-79). So by design **`bd close` does NOT gate on an open inbound `blocks`
edge** — closing a bead that still has an open blocker is the documented happy
path.

> **Correction to my earlier flag (Thought 4 draft):** I'd flagged whether an
> open inbound `blocks` edge makes `bd close` refuse. The source answers **no** —
> the protocol deliberately closes a bead whose `blocks`-bug is still open. The
> close-refusal trigger is something else (below).

**What actually makes `bd close` refuse and halt the chain.**
`close_current_bead_success` (`lifecycle.py:223`) documents the exact failure
families:
- **Open child** — if the current bead has an open child, `bd close` "would fail
  with 'open child issue(s)'" (`lifecycle.py` docstring, ~line 262). The
  type-detectable case (epic) is caught by the `is_excluded_type` branch
  (refuse -> `revert_to_open` -> `state.stop()`); any non-epic bead that ends
  up with an open child hits the generic failure path.
- **Pinned mid-flight** — closing needs `--force`, which `close()` never passes
  (`beads_writes.py:73-78`, no `--force`); bead-chain respects the pin and trots
  on (does NOT halt) (`lifecycle.py` pin branch, ~line 300).
- **Any other `BeadsError`** — `except BeadsError:` -> leave bead `in_progress`
  -> `state.stop()` -> **whole chain halts loudly** (`lifecycle.py:320-335`).

**So the precise "wired wrong -> can't close" mechanism:** if a discovered bug is
mis-wired as a **parent-child** of the current bead (current bead becomes the
parent / the bug becomes its open child) instead of the prescribed **`blocks`**
edge, then `bd close <current-bead>` fails with `open child issue(s)` -> generic
`BeadsError` -> bead-chain leaves the bead `in_progress` and `state.stop()`s the
entire chain (`lifecycle.py:320-335`). Because the agent can't self-close
(close-guard; `BugDiscoveryProtocol.md:206-207`) and judges are the only legit
closer, the bead is **un-closable until a human re-wires the dependency** —
exactly the captured claim.

**Why this is easy to trip:** the rubric hands the agent a raw `bd create ...
--blocks=<id>` shell line (`prompt.py:495-513`) with no validation that the edge
type/direction is correct. A single wrong flag (`--parent` instead of
`--blocks`, or `--blocks` pointed the wrong way so the current bead ends up
depending-on/childing the bug) converts "file a bug and keep flowing" into
"halt the whole chain." There is no guard between the agent's `bd create` and
the later `close()` that re-validates the in-flight bead is still closable.

### Design implication for the new pipeline plugin

- Owning the full pipeline means the plugin can **replace the one-bit seam with
  a real terminal status** (the `synthesis` REC-1 seed: `complete` /
  `exhausted` / `cancelled`) so "exhausted" never closes as "passed."
- It can also **carry judge content** (verdicts/remediation notes) out to the
  driver for per-bead insight, instead of discarding it intra-wiggum.
- And it can **decouple bug-wiring failures from chain liveness** — e.g.
  validate filed-bug edges before they can block the in-flight bead, so a
  mis-wired bug parks one bead rather than halting the whole chain.
