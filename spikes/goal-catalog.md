# Spike: Cataloging the `/goal` Command & Judge-Driven Retry Loop

> **Bead:** `code_puppy_oss-fzh` · Type: spike · Priority: P2
> **Status:** research-only — documents `/goal` *as it exists today*, before any redesign.
> **Companion catalogs:** `/wiggum` (`code_puppy_oss-68r`), `/judges` (`code_puppy_oss-80b`).
> Synthesis bead `code_puppy_oss-0ae` consumes all three.

`/goal` (aliases `/kibble`, `/chow`) is the **judge-gated** sibling of `/wiggum`.
Where `/wiggum` re-runs a prompt forever until a human stops it, `/goal` re-runs
the prompt until **every enabled LLM judge votes "complete."** After each agent
turn, all enabled judges fan out in **parallel**, each independently inspects the
implementor's latest response (and, optionally, its read-only message history),
and returns a structured verdict. The goal completes only when every
non-abstaining judge passes; otherwise the judges' remediation notes are injected
into the next prompt and the loop runs again — up to `goal_max_iterations`.

This is the **`/goal`-side** of the wiggum/goal/judges catalog trilogy. It covers
the end-to-end flow, the entry points, the parallel judge fan-out + abstain
logic + completion rule, the iteration cap and remediation-notes feedback loop,
the read-only `inspect_goal_history` tool, banner/display serialization, Ctrl+C
handling, the `_resolve_judges` fallback, the three cross-cutting lenses (context
/ prompt-agent / tool availability), and the limitations worth filing follow-up
beads against.

---

## 1. What `/goal` does, end to end

```
/goal make tests pass for the auth flow
```

1. `handle_goal_command` (in `register_callbacks.py`) parses the prompt out of
   the command string via `_extract_prompt`.
2. It flips global state on: `state.start(prompt, mode="goal")` — the **same**
   `WiggumState` singleton `/wiggum` uses, just with `mode="goal"`.
3. It prints the activation banner (` GOAL MODE ACTIVATED!`), echoes the goal,
   the iteration cap, and a summary of the configured judges
   (`_emit_configured_judges_summary`).
4. It returns the **prompt string**. The CLI runner executes that string as a
   normal agent turn — so *activation and the first iteration are the same
   action* (identical trick to `/wiggum`; see the `/wiggum` catalog §2).
5. When that turn finishes (success **or** error), the `interactive_turn_end`
   hook fires. `_on_interactive_turn_end` sees `state.is_goal_mode()` is true and
   calls `_run_goal_judges(...)`.
6. `_run_goal_judges` resolves the judge roster (`_resolve_judges`), snapshots
   the implementor's message history, and fans all enabled judges out in
   **parallel** via `asyncio.gather(_run_single_judge(...) for judge in judges)`.
   Each `_run_single_judge` calls `judge_goal` (in `judge.py`).
7. Each judge returns a `GoalJudgement(complete / notes / abstained)`. Abstaining
   judges are excluded from the tally. **`all_complete = all(v.complete for v in
   non-abstaining verdicts)`.**
8. Back in the hook:
   - **Complete** → print ` GOAL COMPLETE!`, `state.stop()`, return `None` (loop
     ends; control returns to the REPL).
   - **Hit `goal_max_iterations`** → print ` GOAL STOPPED`, `state.stop()`,
     return `None`.
   - **Incomplete & under the cap** → stash the formatted remediation notes on
     `state.remediation_notes`, print ` GOAL INCOMPLETE — Retrying!`, and return
     a **continuation request dict** that re-runs the prompt *with the judge
     notes appended*, `clear_context: True`.
9. The CLI runner's continuation loop clears context, waits `delay` seconds, and
   re-runs the new prompt. Back to step 5.

### Aliases & the stop command

| Command | Role |
|---------|------|
| `/goal <prompt>` | canonical |
| `/kibble <prompt>` | puppy-themed alias (same handler) |
| `/chow <prompt>` | puppy-themed alias (same handler) |
| `/goal_stop` | stop the loop — alias of `/wiggum_stop` (also `stopwiggum`, `ws`) |

`/kibble` and `/chow` are registered as `aliases=["kibble", "chow"]` on the
`goal` command — identical behavior, just more on-brand. `/goal_stop` is an alias
of `handle_wiggum_stop_command`; because both modes share one `WiggumState`
singleton, **either stop command stops either mode.**

---

## 2. Entry points & code paths

| Concern | File · Symbol |
|---------|---------------|
| Start command | `code_puppy/plugins/wiggum/register_callbacks.py` · `handle_goal_command` (decorated `@register_command(name="goal", aliases=["kibble","chow"], …)`) |
| Iteration-cap reader | same file · `_get_goal_max_iterations` |
| Judge summary banner | same file · `_emit_configured_judges_summary` |
| Banner/display helpers | same file · `_display_banner_message`, `_display_llm_judge` |
| Judge roster resolution | same file · `_resolve_judges` |
| Remediation-notes formatter | same file · `_format_remediation_block` |
| Single-judge wrapper | same file · `_run_single_judge` |
| Parallel fan-out orchestrator | same file · `_run_goal_judges` |
| Loop driver (goal branch) | same file · `_on_interactive_turn_end` |
| Cancel handler | same file · `_on_interactive_turn_cancel` |
| Hook registration | same file, module scope: `register_callback("interactive_turn_end"/"interactive_turn_cancel", …)` |
| Single-judge execution | `code_puppy/plugins/wiggum/judge.py` · `judge_goal`, `GoalJudgement`, `GoalJudgeOutput` |
| Read-only history tool | same file · `_register_goal_history_tool` (`inspect_goal_history`), `_format_history_window` |
| Thinking-settings scrub | same file · `_strip_thinking_settings` |
| Judge user prompt builder | same file · `_judge_user_prompt` |
| Judge config + fallback | `code_puppy/plugins/wiggum/judge_config.py` · `get_enabled_judges_or_default`, `DEFAULT_JUDGE_PROMPT`, `JudgeConfig` |
| Shared state | `code_puppy/plugins/wiggum/state.py` · `WiggumState`, `is_goal_mode`, `increment`, `remediation_notes` |
| Iteration-cap config knob | `code_puppy/config.py` · `get_value("goal_max_iterations")` (set via `/set`) |
| Hook dispatch | `code_puppy/callbacks.py` · `on_interactive_turn_end`, `on_interactive_turn_cancel` |
| Continuation loop | `code_puppy/cli_runner.py` (~lines 949–1035) |
| Command dispatch | `code_puppy/command_line/command_handler.py` · `handle_command` (returns the prompt string) |

### Data flow (high level)

```
/goal <prompt> ──► handle_goal_command ──► state.start(mode="goal") ──► returns prompt
                                                                          │
                                          (CLI runner runs it as a turn)  ▼
                                                              ┌── agent turn completes
                                                              │
   on_interactive_turn_end ──► _on_interactive_turn_end ──► state.is_goal_mode()? yes
                                                              │
                                                              ▼
                                              _run_goal_judges(agent, goal, result, error)
                                                              │
                              _resolve_judges ──► get_enabled_judges_or_default(fallback_model)
                                                              │
                              asyncio.gather( _run_single_judge ──► judge_goal(...) )   (PARALLEL)
                                                              │
                              verdicts: list[GoalJudgement]   │
                                                              ▼
                              voting = [v for v in verdicts if not v.abstained]
                              all_complete = all(v.complete for v in voting)
                                                              │
                  ┌───────────────────────────────────────────┼─────────────────────────────┐
                  ▼                                            ▼                             ▼
            complete=True                          loop_num >= max_iters            incomplete & under cap
             GOAL COMPLETE                         GOAL STOPPED                   retry: return dict
            state.stop()                            state.stop()                    {prompt+notes, clear_context}
```

---

## 3. Judge execution: `judge_goal` & `GoalJudgement`

### `GoalJudgeOutput` (the model's structured output)

```python
class GoalJudgeOutput(BaseModel):
    complete: bool   # True only when the goal is verifiably complete
    notes: str       # rationale + remediation notes if incomplete
```

The judge agent is constructed with `output_type=ToolOutput(GoalJudgeOutput,
name="goal_judgement", …)` so the model is *forced* to emit a structured verdict
via a tool call rather than freeform prose.

### `GoalJudgement` (the normalized verdict surfaced to `/goal`)

```python
@dataclass(frozen=True)
class GoalJudgement:
    judge_name: str
    complete: bool
    notes: str
    raw_response: str
    abstained: bool = False   # couldn't render a verdict (infra problem)
```

`complete=True` **is** the "no remediation needed" signal — notes alongside a
pass are purely informational and do not block completion.

### `judge_goal(...)` — one fresh, read-only judge per call

For each judge, `judge_goal`:

1. **Re-loads plugin callbacks** (`plugins.load_plugin_callbacks()`) so the judge
   agent sees the same registered tools the implementor would.
2. **Validates the model** against `ModelFactory.load_config()`. If the judge's
   `model` isn't present → returns `abstained=True` with a "model not in config"
   note (misconfiguration is infra, not a real verdict).
3. Builds the model (`ModelFactory.get_model`), picks `judge_config.prompt or
   DEFAULT_JUDGE_PROMPT` as instructions, and assembles the user prompt via
   `_judge_user_prompt(goal, response, error)` (which embeds the goal, the latest
   agent response, any run error, and a hint to use `inspect_goal_history`).
4. **Strips thinking settings** (`_strip_thinking_settings`) — Anthropic models
   reject `thinking` + `ToolOutput` simultaneously; other providers don't mind
   the missing keys. Scrubs `anthropic_thinking`, `thinking`, `thinking_enabled`,
   `thinking_level`, and `extra_body.output_config`.
5. Constructs a **fresh `pydantic_ai.Agent`** with `retries=3` and the structured
   `ToolOutput`.
6. Grants the judge the implementor's **read-only tools** (intersection of the
   implementor's `get_available_tools()` with the `_READ_ONLY_TOOLS` allow-list)
   plus the `inspect_goal_history` tool.
7. Runs the judge inside a `subagent_context(f"judge:{name}")` so its tool
   banners and chatter are suppressed (the rich renderer / tool display check
   `is_subagent()` and skip rendering), with a
   `UsageLimits(request_limit=GOAL_JUDGE_REQUEST_LIMIT)` cap (**200**) to stop a
   runaway judge from burning the whole loop's token budget.
8. **Cancellation propagates** (`CancelledError` / `KeyboardInterrupt` re-raise);
   **any other exception** (HTTP 4xx/5xx, auth, network, vendor SDK bug) →
   returns `abstained=True` with an "endpoint error" note.
9. On success, normalizes the structured output into a `GoalJudgement`.

### `_READ_ONLY_TOOLS` allow-list

```python
_READ_ONLY_TOOLS = {
    "list_files", "read_file", "grep", "agent_run_shell_command",
    "load_image_for_analysis", "list_agents", "invoke_agent",
}
```

Note that `agent_run_shell_command` is on the allow-list — a judge **can run shell
commands** (e.g. run the test suite) to *verify* completion, even though it's
forbidden from editing files. The "read-only" framing is enforced by the judge's
prompt and tool set, not by sandboxing the shell. (See limitations §7.)

### The `inspect_goal_history` tool

`_register_goal_history_tool` registers a single read-only tool on each judge
agent:

```python
async def inspect_goal_history(context, query: str | None = None, limit: int = 20) -> str
```

It returns a formatted window over the implementor's captured message history
(`_format_history_window`): optional case-insensitive substring `query`, the last
`limit` matching messages (capped at 100), and a `max_chars=12000` budget so a
huge transcript can't blow the judge's context. Each message is rendered via
`stringify_part`. This is what lets a judge dig past the latest response —
"did the agent actually edit the file, or just claim it did?" — without ever
mutating anything.

---

## 4. Parallel fan-out, abstain logic & completion rule

`_run_goal_judges` is the orchestrator:

```python
judges = _resolve_judges(agent)
if not judges:
    return False, "No judge agents configured.", []   # effectively dead-code; fallback always returns ≥1

history = list(agent.get_message_history())
response_text = _response_text(result)

# announce the roster, then fan out
verdicts = await asyncio.gather(
    *(_run_single_judge(judge, implementor_agent=agent, goal=goal,
                        response=response_text, error=error, history=history)
      for judge in judges)
)
```

### Why a `_run_single_judge` wrapper?

`_run_single_judge` does **no printing** — it just awaits `judge_goal` and, on any
unexpected escaping exception, logs it and returns an **abstain** verdict so one
buggy judge (in *our* plumbing, not the model) can never block `/goal`.
`CancelledError`/`KeyboardInterrupt` are re-raised so cancellation still
propagates.

The reason display is deliberately kept out of the per-judge path: many judges
run concurrently under `asyncio.gather`, and the Rich console uses `\r`
line-clearing tricks. Concurrent writes would **interleave and clobber** each
other. So all banners are serialized at the orchestrator level — `_run_goal_judges`
prints the per-judge verdicts in a plain `for` loop **after** `gather` resolves.

### Abstain logic & the completion rule

```python
voting = [v for v in verdicts if not v.abstained]
if not voting:
    all_complete = False           # everyone abstained → can't decide → not complete
    _display_llm_judge("  All judges abstained — cannot determine completion.")
else:
    all_complete = all(v.complete for v in voting)
```

- **Abstaining judges get no vote.** An abstain means "I couldn't render a
  verdict for an infrastructure reason" (model not in config, endpoint error,
  auth, timeout, plumbing bug) — neither pass nor fail.
- **Completion = strict unanimity of the non-abstaining judges.** Every voting
  judge must report `complete=True`.
- **All-abstain ⇒ incomplete** (with a warning). If the entire roster couldn't
  decide, `/goal` does *not* claim success — it retries (or hits the cap).
- The default single-judge fallback means the common case is "1 judge must pass."

### Verdict display glyphs

| State | Glyph |
|-------|-------|
| non-abstain, complete | ` PASS` |
| non-abstain, incomplete | ` FAIL` |
| abstained | `  ABSTAIN` |

---

## 5. Iteration cap + remediation-notes feedback loop

### The cap (`goal_max_iterations`)

```python
GOAL_MAX_ITERATIONS_DEFAULT = 10
GOAL_MAX_ITERATIONS_FLOOR    = 1
GOAL_MAX_ITERATIONS_CEILING  = 1000
```

`_get_goal_max_iterations()` reads `get_value("goal_max_iterations")`, coerces to
`int` (falling back to **10** on `ValueError`/`TypeError`/empty), and **clamps to
`[1, 1000]`**. Users override per-session with `/set goal_max_iterations=<int>`.

The cap is checked in the hook:

```python
loop_num = state.increment()        # shared WiggumState counter
...
if loop_num >= max_iters:
    " GOAL STOPPED — Hit max iterations (N)."
    state.stop()
    return None
```

Unlike `/wiggum` (where `loop_count` is cosmetic), in `/goal` the **same counter
actually gates the loop**. `state.increment()` runs once per turn-end *before*
the completion check is consumed, so iteration N's judges run, and only if they
fail does the cap comparison fire.

### Remediation-notes feedback loop

`_format_remediation_block(verdicts)` builds a human-readable block from **all**
verdicts (including abstainers, labeled ` ABSTAIN`):

```
[judy]  PASS
  looks good, tests pass

[joe-brown]  FAIL
  auth_test.py::test_login still failing — fixture missing
```

On an incomplete-but-under-cap iteration the hook stashes that block on
`state.remediation_notes` (for state visibility) and returns:

```python
return {
    "prompt": f"{goal_prompt}\n\nJudge remediation notes:\n{notes}",
    "clear_context": True,
    "delay": 0.5,
    "reason": "goal",
}
```

So the **next** iteration's prompt is `original goal + judge notes`. This is the
key difference from `/wiggum`: even though context is wiped between iterations,
the agent still gets *targeted feedback* about what the judges found lacking,
re-injected into the fresh prompt. The agent starts blind every loop but with a
to-do list.

> **Subtlety:** `state.remediation_notes` is written but the **prompt is built
> from the local `notes` variable**, not re-read from state. The state field is
> effectively a breadcrumb / debugging aid, not the source of truth for the next
> prompt. (Minor cohesion smell — see §7.)

---

## 6. Cross-cutting concerns (the three synthesis lenses)

These mirror the lenses the synthesis bead (`code_puppy_oss-0ae`) tracks across
all three commands.

### (1) Context management — `clear_context=True` every iteration, plus notes

Every `/goal` continuation dict sets `clear_context: True`, identical mechanics
to `/wiggum` (see that catalog §4). When the CLI runner sees it:

```python
new_session_id = finalize_autosave_session()   # persist + rotate autosave id
current_agent.clear_message_history()           # _message_history = []; hash cache cleared
```

| Carried across iterations | Reset each iteration |
|---------------------------|----------------------|
| The **goal prompt** (re-sent) | Implementor's **message history** (full reset) |
| **Judge remediation notes** appended to the next prompt | Compacted-message hash cache |
| The **agent instance** (same object) | Autosave session id (rotated; prior transcript saved) |
| Global goal state (`loop_count`, `mode`, `remediation_notes`) | — |

Net: each `/goal` iteration is a **fresh-context run of `goal + judge notes`** on
the same agent. The implementor has no memory of prior loops, but the *judges*
bridge that gap — they read the saved/in-memory history (snapshotted *before*
the clear, then made available via `inspect_goal_history`) and re-inject findings
as notes. The judges are the loop's "memory."

> **Snapshot timing:** `_run_goal_judges` calls `agent.get_message_history()`
> **before** any context clear (the clear happens later, in the CLI runner, when
> it consumes the returned dict). So judges always see the *just-completed* turn's
> full history, even though the next iteration starts clean.

### (2) System prompt / agent & model assignment

**Implementor side:** no reassignment — the same `current_agent` runs every
iteration with its own model and system prompt (rebuilt per run via the usual
prompt assembly, including any `load_prompt` plugin hooks). Identical to
`/wiggum`. You can't `/agent`-switch mid-loop because the REPL is held by the
loop.

**Judge side:** completely **separate** agents. Each judge is a *fresh*
`pydantic_ai.Agent` per iteration (`judge_goal` builds a new one every call) with:
- its **own model** (`judge_config.model`, or the implementor's model for the
  synthetic `default` judge — see `_resolve_judges`),
- its **own instructions** (`judge_config.prompt` or `DEFAULT_JUDGE_PROMPT`),
- thinking settings scrubbed (`_strip_thinking_settings`) to coexist with
  `ToolOutput`,
- a request-limit cap (200).

So `/goal` runs **two tiers of agents**: one implementor (stable across loops)
and N ephemeral judges (rebuilt every iteration). Judges never share the
implementor's conversation object — they only get a read-only history snapshot.

### (3) Tool / MCP / skill availability

**Implementor side:** the full tool/MCP/skill set the agent normally has — except
`ask_user_question` is **disabled** during the loop. The gate in
`code_puppy/tools/ask_user_question/handler.py` checks `is_wiggum_active()`, which
is `state.active` and therefore **mode-agnostic — true for `/goal` too.** An
autonomous loop can't block on a human, so the tool returns a soft error telling
the agent to make a reasonable decision and proceed. (The error copy is now
mode-aware — recent fix `66663fc` derives the mode name from `state.mode` rather
than hardcoding "/wiggum".)

**Judge side:** judges get a **restricted, read-only** tool set — the
intersection of the implementor's available tools with `_READ_ONLY_TOOLS`, plus
`inspect_goal_history`. Crucially this **includes `agent_run_shell_command`**, so
judges can run tests/builds to verify, but excludes any file-mutating tool. The
`DEFAULT_JUDGE_PROMPT` reinforces "Never modify files" and "Never ask the user
questions." Judges run under `subagent_context`, so they're also subject to the
sub-agent interactive-tool gate independent of loop mode.

---

## 7. Current limitations & edge cases

1. **"Read-only" judges can run arbitrary shell commands.** `agent_run_shell_command`
   is on the `_READ_ONLY_TOOLS` allow-list. A judge is told (by prompt) not to
   mutate state, but nothing *enforces* it — a judge could `rm`, `git commit`, or
   hit the network. The read-only guarantee is prompt-deep, not sandbox-deep.
2. **Strict unanimity, no quorum.** Completion requires *every* non-abstaining
   judge to pass. There's no "2 of 3" / weighted / majority policy. One picky
   judge can pin the loop to the iteration cap.
3. **All-abstain looks like a normal retry.** If every judge abstains (e.g. a
   bad model config across the board, or a provider outage), `/goal` prints a
   warning but otherwise treats it as "incomplete, retry" and burns iterations
   re-running the implementor pointlessly until the cap. There's no early "all
   judges are broken, bail" exit.
4. **`remediation_notes` on state is written-but-not-read for the prompt.** The
   next prompt is built from a local variable; the state field is a breadcrumb.
   Two sources, one of which is dead for the actual feedback path — a small
   cohesion smell on the shared `WiggumState`.
5. **No backoff / cost ceiling.** Each iteration runs the implementor *plus* N
   judges (each judge itself capped at 200 requests). A goal that oscillates near
   completion can be expensive: `iterations × (implementor + N judges)`. The only
   guard is `goal_max_iterations` (count), not a token/$ budget.
6. **Judge history window is lossy.** `inspect_goal_history` caps at 100 messages
   / 12 000 chars and truncates the last block mid-string. On a long transcript a
   judge may simply not *see* the evidence it needs and (correctly, per its
   strict prompt) mark incomplete — a false-negative driven by truncation.
7. **Per-iteration judge construction is heavy.** `judge_goal` rebuilds the
   agent, reloads model config, and re-registers tools *every* iteration *per
   judge*. Fine for small rosters, but redundant work that scales with
   iterations × judges.
8. **Cancellation is best-effort across two layers.** `Ctrl+C` is caught in both
   `_run_goal_judges` (around the gather) and again in `_on_interactive_turn_end`
   (belt-and-suspenders), each calling `state.stop()`. It's robust, but the
   double-catch is a sign the cancellation contract is implicit rather than
   centralized.
9. **`if not judges:` is effectively dead code.** `_resolve_judges` always
   returns at least the synthetic `default` judge (via
   `get_enabled_judges_or_default`), so the "No judge agents configured" branch
   in `_run_goal_judges` can't actually fire in normal operation.
10. **No visibility into *why* a judge abstained at a glance.** Abstain reasons
    live in `notes` and are shown, but there's no aggregate "2 judges couldn't
    run — check your model config" summary; the user must read each line.

---

## 8. Concrete improvement opportunities (seeds for follow-up beads)

> These are catalog observations, **not** committed work. File as separate beads.

1. **Configurable completion policy (quorum / weights).** Replace strict
   unanimity with a pluggable rule — `all`, `majority`, `k-of-n`, or per-judge
   weights — so one strict judge can't pin the loop. *(medium)*
2. **Early bail on systemic abstain.** If *every* judge abstains for an
   infrastructure reason on a given iteration (or M iterations running),
   short-circuit with a clear "judges are misconfigured/unreachable" message
   instead of silently burning the cap. *(small)*
3. **Token / cost ceiling for the loop.** Add a budget cap (tokens or $) across
   the whole `/goal` run alongside `goal_max_iterations`, since each iteration is
   implementor + N judges. *(medium)*
4. **Sandbox or further restrict the judge shell.** Either drop
   `agent_run_shell_command` from the judge allow-list, gate it behind a
   read-only/sandbox flag, or run judges with a command allow-list, so
   "read-only" is enforced, not just requested. *(medium)*
5. **Make remediation-notes the single source of truth.** Build the next prompt
   *from* `state.remediation_notes` (or drop the field entirely) so there aren't
   two parallel representations of the same data. *(trivial)*
6. **Smarter / paginated history window for judges.** Let `inspect_goal_history`
   page or summarize instead of hard-truncating, so judges don't false-negative
   on long transcripts. *(small–medium)*
7. **Cache / reuse judge agents across iterations.** Build each judge agent once
   per `/goal` run instead of per iteration, refreshing only the per-turn inputs.
   *(small)*
8. **Surface an abstain summary.** Aggregate abstaining judges into a single
   actionable banner ("N judges couldn't run — check models") rather than
   per-line notes only. *(trivial)*
9. **Centralize the cancellation contract.** Collapse the double Ctrl+C catch
   into one well-defined cancellation path so the "stop the loop cleanly"
   guarantee is explicit. *(small)*
10. **Stream judge progress.** Today the user sees nothing until all judges
    resolve (display is serialized post-`gather`). A progress indicator
    ("3/4 judges reported…") would help on slow/large rosters without
    re-introducing the interleaving bug. *(small)*

---

## 9. Cross-references for the synthesis bead (`code_puppy_oss-0ae`)

- `/goal` and `/wiggum` are **two modes of one plugin** sharing one
  `WiggumState`, one `interactive_turn_end` hook, and one stop command. `mode` is
  the only behavioral fork. See the `/wiggum` catalog (`code_puppy_oss-68r`) §4
  for the shared-machinery deep dive.
- `/goal` is the **consumer** of the judges configured by `/judges`. The config
  schema, persistence, default-judge fallback, and TUI live in the `/judges`
  catalog (`code_puppy_oss-80b`). `judges.json` is the single source of truth;
  `/goal` reads it fresh (via `get_enabled_judges_or_default`) every iteration.
- **Three lenses summary for `/goal`:**
  - *Context:* `clear_context=True` every loop (full history reset), but judge
    remediation notes are re-injected into the next prompt, and judges read the
    pre-clear history snapshot — judges are the loop's memory.
  - *Prompt/agent:* implementor frozen at loop start; judges are ephemeral,
    per-iteration, per-judge agents with their own model + prompt.
  - *Tools:* implementor keeps its full toolset minus `ask_user_question`
    (autonomous-loop gate); judges get a read-only subset (incl. shell) plus
    `inspect_goal_history`.
- The completion rule (strict unanimity of non-abstaining judges) and the
  iteration cap (`goal_max_iterations`, default 10, clamp 1–1000) are the two
  knobs that bound the loop. `/wiggum` has neither.
