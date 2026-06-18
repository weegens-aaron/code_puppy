# Spike puppy-27g.1 — Builtin Plugin Loading / Precedence Inventory & Externalization Risks

> **DISCOVERY spike — research only.** No implementation here. This document
> inventories how builtin plugins load *today*, the precedence model, and the
> concrete risks of shipping builtins into the user/external dir instead of the
> in-package builtin tier. Follow-up implementation beads are *proposed*, not
> built.
>
> Parent epic: **puppy-27g — Externalize Builtin Plugins with Hash-Aware Updates**.
> Sibling: **puppy-27g.2** (`docs/PLUGIN_HASH_AWARE_UPDATES.md`) designs the
> hash-aware update algorithm; this bead supplies the loader/precedence ground
> truth that algorithm must respect.

---

## 0. Architecture Grounding (REQUIRED FIRST STEP)

Per the project's hard acceptance contract, the **first step was to activate
the `code-puppy-agent` architecture skill**, and each finding/decision below is
tied back to the specific `SKILL.md` section that constrains it. This is not
ceremony — the skill is the authoritative description of the loader's intended
behaviour, and the externalization risks are *deviations from* what the skill
documents.

| # | Finding / claim in this doc | Grounded in `code-puppy-agent` SKILL.md |
|---|------------------------------|------------------------------------------|
| 1 | Three tiers load builtin → user → project; project wins on collision | §4.1 *Plugin discovery (three tiers)* |
| 2 | Plugin = directory + `register_callbacks.py`; register at module scope; loader auto-discovers | §4.2, §4.3, §12.2 |
| 3 | Externalized plugins must "fail gracefully / never crash the app" and live flat under `~/.code_puppy/plugins/` | §12.1 (rule 4), §10 (config dirs) |
| 4 | Precedence-by-name analogy: project overrides user exactly like JSON agents (`discover_json_agents`) | §2.2 *Discovery & precedence* |
| 5 | "Plugins over core / if a hook exists, use it" — the shell_safety conditional-skip is core logic, not a hook, and is a relocation hazard | §12.1 (rule 1), §12.3 *Zen* |
| 6 | The loader lives at `plugins/__init__.py`; callbacks engine at `callbacks.py` | §13 *Key File Map* |
| 7 | "Flat is better than nested / files readable in one sitting / 600-line cap" governs the proposed bootstrap-sync module | §12.3, §12.1 (rule 3) |

> The single most important grounding consequence: the skill (§4.1) describes
> all three tiers as "the same pattern — drop a `register_callbacks.py`," but the
> **code does not treat them the same** (see §3 below). That gap *is* the
> externalization risk.

---

## 1. The Current Load Pipeline (exact functions)

All code referenced lives in `code_puppy/plugins/__init__.py` unless noted.

### 1.1 Entry point & idempotency

`load_plugin_callbacks() -> dict[str, list[str]]` is the single entry point. It
is **idempotent**, guarded by the module-global `_PLUGINS_LOADED`. The first
call loads everything; subsequent calls log a debug line and return
`{"builtin": [], "user": [], "project": []}`. This matters because it is invoked
from *many* call sites (all relying on the guard):

- `code_puppy/cli_runner.py:51` — **at module import time** (the real startup path)
- `code_puppy/command_line/command_registry.py:47`
- `code_puppy/command_line/command_handler.py:149`
- `code_puppy/command_line/prompt_toolkit_completion.py:465`
- `code_puppy/plugins/wiggum/judge.py:208`

Sequence inside `load_plugin_callbacks()`:

```
1. if _PLUGINS_LOADED: return empty dict           # idempotency gate
2. plugins_dir = Path(__file__).parent             # the in-package builtin dir
3. project_plugins_dir = get_project_plugins_directory()
4. project_plugin_names = _scan_plugin_names(project_plugins_dir)   # cheap FS scan, no import
5. builtin_loaded = _load_builtin_plugins(plugins_dir)
6. user_skip_names = set(builtin_loaded) | project_plugin_names
7. user_loaded = _load_user_plugins(USER_PLUGINS_DIR, skip_names=user_skip_names)
8. project_loaded = _load_project_plugins(project_plugins_dir, builtin_names, user_names)
9. _PLUGINS_LOADED = True; _loaded_plugin_names.update(result)
10. return {"builtin": ..., "user": ..., "project": ...}
```

### 1.2 Builtin tier — `_load_builtin_plugins(plugins_dir)`

- Iterates `plugins_dir.iterdir()`, skipping dirs whose name starts with `_`.
- Requires a `register_callbacks.py` to exist in the subdir.
- **Conditional skip:** if `plugin_name == "shell_safety"`, it calls
  `get_safety_permission_level()` and **skips loading** unless the level is
  `"none"` or `"low"`. This gating lives *only* in the builtin loader.
- Imports via **`importlib.import_module(f"code_puppy.plugins.{plugin_name}.register_callbacks")`**
  — i.e. as a **real, fully-qualified package module**. This is the critical
  detail: `__package__` is `code_puppy.plugins.<name>`, so both absolute
  (`from code_puppy.plugins.<name>.sub import …`) and relative (`from . import …`)
  imports resolve naturally because the parent package genuinely exists on disk
  inside the installed wheel.
- Wraps each import in try/except `ImportError` (warning) and bare `Exception`
  (error) — fail-graceful (SKILL §12.1 rule 4).

### 1.3 User tier — `_load_user_plugins(USER_PLUGINS_DIR, skip_names)`

- `USER_PLUGINS_DIR = Path.home() / ".code_puppy" / "plugins"` (SKILL §10).
- Returns early if the dir doesn't exist (no auto-create here; `ensure_user_plugins_dir()` is the explicit creator).
- Inserts `str(user_plugins_dir)` at `sys.path[0]`.
- Skips dirs starting with `_` or `.`, **and** any name in `skip_names`
  (= builtin names ∪ project names) so higher-precedence tiers win.
- Imports via **`importlib.util.spec_from_file_location(f"{plugin_name}.register_callbacks", callbacks_file)`**
  and registers `sys.modules["{plugin_name}.register_callbacks"]`.
  - **There is no synthetic parent package** `sys.modules["{plugin_name}"]`
    with a `__path__`. (Contrast the project tier, §1.5.)
- Fallback: if no `register_callbacks.py`, it tries `__init__.py` registered as
  `sys.modules[plugin_name]` (bare top-level name).

### 1.4 Project name pre-scan — `_scan_plugin_names(plugins_dir)`

Cheap filesystem-only scan (no imports). Returns the set of subdir names that
have a `register_callbacks.py` **or** `__init__.py`. Used so user plugins that
the project tier will supersede are skipped (project-wins, mirroring JSON-agent
dedup — SKILL §2.2).

### 1.5 Project tier — `_load_project_plugins(dir, builtin_names, user_names)`

- `get_project_plugins_directory()` returns `<CWD>/.code_puppy/plugins` **only
  if it already exists** — never auto-created (team opts in intentionally).
- Inserts the project dir at `sys.path[0]`.
- `_ensure_project_ns()` creates a synthetic top-level namespace package
  `project_plugins` (a `types.ModuleType` with `__path__ = []`).
- For each plugin, `_ensure_plugin_package(item, plugin_name)` registers
  `sys.modules["project_plugins.<name>"]` — either by executing the plugin's
  `__init__.py` (with `submodule_search_locations=[plugin_dir]`) or, failing
  that, a bare namespace module whose `__path__` points at the plugin dir.
  **This is what makes relative imports (`from . import state`) resolve.**
- Imports `register_callbacks.py` as `project_plugins.<name>.register_callbacks`.
- **Warns** (`logger.warning`) when a project plugin name shadows a builtin.
- Module namespace isolation: project plugins live under `project_plugins.*`
  in `sys.modules`, so they can't collide with user-tier import names.

### 1.6 Precedence model (net)

| Concern | Behaviour | Mechanism |
|---------|-----------|-----------|
| Load order | builtin → user → project | call order in `load_plugin_callbacks()` |
| user vs builtin name clash | builtin wins (user skipped) | `skip_names ⊇ builtin_loaded` |
| user vs project name clash | project wins (user skipped) | `skip_names ⊇ project_plugin_names` (pre-scan) |
| project vs builtin name clash | **both load**; project loads last → its callbacks register after builtin's; warning emitted | no skip, only `logger.warning` |
| disabled plugins | still imported, callbacks **skipped at dispatch** | `plugins/config.py` `disabled_plugins` set, read by `callbacks.py` |

> Note the asymmetry: a project plugin that *shadows a builtin* does **not**
> prevent the builtin from also registering its callbacks — both fire (the
> system only warns). De-duplication of identical callback *functions* is
> handled separately in `register_callback()` (`callbacks.py`), but two
> *different* implementations of the same hook from a builtin + a same-named
> project plugin will both run. Relevant if externalization changes a plugin's
> tier.

### 1.7 Ownership tracking

`callbacks.py` exposes `set_loading_context(name)` / `clear_loading_context()`.
The loader brackets every plugin import with these so `register_callback()` can
record `_callback_owners[func] = name`. This powers `/plugins` listing and
per-plugin disable. Any externalization must keep calling these around the
import or ownership/disable breaks.

---

## 2. Builtin Plugin Inventory (import-style classification)

The decisive risk factor for externalization is **how each builtin imports its
own submodules**, because that determines whether it can be relocated to the
user tier as-is. Two mutually-incompatible styles exist today:

### 2.1 Absolute-import builtins (`from code_puppy.plugins.<name>.<sub> import …`)

These hard-code the in-package path. Examples found by grep:

- `agent_skills` (discovery, downloader, skill_catalog, skills_menu, register_callbacks, …)
- `shell_safety` (`command_cache`, `agent_shell_safety`)
- `token_ratio_learner` (`ratios`)
- `context_indicator` (`usage`; also reaches into `puppy_kennel.retriever`)
- `destructive_command_guard` (`detector`)
- `frontend_emitter` (`emitter`, `session_context`)
- `force_push_guard` (`detector`)
- `prompt_newline` (`config`)
- `plugin_list`, `example_custom_command` (import `code_puppy.plugins.config` / `customizable_commands`)

### 2.2 Relative-import builtins (`from . import x`, `from .sub import …`)

- `wiggum`, `agent_steering`, `universal_constructor`, `aws_bedrock`,
  `azure_foundry`, `claude_code_hooks`, `claude_code_oauth`, `chatgpt_oauth`,
  `copilot_auth`, `dbos_durable_exec`, `obsidian_agent`, `theme`.

### 2.3 Cross-plugin coupling (a third hazard)

Some builtins import *other* builtins by absolute path, e.g.:

- `context_indicator/usage.py` → `code_puppy.plugins.puppy_kennel.retriever`
- `agent_skills/skill_commands.py` → `code_puppy.plugins.customizable_commands.register_callbacks`
- `example_custom_command` → `code_puppy.plugins.customizable_commands.register_callbacks`
- `plugin_list` & `callbacks.py` → `code_puppy.plugins.config`

These create a **dependency graph between builtins**, so they cannot be
externalized individually without breaking siblings.

---

## 3. Externalization Risks & Edge Cases

Goal under analysis: ship builtins into `~/.code_puppy/plugins/` (the external
user dir) instead of the in-package builtin tier. Risks, most-severe first:

### 3.1  Internal imports break — *both* styles (BLOCKING)

This is the headline finding.

- **Absolute-import builtins (§2.1):** `from code_puppy.plugins.shell_safety.command_cache import …`
  resolves against the *installed package*. The entire point of externalization
  is to **remove** the in-package copy — at which point `code_puppy.plugins.shell_safety`
  no longer exists and the import raises `ModuleNotFoundError`. If we *keep* the
  in-package copy as a fallback, we now ship the plugin twice and the externalized
  copy's edits are silently ignored (its absolute imports still bind to the
  in-package modules). Either way: broken or pointless.
- **Relative-import builtins (§2.2):** the user tier (`_load_user_plugins`)
  registers `sys.modules["<name>.register_callbacks"]` but **never creates a
  parent package** `sys.modules["<name>"]` with a `__path__`. So `from . import
  state` fails — there is no package for `.` to refer to. The project tier solves
  this via `_ensure_plugin_package`/`_ensure_project_ns`; **the user tier does
  not**. So today's user loader can only safely host *single-file or
  fully-self-contained* plugins — which the multi-file builtins are not.

> Conclusion: the current user-tier loader cannot host the existing builtins
> *as written*, regardless of import style. Externalization is therefore not a
> file-copy; it requires loader changes and/or a plugin-rewrite convention.

### 3.2  `sys.path` pollution & top-level name shadowing

Both user and project loaders `sys.path.insert(0, …)`. The `__init__.py`
fallback in the user tier registers plugins under their **bare** name
(`sys.modules[plugin_name]`). A plugin dir literally named `config`, `state`,
`utils`, `token`, or `theme` would register a top-level module of that name,
shadowing other modules imported by bare name later in the process. Several
builtins have submodules with exactly these generic names (`config.py`,
`state.py`, `utils.py`, `token.py`), and with the dir on `sys.path[0]` a bare
`import config` from anywhere could resolve to the wrong file.

### 3.3  Version drift / partial upgrades

If builtins live in the user dir, `pip install -U code-puppy` updates the
*package* but not the *user-dir copies*. Stale externalized plugin code then
runs against newer core (or vice-versa) → API mismatch, subtle breakage. This is
exactly the problem **puppy-27g.2** (hash-aware updates) exists to solve; this
bead confirms the loader provides **no** version guard today — nothing compares
plugin version to core version at load time.

### 3.4  Fresh-install bootstrap is a new failure surface

A clean machine has an empty (or non-existent) `~/.code_puppy/plugins/`. With
externalized builtins, **core functionality is missing until a bootstrap step
populates the dir**. That introduces new failure modes the current design never
had: disk-full, permission denied, partial copy, interrupted first run, a
read-only `$HOME` (CI/containers). Today builtins ship inside the wheel and are
always present — bootstrap cannot fail.

### 3.5  The `shell_safety` conditional-skip is core logic, not a hook

`_load_builtin_plugins` hard-codes "skip `shell_safety` unless
`safety_permission_level` ∈ {none, low}". The user tier has **no equivalent
gate**. Externalizing `shell_safety` would either (a) lose the gate → it always
loads, a behavioural regression, or (b) require re-implementing the gate in the
user loader. Per SKILL §12.1 rule 1 / §12.3 ("if a hook exists for it, use it"),
this conditional really wants to become a general mechanism rather than a
special-case `if plugin_name == "shell_safety"`.

### 3.6  Tier-collision determinism

If a builtin is moved into the user dir *and* a user has their own plugin of the
same name in the same dir, there is no builtin tier left to arbitrate — they're
both "user tier," distinguished only by `iterdir()` order, which is
**filesystem-dependent and non-deterministic**. The current builtin→user skip
logic (`skip_names`) silently assumes the two never share a tier.

### 3.7  Cross-plugin dependency graph (§2.3)

Externalizing one builtin can break a sibling that imports it by absolute path
(`context_indicator` → `puppy_kennel`, `agent_skills` → `customizable_commands`,
`plugin_list` → `plugins.config`). Externalization must be all-or-nothing across
a dependency cluster, or the shared modules (`plugins/config.py`, etc.) must stay
in-package as a stable API.

### 3.8  Reported-tier semantics change

`get_loaded_plugins()` groups names by tier and feeds the `/plugins` menu.
Moving builtins to the user tier changes their reported tier, which any logic
keying on "is this a builtin?" would observe. Low-severity but a behavioural
contract change.

### 3.9  Idempotency vs. re-sync

`_PLUGINS_LOADED` makes loading one-shot per process. A bootstrap/update that
writes files to the user dir must run **before** the (idempotent) load, because
there is no supported "reload plugins" path mid-process. (27g.2's design already
assumes sync-at-startup; this confirms the loader gives no second chance.)

---

## 4. What Must Be True for Externalization to Be Safe

A clear, testable checklist (the synthesis bead can turn each into a
follow-up implementation bead):

1. **Relocatable imports.** Externalized plugins must not use absolute
   `code_puppy.plugins.<name>.*` imports. Either (a) convert all to relative
   imports *and* upgrade the user loader to build a synthetic parent package
   (mirror `_ensure_plugin_package`/`_ensure_project_ns` from the project tier),
   or (b) keep absolute imports but keep those plugins in-package. You cannot mix.

2. **User tier must support multi-file plugins.** Today it does not (no parent
   package, no `__path__`). The project tier already solved this; the fix is to
   factor that namespace-package logic into a shared helper used by *both*
   non-builtin tiers. (SKILL §4.1 promises all three tiers are "the same
   pattern" — make the code honour that promise.)

3. **Hash-aware bootstrap + update.** A startup-time sync must populate the user
   dir on fresh install and update it on upgrade, detecting user-modified files
   so it never clobbers local edits. This is precisely **puppy-27g.2's**
   BASE/NEW/CUR 3-way design — externalization *depends on* it landing first.

4. **Version-compatibility guard.** The loader (or sync) must refuse to run an
   externalized plugin whose declared version is incompatible with core, to
   contain §3.3 drift.

5. **Relocate the conditional-load gate.** `shell_safety`'s skip logic must move
   out of `_load_builtin_plugins` into a tier-agnostic mechanism (e.g. a manifest
   field "load only if config predicate holds", or a `should_load` hook).

6. **Deterministic precedence when a builtin and a real user plugin share a
   name** in the same dir — needs an explicit rule, not `iterdir()` order.

7. **`sys.path` hygiene.** Avoid bare-name top-level registration for the
   `__init__.py` fallback; namespace externalized plugins (e.g. an
   `external_plugins.<name>` namespace, paralleling `project_plugins.<name>`) to
   prevent §3.2 shadowing.

8. **Fail-graceful bootstrap.** Per SKILL §12.1 rule 4, a failed sync/bootstrap
   (read-only $HOME, partial copy) must degrade gracefully — ideally falling back
   to an in-package copy — never crash startup.

---

## 5. Findings for the Synthesis Bead (puppy-27g.4 / ADR puppy-1ng)

- **Three tiers are documented as identical but implemented differently.** The
  builtin tier imports as a real package (`importlib.import_module`); the user
  tier uses `spec_from_file_location` with **no parent package**; only the
  project tier builds a synthetic namespace package supporting relative imports.
  *This asymmetry is the root externalization blocker.*
- **Builtins split ~50/50 between absolute and relative internal imports**, and
  several import each other. Neither style survives a naive copy to the user dir.
- **The user loader cannot host multi-file plugins today.** Fixing externalization
  is mostly a *loader* problem, not just a *packaging* problem.
- **`shell_safety` carries core-side conditional-load logic** that has no home in
  the user tier.
- **No version guard exists** at load time → 27g.2's hash-aware update is a hard
  prerequisite, not a nice-to-have.
- **Recommended sequencing for the ADR:** (1) generalize the project tier's
  namespace-package machinery into a shared helper and adopt it for the user
  tier; (2) standardize externalized plugins on relative imports + a manifest;
  (3) land 27g.2 hash-aware sync as the bootstrap/update engine; (4) only then
  relocate builtins, dependency-cluster by dependency-cluster, starting with
  self-contained leaf plugins (no cross-plugin imports).

### Proposed follow-up beads (do NOT implement in this epic)

- *Loader unification:* extract `_ensure_plugin_package`/`_ensure_project_ns`
  into a shared helper; make the user tier build a parent namespace package.
- *Import-style normalization:* convert builtin internal imports to a single
  relocatable convention; document it in `CONTRIBUTING`/SKILL.
- *Conditional-load mechanism:* replace the `shell_safety` special-case with a
  declarative "load predicate" in the plugin manifest.
- *Tier-collision policy:* define + test deterministic precedence for same-name
  plugins across tiers.
- *Bootstrap module:* startup sync that populates/updates the external dir using
  27g.2's hash-aware algorithm, with graceful in-package fallback.

---

*Spike complete. No code changed. All claims above are grounded in the
`code-puppy-agent` skill (see §0) and verified directly against
`code_puppy/plugins/__init__.py`, `code_puppy/callbacks.py`,
`code_puppy/plugins/config.py`, `code_puppy/plugins/shell_safety/register_callbacks.py`,
and a repo-wide import-style grep.*
