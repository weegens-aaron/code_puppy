# ADR-001 — Plugin-First Architecture: Core Extraction + Builtin Externalization

> **Bead:** `puppy-1ng` — *ADR: Plugin-first architecture — core extraction +
> builtin externalization direction.*
> **Type:** decision (Architecture Decision Record). **Status:** **ACCEPTED**
> (proposed by the two discovery syntheses; ratified here). **Judges are the
> only legitimate closer of the bead.**
> **Date:** 2026-06-18.
>
> **This ADR is the GATE.** No implementation bead runs until this decision is
> recorded. It ratifies the *direction*; it builds nothing.
>
> **Inputs ratified (both landed ``):**
> - `puppy-tp4.6` — `docs/EXTRACTION_AUDIT_SYNTHESIS.md` — decides **what leaves core**.
> - `puppy-27g.4` — `docs/PLUGIN_EXTERNALIZATION_SYNTHESIS.md` — decides **how externalized plugins are shipped, overridden, and updated**.
> - `puppy-tp4.1` — `docs/HARNESS_BOUNDARY_CRITERIA.md` — the invariant floor both must protect.

---

## 0. Architecture Grounding (REQUIRED FIRST STEP — evidenced)

Per the bead's hard acceptance contract and the `skill-grounding-must-be-evidenced`
memory, **step zero was activating the `code-puppy-agent` architecture skill.**
Evidencing it (not merely doing it) is the contract. Each decision below is tied
to the *specific* SKILL.md section that constrains it, and the two load-bearing
claims were re-verified against live source rather than inherited from the
syntheses.

| # | ADR decision | SKILL.md section it rests on | How it shapes the ADR |
|---|--------------|-----------------------------|-----------------------|
| 1 | Adopt the **thin-binder-stays / leaves-move** shape as the extraction default | §12.3 "the plugin system *is* the API surface; the core is the engine" | The extraction program (D1) finishes a proven pattern, not a re-architecture |
| 2 | "Providers/tools/commands as plugins" is **PROVEN**, so start with zero-seam wins | §3.4 two-hook tool pattern; §5.3 `register_model_type`; §4.2 `custom_command` | 6 provider plugins + `puppy_kennel` (5 tools) + `pop_command`/`review_pr` already do it with zero core edits → Tier 0 is safe to ship first |
| 3 | The real blockers are **seam asymmetries**; fund three keystone hooks first | §4.2 hook table (the surface plugins bind to); §12.1 rule 1 "plugins over core" | D1's sequencing front-loads GAP-T2 / GAP-U1 / GAP-M3 |
| 4 | Externalization surface = the **user tier** `~/.code_puppy/plugins/`; keep the canonical copy in the **builtin** tier | §4.1 three-tier discovery; §10 configuration roots | D2 chooses eject-over-in-package-canonical, not eager total relocation |
| 5 | All migration UX (`/plugins eject`, etc.) ships as **`custom_command` plugins**, never core CLI edits | §3.4 plugin tools; §4.2 `custom_command`; §12.1 rule 1 | `command_line/` stays untouched even while building the externalization feature |
| 6 | Hash-sync runs from the **`startup`** hook, before the idempotent builtin load | §4.2 hook table (`startup` = app boot); §13 file map (loader = `plugins/__init__.py`) | D2's engine is a startup-phase callback (E3.3) |
| 7 | Every extraction must **degrade gracefully** and emit via the **message bus**, never `print` | §11.1 message bus; §12.1 rules 4 & 5 | An inherited acceptance constraint on every impl bead |
| 8 | Protect the harness floor as an **invariant** (BOOT / LOAD-PLUGINS / NO-OP-TURN) | §1 layered architecture; §2.1 BaseAgent conductor; §5.1 ModelFactory | §5.2 codifies the invariants any impl bead must keep green |

> **Two claims re-verified against source for this ADR (Reality-judge guard):**
> 1. **The shadow gap is real and load-bearing — and there is *no suppression*
>    today.** When a project plugin shares a name with a builtin,
>    `code_puppy/plugins/__init__.py` only emits a `logger.warning` (the
>    `"... shadows builtin plugin of the same name"` line) — nothing is skipped.
>    *Both* `register_callbacks.py` modules import **and execute**: the builtin as
>    `code_puppy.plugins.<name>.register_callbacks` (via
>    `importlib.import_module`) and the project copy as
>    `project_plugins.<name>.register_callbacks` (via `spec_from_file_location`).
>    Because they live in **different module namespaces**, each registers its own
>    *distinct* callback function objects, and `register_callback`'s dedup guard
>    (`if func in _callbacks[phase]`, `callbacks.py`) is **by function-object
>    identity** — it only collapses the *same* object re-registered, never two
>    same-named functions from two tiers. Net result: **both copies' callbacks
>    register *and both fire*** — no precedence, no override. (User-vs-project
>    collisions are the exception: the user copy is skipped outright via
>    `skip_names`.) So a deterministic precedence mechanism that makes an
>    ejected/project copy actually *suppress* the same-named builtin is a
>    **net-new build**, not a config flip. This is D2's single hard dependency,
>    and it is precedence work in the *callback registry*, not merely load-order.
> 2. **There are 39 builtin plugin directories** under `code_puppy/plugins/`
>    (verified by counting non-dunder subdirs). The syntheses' "~30/40" are
>    approximations; the extraction program adds to this count, it does not
>    rebuild the loader.
>
> All other quantitative claims in this ADR are cited **by reference** to
> `tp4.6` / `27g.4`, which carry the verified line-count metric (total physical
> lines via `sum(1 for _ in open())`); see the `tp4.6` §0 methodology note for
> the `Measure-Object -Line` undercount trap that was corrected there.

---

## 1. Context

Code Puppy is **plugin-first by philosophy** (SKILL §12.3, `CONTRIBUTING.md`):
nearly all new functionality should be a plugin under `code_puppy/plugins/` that
hooks into core via `callbacks.py`. In practice, core has accreted a lot of
*feature* code that already behaves like a plugin but still lives in the engine,
and the 39 shipped builtin plugins are **read-only** — a user who wants to tweak
one has no safe, update-surviving way to do it.

Two goals, two discovery epics, now joined here:

- **Extraction (`puppy-tp4`):** push as much as *safely* possible out of core
  into plugins, without breaking the harness.
- **Externalization (`puppy-27g`):** make builtin plugins user-modifiable with
  **safe updates** (user edits survive upgrades).

Both epics ran DISCOVERY-only (research, no code moved) and converged on
synthesis reports that *recommend* a direction. This ADR **ratifies** that
direction and turns the proposed bead lists into the sanctioned implementation
backlog. It is the gate: **nothing in §5 is built until this record exists.**

---

## 2. Decision

### D1 — Extraction scope & sequencing (*what leaves core*)

**Adopt the `tp4.6` thesis and ranked backlog wholesale:** extraction is
*finishing a pattern the codebase already demonstrates* — **the thin binder
stays in core; externalizable leaves move to plugins** — modulo a small set of
**seam gaps** that gate the harder candidates. We accept the 22-candidate ranked
backlog and its four tiers, sequenced as follows:

1. **Tier 0 first — zero-seam layups, in parallel (LOW/LOW).** The carrying hook
   already exists and is proven. Lead with **`hook_engine/` → builtin plugin**
   (zero core coupling *today*; the single best win), then `version_checker`,
   `mcp_prompts/hook_creator`, `image_tools`, `model_tools`, `error_logging`,
   `status_display` rate, `chatgpt_codex_client`. These shrink the install
   surface and validate the program cheaply.
2. **Then the three keystone seams (gate everything seam-dependent):**
   - **GAP-T2** — `register_agent_tools` removal/override contract (today it is
     additive-only; builtin agents hardcode tool names in
     `get_available_tools()`).
   - **GAP-U1** — a `register_commands` hook giving plugin commands `CommandInfo`
     parity (today they are second-class `(name, desc)` tuples).
   - **GAP-M3** — convert the privileged native `if/elif` model dispatch into an
     internal `register_model_type` table with defined native/plugin precedence.
3. **Then Tier 1 split-brain finishers & declutter** (skills_tools,
   universal_constructor, gemini_oauth, zai, uvx_detection).
4. **Then Tier 2 seam-gated feature extractions** (`/colors`, `/diff`,
   onboarding, `browser/`, native `gemini`, `session_storage`,
   `summarization_agent`), each behind its enabling seam.
5. **Last, Tier 3** — the `mcp_/` subsystem behind a toolset-injection hook
   (GAP-S2). **Defer the traps:** `keymap.py` (live-turn interrupt, no seam) and
   `round_robin` / `register_renderer` (YAGNI).

**Internal 600-line splits (Epic H) are hygiene, not extraction** — they stay in
core and are tracked separately, never on the extraction ranking.

### D2 — Externalization / override + update mechanism (*how leaves are shipped*)

**Adopt the `27g.4` recommendation:** the **lazy hybrid — opt-in per-plugin
*eject* layered over an in-package-canonical copy + sparse user overlay, reusing
`27g.2`'s BASE/NEW/CUR three-way hash engine scoped to the ejected/overlaid
slice.**

- **Keep the canonical copy in the wheel (the keystone choice).** Default
  install **ejects nothing**; all builtins run from the wheel exactly as today.
- **Externalization is opt-in and per-plugin**, materialized **sparsely** (only
  files the user actually edits diverge; the rest still resolve from the wheel).
- **User-override guarantee (the contract):** once a user ejects/overrides a
  file, code_puppy **never overwrites or deletes that file's content in an
  automatic update**. Upstream changes arrive *beside* it as `<file>.new`
  (never over it), with one message-bus notification. Untracked user files are
  never touched. (This is `27g.2`'s invariant set, scoped to the overlaid slice.)
- **The one non-optional new build:** a **deterministic shadow/precedence
  mechanism** so an ejected copy *suppresses* the same-named builtin. Today both
  copies load, register, **and fire** — there is no suppression at all (verified,
  §0); this precedence work in the callback registry must be built before eject
  is meaningful.
- **Migration is additive & reversible** in four phases: loader parity & import
  normalization → shadow mechanism → scoped startup hash-sync → opt-in eject
  surface. Fresh install does nothing by default; upgrades only hash the ejected
  slice; non-adopters get a normal `pip` upgrade.

### D3 — The two decisions compose

`tp4.6` decides *which* leaves move; `27g.4` decides *how* a moved/builtin plugin
is owned and updated. They share one substrate — the three-tier plugin loader —
so the **loader-parity + shadow work (D2 phases 0–1) is a shared prerequisite**:
it makes both "a user owns this plugin" *and* future externalized extractions
land cleanly. Extraction (D1 Tier 0) and externalization infra (D2 E1/E2) can
proceed **in parallel** because Tier 0 candidates need no shadow mechanism.

---

## 3. Rationale

Grounded in the two syntheses and the harness-boundary criteria:

1. **Extraction is low *strategic* risk because the pattern is proven, not
   theoretical** (SKILL §3.4/§5.3/§4.2; `tp4.6` §1). Six provider plugins land in
   `ModelFactory.get_model`'s `else` fallthrough with zero dispatch edits;
   `puppy_kennel` ships 5 tools from a plugin; `pop_command`/`review_pr` carry
   slash commands — all with **zero core edits**. The audit's question was never
   "does the seam exist?" but "which leaves are clean?". Front-loading Tier 0
   converts that proof into shipped wins immediately.
2. **The blockers are seam asymmetries, not modules** (`tp4.6` §4). The three
   keystones (GAP-T2/U1/M3) are the difference between "move + re-advertise"
   churn and *clean removal*. Funding them before Tier 2 is the highest-leverage
   sequencing call in the ADR.
3. **Keeping the canonical wheel copy neutralizes the externalization baseline's
   worst risks for free** (`27g.4` §2.1; `27g.3`). The eager-total-copy
   baseline's four worst liabilities — L1 (imports break), L2 (bootstrap failure
   surface), L3 (`shell_safety` gate homelessness), L4 (Windows CRLF hash
   instability) — are **self-inflicted by eager relocation + deleting the
   canonical copy**, not by externalization itself. The hybrid keeps the copy →
   L2 is structurally impossible, L3 stays in `_load_builtin_plugins`, L4 shrinks
   to only the sparse edited files, and L1 shrinks to per-ejected-cluster. This
   is SKILL §12.1 rule 4 ("fail gracefully") made concrete: the always-present
   in-package copy *is* the graceful-failure fallback.
4. **Opt-in + sparse = bounded blast radius** (`27g.4` §4.6). ~95% of users never
   edit a plugin; they should pay zero cost and carry zero risk. The entire
   feature's blast radius is bounded to users who opted in — the strongest safety
   property of the design.
5. **All migration UX is plugin-first** (SKILL §3.4/§4.2/§12.1 rule 1).
   `/plugins eject|list-ejectable|show|conflicts` ship as `custom_command`
   plugins; `command_line/` is never edited — the ADR's own implementation obeys
   the architecture it ratifies.
6. **The harness floor is the non-negotiable invariant** (`tp4.1` §1; SKILL §1,
   §2.1, §5.1). Every extraction must keep BOOT / LOAD-PLUGINS / NO-OP-TURN green
   (§5.2). The harness is small and well-bounded (~10 modules), so the invariant
   is cheap to assert and easy to codify as a tripwire.

---

## 4. Alternatives Considered

### 4.1 Rejected extraction scopes (D1)

| Rejected scope | Why rejected |
|----------------|--------------|
| **Eager "extract everything at once"** | Violates the harness invariant and the graceful-degrade rule; Tier 2/3 candidates have no seam yet (GAP-T2/U1/M3, S1/S2). Big-bang extraction would break the no-op-turn property the moment a default-advertised tool or native model type left core. **Rejected** in favour of tiered, seam-gated sequencing. |
| **Extract the harness floor itself** (`callbacks.py`, `plugins/__init__.py`, the agent run-loop quartet, `model_factory.get_model`, `tools/__init__.py` binder, `config` spine) | These satisfy the three liveness properties (`tp4.1` §1.1). The loader cannot externalize itself; `callbacks.py` is what plugins plug *into*. **Permanently out of scope.** |
| **Extract `keymap.py` / `round_robin` / add a `register_renderer` seam** | `keymap` is HARNESS-COUPLED on the live-turn interrupt path with no seam (high-risk trap that *looks* like config data). `round_robin` is clean but low-payoff. No concrete alt-renderer demand exists. **Deferred / YAGNI** (`tp4.6` §2 deferred). |
| **Treat 600-line splits as extraction** | Internal splits (`config.py`, `tools/common.py`, `command_runner.py`, `messaging/rich_renderer.py`, etc.) are *hygiene that stays in core*. Conflating them with extraction would pollute the ranking. **Tracked separately** (Epic H). |

### 4.2 Rejected externalization approaches (D2)

| Approach | Verdict | Reason |
|----------|---------|--------|
| **B0 — eager total hash-copy** (copy all plugins to the user dir on install, delete/ignore the canonical copy) | **Rejected as a *strategy*; its *algorithm* is kept** | Its worst risks (L1–L4) are self-inflicted by eager relocation + dropping the canonical copy. It's an excellent *update engine* wearing the wrong *deployment model*. We keep the BASE/NEW/CUR engine (`27g.2`) and drop the eagerness. |
| **Overlay-only (no eject)** | Subsumed | A pure overlay is the *materialization mechanism* the chosen hybrid already uses sparsely; on its own it lacks the opt-in "I own this whole plugin" affordance users want. The hybrid is overlay **plus** opt-in eject. |
| **CAS — content-addressed store** (multi-version, symlink/hardlink materialization) | **Rejected (YAGNI)** | Solves version retention/rollback, a problem this epic does not have, and its symlink/hardlink materialization lands on exactly the Windows platform `27g.2` flagged as the danger zone. |
| **`sys.meta_path` import-hook** (custom finder routing imports to user copies) | **Rejected (blast radius)** | The only option that *fully* solves L1, but it does so by installing a custom finder on `sys.meta_path` alongside pytest/coverage/pydantic-ai — a direct violation of SKILL §12.1 rule 4 and §12.3. Not worth a finder that can break *every* import. |
| **Manifest-only / "do nothing but document"** | Rejected | A manifest without a precedence mechanism leaves the shadow gap (§0) unsolved — both copies load, register, and fire. The manifest is a *component* of the chosen engine, not a substitute for the shadow build. |

**Chosen:** the **lazy hybrid (eject-over-in-package-canonical + sparse overlay,
scoped BASE/NEW/CUR engine)** — the only approach that neutralizes L2/L3/L4 for
free, bounds the blast radius to opt-in users, and respects the loader-tier
asymmetry instead of routing around it. Its single weak column —
discoverability-on-disk — is cheaply restored by `/plugins list-ejectable` +
`/plugins show` (`27g.4` §2.2).

---

## 5. Consequences

### 5.1 Implementation epics/beads this UNBLOCKS

This ADR is the gate; with it recorded, the Bead Master may file the following
**proposed** beads (titles + 1-line scope verbatim from the syntheses — nothing
is created or built by this ADR).

**Extraction (from `tp4.6` §5):**
- **Epic P1 — Zero-seam extractions (Tier 0; parallel):** `hook_engine/`,
  `version_checker`, `mcp_prompts/hook_creator`, `image_tools`, `model_tools`,
  `error_logging`, `status_display` rate, finish `chatgpt_codex_client`.
- **Epic P2 — Keystone seams:** GAP-T2 (`register_agent_tools` removal/override),
  GAP-U1 (`register_commands` hook), GAP-M3 (native dispatch → internal table).
- **Epic P3 — Split-brain finishers:** skills_tools, universal_constructor,
  gemini_oauth, zai provider, uvx_detection.
- **Epic P4 — Seam-gated features:** `/colors`+`/diff` (GAP-U1), onboarding,
  `browser/` (GAP-T1), native `gemini` (GAP-M3), `session_storage` (GAP-X2),
  `summarization_agent` (GAP-S1).
- **Epic P5 — Big prize:** `mcp_/` subsystem via a toolset-injection hook
  (GAP-S2).
- **Epic P6 — Provider-seam hygiene:** consolidate `register_model_providers` +
  `register_model_type` (GAP-M1); registerable provider identity (GAP-M2).
- **Epic H — Internal 600-line splits (hygiene, stays in core):** split
  `config.py`, `tools/common.py`, `command_runner.py`,
  `messaging/rich_renderer.py`, `model_factory.py`, etc.; GAP-X1 import-arrow fix.

**Externalization (from `27g.4` §5), dependency order `E1 → E2 → E3 → E4`:**
- **Epic E1 — Loader parity & import normalization (Phase 0; no behavior
  change):** shared namespace-package helper (E1.1), builtin import-style
  normalization (E1.2), declarative `should_load` predicate to de-home the
  `shell_safety` gate (E1.3).
- **Epic E2 — Deterministic precedence / shadow mechanism (the hard
  dependency):** ejected/user copy suppresses the same-named builtin (E2.1);
  tier-collision policy + tests (E2.2).
- **Epic E3 — Scoped hash-aware sync engine:** `plugin_sync` module (E3.1),
  build-time newline-normalized shipped-manifest generator (E3.2), wire into
  `startup` before the idempotent load (E3.3).
- **Epic E4 — Opt-in eject surface:** `/plugins eject` (E4.1),
  `/plugins list-ejectable` + `show` (E4.2), `/plugins conflicts` reviewer
  (E4.3), dependency-cluster detection on eject (E4.4).
- **Deferred / YAGNI for v1:** diff3 auto-merge (OPT.1), rename-hint support
  (OPT.2).

**Cross-cutting prerequisite:** E1 + E2 (loader parity + shadow) are the shared
substrate for both programs and carry **no user-visible behavior change**, so
they can ship first to de-risk everything downstream.

### 5.2 Harness invariants that MUST be protected

Any implementation bead above must keep the `tp4.1` liveness properties green:

1. **BOOT** — `cli_runner.main_entry()` reaches the dispatch point without
   raising.
2. **LOAD-PLUGINS** — `plugins.load_plugin_callbacks()` completes and registers
   all three tiers through `callbacks.register_callback`.
3. **NO-OP-TURN** — `get_current_agent().run_with_mcp(<prompt>)` resolves a
   `Model`, builds the pydantic-ai `Agent`, and returns a result with **zero
   tools requested**.

Concrete must-keep modules (the harness floor, `tp4.6` §3 / `tp4.1` §1.1): the
boot spine (`cli_runner`/`main`/`__main__`/`pydantic_patches`), `callbacks.py`,
`plugins/__init__.py`, the agent run-loop quartet
(`base_agent`/`_builder`/`_runtime`/`agent_manager`), `model_factory.get_model`
(+ bundled `models.json`), the `tools/__init__.py` binder, and `config` +
`messaging` as boot-time dependencies. Additional invariants D2 must hold:

- **The canonical wheel copy is never removed** — it is the L2/L3/L4 safety net.
- **The user-override guarantee** (§D2) — ejected file content is never
  auto-overwritten; upstream lands as `<file>.new`; untracked files are never
  touched.
- **Newline-normalize text before hashing** — mandatory, or every Windows user
  hits spurious conflicts on every update (L4).
- **`shell_safety`'s conditional-load gate must not be orphaned** — it stays
  in-package (or moves behind the E1.3 `should_load` predicate), never homeless.

**Codify these as a tripwire:** file the proposed `tp4.1` §4.1 harness smoke-test
(BOOT / LOAD-PLUGINS / NO-OP-TURN as a pytest) so every extraction gets an
automated guard instead of a manual checklist.

### 5.3 Other consequences

- **Smaller install surface** as Tier 0 lands (esp. `hook_engine/`; later
  `browser/` + Playwright off the default install). The bundled 535 KB
  `models_dev_api.json` (GAP-D1) is parked, not scheduled.
- **Documentation debt:** SKILL §4.1 and `CONTRIBUTING.md` currently claim the
  three tiers behave identically and that "project wins on collision." E1/E2 make
  that *true* (it isn't, for builtin clashes, today — both copies load and fire,
  §0). Docs update ships with E2.
- **No regression budget for the harness:** the boundary is operational, not
  cosmetic — a bead that breaks a liveness property is incorrect by definition.

---

## 6. Ratification & Acceptance Mapping

| Bead acceptance criterion | Where satisfied |
|---------------------------|-----------------|
| Decision: chosen extraction scope + sequencing | §2 D1 (Tier 0 → keystones → Tier 1 → Tier 2 → Tier 3; traps deferred) |
| Decision: chosen externalization/override + update mechanism | §2 D2 (lazy hybrid eject-over-canonical + sparse overlay + scoped BASE/NEW/CUR; shadow mechanism as the hard dependency) |
| Rationale grounded in the two syntheses + harness-boundary criteria | §3 (6 points, each cited to `tp4.6` / `27g.4` / `tp4.1` + SKILL §) |
| Alternatives Considered: rejected extraction scopes | §4.1 |
| Alternatives Considered: hash-copy vs overlay vs eject vs manifest (vs CAS vs import-hook) | §4.2 |
| Consequences: implementation epics/beads unblocked | §5.1 (Epics P1–P6, H, E1–E4 + deferred) |
| Consequences: harness invariants to protect | §5.2 (3 liveness properties + must-keep floor + D2 invariants + tripwire) |
| REQUIRED FIRST STEP: leverage `code-puppy-agent` skill, evidenced | §0 grounding table (skill activated; each decision cited to a SKILL.md section; 2 claims re-verified against source) |

**Decision in one line:** *Finish the proven thin-binder/leaves-move pattern in
tiered, seam-gated order (Tier 0 first, three keystones before Tier 2), and make
builtins user-modifiable via opt-in per-plugin eject over an always-present
in-package canonical copy with a scoped BASE/NEW/CUR hash engine — protecting the
BOOT / LOAD-PLUGINS / NO-OP-TURN harness floor as an invariant throughout.*

*This ADR records the decision only. It implements nothing. The judges are the
only legitimate closer of `puppy-1ng`.*
