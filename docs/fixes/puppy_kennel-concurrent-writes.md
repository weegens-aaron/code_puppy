# Fix: puppy_kennel concurrent multiprocess writes silently dropped

**Commit (original):** `1888a7c` — cherry-picked onto `fix/puppy-kennel-concurrent-writes` as `7ea9bce`
**Plugin:** `code_puppy/plugins/puppy_kennel/` (SQLite-backed store)
**Scope:** 3 files, +33 / −2 — `schema.py`, `state.py`, `tests/plugins/test_puppy_kennel.py`

> **Note on the `bead-factory-bna` tag in the original commit message:** that is a
> bead-ID from a temporary/stealth bead tracker that prefixes *every* bead
> `bead-factory-`. It is **not** a coupling to the `bead_factory` plugin. This fix
> is entirely self-contained within `puppy_kennel`; `bead_factory` neither caused
> it nor depends on it.

---

## Symptom

The test `test_concurrent_multiprocess_writes_do_not_corrupt` fired **500 writes**
across multiple spawned processes and observed `count_drawers == 0` — i.e. **every
single write was silently lost** (no error, no corruption, just gone).

## Two independent root causes

### 1. Enable-check did not survive a `multiprocessing` *spawn*

`is_enabled()` read only the persisted `kennel_enabled` flag from `puppy.cfg`.

- The test harness isolates config **in the parent process only**.
- `multiprocessing` *spawn* starts a **fresh interpreter** per child, which re-reads
  the developer's **real** `puppy.cfg`.
- So if the developer happened to have `kennel_enabled = false` locally, every
  spawned child silently **no-op'd** its write — 500 writes → 0 rows.

**Fix:** added a `PUPPY_KENNEL_ENABLED` environment override (mirrors the existing
`PUPPY_KENNEL_ROOT`). Env vars **are** inherited across the spawn boundary, unlike
in-parent config isolation. `is_enabled()` now checks the env var first, then falls
back to the cfg value. The test pins it on in both the fixture and the worker.

```python
raw = os.environ.get(_ENV_KEY)   # PUPPY_KENNEL_ENABLED — survives spawn
if raw is None:
    raw = get_value(_CFG_KEY)    # kennel_enabled in puppy.cfg
```

### 2. PRAGMA ordering race on first concurrent `initialize()`

`PRAGMA busy_timeout=5000` was applied **after** `PRAGMA journal_mode=WAL`.

- Switching `journal_mode` to WAL takes a brief **exclusive lock**.
- With N processes calling `initialize()` simultaneously, the first collision could
  hit `OperationalError: database is locked` **before** the busy handler was armed.

**Fix:** reordered the `PRAGMAS` tuple so `busy_timeout` comes **first**, arming the
5-second grace period *before* the WAL switch (or any write) is attempted.

```python
PRAGMAS = (
    "PRAGMA busy_timeout=5000",   # arm the grace FIRST
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)
```

## Why it mattered

Without these fixes, concurrent multi-process access to the kennel DB could either
**lose writes outright** (cause #1) or **crash on startup contention** (cause #2) —
silent data loss being the more dangerous of the two.

## Verification

```bash
python -m pytest tests/plugins/test_puppy_kennel.py -q
```

## Why this is on its own branch

It was originally committed onto the `bead-factory` working branch and tagged with a
stealth-tracker bead ID, which made it *look* associated with the `bead_factory`
plugin work. It is not — it's an independent `puppy_kennel` concurrency fix, so it
has been cherry-picked onto `fix/puppy-kennel-concurrent-writes` (based on `main`)
to be reviewed and merged on its own.
