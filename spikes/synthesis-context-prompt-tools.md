# Spike: Synthesis — Context / Prompt-Agent / Tool Availability across `/goal`, `/wiggum`, `/judges`

> **Bead:** `code_puppy_oss-0ae` · Type: spike · Priority: P2
> **Status:** research-only synthesis — consolidates the three command catalogs into a
> cross-cutting comparison and a prioritized rework roadmap. Makes **no** code changes.
> **Inputs (all closed):**
> - `/wiggum` catalog — `code_puppy_oss-68r` → [`wiggum-catalog.md`](./wiggum-catalog.md)
> - `/judges` catalog — `code_puppy_oss-80b` → [`judges-catalog.md`](./judges-catalog.md)
> - `/goal` catalog — `code_puppy_oss-fzh` → [`goal-catalog.md`](./goal-catalog.md)

This doc reads the three catalogs side by side along the **three cross-cutting lenses** the
synthesis bead tracks:

1. **Context management** — how each command sources / resets / carries message history.
2. **System prompt / agent + model assignment** — what decides agent, model, and prompt.
3. **Tool / MCP / skill availability** — what gates what each mode can call.

For each lens it separates the **shared core machinery** from **per-command divergence**,
calls out **inconsistencies, duplication, and gaps**, and ends with a **consolidated,
prioritized roadmap** that recommends how to split the work into follow-up beads
(feature / decision / chore).

---

## 0. Orientation — who is who

The three commands are not three independent features; they form **one subsystem** with a
config surface and two execution modes:

| Command | Role | Lives in |
|---------|------|----------|
| `/judges` | **Config surface** — CRUD on the judge roster (`judges.json`). No loop behavior. | `command_line/judges_menu.py` (TUI) + `plugins/wiggum/judge_config.py` (schema/persistence) |
| `/wiggum` | **Dumb loop** — re-run a prompt forever until a human stops it. No judges, no cap. | `plugins/wiggum/register_callbacks.py` (`mode="wiggum"`) |
| `/goal` | **Judge-gated loop** — re-run until enabled judges unanimously vote complete, capped by `goal_max_iterations`. Consumes `/judges`' config. | `plugins/wiggum/register_callbacks.py` (`mode="goal"`) + `judge.py` |

Key structural fact that drives everything below: **`/wiggum` and `/goal` are two modes of
one plugin sharing a single `WiggumState` singleton, one `interactive_turn_end` hook, and one
stop command.** `mode` (`"wiggum"` / `"goal"`) is the only behavioral fork. `/judges` is a
separate config surface that `/goal` reads fresh every iteration. The actual *looping* is
owned by core (`cli_runner.py`'s continuation loop); the plugin only returns **policy dicts**
saying "run this next, clear context or not."

```
                       ┌──────────────────────────────────────────┐
                       │  core: cli_runner.py continuation loop     │  ← owns EXECUTION
                       │  (clears context, sleeps delay, re-runs)   │
                       └───────────────▲───────────────┬───────────┘
            returns policy dict        │               │ runs prompt as a turn
                       ┌───────────────┴───────────────▼───────────┐
                       │  plugins/wiggum: _on_interactive_turn_end  │  ← owns POLICY
                       │  branch on state.is_goal_mode()            │
                       └──────┬─────────────────────────┬──────────┘
                       wiggum │                          │ goal
                              ▼                          ▼
                   re-run same prompt           _run_goal_judges → judges
                   (no feedback)                (parallel, judges.json roster)
```

---

## 1. Lens 1 — Context Management

### 1.1 Side-by-side

| Aspect | `/judges` | `/wiggum` | `/goal` |
|--------|-----------|-----------|---------|
| Owns a message-history loop? | **No** (config TUI only) | Yes | Yes |
| `clear_context` between iterations | n/a | **`True`, hardcoded** | **`True`, hardcoded** |
| What is **carried** across iterations | n/a | prompt (verbatim), agent instance, system prompt, `loop_count`/`mode` | same **+ judge remediation notes appended to next prompt** |
| What is **reset** each iteration | n/a | implementor message history (full), compacted-hash cache, autosave session id (rotated) | identical |
| Feedback into next iteration | n/a | **none** (error printed, not re-injected) | **judge remediation notes** appended to prompt |
| Who remembers prior loops | n/a | **nobody** — agent starts blind | **the judges** — they read the pre-clear history snapshot + re-inject notes |
| History a *judge* sees | n/a (judges don't run) | n/a | snapshot taken **before** the clear via `agent.get_message_history()`, exposed through the read-only `inspect_goal_history` tool (≤100 msgs / 12 000 chars) |

### 1.2 Shared core machinery

Both loops route through **one** code path in `cli_runner.py` when they return a continuation
dict with `clear_context: True`:

```python
new_session_id = finalize_autosave_session()   # persist snapshot + rotate autosave id
current_agent.clear_message_history()           # _message_history = []; hash cache cleared
```

- `finalize_autosave_session()` (`config.py`) **persists the prior transcript to disk** then
  rotates to a fresh autosave id — nothing is lost, it's just dropped from live context.
- `clear_message_history()` (`base_agent.py`) is the single reset primitive both modes lean on.

So the *mechanism* is 100% shared. The **divergence is purely in the policy dict** the plugin
returns: wiggum sends `{prompt}` unchanged; goal sends `{prompt + "\n\nJudge remediation
notes:\n" + notes}`. Same `clear_context: True`, same `delay: 0.5`.

### 1.3 Inconsistencies / duplication / gaps

- **G1 — `clear_context` is non-negotiable in both modes.** Neither command exposes a "carry
  context forward" option. For "iteratively refine one growing artifact" workflows this is the
  wrong default, and it's hardcoded in two places (wiggum branch + goal branch) rather than
  driven by a shared policy knob.
- **G2 — wiggum has no feedback channel; goal has one but it's half-wired.** Wiggum re-loops on
  error with zero feedback (the agent can't learn from a repeated crash because context is
  wiped). Goal *does* inject judge notes — but the `state.remediation_notes` field is **written
  but not read** for prompt construction (the next prompt is built from a local `notes`
  variable). Two representations of the same data, one dead. (`/goal` catalog §5, §7.4.)
- **G3 — the shared `WiggumState` carries a mode-specific field.** `remediation_notes` lives on
  the singleton but is dead weight for wiggum (never read/written there). A cohesion smell from
  forcing two modes onto one struct (SRP).
- **Gap — judge history window is lossy.** `inspect_goal_history` hard-truncates at 100 msgs /
  12 000 chars mid-string, so a judge can false-negative on a long transcript simply by not
  *seeing* the evidence. The loop's only "memory" is therefore lossy by design.

---

## 2. Lens 2 — System Prompt / Agent + Model Assignment

### 2.1 Side-by-side

| Aspect | `/judges` | `/wiggum` | `/goal` (implementor) | `/goal` (judges) |
|--------|-----------|-----------|------------------------|-------------------|
| Who runs the work | nobody (config) | the active agent | the active agent | N ephemeral judge agents |
| Agent reassigned between iterations? | n/a | **No** — frozen at loop start | **No** — frozen at loop start | rebuilt **every iteration, per judge** |
| Model source | model picker (for editing a judge's `model`) | agent's configured model | agent's configured model | `judge_config.model`; falls back to implementor's model for the synthetic `default` judge |
| System prompt source | n/a | agent's full prompt, rebuilt per run (incl. `load_prompt` hooks) | same | `judge_config.prompt` **or** `DEFAULT_JUDGE_PROMPT` |
| Can you `/agent`-switch mid-loop? | n/a | No — REPL held by loop | No — REPL held by loop | n/a |
| Special model handling | — | — | — | `_strip_thinking_settings` (Anthropic rejects `thinking` + `ToolOutput`); `retries=3`; `UsageLimits(request_limit=200)` |

### 2.2 Shared core machinery

- **Implementor side is identical across `/wiggum` and `/goal`:** no reassignment. The same
  `current_agent` object the CLI passes into the continuation loop runs every iteration, with
  its model and system prompt rebuilt per run from the *same* agent (so effectively stable,
  modulo `load_prompt` plugin hooks that fire on every assembly). "Frozen at loop start" is the
  rule for both.
- **`_resolve_judges(implementor_agent)` is the only agent-assignment branch.** It computes a
  `fallback_model` (`get_pydantic_agent().model.model_name` → `get_model_name()` → literal
  `"code-puppy"`), then defers to `get_enabled_judges_or_default(fallback_model)`. That fallback
  is what makes `/goal` zero-config: with no enabled judges it synthesizes one ephemeral
  `default` judge using the implementor's own model + `DEFAULT_JUDGE_PROMPT`, never persisted.

### 2.3 Inconsistencies / duplication / gaps

- **D1 — two completely different "assign an agent" idioms in one subsystem.** The implementor
  is assigned by *core* (passed in, frozen). Judges are assigned by *the plugin*, rebuilt from
  scratch every iteration × judge. There is no shared abstraction for "make me an agent with
  model M, prompt P, toolset T" — `judge_goal` hand-rolls `pydantic_ai.Agent` construction
  inline, duplicating model-loading/validation that core's agent factory already does elsewhere.
- **Perf — per-iteration judge construction is heavy** (`/goal` §7.7): `judge_goal` reloads
  plugin callbacks, reloads model config, rebuilds the agent, and re-registers tools *every
  iteration, per judge*. Redundant work scaling with `iterations × judges`.
- **Gap — system-prompt plugin hooks (`get_model_system_prompt` / `load_prompt`) only touch the
  implementor.** Judges bypass the normal prompt-assembly pipeline entirely — they use
  `judge_config.prompt or DEFAULT_JUDGE_PROMPT` directly. A plugin that overlays the implementor
  prompt has **no** way to influence judge prompts. Whether that's correct (judges *should* be
  isolated) or a gap (judges *should* honor org-wide prompt policy) is a **decision**, not an
  obvious bug.
- **Gap — the synthetic `default` judge is un-tunable.** Zero-config users always get
  `DEFAULT_JUDGE_PROMPT` on the implementor's model; there's no `/set` knob to customize the
  fallback judge without persisting one (`/judges` §7.8).
- **Dead code (consistent across catalogs):** `_run_goal_judges`' `if not judges: return
  False, "No judge agents configured.", []` can't fire, because `_resolve_judges` always returns
  ≥1 (the synthetic default). Flagged identically in both the `/judges` and `/goal` catalogs.

---

## 3. Lens 3 — Tool / MCP / Skill Availability

### 3.1 Side-by-side

| Aspect | `/judges` | `/wiggum` | `/goal` (implementor) | `/goal` (judges) |
|--------|-----------|-----------|------------------------|-------------------|
| `ask_user_question` (interactive) | available (TUI itself is the human surface) | **disabled** — autonomous loop gate | **disabled** — same gate | disabled (also via `subagent_context`) |
| Gate mechanism | n/a | `is_wiggum_active()` in `ask_user_question/handler.py` | **same** `is_wiggum_active()` — mode-agnostic (`state.active`) | `is_subagent()` (orthogonal gate) |
| MCP servers | unchanged | unchanged | unchanged | inherited then **filtered** to read-only allow-list |
| Skills | unchanged | unchanged | unchanged | unchanged (judges get whatever survives the tool filter) |
| Tool set | n/a | agent's full set minus `ask_user_question` | agent's full set minus `ask_user_question` | **intersection** of implementor `get_available_tools()` with `_READ_ONLY_TOOLS`, **plus** `inspect_goal_history` |
| Can run shell? | n/a | yes | yes | **yes** — `agent_run_shell_command` is on the read-only allow-list (runs tests to verify) |

### 3.2 Shared core machinery

- **One gate, one mechanism, both loop modes.** `ask_user_question/handler.py` imports
  `is_active as is_wiggum_active` from `plugins.wiggum.state`. Because the check is
  `state.active` (not `state.mode`), it disables interactive tools for **both** `/wiggum` and
  `/goal` from a single line. The error is a *soft* `error_response` ("make a reasonable
  decision and proceed"), not a raise, so the autonomous agent recovers and keeps looping.
- The gate also blocks sub-agents (`is_subagent()`) — an orthogonal concern, but it means
  `/goal` judges hit *two* independent interactive-tool gates.

### 3.3 Inconsistencies / duplication / gaps

- **D2 — judge tool-gating bypasses the `register_agent_tools` hook entirely.** Core *has* a
  first-class mechanism for "decide which tools an agent gets": the `register_agent_tools` hook
  (used by `puppy_kennel` to advertise its tools per agent; merged in `tools/__init__.py` via
  `on_register_agent_tools(agent_name)`). The judge subsystem **does not use it** — it instead
  hand-rolls an `intersection(get_available_tools(), _READ_ONLY_TOOLS)` filter inline in
  `judge.py`. So there are now **two parallel philosophies** for shaping an agent's toolset:
  the hook (additive, plugin-driven) and the judge intersection (subtractive, hardcoded set).
  This is the single biggest "duplicated logic / missed-abstraction" finding of the synthesis.
- **G4 — "read-only" judges are not actually sandboxed.** `agent_run_shell_command` is on
  `_READ_ONLY_TOOLS`, so a judge can run *arbitrary* shell (`rm`, `git commit`, network). The
  read-only guarantee is **prompt-deep, not sandbox-deep** — enforced only by
  `DEFAULT_JUDGE_PROMPT` text. (Both `/goal` §7.1 and the persisted memory
  `goal-judges-shell-not-sandboxed` flag this.) This is a security/trust gap, but "tighten it"
  is a **decision** (the shell access is *intentional* so judges can run tests).
- **Stale-catalog reconciliation — the `ask_user_question` gate copy.** The `/wiggum` catalog
  (§5.4) flags the error message as a bug ("says `/wiggum` during `/goal` too"). The `/goal`
  catalog (§6.3) and the live code disagree: `handler.py` now imports `get_state` and derives
  the mode name from `state.mode` (fix `66663fc`, corroborated by memories
  `wiggum-gate-mode-aware-copy` / `wiggum-goal-shared-gate-mode`). **Verified in-tree: the copy
  is mode-aware today.** The wiggum catalog is simply older than the fix. → *No bead needed;
  noted so the roadmap doesn't resurrect a closed issue.*
- **Gap — no per-mode MCP/skill policy.** Neither loop touches MCP or skills; judges inherit
  then tool-filter. There's no way to, e.g., disable an expensive MCP server during an
  autonomous loop, or grant a judge a specific verification-only MCP. Whether that's needed is
  speculative (YAGNI) — flag, don't build.

---

## 4. Shared core vs per-command divergence — the consolidated picture

| Layer | Shared core machinery | Per-command divergence |
|-------|------------------------|-------------------------|
| **Loop execution** | `cli_runner.py` continuation loop; the "returned string becomes a prompt" activation trick; `clear_context` → `finalize_autosave_session` + `clear_message_history` | wiggum: re-run verbatim, forever. goal: judges + `goal_max_iterations` + notes. judges: no loop. |
| **State** | one `WiggumState` singleton; `start/stop/is_active/increment` | `mode` fork; `loop_count` is cosmetic in wiggum, a **gate** in goal; `remediation_notes` used by goal only |
| **Hooks** | one `interactive_turn_end` + one `interactive_turn_cancel`; one stop command (`/wiggum_stop`≡`/goal_stop`) | branch on `state.is_goal_mode()` |
| **Context** | `clear_context: True` + the two reset primitives | goal appends judge notes; goal snapshots history pre-clear for judges |
| **Agent/model** | implementor frozen-at-loop-start (assigned by core) | goal additionally builds N ephemeral judges (assigned by plugin, per-iteration) |
| **Tools** | one `is_wiggum_active()` interactive-tool gate (covers both modes) | judges get a subtractive read-only intersection + `inspect_goal_history`; everyone else unchanged |
| **Config** | `judges.json` single source of truth (no cache); `/judges` writes, `/goal` reads fresh | `goal_max_iterations` via `/set` (goal only) |

**One-line summary:** the *plumbing* (loop, state, context-clear, gate) is genuinely shared and
clean; the *divergence* is concentrated in (a) feedback policy, (b) the second tier of
ephemeral judge agents, and (c) two parallel tool-shaping idioms.

---

## 5. Cross-cutting findings ranked by "fix-once, help-everywhere" leverage

These are the issues that touch **more than one** command or lens — the highest-leverage
targets for the rework decomposition.

1. **Two tool-shaping philosophies (D2).** Hook-based (`register_agent_tools`, additive) vs
   hardcoded intersection (`_READ_ONLY_TOOLS`, subtractive). Reconciling these would let judge
   tool policy be plugin-driven and testable like everything else. *(architecture)*
2. **Two agent-construction idioms (D1).** Core-assigned implementor vs hand-rolled
   `pydantic_ai.Agent` per judge. A shared "build agent (model, prompt, tools)" factory would
   kill the per-iteration rebuild cost *and* the inline model-validation duplication. *(arch)*
3. **`clear_context` is hardcoded in both loop branches (G1)** with no shared policy knob, and
   the feedback channel is wiggum-absent / goal-half-wired (G2). A single "continuation policy"
   object (prompt transform + clear flag + feedback source) would unify wiggum & goal and make
   `remediation_notes` the real source of truth. *(architecture, ties to the `/wiggum` §6.6
   "policy object" suggestion)*
4. **Mode-specific fields on a shared singleton (G3).** `remediation_notes` is dead weight for
   wiggum; `loop_count` means different things per mode. SRP cleanup. *(chore)*
5. **"Read-only" judges aren't sandboxed (G4).** Cross-cuts security + tool lens. Needs a
   **decision** before any code. *(decision)*

---

## 6. Consolidated improvement roadmap (seeds for follow-up beads)

Grouped by **type** (decision / feature / chore) with a suggested priority and the catalog
findings each rolls up. **Nothing here is committed work** — this is the decomposition the
synthesis bead exists to seed. Recommend pinging the Bead Master to wire the graph.

### 6.1 Decisions (resolve these first — they gate the features)

| # | Decision bead | Rolls up | Why a decision |
|---|---------------|----------|----------------|
| DEC-1 | **Judge tool-policy model: adopt `register_agent_tools` hook vs keep hardcoded allow-list** | D2, lens 3 | Picks the architecture for every tool-shaping change below. Blocks FEAT-2. |
| DEC-2 | **Should "read-only" judges be sandboxed?** (drop shell / command allow-list / sandbox flag / leave as-is) | G4, `/goal` §8.4 | Security vs the *intentional* "judge runs the test suite" capability. Trade-off, not a bug. |
| DEC-3 | **Completion policy: keep strict unanimity vs quorum/k-of-n/weights** | `/goal` §8.1, memory `goal-completion-rule-and-cap` | Product behavior change; one picky judge can pin the loop. |
| DEC-4 | **Should judge prompts honor `get_model_system_prompt`/`load_prompt` overlays, or stay isolated?** | §2.3 gap | Determines whether judges bypass the prompt pipeline by design or by omission. |

### 6.2 Features

| # | Feature bead | Rolls up | Priority | Size |
|---|--------------|----------|----------|------|
| FEAT-1 | **Unify wiggum/goal under a continuation-policy object** (prompt transform + `clear_context` + feedback source); make `remediation_notes` the single source of truth | G1, G2, G3, `/wiggum` §6.6, `/goal` §8.5 | P2 | M |
| FEAT-2 | **Route judge tools through the chosen tool-policy mechanism** (per DEC-1) | D2 | P2 | M (blocked by DEC-1) |
| FEAT-3 | **Shared agent factory for judges** — build once per `/goal` run, reuse across iterations; reuse core model-load/validation | D1, `/goal` §8.7 | P2 | S–M |
| FEAT-4 | **Loop safety valves for `/wiggum`** — opt-in `wiggum_max_iterations` + error backoff / circuit breaker | `/wiggum` §6.1, §6.2 | P2 | S–M |
| FEAT-5 | **Optional context carry-over** (`--keep-context`) for both loops, enabled by FEAT-1's policy object | G1, `/wiggum` §6.3 | P3 | S |
| FEAT-6 | **Early-bail on systemic judge abstain** + abstain summary banner | `/goal` §8.2, §8.8 | P2 | S |
| FEAT-7 | **Token/cost ceiling across a `/goal` run** (alongside iteration count) | `/goal` §8.3 | P3 | M |
| FEAT-8 | **Paginated / summarizing `inspect_goal_history`** to stop false-negatives on long transcripts | `/goal` §6 gap, §8.6 | P3 | S–M |
| FEAT-9 | **Configurable completion policy implementation** (once DEC-3 lands) | DEC-3 | P3 | M |
| FEAT-10 | **`/judges` UX:** delete confirmation, model-validity check at edit time + mis-pointed-judge flag, surface skipped entries, templates/clone, export/import/reset | `/judges` §8 (multiple) | P3 | split into ≥3 small beads |

### 6.3 Chores

| # | Chore bead | Rolls up | Size |
|---|------------|----------|------|
| CHORE-1 | **Remove dead `if not judges:` branch** in `_run_goal_judges` (unreachable; flagged by both `/judges` and `/goal` catalogs) | §2.3, `/goal` §7.9 | XS |
| CHORE-2 | **De-mode the shared singleton** — move `remediation_notes` off `WiggumState` (or fold into FEAT-1's policy object) | G3 | S |
| CHORE-3 | **Centralize the Ctrl+C / cancellation contract** (today double-caught in `_run_goal_judges` and `_on_interactive_turn_end`) | `/goal` §8.9 | S |
| CHORE-4 | **Deprecate the `command_line/wiggum_state.py` back-compat shim** once tests import from `plugins.wiggum.state` | `/wiggum` §6.8 | S (test-touching) |
| CHORE-5 | **Make the wiggum `Loop #N` counter meaningful or stop showing it as if it gates** (depends on FEAT-4) | `/wiggum` §6.7 | XS |
| CHORE-6 | **Refresh the `/wiggum` catalog's stale `ask_user_question` copy note** — the mode-aware fix (`66663fc`) already landed; the catalog predates it | §3.3 reconciliation | XS (doc-only) |

### 6.4 Suggested sequencing

```
DEC-1 ─┬─► FEAT-2
       │
DEC-3 ─┴─► FEAT-9
DEC-2 ────► (informs FEAT-2 scope)
DEC-4 ────► (informs whether judge prompt pipeline changes)

FEAT-1 ─┬─► FEAT-5            (carry-over needs the policy object)
        ├─► CHORE-2           (remediation_notes folds in here)
        └─► CHORE-5

FEAT-3, FEAT-4, FEAT-6, CHORE-1, CHORE-3, CHORE-4, CHORE-6  — independent, parallelizable
```

Land the **decisions** first (they unblock the two architectural features), then the
**continuation-policy unification (FEAT-1)** as the spine that several smaller items hang off,
and pick up the independent chores opportunistically.

---

## 7. TL;DR for the next reader

- **The plumbing is shared and clean.** One state singleton, one turn-end hook, one
  context-clear path, one interactive-tool gate covering both loop modes. Don't rebuild it.
- **The divergence lives in three places:** feedback policy (wiggum none / goal notes), the
  second tier of ephemeral judge agents, and **two parallel tool-shaping idioms**
  (`register_agent_tools` hook vs hardcoded `_READ_ONLY_TOOLS` intersection).
- **Highest-leverage rework:** reconcile the two tool-shaping idioms (DEC-1 → FEAT-2) and the
  two agent-construction idioms (FEAT-3), and unify wiggum/goal under one continuation-policy
  object (FEAT-1) that makes `remediation_notes` the single source of truth.
- **Two things need a human decision, not a patch:** judge shell sandboxing (G4/DEC-2) and the
  completion policy (DEC-3). They're intentional trade-offs, not bugs.
- **One stale finding reconciled:** the `ask_user_question` "/wiggum during /goal" copy bug is
  **already fixed** (`66663fc`); only the `/wiggum` catalog predates it. No new bead — just a
  doc refresh (CHORE-6).
