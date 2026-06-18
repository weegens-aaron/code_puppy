# Core→Plugin Extraction Audit — Synthesis (`puppy-tp4.6`)

> **Type:** DISCOVERY **synthesis** spike — *research only, zero implementation.*
> No code was moved or refactored. This is the **JOIN bead** for epic `puppy-tp4`:
> it consumes the harness-boundary criteria and all four subsystem audits,
> reconciles them into a **single ranked extraction backlog**, names the
> **harness-must-keep floor**, lists the **hook-surface gaps that block clean
> extraction** (each as a *proposed* follow-up bead), and enumerates the
> **proposed implementation epics/beads** (titles + 1-line scope) for the chosen
> candidates. It builds none of them.
>
> **Parent epic:** `puppy-tp4` — Core to Plugin Extraction Audit (DISCOVERY only).
> **Inputs consumed (JOIN):**
> - `puppy-tp4.1` — `docs/HARNESS_BOUNDARY_CRITERIA.md` (the Stub-and-Boot test + the harness floor)
> - `puppy-tp4.2` — `docs/MODEL_PROVIDER_LAYER_EXTRACTION_AUDIT.md` (model/provider layer)
> - `puppy-tp4.3` — `docs/TOOLS_LAYER_EXTRACTION_AUDIT.md` (`tools/` layer)
> - `puppy-tp4.4` — `docs/UI_LAYER_EXTRACTION_AUDIT.md` (UI / `command_line` / messaging)
> - `puppy-tp4.5` — `docs/SERVICES_LAYER_EXTRACTION_AUDIT.md` (services / config / mcp / hook_engine)
>
> **Feeds:** ADR `puppy-1ng` (the decider). The ADR's *other* input is the
> externalization synthesis `puppy-27g.4`
> (`docs/PLUGIN_EXTERNALIZATION_SYNTHESIS.md`). **This doc decides *what leaves
> core*; `27g.4` decides *how externalized plugins are shipped, overridden, and
> updated*. The ADR ratifies both.**

---

## 0. Architecture Grounding (REQUIRED FIRST STEP — evidenced)

Per the bead's hard acceptance contract and the `skill-grounding-must-be-evidenced`
memory: the `code-puppy-agent` architecture skill was **activated first**, and
each synthesis decision below is tied to the *specific* SKILL.md section that
constrains it. A synthesis that ignored the skill would just be averaging four
opinions; grounding it makes the ranking *fit the real architecture*.

| # | Synthesis decision (this doc) | SKILL.md section it rests on | How it shapes the synthesis |
|---|-------------------------------|-----------------------------|-----------------------------|
| 1 | Adopt `tp4.1`'s **Stub-and-Boot Test** + the 3-state rule (HARNESS / PLUGIN-CANDIDATE / **HARNESS-COUPLED**) as the single shared yardstick — do not re-classify, only rank | §12.3 "the plugin system is the API surface; the core is the engine" | All four audits already used this rule, so the candidates compose into one list without translation |
| 2 | Rank by **value vs risk**, where "clean leaf behind a thin binder" = highest value/lowest risk and "HARNESS-COUPLED, needs a seam" = lower until the seam exists | §12.3 Zen "simple is better than complex / flat is better than nested"; §12.1 rule 4 "fail gracefully" | The ranking front-loads the zero-seam layups and defers seam-gated work |
| 3 | The recurring extraction shape is **thin binder stays / externalizable leaves move** — treat it as the default pattern across model, tools, UI, services | §3.1/§3.2 (`TOOL_REGISTRY` + `register_tools_for_agent`); §5.1 (`ModelFactory.get_model`); §4.2 hook table | Confirmed by *all four* audits; it's the unifying thesis (§1) |
| 4 | "Providers/tools/commands as plugins" is **PROVEN, not theoretical** — count the shipping plugins that already do it with zero core dispatch edits | §3.4 two-hook tool pattern; §5.3 `register_model_type`; §4.2 `custom_command` | 6 provider plugins + `puppy_kennel` (5 tools) + `pop_command`/`review_pr` are living proof; the audit is *which leaves are clean*, not *whether the seam exists* |
| 5 | The blockers are **seam asymmetries, not modules** — additive-only tool advertisement, privileged native model dispatch, second-class plugin commands, no compaction/cleanup/identity seams | §4.2 hook table (the surface plugins bind to); §12.1 rule 1 "plugins over core" | The hook-surface GAPs (§4) are the synthesis's most important output for the ADR |
| 6 | Every proposed extraction must **degrade gracefully** and emit via the **message bus**, never `print` | §11.1 message bus; §12.1 rules 4 & 5 | Inherited acceptance constraint on every candidate |
| 7 | Internal **600-line splits are hygiene, not extraction** — keep them out of the extraction ranking, list them separately | §12.1 rule 3 "600-line hard cap" | `config.py` (1,743), `common.py` (1,599), `command_runner.py` (1,319), `rich_renderer.py` (1,402), `model_factory.py` (991), etc. are refactors-in-core (§6) |

> **Line-count methodology (Reality-judge guard):** every line number in this
> synthesis is a *total physical line* count carried forward verbatim from the
> sibling audits, which standardized on the editor-gutter / `(Get-Content).Count`
> / Python-newline metric. **Do not** re-verify with PowerShell
> `Get-Content | Measure-Object -Line` — it reports **non-blank lines only** and
> undercounts these CRLF files by exactly their blank-line count. The full proof
> table lives in `tp4.2` §0.5 (memory keys `tp4.2-remediation-linecount-tool-artifact`,
> `reality-judge-fuzzy-numbers`).
>
> **Correction (post-review):** `rich_renderer.py` was originally carried from
> `tp4.4` as **1,190** — which was itself a `Measure-Object -Line` non-blank
> undercount, the exact trap this note warns about. The total physical line count
> is **1,402** (`sum(1 for _ in open(...))` / `(Get-Content).Count`), and it is
> corrected to 1,402 in both places above. All other figures here (`common.py`
> 1,599, `command_runner.py` 1,319, `model_factory.py` 991) re-verify exactly
> against the total-line metric, confirming only the `tp4.4` value drifted from
> the standard.

---

## 1. The Unifying Thesis — "thin binder stays, leaves move"

All four audits independently converged on the **same shape**, which is the
single most important synthesis finding:

| Layer | Binder that **stays** (harness) | Leaves that **move** (candidates) | Proof it already works |
|-------|----------------------------------|-----------------------------------|------------------------|
| **Model** | `ModelFactory.get_model` dispatch (`model_factory.py:533`) | per-type provider impls (`gemini_*`, `chatgpt_codex_client`, `ZaiChatModel`) | **6** plugins register model types via `register_model_type`, landing in the `else` fallthrough (`model_factory.py:969`) with zero dispatch edits |
| **Tools** | `TOOL_REGISTRY` + `register_tools_for_agent` (`tools/__init__.py`) | leaf tool impls (`image_tools`, `model_tools`, `browser/`, UC, skills) | `puppy_kennel` ships **5** tools from a plugin via `register_tools` + `register_agent_tools`, zero core edits |
| **UI** | `command_handler` + `command_registry` dispatch | feature commands + chrome (`/colors`, `/diff`, onboarding, status panel) | `pop_command` & `review_pr` carry commands via `custom_command` + `custom_command_help` |
| **Services** | `config.py` spine, `pydantic_patches`, http clients | side-effect features (`hook_engine`, `version_checker`, `error_logging`, MCP) | `hook_engine/` is already a zero-coupling library; `version_checker` already emits bus-only at boot |

**Consequence for the ADR:** the extraction program is *not* a risky re-architecture.
It is "finish a pattern the codebase already demonstrates," modulo a handful of
**seam gaps** (§4) that gate the harder, default-path candidates.

A second recurring shape is **split-brain modules** — features whose logic
*already lives in a plugin* but whose registration is still parked in core.
These are the lowest-risk wins because extraction is mostly relocation:

- `chatgpt_codex_client.py` — dispatch branch already removed; only the client class is left in core (`tp4.2` §2.9)
- `gemini_code_assist.py` / `gemini_oauth` — oauth in a (non-builtin) plugin; transport + dispatch still core (`tp4.2` §2.10)
- `skills_tools.py` — logic in `plugins/agent_skills`; only the tool registration is core (`tp4.3` §2.8)
- `universal_constructor.py` — models in `plugins/universal_constructor`; extraction even *deletes* a core special-case (`tp4.3` §2.10)
- `mcp_prompts/hook_creator.py` — a single prompt string whose only importer is the `hook_creator` plugin (`tp4.4` §2.9)
- `status_display.py` — only live consumer is the `statusline` plugin; the Live panel is dead code (`tp4.4` §2.5)

---

## 2. The Single Ranked Extraction Backlog (value vs risk)

One list, all four subsystems, sorted **best-value/lowest-risk first**. Tiers
group by *what unblocks them*, which is what the ADR needs for sequencing.

### Tier 0 — Zero-seam layups (LOW/LOW; do first, in parallel)

These need **no new hook**. The carrying hook already exists and is proven.

| # | Candidate | Layer | Carrying hook | Risk/Effort | Why it's first |
|---|-----------|-------|---------------|:-----------:|----------------|
| 1 | **`hook_engine/` → builtin plugin** ⭐ | services | `pre_tool_call` + `run_shell_command` | LOW/LOW | Zero core coupling *today* (`rg` finds only self+tests); ~50 KB / 9 modules; lives in the wrong dir. The single best win. |
| 2 | **`version_checker` → `startup` plugin** ⭐ | services | `startup` | LOW/LOW | Already bus-only side-effect at boot; canonical startup plugin. |
| 3 | **`mcp_prompts/hook_creator` → `hook_creator` plugin** | UI | (none — relocate string) | LOW/LOW | One importer, one string; deletes the `mcp_prompts/` package. |
| 4 | **`image_tools` → plugin** | tools | `register_tools` + `register_agent_tools` | LOW/LOW | Self-contained; no runtime importer; textbook `puppy_kennel` clone. |
| 5 | **`model_tools` (`list_available_models`) → plugin** | tools | `register_tools` + `register_agent_tools` | LOW/LOW | Read-only config projection; no hat-(b) importer. |
| 6 | **`error_logging` → observability plugin** | services | `agent_exception` / `post_tool_call` | LOW/LOW | Pure best-effort side-effect; only dep is `STATE_DIR`. |
| 7 | **`status_display` rate → `statusline` plugin (+ delete dead Live panel)** | UI | `stream_event` | LOW/LOW–MED | Only live consumer is already a plugin; removes dead UI. |
| 8 | **`chatgpt_codex_client` → `chatgpt_oauth` plugin** | model | `register_model_type` (already done) | LOW/LOW | Split-brain finisher; native branch already gone; single plugin-side importer. |
| 9 | **`/generate-pr-description` → `review_pr` plugin** | UI | `custom_command` (+ `register_commands`, §4) | LOW/LOW | Self-contained; target plugin already exists. |

### Tier 1 — Split-brain finishers & declutter (LOW–MED; small seam or re-advertise)

| # | Candidate | Layer | Note | Risk/Effort |
|---|-----------|-------|------|:-----------:|
| 10 | **`skills_tools` → `agent_skills` plugin** | tools | Logic already there; must **re-advertise same names** (GAP-T2) | LOW/MED |
| 11 | **`universal_constructor` tool → `universal_constructor` plugin** | tools | Migrate the enable-gate into `register_agent_tools`; **deletes** the core `if tool_name == "universal_constructor"` special-case (GAP-T3) | MED/MED |
| 12 | **`gemini_code_assist` → `gemini_oauth` plugin** | model | Most-split provider; `:931` import is branch-local lazy, no module-load coupling | LOW/MED |
| 13 | **`ZaiChatModel` + `zai_*` branches → `zai` plugin** | model | Removes an inline class + 2 dispatch branches from the binder | LOW/MED |
| 14 | **`uvx_detection` → keybinding plugin** | services | Core already treats it optional; consumers in `command_line`/`keymap` (coordinate w/ tp4.4) | LOW/MED |

### Tier 2 — Seam-gated (MED; build the enabler first — see §4)

| # | Candidate | Layer | Blocked on (gap) | Risk/Effort |
|---|-----------|-------|------------------|:-----------:|
| 15 | **`/colors` + `colors_menu.py` → `theme` plugin** | UI | GAP-U1 `register_commands` | LOW/MED |
| 16 | **`/diff` + `diff_menu.py` → plugin** | UI | GAP-U1 `register_commands` | MED/MED |
| 17 | **Onboarding (`/tutorial`, wizard, slides) → `onboarding` plugin** | UI | `startup` first-run guard | MED/MED |
| 18 | **`browser/` (~3k ln + Playwright) → `browser` plugin** | tools | GAP-T1 tool-cleanup seam | MED/MED |
| 19 | **`gemini_model` (native) → `gemini` plugin** | model | GAP-M3 native-dispatch table; module-level static import `:22` | MED/MED |
| 20 | **`session_storage` + autosave → plugin** | services | Break `config → save_session` top import | MED/MED |
| 21 | **`summarization_agent` → plugin** | services | GAP-S1 compaction-strategy seam | MED/MED |

### Tier 3 — Big cohesive prize (HIGH; last)

| # | Candidate | Layer | Note | Risk/Effort |
|---|-----------|-------|------|:-----------:|
| 22 | **`mcp_/` subsystem (18 modules, ~267 KB) → plugin** | services | Outside the harness already; seam lives inside `_builder` build path — needs a toolset-injection hook (GAP-S2) | HIGH/MED |

### Deferred / YAGNI (recorded, not recommended)

- `round_robin_model` → plugin (LOW/LOW–MED) — clean but low payoff; **defer** (`tp4.2` §2.12).
- `register_renderer` seam (UI) — no concrete alt-renderer demand; theme plugin's style-patching covers the common case (`tp4.4` §3.2).
- `TOOL_EXPANSIONS` plugin-registrable compound tools (`tp4.3` §3.4) — minor.
- `find_available_port` extraction (`tp4.5` §4) — one function, not worth a bead.
- `list_filtering.py` (18 ln) — plugin-izing is pure indirection (`tp4.4` §2.8).

---

## 3. Harness-Must-Keep — the consolidated floor

Stubbing **any** of these breaks BOOT, plugin loading, model resolution, the
no-op turn, cancellation, or user input (the `tp4.1` liveness properties). These
are **never** extraction candidates. Consolidated across all four audits:

**Boot & engine spine**
- `cli_runner.py` (`main_entry`→`main`), `main.py`, `__main__.py` — the ordered boot.
- `pydantic_patches.py` — runs *before* plugins load; the `_writeback_tool_args` patch is what makes the `pre_tool_call` **rewrite** seam work at all. Extracting it is a chicken-and-egg paradox (`tp4.5` §2.10).
- `callbacks.py` — the hook engine plugins plug *into*; cannot itself be a plugin.
- `plugins/__init__.py` — the loader; "load plugins" is liveness property #2.

**Agent run loop**
- `agents/base_agent.py`, `_builder.py`, `_runtime.py`, `agent_manager.py` — no turn without the quartet.
- `summarization_agent.py` — HARNESS-COUPLED via `_compaction`; extract only behind a compaction seam (GAP-S1).

**Model dispatch**
- `model_factory.py` (`get_model` / `load_config`) — the dispatch binder every agent build calls.
- `claude_cache_client.py` — shared Anthropic cache client that **core AND provider plugins** (`aws_bedrock`, `azure_foundry`) import; arrow points plugin→core (correct).
- `model_utils.py` — `prepare_prompt_for_model`, the prompt-prep hook host on the turn path.
- `provider_identity.py`, `provider_credentials.py` — pydantic-ai `provider.name` compat boundary + credential hydration.
- `model_descriptions.py`, `model_switching.py` — tiny binder/control glue.
- `http_utils.py`, `reopenable_async_client.py` — the live turn's LLM HTTP client.

**Tools binder & second-hat plumbing**
- `tools/__init__.py` — `TOOL_REGISTRY` + `register_tools_for_agent`.
- `command_runner.py` — shell tool **and** `_RUNNING_PROCESSES`/`is_awaiting_user_input` on the cancel/input path.
- `file_modifications.py` / `file_operations.py` — default read/write toolset + the `file_permission`/`edit_file` hook host.
- `ask_user_question/` — the interactive-input primitive coordinated with the turn loop.
- `subagent_invocation.py` / `agent_tools.py` — sub-agent dispatch + running-task registry.
- `tools/common.py`, `subagent_context.py`, `display.py` — shared plumbing imported by `agents/` and `messaging/`.

**UI transport & REPL spine**
- `command_handler.py` / `command_registry.py` — slash routing + the `custom_command` hook host.
- `prompt_toolkit_completion.py` + live completers — the input engine.
- `messaging/` (bus, queue, renderers, pause_controller, spinner, subagent_console) — the UI transport every agent module imports.
- `terminal_utils.py` — cross-platform terminal sanity on BOOT and turn.
- `keymap.py` — HARNESS-COUPLED; on the live-turn interrupt path with no seam. **Defer** (the one HIGH-risk trap that *looks* like config data).
- Harness-config commands: `/help`, `/cd`, `/exit`, `/agent`, `/model`, `/set`, `/clear`, `/compact`, `/truncate`, `/session`.

**Config spine**
- `config.py` clusters A/B/D/G (dir roots, `puppy.cfg` read/write, model resolution, the **safety-permission gate**). Orphaning the shell-safety gate is a known self-inflicted wound (memory `externalization-alternatives-lean` L3) — keep it in the spine.

---

## 4. Hook-Surface Gaps That Block Extraction (each → a PROPOSED follow-up bead)

The synthesis's headline: **the blockers are seam asymmetries, not modules.**
Three are *keystones* — enablers that must land before the default-path
candidates can extract cleanly. Each gap below is a **proposed** bead (NOT built
here; the ADR `puppy-1ng` gates implementation).

| Gap ID | Gap | Blocks | Proposed bead (title — 1-line scope) | Keystone? |
|--------|-----|--------|--------------------------------------|:---------:|
| **GAP-T2** | `register_agent_tools` is **additive-only**; builtin agents **hardcode** tool names in `get_available_tools()`. No `unregister`/override precedence (`_load_plugin_tools` is silent last-writer-wins). | #10, #11, #18, any default-listed tool | `feat: add register_agent_tools removal/override (or a documented "re-advertise") contract` — let plugins remove/override hardcoded default tools so default-listed tools can leave core. | ⭐ YES |
| **GAP-U1** | Plugin commands are **second-class**: a `(name, desc)` tuple + manual `if name != "x": return None`, vs built-ins' rich `CommandInfo` (aliases/category/detailed_help). No hook to register a first-class command. | #9, #15, #16 | `feat: add register_commands hook returning CommandInfo-shaped dicts` — give plugin commands parity in the registry & `/help`. | ⭐ YES |
| **GAP-M3** | Native model types are a **privileged 13-arm `if/elif`** checked *before* plugin handlers (`else` at `:967`); native wins, no precedence. Adding/altering a native provider = a core dispatch edit. | #19 (any *default* provider) | `refactor: convert native if/elif model dispatch into an internal register_model_type table` — unify native + plugin providers under one lookup with defined precedence. | ⭐ YES |
| **GAP-T1** | No tool **lifecycle/cleanup** hook. `browser_manager` cleanup is reached by a direct import from `subagent_invocation.py`. | #18 | `feat: add a tool teardown seam (reuse agent_run_end or new teardown_tools hook)` — let tool plugins run cleanup on sub-agent/session end. | no |
| **GAP-T3** | Per-tool **config gating is open-coded in the binder** (`if tool_name == "universal_constructor" ...`). | #11 | (no new hook) `refactor: migrate per-tool enable-gates into the owning plugin's register_agent_tools` — deletes the core special-case. | no |
| **GAP-M1** | **Two overlapping** provider-registration seams: `register_model_type` (consulted *last*, `:969`) vs `register_model_providers` (consulted *first*, `:547`). Opposite precedence, undocumented overlap; builtins use only the former. | clean provider plugins | `refactor: consolidate register_model_providers + register_model_type into one documented seam with defined precedence` | no |
| **GAP-M2** | Plugins **can't register their pydantic-ai provider identity**; `_TYPE_PROVIDER_OVERRIDES` hardcodes plugin provider names in core (inverts the arrow). | clean provider plugins | `feat: let register_model_type carry provider_identity; consult registered types in resolve_provider_identity` | no |
| **GAP-S1** | No **compaction-strategy seam**; `_compaction` imports `run_summarization_sync` directly. | #21 | `feat: add a compaction-strategy seam so the summarizer can be plugin-provided` | no |
| **GAP-S2** | MCP **toolset injection** is wired *inside* `_builder` (`load_mcp_servers`, `filter_conflicting_mcp_tools`), not behind a hook. | #22 | `feat: add a toolset-injection hook so MCP can re-inject agent toolsets from a plugin` | no |
| **GAP-X1** | **Core→plugin import inversion:** `file_modifications.py:51/180` imports `plugins.file_permission_handler` directly instead of going through the `on_file_permission` hook. | hygiene / arrow correctness | `chore: reach file_permission_handler only via the on_file_permission hook` | no |
| **GAP-X2** | **Accidental boot coupling:** `config.py:8` imports `save_session` at module top, dragging `session_storage` onto the BOOT path. | #20 | `refactor: lazy-import session_storage inside the autosave setter to break the config→storage boot coupling` | no |
| **GAP-D1** | **Install-surface weight:** bundled `models_dev_api.json` is **535 KB** in the core install. | thin-core goal | `spike: evaluate a lazily-fetched / plugin-shipped models.dev catalog` | no (parked) |

> **Sequencing rule for the ADR:** land the **three keystones (GAP-T2, GAP-U1,
> GAP-M3) before** their dependent Tier-2 candidates. GAP-T1/S1/S2 are
> single-candidate enablers — bundle each with its candidate.

---

## 5. Proposed Implementation Epics & Beads (titles + 1-line scope — NOT built)

Grouped for the ADR to schedule. IDs are intentionally **unassigned** — the ADR
(`puppy-1ng`) is the gate that files real beads. Nothing here is implemented.

### Epic P1 — Zero-seam extractions (Tier 0; parallelizable, no enabler)
- `feat: extract hook_engine/ to a builtin plugin` — wire via `pre_tool_call` + `run_shell_command`; it already has zero core coupling. *(do first)*
- `feat: version_checker → startup plugin` — bus-only boot side-effect.
- `feat: relocate mcp_prompts/hook_creator into the hook_creator plugin` — delete the `mcp_prompts/` package.
- `feat: image_tools → plugin` — `register_tools` + `register_agent_tools`.
- `feat: model_tools (list_available_models) → plugin` — read-only config projection.
- `feat: error_logging → observability plugin` — via `agent_exception`/`post_tool_call`.
- `feat: status_display token-rate → statusline plugin` — and delete the orphaned Live panel.
- `feat: finish chatgpt_codex_client move into chatgpt_oauth plugin` — native branch already gone.

### Epic P2 — Keystone seams (enablers; gate Tier 1/2)
- `feat: register_agent_tools removal/override contract (GAP-T2)` — let default-listed tools leave core.
- `feat: register_commands hook for first-class plugin commands (GAP-U1)` — `CommandInfo` parity in `/help`.
- `refactor: native if/elif model dispatch → internal register_model_type table (GAP-M3)` — defined native/plugin precedence.

### Epic P3 — Split-brain finishers & declutter (Tier 1; after P2 where noted)
- `feat: skills_tools registration → agent_skills plugin` — re-advertise same names (needs GAP-T2).
- `feat: universal_constructor tool → uc plugin` — self-gate via `register_agent_tools`; deletes core special-case (GAP-T3).
- `feat: finish gemini_oauth extraction (gemini_code_assist → plugin)` — mirror chatgpt_oauth.
- `feat: extract a zai provider plugin` — move inline `ZaiChatModel` + 2 branches.
- `feat: uvx_detection → keybinding plugin` — coordinate consumers in `command_line`/`keymap`.

### Epic P4 — Seam-gated feature extractions (Tier 2)
- `feat: /colors + colors_menu → theme plugin` (needs GAP-U1).
- `feat: /diff + diff_menu → plugin` (needs GAP-U1).
- `feat: onboarding (/tutorial, wizard, slides) → onboarding plugin` — `startup` first-run guard.
- `feat: browser/ → browser plugin` (+ tool-cleanup seam, GAP-T1) — Playwright off the core install.
- `feat: gemini_model (native) → gemini plugin` (after GAP-M3).
- `refactor: break config→session_storage boot import (GAP-X2), then session_storage + autosave → plugin`.
- `feat: compaction-strategy seam (GAP-S1), then summarization_agent → plugin`.

### Epic P5 — Big cohesive externalization (Tier 3)
- `epic: externalize the mcp_/ subsystem as a plugin via a toolset-injection hook (GAP-S2)` — last; high effort.

### Epic P6 — Provider-seam hygiene (clean-up enablers)
- `refactor: consolidate register_model_providers + register_model_type (GAP-M1)`.
- `feat: registerable provider identity (GAP-M2)`.

### Epic H — Internal 600-line splits (hygiene; stays in core, NOT extraction)
- `refactor: split config.py (1,743 ln)` — spine + `config_model.py` (clusters D/E/H/I), per `tp4.5` §3.
- `refactor: split tools/common.py (1,599 ln)` and `command_runner.py (1,319 ln)`.
- `refactor: split messaging/rich_renderer.py (1,402 ln)` into sub-renderers.
- `refactor: split model_factory.py (991), claude_cache_client.py (843), gemini_model.py (840)`.
- `chore: stop file_modifications.py importing file_permission_handler directly (GAP-X1)`.

---

## 6. What the ADR (`puppy-1ng`) should take from this

1. **Adopt the thesis (§1):** extraction = finishing a proven pattern (thin
   binder stays, leaves move), not a re-architecture. Low strategic risk.
2. **Start with Tier 0 (§2):** 8–9 zero-seam wins land immediately and shrink the
   core install surface (esp. `hook_engine/`), validating the program cheaply.
3. **Fund the three keystones (§4: GAP-T2, GAP-U1, GAP-M3) before** Tier 2 — they
   are the difference between "move + re-advertise" churn and clean removal.
4. **Protect the harness floor (§3)** as an invariant: any impl bead must keep
   BOOT / LOAD-PLUGINS / NO-OP-TURN green (codify `tp4.1`'s liveness checks as a
   pytest tripwire — itself a proposed bead from `tp4.1` §4.1).
5. **Coordinate with `27g.4`:** this doc says *what* leaves core; `27g.4` says
   *how* externalized plugins are shipped/overridden/updated (lazy hybrid
   "opt-in eject over in-package-canonical"). The ADR ratifies the union and must
   honor the loader-tier asymmetry (`27g.1`) and the safety-gate-homelessness
   risk (`externalization-alternatives-lean` L3).
6. **Defer the traps:** `keymap.py` (live-turn interrupt, no seam) and the MCP
   subsystem (big, fiddly `_builder` seam) are last; `round_robin` and
   `register_renderer` are YAGNI.

---

## 7. Acceptance-Criteria Mapping

| Acceptance criterion | Where satisfied |
|----------------------|-----------------|
| A single ranked list of plugin-extraction candidates across all subsystems, sorted by value vs risk | §2 (22 candidates, Tier 0→3 + deferred) |
| Explicit harness-must-keep list (things that would break the harness if removed) | §3 (consolidated floor across all four audits) |
| Identified gaps in the current hook surface that block extraction, each as a PROPOSED follow-up bead | §4 (12 gaps, each → a proposed bead; 3 keystones flagged) |
| Proposed implementation epics/beads enumerated for the chosen candidates (titles + 1-line scope) | §5 (Epics P1–P6 + H) |
| Report saved and summarized into the kennel; feeds the plugin-architecture ADR | This file + kennel drawer; §6 wires it to ADR `puppy-1ng` |
| REQUIRED FIRST STEP: leverage `code-puppy-agent` skill, evidenced | §0 grounding table (skill activated; each decision cited to a SKILL.md section) |
