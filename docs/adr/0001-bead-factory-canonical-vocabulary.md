# ADR 0001: bead_factory canonical vocabulary mapping

- **Status:** Accepted
- **Bead:** `bead-factory-mxy` (parent epic `bead-factory-881`)
- **Type:** Decision (ADR)
- **Downstream slices:** `bead-factory-89g` (build rename), `bead-factory-4fd` (factory-name rename)

> This document is the **single source of truth** for the vocabulary rename.
> Every downstream slice follows this map exactly — no improvisation, no shims.

## Context

`bead_factory` must stand alone. The plugins it grew out of — `wiggum` and the
old `bead-chain` — may be deleted any day. So:

- **No cross-plugin imports.** Zero imports of any other `code_puppy.plugins.*`.
- **No inherited vocabulary.** Source-plugin identity (`goal`, `wiggum`) must not
  leak into a standalone plugin's public surface.

We collapse the loop concept onto **one native `build` verb** (`goal` and the
dissolved `wiggum` mode merge into it), finish the already-started
`judges` → `inspectors` rename, and rename the plugin itself
`bead-chain` → `bead-factory`. The generic word **`chain`** is kept as a neutral
mechanism term — it is not the plugin's identity, so it does not move.

## Decision — VOCAB MAP

### 1. `goal` → `build`

| Old | New |
|-----|-----|
| `goal_loop.py` | `build_loop.py` |
| `get_goal_max_iterations` | `get_build_max_iterations` |
| `GOAL_MAX_ITERATIONS_*` | `BUILD_MAX_ITERATIONS_*` |
| `bf_goal_max_iterations` | `bf_build_max_iterations` *(hard rename, **no shim**)* |
| `_run_goal_inspectors` | `_run_build_inspectors` |
| `inspect_goal` | `inspect_build` |
| `inspect_goal_history` | `inspect_build_history` |
| `GoalInspection` / `GoalInspectionOutput` | `BuildInspection` / `BuildInspectionOutput` |
| `format_bead_as_goal` | `format_bead_as_build` |
| banners `GOAL MODE` / `COMPLETE` / `INCOMPLETE` / `STOPPED` | `BUILD MODE` / `…` |
| prompt text `'goal'` | `'build'` |

### 2. `wiggum` → dissolved into `build`

| Old | New |
|-----|-----|
| `loop_state.py` | `build_state.py` |
| `WiggumState` | `BuildState` |
| `wiggum_state` alias | `build_state` |
| all `wiggum` comments | removed |

> **Note on intentional `wiggum` survivors (do NOT scrub):** accurate provenance
> docstrings (e.g. "Relocated from the former wiggum plugin") stay. The
> `wiggum_state` alias is a *code contract* until the build rename slice lands —
> it is removed *as part of* that slice, not by a blanket find/replace.

### 3. `judges` → `inspectors`

Residual prose only — the symbol rename is already done. Clean up remaining
`judge(s)` references in:
`prompt.py`, `prompt_blocks.py`, `close_guard.py`, `inspectors_menu*.py`.

### 4. `bead-chain` (the **plugin name**) → `bead-factory`

| Old | New |
|-----|-----|
| `BeadChainState` | `ChainState` |
| `handle_bead_chain_command` | `handle_bead_factory_command` |
| user-facing strings `'bead-chain'` | `'bead-factory'` |

**KEEP:** the generic word `chain` and the `chain_driver.py` filename. `chain` is
a neutral mechanism term, not the plugin's identity.

### 5. id-citations → scrubbed

Remove foreign-tracker citation tags `(bead_chain-xxx)`, `(bdboard-xxx)`,
`(wiggum)` from prose. **Keep the surrounding prose** — only the tag goes.

### 6. standalone

Zero imports of `code_puppy.plugins.*` (any other plugin). `bead_factory`
depends on core only.

## Alternatives Considered

- **Keep `goal` / `wiggum` as-is** — rejected. Leaks dead source-plugin identity
  into a standalone plugin.
- **Rename generic `chain` too** — rejected by scope decision. `chain` is a
  neutral term; renaming it is churn for no clarity gain.
- **Config-key fallback shim** (`bf_build_max_iterations` → `bf_goal_max_iterations`)
  — rejected. Pre-release; a hard rename is cleaner than carrying a shim.
- **Keep historical `(bead_chain-xxx)` citations** — rejected. They reference a
  foreign tracker and read as a cross-dependency.

## Consequences

- Downstream rename slices have an unambiguous checklist; reviews check against
  this table instead of re-litigating naming.
- `chain_driver.py` and the generic `chain` vocabulary are explicitly out of
  scope, preventing accidental over-renaming.
- After all slices land: no `wiggum`, `judge`, or `bead-chain` (plugin-name)
  references remain in `code_puppy/plugins/bead_factory`, and the plugin imports
  core only.
