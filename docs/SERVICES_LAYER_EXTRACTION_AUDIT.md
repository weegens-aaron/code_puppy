# Services / Cross-Cutting Layer — Plugin Extraction Audit (`puppy-tp4.5`)

> **Type:** DISCOVERY spike — *research only, zero implementation.* No code was
> moved or refactored. This document classifies modules and **proposes** follow-up
> beads; it does not build them.
>
> **Scope (the brief):** `config.py`, `session_storage.py`, `mcp_/`,
> `hook_engine/`, `summarization_agent.py`, `version_checker.py`, `http_utils.py`,
> `uvx_detection.py`, `error_logging.py`, `reopenable_async_client.py`,
> `pydantic_patches.py`.
>
> **Sibling of:** `tp4.1` (harness boundary), `tp4.2` (model/provider),
> `tp4.3` (tools), `tp4.4` (UI/command_line). Feeds synthesis bead **`tp4.6`**.

---

## 0. Architecture Grounding (REQUIRED FIRST STEP — evidenced)

Per the bead's hard contract and the `skill-grounding-must-be-evidenced` memory:
the `code-puppy-agent` architecture skill was **activated first**, and each
classification decision below is tied to the *specific* SKILL.md section that
constrains it — each cross-checked against a **live source line I actually read**.

| # | Decision in this audit | SKILL.md section | Live source evidence I read |
|---|------------------------|------------------|------------------------------|
| 1 | Reuse `tp4.1`'s **Stub-and-Boot Test** + 3-state rule (HARNESS / REMOVABLE / **HARNESS-COUPLED**) instead of inventing a new yardstick | §12.3 "the core is the engine; the plugin system is the API surface" | `docs/HARNESS_BOUNDARY_CRITERIA.md` §1–§2 (Stub-and-Boot procedure + decision rule) |
| 2 | `config.py` directory roots + `get_value`/`set_value`/`ensure_config_exists` are **HARNESS** (read during boot) | §10 "All settings live in `~/.code_puppy/puppy.cfg`… `get_value`/`set_value`"; §13 `config.py` = "Config read/write, directories" | `config.py:11` `_get_xdg_dir`, `:36-62` dir constants, `:217` `ensure_config_exists`, `:264` `get_value`, `:396` `set_value` |
| 3 | Model **resolution/selection** config stays HARNESS (a no-op turn needs a Model) | §5.1 ModelFactory, §5.4 selection precedence "runtime > pinned > field > global" | `config.py:617` `get_global_model_name`, `:431` `_default_model_from_models_json`, `:504` `_validate_model_exists` |
| 4 | **Provider-specific** model settings (OpenAI reasoning/verbosity, temperature, per-model) are PLUGIN-CANDIDATE — they belong with the provider, not the harness | §5.2/§5.3 model types & config "merged at runtime"; §5.4 provider precedence | `config.py:720-810` OpenAI reasoning/verbosity/temperature, `:840-954` per-model setting cluster |
| 5 | MCP subsystem (`mcp_/*`) is **outside** the harness — additive, turn must run with zero servers | §6.2 "Servers can be globally started / agent-bound / auto-started"; §6.1 manager lifecycle | `agents/_builder.py:170` `get_mcp_manager()`, `:475` `load_mcp_servers`, `:507` `filter_conflicting_mcp_tools`; `_runtime.py:561-566` peeks singleton |
| 6 | `hook_engine/` is a **self-contained library NOT wired into the runtime** → PLUGIN-CANDIDATE, integrated via `pre_tool_call`/`run_shell_command` | §4.2 hook table (`pre_tool_call`, `run_shell_command` "return `{blocked:True}`"); §12.1 rule 1 "plugins over core" | `hook_engine/__init__.py` exports only; `rg hook_engine` → **only tests + self** import it (no core importer); `docs/HOOKS.md:198` "self-contained library with no dependency on the rest of Code Puppy" |
| 7 | `summarization_agent.py` is HARNESS-COUPLED — it backs `_compaction`, which `_runtime` invokes during a long turn | §7.2 "Compaction summarizes older messages using a dedicated summarization agent" | `agents/_compaction.py:46` imports `run_summarization_sync`; `summarization_agent.py:53` `run_summarization_sync` |
| 8 | `http_utils.py` / `reopenable_async_client.py` are HARNESS-COUPLED — `ModelFactory` builds clients from them for the live turn | §5.1 ModelFactory builds pydantic-ai Model objects (which need an HTTP client) | `model_factory.py:28` imports `create_async_client`, `:740/:796/:831` call sites |
| 9 | Plugins must **fail gracefully / flat**, emit via the bus not `print`, and return `None` when not theirs — every proposed plugin honors this | §11.1 message bus `emit_*`; §12.1 rules 4 ("never crash the app") & 5 ("return `None`"); §12.3 "flat is better than nested" | `version_checker.py:67` `get_message_bus().emit`; `error_logging.py` silent-`except` pattern; `cli_runner.py:289` `pass # uvx_detection not available` |

---

## 1. Classification Method (inherited from `tp4.1`)

I deliberately **do not** invent a new yardstick. I apply the **Stub-and-Boot
Test** from `tp4.1` (the paper variant: trace importers, ask whether any sits on
the BOOT / LOAD-PLUGINS / NO-OP-TURN path) and the **3-state decision rule**:

| Stubbed module `M` causes… | Classification |
|----------------------------|----------------|
| BOOT raises, plugins don't load, or the no-op turn can't complete | **KEEP-IN-CORE** (harness) |
| Nothing on the liveness path breaks; only a *feature* disappears | **PLUGIN-CANDIDATE** |
| Passes only because **another in-harness module silently absorbs its job** (no clean seam yet) | **HARNESS-COUPLED** (extractable, but a seam must be designed first) |

The third state is the high-value one for `tp4.6`: it marks the modules where a
naive lift-and-shift would break a live turn, and where a **hook/seam must be
designed before extraction**.

---

## 2. Per-Module Classification

### 2.1 `config.py` — **KEEP-IN-CORE (the spine)**, but internally over-stuffed

`config.py` is **1,743 lines** (verified: `Get-Content config.py | Measure-Object
-Line` → 1743) — **≈2.9× the 600-line cap** (SKILL §12.1 rule 3).
It is unambiguously HARNESS at the module level: `ensure_config_exists`,
`get_value`/`set_value`, and the directory constants are read during BOOT by the
plugin loader and the model factory. You cannot stub it. **But** it is a grab-bag
of cohesive concern clusters, several of which are *feature config* that rode
into the spine by convenience. See §3 for the split proposal.

### 2.2 `session_storage.py` — **HARNESS-COUPLED**

Pure persistence helpers (pickle + metadata, `save_session`/`load_session`/
`cleanup_sessions`). The *mechanism* is feature-grade (no-op turn never touches
it). **But** `config.py:8` imports `save_session` at module top — so a naive stub
breaks the config import, which breaks BOOT. That's an accidental coupling, not an
essential one: the autosave/`/save`/`/load` UX is a feature (SKILL §7.3–§7.4).
`restore_autosave_interactively` already reaches *into* `command_line`,
`messaging`, and `agent_manager` (lazy-imported), so it's really a UI feature
wearing a storage costume. **Seam:** break the `config → session_storage` top
import (lazy-import it inside the autosave setter) and the storage layer becomes a
clean PLUGIN-CANDIDATE.

### 2.3 `mcp_/` (18 modules, ~267 KB) — **PLUGIN-CANDIDATE (large, cohesive)**

The whole MCP subsystem is **outside the harness** (SKILL §6.2; `tp4.1` §1.2
explicitly lists `mcp_/*` as feature surface). `_runtime.py:561-566` deliberately
*peeks* at the manager singleton rather than forcing it to exist, and `_builder`
tolerates zero bound servers — proof the turn runs MCP-free. It's the single
biggest cohesive extraction prize, but high-effort: it owns its own registry,
circuit breaker, health monitor, dashboard, config wizard, and `/mcp` commands.
**Seam already exists** (`pre_mcp_autostart` hook, SKILL §6.2 / §4.2) — but the
wiring in `agents/_builder.py` (`load_mcp_servers`, `filter_conflicting_mcp_tools`)
is *inside the agent build path*, so extraction needs a `register_*`-style hook to
re-inject toolsets. Flag for `tp4.6` as "big, clean concept, fiddly seam."

### 2.4 `hook_engine/` (9 modules, ~50 KB) — **PLUGIN-CANDIDATE (cleanest win)** ⭐

The headline finding. `hook_engine/` is a **fully self-contained library that is
not imported by any runtime module** — `rg hook_engine` returns only the package
itself and its tests, and `docs/HOOKS.md:198` confirms it has "no dependency on
the rest of Code Puppy." It implements Claude-Code-compatible `.claude/settings.json`
hooks (PreToolUse/PostToolUse/etc.) but the actual *integration* into a live turn
would happen through the existing `pre_tool_call` and `run_shell_command` callbacks
(SKILL §4.2). This is the **lowest-risk, highest-clarity extraction** in the whole
audit: it already has the shape of a plugin (zero core coupling), it just lives in
the wrong directory. Effort: **LOW**. Risk: **LOW**.

### 2.5 `summarization_agent.py` — **HARNESS-COUPLED**

Backs context compaction (SKILL §7.2). `_compaction.py:46` imports
`run_summarization_sync`, and `_compaction` is on the long-turn path inside
`_runtime`. A short no-op turn never compacts, so by the strict liveness test it
*looks* removable — but the moment a real conversation overflows, the in-harness
compaction code silently depends on it. Extraction requires a **compaction
strategy seam** (a hook the runtime calls to obtain a summarizer). Medium effort;
do **not** extract before designing that seam.

### 2.6 `version_checker.py` — **PLUGIN-CANDIDATE (textbook startup hook)** ⭐

"Is there a newer code-puppy on PyPI?" It already emits exclusively through the
message bus (`version_checker.py:67` `get_message_bus().emit`, plus `emit_*`),
exactly the SKILL §11.1 sanctioned channel, and it's a pure side-effect at boot.
This is the canonical `startup` plugin from the AGENTS.md example. Effort: **LOW**.

### 2.7 `http_utils.py` + `reopenable_async_client.py` — **KEEP-IN-CORE (HARNESS-COUPLED)**

`ModelFactory` builds its async clients here (`model_factory.py:28`, call sites
`:740/:796/:831`) to make the *live turn's* LLM request — that's squarely on the
NO-OP-TURN path (SKILL §5.1). `reopenable_async_client.py` is a leaf dependency of
`http_utils`. These stay in core. (`find_available_port` is a stray utility that
could move, but it's not worth a bead — see §4 "non-findings".)

### 2.8 `uvx_detection.py` — **PLUGIN-CANDIDATE (platform quirk)**

Detects the Windows-`uvx`-eats-Ctrl+C scenario to swap the cancel key to Ctrl+K.
Consumed by `cli_runner.py:244` and `keymap.py:105/186`, and `cli_runner.py:289`
already wraps it in a graceful `pass # uvx_detection module not available` — i.e.
core **already treats it as optional** (SKILL §12.1 rule 4). It's a self-contained
platform shim with `lru_cache`, ideal for a keybinding/startup plugin. Effort:
**LOW–MED** (the consumers are in `command_line`/`keymap`, so the seam touches
`tp4.4`'s territory — note the cross-bead dependency).

### 2.9 `error_logging.py` — **PLUGIN-CANDIDATE (observability)**

Rotating error log to `STATE_DIR/logs/errors.log`. Pure best-effort side-effect
(every function is wrapped in a silent `except` — SKILL §12.1 rule 4 in the flesh).
Its only core dep is `STATE_DIR` from config. A diagnostics/observability plugin
could own this via `agent_exception`/`post_tool_call` hooks (SKILL §4.2). Effort:
**LOW**, but verify all current callers first.

### 2.10 `pydantic_patches.py` — **KEEP-IN-CORE (NEVER extract)** 

Startup monkey-patches for pydantic-ai (clipboard fix, tool-arg writeback, etc.),
applied *before* anything else in boot (SKILL §13 lists it; `tp4.1` §1.1 puts
`pydantic_patches.apply_all_patches()` in the CLI-bootstrap must-keep row). It runs
at the very front of `main_entry` and the `_writeback_tool_args` patch is what
makes the `pre_tool_call` *rewrite* seam work at all (per the
`pre-tool-call-rewrite-seam` memory). If this were a plugin, the plugin system it
patches wouldn't be patched yet — a chicken-and-egg bootstrap paradox. **Hard
KEEP.**

---

## 3. `config.py` Concern Clusters (the split proposal)

`config.py` is the strongest "needs decomposition" finding. It is HARNESS as a
*module* but mixes a small irreducible spine with several **feature-config**
clusters. Splitting is a *refactor within core* (not necessarily plugin
extraction); the feature clusters become natural homes-of-record for the plugins
proposed in §2.

| Cluster | Representative funcs (lines) | Disposition |
|---------|------------------------------|-------------|
| **A. Path/dir roots** | `_get_xdg_dir` (11), dir constants (36-62) | **Spine — KEEP.** Everything imports these. |
| **B. Core read/write** | `ensure_config_exists` (217), `get_value` (264), `set_value` (396), `reset_value` (401), `get_config_keys` (316) | **Spine — KEEP.** The `puppy.cfg` API (SKILL §10). |
| **C. Identity** | `get_puppy_name` (271), `get_owner_name` (275), `get_puppy_token` (710) | KEEP (small), candidate to split into `config_identity`. |
| **D. Model resolution** | `get_global_model_name` (617), `set_model_name` (659), `_default_model_from_models_json` (431), `_validate_model_exists` (504), `get_model_context_length` (295), `clear_model_cache` (527) | **KEEP** (needed for NO-OP turn, SKILL §5.4) — but extract into `config_model.py` for cohesion. |
| **E. Provider-specific settings** | OpenAI reasoning/summary/verbosity (720-781), temperature/top_p/seed (792-1024), per-model cluster (830-954) | **PLUGIN-CANDIDATE.** Provider-coupled (SKILL §5.2/§5.3); should live near the provider, not the spine. |
| **F. MCP config** | `load_mcp_server_configs` (412), `get_mcp_disabled` (1217), `get_mcp_unbound_warning_silenced` (133) | **PLUGIN-CANDIDATE** — moves with the `mcp_/` extraction (§2.3). |
| **G. Safety/permission** | `get_yolo_mode` (1186), `get_safety_permission_level` (1201) | **HARNESS-COUPLED** — the shell-safety gate. `tp4.1` flags `get_safety_permission_level` as boot-grounding; keep in spine, do **not** orphan it (cf. `externalization-alternatives-lean` L3 "shell_safety gate homelessness"). |
| **H. Summarization config** | `get/set_summarization_model_name` (683/701) | Moves with `summarization_agent` (§2.5). |
| **I. HTTP config** | `get_http2` (1377) | Moves with `http_utils` (stays core, §2.7). |
| **J. Session/autosave** | `set_current_autosave_from_session_name` (1770), `normalize_command_history` | **PLUGIN-CANDIDATE** — moves with `session_storage` (§2.2). |
| **K. Feature flags** | `subagent_verbose` (65), `pack_agents` (94), `universal_constructor` (109), `enable_streaming` (174), `suppress_directory_listing` (187), `grep_output_verbose` (1233), `max_hook_retries` (157) | Each belongs with its owning feature; until those extract, KEEP in a `config_flags` module. |

**Recommended first cut (low risk):** split D/E/H/I into a `config_model.py` and
the spine (A/B/C) stays in `config.py`. This alone gets the file under the cap
without moving any boot-critical behavior. Pure refactor — propose as its own
bead, **not** part of this spike.

---

## 4. Risk / Effort Summary (for `tp4.6`)

| Module / concern | Classification | Effort | Risk | Notes |
|------------------|----------------|--------|------|-------|
| `hook_engine/` ⭐ | PLUGIN-CANDIDATE | **LOW** | **LOW** | Zero core coupling today; wire via `pre_tool_call`/`run_shell_command`. **Best first win.** |
| `version_checker.py` ⭐ | PLUGIN-CANDIDATE | LOW | LOW | Canonical `startup` hook; already bus-only. |
| `error_logging.py` | PLUGIN-CANDIDATE | LOW | LOW | Observability plugin via `agent_exception`. |
| `uvx_detection.py` | PLUGIN-CANDIDATE | LOW-MED | LOW | Consumers in `command_line`/`keymap` → coordinate with `tp4.4`. |
| `config.py` split | KEEP (refactor) | MED | MED | 1,743 lines (≈2.9× cap); cut feature clusters per §3. Touches everything → careful. |
| `session_storage.py` | HARNESS-COUPLED | MED | MED | Must break `config → save_session` top import first. |
| `summarization_agent.py` | HARNESS-COUPLED | MED | MED | Needs a compaction-strategy seam before extraction. |
| `mcp_/` | PLUGIN-CANDIDATE | **HIGH** | MED | Big & cohesive; seam lives inside `_builder` agent build path. |
| `http_utils.py` + `reopenable_async_client.py` | KEEP-IN-CORE | — | — | On the live-turn path via `ModelFactory`. |
| `pydantic_patches.py`  | KEEP — **NEVER extract** | — | HIGH if moved | Bootstrap paradox; patches the plugin system itself. |

### Must-NEVER-extract (harness-critical), with why
- **`pydantic_patches.py`** — runs before plugins load; the `_writeback_tool_args`
  patch is what makes the `pre_tool_call` rewrite seam work at all. Extracting it
  is a chicken-and-egg paradox.
- **`config.py` clusters A/B/D/G** — dir roots, `puppy.cfg` read/write, model
  resolution, and the safety-permission gate are read during BOOT and the NO-OP
  turn. (Recall `externalization-alternatives-lean` L3: orphaning the shell-safety
  gate is a known self-inflicted wound — keep it in the spine.)
- **`http_utils.py` / `reopenable_async_client.py`** — the live turn's LLM HTTP
  client comes from here.

### Non-findings (deliberately NOT filed as beads)
- `find_available_port` in `http_utils.py` is a stray utility, but extracting one
  function isn't worth a bead (YAGNI).
- The `config → session_storage` top-level import is an *accidental* coupling, but
  it's captured above as the seam to fix during the §2.2 extraction — not a
  separate bug.

---

## 5. Bug-Discovery Protocol Result

**No unrelated bugs found.** The closest candidates are *architectural smells*
already owned by this audit family:
- `config.py` exceeding the 600-line cap → captured here (§3) and is the subject of
  the proposed split, not a defect to file.
- `config → session_storage` import asymmetry → captured as a §2.2 seam.
- The plugin-tier loader asymmetry (`27g.1`) is owned by the `27g` beads.

Per the protocol, nothing rises to a standalone `bd create --type=bug`.

---

## 6. Proposed Follow-up Beads (NOT built — for `tp4.6`/ADR `1ng` to gate)

1. **`feat:` Extract `hook_engine/` to a builtin plugin** — wire via `pre_tool_call`
   + `run_shell_command`; it already has zero core coupling. *(LOW/LOW — do first.)*
2. **`feat:` `version_checker` → `startup` plugin.** *(LOW/LOW.)*
3. **`feat:` `error_logging` → observability plugin** via `agent_exception`.
4. **`feat:` `uvx_detection` → keybinding plugin** (coordinate with `tp4.4`).
5. **`refactor:` Split `config.py`** into spine + `config_model.py` (clusters D/E/H/I),
   getting under the 600-line cap. *(MED/MED.)*
6. **`refactor:` Break `config → session_storage` top import**, then evaluate
   `session_storage` + autosave as a plugin. *(MED/MED.)*
7. **`feat:` Design a compaction-strategy seam** so `summarization_agent` can be
   provided by a plugin. *(MED/MED — seam first.)*
8. **`epic:` Externalize the `mcp_/` subsystem** as a large cohesive plugin via a
   toolset-injection hook. *(HIGH/MED — last.)*

> These are **proposals**. Per the DISCOVERY-epic charter, this spike does not
> implement them; the ADR (`puppy-1ng`) gates all implementation.

---

## 7. Acceptance Criteria Mapping

| Acceptance criterion | Where satisfied |
|----------------------|-----------------|
| Each module/concern classified KEEP-IN-CORE vs PLUGIN-CANDIDATE w/ rationale | §2 (per module) + §4 table; HARNESS-COUPLED used as the nuanced middle state |
| For `config.py`, identify cohesive concern clusters that could be split out | §3 (11-cluster table + recommended first cut) |
| Note harness-critical modules that must NEVER be extracted, with why | §4 "Must-NEVER-extract" + §2.10 (`pydantic_patches`) |
| Risk/effort estimate per candidate; findings captured for synthesis | §4 table + §6 proposed beads (feeds `tp4.6`) |
| REQUIRED FIRST STEP: leverage `code-puppy-agent` skill | §0 grounding table (skill activated + each decision cited to a SKILL.md section and a live source line) |
