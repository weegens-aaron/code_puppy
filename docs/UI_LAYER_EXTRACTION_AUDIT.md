# UI / `command_line` / Messaging Layer — Plugin Extraction Audit (`puppy-tp4.4`)

> **Type:** DISCOVERY spike — *research only, zero implementation.* No code was
> moved or refactored. This document classifies modules and **proposes** follow-up
> beads; it does not build them.
>
> **Scope (the brief):** `command_line/`, `messaging/`, `status_display.py`,
> `terminal_utils.py`, `keymap.py`, `list_filtering.py`, `mcp_prompts/`. Many UI
> concerns are *already* plugins (theme, statusline, context_indicator,
> wide_completion_menu, subagent_panel, prompt_newline) — the goal is to find what
> *remains in core* that could follow them out.
>
> **Sibling of:** `tp4.1` (harness boundary), `tp4.2` (model/provider),
> `tp4.3` (tools), `tp4.5` (services). Feeds synthesis bead **`tp4.6`**.

---

## 0. Architecture Grounding (REQUIRED FIRST STEP — evidenced)

Per the bead's hard contract and the `skill-grounding-must-be-evidenced` memory:
the `code-puppy-agent` architecture skill was **activated first**, and each
classification decision below is tied to the *specific* SKILL.md section that
constrains it — each cross-checked against a **live source line I actually read**.

| # | Decision in this audit | SKILL.md section | Live source evidence I read |
|---|------------------------|------------------|------------------------------|
| 1 | Reuse `tp4.1`'s **Stub-and-Boot Test** + 3-state rule (HARNESS / PLUGIN-CANDIDATE / **HARNESS-COUPLED**) rather than invent a new yardstick | §12.3 "the plugin system is the API surface; the core is the engine" | `docs/HARNESS_BOUNDARY_CRITERIA.md` §1–§2 (Stub-and-Boot procedure + decision rule) |
| 2 | Slash-command **dispatch + static registry** (`command_handler`, `command_registry`) is HARNESS — the REPL can't route input without it | §1 "TUI collects user input… delegates to the agent manager"; §13 — no entry, but `command_line/` is named the non-pluggable layer | `command_handler.py:178` registry lookup `get_command(cmd_name)`, `command_registry.py:108` `_COMMAND_REGISTRY` |
| 3 | New **commands** are carried by the `custom_command` + `custom_command_help` hooks, *not* by editing `command_line/` | §4.2 hook table (`custom_command` → `True`/`str`/`None`); §12.1 rule 1 "plugins over core" | `command_handler.py:255-280` `callbacks.on_custom_command(...)`; `plugins/pop_command/register_callbacks.py:174` registers both hooks |
| 4 | Plugins must **`return None` when the command isn't theirs** and **fail gracefully** | §12.1 rules 4 & 5; §4.2 "`None` (not mine)" | `pop_command/register_callbacks.py:159` `if name != "pop": return None`; `command_handler.py:283` `except Exception … emit_warning` |
| 5 | UI output goes through the **message bus `emit_*`**, never `print` — so any extracted UI plugin keeps working in TUI + streaming | §11.1 "Plugins emit UI messages through the message bus rather than printing" | `messaging/__init__.py:64-86` exports `emit_info/success/warning/error`; `pop_command` imports them lazily |
| 6 | Streaming-driven UI is observed via the **`stream_event`** hook (observe-only) | §11.2 "The `stream_event` callback lets plugins observe these events" | `callbacks.py:503` `on_stream_event`, `:520` fires `"stream_event"`; `agents/event_stream_handler.py:34/49` consume the bus |
| 7 | The **theme/statusline/context_indicator/wide_completion_menu/subagent_panel/prompt_newline** UI features are *already* plugins → in-core look-alikes should follow the same pattern | §4.1 three-tier plugin discovery; §4.3 minimal plugin example | `plugins/theme/`, `plugins/statusline/`, `plugins/context_indicator/`, `plugins/wide_completion_menu/`, `plugins/subagent_panel/`, `plugins/prompt_newline/` all present |
| 8 | `keymap.py` (cancel/pause keys) is HARNESS-COUPLED — it gates the **live turn's** interrupt path | §7 history/turn control; §2.1 `_runtime.run_with_mcp()` "cancellation" | `agents/_runtime.py:85` `from code_puppy.keymap import cancel_agent_uses_signal`; `agents/_key_listeners.py:21` imports keymap |
| 9 | `terminal_utils.py` is HARNESS — used by BOOT and the turn loop to keep the terminal sane | §1 TUI layer; §2.1 runtime orchestration | `cli_runner.py:43` + `:616/:813/:958` `ensure_ctrl_c_disabled`; `agents/_runtime.py:596` terminal reset |
| 10 | `mcp_prompts/hook_creator.py` is a **prompt string owned by the `hook_creator` plugin** but parked in core → PLUGIN-CANDIDATE | §4.1/§12.1 rule 1 "plugins over core"; §12.2 plugin structure (helpers live *in* the plugin dir) | `plugins/hook_creator/register_callbacks.py:6` `from code_puppy.mcp_prompts.hook_creator import HOOK_CREATION_PROMPT` (the **only** importer) |

---

## 1. Classification Method (inherited from `tp4.1`)

I deliberately **do not** invent a new yardstick. I apply the **Stub-and-Boot
Test** from `tp4.1` (trace importers; ask whether any sits on the
BOOT / LOAD-PLUGINS / NO-OP-TURN liveness path) and the **3-state decision rule**:

| Stubbed module `M` causes… | Classification |
|----------------------------|----------------|
| BOOT raises, plugins don't load, or the REPL can't route a command / complete a turn | **KEEP-IN-CORE** (harness) |
| Nothing on the liveness path breaks; only a *feature* disappears | **PLUGIN-CANDIDATE** |
| Passes only because **another in-harness module silently absorbs its job** (no clean seam yet) | **HARNESS-COUPLED** (extractable, but a seam must be designed first) |

A UI-specific wrinkle: the `command_line/` package is *itself* declared
"do-not-edit / the API surface is the plugin system" (SKILL §12.1 rule 1). So for
this layer the question is sharper: **is this module load-bearing for the REPL/turn,
or is it a self-contained feature that only happens to live under `command_line/`?**

---

## 2. Per-Area Classification

### 2.1 Command dispatch core — **KEEP-IN-CORE**

`command_handler.py` (the dispatcher), `command_registry.py` (the
`@register_command` decorator + `_COMMAND_REGISTRY`), and the import-on-load
command modules are the spine of the REPL. `handle_command()` is what turns a
typed `/foo` into a handler call (`command_handler.py:178`), and it is *also* the
integration point that fires the `custom_command` hook for plugins
(`:255-280`). You cannot stub these without the REPL going deaf.

> **Note for `tp4.6`:** dispatch stays, but it is *also already a hook host*. This
> is the model the rest of this layer should converge on — core owns the router,
> plugins own the commands.

### 2.2 Built-in command handlers — **MIXED** (split by whether they touch the harness)

The `@register_command` handlers fall into two buckets. **Harness-config commands**
(drive things the engine genuinely needs) stay; **feature commands** (self-contained
UX) are PLUGIN-CANDIDATEs that should migrate to the `custom_command` hook.

| Command(s) | File | Verdict | Why |
|------------|------|---------|-----|
| `/help`, `/cd`, `/exit`, `/agent`, `/model`, `/tools`, `/mcp` | `core_commands.py` | **KEEP** | route to agent/model/cwd selection — engine-level (SKILL §2.3, §5.4, §6.3) |
| `/set`, `/show`, `/reasoning`, `/verbosity`, `/pin_model`, `/unpin` | `config_commands.py` | **KEEP** | read/write `puppy.cfg` the engine boots from (SKILL §10) |
| `/clear`, `/compact`, `/truncate`, `/session`, `/dump_context`, `/load_context`, `/autosave_load` | `session_commands.py` | **KEEP** | history/session control the runtime owns (SKILL §7.3–§7.4) |
| `/add_model`, `/model_settings` | `core_commands.py` → `add_model_menu.py` (1211 ln), `model_settings_menu.py` | **HARNESS-COUPLED** | edit model config the engine consumes, but the *menus* are huge & feature-grade |
| `/diff`, `/colors` | `config_commands.py` → `diff_menu.py` (780 ln), `colors_menu.py` (453 ln) | **PLUGIN-CANDIDATE** | pure presentation prefs; `/colors` belongs with the existing **theme** plugin |
| `/generate-pr-description` | `core_commands.py:617` | **PLUGIN-CANDIDATE** | self-contained feature; a **`review_pr`** plugin already exists to absorb it |
| `/paste`, `/tutorial` | `core_commands.py` | **PLUGIN-CANDIDATE** | clipboard paste + onboarding tour are features, not engine |
| `/uc` | `uc_menu.py:874` (745 ln) | **PLUGIN-CANDIDATE** | universal-constructor UX; a **`universal_constructor`** plugin already exists to host it |

### 2.3 Interactive menus & completers — **MIXED**

| Module | Lines | Verdict | Rationale |
|--------|------:|---------|-----------|
| `prompt_toolkit_completion.py` | 831 | **KEEP** (HARNESS) | the REPL input/completion engine cli_runner drives |
| `model_picker_completion.py` | — | **KEEP** | `get_active_model()` is called from the dispatcher (`command_handler.py`) and runtime |
| `file_path_completion.py`, `file_index.py`, `skills_completion.py`, `mcp_completion.py`, `load_context_completion.py`, `pin_command_completion.py` | — | **KEEP** | completers wired into the live prompt session |
| `set_menu*.py` (7 files) | — | **KEEP** | `/set` catalog drives engine config; tightly bound to `config.py` |
| `colors_menu.py` | 453 | **PLUGIN-CANDIDATE** | → fold into **theme** plugin (already external) |
| `diff_menu.py` | 780 | **PLUGIN-CANDIDATE** | diff-rendering preference UX |
| `judges_menu.py`, `autosave_menu.py`, `agent_menu.py`, `mcp_binding_menu.py`, `uc_menu.py` | — | **HARNESS-COUPLED** | feature menus, but reach into agent/mcp/config singletons; need a seam first |
| `onboarding_wizard.py` (299), `onboarding_slides.py` (144) | 443 | **PLUGIN-CANDIDATE** | first-run UX; carry via the **`startup`** hook with a "first run only" guard |
| `clipboard.py`, `attachments.py` | — | **HARNESS-COUPLED** | input plumbing used by the prompt session; extractable but seam-bound |
| `shell_passthrough.py` | 112 | **KEEP** | the `!cmd` REPL escape — dispatcher-adjacent |
| `pagination.py`, `utils.py`, `wiggum_state.py` | — | **KEEP** | tiny shared REPL helpers |

### 2.4 `messaging/` — **KEEP-IN-CORE** (the UI transport, deeply harness-wired)

The whole `messaging/` package is the bus that **every agent module already depends
on**: `agents/_builder.py:37`, `_runtime.py:86`, `_compaction.py:44`,
`event_stream_handler.py:34/49`, `agent_manager.py:18`, etc. all import `emit_*` /
`get_session_context` / `pause_controller`. Stub it and the no-op turn can't report
anything — it *is* the SKILL §11 "Messaging & UI" engine. **KEEP.**

Internal note for `tp4.6`: `rich_renderer.py` is **1,190 lines** (verified
`Get-Content | Measure-Object -Line`) — **≈2× the 600 cap** (SKILL §12.1 rule 3).
That's an *internal split* problem, **not** an extraction one (it's load-bearing).
`bus.py` (498) and `subagent_console.py` (369) are within cap.

> **Missing-hook observation:** renderers are **not** pluggable. There is a
> `stream_event` *observe* hook but no `register_renderer` hook to swap the
> presentation layer. The **theme** plugin works around this by patching styles.
> Flagged in §3; likely YAGNI unless a real alt-renderer demand appears.

### 2.5 `status_display.py` — **PLUGIN-CANDIDATE** (and partly orphaned)

273 lines. The `StatusDisplay` Live-panel class is **not instantiated anywhere in
the runtime** — `rg status_display` / `StatusDisplay(` returns only the class
definition, its own internal `tool_execution()` self-reference, and the
**statusline plugin** reading the *classmethod* `StatusDisplay.get_current_rate()`
(`plugins/statusline/payload.py:79-81`). So the only live consumer is already a
plugin, and it only needs the global token-rate, not the panel. This is the
cleanest UI extraction win in the layer: move the rate-tracking into the
statusline plugin (or a small shared `stream_event` consumer) and **delete the
dead Live-panel UI**. LOW risk because nothing on the liveness path imports it.

### 2.6 `terminal_utils.py` — **KEEP-IN-CORE** (HARNESS)

388 lines. Imported by `cli_runner.py` during BOOT (`:43`) and the cancel/cleanup
path (`:616/:813/:958`) and by `agents/_runtime.py:596` mid-turn. Cross-platform
terminal-sanity (Windows console mode resets, Ctrl-C disabling) is exactly the
kind of "engine plumbing" SKILL §1/§2.1 keeps in core. **KEEP.**

### 2.7 `keymap.py` — **HARNESS-COUPLED**

186 lines. Defines cancel/pause keybindings consumed by `cli_runner.py:35`,
`agents/_runtime.py:85`, and `agents/_key_listeners.py:21` — i.e. the **live
turn's interrupt path** (SKILL §2.1 "cancellation"). The *data* (key tables,
validation) is feature-grade and config-driven, but the runtime imports it
directly with no seam. Extractable only if a `register_keymap`-style seam is
designed; until then it travels with the harness. **Recommend KEEP for now.**

### 2.8 `list_filtering.py` — **KEEP-IN-CORE** (trivial shared util)

18 lines (verified). Two pure functions used by `add_model_menu.py:28` and
`model_picker_completion.py:21`. **YAGNI**: plugin-izing an 18-line dependency-free
helper adds a tier-loading indirection for zero benefit (SKILL §12.3 "simple is
better than complex"). Leave it; if `colors_menu`/`add_model_menu` ever leave, they
can keep importing this shared util from core.

### 2.9 `mcp_prompts/hook_creator.py` — **PLUGIN-CANDIDATE** (misfiled)

85 lines. It is a single `HOOK_CREATION_PROMPT` string whose **only** importer is
`plugins/hook_creator/register_callbacks.py:6`. Per SKILL §12.2 (a plugin's
helpers live *inside* the plugin directory), this prompt should move into the
`hook_creator` plugin. The `mcp_prompts/` package then disappears entirely. LOW
risk — one importer, one string, no runtime coupling.

---

## 3. Hook Coverage — what carries each candidate, and the gaps

| Candidate | Carrying hook(s) | Coverage |
|-----------|------------------|----------|
| Feature commands (`/colors`, `/diff`, `/paste`, `/tutorial`, `/generate-pr-description`, `/uc`) | `custom_command` + `custom_command_help` | COVERED — proven (see `pop_command`, `review_pr`) |
| `status_display` token-rate | `stream_event` (observe deltas) + statusline | COVERED — adequate |
| Onboarding wizard/slides | `startup` (with first-run guard) | COVERED — adequate |
| `mcp_prompts/hook_creator` | n/a — just relocate the string into the plugin | COVERED — no hook needed |
| Alt renderers (theme-beyond-styles) | — | **GAP** — no `register_renderer` hook (only observe-only `stream_event`) |
| Rich plugin commands (aliases/categories/`detailed_help`) | `custom_command_help` returns only `(name, desc)` | **ASYMMETRY** (see below) |

### 3.1 Flagged gap — command-registration asymmetry

Built-in commands get a **rich** `CommandInfo` (`usage`, `aliases`, `category`,
`detailed_help`; `command_registry.py:14-40`) and auto-formatted help. Plugin
commands get a **flat** path: a `(name, description)` tuple via
`custom_command_help` and hand-rolled `if name != "x": return None` dispatch
inside `custom_command` (`command_handler.py`'s two-path design,
`pop_command:159`). There is **no hook to register a first-class command** (with
aliases/category/detailed help) into `_COMMAND_REGISTRY`. This is the single most
relevant missing-hook finding for migrating §2.2's feature commands cleanly —
without it, extracted commands become second-class citizens in `/help`.

> **Proposed (do NOT build here):** a `register_commands` hook returning
> `list[CommandInfo]`-shaped dicts that the registry ingests, giving plugin
> commands parity with built-ins. This is the enabler for most of §2.2.

### 3.2 Flagged gap — renderer pluggability

`stream_event` is observe-only; there is no seam to *replace* presentation. Real
but likely **YAGNI** until a concrete alternate-renderer use case shows up (the
theme plugin's style-patching covers the common case). Recorded for `tp4.6`
completeness, not recommended as priority work.

---

## 4. Risk / Effort Summary

| Candidate | Risk | Effort | Notes |
|-----------|:----:|:------:|-------|
| `mcp_prompts/hook_creator` → `hook_creator` plugin | **LOW** | **LOW** | 1 importer, 1 string; delete `mcp_prompts/` |
| `status_display` rate → statusline plugin; delete Live panel | **LOW** | **LOW–MED** | only live consumer is already a plugin; remove dead UI |
| `/colors` + `colors_menu.py` → **theme** plugin | **LOW** | **MED** | theme plugin already external; 453-line menu to relocate |
| `/generate-pr-description` → **review_pr** plugin | **LOW** | **LOW** | self-contained; home already exists |
| `/paste`, `/tutorial`, onboarding → plugin(s) | **MED** | **MED** | needs first-run `startup` guard; clipboard plumbing coupling |
| `/diff` + `diff_menu.py` → plugin | **MED** | **MED** | 780-line menu; config seam needed |
| `register_commands` hook (enabler) | **MED** | **MED** | touches `command_registry`; must not regress `/help` |
| `keymap.py` extraction | **HIGH** | **HIGH** | live-turn interrupt path; needs a keymap seam first — **defer** |
| `messaging/` extraction | **N/A** | — | KEEP — it *is* the UI engine |
| `rich_renderer.py` 1,190-line internal split | **MED** | **MED** | hygiene (SKILL §12.1 rule 3), **not** extraction |

---

## 5. Must-NEVER-Extract (UI harness floor)

These keep the REPL alive and the turn observable; stubbing any breaks BOOT or the
no-op turn:

- `command_handler.py` / `command_registry.py` — slash routing + hook host.
- `prompt_toolkit_completion.py` + the live completers — the input engine.
- `messaging/` (bus, message_queue, renderers, pause_controller, spinner,
  subagent_console) — the UI transport every agent module imports.
- `terminal_utils.py` — cross-platform terminal sanity on BOOT and turn.
- Harness-config commands: `/help`, `/cd`, `/exit`, `/agent`, `/model`, `/set`,
  `/clear`, `/compact`, `/truncate`, `/session`.

---

## 6. Proposed Follow-Up Beads (research only — **none built here**)

1. **Relocate `mcp_prompts/hook_creator.py` into the `hook_creator` plugin**;
   delete the `mcp_prompts/` package. (LOW/LOW — the layup.)
2. **Extract `status_display` token-rate into the statusline plugin** and remove
   the orphaned Live-panel UI. (LOW)
3. **Add a `register_commands` hook** giving plugin commands `CommandInfo` parity
   (aliases/category/detailed help) — the enabler for §2.2. (MED)
4. **Move `/colors` + `colors_menu.py` into the existing `theme` plugin.** (MED;
   depends on bead 3 for clean command registration.)
5. **Move `/generate-pr-description` into the existing `review_pr` plugin.** (LOW;
   depends on bead 3.)
6. **Extract onboarding (`/tutorial`, wizard, slides) into an `onboarding`
   plugin** via the `startup` hook + first-run guard. (MED)
7. **Internal split of `rich_renderer.py` (1,190 ln)** into sub-renderers to honor
   the 600-line cap. (MED; hygiene, stays in core.)
8. **Spike a `register_renderer` seam** *only if* a concrete alternate-renderer
   demand materializes. (Parked / likely YAGNI.)

---

## 7. Findings for the Synthesis Bead (`tp4.6`)

- **The dispatcher is already a hook host.** `command_handler.handle_command()`
  routes built-ins *and* fires `custom_command`. The target end-state for this
  layer is: **core owns the router + transport; plugins own the commands + chrome.**
- **The cleanest UI wins are tiny and obvious:** the misfiled `mcp_prompts`
  prompt and the orphaned `status_display` Live panel (its only live consumer is
  already a plugin). Both are LOW/LOW.
- **The real blocker is an asymmetry, not a module:** plugin commands are
  second-class (`(name, desc)` tuples + manual dispatch) vs. built-ins' rich
  `CommandInfo`. A **`register_commands` hook** is the keystone that makes the
  §2.2 command migrations clean; sequence it *before* the per-command extractions.
- **`messaging/` is the UI engine — do not extract it.** Its only sin is
  `rich_renderer.py` being ≈2× the line cap; that's an internal split.
- **`keymap.py` is the one HIGH-risk trap:** it looks like config-data but sits on
  the live-turn interrupt path with no seam. Defer until a keymap seam is designed.
- **Don't over-extract:** `list_filtering.py` (18 ln) and the live completers stay
  — plugin-izing them is pure indirection (SKILL §12.3 "simple is better than
  complex").
