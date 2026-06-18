# `tools/` Layer — Plugin Extraction Audit (`puppy-tp4.3`)

> **Type:** DISCOVERY spike — *research only, zero implementation.* No code was
> moved or refactored. This document classifies modules and **proposes** follow-up
> beads; it does not build them.
>
> **Scope (the brief):** `tools/` — `file_modifications.py`, `file_operations.py`,
> `command_runner.py`, `image_tools.py`, `browser/`, `ask_user_question/`,
> `model_tools.py`, `skills_tools.py`, `subagent_invocation.py`,
> `universal_constructor.py`, `agent_tools.py` (plus the shared support modules
> `__init__.py`, `common.py`, `subagent_context.py`, `display.py`). The question:
> which tools could be *supplied by plugins* via `register_tools` /
> `register_agent_tools` instead of being hardcoded in `TOOL_REGISTRY`, and which
> are harness-essential?
>
> **Sibling of:** `tp4.1` (harness boundary), `tp4.2` (model/provider),
> `tp4.4` (UI/command_line), `tp4.5` (services). Feeds synthesis bead **`tp4.6`**.

---

## 0. Architecture Grounding (REQUIRED FIRST STEP — evidenced)

Per the bead's hard contract and the `skill-grounding-must-be-evidenced` memory:
the `code-puppy-agent` architecture skill was **activated first**, and each
classification decision below is tied to the *specific* SKILL.md section that
constrains it — each cross-checked against a **live source line I actually read**.

| # | Decision in this audit | SKILL.md section | Live source evidence I read |
|---|------------------------|------------------|------------------------------|
| 1 | Reuse `tp4.1`'s **Stub-and-Boot Test** + 3-state rule (HARNESS / PLUGIN-CANDIDATE / **HARNESS-COUPLED**) rather than invent a new yardstick | §12.3 "the plugin system is the API surface; the core is the engine" | `docs/HARNESS_BOUNDARY_CRITERIA.md` §1–§2 (Stub-and-Boot + decision rule); `tp4.1` already classed `file_operations.py` = "REMOVABLE-impl behind a HARNESS binder" |
| 2 | `TOOL_REGISTRY` (the name→`register_func` map) + `register_tools_for_agent()` is the **HARNESS binder** — it stays; leaf impls can move | §3.1 "A flat dict mapping tool-name strings to registration functions"; §3.2 build-time registration | `tools/__init__.py:91` `TOOL_REGISTRY = {…}`, `:263` `register_tools_for_agent`, `:497` `_builder.py` calls it |
| 3 | A plugin **defines** a tool via `register_tools` (merged into `TOOL_REGISTRY`) and **advertises** it via `register_agent_tools` — both hooks needed | §3.4 "Step 1 makes the tool *exist*; step 2 makes agents *see* it. Both are needed" | `tools/__init__.py:172` `_load_plugin_tools()` → `TOOL_REGISTRY[name]=func`; `:286` `on_register_agent_tools(agent_name)` union |
| 4 | The **canonical working proof** of full tool extraction is `puppy_kennel` — it ships 5 tools entirely from a plugin | §3.4 plugin-tools two-hook pattern; §4.3 minimal plugin | `plugins/puppy_kennel/register_callbacks.py:82` `register_tools` + `:93` `register_agent_tools` (advertises 5 kennel tools to every agent) |
| 5 | Tools that already host **hooks** (`file_permission`, `edit_file`, `delete_file`, `run_shell_command`) are HARNESS binders, not candidates | §4.2 hook table (`pre_tool_call`, `file_permission`, `run_shell_command`) | `file_modifications.py:423/453/484/601` `on_file_permission(...)`, `:700` `on_edit_file`; `command_runner.py:930` `on_run_shell_command` |
| 6 | "Fail gracefully / return None" governs plugin tool loading — a bad plugin tool must not break core | §12.1 rules 4 & 5 | `tools/__init__.py:189-191` `except Exception: pass` around `_load_plugin_tools` |
| 7 | UC is **already half-plugin**: the core tool imports the plugin's models, and a `universal_constructor` plugin exists | §3.3 "UC: `universal_constructor` (dynamic tool factory)"; §4.1 builtin tier | `universal_constructor.py:18` `from code_puppy.plugins.universal_constructor.models import …`; `plugins/universal_constructor/register_callbacks.py:45` `register_callback("startup", …)` |
| 8 | Skills tools are **already half-plugin**: the core tool imports all logic from `plugins/agent_skills` | §8.2 skill discovery lives in `plugins/agent_skills/`; §3.3 "Skills: `activate_skill`…" | `skills_tools.py:47-50` `from code_puppy.plugins.agent_skills.{config,enabled_skills,metadata} import …` |
| 9 | Per-model conditional tool stripping (`agent_share_your_reasoning` under extended thinking) is engine policy → its host stays HARNESS | §5.4 model selection; §9 prompt assembly (extended-thinking note) | `tools/__init__.py:204` `has_extended_thinking_active`, `EXTENDED_THINKING_PROMPT_NOTE` |
| 10 | Don't over-extract: tiny shared utils used across the binder stay in core | §12.3 "Simple is better than complex / Flat is better than nested" | `tools/common.py` `generate_group_id` imported by ~8 tool modules; `subagent_context.is_subagent` imported by `messaging/spinner` |

---

## 1. Classification Method (inherited from `tp4.1`)

I deliberately **do not** invent a new yardstick. I apply the **Stub-and-Boot
Test** from `tp4.1` (trace importers; ask whether any sits on the
BOOT / LOAD-PLUGINS / NO-OP-TURN liveness path) and the **3-state decision rule**:

| Stubbed module `M` causes… | Classification |
|----------------------------|----------------|
| BOOT raises, plugins don't load, or the runtime can't complete a turn / cancel / take user input | **KEEP-IN-CORE** (harness) |
| Nothing on the liveness path breaks; only a *tool capability* disappears | **PLUGIN-CANDIDATE** |
| Passes only because **another in-harness module silently absorbs its job** (no clean seam yet) | **HARNESS-COUPLED** (extractable, but a seam must be designed first) |

A tools-specific wrinkle: a `tools/` module wears **two hats** — (a) the *tool
impl* the LLM calls, and (b) sometimes *runtime plumbing* imported by `agents/` or
`cli_runner.py`. A module is only a clean PLUGIN-CANDIDATE when **hat (b) is
empty** — i.e. nothing outside `tools/__init__.py` imports its internals.

---

## 2. Per-Module Classification

### 2.1 `file_operations.py` (list_files / read_file / grep) — **KEEP** (default toolset)
933 lines. The impl is technically removable (`tp4.1` flagged it
"REMOVABLE-impl behind a HARNESS binder"), but these three are in **every
builtin agent's hardcoded `get_available_tools()`** (`agent_code_puppy.py:27-29`).
Its only non-binder importer is `tools/common.py` (shared `DIR_IGNORE_PATTERNS`,
`:730`). Extraction is *possible* but pure churn — the read/list/grep triad is the
irreducible default capability, and `register_agent_tools` can only **add**, not
remove, so you'd gain nothing. **KEEP.** (See §3.2 GAP-2.)

### 2.2 `file_modifications.py` (create / replace_in_file / delete_snippet / delete_file / edit_file) — **KEEP** (HARNESS hook host)
935 lines. Already a **hook host**, not a candidate: it fires `on_file_permission`
four times (`:423/453/484/601`) and `on_edit_file`/`on_delete_file` (`:700/770/
891/928`). This is exactly the SKILL §4.2 seam pattern — core owns the guarded
write, plugins (`file_permission_handler`) decide allow/deny. **KEEP.**
> **Smell for `tp4.6` (not a bug):** `:51` and `:180` import *directly* from
> `plugins.file_permission_handler.register_callbacks` — a **core→plugin import**
> that inverts the dependency arrow (§12.1 rule 1). The clean path is to reach the
> handler only through the `on_file_permission` hook, which it already does at
> `:426`. Worth a hygiene bead.

### 2.3 `command_runner.py` (agent_run_shell_command / agent_share_your_reasoning) — **HARNESS-COUPLED → KEEP**
1,319 lines (≈2.2× the 600 cap, §12.1 rule 3). The *tool* fires the
`on_run_shell_command` block hook (`:930`, honors `{"blocked": True}` only — it
**cannot rewrite**, per the `pretoolcall-rewrites-shellhook-blocks` memory). But
the module also hosts **live-turn plumbing**: `_RUNNING_PROCESSES` and
`_tear_down_live_panels` (consumed by `agents/_run_signals.py:34/48` on the cancel
path), `is_awaiting_user_input` (`cli_runner.py:468`, `agents/_runtime.py:87`),
and `set_awaiting_user_input` (`common.py:1140/1337`, `ask_user_question/
terminal_ui.py:317`). Hat (b) is heavy. **KEEP** — but it's the prime internal-split
target.

### 2.4 `image_tools.py` (load_image_for_analysis) — **PLUGIN-CANDIDATE**  clean
192 lines. Self-contained: imports only `messaging.emit_*` + `common.generate_group_id`
(`:22`). No `agents/`/`cli_runner.py` importer (hat (b) empty). It's an optional
multimodal capability used by browser/QA agents. Drops straight into a plugin via
`register_tools` + `register_agent_tools`. **LOW / LOW** — the textbook layup.

### 2.5 `browser/` (30+ `browser_*` tools, Playwright) — **PLUGIN-CANDIDATE**  (big, optional)
~3,001 lines across 9 files (largest: `browser_locators.py` 640, `browser_interactions.py`
545, `browser_scripts.py` 462). A cohesive, **optional** feature with a heavy
external dep (Playwright). Per SKILL §3.3 it's already siloed. The one tether:
`subagent_invocation.py:122/410` imports `browser.browser_manager` to clean up
browser sessions when a sub-agent ends — so extraction needs a small lifecycle
**seam** (e.g. a `shutdown`/`agent_run_end` hook the browser plugin owns) instead
of a direct import. **MED / MED** (size + the cleanup seam), but high value: it
removes Playwright from the core install surface.

### 2.6 `ask_user_question/` — **HARNESS-COUPLED → KEEP**
~2,107 lines across 10 files. It *is* a tool, but it sits on the **user-input
liveness path**: `terminal_ui.py:317` calls `command_runner.set_awaiting_user_input`
to coordinate with the turn loop's interrupt handling. Stubbing it breaks
interactive turns, not just a feature. The clean default-interaction primitive
belongs in the harness. **KEEP** (defer; revisit only if a headless seam appears).

### 2.7 `model_tools.py` (list_available_models) — **PLUGIN-CANDIDATE** 
105 lines. Reads `model_factory`/`config` (read-only metadata projection) +
`common.generate_group_id`. No hat (b). A self-contained discovery tool — easily
plugin-supplied. **LOW / LOW.** (Mild caveat: it reads core model config, so the
plugin would import `config`, which is fine — plugins read config routinely.)

### 2.8 `skills_tools.py` (activate_skill / list_or_search_skills) — **PLUGIN-CANDIDATE**  (split-brain)
227 lines. **Already half-plugin:** every code path imports its logic from
`plugins/agent_skills/{config,enabled_skills,metadata}` (`:47-50`). The tool
registration is the *only* part still parked in core. Per SKILL §12.2 (a plugin's
tools live *in* the plugin dir), these two `register_*` functions should move into
the `agent_skills` plugin and be supplied via `register_tools` +
`register_agent_tools`. **LOW / MED** — caveat: both names are hardcoded in builtin
agents' `get_available_tools()` (`agent_code_puppy.py:36-37`), so the plugin must
re-advertise them under the same names (works — see §3.2 GAP-2/3).

### 2.9 `subagent_invocation.py` (invoke_agent / invoke_agent_with_model) — **HARNESS-COUPLED → KEEP**
506 lines. This is **agent-orchestration**, not a leaf tool: it calls
`agent_manager`, re-enters `register_tools_for_agent` (`:246`), reads
`subagent_context` (`:37`), and drives the browser cleanup (`:122`). It's the
in-process embodiment of SKILL §2.3 (the agent manager) exposed as a tool.
Stubbing it kills sub-agent dispatch. **KEEP.**

### 2.10 `universal_constructor.py` (universal_constructor) — **PLUGIN-CANDIDATE**  (split-brain + special-cased)
893 lines. **Already half-plugin:** the core tool imports all its output models
from `plugins/universal_constructor.models` (`:18`), and a `universal_constructor`
plugin already exists (currently registering only a `startup` hook). Worse, the
binder carries a **hardcoded special case** for it: `tools/__init__.py:330`
`if tool_name == "universal_constructor" and not get_universal_constructor_enabled()`
— a textbook Zen violation ("special cases aren't special enough to break the
rules"). Once UC is a plugin tool, that gate moves *into the plugin* (return `[]`
from `register_agent_tools` when disabled, exactly like `puppy_kennel` does at
`register_callbacks.py:84`), and the core special-case **deletes itself**.
**MED / MED** — the cleanest "remove a wart while extracting" win in this layer.

### 2.11 `agent_tools.py` (list_agents) — **HARNESS-COUPLED → KEEP**
295 lines. The `list_agents` tool is small, but the module also owns
`_active_subagent_tasks`, imported by `agents/_run_signals.py:15` on the
**cancellation path**. Hat (b) is non-empty → can't cleanly leave without a seam
for the running-task registry. **KEEP.**

### 2.12 Shared support modules — **KEEP** (binder + utils)
| Module | Lines | Verdict | Why |
|--------|------:|---------|-----|
| `__init__.py` (`TOOL_REGISTRY`, `register_tools_for_agent`) | 475 | **KEEP** (the binder) | §3.1/§3.2 — the harness seam itself; `_builder.py:465/556` depends on it |
| `common.py` | 1,599 | **KEEP** but **≈2.7× cap** | shared helpers (`generate_group_id`, `atomic_write_text`, `get_user_approval_async`) imported by ~8 tool modules — **internal split**, not extraction |
| `subagent_context.py` | 158 | **KEEP** | `is_subagent` is cross-cutting — imported by `messaging/spinner/__init__.py:36/61`, `command_runner`, `display` |
| `display.py` | 91 | **KEEP** | `display_non_streamed_result` consumed by `agents/_non_streaming_render.py:30` |
| `subagent_context`/`tools_content.py` (50) | — | **KEEP** | tiny shared data; §12.3 YAGNI to plugin-ize |

---

## 3. Hook Coverage — can `register_tools` / `register_agent_tools` supply each candidate?

| Candidate | `register_tools` defines it? | `register_agent_tools` advertises it? | Coverage |
|-----------|:---:|:---:|----------|
| `image_tools` | YES (trivial) | YES (universal, like kennel) | **FULLY COVERED** — proven by `puppy_kennel` |
| `model_tools` | YES | YES | **FULLY COVERED** |
| `skills_tools` | YES (logic already in `agent_skills`) | YES (re-advertise same names) | **COVERED** w/ GAP-2/3 caveat |
| `universal_constructor` | YES (models already in plugin) | YES (self-gate replaces core special-case) | **COVERED** — removes a wart |
| `browser/` | YES (bulk define) | YES (per-agent: only browser/QA agents) | **COVERED** w/ lifecycle-seam gap (§3.1) |

The two hooks are **sufficient to define and route** every candidate above —
`puppy_kennel` is living proof (5 tools, zero core edits). The gaps below are about
*removal, gating, and lifecycle*, not definition.

### 3.1 GAP — no tool lifecycle / cleanup hook
`browser_manager` cleanup is reached by a **direct import** from
`subagent_invocation.py`, not a hook. There is no `register_tools`-adjacent way for
a tool plugin to say "run this when a sub-agent / session ends." Browser
extraction needs the plugin to hang cleanup on `agent_run_end` (or a new
`teardown_tools` hook). **Flag for `tp4.6`.**

### 3.2 GAP (keystone) — `register_agent_tools` is **additive only**; agents hardcode tool names
`on_register_agent_tools` **unions** names into the list (`tools/__init__.py:286-294`)
— it can add but **never remove**. Meanwhile builtin agents hardcode tool *names*
in `get_available_tools()` (`agent_code_puppy.py:24-38`). Consequence: extracting a
tool that's in a hardcoded list (skills, file ops) only works if the plugin
**re-advertises the same name** so the binder still resolves it; otherwise
`register_tools_for_agent` emits `"Unknown tool … skipping"` (`:309`). So
"extraction" of a default-listed tool is really "**move the impl + re-advertise**,"
not "remove from core." There is **no `unregister`/override-precedence** mechanism —
`_load_plugin_tools` does a silent last-writer-wins `TOOL_REGISTRY[name]=func`
(`:185`). **This is the single most important finding for `tp4.6`'s sequencing.**

### 3.3 GAP — per-tool config gating is open-coded in the binder
UC's `get_universal_constructor_enabled()` check is hardcoded in core
(`tools/__init__.py:315/332`) instead of living in the owning plugin. The plugin
pattern (`register_agent_tools` returns `[]` when disabled — `puppy_kennel:84`) is
strictly better and removes the special case. No new hook needed; just migrate the
gate with the tool.

### 3.4 GAP — compound/expansion tools are core-only
`TOOL_EXPANSIONS` (`edit_file` → 3 tools, `tools/__init__.py:165`) is a core dict; a
plugin can't register an expansion tool. Minor / likely **YAGNI** — recorded for
completeness.

---

## 4. Risk / Effort Summary

| Candidate | Risk | Effort | Notes |
|-----------|:----:|:------:|-------|
| `image_tools` → plugin | **LOW** | **LOW** | self-contained; textbook layup |
| `model_tools` → plugin | **LOW** | **LOW** | read-only config; clean |
| `skills_tools` → `agent_skills` plugin | **LOW** | **MED** | split-brain; re-advertise hardcoded names (GAP-2/3) |
| `universal_constructor` → `universal_constructor` plugin | **MED** | **MED** | split-brain; **deletes** core special-case (GAP-3) |
| `browser/` → plugin | **MED** | **MED** | 3k lines + Playwright dep; needs cleanup seam (GAP-1) |
| `file_operations` extraction | **MED** | **HIGH** | default triad; net-zero value (additive-only, GAP-2) — **don't** |
| `command_runner` extraction | **HIGH** | **HIGH** | live-turn cancel + input plumbing — **KEEP** |
| `ask_user_question` extraction | **HIGH** | **HIGH** | user-input liveness path — **KEEP** |
| `agent_tools` / `subagent_invocation` | **HIGH** | — | orchestration + cancel registry — **KEEP** |
| `common.py` 1,599-line internal split | **MED** | **MED** | hygiene (§12.1 rule 3), **not** extraction |
| `command_runner.py` 1,319-line internal split | **MED** | **MED** | hygiene, **not** extraction |
| `register_agent_tools` removal/override seam (GAP-2) | **MED** | **MED** | enabler for clean default-tool extraction |

---

## 5. Must-NEVER-Extract (tools harness floor)

Stubbing any of these breaks BOOT, the no-op turn, cancellation, or user input:

- `tools/__init__.py` — `TOOL_REGISTRY` + `register_tools_for_agent()`, the binder
  every agent build calls (`_builder.py:465/556`).
- `command_runner.py` — shell tool **and** `_RUNNING_PROCESSES`/`is_awaiting_user_input`
  on the cancel/input path (`_run_signals.py`, `_runtime.py`, `cli_runner.py`).
- `file_modifications.py` / `file_operations.py` — the default read/write toolset +
  the `file_permission`/`edit_file` hook host.
- `ask_user_question/` — the interactive-input primitive coordinated with the turn loop.
- `subagent_invocation.py` / `agent_tools.py` — sub-agent dispatch + running-task registry.
- `common.py`, `subagent_context.py`, `display.py` — shared plumbing imported by
  `agents/` and `messaging/`.

---

## 6. Proposed Follow-Up Beads (research only — **none built here**)

1. **Extract `image_tools` into a plugin** via `register_tools` +
   `register_agent_tools`. (LOW/LOW — the layup; do first.)
2. **Extract `model_tools` (`list_available_models`) into a plugin.** (LOW/LOW.)
3. **Move `skills_tools` registration into the existing `agent_skills` plugin**
   (logic is already there); re-advertise `activate_skill`/`list_or_search_skills`
   under the same names. (LOW/MED — depends on bead 6.)
4. **Move `universal_constructor` tool into the existing `universal_constructor`
   plugin** and migrate its enable-gate into `register_agent_tools`, **deleting**
   the core `if tool_name == "universal_constructor"` special-case. (MED.)
5. **Extract `browser/` into a `browser` plugin** (per-agent advertisement) and add
   a tool-cleanup seam (GAP-1) so `subagent_invocation` stops importing
   `browser_manager`. (MED.)
6. **Add a `register_agent_tools` removal/override mechanism (GAP-2)** — or a
   documented "re-advertise" contract — so default-listed tools can leave core
   without breaking hardcoded agent lists. (MED — sequence *before* beads 3–5.)
7. **Internal split of `common.py` (1,599 ln) and `command_runner.py` (1,319 ln)**
   to honor the 600-line cap. (MED; hygiene, stays in core.)
8. **Hygiene: stop `file_modifications.py` importing
   `plugins.file_permission_handler` directly** — reach it only through the
   `on_file_permission` hook. (LOW; §1 dependency-arrow inversion.)
9. **Spike a `teardown_tools` hook** *only if* GAP-1 isn't covered by reusing
   `agent_run_end`. (Parked / likely YAGNI.)

---

## 7. Findings for the Synthesis Bead (`tp4.6`)

- **`TOOL_REGISTRY` + `register_tools_for_agent` is a binder, and the binder
  already works.** `puppy_kennel` ships 5 tools from a plugin with zero core
  edits — so "tools can be plugins" is *proven*, not theoretical. The audit is
  about *which* leaves are clean to move, not *whether* the seam exists.
- **Two tools are already half-extracted (split-brain):** `skills_tools` (logic in
  `plugins/agent_skills`) and `universal_constructor` (models in
  `plugins/universal_constructor`). Finishing those is low-risk and *removes* core
  weight — UC's migration even deletes a hardcoded special-case.
- **The cleanest brand-new wins are `image_tools` and `model_tools`** — small,
  self-contained, no runtime importers. LOW/LOW.
- **`browser/` is the biggest payoff** (≈3k lines + Playwright off the core install
  surface) but needs a lifecycle seam first (GAP-1).
- **The keystone blocker is an asymmetry, not a module (GAP-2):** `register_agent_tools`
  can only *add* tools, while builtin agents *hardcode* tool names. So any
  default-listed tool must be "moved + re-advertised under the same name," and
  there's no removal/override precedence. **Sequence the GAP-2 mechanism before the
  default-tool migrations.**
- **The harness floor is bigger than it looks** because half of `tools/` wears a
  second hat: `command_runner`, `agent_tools`, `subagent_invocation`, and
  `ask_user_question` all sit on the cancel/input/orchestration paths and must stay.
- **Two over-cap hygiene items rode along:** `common.py` (1,599 ln) and
  `command_runner.py` (1,319 ln) need internal splits — but they **KEEP**; that's
  §12.1 rule 3, not extraction.
