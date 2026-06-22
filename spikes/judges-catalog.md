# Spike: Cataloging the `/judges` Command & Judge Configuration System

> **Bead:** `code_puppy_oss-80b` · Type: spike · Priority: P2
> **Status:** research-only — documents `/judges` *as it exists today*, before any redesign.
> **Companion catalogs:** `/wiggum` (`code_puppy_oss-68r`), `/goal` (`code_puppy_oss-fzh`).
> Synthesis bead `code_puppy_oss-0ae` consumes all three.

`/judges` is the configuration surface for the **LLM judges** that `/goal` fans
out to between iterations. A "judge" is a persisted `(name, model, prompt,
enabled)` record. `/goal` loads every *enabled* judge, runs them in parallel
against the implementor's latest turn, and only declares the goal complete when
every non-abstaining judge votes "complete." `/judges` opens an interactive
prompt_toolkit TUI for CRUD on that judge roster.

This doc is the **judges-side** of the wiggum/goal catalog trilogy. It covers the
config schema, persistence, the TUI flow, the default-judge fallback, where the
command is wired up, the threading model, and the limitations worth filing
follow-up beads against.

---

## 1. What `/judges` does, end to end

```
/judges
```

1. The user types `/judges`. The command is registered by the wiggum plugin
   (`code_puppy/plugins/wiggum/register_callbacks.py` → `handle_judges_command`).
2. The handler spins up a `ThreadPoolExecutor`, submits
   `lambda: asyncio.run(interactive_judges_menu())`, and blocks on
   `future.result(timeout=600)`. The async TUI runs on a **fresh event loop on a
   worker thread** so it never collides with whatever loop the CLI REPL is using.
3. `interactive_judges_menu()` (`code_puppy/command_line/judges_menu.py`) loads
   the persisted judges, switches the terminal to the alternate screen buffer,
   and renders a **split-panel list view**: judges on the left, a detail/preview
   of the selected judge on the right.
4. The user navigates and acts via single-key bindings (see §4). Add/Edit open a
   secondary in-TUI **form** (Name / Model picker / Prompt). Toggle / Delete act
   on the highlighted row directly.
5. Every mutation is persisted to `judges.json` immediately (no "save & exit"
   step — each add/edit/toggle/delete writes through to disk).
6. `Esc` or `Ctrl+C` closes the menu, restores the main screen buffer, and
   returns control to the REPL.

There is **no separate "apply" step** — the menu reads from and writes to
`judges.json` on every action, and `/goal` reads the same file fresh on every
iteration.

---

## 2. Entry points & code paths

| Concern | File | Symbol(s) |
|---|---|---|
| Command registration | `code_puppy/plugins/wiggum/register_callbacks.py` | `handle_judges_command` (decorated `@register_command(name="judges", …)`) |
| TUI driver | `code_puppy/command_line/judges_menu.py` | `interactive_judges_menu()` |
| List-view rendering | `code_puppy/command_line/judges_menu.py` | `_render_menu`, `_render_preview` |
| Add/Edit form | `code_puppy/command_line/judges_menu.py` | `_run_judge_form`, `_add_judge_flow`, `_edit_judge_flow`, `_FormResult` |
| Inline model picker | `code_puppy/command_line/judges_menu.py` | `_render_model_list`, `_load_available_models` |
| Display sanitizers | `code_puppy/command_line/judges_menu.py` | `_sanitize`, `_wrap` |
| Config schema + persistence | `code_puppy/plugins/wiggum/judge_config.py` | `JudgeConfig`, `JudgeRegistry`, `load_judges`, `save_judges`, `add_judge`, `update_judge`, `delete_judge`, `toggle_judge`, `validate_name`, `get_enabled_judges_or_default` |
| Judge execution (consumer) | `code_puppy/plugins/wiggum/judge.py` | `judge_goal`, `GoalJudgement`, `GoalJudgeOutput` |
| `/goal` orchestration (consumer) | `code_puppy/plugins/wiggum/register_callbacks.py` | `_resolve_judges`, `_run_goal_judges`, `_run_single_judge`, `_on_interactive_turn_end` |
| Pagination helpers | `code_puppy/command_line/pagination.py` | `get_total_pages`, `get_page_bounds`, `get_page_for_index`, `ensure_visible_page` |
| Model name source | `code_puppy/command_line/model_picker_completion.py` | `load_model_names` |

### Data flow (high level)

```
/judges ──► handle_judges_command ──► (worker thread) asyncio.run(interactive_judges_menu)
                                                          │
                                                          ▼
                                     load_judges / add_judge / update_judge / toggle_judge / delete_judge
                                                          │
                                                          ▼
                                              judges.json  (XDG_DATA_HOME)
                                                          ▲
                                                          │  (read fresh each iteration)
/goal ──► _on_interactive_turn_end ──► _run_goal_judges ──► _resolve_judges ──► get_enabled_judges_or_default
                                                          │
                                                          ▼
                                  asyncio.gather(judge_goal(...) for each enabled judge)
```

---

## 3. `JudgeConfig` schema & `judges.json` persistence

### Schema (`judge_config.py`)

```python
@dataclass
class JudgeConfig:
    name: str                       # 1–64 chars, [a-zA-Z0-9_-], no spaces
    model: str                      # must exist in the model config to actually run
    prompt: str = DEFAULT_JUDGE_PROMPT
    enabled: bool = True
```

- **`name`** — validated by `validate_name` against `^[a-zA-Z0-9_\-]{1,64}$`.
  Empty or space-containing names are rejected. Names are unique (enforced on add
  and on rename).
- **`model`** — a model name string. *Not* validated against the model config at
  add/edit time — only the model picker's available list is offered, but a stale
  `judges.json` can reference a model that no longer exists. `judge_goal` handles
  that at run time by **abstaining** (see §5).
- **`prompt`** — free text. Falls back to `DEFAULT_JUDGE_PROMPT` whenever it's
  empty/None (both in `from_dict` and `update_judge`). The default prompt frames
  the judge as a strict, read-only, never-ask-questions completion verifier.
- **`enabled`** — only enabled judges are surfaced to `/goal`.

`JudgeRegistry` is the in-memory snapshot wrapper: `judges: list[JudgeConfig]`
plus convenience methods `names()`, `enabled()`, `find(name)`.

### Persistence location

```python
JUDGES_FILE = os.path.join(DATA_DIR, "judges.json")
# DATA_DIR = $XDG_DATA_HOME/code_puppy   (falls back to ~/.local/share/code_puppy)
```

`DATA_DIR` comes from `code_puppy/config.py` (`_get_xdg_dir("XDG_DATA_HOME",
".local/share")`). On a typical Linux box this is
`~/.local/share/code_puppy/judges.json`.

### On-disk shape

```json
{
  "judges": [
    {
      "name": "tests-pass",
      "model": "gpt-5.4",
      "prompt": "You are Code Puppy's goal-completion judge...",
      "enabled": true
    }
  ]
}
```

### Read path — `load_judges()` is defensively strict

`load_judges()` returns an **empty registry** rather than throwing on any of:

- file missing,
- `OSError` / `json.JSONDecodeError` (corrupt file),
- top-level not a dict, or `judges` not a list.

Per-entry, it **silently skips** (with a `logger.warning`) any judge that is:
non-dict, fails `JudgeConfig.from_dict`, has an invalid name, is a duplicate
name, or has an empty model. So a partially-broken `judges.json` degrades to "the
good judges still load" rather than crashing the menu or `/goal`.

### Write path — `save_judges()` is atomic

Writes to `{JUDGES_FILE}.tmp` then `os.replace()`s it over the real file.
`os.replace()` is an atomic rename on POSIX systems and on Windows as of
Python 3.8+, so the swap is cross-platform atomic. `ensure_ascii=False`,
`indent=2`. The mutators
(`add_judge`, `update_judge`, `delete_judge`, `toggle_judge`) each load → mutate
→ save, so every menu action is a full round-trip to disk.

---

## 4. The TUI flow & key handling

Two screens: the **list view** (`interactive_judges_menu`) and the **add/edit
form** (`_run_judge_form`). The main loop runs the list-view `Application`,
exits it when a key sets a "pending action," performs the action (which may run
the form `Application`), then re-enters the list-view loop. This
exit-act-reenter pattern is how it juggles two prompt_toolkit `Application`s
without nesting them.

### List view (`_render_menu` + key bindings)

Layout: `VSplit([Frame(menu, 40%), Frame(preview, 60%)])`. Left panel lists
judges with an `[on]`/`[off]` glyph, name, and model; right panel shows the full
detail of the highlighted judge (name, model, enabled, wrapped prompt).

| Key | Action | Mechanism |
|---|---|---|
| `↑` / `↓` | move selection | mutates `selected_idx`, re-pages via `ensure_visible_page` |
| `←` / `→` | page back / forward | adjusts `current_page`, snaps selection to page start |
| `N` | add new judge | sets `pending_action="add"`, exits app → `_add_judge_flow` |
| `Enter` / `E` | edit highlighted judge | sets `pending_action="edit"` + target → `_edit_judge_flow` |
| `T` | toggle enabled | sets `pending_action="toggle"` → `toggle_judge(target)` |
| `D` | delete highlighted | sets `pending_action="delete"` → `delete_judge(target)` |
| `Esc` / `Ctrl+C` | close menu | sets `pending_action="close"`, breaks the loop |

After every mutating action the loop calls `refresh(select_name=...)`, which
**re-reads `judges.json` from disk** and tries to keep the cursor on the same
judge by name (falling back to clamping the index).

### Add/Edit form (`_run_judge_form`)

Three tabbable sections stacked vertically (`HSplit`):

1. **Name** — single-line `TextArea`.
2. **Model** — an inline paginated picker rendered into a focusable `Window`
   (`FormattedTextControl`). Whatever row is highlighted **is** the selection —
   there's no separate "confirm" gesture. Models come from `load_model_names()`.
3. **Prompt** — multiline `TextArea` (scrollable), pre-filled with the existing
   prompt or `DEFAULT_JUDGE_PROMPT`.

| Key | Action |
|---|---|
| `Tab` / `Shift+Tab` | cycle focus Name → Model → Prompt → … |
| `↑` / `↓` | (Model focused) move model selection |
| `←` / `→`, `PgUp` / `PgDn` | (Model focused) page through models |
| `Home` / `End` | (Model focused) jump to first / last model |
| `Ctrl+S` | validate + save |
| `Esc` / `Ctrl+C` | cancel |

The model-navigation bindings are gated behind a `Condition(is_model_focused)`
filter so arrow keys only steer the picker when the Model window has focus;
otherwise they behave normally inside the TextAreas.

`Ctrl+S` validation order: name (`validate_name`) → model present → prompt
non-empty. Failures set a red `status_line` and keep the form open. On success it
populates a `_FormResult` struct (`saved`, `name`, `model`, `prompt`) that the
caller reads.

### Terminal management

- `set_awaiting_user_input(True/False)` brackets the whole menu so the
  command-runner knows a human is at the keyboard.
- The list view switches to the **alternate screen buffer**
  (`\033[?1049h` on enter, `\033[?1049l` on exit) and clears the screen between
  loop iterations.
- `await asyncio.sleep(0.05)` after entering the alt buffer gives the terminal a
  beat to flip before the first render.
- `_sanitize()` strips characters that aren't in a curated set of "safe" Unicode
  categories (letters/digits/punct/symbols/space) so judge names/models with
  exotic glyphs don't wreck prompt_toolkit's width math.

---

## 5. The default-judge fallback

`get_enabled_judges_or_default(fallback_model)`:

```python
registry = load_judges()
enabled = registry.enabled()
if enabled:
    return enabled
return [JudgeConfig(name="default", model=fallback_model,
                    prompt=DEFAULT_JUDGE_PROMPT, enabled=True)]
```

So `/goal` works **out of the box with zero configuration**: if the user has
never run `/judges` (or has disabled every judge), `/goal` synthesizes a single
ephemeral judge named `default` that uses the *implementor agent's own model* and
the standard goal-judge prompt. This synthetic judge is **not written to disk** —
it only exists for that run.

`_resolve_judges(implementor_agent)` (in `register_callbacks.py`) computes the
`fallback_model`: it tries `implementor_agent.get_pydantic_agent().model.model_name`,
then `implementor_agent.get_model_name()`, then literally `"code-puppy"` as a
last resort, before handing off to `get_enabled_judges_or_default`.

Note the subtle distinction: **"no enabled judges"** (fallback fires) vs **"no
judges at all"** in `_run_goal_judges`, which guards `if not judges: return
False, "No judge agents configured.", []` — but in practice `_resolve_judges`
always returns at least the synthetic default, so that guard is effectively
dead-code defense.

---

## 6. Threading & concurrency model

- **`/judges` menu** runs via `asyncio.run(...)` on a **`ThreadPoolExecutor`
  worker thread**, with `future.result(timeout=600)` blocking the calling
  command handler. A new event loop per invocation avoids clashing with the REPL
  loop. A 10-minute timeout caps a wedged menu; `TimeoutError` and generic
  `Exception` both degrade to an `emit_warning` instead of crashing.
- **`/goal` judges** run via `asyncio.gather(...)` — all enabled judges fire in
  **parallel** on the REPL's loop. Each judge is a fresh, read-only
  `pydantic_ai` Agent (`judge_goal`) run inside a `subagent_context` so its tool
  banners and chatter are suppressed.
- **Display is serialized at the orchestrator level.** `_run_single_judge`
  deliberately does **no** printing, because concurrent writes into the Rich
  console (which uses `\r` line-clearing) would interleave and clobber each
  other. `_run_goal_judges` prints the per-judge verdicts *after* `gather`
  resolves.
- **Abstain semantics.** A judge that can't render a verdict for an
  infrastructure reason (model not in config, HTTP 4xx/5xx, auth, timeout,
  plumbing bug) returns `GoalJudgement(abstained=True)`. Abstainers are excluded
  from the tally — the goal completes when every *non-abstaining* judge votes
  complete. If **every** judge abstains, the result is treated as incomplete with
  a warning (can't decide → don't claim done).
- **Cancellation.** `Ctrl+C` / `CancelledError` propagates out of the judge
  gather and is caught at the plugin boundary (`_on_interactive_turn_end`), which
  stops the goal loop cleanly rather than treating it as "incomplete, retry."

---

## 7. Current limitations & edge cases

1. **Emoji / phantom-space completion bug (known).** The `/judges` command's
   warning emoji (and other emoji slash-commands) carry Unicode variation selectors
   (U+FE00–FE0F) that desync prompt_toolkit's width math from the terminal,
   showing up as **phantom spaces** in the input line. Worked around in
   `code_puppy/command_line/prompt_toolkit_completion.py` via
   `_strip_variation_selectors` (on completion display strings) and
   `_normalize_emoji_spacing` (pads Neutral-width emoji). This is a *mitigation*,
   not a root-cause fix — it lives in core, not the plugin.
2. **Model validity isn't checked at add/edit time.** You can save a judge whose
   `model` later disappears from the model config (or hand-edit `judges.json` to
   anything). The failure only surfaces at `/goal` run time as an **abstain**,
   which silently drops that judge's vote. There's no "this judge is
   mis-pointed" indicator in the `/judges` list.
3. **No confirmation on delete.** `D` deletes the highlighted judge immediately
   with no "are you sure?" — and there's no undo.
4. **No duplicate / import / export / reset.** No way to clone a judge as a
   starting point, no bulk enable/disable, no export/import, no "restore
   defaults." Multi-judge setups must be built one-by-one through the form.
5. **Hand-edited dupes/invalid entries vanish silently.** `load_judges` skips
   duplicate names, empty models, and invalid names with only a `logger.warning`
   — a user who hand-edited `judges.json` gets no in-TUI feedback that entries
   were dropped.
6. **Two `Application`s, exit-and-reenter loop.** The menu exits the list-view
   app to run the form app, then rebuilds/re-enters. It works, but it's heavier
   than a single nested-layout app and means full screen redraws between every
   action.
7. **Prompt editing is in-TUI only.** The form's prompt is a plain multiline
   `TextArea` — no `$EDITOR` popout, no syntax help, no templating. Long
   judge prompts are awkward to author in a small scrolling box.
8. **Default-judge prompt is fixed.** The synthetic `default` judge always uses
   `DEFAULT_JUDGE_PROMPT` with the implementor's model — there's no `/set` knob
   to customize the zero-config judge without creating a persisted one.
9. **No test coverage of the live `Application` event loop.** Tests
   (`tests/command_line/test_judges_menu.py`) exercise the pure renderers
   (`_render_menu`, `_render_model_list`) and config CRUD, but the actual key
   bindings / focus cycling aren't driven by an automated harness.
10. **`name` rename ↔ "keep cursor" coupling.** `refresh(select_name=...)` after
    an edit relies on matching by the (possibly new) name; if a rename collides
    or the entry is dropped on reload, the cursor silently falls back to index
    clamping. Minor, but a source of "where'd my selection go" surprise.

---

## 8. Improvement opportunities (seeds for follow-up beads)

> These are catalog observations, **not** committed work. File as separate beads.

- **Validate model against the model config at add/edit time** and flag
  mis-pointed judges in the list view (e.g. a red warning glyph) so abstains aren't
  the first time a user learns a judge is broken.
- **Add a delete confirmation** (or soft-delete / undo) to the list view.
- **Surface skipped/invalid `judges.json` entries** in the TUI (a banner like
  "2 invalid judge entries were ignored") instead of log-only warnings.
- **Judge templates / clone-as-new** so users can start from an existing judge
  or a curated preset ("tests pass," "docs updated," "no TODOs left").
- **Export / import / reset-to-defaults** for sharing judge sets across machines
  or teams (mirrors how agents/skills are shareable).
- **`$EDITOR` popout (or larger modal) for the prompt field** to make authoring
  long judge prompts comfortable.
- **Per-judge weight / quorum policy** — today completion is strict unanimity of
  non-abstainers; a configurable quorum (e.g. "2 of 3") would be more flexible.
- **Configurable default judge** via `/set` (model + prompt) so the zero-config
  experience is tunable without persisting a judge.
- **Consolidate the two-Application flow** into a single nested-layout app to cut
  the full-screen redraws and simplify state management.
- **Drive the key bindings under test** with a prompt_toolkit test harness so
  navigation/focus regressions are caught.
- **Root-cause the emoji width issue** upstream (or strip variation selectors at
  the command-name source) rather than patching two render paths.

---

## 9. Cross-references for the synthesis bead (`code_puppy_oss-0ae`)

- `/judges` is purely a **config surface**; the *behavior* lives in `/goal`.
  Judge execution semantics (parallel fan-out, abstain handling, unanimity,
  remediation-notes feedback loop, `goal_max_iterations` cap) are documented in
  the `/goal` catalog (`code_puppy_oss-fzh`) and the shared-machinery section of
  the `/wiggum` catalog (`code_puppy_oss-68r`).
- `judges.json` is the single source of truth shared between the TUI (writer) and
  `/goal` (reader). There's no caching layer — both sides read fresh.
- The single-default-judge fallback (`get_enabled_judges_or_default`) is what
  makes `/goal` usable with zero `/judges` configuration.
