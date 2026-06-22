# Spike: Cataloging the `/wiggum` Loop Command

> **Bead:** `code_puppy_oss-68r` · Type: spike · Priority: P2
> **Status:** research-only — documents `/wiggum` *as it exists today*, before any redesign.
> **Companion catalogs:** `/goal` (`code_puppy_oss-fzh`), `/judges` (`code_puppy_oss-80b`).
> Synthesis bead `code_puppy_oss-0ae` consumes all three.

`/wiggum` and `/goal` are **two modes of the same plugin** (`code_puppy/plugins/wiggum/`).
They share one state object, one turn-end hook, and one stop command. The *only*
difference is: `/goal` runs LLM judges between iterations and stops when they vote
"complete"; `/wiggum` is the dumb-but-loyal cousin that just re-runs the same prompt
forever until you stop it. This doc focuses on `/wiggum` but calls out the shared
machinery so the `/goal` catalog stays consistent.

---

## 1. What `/wiggum` does, end to end

```
/wiggum say hello world
```

1. `handle_wiggum_command` parses the prompt out of the command string.
2. It flips global state on: `state.start(prompt, mode="wiggum")`.
3. It returns the **prompt string**, which the CLI runner executes as a normal turn.
4. When that turn finishes (success **or** error), the `interactive_turn_end` hook
   fires. `_on_interactive_turn_end` sees wiggum mode is active and returns a
   **continuation request dict**: `{"prompt", "clear_context": True, "delay": 0.5,
   "reason": "wiggum"}`.
5. The CLI runner's continuation loop clears context, waits `delay` seconds, and
   re-runs the prompt. Back to step 4.
6. The loop only ends when the user runs `/wiggum_stop` (or an alias) or hits
   **Ctrl+C**, which trips `interactive_turn_cancel` → `state.stop()`.

There is **no completion check and no iteration cap** in wiggum mode — it loops
literally forever until a human stops it. (Contrast with `/goal`, which has judges
and `goal_max_iterations`.)

### `/wiggum_stop` and its aliases

`handle_wiggum_stop_command` is registered as `/wiggum_stop` with aliases:

| Command | Notes |
|---------|-------|
| `/wiggum_stop` | canonical |
| `/stopwiggum` | alias |
| `/ws` | short alias |
| `/goal_stop` | shared with `/goal` — stops *either* mode, since both share state |

It calls `state.stop()` if active, otherwise just says "not active." The fact that
`/goal_stop` stops wiggum (and `/wiggum_stop` stops goal) is a direct consequence of
the shared `WiggumState` singleton — there is exactly one loop in flight at a time.

---

## 2. Entry points & code paths

| Concern | File · Symbol |
|---------|---------------|
| Start command | `code_puppy/plugins/wiggum/register_callbacks.py` · `handle_wiggum_command` |
| Stop command | same file · `handle_wiggum_stop_command` (aliases `stopwiggum`, `ws`, `goal_stop`) |
| Loop driver | same file · `_on_interactive_turn_end` (wiggum branch) |
| Cancel handler | same file · `_on_interactive_turn_cancel` |
| Hook registration | same file, module scope: `register_callback("interactive_turn_end"/"interactive_turn_cancel", ...)` |
| Shared state | `code_puppy/plugins/wiggum/state.py` · `WiggumState`, module helpers `start/stop/is_active/is_goal_mode/get_prompt/increment` |
| Back-compat shim | `code_puppy/command_line/wiggum_state.py` (delegates to the plugin) |
| Interactive-tool gate | `code_puppy/tools/ask_user_question/handler.py` · `is_wiggum_active()` check |
| Hook dispatch | `code_puppy/callbacks.py` · `on_interactive_turn_end`, `on_interactive_turn_cancel` |
| Continuation loop | `code_puppy/cli_runner.py` (~lines 949–1035) |
| Command dispatch | `code_puppy/command_line/command_handler.py` · `handle_command` (returns the prompt string) |

### The "returned string becomes a prompt" trick

`handle_wiggum_command` returns `prompt` (a `str`). `handle_command` passes that
straight through (`return cmd_info.handler(command)`). In `cli_runner.py`, a `str`
result that isn't the special `__AUTOSAVE_LOAD__` sentinel is assigned to `task` and
executed as a normal agent turn. So **activation and the first run are the same
action** — there's no separate "kick off the first iteration" step. This is worth
remembering: the *first* iteration is driven by the command return value; every
*subsequent* iteration is driven by the turn-end hook.

### The continuation loop (cli_runner.py)

After a turn completes, the runner enters a `while True:` loop that:

1. Calls `await on_interactive_turn_end(agent, prompt, result, success=, error=)`.
2. Picks the first dict from the returned list (`next((r for r in ... if isinstance(r, dict)), None)`).
3. If no dict, or the dict's `prompt` is empty → **break** (loop ends).
4. If `clear_context` is truthy → rotate the autosave session and clear history (see §4).
5. Sleep `delay` seconds.
6. Re-run the prompt via `run_prompt_with_attachments`, capture result/error.
7. Loop back to step 1, feeding the new result/error into the next hook call.

The CLI owns *execution*; the plugin owns *policy*. The plugin never runs the agent
itself — it just returns dicts saying "please run this next."

---

## 3. Re-loop-on-error behavior & stop conditions

The wiggum branch of `_on_interactive_turn_end` re-loops on **both** success and
error. The only difference is cosmetic messaging:

```python
if error is not None:
    emit_warning(f"\n WIGGUM RETRYING AFTER ERROR! (Loop #{loop_num})")
    emit_system_message(f"Previous run failed: {error}")
else:
    emit_warning(f"\n WIGGUM RELOOPING! (Loop #{loop_num})")
emit_system_message(f"Re-running prompt: {goal_prompt}")
return {"prompt": goal_prompt, "clear_context": True, "delay": 0.5, "reason": "wiggum"}
```

So a crashing prompt loops just as eagerly as a succeeding one — wiggum does **not**
back off, cap retries, or give up on repeated failures.

**Stop conditions for wiggum mode (the complete list):**

1. **`/wiggum_stop`** (or `stopwiggum` / `ws` / `goal_stop`) → `state.stop()`.
2. **Ctrl+C** during a turn → the CLI calls `on_interactive_turn_cancel(...)`,
   which routes to `_on_interactive_turn_cancel` → `state.stop()` + a warning.
3. **Empty prompt edge case** — if `state.get_prompt()` ever returns falsy when the
   hook fires, `_on_interactive_turn_end` defensively calls `state.stop()` and returns
   `None`. (In practice `start()` always stores a non-empty prompt, so this is a guard,
   not a normal path.)

There is **no** natural/automatic termination. No max-iterations, no completion
detection, no error threshold. The loop counter (`state.increment()` →
`loop_count`) is incremented every iteration but is **only used for display** in
wiggum mode (the `Loop #N` text). It does *not* gate anything. (In `/goal` mode the
same counter *is* compared against `goal_max_iterations`.)

### Ctrl+C paths (there are several)

The cancel hook is invoked from multiple spots in `cli_runner.py`, all converging on
`_on_interactive_turn_cancel`:

- Ctrl+C at the input prompt (`reason="Ctrl+C"`).
- Agent task returning `None` / cancellation mid-turn (`reason="cancellation"`).
- `KeyboardInterrupt` caught around the turn (`reason="Ctrl+C"`).
- Cancellation/Ctrl+C *inside the continuation loop itself* (after a re-loop run).

All of them call `state.stop()` via the cancel hook, so any flavor of Ctrl+C reliably
kills the loop.

---

## 4. Cross-cutting concerns

### (1) Context management — `clear_context=True` on every re-loop

Every wiggum continuation dict sets `clear_context: True`. When the CLI runner sees
this it does two things (cli_runner.py):

```python
if continuation.get("clear_context", False):
    new_session_id = finalize_autosave_session()
    current_agent.clear_message_history()
    emit_system_message(f"Context cleared. Session rotated to: {new_session_id}")
```

- **`finalize_autosave_session()`** (`config.py`) persists the current autosave
  snapshot (`record_terminal_session` + `auto_save_session_if_enabled`) and then
  **rotates to a fresh autosave id** (`rotate_autosave_id`). So the prior iteration's
  transcript is saved to disk before being dropped from live memory — nothing is lost,
  it's just no longer in-context.
- **`agent.clear_message_history()`** (`base_agent.py`) resets the in-memory
  conversation: `self._message_history = []` and `self._compacted_message_hashes.clear()`.

**What is reset vs carried across iterations:**

| Carried across iterations | Reset each iteration |
|---------------------------|----------------------|
| The **prompt** (re-sent verbatim) | The agent's **message history** (full reset) |
| The **agent instance** (same object) | Compacted-message hash cache |
| The **system prompt** (rebuilt from same agent) | Autosave session id (rotated) |
| Global wiggum state (`loop_count`, `mode`) | — |

Net effect: each wiggum iteration is a **fresh-context run of the same prompt** on the
same agent. The agent has *no memory* of what it did last loop — wiggum is effectively
"keep doing this task from scratch, over and over." (This is also why an
agent in a wiggum loop can't naturally "notice" it already did the work — it starts
blind every time.)

>  Note: `/wiggum` hardcodes `clear_context: True`. There is no user-facing toggle
> to carry context across loops. `/goal` makes the same choice but *injects judge
> remediation notes* into the next prompt, so goal iterations at least get feedback;
> wiggum gets nothing but the original prompt.

### (2) System prompt / agent & model assignment

There is **no reassignment**. The same `current_agent` object that ran the first turn
is reused for every re-loop (the CLI runner passes `current_agent` into the
continuation loop). The model and system prompt are whatever that agent is configured
with — `get_full_system_prompt()` is rebuilt each run but from the *same* agent, so
it's effectively stable across iterations (modulo any `load_prompt` plugin hooks,
which run every time the prompt is assembled).

Wiggum does **not** pick a different agent, swap models, or alter the system prompt
between iterations. If you `/agent`-switch mid-loop... actually you can't, because
the loop holds the REPL — there's no opportunity to run another command until you
stop the loop. So agent/model assignment is "frozen at loop start."

### (3) Tool / MCP / skill availability — `ask_user_question` is disabled

During wiggum mode, the agent runs **autonomously**, so any tool that needs a human
would deadlock the loop. The guard lives in
`code_puppy/tools/ask_user_question/handler.py`:

```python
from code_puppy.plugins.wiggum.state import is_active as is_wiggum_active
...
# Block interactive tools in wiggum (autonomous loop) mode
if is_wiggum_active():
    return AskUserQuestionOutput.error_response(
        "Interactive tools are disabled during /wiggum mode. ..."
    )
```

**Mechanism details worth noting:**

- The gate is `is_active()` — which is **`True` for goal mode too** (it only checks
  `state.active`, not `mode`). So `ask_user_question` is disabled during `/goal` as
  well, despite the user-facing message saying "/wiggum mode." (Minor wording bug —
  see §5.)
- The check happens **after** input validation, so a malformed `ask_user_question`
  call still gets a useful schema error rather than a confusing wiggum error. This
  ordering is deliberate (there's a comment in the handler explaining it).
- The tool returns a *soft* error (`error_response`) telling the agent to "make a
  reasonable decision to proceed" — it does **not** raise, so the agent can recover
  and keep going. Good for autonomy.
- The same handler *also* blocks interactive tools for sub-agents
  (`is_subagent()`) — a separate, orthogonal gate.

**No other tool/MCP/skill availability changes** in wiggum mode. MCP servers, skills,
and every other tool remain exactly as they were for that agent. Only the
human-in-the-loop `ask_user_question` tool is gated.

### Shared state machinery with `/goal`

`WiggumState` (`state.py`) is a tiny dataclass singleton (`_STATE`) with:

| Field | Purpose |
|-------|---------|
| `active: bool` | is a loop running? |
| `prompt: str \| None` | the prompt being looped |
| `loop_count: int` | iteration counter (display-only in wiggum, gate in goal) |
| `mode: str` | `"wiggum"` or `"goal"` — the *only* behavioral fork |
| `remediation_notes: str \| None` | goal-only; judge feedback for next iteration. **Unused by wiggum.** |

Module-level helpers (`start/stop/is_active/is_goal_mode/get_prompt/increment`)
wrap the singleton. `_on_interactive_turn_end` branches on `state.is_goal_mode()`:
goal → run judges; otherwise → the wiggum re-loop. So **one hook drives both loops**;
`mode` is the discriminator.

`code_puppy/command_line/wiggum_state.py` is a **backward-compat shim**. Wiggum used
to live under `command_line/`; it's a plugin now, but old imports/tests still reach
for `code_puppy.command_line.wiggum_state`. The shim just forwards to
`plugins.wiggum.state` (`start_wiggum` → `get_state().start(prompt, mode="wiggum")`,
etc.). It hardcodes `mode="wiggum"`, so the legacy API can only start wiggum loops,
never goal loops. Tests in `tests/command_line/test_wiggum_state.py` and
`tests/test_cli_runner_full_coverage.py` still import through this shim.

---

## 5. Current limitations & edge cases

1. **Infinite loop by design.** No iteration cap, no completion detection, no
   error-backoff. The only exits are `/wiggum_stop` and Ctrl+C. A wiggum loop on a
   cheap prompt is fine; on an expensive multi-tool prompt it will happily burn tokens
   and money forever. `/goal` at least has `goal_max_iterations`; wiggum has nothing.
2. **Re-loops on error with zero feedback.** A prompt that crashes every run will loop
   forever, re-crashing each time, with no escalation. The error is printed but not
   fed back into the next prompt (unlike goal's remediation notes). An agent has no way
   to learn from the failure because context is wiped between loops.
3. **Loop counter is cosmetic in wiggum mode.** `loop_count` is incremented and shown
   as `Loop #N` but gates nothing. Easy to mistake for a meaningful limit.
4. **`ask_user_question` error message says "/wiggum" during `/goal` too.** The gate
   uses `is_active()` (mode-agnostic), but the message hardcodes "/wiggum mode." Goal
   users who hit the gate get a slightly wrong explanation. Cheap copy fix.
5. **Single global loop.** `WiggumState` is a process-wide singleton, so only one
   loop (wiggum *or* goal) can run at a time. Starting `/wiggum` while a `/goal` loop
   is "active" would just overwrite state via `start()` — but in practice the REPL is
   blocked during a loop, so you can't issue the second command anyway.
6. **`remediation_notes` is dead weight for wiggum.** The field exists on the shared
   state and is reset by `start()`/`stop()`, but wiggum never reads or writes it. Minor
   cohesion smell from sharing one state object across two modes.
7. **No persistence of "I'm in a loop" across restarts.** State is in-memory only; a
   crash or restart silently ends the loop (arguably fine, but undocumented).
8. **`clear_context` is non-negotiable.** There's no way to run wiggum *with* memory
   carried forward. For some "keep iterating on the same growing artifact" use cases
   that's the wrong default, but it's hardcoded.
9. **Continuation-loop error handling is broad.** Inside the cli_runner continuation
   loop, a generic `except Exception` re-renders the error and lets the loop continue —
   consistent with "re-loop on error," but it means a persistent environment problem
   (e.g. auth failure) spins silently.

---

## 6. Concrete improvement opportunities (seeds for follow-up beads)

These are **suggestions to seed follow-up work**, not changes made in this spike.

1. **Add a wiggum iteration cap / safety valve.** Mirror `goal_max_iterations` with a
   `wiggum_max_iterations` (default e.g. 0 = unlimited, opt-in cap). Prevents runaway
   token burn. *(small)*
2. **Error backoff / circuit breaker.** If N consecutive iterations error, pause or
   stop with a clear message instead of re-crashing forever. Optionally exponential
   `delay`. *(small–medium)*
3. **Optional context carry-over.** A `/wiggum --keep-context` flag (or config) that
   sets `clear_context: False`, for "iteratively refine one artifact" workflows. *(small)*
4. **Feed the previous error into the next prompt.** Like goal's remediation notes —
   append "previous run failed: …" to the next wiggum prompt so the agent can adapt.
   Reuses the existing `remediation_notes` field that's currently dead weight in
   wiggum. *(small)*
5. **Fix the `ask_user_question` gate message** to be mode-aware (say "/goal" or
   "/wiggum" based on `state.mode`), or make it generic ("autonomous loop mode").
   *(trivial)*
6. **Unify wiggum/goal under an explicit policy object.** The two modes share one
   state + one hook but fork on `mode` strings and carry mode-specific fields
   (`remediation_notes`) on a shared struct. A small strategy/policy split would
   improve cohesion (SOLID's SRP) without changing behavior. This is exactly the kind
   of thing the synthesis bead (`code_puppy_oss-0ae`) should weigh. *(medium)*
7. **Surface loop status / make the counter meaningful.** Either remove the cosmetic
   `Loop #N` confusion or wire it to a real cap (#1). *(trivial once #1 exists)*
8. **Consider deprecating the `command_line/wiggum_state.py` shim** once tests are
   migrated to import from `plugins.wiggum.state`, to remove a duplicated surface.
   *(small, test-touching)*

---

## Appendix: file/symbol quick reference

```
code_puppy/plugins/wiggum/
├── register_callbacks.py   # commands + hooks (wiggum AND goal live here)
│   ├── handle_wiggum_command         (/wiggum <prompt>)
│   ├── handle_goal_command           (/goal, /kibble, /chow)  ← goal sibling
│   ├── handle_wiggum_stop_command    (/wiggum_stop, stopwiggum, ws, goal_stop)
│   ├── _on_interactive_turn_end      (wiggum branch + goal branch)
│   ├── _on_interactive_turn_cancel   (Ctrl+C → state.stop())
│   └── register_callback("interactive_turn_end"/"interactive_turn_cancel", ...)
├── state.py                # WiggumState singleton + module helpers (shared w/ goal)
├── judge.py / judge_config.py  # GOAL-ONLY (judges); irrelevant to wiggum
└── __init__.py

code_puppy/command_line/wiggum_state.py   # back-compat shim → plugins.wiggum.state
code_puppy/tools/ask_user_question/handler.py   # is_wiggum_active() gate
code_puppy/callbacks.py                   # on_interactive_turn_end/_cancel dispatch
code_puppy/cli_runner.py                  # continuation loop that runs the dicts
code_puppy/command_line/command_handler.py # returns prompt string → becomes a turn
```
