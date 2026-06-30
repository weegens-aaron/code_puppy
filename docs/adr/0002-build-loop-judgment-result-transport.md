# ADR 0002: build-loop judgment-result shape + transport

- **Status:** Accepted
- **Bead:** `bead-factory-1r6` (parent epic `bead-factory-vui`)
- **Type:** Decision (ADR)
- **Blocks:** `bead-factory-ush` (Add `BuildResult` dataclass + builder from verdicts)
- **Labels:** architecture, bead-factory, build-loop

> This document is the **single source of truth** for *what* the build loop's
> judgment result contains and *how* it travels from the build-loop turn hook to
> the chain driver. Implementation slices follow it exactly.

## Context

When the build loop's inspector judgment finishes, `build_loop.on_interactive_turn_end`
computes rich data in `_run_build_inspectors` — per-inspector verdicts, the
pass/fail/abstain tally, and the aggregated remediation notes — then **displays
and discards** it. The loop exits via the bit-flag contract: a continuation dict
(retry) or `None` (stop). Future headless `--bead-factory` mode
(`bead-factory-ogz`) and the chain driver's close boundary both want this
structured result, so we must capture and transport it.

### The binding constraint: the turn-hook return contract

`interactive_turn_end` callbacks return **`dict | None`** and nothing else:

- `dict` → CLI re-runs the turn with that continuation prompt (retry).
- `None` → the turn ends (stop).

The host walks the registered callbacks; the build-loop hook is registered
**first** and the chain-driver hook (`chain_driver._on_interactive_turn_end`)
**second** (`_ensure_hooks_registered`, build-then-chain ordering). The
structured result therefore **cannot ride the return value** — that channel is
already spoken for and semantically overloaded. It must travel a **side
channel** that the chain driver reads at the close boundary.

### Fail-soft discipline

Per the plugin rules, the build loop must **never crash the REPL**. Every bd
call, render, and inspector run already soft-fails. The result transport must
honour the same discipline: a missing/empty result must degrade to today's
behaviour (chain driver proceeds with no result), never raise.

## Decision

### (a) Result field set — `BuildResult`

A frozen dataclass enumerating exactly:

| Field | Type | Meaning |
|-------|------|---------|
| `completed` | `bool` | `True` iff every non-abstaining inspector passed (== `stop_reason is COMPLETE`). |
| `stop_reason` | `StopReason` enum | Why the loop exited (see below). |
| `total` | `int` | Total inspectors that ran (`len(verdicts)`). |
| `passed` | `int` | Non-abstaining inspectors with `complete=True`. |
| `failed` | `int` | Non-abstaining inspectors with `complete=False`. |
| `abstained` | `int` | Inspectors that abstained (excluded from the vote). |
| `verdicts` | `list[BuildInspection]` | Per-inspector verdicts, verbatim. |
| `aggregated_notes` | `str` | The formatted remediation block (`_format_remediation_block`). |
| `loop_count` | `int` | Iteration number at exit (`build_state.loop_count`). |
| `bead_id` | `str \| None` | The bead being built (`build_state.bead_id`). |

Invariant: `total == passed + failed + abstained`, and
`completed == (stop_reason is StopReason.COMPLETE)`. `completed` is kept as an
explicit field (despite being derivable) because headless consumers and the
close boundary read it as the primary success bit without needing the enum.

#### `StopReason` enum

One value per real exit path in `on_interactive_turn_end`:

| Value | Exit path |
|-------|-----------|
| `COMPLETE` | All voting inspectors passed → `BUILD COMPLETE!` |
| `MAX_ITERATIONS` | `loop_num >= max_iters` → `BUILD STOPPED` |
| `CANCELLED` | `CancelledError`/`KeyboardInterrupt` (Ctrl+C) |
| `NO_PROMPT` | No active build prompt at hook entry (defensive `state.stop()` early-return) |

> The `RETRY` path (loop incomplete, returning a continuation dict) is **not** a
> stop reason — the loop is still running, so no terminal result is emitted on
> that turn. A `BuildResult` is produced **only on the terminal turn** (the one
> that returns `None`).

### (b) Transport — dedicated results sink module

Introduce a tiny **results sink singleton** module
(`build_result.py`, alongside `build_loop.py` / `build_state.py`) with
**consume-once** semantics:

```python
def set_last(result: BuildResult) -> None: ...   # build loop writes at each terminal exit
def take_last() -> BuildResult | None: ...        # chain driver pops + clears at close boundary
def peek_last() -> BuildResult | None: ...        # non-destructive read (tests / headless)
def clear() -> None: ...                           # test isolation
```

The build loop calls `set_last(...)` **immediately before** each `state.stop()`
(or the `NO_PROMPT` early-return), wrapped so a sink failure can never crash the
loop. The chain driver calls `take_last()` at its close boundary
(`_on_interactive_turn_end`, after it observes `build_state` went inactive).

## Rationale

### Why a dedicated sink, not `build_state`

`build_state.stop()` **zeroes `loop_count`, `bead_id`, prompts** — and the build
loop calls `stop()` on the *same dispatch turn*, *before* the chain-driver hook
runs second. Any result stashed on `build_state` would be wiped by the very
`stop()` that signals "build done" before the chain driver could read it. Making
the result survive `stop()` would mean carving an exception into a box whose
entire contract is "self-clears on start/stop" — a lifecycle landmine and an SRP
violation (`build_state` is the *input/identity* box, not an *output* box).

### Why not `ChainState`

`ChainState` is the chain's own lifecycle box (active flag, current bead,
completed tally). Bolting build-loop output onto it couples a sub-component's
result schema into the chain's identity state and inverts the dependency: the
build loop already imports `state as chain_state` to refresh the pin, but making
it *write its result product* into chain state widens that coupling and muddies
ownership. The chain driver should *read* the build loop's output, not *host* it.

### Why the sink wins

- **Decoupled from both lifecycles.** Neither `build_state.stop()` nor
  `ChainState.stop()` can clobber it; the sink owns its own clear.
- **Consume-once kills staleness.** `take_last()` pops-and-clears, so a verdict
  from bead N can never leak into bead N+1's close decision — the exact wipe
  risk that sank the `build_state` option, solved by design instead of by luck.
- **Fail-soft by construction.** If the loop crashes before `set_last`, the sink
  is empty and `take_last()` returns `None` → chain driver proceeds exactly as
  today. No new crash surface.
- **Headless-ready.** `--bead-factory` mode reads `peek_last()`/`take_last()`
  without depending on either state box.
- **SRP-clean.** One module, one job: hold the most recent terminal
  `BuildResult` between the producing hook and its consumer.

Cost: one more small module. Accepted — it is strictly less coupling than the
alternatives and matches the existing tiny-singleton pattern already used by
`build_state` and `state`.

## Alternatives Considered

- **Stash on `build_state`** (next to `loop_count`/`bead_id`) — rejected.
  `stop()` runs on the terminal turn *before* the chain driver reads, wiping the
  result. Surviving `stop()` would break the box's self-clearing contract and
  conflate input state with output product.
- **Attach to `ChainState`** (new field, read at close boundary) — rejected.
  Couples build-loop output into chain identity state and inverts ownership; the
  chain driver should consume the result, not host it.
- **Return it up the hook chain** — rejected. Impossible: the
  `interactive_turn_end` contract is `dict | None`, already overloaded as the
  retry/stop bit flag.

## Consequences

- `bead-factory-ush` implements `BuildResult` + `StopReason` + the builder from
  `(complete, notes, verdicts, loop_count, bead_id, stop_reason)` exactly per the
  table above, plus the `build_result.py` sink module.
- The build loop gains a `set_last(...)` call before every terminal exit
  (`COMPLETE`, `MAX_ITERATIONS`, `CANCELLED`, `NO_PROMPT`), each wrapped
  fail-soft.
- The chain driver reads `take_last()` at the close boundary; absent result →
  unchanged behaviour.
- Tests assert the consume-once contract and the
  `total == passed + failed + abstained` / `completed == (stop_reason is COMPLETE)`
  invariants.
