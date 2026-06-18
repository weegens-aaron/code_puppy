# Spike puppy-27g.4 — Externalization Synthesis, Recommendation & Migration Plan

> **DISCOVERY synthesis spike — research only.** No implementation here. This is
> the **JOIN bead** for epic `puppy-27g`: it consumes the three sibling spikes,
> reconciles them, picks **one** recommended approach with explicit
> justification, lays out a migration/rollout plan (fresh install + upgrade),
> states the user-override preservation guarantee precisely, and enumerates the
> implementation epics/beads to file next (titles + 1-line scope, **not** built).
>
> **Parent epic:** `puppy-27g` — Externalize Builtin Plugins with Hash-Aware Updates.
> **Inputs consumed (JOIN):**
> - `puppy-27g.1` — `docs/PLUGIN_EXTERNALIZATION_INVENTORY.md` (loader/precedence ground truth + risks)
> - `puppy-27g.2` — `docs/PLUGIN_HASH_AWARE_UPDATES.md` (BASE/NEW/CUR hash-aware update algorithm)
> - `puppy-27g.3` — `docs/PLUGIN_EXTERNALIZATION_ALTERNATIVES.md` (alternatives eval + recommendation lean)
> **Feeds:** ADR `puppy-1ng` (the decider — this doc consolidates and recommends; the ADR ratifies).

---

## 0. Architecture Grounding (REQUIRED FIRST STEP)

Per the project's hard acceptance contract, the **first step was to activate the
`code-puppy-agent` architecture skill**. Each synthesis decision below is tied
back to the specific `SKILL.md` section that constrains it, and the one
load-bearing precedence claim was re-verified directly against the live loader
(`code_puppy/plugins/__init__.py:307-311` — a project-vs-builtin name clash only
`logger.warning`s; **both copies load**, there is no skip/shadow). A synthesis
that ignored the skill would just be averaging three opinions; grounding it
makes the recommendation *fit the real architecture*.

| # | Synthesis decision (this doc) | Grounded in `code-puppy-agent` SKILL.md | Why it constrains the synthesis |
|---|-------------------------------|------------------------------------------|---------------------------------|
| 1 | Externalization surface = the **user-tier** dir `~/.code_puppy/plugins/`; keep the canonical copy in the wheel's **builtin** tier | §4.1 *Plugin discovery (three tiers)* + §10 *Configuration System* | The recommendation moves *editable* files to a real, documented user dir while preserving the read-only builtin tier as the always-present fallback. |
| 2 | Recommended approach is **opt-in eject over in-package canonical** (not eager total copy) | §12.1 rule #4 *fail gracefully* + §12.3 *Zen* (*simple > complex, flat > nested*) | Keeping the canonical copy is the cheapest graceful-failure fallback; eager total relocation throws it away and manufactures the risks. |
| 3 | New user-facing surface (`/plugins eject`, `/plugins conflicts`, `/plugins list-ejectable`) ships as **`custom_command` plugins**, never core CLI edits | §3.4 *Plugin tools* + §4.2 `custom_command` hook + §12.1 rule #1 *plugins over core* | All migration UX is plugin-first; `command_line/` stays untouched. |
| 4 | Sync runs from the **`startup`** hook, before `_load_builtin_plugins()` imports anything | §4.2 hook table (`startup` = app boot) + §13 file map (loader = `plugins/__init__.py`) | The hash-sync must land files before the idempotent one-shot load (27g.1 §3.9), so the seam is a startup-phase callback. |
| 5 | Conflicts surface via **`emit_warning`** (message bus), never `print` | §11.1 *Message bus* (renders in TUI **and** non-TTY/CI) | The migration must behave on CI/containers where updates run headless. |
| 6 | The eject/overlay step requires a **net-new shadow/precedence mechanism** | §4.1 (*project shadows user on collision*) — contradicted by the live loader for builtin-vs-project | The skill promises shadowing the code does not deliver for builtin clashes; the migration must build it. |
| 7 | Reuse 27g.2's BASE/NEW/CUR engine, **scoped to the ejected/overlaid slice** | §13 (loader location) + sibling `27g.2` | We narrow the hash engine's blast radius rather than discarding the design. |
| 8 | Builtin internal-import normalization + user-tier parent-package fix are prerequisites | §4.1 (*all three tiers "same pattern"*) — contradicted by the implementation (27g.1 §3.1) | The skill's "identical tiers" promise is the gap; honouring it is migration step zero. |

> **The single most important grounding consequence:** the skill describes all
> three tiers as "the same pattern — drop a `register_callbacks.py`," but the
> code does **not** treat them the same (builtin imports as a real package; the
> user tier builds no parent package; only the project tier has a synthetic
> namespace). *That asymmetry is the root externalization blocker* — and every
> recommendation below is shaped to close it rather than route around it.

---

## 1. What the Three Spikes Established (consolidated)

| Spike | Question it answered | Load-bearing finding the synthesis inherits |
|-------|----------------------|---------------------------------------------|
| **27g.1** (inventory/risks) | *Can today's loader host externalized builtins?* | **No, not as written.** Three tiers load *differently* (builtin = real package; user = no parent package; project = synthetic namespace). Builtins split ~50/50 absolute vs relative imports and several import each other. `shell_safety` carries a core-side conditional-load gate. No version guard exists. |
| **27g.2** (hash algorithm) | *How do we update on-disk plugin files without clobbering edits?* | A **BASE/NEW/CUR three-way model** with a 12-row decision table, two manifests (shipped read-only / installed read-write), non-blocking `.new` sidecar conflict UX, baseline-always-advances-to-NEW, and a mandatory **newline-normalize-before-hash** to survive Windows CRLF. |
| **27g.3** (alternatives) | *Is eager total copy even the right strategy?* | **No.** The baseline's worst risks (L1 imports, L2 bootstrap, L3 gate, L4 CRLF) are *self-inflicted by eagerly relocating everything and deleting the canonical copy.* Keeping the in-package copy neutralizes L2/L3/L4 for free and shrinks L1. Lean: **hybrid eject-over-overlay**, reusing 27g.2 scoped to the touched set. |

### 1.1 The five liabilities (the shared vocabulary)

From 27g.1, distilled by 27g.3 — every approach is scored on how many it neutralizes:

- **L1 — Imports break.** Absolute `code_puppy.plugins.<name>.*` imports die when the canonical copy is gone; relative imports die because the user tier builds no parent package.
- **L2 — Bootstrap is a new failure surface.** A clean machine has core functionality *missing* until a copy succeeds (disk-full, read-only `$HOME`, CI/containers, partial copy).
- **L3 — `shell_safety` gate homelessness.** Its conditional-load logic lives only in `_load_builtin_plugins`; the user tier has no equivalent.
- **L4 — Windows CRLF hash instability.** Raw-byte hashing falsely flags every file as user-modified under `core.autocrlf=true`.
- **L5 — Cross-plugin dependency clusters.** Some builtins absolute-import siblings; they must move all-or-nothing.

---

## 2. The Decision

> ### Recommended approach: **Lazy hybrid — opt-in per-plugin *eject* layered over an in-package-canonical + sparse user overlay, reusing 27g.2's BASE/NEW/CUR hash engine scoped to the ejected/overlaid slice.** Reject eager total hash-copy (B0), reject CAS (C), reject the `sys.meta_path` import-hook (D).

This is the lean from 27g.3, now ratified as the synthesis recommendation and made concrete for the ADR.

### 2.1 Explicit justification (why this and not the baseline)

1. **Keep the canonical copy in the wheel — the keystone.** This single choice deletes **L2** (bootstrap can't fail; there's always a working copy), keeps **L3** intact (the `shell_safety` gate never leaves `_load_builtin_plugins`), and shrinks **L4** to only the sparse files a user actually edits. Grounded in §12.1 rule #4: the always-present in-package copy *is* the graceful-failure fallback the baseline throws away.
2. **Make externalization opt-in & per-plugin.** ~95% of users never edit a plugin; they should pay zero cost and carry zero risk. Default install ejects nothing → **L2 is a non-event** and **L4 touches nothing** on a normal machine.
3. **Materialize sparsely.** Eject the *plugin*; within it, only files the user edits diverge — the rest still resolve from the wheel. Smallest divergence = smallest blast radius (§12.3 *flat/simple*).
4. **Reuse, don't replace, 27g.2.** The 3-way hash + `.new` sidecar is still the correct *update engine*; we run it over the **ejected/overlaid slice** instead of all ~30 plugins. 27g.2's design is preserved and *focused*, not discarded.
5. **Reject D (import-hook).** It's the only option that fully solves L1, but it does so by putting a custom finder on `sys.meta_path` alongside pytest/coverage/pydantic-ai — a direct violation of §12.1 rule #4 and §12.3. The L1 win is not worth a finder that can break *every* import.
6. **Reject C (CAS).** Multi-version content-addressed storage solves a problem the epic doesn't have (version retention/rollback) and its symlink/hardlink materialization lands on exactly the Windows platform 27g.2 §6.2 flagged as the danger zone. Textbook YAGNI.
7. **Reject B0 (eager total copy) as the *strategy*** — while keeping its *algorithm*. B0 is an excellent update mechanism wearing the wrong deployment model: it eagerly relocates everything, which is precisely what activates L1–L4. We keep the engine, drop the eagerness.

### 2.2 The decision in one matrix row

| | Override safety | Bootstrap (L2) | `shell_safety` (L3) | Windows CRLF (L4) | Imports (L1) | Discoverability | Verdict |
|---|---|---|---|---|---|---|---|
| **Recommended (eject-over-overlay)** | `++` (ejected file is the user's) | `++` canonical always present | `++` stays in-package | `~` only ejected set hashed | `~` per-ejected-cluster (needs `__path__`) | `~` (needs `list-ejectable`/`show` helper) | **CHOSEN** |
| B0 eager hash-copy | `++` | `--` | `--` | `--` every user every update | `--` breaks all | `++` all on disk | rejected as strategy |

Discoverability is the recommendation's only `~`, and it is **cheap to patch** with a `/plugins list-ejectable` + `/plugins show <name>` helper command (a `custom_command`, §4.2). Every other column is a clear win over the baseline.

### 2.3 The one hard dependency the decision forces

The eject step needs an **ejected copy to actually win over the builtin**. The live loader does **not** do this: on a builtin-vs-project name clash *both* register their callbacks and the loader only `logger.warning`s (verified `plugins/__init__.py:307-311`; documented 27g.1 §1.6). So the recommendation has a **net-new prerequisite: a real shadow/precedence mechanism that lets a user-owned copy suppress the builtin.** This is the same gap as 27g.1 checklist #6 and 27g.3 §4.1 — now confirmed load-bearing from all three angles, so it is **not optional**.

---

## 3. User-Override Preservation Guarantee (stated precisely)

> **Guarantee.** Once a user materializes (ejects/overrides) a plugin file,
> code_puppy will **never overwrite or delete that file's content as part of any
> automatic update**. Upstream changes to a file the user has modified are
> delivered *beside* it as `<file>.new` (never *over* it), and the user is
> notified once via the message bus. Files the user creates that code_puppy
> never shipped are *never* touched.

Mechanically, this is exactly 27g.2's invariants, scoped to the overlaid set:

- **A file is "user-modified" iff `CUR != BASE`** — where BASE is the hash code_puppy last *wrote* for that path (from the installed manifest), not the current disk bytes. We compare against what *we* last wrote, so we can distinguish user edits from our own (27g.2 §2).
- **Rows 3, 10, 12 of the decision table are the preservation rows** (27g.2 §3): user-modified + upstream-unchanged → keep; upstream-deleted + user-modified → keep as orphan; untracked (never shipped) → never touch.
- **Conflicts (rows 5 & 8) are non-destructive:** the user's bytes are left untouched and the upstream version is written to `<file>.new` (27g.2 §5.2).
- **Baseline always advances to NEW** even for preserved/conflicted files, so the user is never silently re-clobbered and never re-prompted for the same shipped version (27g.2 §3.2).
- **Scope difference from B0:** because only ejected/overlaid files are managed, the guarantee's surface area is the handful of files the user *chose* to own — not all ~30 plugins. The non-ejected canonical copies are updated freely by `pip` (no hashing, no risk) because the user never edited them.

This guarantee is *stronger and cheaper* than the baseline's: the baseline must hash-protect every file on every startup; the hybrid only protects what the user actually touched, and protects the rest by simply not moving it.

---

## 4. Migration / Rollout Plan

The migration is **strictly additive and reversible at every step** — nothing is removed from the wheel, so any step can be paused or rolled back by reverting the corresponding plugin/loader change. Builtins keep working unmodified until the very last (optional) phase.

### 4.0 Sequencing principle

Land *enabling infrastructure* first (loader parity, import normalization, shadow mechanism, scoped sync), then *opt-in surface* (eject command), then — only if desired — flip individual plugins. Self-contained leaf plugins (no cross-plugin imports) go first; dependency clusters (L5) go all-or-nothing or never.

### 4.1 Phase 0 — Loader parity & import normalization (no behavior change)

- Extract the project tier's `_ensure_plugin_package`/`_ensure_project_ns` into a **shared namespace-package helper**, and adopt it in the user tier so multi-file user plugins (and future overlays) get a real parent package — closing the §4.1 "tiers are identical" gap (27g.1 §4 item 1–2).
- Normalize builtin internal imports onto **one relocatable convention** (relative imports) and document it in `CONTRIBUTING`/SKILL (27g.1 §4 item 1, addresses L1).
- **Outcome:** the user tier can now host multi-file plugins; nothing externalizes yet. Fully shippable on its own.

### 4.2 Phase 1 — Build the shadow/precedence mechanism (the hard dependency, §2.3)

- Make a user/ejected copy **suppress** the same-named builtin instead of running both (today both fire — `plugins/__init__.py:307-311`). Deterministic, tested precedence; replaces the current warn-only behavior (27g.1 §3.6/#6).
- **Outcome:** "I own this plugin → mine wins" becomes true. Still nothing externalized.

### 4.3 Phase 2 — Scoped hash-sync engine (27g.2, narrowed)

- Implement `plan_update`/`apply_update`, the build-time **shipped-manifest** generator, and installed-manifest read/write — but run it over the **ejected/overlaid slice only**, fast-pathed by the `package_version` short-circuit (27g.2 §4.3).
- Wire it into the **`startup`** hook, *before* the idempotent load (27g.1 §3.9, §0 row 4); conflicts via `emit_warning` (§0 row 5).
- **Mandatory:** newline-normalize text files before hashing (27g.2 §6.2) or every Windows user hits spurious conflicts (L4).
- **Outcome:** the update engine exists and is safe, but only acts on what's been ejected (nothing, by default).

### 4.4 Phase 3 — Opt-in eject surface (the user-facing feature)

- Ship `/plugins eject <name>`, `/plugins list-ejectable`, `/plugins show <name>`, and `/plugins conflicts` as **`custom_command` plugins** (§3.4/§4.2). Eject copies one plugin (or whole dependency cluster — L5) to the user dir and records its hash baseline.
- Refuse to eject a plugin that absolute-imports a non-ejected sibling without offering to eject the whole cluster (27g.1 §2.3/§3.7; 27g.3 follow-ups).
- **Outcome:** users can opt in per-plugin. This is the feature epic 27g actually promises.

### 4.5 First-run bootstrap path (fresh install)

A clean machine has an **empty or absent** `~/.code_puppy/plugins/` and **no installed manifest**.

- **Default fresh install: do nothing.** No bootstrap copy happens because nothing is ejected — all builtins run from the wheel exactly as today. **L2 is structurally impossible** (there is no bootstrap step that can fail). This is the headline advantage over B0, which *required* a populate-or-degrade step on every clean machine.
- **First eject on a fresh machine** triggers a *scoped* bootstrap for that one plugin: with no BASE, treat each shipped file as an **adopt** candidate (27g.2 §6.1) — write if absent, adopt silently if `CUR == NEW`, conflict-sidecar if `CUR != NEW` — then write the installed manifest for that slice. Fails gracefully (read-only `$HOME` → emit a warning, leave the builtin running from the wheel).

### 4.6 Upgrade path (existing installs)

- **Non-ejected plugins:** `pip install -U code-puppy` replaces the wheel; the canonical copies update for free. **No hashing, no manifest, no risk** — these were never the user's.
- **Ejected plugins:** on the next `startup`, the scoped sync runs 27g.2's 3-way over just the ejected slice (fast-pathed by `package_version`). Clean upstream changes land in place (row 2); user-modified files are preserved (row 3); true conflicts get a `.new` sidecar + one aggregated warning (rows 5/8). Baseline advances to NEW (§3.2).
- **Users who never ejected anything** (the majority) experience a *normal pip upgrade* — the externalization machinery is inert for them. This is the migration's biggest safety property: **the blast radius of the entire feature is bounded to users who opted in.**

### 4.7 Rollback

- Any phase reverts cleanly because the wheel always retains canonical copies. Worst case, disabling the eject command + scoped sync returns the product to today's behavior with zero data loss (ejected files remain on disk as the user's own; the loader's shadow mechanism simply has nothing to shadow).

---

## 5. Proposed Implementation Epics / Beads (titles + 1-line scope — NOT implemented)

> This is a DISCOVERY synthesis spike. The following are *proposed* for the ADR
> (`puppy-1ng`) to ratify and Bead Master to file — **none are created here.**

### Epic E1 — Loader parity & import normalization (Phase 0)

- **E1.1 Shared namespace-package helper** — extract `_ensure_plugin_package`/`_ensure_project_ns` into one helper; adopt in the user tier so it builds a real parent package (closes L1 for relative imports; honours §4.1 "tiers identical").
- **E1.2 Builtin import-style normalization** — convert builtin internal imports to one relocatable (relative) convention; document in `CONTRIBUTING`/SKILL.
- **E1.3 Conditional-load mechanism** — replace the `shell_safety` `if plugin_name == ...` special-case with a declarative manifest "load predicate"/`should_load` hook (de-homes L3).

### Epic E2 — Deterministic precedence / shadow mechanism (Phase 1, the hard dependency)

- **E2.1 User/ejected copy suppresses same-named builtin** — replace warn-only (`plugins/__init__.py:307-311`) with deterministic, tested precedence so an owned copy wins instead of both loading (27g.1 #6; prerequisite for eject).
- **E2.2 Tier-collision policy + tests** — define + test precedence when a builtin and a real user plugin share a name in the same dir (27g.1 §3.6).

### Epic E3 — Scoped hash-aware sync engine (Phase 2, from 27g.2)

- **E3.1 `plugin_sync` module** — `plan_update`/`apply_update` + installed-manifest read/write, scoped to the overlaid/ejected set.
- **E3.2 Build-time shipped-manifest generator** — emit `_shipped_manifest.json` with **newline-normalized** sha256 at packaging time (mandatory L4 mitigation).
- **E3.3 Wire sync into `startup`** — run once per launch before the idempotent load; `package_version` fast-path; conflict warnings via the message bus.

### Epic E4 — Opt-in eject surface (Phase 3, the feature)

- **E4.1 `/plugins eject <name>`** — `custom_command` that copies one plugin (or cluster) on demand and records its hash baseline.
- **E4.2 `/plugins list-ejectable` + `/plugins show <name>`** — discoverability helpers (patches the recommendation's only weak column).
- **E4.3 `/plugins conflicts` reviewer** — review / accept-upstream / keep-mine / open-diff for `.new` sidecars (27g.2 §9 #3).
- **E4.4 Dependency-cluster detection on eject** — refuse to eject a plugin that absolute-imports a non-ejected sibling without offering to eject the whole cluster (L5).

### Optional / deferred (explicitly YAGNI for v1)

- **OPT.1 diff3 auto-merge layer** — clean 3-way auto-merge atop the sidecar (27g.2 §5.3).
- **OPT.2 rename-hint support** — manifest `renames` to migrate user edits across renamed paths (27g.2 §7).

### Dependency order (for the ADR / Bead Master)

```
E1  ──►  E2  ──►  E3  ──►  E4   (then OPT.* if ever)
(parity)  (shadow) (sync)  (feature)
```

E1 and E2 are prerequisites with **no user-visible behavior change** and can ship independently — derisking the whole epic before any plugin externalizes.

---

## 6. Findings Summary (for ADR `puppy-1ng`)

- **Recommendation:** adopt the **lazy hybrid — opt-in eject over in-package-canonical + sparse overlay**, reusing 27g.2's BASE/NEW/CUR engine scoped to the ejected slice. Reject eager total hash-copy (B0) *as a strategy* while keeping its *algorithm*; reject CAS (C, YAGNI) and the import-hook (D, blast radius).
- **Why:** the baseline's worst risks (L1 imports, L2 bootstrap, L3 gate, L4 CRLF) are self-inflicted by *eager total relocation*. Keeping the canonical copy in the wheel neutralizes L2/L3/L4 for free, shrinks L1, and bounds the entire feature's blast radius to users who opt in.
- **Override guarantee:** once ejected, a file's content is never overwritten/deleted by an update; upstream lands as `<file>.new`; untracked user files are never touched (27g.2 invariants, scoped).
- **Migration:** four additive, reversible phases — loader parity (E1) → shadow mechanism (E2) → scoped sync (E3) → eject surface (E4). Fresh install does nothing by default (L2 structurally impossible); upgrades only hash the ejected slice; non-adopters get a normal `pip` upgrade.
- **The one non-optional new build:** a deterministic **shadow/precedence mechanism** (today builtin + project both load on clash — verified `plugins/__init__.py:307-311`). Confirmed load-bearing by all three spikes.
- **Mandatory implementation detail:** newline-normalize text files before hashing, or every Windows user hits spurious conflicts on every update (L4).
- **The ADR's remaining trade-off to ratify:** discoverability-on-disk (baseline wins) vs bootstrap/import/gate safety + bounded blast radius (hybrid wins). The synthesis judges the latter decisively more important, with discoverability cheaply restored by `/plugins list-ejectable` + `/plugins show`.

---

*Spike complete. No code changed. All claims grounded in the `code-puppy-agent`
skill (see §0) and verified against `code_puppy/plugins/__init__.py` plus the
three sibling deliverables `docs/PLUGIN_EXTERNALIZATION_INVENTORY.md` (27g.1),
`docs/PLUGIN_HASH_AWARE_UPDATES.md` (27g.2), and
`docs/PLUGIN_EXTERNALIZATION_ALTERNATIVES.md` (27g.3).*
