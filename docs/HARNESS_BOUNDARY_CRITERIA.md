# Harness Boundary Criteria — Spike puppy-tp4.1

> **Bead:** `puppy-tp4.1` — *Spike: Establish harness-boundary criteria
> (removable-without-breaking test)*
> **Epic:** `puppy-tp4` — Core to Plugin Extraction Audit (**DISCOVERY** — research
> only, no implementation; proposes follow-up beads, does not build them).
> **Status:** report for synthesis bead `puppy-tp4.6`. Judges are the only
> legitimate closer.

This document defines the **harness boundary** — the irreducible core that must
stay in `code-puppy` for the app to *boot*, *run one agent turn*, and *load
plugins* — and gives the sibling audits (`tp4.2`–`tp4.5`) a **single, repeatable
classification test** for deciding whether any module is
*removable-without-breaking-the-harness*.

---

## 0. Architecture Grounding (REQUIRED FIRST STEP)

Per the project contract, step zero is **activating the `code-puppy-agent`
architecture skill** and *evidencing* it: every load-bearing claim below is tied
to the specific SKILL.md section that constrains it, and cross-checked against the
live source. (Memory keys `skill-grounding-must-be-evidenced` &
`plugin-tier-loader-asymmetry`.)

| # | Design decision in this report | SKILL.md section it rests on | Verified against source |
|---|--------------------------------|------------------------------|--------------------------|
| 1 | The **callback engine** (`callbacks.py`) is in-harness — it's the API surface, not a feature | §4.2 "callback engine fires hooks"; §12.3 "the plugin system *is* the API surface; the core is the engine" | `callbacks.py:235/273` `_trigger_callbacks(_sync)`; 45+ `PhaseType`s |
| 2 | The **plugin loader** (`plugins/__init__.py`) is in-harness — "load plugins" is a harness duty by definition | §4.1 three-tier discovery table (builtin→user→project) | `plugins/__init__.py:load_plugin_callbacks()` |
| 3 | **Model dispatch entrypoint** (`model_factory.get_model` / `ModelFactory`) is in-harness — no Model object, no turn | §5.1 "ModelFactory resolves a model-name string into a pydantic-ai Model" | `model_factory.py:411` class, `:533` `get_model()` |
| 4 | **Agent run loop** (`base_agent` + `_builder` + `_runtime`) is in-harness | §2.1 "BaseAgent is a thin conductor"; §2.1 sibling-module table | `base_agent.py:273` `run_with_mcp`, `_runtime.py:304` |
| 5 | **Tool registry + binder** (`tools/__init__.py`) is in-harness; the *tool implementations* are not necessarily | §3.1 "flat dict mapping tool-name strings"; §3.2 `register_tools_for_agent()` | `tools/__init__.py` `TOOL_REGISTRY`, `register_tools_for_agent()` |
| 6 | **CLI bootstrap** (`cli_runner.main_entry`→`main`) is in-harness — it sequences patch→load_plugins→startup→agent | §1 "TUI collects input… delegates to agent manager" | `cli_runner.py:1123` `main_entry`, `:51` `load_plugin_callbacks()`, `:365` `on_startup()`, `:1033` `run_with_mcp` |
| 7 | Builtin **plugins are candidates for removal**, not harness — they hang off hooks, not the boot path | §4 "nearly all new functionality should be a plugin"; §12.1 rule 1 | 40 dirs under `code_puppy/plugins/`, each a `register_callbacks.py` |
| 8 | The test must **fail gracefully** when a module is stubbed — a clean degrade is the signal, a crash is the boundary | §12.1 rule 4 "plugins must never crash the app"; §12.3 "flat is better than nested" | loader wraps every plugin import in try/except (`plugins/__init__.py`) |
| 9 | `config.py` is in-harness because the loader & factory call it *during boot* | §10 config root/dirs; §5.2 model config merge | `plugins/__init__.py` imports `get_safety_permission_level`; `model_factory` imports config paths |

> **Note on the known loader asymmetry** (memory `plugin-tier-loader-asymmetry`):
> the three tiers are loaded by *different* import mechanisms (builtin =
> `importlib.import_module`; user/project = `spec_from_file_location`). That
> asymmetry is real and is the subject of sibling/`27g` beads — it does **not**
> change the boundary, because all three tiers still funnel through
> `load_plugin_callbacks()`, which *is* the harness duty we test for.

---

## 1. The Harness Boundary — Definition

The **harness** is the minimal set of modules required to satisfy three
liveness properties, in order:

1. **BOOT** — `main_entry()` runs to the interactive/`-p` dispatch point without
   raising.
2. **LOAD PLUGINS** — `plugins.load_plugin_callbacks()` completes and the three
   tiers (builtin/user/project) are discovered and registered through
   `callbacks.register_callback`.
3. **RUN A NO-OP TURN** — `get_current_agent().run_with_mcp(<prompt>)` builds a
   pydantic-ai `Agent`, resolves a `Model`, and returns a result (a turn that
   calls *no* tools — hence "no-op").

Anything **outside** this set is, by definition, a *feature* and a candidate for
extraction to a plugin.

### 1.1 Must-keep responsibilities (the irreducible core)

| Responsibility | Owning module(s) | Why it's irreducible |
|----------------|------------------|----------------------|
| **CLI bootstrap & sequencing** | `cli_runner.py` (`main_entry`→`main`), `main.py`, `__main__.py`, `pydantic_patches.apply_all_patches()` | The ordered boot: patch pydantic-ai → `load_plugin_callbacks()` (line 51, *at import time*) → `on_startup()` (line 365) → dispatch to `run_with_mcp` (line 1033). Remove it and there is no process. |
| **Callback / hook engine** | `callbacks.py` | Stores + fires per-phase hooks (`_trigger_callbacks(_sync)`). This is the *contract* every plugin binds to. It cannot be a plugin — it's what plugins plug into (SKILL §12.3). |
| **Plugin loader** | `plugins/__init__.py` (`load_plugin_callbacks`, the three `_load_*` tier functions) | "Load plugins" is liveness property #2. The loader literally cannot externalize itself. |
| **Agent run loop** | `agents/base_agent.py`, `agents/_builder.py`, `agents/_runtime.py`, `agents/agent_manager.py` | `base_agent` is a thin conductor (SKILL §2.1); `_builder` assembles the pydantic-ai Agent + tools + MCP; `_runtime.run_with_mcp` orchestrates the turn; `agent_manager` resolves *which* agent. No turn without all four. |
| **Model dispatch entrypoint** | `model_factory.py` (`ModelFactory`, `get_model`), `config.py` (model config read) | Resolves a model-name string → pydantic-ai `Model` (SKILL §5.1). A turn needs a Model object. `models.json` (bundled defaults) is the minimal data dependency. |
| **Tool registry + binder** | `tools/__init__.py` (`TOOL_REGISTRY`, `register_tools_for_agent`) | The *binding mechanism* is harness; the *contents* are negotiable. A no-op turn needs the binder to exist and to tolerate an **empty** tool list. |
| **Config & directory roots** | `config.py` (`ensure_config_exists`, dir constants, `get_safety_permission_level`) | Called *during* boot by the loader and factory (grounding row 9). Harness by data-dependency, not by feature. |
| **Messaging spine** | `messaging/` (`emit_*`, message bus) | The loader, factory, and runtime all emit through it; `emit_warning` is the sanctioned non-`print` channel (SKILL §11.1). A stub bus is fine, but *some* sink must exist or boot raises on first `emit_*`. |

### 1.2 Explicitly OUTSIDE the harness (feature surface)

- **All 40 builtin plugins** under `code_puppy/plugins/<name>/` — they attach via
  hooks and degrade cleanly when absent (the loader try/excepts each import).
- **Concrete tool *implementations*** (`tools/file_operations.py`,
  `tools/browser/*`, `tools/command_runner.py`, …) — the *registry* is harness,
  each *register_func* is a leaf the binder tolerates missing.
- **Non-default agents** (`agent_creator_agent.py`, `agent_helios.py`,
  `agent_qa_kitten.py`, `agent_planning.py`) — only one bootable default agent is
  required for a turn.
- **Optional model providers** (OAuth clients, `gemini_*`, `chatgpt_codex_client`,
  `claude_cache_client`, `round_robin_model`) — extra `type` handlers, reachable
  but not required for the bundled default model.
- **MCP subsystem** (`mcp_/*`) — `run_with_mcp` must run *with zero servers
  bound*; MCP is additive (SKILL §6.2).

---

## 2. The Classification Test (repeatable; for sibling audits)

> **Name:** the **Stub-and-Boot Test**. Apply it to any module `M` to classify it
> as **HARNESS** (must-keep) or **REMOVABLE** (extraction candidate).

### 2.1 Procedure

For the module under test `M`:

1. **Stub it.** Replace `M` with a minimal no-op shim (functions return `None`/
   empty; classes become empty stubs) — *or* the cheaper paper version: trace its
   importers and ask "is any of them on the BOOT / LOAD-PLUGINS / NO-OP-TURN
   path?"
2. **BOOT:** does `main_entry()` reach the dispatch point without raising?
3. **LOAD PLUGINS:** does `load_plugin_callbacks()` complete and return the three
   tier lists?
4. **NO-OP TURN:** does `get_current_agent().run_with_mcp("say hi, call no
   tools")` return a result?
5. **Classify** using the decision rule below.

### 2.2 Decision rule

| Stubbed `M` causes… | Classification |
|----------------------|----------------|
| BOOT raises, OR plugins don't load, OR no-op turn can't complete | **HARNESS** (must-keep) |
| All three pass — possibly with a *graceful* degrade (a warning, a missing feature, a skipped tool) | **REMOVABLE** (extraction candidate) |
| All three pass *but only because another in-harness module silently absorbs M's job* | **HARNESS-COUPLED** — flag for `tp4.6`: extraction needs a seam first |

The third row is the trap the audits must watch for: a module can *look*
removable while being load-bearing through a back-channel. **A clean,
*intentional* degrade is REMOVABLE; an accidental "it happened to still work" is
HARNESS-COUPLED.** (SKILL §12.1 rule 4: graceful failure is the design, not luck.)

### 2.3 Checklist (copy/paste per module)

```
Module: ___________________________
[ ] Is it imported anywhere on the BOOT path (cli_runner main_entry→main)?
[ ] Is it imported by the plugin loader (plugins/__init__.py) at load time?
[ ] Is it imported by the agent build/run loop (_builder/_runtime/base_agent)?
[ ] Is it imported by model dispatch (model_factory.get_model)?
[ ] Does run_with_mcp on a no-tool prompt touch it?
[ ] If stubbed, does the failure DEGRADE gracefully (warn) or CRASH?
  → any of first 5 = yes AND stub crashes  ⇒ HARNESS
  → all no / graceful degrade               ⇒ REMOVABLE
  → passes only via a silent absorber       ⇒ HARNESS-COUPLED (needs seam)
```

> **Tip for the audits:** the cheap paper version (import-graph trace) is enough
> to classify most modules. Reserve the full stub-and-run for the ambiguous
> HARNESS-COUPLED cases — that's where the real extraction-design work lives.

---

## 3. Worked Examples (applying the test)

### 3.1 `code_puppy/plugins/emoji_filter/` → **REMOVABLE**

- BOOT: the loader try/excepts each plugin import; a missing/broken
  `emoji_filter` is caught and logged. 
- LOAD PLUGINS: `load_plugin_callbacks()` still returns all three tiers (this
  plugin just isn't in the list). 
- NO-OP TURN: it only attaches to `startup`, `pre_tool_call`,
  `custom_command(_help)` (verified in its `register_callbacks.py`). A no-op turn
  fires none of those in a way that needs it. 
- Degrade: clean — emojis simply aren't stripped.
- **Verdict: REMOVABLE.** Textbook feature-as-plugin. Already on the right side
  of the boundary; an extraction-to-external-package candidate, not a core risk.

### 3.2 `code_puppy/callbacks.py` → **HARNESS**

- Stub it and `register_callback` / `_trigger_callbacks` vanish.
- LOAD PLUGINS: `plugins/__init__.py` imports `set_loading_context` /
  `clear_loading_context` from it at module top, and every plugin calls
  `register_callback` at import → instant `ImportError`/`AttributeError`. 
- **Verdict: HARNESS.** It's the engine plugins plug into (SKILL §12.3); it
  cannot itself be a plugin. Non-negotiable must-keep.

### 3.3 `code_puppy/tools/file_operations.py` → **REMOVABLE** (impl), with a **HARNESS** registry

- The *registry* (`tools/__init__.py`) is harness — `_builder` calls
  `register_tools_for_agent` on every agent build.
- The *implementation* file is a leaf: `register_tools_for_agent` already
  tolerates unknown/missing tools — it `emit_warning`s and skips
  (`tools/__init__.py`, "Skip unknown tools with a warning"). So if
  `read_file`/`grep`/`list_files` weren't registered, a **no-op turn still
  completes** (it requests no tools). 
- Degrade: graceful (warning + skip), *by design*.
- **Verdict: REMOVABLE implementation behind a HARNESS binder.** This is the
  canonical shape the extraction audit wants: keep the thin binder, externalize
  the leaves. *Caveat for `tp4.6`:* the **default agent's advertised tool list**
  is a coupling point — externalizing the impls means the default agent must
  degrade to a smaller toolset, not crash. Flag as the seam to design.

---

## 4. Findings (feed to synthesis bead `puppy-tp4.6`)

1. **The harness is small and well-bounded.** Six responsibilities, ~10 modules:
   `cli_runner`(+`main`/`__main__`/`pydantic_patches`), `callbacks`,
   `plugins/__init__`, the `agents` run-loop quartet
   (`base_agent`/`_builder`/`_runtime`/`agent_manager`), `model_factory`(+bundled
   `models.json`), `tools/__init__` (binder only), and `config` + `messaging` as
   boot-time data/IO dependencies.
2. **The boundary is operational, not architectural.** Defined by three liveness
   properties (BOOT / LOAD-PLUGINS / NO-OP-TURN), so the audits classify by
   *behavior under stub*, not by folder or vibes.
3. **The Stub-and-Boot Test is the uniform instrument** for `tp4.2`–`tp4.5`. Its
   sharp edge is the **HARNESS-COUPLED** third state — modules that pass only
   because something silently absorbs their job. Those are where extraction needs
   a *seam designed first*, and they're the highest-value findings for `tp4.6`.
4. **"Binder is harness, leaves are removable" is the recurring pattern.** Seen in
   tools (registry vs impls) and will recur in models (factory vs provider
   handlers) and agents (manager vs concrete agents). The synthesis should treat
   *thin-binder / externalizable-leaf* as the default extraction shape.
5. **The known loader asymmetry** (`plugin-tier-loader-asymmetry`) does **not**
   move the boundary — all tiers still flow through `load_plugin_callbacks()` —
   but it is a latent extraction risk the `27g`/sibling beads already own. Noted,
   not re-filed.

### 4.1 Proposed follow-up beads (DISCOVERY epic — NOT built here)

These are *proposals* for the synthesis/ADR to schedule; nothing is implemented
in this spike (epic rule: research only).

- **`tp4.x` (impl, later): Default-agent tool-list degrade seam.** Make the
  bootable default agent tolerate an empty/partial advertised toolset so tool
  *impls* can externalize without breaking the no-op-turn property (from §3.3).
- **`tp4.x` (test, later): A harness smoke-test fixture.** Codify the three
  liveness checks (BOOT / LOAD-PLUGINS / NO-OP-TURN) as a pytest so future
  extractions get an automated tripwire instead of a manual checklist.

---

## 5. Acceptance-criteria mapping

| Acceptance criterion | Where satisfied |
|----------------------|-----------------|
| Written harness-boundary definition with explicit must-keep responsibilities (callbacks/loader, model dispatch, agent run loop, CLI bootstrap) | §1, §1.1 |
| A repeatable classification test/checklist the sibling audits apply uniformly | §2 (Stub-and-Boot Test + §2.3 checklist) |
| Examples applying the test to 2–3 modules | §3 (three worked examples) |
| Output captured as a short report section feeding the synthesis bead | §4 (findings for `tp4.6`) + §4.1 proposed beads |
| REQUIRED FIRST STEP: leverage `code-puppy-agent` skill, evidenced | §0 grounding table |
