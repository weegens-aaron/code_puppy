"""Tests for the build-time shipped-manifest generator (E3.2).

Core acceptance: hashes are newline-normalized so identical content matches
across CRLF/LF (closes L4 — Windows CRLF false-conflicts).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from code_puppy.plugins import shipped_manifest as sm

_PLUGIN_SRC = (
    "from code_puppy.callbacks import register_callback\n"
    "\n"
    "def _on_startup():\n"
    "    return None\n"
    "\n"
    'register_callback("startup", _on_startup)\n'
)


def _make_plugin(plugins_dir: Path, name: str, *, src: str = _PLUGIN_SRC) -> Path:
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    # newline="" disables OS newline translation so the bytes on disk match
    # *src* exactly — critical for the CRLF-vs-LF equality test on Windows,
    # where write_text would otherwise rewrite every \n to \r\n.
    (plugin_dir / "register_callbacks.py").write_text(src, newline="")
    return plugin_dir


# ---------------------------------------------------------------------------
# normalize_newlines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"a\r\nb", b"a\nb"),  # CRLF -> LF
        (b"a\rb", b"a\nb"),  # lone CR -> LF
        (b"a\nb", b"a\nb"),  # LF unchanged
        (b"a\r\n\r\nb", b"a\n\nb"),  # double CRLF
        (b"mixed\r\nand\rlone\n", b"mixed\nand\nlone\n"),
    ],
)
def test_normalize_newlines(raw, expected):
    assert sm.normalize_newlines(raw) == expected


def test_content_hash_identical_across_line_endings():
    lf = b"line1\nline2\nline3\n"
    crlf = b"line1\r\nline2\r\nline3\r\n"
    cr = b"line1\rline2\rline3\r"
    assert sm.compute_content_hash(lf) == sm.compute_content_hash(crlf)
    assert sm.compute_content_hash(lf) == sm.compute_content_hash(cr)


# ---------------------------------------------------------------------------
# compute_plugin_hash — the L4 mitigation in directory form
# ---------------------------------------------------------------------------


def test_plugin_hash_identical_across_crlf_and_lf(tmp_path):
    """Same content with different newlines -> identical plugin hash."""
    lf_dir = tmp_path / "lf"
    crlf_dir = tmp_path / "crlf"
    body = "import os\n\n\ndef hook():\n    return 1\n"
    _make_plugin(lf_dir, "p", src=body)
    _make_plugin(crlf_dir, "p", src=body.replace("\n", "\r\n"))

    assert sm.compute_plugin_hash(lf_dir / "p") == sm.compute_plugin_hash(
        crlf_dir / "p"
    )


def test_plugin_hash_changes_on_content_change(tmp_path):
    a = _make_plugin(tmp_path / "a", "p", src="x = 1\n")
    b = _make_plugin(tmp_path / "b", "p", src="x = 2\n")
    assert sm.compute_plugin_hash(a) != sm.compute_plugin_hash(b)


def test_plugin_hash_sensitive_to_rename(tmp_path):
    """Moving content to a differently named file changes the hash."""
    a = tmp_path / "a" / "p"
    a.mkdir(parents=True)
    (a / "register_callbacks.py").write_text("y = 1\n")
    b = tmp_path / "b" / "p"
    b.mkdir(parents=True)
    (b / "register_callbacks.py").write_text("")
    (b / "renamed.py").write_text("y = 1\n")
    assert sm.compute_plugin_hash(a) != sm.compute_plugin_hash(b)


def test_plugin_hash_ignores_pycache_and_pyc(tmp_path):
    p = _make_plugin(tmp_path / "root", "p", src="z = 1\n")
    before = sm.compute_plugin_hash(p)
    cache = p / "__pycache__"
    cache.mkdir()
    (cache / "register_callbacks.cpython-311.pyc").write_bytes(b"\x00\x01compiled")
    (p / "stale.pyc").write_bytes(b"\x00bytecode")
    assert sm.compute_plugin_hash(p) == before


def test_plugin_hash_recurses_into_subdirs(tmp_path):
    p = _make_plugin(tmp_path / "root", "p", src="a = 1\n")
    before = sm.compute_plugin_hash(p)
    sub = p / "sub"
    sub.mkdir()
    (sub / "extra.py").write_text("b = 2\n")
    assert sm.compute_plugin_hash(p) != before


# ---------------------------------------------------------------------------
# generate / write / load
# ---------------------------------------------------------------------------


def test_generate_manifest_structure(tmp_path):
    _make_plugin(tmp_path, "alpha")
    _make_plugin(tmp_path, "beta")
    # Not a plugin: no entry point -> excluded.
    (tmp_path / "not_a_plugin").mkdir()
    (tmp_path / "not_a_plugin" / "readme.txt").write_text("hi\n")
    # Underscore/dot dirs excluded.
    (tmp_path / "_private").mkdir()

    manifest = sm.generate_shipped_manifest(tmp_path, package_version="9.9.9")

    assert manifest["manifest_version"] == sm.MANIFEST_VERSION
    assert manifest["algorithm"] == sm.HASH_ALGORITHM == "sha256-nl"
    assert manifest["package_version"] == "9.9.9"
    assert set(manifest["plugins"]) == {"alpha", "beta"}
    assert all(
        isinstance(h, str) and len(h) == 64 for h in manifest["plugins"].values()
    )


def test_write_and_load_round_trip(tmp_path):
    _make_plugin(tmp_path, "alpha")
    out = tmp_path / sm.SHIPPED_MANIFEST_FILENAME
    written = sm.write_shipped_manifest(
        out, plugins_dir=tmp_path, package_version="1.0"
    )
    assert written == out
    assert out.read_text(encoding="utf-8").endswith("\n")

    loaded = sm.load_shipped_manifest(tmp_path)
    assert loaded == json.loads(out.read_text(encoding="utf-8"))
    assert loaded["plugins"]["alpha"] == sm.compute_plugin_hash(tmp_path / "alpha")


def test_load_missing_manifest_returns_none(tmp_path):
    assert sm.load_shipped_manifest(tmp_path) is None


def test_load_corrupt_manifest_returns_none(tmp_path):
    (tmp_path / sm.SHIPPED_MANIFEST_FILENAME).write_text("{ not json")
    assert sm.load_shipped_manifest(tmp_path) is None


def test_generated_manifest_excludes_itself(tmp_path):
    """A manifest sitting in plugins_dir must not be hashed as a plugin."""
    _make_plugin(tmp_path, "alpha")
    sm.write_shipped_manifest(
        tmp_path / sm.SHIPPED_MANIFEST_FILENAME, plugins_dir=tmp_path
    )
    # Regenerate; the manifest file (top-level, underscore-prefixed) is ignored.
    manifest = sm.generate_shipped_manifest(tmp_path)
    assert set(manifest["plugins"]) == {"alpha"}


# ---------------------------------------------------------------------------
# Build-time isolation invariant: the generator is path-loadable without
# importing the code_puppy package (so the hatch build hook stays safe).
# ---------------------------------------------------------------------------


def test_generator_is_loadable_by_path_without_package_import():
    module_path = Path(sm.__file__)
    # Drop any pre-imported copies so we observe a clean path-load.
    sentinel = "_cp_shipped_manifest_isolation_probe"
    sys.modules.pop(sentinel, None)

    spec = importlib.util.spec_from_file_location(sentinel, module_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        # The path-loaded module must expose the same primitives the hook uses.
        assert hasattr(module, "write_shipped_manifest")
        assert hasattr(module, "SHIPPED_MANIFEST_FILENAME")
        # And it must not depend on code_puppy internals to function.
        assert module.normalize_newlines(b"a\r\nb") == b"a\nb"
    finally:
        sys.modules.pop(sentinel, None)
