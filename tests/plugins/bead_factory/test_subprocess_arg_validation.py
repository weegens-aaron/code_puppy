"""Security tests: bead-id sanitization + BEADS_BIN validation (bead_chain-b2j).

Two hardening items, both about what reaches ``subprocess.run`` in
:mod:`beads`:

1. **Bead ids** flow from bd JSON output and CLI args straight into bd
   as subprocess args. List-form ``subprocess.run`` blocks *shell*
   injection, but a crafted id (leading dash, whitespace, NUL, shell
   metachars) can still confuse bd's own argument parser. They must be
   pinned to ``^[a-zA-Z0-9_.-]+$`` and rejected loudly otherwise.

2. **BEADS_BIN** is an attacker-reachable env var that picks the
   executable. It must resolve to an absolute, real, executable file
   before first use, and raise a clear error otherwise instead of
   silently exec'ing junk.

``beads.py`` is pure-stdlib (no code_puppy imports), so these run
standalone: ``python3 -m pytest tests/`` or
``python3 tests/test_subprocess_arg_validation.py``.
"""

from __future__ import annotations

import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beads  # noqa: E402

_WINDOWS = sys.platform == "win32"


def _skip_on_windows(reason: str):
    """Mark a test as POSIX-only.

    Under pytest this is a real ``skipif`` (so it reports as skipped,
    not silently passes). pytest is imported lazily so this module
    still runs standalone — ``python tests/test_subprocess_arg_validation.py``
    — without pytest installed; the ``__main__`` runner honors the
    ``_skip_on_windows`` attribute directly.
    """

    def deco(fn):
        fn._skip_on_windows = reason
        try:
            import pytest
        except ImportError:
            return fn
        return pytest.mark.skipif(_WINDOWS, reason=reason)(fn)

    return deco


def _expect_beads_error(fn, *args, **kwargs):
    """Assert ``fn(*args)`` raises BeadsError; return the exception."""
    try:
        fn(*args, **kwargs)
    except beads.BeadsError as exc:
        return exc
    raise AssertionError(f"expected BeadsError from {fn.__name__}")


# --------------------------------------------------------------------------
# _validate_bead_id
# --------------------------------------------------------------------------


def test_valid_ids_pass_through_unchanged():
    for good in ("bead_chain-b2j", "abc123", "a", "x.y-z_1", "ISSUE-42"):
        assert beads._validate_bead_id(good) == good


def test_leading_dash_rejected():
    # The nastiest case: bd would read it as a flag, not an id.
    _expect_beads_error(beads._validate_bead_id, "--force")
    _expect_beads_error(beads._validate_bead_id, "-rf")


def test_whitespace_rejected():
    for bad in ("bead 1", "bead\t1", "bead\n1", " lead", "trail "):
        _expect_beads_error(beads._validate_bead_id, bad)


def test_shell_metacharacters_rejected():
    for bad in ("a;b", "a|b", "a&b", "a$b", "a`b`", "a/b", "a*b", "$(x)"):
        _expect_beads_error(beads._validate_bead_id, bad)


def test_nul_and_empty_rejected():
    _expect_beads_error(beads._validate_bead_id, "")
    _expect_beads_error(beads._validate_bead_id, "a\x00b")


def test_non_string_rejected():
    _expect_beads_error(beads._validate_bead_id, 123)  # type: ignore[arg-type]
    _expect_beads_error(beads._validate_bead_id, None)  # type: ignore[arg-type]


def test_error_message_mentions_offending_value():
    exc = _expect_beads_error(beads._validate_bead_id, "bad id")
    assert "bad id" in str(exc)


# --------------------------------------------------------------------------
# Public entry points reject bad ids BEFORE hitting subprocess
# --------------------------------------------------------------------------


def test_entry_points_reject_bad_ids_without_calling_bd():
    """A bad id must never reach _run_bd — validate first, run never."""
    calls: list[tuple] = []

    def _spy(*a, **k):
        calls.append(a)
        return "{}"

    original = beads._run_bd
    beads._run_bd = _spy  # type: ignore[assignment]
    try:
        for fn in (beads.show, beads.claim, beads.revert_to_open, beads.close):
            _expect_beads_error(fn, "--evil")
        for fn in (beads.next_ready_in_epic, beads.has_open_children):
            _expect_beads_error(fn, "$(rm -rf /)")
        # Read helpers that normally soft-fail must still surface a bad id.
        _expect_beads_error(beads.is_pinned, "-rf")
        _expect_beads_error(beads.open_blocker_ids, "; drop")
        _expect_beads_error(beads.lint_warnings, "a b")
    finally:
        beads._run_bd = original  # type: ignore[assignment]

    assert calls == [], f"bad ids leaked into subprocess: {calls}"


def test_falsy_ids_short_circuit_not_validate():
    """Empty/None ids hit the documented no-op fast path, not validation."""
    assert beads.next_ready_in_epic("") is None
    assert beads.has_open_children("") is False
    assert beads.show("") is None
    assert beads.open_blocker_ids("") == []
    assert beads.is_pinned("") is False


# --------------------------------------------------------------------------
# _bd_bin / _validate_beads_bin
# --------------------------------------------------------------------------


def _clear_beads_bin():
    os.environ.pop("BEADS_BIN", None)


def test_unset_returns_default():
    _clear_beads_bin()
    assert beads._bd_bin() == beads.DEFAULT_BD_BIN


def test_empty_returns_default():
    os.environ["BEADS_BIN"] = ""
    try:
        assert beads._bd_bin() == beads.DEFAULT_BD_BIN
    finally:
        _clear_beads_bin()


def test_absolute_executable_file_resolves(tmp_path):
    fake = tmp_path / "bd-fake"
    fake.write_text("#!/bin/sh\necho hi\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    os.environ["BEADS_BIN"] = str(fake)
    try:
        assert beads._bd_bin() == str(fake)
        assert os.path.isabs(beads._bd_bin())
    finally:
        _clear_beads_bin()


def test_relative_path_resolved_to_absolute(tmp_path):
    fake = tmp_path / "bd-rel"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    os.environ["BEADS_BIN"] = "./bd-rel"
    try:
        resolved = beads._bd_bin()
        assert os.path.isabs(resolved)
        assert resolved == str(fake)
    finally:
        os.chdir(cwd)
        _clear_beads_bin()


def test_bare_name_resolved_via_path(tmp_path):
    # A bare name (no path separator) is resolved via PATH. shutil.which
    # only finds names carrying a PATHEXT extension on Windows, so the
    # on-disk file needs a .bat suffix there; POSIX resolves the
    # extensionless name once the exec bit is set. Either way, BEADS_BIN
    # stays the bare name and _bd_bin() must return the resolved file.
    name = "bd-onpath"
    if _WINDOWS:
        fake = tmp_path / f"{name}.bat"
        fake.write_text("@echo off\n")
    else:
        fake = tmp_path / name
        fake.write_text("#!/bin/sh\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(tmp_path) + os.pathsep + old_path
    os.environ["BEADS_BIN"] = name
    try:
        # normcase folds case + separators on Windows (where paths are
        # case-insensitive and shutil.which echoes the PATHEXT casing,
        # e.g. .BAT) and is a no-op on POSIX.
        assert os.path.normcase(beads._bd_bin()) == os.path.normcase(str(fake))
    finally:
        os.environ["PATH"] = old_path
        _clear_beads_bin()


def test_missing_file_raises(tmp_path):
    os.environ["BEADS_BIN"] = str(tmp_path / "does-not-exist")
    try:
        exc = _expect_beads_error(beads._bd_bin)
        assert "BEADS_BIN" in str(exc)
    finally:
        _clear_beads_bin()


def test_bare_name_not_on_path_raises():
    os.environ["BEADS_BIN"] = "definitely-not-a-real-binary-xyz"
    try:
        exc = _expect_beads_error(beads._bd_bin)
        assert "PATH" in str(exc)
    finally:
        _clear_beads_bin()


def test_directory_rejected(tmp_path):
    os.environ["BEADS_BIN"] = str(tmp_path)
    try:
        exc = _expect_beads_error(beads._bd_bin)
        assert "not a file" in str(exc)
    finally:
        _clear_beads_bin()


def test_non_executable_file_rejected(tmp_path):
    plain = tmp_path / "bd-noexec"
    plain.write_text("data")
    plain.chmod(stat.S_IRUSR | stat.S_IWUSR)  # rw, no x
    os.environ["BEADS_BIN"] = str(plain)
    try:
        exc = _expect_beads_error(beads._bd_bin)
        assert "not executable" in str(exc)
    finally:
        _clear_beads_bin()


# POSIX-only: exec bits / os.access(X_OK) have no meaning on Windows, where
# every existing file reads as executable, so there is no failing case to
# assert. Applied without @-decorator syntax purely as an editor-tooling
# workaround; the behavior is identical to a module-level decorator.
test_non_executable_file_rejected = _skip_on_windows(
    "exec bits / os.access(X_OK) are POSIX-only; Windows treats every "
    "existing file as executable, so there is no 'not executable' case"
)(test_non_executable_file_rejected)


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        skip_reason = getattr(fn, "_skip_on_windows", None)
        if _WINDOWS and skip_reason:
            print(f"SKIP {name}: {skip_reason}")
            continue
        try:
            # crude tmp_path shim for standalone runs
            if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    import pathlib

                    fn(pathlib.Path(d))
            else:
                fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
