# Spike puppy-27g.3 — Alternatives to File-Copy Externalization

> **DISCOVERY spike — research only.** No implementation here. This document
> evaluates **alternatives** to the "copy every builtin into the user dir and
> hash-sync it" approach (the *"or something better"* branch of epic `27g`).
> Follow-up implementation beads are *proposed*, not built.
>
> **Parent epic:** `puppy-27g` — Externalize Builtin Plugins with Hash-Aware Updates.
> **Siblings consumed as input:**
> - `puppy-27g.1` (`docs/PLUGIN_EXTERNALIZATION_INVENTORY.md`) — loader/precedence
>   ground truth & externalization risks.
> - `puppy-27g.2` (`docs/PLUGIN_HASH_AWARE_UPDATES.md`) — the hash-copy update
>   algorithm, which is the **baseline** this bead compares against.
> **Feeds:** `puppy-27g.4` (synthesis) and ADR `puppy-1ng` (the decider — this
> doc only *leans*).

---

## 0. Architecture Grounding (REQUIRED FIRST STEP)

Per the project's hard acceptance contract, the **first step was to activate the
`code-puppy-agent` architecture skill**, and each alternative/decision below is
tied back to the specific `SKILL.md` section that constrains it. The skill is
the authoritative description of how the loader, callbacks, and config dirs
actually behave; an "alternative externalization strategy" that ignored it would
just be a daydream. Every claim was *also* verified directly against
`code_puppy/plugins/__init__.py`.

| # | Alternative / decision in this doc | Grounded in `code-puppy-agent` SKILL.md | Why it constrains the analysis |
|---|------------------------------------|------------------------------------------|--------------------------------|
| 1 | All alternatives target the **user-tier** dir `~/.code_puppy/plugins/` as the externalization surface | §4.1 *Plugin discovery (three tiers)* + §10 *Configuration System* | Externalization moves files from the read-only **builtin** tier into a real, documented user dir — not an invented path. |
| 2 | **Overlay (A)** & **import-hook (D)** keep builtins in the wheel and rely on the builtin loader importing them as a *real package* (`importlib.import_module`) | §4.1 (tiers) + §13 *Key File Map* (loader = `plugins/__init__.py`) | Because the builtin tier imports a genuine package, an overlay can intercept at the package `__path__`/meta-path level; this is the technical hinge for A and D. |
| 3 | **Eject (B)** is exposed as a `/plugins eject <name>` **custom command**, not a core CLI edit | §3.4 *Plugin tools* + §4.2 `custom_command` hook + §12.1 rule #1 (*plugins over core*) | New user-facing functionality must be a plugin/hook, not an edit to `command_line/`. |
| 4 | Commands return `None` when not theirs; eject reviewer is plugin-first | §4.2 (`custom_command` returns `None` when not owned) | Keeps the reviewer composable with the existing `/plugins` surface. |
| 5 | **CAS (C)** and **import-hook (D)** are *rejected* largely on simplicity grounds | §12.3 *Zen of Code Puppy* (*simple > complex, flat > nested, readable in one sitting*) | The Zen is a real evaluation axis here, not decoration — it rules out the two most magical options. |
| 6 | Every alternative must degrade gracefully and keep an always-present fallback | §12.1 rule #4 (*fail gracefully — never crash the app*) | The in-package canonical copy is the cheapest possible fallback; the baseline throws it away (see §2). |
| 7 | The recommended hybrid **reuses 27g.2's BASE/NEW/CUR hash logic**, scoped to the sparse overlaid set | §13 (loader location) + sibling `27g.2` | We don't discard the hash-aware design; we *narrow its blast radius*. |
| 8 | Net precedence (builtin → user → project; project shadows user; **builtin + project both fire** on clash) drives the "eject needs a real shadow mechanism" finding | §4.1 (*load order … project shadows user on collision*) + §2.2 (precedence analogy) — corrected against the live loader | The skill's "project shadows user" promise is *not* matched by builtin-vs-project (both load, warning only); any shadow-based alternative must build the missing mechanism. |

> **The single most important grounding consequence:** the baseline (27g.2)
> *eagerly relocates everything*, which is precisely what manufactures
> `27g.1`'s worst risks — §3.1 (absolute imports break), §3.4 (fresh-install
> bootstrap becomes a new failure surface), §3.5 (the `shell_safety` gate loses
> its home). Every alternative below is, at heart, a way to **keep the canonical
> in-package copy** so those risks evaporate. That observation *is* the spike.

---

## 1. The Baseline Being Challenged (B0)

**B0 = Eager hash-copy externalization** (designed in `27g.2`):

> On startup, sync *all* builtin plugins from the wheel into
> `~/.code_puppy/plugins/`, using a BASE/NEW/CUR three-way hash model and a
> `.new` sidecar for conflicts. After sync, the user-tier loader runs the
> on-disk copies.

B0 is an excellent *update algorithm*. The question this bead asks is whether
**eager, total, copy-based** externalization is the right *strategy* — or whether
a lazier / non-copying strategy delivers the same user benefit (editable plugin
files) with less risk. The benefit we are trying to preserve in every option:

> **A user can read and edit the code of a builtin plugin, and their edits
> survive upgrades.**

Everything else (copying, manifests, sidecars) is *mechanism*, not *goal*.

### 1.1 What B0 costs (from `27g.1`, restated as liabilities)

- **L1 — Absolute imports break (§3.1).** ~half the builtins `import
  code_puppy.plugins.<name>.<sub>`; once the canonical copy is gone, these
  raise `ModuleNotFoundError`, and the user-tier loader builds **no parent
  package** so relative-import builtins break too.
- **L2 — Bootstrap is a new failure surface (§3.4).** A clean machine has core
  functionality *missing* until a copy succeeds: disk-full, read-only `$HOME`,
  CI/containers, partial copy. Today builtins ship in the wheel and *cannot* be
  absent.
- **L3 — The `shell_safety` conditional-load gate (§3.5)** is core logic in
  `_load_builtin_plugins`; the user tier has no equivalent.
- **L4 — CRLF/LF hash instability (`27g.2` §6.2)** — every Windows user risks
  spurious conflicts on every update.
- **L5 — Cross-plugin dependency clusters (§2.3/§3.7)** must move all-or-nothing.

The alternatives are scored on **how many of L1–L5 they neutralize** for free.

---

## 2. The Alternatives

### Alternative A — In-package canonical + sparse per-file user overlay (no bulk copy)

**Idea.** Builtins stay in the wheel as the *canonical, read-only* copy. The
user dir is an **overlay**: it holds *only* the files the user chose to
override. At load time, for each plugin file the loader resolves
**user-overlay-first, in-package-fallback**. Nothing is copied up front; the
user materializes a file only when they want to change it (e.g. via a
`/plugins override <name>/<file>` helper that drops one editable copy).

Mechanism options (both grounded in §4.1 — builtin loads as a real package):
- prepend the per-plugin overlay dir to that package's `__path__`, or
- a thin resolver in the loader that prefers `~/.code_puppy/plugins/<name>/<file>`
  over the package file before `importlib.import_module`.

| | |
|---|---|
| **Pros** | Canonical copy always present → **L2 gone** (bootstrap can't fail; there's always a working copy) and **L4 hugely reduced** (only sparse overridden files are hashed). **L3 stays put** (the in-package gate is untouched). Overrides are explicit and tiny → minimal blast radius. Update of *non-overridden* files is free — `pip` updates the wheel. |
| **Cons** | **L1 still bites the overridden file's imports**: an overridden `shell_safety/command_cache.py` whose siblings `import code_puppy.plugins.shell_safety.x` will still bind to the *wheel* copies unless `__path__` is manipulated per-plugin (doable, but fiddly). Discoverability is lower (you can't `ls` all plugins on disk; only your overrides show). You still need a *small* hash-base record per overridden file to answer "is my override based on a stale version?" (a scoped slice of 27g.2, not a replacement for it). |

### Alternative B — Opt-in per-plugin **eject** command

**Idea.** Builtins stay in-package by default. `/plugins eject <name>` copies
**exactly one plugin** to the user dir on demand; from then on, that user copy
*shadows* the builtin and the user owns it. The 95% of plugins nobody touches
never leave the wheel.

This is a `custom_command` plugin (§3.4/§4.2), not a core edit.

| | |
|---|---|
| **Pros** | Smallest realistic blast radius — only ejected plugins externalize, so **L2 is a non-event** (default install ejects nothing) and **L4 only touches the handful ejected**. **L3 stays put** unless `shell_safety` itself is ejected. Crisp mental model: "I ejected it → I own it." Reuses 27g.2's hash sync but **scoped to the ejected set** — narrows its scope rather than discarding it. |
| **Cons** | **Requires a shadow mechanism that does not exist today**: the live loader runs **builtin *and* project copies on a name clash (warning only)** — so an "ejected copy must win, builtin must step aside" rule is *net-new* loader work (a concrete follow-up bead). **L1/L5 reappear for ejected plugins**: ejecting `agent_skills` (which absolute-imports `customizable_commands`) means its absolute imports still bind to the wheel, so cross-cluster ejects must move the whole cluster. Discoverability of "what *can* I eject" needs a list command. |

### Alternative C — Versioned manifest + content-addressed store (CAS)

**Idea.** Store every shipped file as a blob keyed by its sha256 under
`~/.code_puppy/plugins/.store/<hash>`, and materialize the plugin tree from a
version-pinned manifest (via copy, hard-link, or symlink). Updates write new
blobs and flip the manifest pointer; old versions are retained; rollback is a
pointer flip.

| | |
|---|---|
| **Pros** | Transactional, atomic updates (flip one pointer); trivial rollback; cross-version dedup; a genuinely strong *update* story. |
| **Cons** | **Wildly over-engineered for ~30 tiny `.py` files** — a flagrant YAGNI / "git internals for a config dir" violation of §12.3 (*simple > complex, flat > nested*). **Symlinks/hardlinks are a Windows minefield** — and §6.2 already flags Windows as the danger zone. **Discoverability is terrible** (opaque hash blobs, indirection to read your own plugin). The user-override story is *worse*: editing a materialized symlink mutates a shared blob or breaks the link. Large machinery = large blast radius. It solves a problem (multi-version retention) that the epic does not have. |

### Alternative D — Import-hook / layered virtual filesystem overlay

**Idea.** Install a `sys.meta_path` finder that intercepts
`code_puppy.plugins.<name>.*` imports and resolves user-overlay-first,
in-package-fallback — *virtually*, at import time. No files copied; the overlay
is purely an import-resolution layer.

| | |
|---|---|
| **Pros** | **Elegantly kills L1** — the finder can transparently redirect `code_puppy.plugins.shell_safety.command_cache` to a user file, so *both* absolute- and relative-import builtins "just work" with no rewrite. No bulk copy; canonical fallback always present (**L2 gone**); sparse overrides. |
| **Cons** | **Deep import magic** — a frontal assault on §12.3 (*simple is better than complex*) and §12.1 rule #4 (*never crash the app*): `sys.meta_path` is shared with pydantic-ai, pytest's assertion rewriter, coverage, and import-time monkeypatches (`pydantic_patches.py`). A misbehaving finder can break **all** imports, not just plugins — the worst-possible blast radius. Debugging import resolution is a nightmare; fragile across Python versions. Still needs a base-hash record to flag stale overrides. The cost/benefit is upside-down: it solves L1 beautifully but risks everything else. |

---

## 3. Comparison Matrix vs the Hash-Copy Baseline

Scored on the dimensions the acceptance criteria name (**override safety,
update cost, UX/discoverability, risk/blast radius**) plus the four other axes
that `27g.1` proved matter. Legend: **`++`** strong / **`~`** mixed / **`--`** weak.

| Dimension | **B0 Hash-copy (baseline)** | **A Overlay** | **B Eject** | **C CAS** | **D Import-hook** |
|-----------|------------------------------|---------------|-------------|-----------|-------------------|
| **Override safety** (user edits never clobbered) | `++` (3-way + `.new`) | `++` (canonical untouched; override is the user's own file) | `++` (ejected file is the user's) | `--` (symlink edits mutate shared blob) | `++` (overlay is user's own file) |
| **Update cost / simplicity** | `~` (full manifest + sync every release) | `++` (`pip` updates wheel; only sparse overrides need a hash check) | `++` (sync scoped to ejected set) | `--` (build CAS, manifest pointers, GC) | `~` (no copy, but finder must stay correct forever) |
| **UX / discoverability** | `++` (all plugins visible on disk) | `~` (only overrides on disk; needs a "show source" helper) | `~` (needs `/plugins list-ejectable`) | `--` (opaque hash blobs) | `--` (nothing on disk unless overridden) |
| **Risk / blast radius** | `--` (whole tree moves; L1/L2/L3 all active) | `++` (sparse; canonical fallback) | `++` (per-plugin; canonical fallback) | `--` (huge machinery; Windows symlinks) | `--` (can break *all* imports) |
| **L1 absolute/relative imports** | `--` breaks both | `~` breaks only for overridden file's siblings (needs `__path__`) | `~` breaks for ejected cluster | `~` (copy resolves, links may not) | `++` finder fixes it transparently |
| **L2 fresh-install bootstrap** | `--` new failure surface | `++` canonical always present | `++` default ejects nothing | `--` must populate store first | `++` canonical always present |
| **L3 `shell_safety` gate** | `--` loses its home | `++` stays in-package | `++` stays unless ejected | `--` loses its home | `++` stays in-package |
| **L4 Windows CRLF hash** | `--` every user, every update | `~` only sparse overrides | `~` only ejected set | `--` + symlink woes | `~` only sparse overrides |
| **Implementation cost** | `~` medium (per 27g.2) | `~` medium (loader overlay + scoped hash) | `~` medium (eject cmd + **shadow mechanism**) | `--` high | `--` high + risky |

**Reading the matrix:** B0's `--` cells are exactly L1–L4 — all artifacts of
*moving everything eagerly*. **A** and **B** turn most of those to `++` by keeping
the canonical copy in-package, at the price of slightly weaker discoverability
(easily patched with a helper command). **C** and **D** each have a single
brilliant column and a wall of `--` — classic "powerful but wrong tool."

---

## 4. Recommendation Lean (feeds synthesis; the ADR decides)

> **Lean: adopt a hybrid of B (opt-in eject) layered over A (in-package
> canonical + sparse overlay), and *retain 27g.2's BASE/NEW/CUR hash logic but
> scope it to the overlaid/ejected set* rather than the whole builtin tree.
> Reject C (YAGNI) and D (blast radius) for v1.**

Justification, point by point:

1. **Keep the canonical copy in the wheel.** This is the keystone. It deletes
   L2 (bootstrap can't fail — there's always a working copy), keeps L3 intact
   (the `shell_safety` gate never leaves `_load_builtin_plugins`), and shrinks
   L4 to the sparse set the user actually touched. Grounded in §12.1 rule #4 —
   the always-present in-package copy *is* the graceful-failure fallback the
   baseline throws away.
2. **Make externalization opt-in & per-plugin (B).** Most users never edit a
   plugin; they should pay zero cost and carry zero risk. Eject is a
   `custom_command` (§3.4/§4.2) — plugin-first, no core CLI edits.
3. **Materialize sparsely (A).** Eject the *plugin*; within it, only the files
   the user edits diverge — the rest can still resolve from the wheel. Smallest
   possible divergence = smallest possible blast radius (§12.3 *flat/simple*).
4. **Reuse, don't replace, 27g.2.** The 3-way hash + `.new` sidecar is still the
   right *update* engine — we just run it over the **overlaid/ejected slice**
   instead of all ~30 plugins. 27g.2's work is preserved and *focused*, not
   wasted.
5. **Reject D.** It's the only option that fully solves L1, but it does so by
   putting a custom finder on `sys.meta_path` alongside pytest/coverage/
   pydantic-ai — a direct violation of §12.1 rule #4 and §12.3. The L1 win is
   not worth a finder that can break *every* import.
6. **Reject C.** Multi-version CAS solves a problem the epic doesn't have, and
   its symlink/hardlink materialization lands squarely on the Windows platform
   `27g.2` §6.2 already flagged as the danger zone. Textbook YAGNI.

### 4.1 The one new thing the hybrid forces us to confront

The hybrid's eject step needs an **ejected/overlaid copy to actually win over
the builtin**. The live loader does **not** do this for builtin-vs-project: on a
name clash *both* register their callbacks and the loader only `logger.warning`s
(verified in `_load_project_plugins`; documented in `27g.1` §1.6). So the
recommendation has a hard dependency: **build a real precedence/shadow mechanism
that lets a user-owned copy suppress the builtin**, rather than running both.
This is the same gap `27g.1` flagged (its checklist item #6, deterministic
precedence) — the alternatives analysis confirms it's not optional for any
copy/overlay-then-shadow strategy.

---

## 5. Findings for the Synthesis Bead (puppy-27g.4 / ADR puppy-1ng)

- **The baseline's worst risks are self-inflicted by *eagerness*, not by
  externalization per se.** L1 (imports), L2 (bootstrap), L3 (`shell_safety`
  gate), L4 (CRLF) all stem from *moving everything up front and deleting the
  canonical copy*. Any strategy that **keeps the in-package copy** neutralizes
  L2/L3/L4 for free and shrinks L1.
- **At least three viable alternatives exist** (overlay A, eject B,
  import-hook D) plus one anti-pattern (CAS C). A and B dominate the matrix; D
  is brilliant-but-dangerous; C is over-engineered.
- **Recommended lean: hybrid B-over-A**, retaining 27g.2's hash logic scoped to
  the sparse overlaid set. This is *additive* to 27g.2, not a rejection of it.
- **Hard dependency surfaced:** a real **shadow/precedence mechanism** is
  required (the loader currently runs builtin + project together). This is the
  same gap as `27g.1` checklist #6 — now confirmed load-bearing for the
  alternatives too.
- **Discoverability is the hybrid's only real weakness**, and it's cheap to fix
  with a `/plugins list-ejectable` + `/plugins show <name>` helper (a
  `custom_command`, §4.2).
- **The ADR (`puppy-1ng`) must decide between:** (i) the baseline's eager
  hash-copy, (ii) this lazy hybrid, weighing discoverability-on-disk (baseline
  wins) against bootstrap/import/gate safety (hybrid wins).

### Proposed follow-up beads (do NOT implement in this epic)

- *Shadow/precedence mechanism:* make a user/ejected copy suppress the
  same-named builtin (today both load) — prerequisite for B and for `27g.1` #6.
- *`/plugins eject <name>` + `/plugins list-ejectable`:* `custom_command`
  plugin that copies one plugin (or cluster) on demand and records its hash
  baseline.
- *Sparse overlay resolver:* loader change to resolve overridden files
  user-first / wheel-fallback, with per-plugin `__path__` handling so an
  overridden file's sibling absolute imports still resolve.
- *Scoped hash-sync:* run 27g.2's `plan_update`/`apply_update` over the
  overlaid/ejected slice only, fast-pathed by `package_version` (27g.2 §4.3).
- *Dependency-cluster detection:* refuse to eject a plugin that absolute-imports
  a non-ejected sibling without offering to eject the whole cluster
  (`27g.1` §2.3/§3.7).

---

*Spike complete. No code changed. All claims grounded in the `code-puppy-agent`
skill (see §0) and verified directly against `code_puppy/plugins/__init__.py`,
plus the sibling deliverables `docs/PLUGIN_EXTERNALIZATION_INVENTORY.md`
(`27g.1`) and `docs/PLUGIN_HASH_AWARE_UPDATES.md` (`27g.2`).*
