# Hash-Aware Plugin Update / Override Algorithm — Design Spike

> **Bead:** `puppy-27g.2` (DISCOVERY spike — research only, no implementation).
> **Parent epic:** `puppy-27g` — Externalize Builtin Plugins with Hash-Aware Updates.
> **Feeds:** `puppy-27g.4` (synthesis).
>
> This document specifies *how* code_puppy should update externalized plugin
> files across releases **without clobbering user modifications**. It does not
> decide *whether* to externalize, *which* plugins move, or the loader changes
> (those are `27g.1` / `27g.3`). The algorithm here is deliberately agnostic to
> the exact on-disk location, referred to throughout as the **managed plugin
> root**.

---

## 1. Problem Statement

Today builtin plugins ship *inside* the Python package at
`code_puppy/plugins/<name>/` (see `code_puppy/plugins/__init__.py`,
`_load_builtin_plugins`). They are read-only: `pip install --upgrade` replaces
the whole package directory, so there is no concept of a "user edit" surviving
an upgrade — and no way for a user to tweak a builtin plugin in place.

Epic `27g` wants to **externalize** these plugins onto disk (e.g. under
`~/.code_puppy/plugins/`) so users can read and edit them. That immediately
creates the package-manager problem every distro has solved:

> When a new release ships an updated version of a file the user may have
> edited locally, what happens?

Naïvely copying on every startup has two failure modes, both unacceptable:

- **Clobber:** overwrite the user's file → silent data loss of their edits.
- **Skip:** never overwrite → users are frozen on stale plugin code forever and
  never receive bug fixes.

The fix is to be **hash-aware**: detect whether each file was changed by the
*user*, by *upstream*, or by *both*, and act accordingly.

---

## 2. Core Model — Three Hashes

For every managed file path we reason about **three** content hashes:

| Symbol | Meaning | Source |
|--------|---------|--------|
| **BASE** | hash of the file *as code_puppy last shipped/wrote it* on this machine | the **installed manifest** on disk |
| **NEW**  | hash of the file in the *release being installed now* | the **shipped manifest** inside the package |
| **CUR**  | hash of the file *currently on disk* | computed live at update time |

Two derived booleans drive every decision:

```
user_modified  := (CUR != BASE)   # the user touched it since we last wrote
upstream_change := (NEW != BASE)   # we changed it in this release
```

> **Why BASE comes from a stored manifest, not from CUR:** we cannot ask the
> filesystem "did the user edit this?" — the filesystem only knows the current
> bytes. We must remember what *we* last wrote (BASE) to tell user edits apart
> from our own. This is exactly how dpkg stores conffile md5sums in
> `/var/lib/dpkg/status` and pacman stores mtree hashes.

This is a textbook **three-way merge** base / theirs / mine, where:
`BASE = merge base`, `NEW = theirs (upstream)`, `CUR = mine (user)`.

---

## 3. Decision Table (precise spec)

Iterate over the union of paths in `BASE ∪ NEW ∪ disk`. For each path:

| # | BASE | NEW | CUR | Classification | **Action** |
|---|------|-----|-----|----------------|-----------|
| 1 | present | == BASE | == BASE | nothing changed anywhere | **no-op** |
| 2 | present | != BASE | == BASE | clean upstream update, user untouched | **write NEW** (in place) |
| 3 | present | == BASE | != BASE | user owns it, upstream unchanged | **preserve CUR** (keep user file) |
| 4 | present | != BASE | == NEW | both made the *same* change (converged) | **no-op** (already == NEW) |
| 5 | present | != BASE | != BASE and != NEW | **TRUE 3-WAY CONFLICT** | **conflict surface** (Sec 5) |
| 6 | absent (added) | present | absent | new file from upstream | **write NEW** |
| 7 | absent (added) | present | == NEW | user already has identical file | **no-op** (adopt) |
| 8 | absent (added) | present | != NEW | user authored a file we now also ship | **conflict surface** (Sec 5) |
| 9 | present | absent (deleted) | == BASE | upstream removed, user untouched | **delete file** |
| 10 | present | absent (deleted) | != BASE | upstream removed, but user modified | **preserve CUR + warn** (orphan) |
| 11 | present | absent (deleted) | absent | already gone | **no-op** |
| 12 | absent (untracked) | absent | present | file the user added, never shipped | **never touch** (out of scope) |

Rows 3, 10, 12 are the "preserve user work" guarantees. Rows 2, 6, 9 are the
"deliver upstream changes" guarantees. Rows 5 and 8 are the only cases that need
human attention.

### 3.1 Pseudocode

```python
def plan_update(base: Manifest, new: Manifest, root: Path) -> list[Op]:
    """Pure planning pass. No side effects — returns an ordered op list.

    base = installed manifest (what we last wrote here)   -> BASE hashes
    new  = shipped manifest (this release)                -> NEW hashes
    root = managed plugin root on disk                    -> CUR hashes (live)
    """
    ops: list[Op] = []
    paths = set(base) | set(new)        # only files WE manage; row 12 excluded

    for path in sorted(paths):
        b = base.get(path)              # BASE hash or None (added)
        n = new.get(path)               # NEW hash or None (deleted)
        cur = hash_on_disk(root / path) # CUR hash or None (absent)

        # ---- deletions (NEW absent) ----
        if n is None:
            if cur is None:
                continue                                   # row 11
            if cur == b:
                ops.append(Delete(path))                   # row 9
            else:
                ops.append(KeepOrphan(path, reason="user-modified")) # row 10
            continue

        # ---- additions (BASE absent) ----
        if b is None:
            if cur is None:
                ops.append(Write(path, n))                 # row 6
            elif cur == n:
                ops.append(Adopt(path, n))                 # row 7 (no write)
            else:
                ops.append(Conflict(path, base=None, new=n, cur=cur))  # row 8
            continue

        # ---- present in both BASE and NEW ----
        user_modified  = (cur != b)
        upstream_change = (n != b)

        if not upstream_change and not user_modified:
            continue                                       # row 1
        elif upstream_change and not user_modified:
            ops.append(Write(path, n))                     # row 2
        elif not upstream_change and user_modified:
            ops.append(Preserve(path))                     # row 3
        elif cur == n:
            ops.append(Adopt(path, n))                     # row 4 (converged)
        else:
            ops.append(Conflict(path, base=b, new=n, cur=cur))  # row 5

    return ops


def apply_update(ops, root, new: Manifest) -> Manifest:
    """Execute the plan atomically-ish, then rewrite the installed manifest."""
    conflicts = []
    for op in ops:
        match op:
            case Write(path, _) | Adopt(path, _):
                atomic_write(root / path, new.blob(path))  # temp + os.replace
            case Delete(path):
                safe_unlink(root / path)
            case Conflict(path, base, new_hash, cur):
                # NON-DESTRUCTIVE: keep the user's file, drop upstream beside it
                atomic_write(root / f"{path}.new", new.blob(path))
                conflicts.append(path)
            case Preserve(_) | KeepOrphan(_) :
                pass                                        # leave disk as-is

    if conflicts:
        emit_warning(f"{len(conflicts)} plugin file(s) had conflicting "
                     f"changes; upstream versions written as *.new. "
                     f"Run /plugins conflicts to review.")

    return write_installed_manifest(root, advance_baseline(new, ops))
```

### 3.2 Baseline advancement (the subtle bit)

After a successful run, rewrite the installed manifest so the *next* update
diffs correctly. The new BASE for each path is the **upstream hash we last
offered** — i.e. `NEW` — **for every managed path**, *including* preserved and
conflicted ones. Rationale:

- Rows 2/4/6/7: we wrote NEW, so BASE := NEW is obviously correct.
- Row 3 (preserved, upstream unchanged): NEW == BASE anyway → no change.
- Rows 5/8 (conflict): we advance BASE := NEW even though we kept the user's
  file. This means the user stays flagged `user_modified` until they
  reconcile — *and that is correct*. It guarantees we never silently re-clobber
  them and never re-prompt for the same already-shipped version. This mirrors
  dpkg recording the shipped conffile hash after a prompt.
- Rows 9/11 (deleted): drop the path from the manifest.
- Row 10 (orphan kept): drop from manifest → becomes an untracked user file
  (row 12 semantics from now on). We will never touch it again.

---

## 4. Manifest — Format, Location, Versioning

There are **two** manifests, and conflating them is the classic bug:

### 4.1 Shipped manifest (NEW, read-only, in-package)

Generated at **build time**, shipped inside the wheel. Source of `NEW` hashes
and the file blobs to write. Lives at e.g.
`code_puppy/plugins/_shipped_manifest.json` (package data).

### 4.2 Installed manifest (BASE, read-write, on disk)

Written by code_puppy after every sync into the managed plugin root, e.g.
`<managed_root>/.code_puppy_plugins_manifest.json`. Source of `BASE` hashes.
Absent on a fresh install → triggers bootstrap (§6.1).

### 4.3 Format

```json
{
  "schema_version": 1,
  "package_version": "1.4.2",
  "generated_at": "2026-06-18T21:00:00Z",
  "hash_algo": "sha256",
  "newline_normalized": true,
  "files": {
    "emoji_filter/register_callbacks.py": {
      "sha256": "9f2c…",
      "mode": "0644",
      "binary": false
    },
    "emoji_filter/__init__.py": { "sha256": "1ab3…", "mode": "0644", "binary": false }
  }
}
```

- **Flat, path-keyed** (paths relative to the managed root, forward-slashed and
  normalized so they compare identically on Windows/POSIX). Flat beats
  per-plugin nesting — *flat is better than nested*, and the loader already
  treats each top-level dir as a plugin so the namespace is implicit in the key.
- `package_version` lets us short-circuit: if installed `package_version` ==
  shipped, skip the whole scan (fast no-op on every normal startup).
- `schema_version` gates manifest-format migrations.
- `mode` (optional) preserves the executable bit; `binary` selects raw-vs-
  normalized hashing (§7.1).

### 4.4 Versioning strategy

- One manifest covers **all** managed plugins (single source of truth, single
  atomic write). Per-plugin versioning is YAGNI for v1 — every plugin ships in
  lockstep with the package.
- The installed manifest is the *only* persistent state. It is rebuilt from the
  shipped manifest on every successful sync, so a corrupted/missing installed
  manifest is self-healing via bootstrap (§6.1) rather than fatal.

---

## 5. Conflict-Surface UX Decision

**Decision: non-blocking `.new` sidecar (pacman/`.rpmnew` style), with an
optional clean-3-way-auto-merge enhancement deferred to a follow-up.**
*Never* prompt interactively in the update path; *never* clobber.

### 5.1 Options considered

| Strategy | User edits safe? | Upstream delivered? | Blocks startup/CI? | Verdict |
|----------|------------------|---------------------|--------------------|---------|
| Overwrite (clobber) | NO (data loss) | yes | no | **rejected** — violates the whole point |
| Skip (keep user) | yes | NO (frozen stale) | no | **rejected** — no bug fixes ever land |
| Interactive prompt (dpkg) | yes | yes | **YES** | **rejected** — updates run at `pip`/startup, often non-TTY/CI |
| **`.new` sidecar** (pacman) | yes | yes (beside) | no | **chosen** — proven, reversible, deferrable |
| Inline 3-way auto-merge (git) | yes (when clean) | yes | no | **chosen as optional layer** atop sidecar |

### 5.2 Chosen behavior

On a conflict (rows 5 and 8):

1. **Leave the user's file byte-for-byte untouched.**
2. Write the upstream version next to it as `<file>.new`.
3. (Row 5 only) optionally also write the merge base as `<file>.orig` to make a
   manual 3-way merge possible.
4. Emit **one** aggregated `emit_warning` via the message bus naming the
   conflicting files and pointing at a review command.
5. Propose a follow-up `/plugins conflicts` command (review / accept-upstream /
   keep-mine / open-diff). **Out of scope for this spike** — filed as a
   follow-up bead suggestion (§9).

### 5.3 Optional auto-merge layer (deferred)

When BASE, NEW, and CUR are all UTF-8 text, attempt a `diff3`/`git merge-file`
style three-way merge. If it merges with **zero** conflict hunks, apply the
merged result in place and *skip* the sidecar (best UX — user keeps edits *and*
gets upstream fixes). If it would produce conflict markers, fall back to the
`.new` sidecar. This is strictly additive and is **explicitly YAGNI for v1** —
the sidecar alone is a complete, safe solution.

---

## 6. Edge Cases

### 6.1 Bootstrap (no installed manifest / fresh externalization)

First time we externalize (or installed manifest is missing/corrupt) there is
no BASE. Treat every shipped file as an **adopt** candidate:

- file absent on disk → write NEW (row 6).
- file present and `CUR == NEW` → adopt silently, no write (row 7).
- file present and `CUR != NEW` → we *cannot* know if it's a user edit or a
  stale leftover, so be conservative → **conflict / `.new` sidecar** (row 8).

Then write the installed manifest = shipped manifest. Subsequent updates have a
real BASE and behave per §3.

### 6.2 Line endings — **the Windows gotcha**

The repo's `.gitattributes` does **not** enforce LF (verified: it only has a
beads merge rule). If we hash raw bytes, a checkout under `core.autocrlf=true`
rewrites every shipped `.py` to CRLF on disk while the manifest (generated on a
LF box) records the LF hash → **every file falsely flags as `user_modified`** →
spurious conflicts for every Windows user on every update. This is the single
most likely way to ship a broken algorithm.

**Mitigation:** for text files (`binary: false`), **normalize newlines to `\n`
before hashing** (both at build time and at scan time). Hash binary files raw.
Write files using the manifest's recorded line style (LF). Document the policy
in the manifest via `newline_normalized: true`. Optionally also strip a single
trailing-newline ambiguity — but do **not** strip interior whitespace (too
aggressive; would mask real edits).

### 6.3 Atomicity & idempotency

- Write each file to a temp path in the same dir, then `os.replace` (atomic on
  POSIX and Windows).
- Rewrite the installed manifest **last**, after all file ops succeed.
- If interrupted mid-run, re-running is safe: CUR is recomputed live and the
  plan converges (a half-written file is just another CUR state). The algorithm
  is fully idempotent — running it twice in a row is a guaranteed no-op the
  second time.

### 6.4 Untracked user files (row 12)

Any file under the managed root that appears in **neither** BASE nor NEW is a
pure user creation. We **never** delete or overwrite it. Only files we have
provably shipped (present in BASE) are eligible for deletion (rows 9/10). This
makes "user drops an extra `helpers.py` into a builtin plugin dir" safe.

### 6.5 Permissions / executable bit

Track `mode` in the manifest and restore it on write. Low priority; most plugin
files are plain `.py`. A "mode changed but content didn't" case is treated as
no-op for v1 (content is the only thing we hash).

---

## 7. Renames

Without explicit metadata, a rename is indistinguishable from `delete(old) +
add(new)` and is handled correctly *but bluntly* by the table:

- old path: row 9 (deleted, untouched → removed) or row 10 (user-modified →
  preserved as orphan).
- new path: row 6 (written).

The risk: if the user had edited `old`, their edits don't migrate to `new`;
`old` is preserved as an orphan (data kept, just stranded) and `new` arrives
fresh. **No data loss**, but a confusing split.

**Recommendation:** default to delete+add semantics (simple, *flat is better
than nested*). Allow the shipped manifest to *optionally* carry rename hints:

```json
"renames": [{ "from": "old/path.py", "to": "new/path.py" }]
```

When a hint is present and `old` is `user_modified`, surface a conflict on the
`to` path (write the user's stranded content as `<to>.orig` and upstream as the
live file) so the user is told their edits moved. This is an **optional**
enhancement — recommend filing it as a follow-up, not building it in v1.

---

## 8. Prior-Art Comparison

| System | Tracks BASE? | Conflict resolution | Non-blocking? | What we borrow |
|--------|--------------|---------------------|---------------|----------------|
| **dpkg conffiles** | yes (md5 in `status`) | interactive prompt (keep/replace/diff/shell), default keep | no (prompts) | the stored-base 3-way detection + advancing base after a "keep" |
| **`ucf`** (dpkg helper) | yes | adds 3-way merge via diff3 on top of dpkg | partial | the optional auto-merge idea (Sec 5.3) |
| **pacman `.pacnew`** | yes (mtree) | writes `.pacnew` sidecar, never clobbers; `.pacsave` on remove | yes | **our primary model** — the sidecar |
| **rpm `.rpmnew`/`.rpmsave`** | yes | `%config`: ship new as `.rpmnew` if user-modified; `%config(noreplace)` | yes | per-file "noreplace" framing |
| **git 3-way merge** | yes (merge base) | auto-merge, conflict markers on overlap | yes (markers) | the diff3 auto-merge layer + BASE/NEW/CUR vocabulary |
| **npm `node_modules`** | no | full overwrite, no preservation | yes | nothing — node_modules is disposable, not user-editable |
| **pip package dir** | no | overwrites | yes | nothing — same disposability assumption (this is *exactly* the assumption externalization breaks, hence this spike) |
| **Homebrew formulae** | n/a | reinstall; user edits not preserved | yes | nothing |

**Synthesis:** our design = **pacman's non-blocking sidecar** (UX) + **dpkg's
stored-base 3-way detection** (correctness) + **git/ucf's optional clean
auto-merge** (deferred niceness). We deliberately reject pip/npm's
"package dirs are disposable" assumption, because externalization's whole
purpose is to make plugin files user-editable.

---

## 9. Proposed Follow-Up Beads (for the synthesis, `27g.4`)

These are *suggestions* for implementation work — this spike does **not** create
them (DISCOVERY epic: propose, don't build):

1. **Implement `plugin_sync` module** — `plan_update` / `apply_update`,
   shipped-manifest build-time generator, installed-manifest read/write.
2. **Build-time manifest generation** — hook into the packaging step
   (`pyproject.toml` / build backend) to emit `_shipped_manifest.json` with
   newline-normalized sha256 hashes.
3. **`/plugins conflicts` command** — review/accept/keep/diff UI for `.new`
   sidecars (a `custom_command` plugin, naturally).
4. **Wire sync into startup** — call the planner once per launch, fast-pathed by
   the `package_version` short-circuit; emit conflict warnings via message bus.
5. **(Optional) diff3 auto-merge layer** — §5.3.
6. **(Optional) rename-hint support** — §7.

---

## 10. Findings Summary (for `27g.4`)

- **Algorithm:** a three-hash (BASE/NEW/CUR) three-way model. BASE comes from an
  on-disk **installed manifest**, NEW from an in-package **shipped manifest**,
  CUR is computed live. A 12-row decision table (§3) covers
  unmodified/modified/added/deleted/converged/conflict; pseudocode in §3.1.
- **Baseline always advances to NEW** after a sync, even for preserved/conflict
  files — this prevents re-clobber and re-prompt loops (§3.2).
- **Manifest:** flat, path-keyed JSON, sha256, `package_version` fast-path,
  `schema_version` for migrations; two copies (shipped=read-only in package,
  installed=read-write in managed root) — **conflating them is the classic
  bug**.
- **Conflict UX:** non-blocking `.new` sidecar (pacman/rpm style), aggregated
  warning, proposed `/plugins conflicts` reviewer. Interactive prompts rejected
  (updates run non-TTY). Inline diff3 auto-merge is a deferred optional layer.
- **Biggest implementation risk:** CRLF/LF hash instability on Windows
  (`.gitattributes` does not enforce LF). **Must normalize newlines before
  hashing text files**, or every Windows user hits spurious conflicts on every
  update.
- **Safety invariants:** never delete a file not in BASE (untracked = user's);
  never overwrite a `user_modified` file; atomic temp+replace writes; manifest
  written last; fully idempotent.
- **Prior art:** borrow pacman sidecar (UX) + dpkg stored-base (correctness) +
  git/ucf auto-merge (deferred). Reject pip/npm disposability assumption.
```
