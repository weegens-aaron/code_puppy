# Model / Provider Layer — Plugin Extraction Audit (`puppy-tp4.2`)

> **Type:** DISCOVERY spike — *research only, zero implementation.* No code was
> moved or refactored. This document classifies modules and **proposes** follow-up
> beads; it does not build them.
>
> **Scope (the brief):** the model/provider layer — `model_factory.py`,
> `gemini_model.py`, `gemini_code_assist.py`, `claude_cache_client.py`,
> `chatgpt_codex_client.py`, `model_switching.py`, `round_robin_model.py`,
> `model_utils.py`, `model_descriptions.py`, `provider_credentials.py`,
> `provider_identity.py`, `models_dev_parser.py`. The question: which providers
> could be *supplied by plugins* via the existing `register_model_type` /
> `load_models_config` / `register_model_providers` hooks instead of being
> hardcoded in `ModelFactory.get_model`'s dispatch, and which dispatch/binder
> must remain in core.
>
> **Sibling of:** `tp4.1` (harness boundary), `tp4.3` (tools), `tp4.4` (UI),
> `tp4.5` (services). Feeds synthesis bead **`tp4.6`**.

---

## 0. Architecture Grounding (REQUIRED FIRST STEP — evidenced)

Per the bead's hard contract and the `skill-grounding-must-be-evidenced` memory:
the `code-puppy-agent` architecture skill was **activated first**, and each
classification decision below is tied to the *specific* SKILL.md section that
constrains it — each cross-checked against a **live source line I actually read**.

| # | Decision in this audit | SKILL.md section | Live source evidence I read |
|---|------------------------|------------------|------------------------------|
| 1 | Reuse `tp4.1`'s **Stub-and-Boot Test** + 3-state rule (KEEP / PLUGIN-CANDIDATE / **HARNESS-COUPLED**) rather than invent a new yardstick | §12.3 "the plugin system is the API surface; the core is the engine" | `docs/HARNESS_BOUNDARY_CRITERIA.md` §1–§2; `tp4.1` already classed `model_factory.py` on the boot path |
| 2 | `ModelFactory.get_model()` (the `model_type`→`Model` dispatch) is the **HARNESS binder** — it stays; per-type *leaf* impls can move | §5.1 "ModelFactory resolves a model-name string into a pydantic-ai `Model` object"; §13 `model_factory.py` = "Model-name → pydantic-ai Model" | `model_factory.py:533` `def get_model`, the `if/elif model_type ==` chain `:557–:946` |
| 3 | A plugin **supplies a provider** by returning a `{type, handler}` from `register_model_type`; the `else` branch routes unknown types to it | §5.3 "Plugin types — Registered via `register_model_type` callback"; §4.2 hook table | `model_factory.py:969` `callbacks.on_register_model_types()` fallthrough, `:991` `raise ValueError("Unsupported model type")` |
| 4 | The **canonical working proof** of provider-as-plugin is the 5 shipping plugins that register model types with **zero core dispatch edits** | §5.3 plugin types; §4.3 minimal plugin | `plugins/aws_bedrock/register_callbacks.py:243`, `plugins/azure_foundry/…:497`, `plugins/ollama/…:127`, plus `claude_code_oauth`/`chatgpt_oauth` (notes at `model_factory.py:679/944`) |
| 5 | A plugin can **ship the model defs too** via `load_models_config` and tweak copy via `load_model_descriptions` — so a provider plugin is self-contained | §5.2 "Plugin injection — `load_models_config` callback returns a dict" | `model_factory.py:480-490` `on_load_models_config()` `config.update`, `:510-512` `on_load_model_descriptions()` |
| 6 | "Fail gracefully / return None" governs provider loading — a bad provider plugin must not break model resolution | §12.1 rules 4 & 5 | `model_factory.py:976-983` `except Exception … return None` around the plugin handler call; `:42-51` `_load_plugin_model_providers` swallows errors |
| 7 | ChatGPT-Codex support is **already half-plugin (split-brain)**: the dispatch branch moved to the `chatgpt_oauth` plugin, but the client class is still parked in core | §5.3 plugin types; §12.2 "a plugin's … logic split out" lives in the plugin dir | `model_factory.py:944` "now handled by the chatgpt_oauth plugin"; sole importer of `chatgpt_codex_client.create_codex_async_client` is `plugins/chatgpt_oauth/register_callbacks.py:113` |
| 8 | Gemini-OAuth (Code Assist) is the **most-split** provider: oauth config/utils live in a (non-builtin) `gemini_oauth` plugin, yet the `Model` class **and** the dispatch branch are still in core | §4.1 tiers (user/project plugins); §5.3 plugin types | `model_factory.py:886` `elif model_type == "gemini_oauth"` lazy-imports `code_puppy.plugins.gemini_oauth.{config,utils}` (dir **absent** from builtin tier); Model = `gemini_code_assist.GeminiCodeAssistModel` (`model_factory.py:931`) |
| 9 | `prepare_prompt_for_model` is a **hook host** on the turn path, not a provider leaf → KEEP | §9 prompt assembly ("per-model patches via `prepare_prompt_for_model`"); §4.2 `get_model_system_prompt` | `model_utils.py:60/85` fire `on_prepare_model_prompt` + `on_get_model_system_prompt`; imported by `_builder.py:422`, `_runtime.py:250`, `base_agent.py:214`, `summarization_agent.py:71` |
| 10 | The Anthropic caching client is **shared core infra consumed by provider plugins** — the dependency arrow points the *right* way (plugin→core) → KEEP | §12.1 rule 1 (plugins over core; don't invert the arrow); §5.3 anthropic family | `claude_cache_client.patch_anthropic_client_messages` imported by `model_factory.py:26`, **and** by `plugins/aws_bedrock/…:167` & `plugins/azure_foundry/…:320` |
| 11 | Don't over-extract tiny shared utils the binder needs (Zen: simple/flat) | §12.3 "Simple is better than complex / Flat is better than nested" | `model_descriptions.py` (62 ln) used by `model_factory.py:496` + UI; `model_switching.py` (63 ln) used by `/model` commands |

---

## 1. Classification Method (inherited from `tp4.1`)

I deliberately **do not** invent a new yardstick. I apply the **Stub-and-Boot
Test** from `tp4.1` (trace importers; ask whether any sits on the
BOOT / LOAD-PLUGINS / NO-OP-TURN liveness path) and the **3-state decision rule**:

| Stubbed module `M` causes… | Classification |
|----------------------------|----------------|
| BOOT raises, plugins don't load, or the runtime can't resolve the configured model / complete a turn | **KEEP-IN-CORE** (harness) |
| Nothing on the liveness path breaks; only one *provider/model-type capability* disappears | **PLUGIN-CANDIDATE** |
| Passes only because **another in-harness module silently absorbs its job** (no clean seam yet) | **HARNESS-COUPLED** (extractable, seam must be designed first) |

A provider-specific wrinkle: `ModelFactory.get_model()` is a **dispatcher**. A
provider module is a clean PLUGIN-CANDIDATE only when its `model_type` is *not*
the globally-configured default and the dispatch already has (or trivially gains)
a `register_model_type` route. The dispatch chain itself **always stays**.

---

## 2. Per-Module Classification

### 2.1 `model_factory.py` (the dispatch binder) — **KEEP** (binder; over-cap split)
**991 lines** (≈1.65× the 600 cap, §12.1 rule 3). This is the **harness binder**:
`get_model()` (`:533`) resolves `model_type` → pydantic-ai `Model` via a 13-branch
`if/elif` chain (`:557` gemini … `:946` round_robin) and then an `else`
fallthrough to plugin handlers (`:969`). `load_config()` (`:415`) merges
bundled → OAuth files → `extra_models.json` → `load_models_config` plugins. It is
on the NO-OP-TURN path (every agent build resolves a model). **KEEP** — but the
*per-type leaf branches* are the extraction surface (§2.x below), and the file is a
prime **internal-split** target (dispatch vs. load_config vs. `get_custom_config`
vs. the inline `ZaiChatModel` class at `:334`).

### 2.2 `claude_cache_client.py` (Anthropic cache client + SDK patch) — **KEEP** (shared infra)
**843 lines** (≈1.4× cap). Backs the `anthropic` / `custom_anthropic` branches
(`:596/:644`) **and** is imported by the `aws_bedrock` and `azure_foundry` provider
plugins (`patch_anthropic_client_messages`). This is a **shared Anthropic-caching
primitive that plugins depend on** — the dependency arrow points the correct way
(plugin→core, §12.1 rule 1). It must stay so any Anthropic-family provider plugin
can reuse it. **KEEP** (over-cap → internal-split hygiene; *not* extraction).

### 2.3 `model_utils.py` (prompt-prep hook host + thinking policy) — **KEEP** (harness)
**185 lines.** `prepare_prompt_for_model` fires the `prepare_model_prompt` and
`get_model_system_prompt` hooks (`:60/:85`) and is imported across the turn path:
`_builder.py:422`, `_runtime.py:250`, `base_agent.py:214`, `summarization_agent.py`,
`tools/__init__.py:245`, `config.py:579`. It also holds Anthropic
adaptive-thinking capability policy (`supports_adaptive_thinking`, etc.). **KEEP.**
> **Smell for `tp4.6` (not a bug):** the adaptive-thinking *tag lists* are
> Anthropic-model-specific policy hardcoded in a "model-agnostic" util. Arguably
> belongs with an anthropic provider, but `claude_cache_client.py:50` also imports
> it — it's a genuine shared seam. Leave as-is; note it.

### 2.4 `provider_identity.py` (pydantic-ai `provider.name` boundary) — **KEEP** (harness) + coupling smell
**107 lines.** `resolve_provider_identity` + the `Aliased*Provider` classes give
pydantic-ai stable `provider.name` values (the replay/compat boundary). Used by
every native branch in `get_model`. **KEEP.**
> **Keystone smell (GAP-B, §3.2):** `_TYPE_PROVIDER_OVERRIDES` (`:40`) **hardcodes
> plugin provider identities** (`aws_bedrock`, `azure_openai`,
> `azure_foundry_openai`, `chatgpt_oauth`, `gemini_oauth`) *in core*. A plugin that
> registers a model type **cannot** register its own identity mapping → core knows
> about plugin types, inverting the arrow (§12.1 rule 1).

### 2.5 `provider_credentials.py` (credential discovery / hydration) — **KEEP** (cross-cutting)
**169 lines.** Single source of truth for "which `$ENV` each model needs," masking,
and `save_credential`. Used by `config.load_api_keys_to_environment` (`config.py:2130`)
and the `/model` + `/add_model` UX. On the credential-hydration path (keys must be
in env before model construction). **KEEP.**

### 2.6 `model_descriptions.py` (description overlays) — **KEEP** (tiny util)
**62 lines.** `apply_description_overlays` / `get_model_description`, used by
`load_config` (`:496`) and the picker UI. Surgical, model-agnostic. §12.3 YAGNI to
plugin-ize. **KEEP.**

### 2.7 `model_switching.py` (set-model + reload-agent glue) — **KEEP** (control glue)
**63 lines.** `set_model_and_reload_agent` is invoked by `/model` commands
(`core_commands.py:195`, `model_picker_completion.py:22`) and the `copilot_auth`
plugin. It's agent-lifecycle control glue, not provider-specific. **KEEP.**

### 2.8 `models_dev_parser.py` (models.dev catalog parser) — **KEEP** (UI support)
**592 lines** (+ the **535 KB** bundled `models_dev_api.json` snapshot). Sole
importer is `command_line/add_model_menu.py:30` (the `/add_model` browser).
Tied to a core `command_line/` feature (which §12.1 rule 1 says not to edit). It's
*adjacent* to the model layer but is really catalog/UX support. **KEEP** — though
the 535 KB JSON snapshot is an install-surface line item worth noting for `tp4.6`.

### 2.9 `chatgpt_codex_client.py` (Codex HTTP interceptor) — **PLUGIN-CANDIDATE** [split-brain] 
**393 lines.** **Already half-plugin:** the `chatgpt_oauth` dispatch branch was
*already* moved into the plugin (core note at `model_factory.py:944`), and the
**only** importer of `create_codex_async_client` is
`plugins/chatgpt_oauth/register_callbacks.py:113`. The client class is the *last*
piece still parked in core. Moving it *into* the `chatgpt_oauth` plugin is pure
relocation with a single importer. **LOW / LOW** — the textbook layup of this layer.

### 2.10 `gemini_code_assist.py` (Code Assist `Model`) — **PLUGIN-CANDIDATE** [most split-brain] 
**385 lines.** Backs the `gemini_oauth` type. The dispatch branch
(`model_factory.py:886`) already lazy-imports `code_puppy.plugins.gemini_oauth.*`
for oauth config/utils — **but that plugin isn't even in the builtin tier** (it's a
user-installed plugin), while the `Model` class *and* the dispatch branch sit in
core. This is the **most-split** provider: oauth in a plugin, transport + dispatch
in core. The clean finish mirrors `chatgpt_oauth`/`claude_code_oauth`: move the
`Model` class and the branch into the `gemini_oauth` plugin's `register_model_type`
handler. **LOW / MED** (depends on shipping/owning the `gemini_oauth` plugin).

### 2.11 `gemini_model.py` (native Gemini `Model`, no google-genai dep) — **PLUGIN-CANDIDATE** [native] 
**840 lines** (≈1.4× cap). A hand-rolled httpx Gemini `Model` backing the **native**
`gemini` + `custom_gemini` types (`:557/:788`). Self-contained (only `model_factory`
imports it). Extraction is **proven possible** by `register_model_type` (aws_bedrock
et al.), but `gemini` is a *default-install* provider, so this is "move impl +
re-register the type," analogous to `tp4.3`'s GAP-2 default-tool problem. **MED / MED**
— high *surface* value (840 ln + Gemini streaming off core) but it's a first-class
provider, so sequence it deliberately (see §3.3 GAP-C).

### 2.12 `round_robin_model.py` (meta-model) — **PLUGIN-CANDIDATE** [low value] 
**150 lines.** A composing meta-`Model` (rate-limit distribution), only imported by
`model_factory` with a `round_robin` branch (`:946`) that recursively calls
`ModelFactory.get_model`. A plugin handler could `register_model_type("round_robin")`
and recurse into the factory. Self-contained and clean, but small and generic.
**LOW / LOW-MED** — extractable, but low payoff; likely **defer** (Zen: don't move
for the sake of moving).

### 2.13 Inline `ZaiChatModel` + `zai_coding`/`zai_api` branches — **PLUGIN-CANDIDATE** [declutter] 
`ZaiChatModel` is defined **inline** in `model_factory.py:334`, with two dispatch
branches (`:755/:771`). A `zai` provider plugin (`register_model_type` ×2 +
`load_models_config`) would remove a class and two branches from the binder.
**LOW / MED** — clean declutter of the dispatcher.

---

## 3. Hook Coverage — can the model hooks supply each candidate?

| Candidate | `register_model_type` defines it? | `load_models_config` ships its models? | Coverage |
|-----------|:---:|:---:|----------|
| `chatgpt_codex_client` → `chatgpt_oauth` plugin | YES (branch already there) | YES (plugin already does) | **FULLY COVERED** — finish the move |
| `gemini_code_assist` → `gemini_oauth` plugin | YES (mirror chatgpt_oauth) | YES | **COVERED** w/ plugin-ownership caveat |
| `gemini_model` (native) → `gemini` plugin | YES (proven by aws_bedrock) | YES | **COVERED** w/ default-provider caveat (GAP-C) |
| `ZaiChatModel` → `zai` plugin | YES (×2 types) | YES | **FULLY COVERED** |
| `round_robin_model` → plugin | YES (recurse into factory) | n/a (meta) | **COVERED** (low value) |

The hooks are **sufficient to define and route** every candidate — the **5 shipping
provider plugins** (`aws_bedrock`, `azure_foundry`, `ollama`, `claude_code_oauth`,
`chatgpt_oauth`) are living proof that a provider can leave core with **zero edits to
the dispatch chain** (they land in the `:969` `else`). The gaps below are about
*precedence, identity, and the privileged native branches* — not about whether the
seam exists.

### 3.1 GAP-A — two overlapping provider-registration mechanisms
There are **two** plugin provider seams: `register_model_type`
(returns `{type, handler}`, consulted **last** at `:969`) **and**
`register_model_providers` (returns `{type: ProviderClass}`, stored in
`_CUSTOM_MODEL_PROVIDERS` and consulted **first** at `:547`, *before* the native
branches). Two registries, opposite precedence, undocumented overlap. Every shipping
plugin uses `register_model_type`; `register_model_providers` appears unused by any
builtin plugin. **Consolidate or document precedence.** Flag for `tp4.6`.

### 3.2 GAP-B — provider *identity* can't be registered by plugins
`provider_identity._TYPE_PROVIDER_OVERRIDES` hardcodes plugin provider names in
core (§2.4). A plugin registering a new `model_type` has **no hook** to also declare
its pydantic-ai `provider.name`, so it must either piggyback on a core-known
identity or fall through to the heuristic. **Proposal:** let a `register_model_type`
entry optionally carry `provider_identity`, and have `resolve_provider_identity`
consult registered types before its hardcoded map. Flag for `tp4.6`.

### 3.3 GAP-C — native types are a privileged hardcoded chain (the `tp4.3` GAP-2 analog)
The native `model_type` branches are a 13-arm `if/elif` (`:557–:946`) checked
**before** plugin handlers (`:969`). Adding/altering a native provider = a **core
dispatch edit**, while plugin providers are open via the `else`. This is the
model-layer twin of `tp4.3`'s "default-listed" asymmetry: native providers are
privileged and not removable via a hook. Extracting a *default* provider
(`gemini`) therefore means "move impl + register the same type name," and there is
no override/precedence between a native branch and a plugin handler of the same
type (native wins — the `elif` short-circuits before `:969`). **Sequence a
"native-branch → internal `register_model_type` table" refactor before extracting
any default provider** (Zen: flat is better than nested). Flag for `tp4.6`.

### 3.4 GAP-D — bundled catalog weight
`models.json` + the **535 KB** `models_dev_api.json` snapshot ride in the core
install. Not an extraction per se, but a payload `tp4.6` should weigh against the
"thin core" goal (could become a lazily-fetched/plugin-shipped catalog).

---

## 4. Risk / Effort Summary

| Candidate | Risk | Effort | Notes |
|-----------|:----:|:------:|-------|
| `chatgpt_codex_client` → `chatgpt_oauth` plugin | **LOW** | **LOW** | split-brain; single importer; pure relocation |
| `ZaiChatModel` + zai branches → `zai` plugin | **LOW** | **MED** | declutters the binder; inline class + 2 branches |
| `gemini_code_assist` → `gemini_oauth` plugin | **LOW** | **MED** | most-split; needs the `gemini_oauth` plugin owned/shipped |
| `gemini_model` (native) → `gemini` plugin | **MED** | **MED** | default provider; sequence after GAP-C |
| `round_robin_model` → plugin | **LOW** | **LOW-MED** | clean but low value — **defer** |
| GAP-A consolidate provider registries | **MED** | **MED** | two overlapping seams; pick one |
| GAP-B registerable provider identity | **MED** | **MED** | enabler for clean provider plugins |
| GAP-C native-branch → internal type table | **MED** | **MED-HIGH** | enabler; sequence *before* default-provider extraction |
| `model_factory.py` 991-ln internal split | **MED** | **MED** | hygiene (§12.1 rule 3), **not** extraction |
| `claude_cache_client.py` 843-ln / `gemini_model.py` 840-ln splits | **LOW-MED** | **MED** | hygiene; both KEEP |

---

## 5. Must-NEVER-Extract (provider harness floor)

Stubbing any of these breaks BOOT, model resolution, or the no-op turn:

- `model_factory.py` — `ModelFactory.get_model` / `load_config`, the dispatch
  binder every agent build calls.
- `claude_cache_client.py` — the shared Anthropic cache client/patch that **core
  and provider plugins** (`aws_bedrock`, `azure_foundry`) both depend on.
- `model_utils.py` — `prepare_prompt_for_model`, the prompt-prep hook host on the
  turn path (`_builder`, `_runtime`, `base_agent`, `summarization_agent`).
- `provider_identity.py` — the pydantic-ai `provider.name` compatibility boundary.
- `provider_credentials.py` — credential hydration feeding `config` + the pickers.
- `model_descriptions.py`, `model_switching.py` — tiny binder/control glue.
- `models_dev_parser.py` — bound to the core `/add_model` command (`command_line/`).

---

## 6. Proposed Follow-Up Beads (research only — **none built here**)

1. **Move `chatgpt_codex_client.py` into the `chatgpt_oauth` plugin** (single
   importer; branch already there). (LOW/LOW — do first.)
2. **Extract a `zai` provider plugin** — move the inline `ZaiChatModel` +
   `zai_coding`/`zai_api` branches out via `register_model_type` ×2 +
   `load_models_config`. (LOW/MED — declutters the binder.)
3. **Finish `gemini_oauth` extraction** — move `gemini_code_assist.py` and the
   `gemini_oauth` branch into a (builtin-or-owned) `gemini_oauth` plugin, mirroring
   `chatgpt_oauth`/`claude_code_oauth`. (LOW/MED — depends on plugin ownership.)
4. **Add a `register_model_type` provider-identity field + consult it in
   `resolve_provider_identity` (GAP-B)** so plugin providers stop being hardcoded in
   core. (MED.)
5. **Refactor the native `if/elif` dispatch into an internal `register_model_type`
   table (GAP-C)** so native and plugin providers share one lookup with defined
   precedence — the prerequisite for cleanly extracting a *default* provider. (MED.)
6. **Extract `gemini_model.py` into a `gemini` plugin** (native API-key Gemini),
   sequenced *after* bead 5. (MED.)
7. **Consolidate `register_model_providers` and `register_model_type` (GAP-A)** —
   one documented seam with defined precedence. (MED.)
8. **Internal split of `model_factory.py` (991 ln), `claude_cache_client.py`
   (843 ln), `gemini_model.py` (840 ln)** to honor the 600-line cap. (MED; hygiene,
   stays in core.)
9. **Evaluate lazy/plugin-shipped `models_dev_api.json` (535 KB) (GAP-D)** to trim
   the core install surface. (Parked; weigh in `tp4.6`.)

---

## 7. Findings for the Synthesis Bead (`tp4.6`)

- **"Providers as plugins" is PROVEN, not theoretical.** Five plugins
  (`aws_bedrock`, `azure_foundry`, `ollama`, `claude_code_oauth`, `chatgpt_oauth`)
  already register model types and land in the `get_model` `else` fallthrough
  (`model_factory.py:969`) with **zero edits to the dispatch chain**. The audit is
  about *which leaves are clean to move*, not *whether* the seam exists.
- **Two providers are already half-extracted (split-brain):** `chatgpt_codex_client`
  (dispatch already in the plugin; only the client class left in core) and
  `gemini_code_assist`/`gemini_oauth` (the *most-split* — oauth in a non-builtin
  plugin, transport + dispatch still in core). Finishing both is low-risk and
  *removes* core weight.
- **The cleanest brand-new wins** are `chatgpt_codex_client` (LOW/LOW) and the
  inline `ZaiChatModel` declutter (LOW/MED).
- **`gemini_model.py` is the biggest single payoff** (840 ln + Gemini streaming off
  core) but it's a *default* provider, so it must wait on the GAP-C native-dispatch
  refactor.
- **The keystone blockers are seam asymmetries, not modules:**
  **(GAP-A)** two overlapping provider registries with opposite precedence;
  **(GAP-B)** plugins can't register their pydantic-ai provider identity (it's
  hardcoded in `provider_identity.py`); **(GAP-C)** native types are a privileged
  hardcoded `if/elif` checked before plugin handlers — the model-layer twin of
  `tp4.3`'s default-tool problem. **Sequence GAP-B/GAP-C before extracting any
  default provider.**
- **The harness floor is real:** `model_factory` (dispatch), `claude_cache_client`
  (shared infra that *plugins* import), `model_utils` (prompt-prep hook host),
  `provider_identity`/`provider_credentials` (compat + key hydration) all sit on the
  model-resolution / turn path and must stay.
- **Three over-cap hygiene items rode along:** `model_factory.py` (991 ln),
  `claude_cache_client.py` (843 ln), `gemini_model.py` (840 ln) need internal splits
  — but they (mostly) **KEEP**; that's §12.1 rule 3, not extraction.
- **Install-surface note:** the bundled `models_dev_api.json` is **535 KB** — weigh
  a lazy/plugin catalog against the thin-core goal.
