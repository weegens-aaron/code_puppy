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

---

## Thought 5 — Plugin owns the full line; staffing is pluggable; surfacing is non-LLM

**Claim captured (composite):** The new plugin owns the entire pipeline end-to-end —
**no dependency on `wiggum`, `/goal`, or `judges.json`.** Worker and inspector
**staffing agents** ship with the plugin but are pluggable: a user can swap in
their own staffing agent, or **disable staffing entirely** and fall back to a
single default worker (code-puppy) + default judge pool. Crafted system prompts
accumulate in a **prompt pool**, and **non-LLM methods** must surface candidate
prompts from that pool — staffing decisions cannot afford an LLM call per bead.

### 5.1 Ownership: the plugin replaces the wiggum/goal/judges stack

The current pipeline (synthesis §1) crosses **two plugins** sharing one in-process
boolean:

```
bead-chain hook → wiggum hook → judges.json → WiggumState.active = False → bd close
```

The new plugin **owns every box** in `possible-flow.canvas`:

- Bead pulling (today: `bead-chain`'s `lifecycle.pick_next_bead`).
- Worker composition + execution (today: core's frozen agent + `wiggum`'s loop).
- Inspector composition + voting (today: `judges.json` static roster + hardcoded
  unanimity in `wiggum/register_callbacks.py::_run_goal_judges`).
- Verdict → `bd close` (today: the lossy 1-bit seam, synthesis §4).

**Consequences this unlocks:**

1. **The seam stops being one bit.** With no inter-plugin handoff there is no
   shared `is_active()` to read. The inspection step returns a structured
   verdict in-process (the §4 false-positive close path simply cannot exist).
2. **No more hook-ordering dance.** Today `bead-chain` registers its
   `interactive_turn_end` hook LAZILY (synthesis §2.4) so it appends after
   `wiggum`'s — that ordering IS the synchronization primitive. Owning the loop
   replaces an emergent invariant with a function call.
3. **`remediation_notes` stop being intra-wiggum** (synthesis §5.3). Inspector
   content can flow out to the driver for per-bead insight without crossing a
   plugin boundary.
4. **The close-guard simplifies.** Today `close_guard.py` blocks
   *agent-issued* `bd close` while the chain is active (synthesis §5.4) because
   the implementor and the judges share the agent surface. With a plugin-owned
   inspector path, the closer is the plugin itself; the guard becomes a default-
   on policy, not a structural necessity.

### 5.2 Staffing modes (three, per side — worker and inspector are symmetric)

Each side of the pipeline (worker staffing, inspector staffing) has the same
three modes, set independently:

| Mode | Behavior | When to use |
|------|----------|-------------|
| **plugin-default** | Built-in staffing agent composes the 5 layers (prompt / bead content / tools / skills / mcp) per bead | Default; covers the canvas's "per bead agent and judge panel creation" |
| **BYO** | User-supplied staffing agent (any registered agent) is invoked with the bead + pool access; returns an `AgentSpec` | Domain-specific staffing (e.g. a frontend-aware staffer that always picks WCAG-tuned inspectors) |
| **disabled** | Skip composition; use the **single default worker** (code-puppy with its built-in toolset) and the **default judge pool** | Cheapest path; mirrors today's `/goal` behavior; useful as a baseline for A/B comparisons |

The two sides are independent: disabling **worker** staffing while keeping
**inspector** staffing on is a valid configuration (and probably the lowest-risk
incremental rollout — get smarter inspection first, leave the implementor as
code-puppy).

**Staffing-agent contract (uniform across modes):** `(bead, pool, ctx) -> AgentSpec`
where `AgentSpec` is the 5-layer recipe (prompt-id, bead-content slice, tool
set, skill set, mcp set) — i.e. the canvas's worker/inspector composition stack
is the *output schema* of any staffing agent, not a fixed control flow.

### 5.3 The prompt pool — a curated registry, not a roster

Crafted **system prompts** accumulate in a pool. This is the structural analog
of the UC tool registry (Thought 3): persisted, globally discoverable, additive
across runs.

| Aspect | Prompt pool | UC tool registry (Thought 3) |
|--------|-------------|------------------------------|
| Unit | Crafted agent system prompt + metadata (tags, embeddings, prior-pass stats) | Forged Python tool file + `TOOL_META` |
| Storage | Plugin-owned (TBD: `$DATA_DIR/prompt_pool/*.md` or similar) | `USER_UC_DIR/*.py` |
| Authoring | Manually crafted, imported, or distilled from prior runs | UC/Helios `create` action |
| Discovery | Non-LLM surfacing (§5.4) | `UCRegistry.scan()` rglob |
| Per-bead use | Staffing agent picks N candidates per slot (worker prompt, each inspector prompt) | Staffing agent attaches relevant tools |

Crucially the pool is **not a roster.** Today's `judges.json` IS a roster — the
*set of judges that vote* is the file's contents. A prompt pool is a **menu**:
the staffing agent picks per-bead which prompts to instantiate. Same prompt can
back a worker on one bead and an inspector on another.

### 5.4 Non-LLM surfacing is load-bearing, not an optimization

**Why no LLM in the surfacing layer:**

- **Cost.** A 50-bead chain with staffing-pick-via-LLM = 50 extra LLM calls just
  to pick a prompt, before any actual work. Compounds with N inspectors per bead.
- **Determinism.** Staffing decisions need to be reproducible — same bead, same
  pool, same surfacing result. Reproducible failures are debuggable; LLM-picked
  prompts are not.
- **Latency on the hot path.** Staffing sits between every bead transition; an
  LLM call there serializes the whole chain on the slowest model.
- **Bootstrapping.** When the pool is small (early days), LLM ranking adds
  almost nothing over a hand-tuned tag match.

**Candidate non-LLM methods (decision deferred, options listed):**

| Method | What it indexes | Strength | Weakness |
|--------|-----------------|----------|----------|
| **Tag match** | Hand-authored tags on each pool entry (e.g. `frontend`, `tdd`, `security-review`) | Trivial; deterministic; cheap to author | Requires curation; brittle to taxonomy drift |
| **BM25 / sparse retrieval** over prompt body + bead text | Lexical overlap | No embedding model needed; works zero-config | Misses synonymy |
| **Dense embeddings** (precomputed once per pool entry; cosine vs bead embedding) | Semantic similarity | Catches paraphrase; works without tags | Needs an embedding model + cache |
| **Prior-pass score** (per-prompt rolling success rate from prior inspections) | Empirical performance | Closes the loop with §5.6 verdicts | Cold-start problem on new prompts |
| **Hybrid** (tag pre-filter → BM25/embed rank → prior-pass tiebreak) | All of the above | Best practical accuracy | Most moving parts |

The staffing agent receives the surfacing result as a *ranked candidate list*
and makes the final pick — possibly with an LLM call inside the staffing agent
itself when it runs (that LLM call is amortized across all 5 layers of one
spec, not per-prompt).

### 5.5 Built-in agents the plugin ships (the "with batteries" baseline)

To preserve out-of-the-box usability while staying pluggable:

- **Default worker staffing agent** — plugin-built; uses non-LLM surfacing +
  cheap LLM finalization to produce an `AgentSpec`.
- **Default inspector staffing agent** — same shape, but emits N specs (one per
  inspector); also decides the panel size and quorum policy per bead.
- **Default worker** (disabled-mode fallback) — **code-puppy** with its
  built-in toolset; matches today's `/goal` implementor behavior.
- **Default judge pool** (disabled-mode fallback) — a small, fixed inspector
  panel using the implementor's model with the read-only-prompt convention
  (mirrors today's `judges.json`-with-default-judge behavior, synthesis §3.1).

The matrix: any combination of {plugin-default, BYO, disabled} × {worker side,
inspector side} is supported. "All disabled on both sides" = today's `/goal`
behavior, reimplemented inside this plugin so it can still benefit from the
non-1-bit seam (§5.1) and the durable verdict (§5.6) without doing any
composition work.

### 5.6 What the new seam carries (replacing the lossy bit)

With the plugin owning both sides, the verdict crossing inspection → close is a
real value, not a flipped flag. Minimum viable shape:

```python
@dataclass
class InspectionVerdict:
    outcome: Literal["passed", "failed", "exhausted", "cancelled"]
    inspector_results: list[InspectorResult]   # per-inspector vote + notes
    rationale: str                              # human/agent-readable
    retry_hint: str | None                      # for the 13b → worker edge
```

This directly resolves the synthesis REC-1 / REC-2 seeds: `outcome` is the
distinguishable terminal status, and `inspector_results` carries the content
that today gets discarded inside wiggum's `remediation_notes`. The `bd close`
reason becomes derived from `outcome` + `rationale`, not a hardcoded
"LLM judges passed" string.

### 5.7 Open questions (flagged, not resolved)

- **Default staffing-agent model.** Plugin-default staffing runs per bead — what
  model? A cheap one (cost) vs the implementor's model (consistency)?
- **Prompt pool scope.** User-global like UC tools? Per-project? Per-bead-graph?
  (Affects multi-tenant / multi-repo behavior.)
- **Disabled-mode inspector panel size.** Today's `judges.json` default is one
  synthetic judge. Match that, or ship a small fixed panel (e.g. 3) for
  reliability?
- **Prompt authoring UX.** TUI like `/judges`? File-drop? Distillation from
  successful runs? (The pool grows or it stagnates.)
- **Surfacing method choice.** Pick one and ship (tag match is simplest); or
  ship hybrid from day one?
- **Inspector quorum policy.** Today: hardcoded unanimity. Per-bead policy
  authored by the inspector staffing agent? Per-pool default?
- **BYO staffing-agent registration.** Reuse the existing agent registry
  (`register_agents` hook), or a dedicated `register_staffing_agent` hook to
  keep staffing semantics distinct from general agents?

### Tie to canvas (updates this revision)

The canvas now reflects this thought directly:

- **`plugin_owns_banner`** (top-left): asserts ownership of the full line and
  the elimination of the 1-bit seam.
- **`worker_staffing_agent`** + **`inspector_staffing_agent`**: inserted as
  explicit nodes between the `creation/selection` boxes and the 5-layer
  composition stacks. Both annotated with the three modes (plugin-default / BYO
  / disabled). Edges relabeled `8a delegate to` → `8b compose` (worker) and
  `10a delegate to` → `10b compose` (inspector).
- **`prompt_pool`**: feeds candidate prompts down to both prompt-slot nodes.
- **`non_llm_surfacing`**: sits above the pool, ranks/filters into it.
- **`disabled_fallback`**: hangs off `worker creation/selection` with the
  "if staffing disabled" edge; loops back to `bead-chain` so the disabled-mode
  bypass is visible as a complete alternative path.

---

## Thought 6 — Scope correction: plugin starts at bead-chain; planning is external; worker bypass is fully symmetric with inspector bypass

**Claim captured (two corrections to Thought 5):**

1. **Plugin scope starts at bead-chain.** Steps 1–5 on the canvas
   (`plan_weaver` → `plan` → `bead_master` → `bead graph`) are **external** to
   the plugin. The plugin may *supply means* to a planner (prompts, recipes,
   exported tools) but does **not** own or run planning.
2. **Worker staffing is bypassable in exactly the same way inspector staffing
   is.** Thought 5 implied a single fused "disabled mode" combining both
   defaults. Wrong shape — they are **two independent toggles**, each producing
   a usable downstream artifact when off (code-puppy as the worker; a fixed
   judge pool as the inspectors).

### 6.1 The plugin boundary in one diagram

```
EXTERNAL (not plugin-owned)              │   PLUGIN STARTS HERE
                                         │
 user ⇄ plan_weaver → plan               │   bead-chain
                ↓                        │     ↓
        bead_master → bead graph ────────→   worker creation/selection
                                         │     ↓
                                         │   [worker staffing agent | bypass→code-puppy]
                                         │     ↓
                                         │   worker → works
                                         │     ↓
                                         │   inspector creation/assignment
                                         │     ↓
                                         │   [inspector staffing agent | bypass→fixed judge pool]
                                         │     ↓
                                         │   inspector(s) → Inspection finished
                                         │     ↓
                                         │   13a passed → bd close, next ready
                                         │   13b failed → worker (retry)
```

**What the plugin owns:** every step from bead-chain pulling `bd ready` through
the structured verdict driving `bd close`. **What it doesn't:** how the bead
graph got authored. A user (or the existing `bead-planner` agent, or a
hand-edited DAG, or another tool entirely) is responsible for producing the
graph that lands in `bd` before bead-chain runs.

### 6.2 What "may supply means, does not own" cashes out as

The plugin can **export** artifacts a planner might consume, without
owning planning runtime:

- The **prompt pool** (§5.3) is readable by any planner that wants
  inspector-style critique prompts during decomposition.
- **UC-forged tools** (Thought 3) are globally discoverable; a planner
  can call them just as easily as the staffing path can.
- Any **recipes** for "what a good per-bead `AgentSpec` looks like" can be
  documented and consumed by planners that want to pre-pin
  `execution_*` metadata on beads.

None of these create a runtime dependency in the other direction — the plugin
runs the same regardless of whether the planner used any of them.

**What stays out of scope:**

- Conversation loops with the user (plan_weaver's job).
- Plan → graph translation logic (bead_master's job).
- Anything about *what* beads should exist or how they should be decomposed.

This boundary also clarifies the **DBOS question** floating on the canvas
(`87e6c2b44a4fb220`). DBOS-style durable execution applies to the plugin's
internal loop — the bead-chain → staffing → work → inspection → close cycle —
not to the planning phase. Planning is human-driven and inherently resumable
(the bead graph is the checkpoint). The recoverability question is entirely
inside the plugin's box.

### 6.3 Symmetric bypass: two toggles, four configurations

Rewriting Thought 5's mode matrix as it actually behaves — **worker side** and
**inspector side** are fully independent. Each side has three modes
(plugin-default / BYO / disabled), and disabled on each side has a
side-specific default:

| Worker side | Inspector side | Behavior |
|-------------|----------------|----------|
| plugin-default | plugin-default | Full per-bead composition both sides. The canvas's headline configuration. |
| plugin-default | disabled | Per-bead worker; fixed judge pool inspects. Useful when inspection style is stable but worker needs vary. |
| disabled | plugin-default | code-puppy works; per-bead inspector panel grades. **Lowest-risk incremental rollout** — smarter inspection without disturbing the implementor. |
| disabled | disabled | code-puppy works; fixed judge pool inspects. Closest to today's `/goal` + `judges.json` behavior, but inside the plugin so it still gets the structured verdict (§5.6). |
| BYO | * | User-supplied worker staffing agent. Inspector side configured independently. |
| * | BYO | User-supplied inspector staffing agent. Worker side configured independently. |

The earlier Thought 5 §5.5 "disabled-mode fallback" wording was misleading
because it bundled both bypasses. Reading it now: each bypass independently
produces its own default. The fused "all disabled = today's `/goal`"
configuration is just one cell in the matrix (bottom-right).

### 6.4 Bypass mechanics — the flow does not collapse, it short-circuits one box

The critical correctness property: **bypassed branches feed forward into the
normal pipeline**, they do not loop back early.

- **Worker bypass:** `worker creation/selection` → `code-puppy worker` (skipping
  the 5-layer staffing stack) → **same inspection step** as the staffed path.
  The inspection seam (§5.6) still produces a structured verdict.
- **Inspector bypass:** `inspector creation/assignment` → `fixed judge pool`
  (skipping the per-inspector composition stack) → **same Inspection finished
  step**, **same verdict shape**.

So even with both sides bypassed, the plugin still benefits from:

- Owning the seam (no shared `WiggumState.active` boolean to race on).
- Structured `InspectionVerdict` (§5.6) instead of the 1-bit collapse.
- Distinguishable `outcome` (no false-positive `bd close --reason "LLM judges
  passed"` on exhaustion — synthesis §4 fixed at the structural level).
- No external plugin coupling (no hook-ordering dance — synthesis §2.4).

These benefits come from **plugin ownership of the post-bead-chain pipeline**,
not from per-bead composition. Per-bead composition is a *capability* layered
on top; bypass turns it off without losing the seam fix. This is why "disabled
on both sides" is still strictly better than today's setup, not a regression to
it.

### 6.5 Open question superseded

Thought 5 §5.7 asked "*Disabled-mode inspector panel size — match today's
single synthetic judge, or ship a small fixed panel (e.g. 3)?*" That question
stands but is now scoped to **only the inspector-bypass default**, not to a
fused fallback. The worker-bypass default is unambiguously "code-puppy with
built-in tools" (one worker, no panel — the worker side has no panel concept).

### Tie to canvas (updates this revision)

- **`plugin_owns_banner`** rewritten: scope starts at bead-chain; planning
  external; worker AND inspector each independently bypassable; no wiggum /
  no /goal / no judges.json **inside the plugin**.
- **`disabled_fallback`** repurposed to **worker bypass only**, with edge
  retargeted: instead of looping back to `bead-chain` with
  *"completes & returns"*, it now flows forward into the `worker` node with
  *"becomes worker"* — making the symmetry with the inspector bypass explicit.
- **`inspector_bypass`** added as a peer node: branches off the
  `inspector_staffing_agent` on the *"if inspector staffing disabled"* edge,
  flows forward into `inspector(s)` with *"becomes inspector(s)"*.
- The user-added **`plugin starts here`** marker (pointing at `bead-chain`) is
  the single source of truth for the scope boundary in the canvas.
