"""Build-time shipped-manifest generation + newline-normalized hashing (E3.2).

This module is the **single source of the content-hash primitive** used by the
scoped hash-aware sync engine (epic E3). At packaging time the build hook
(``hatch_build.py``) emits ``_shipped_manifest.json`` next to this file; it maps
every builtin plugin name to a ``sha256`` taken over **newline-normalized**
content. The future runtime sync (E3.1 / E3.3) imports the very same
:func:`compute_plugin_hash` to compute the CUR hash, so a wheel built on Linux
(LF) and a checkout edited on Windows (CRLF) produce **identical** hashes for
identical content — closing liability **L4** (Windows CRLF false-conflicts).

Design constraint — *stdlib only*
---------------------------------
The build hook loads this file **by path** (``spec_from_file_location``) so it
never triggers ``code_puppy/plugins/__init__.py`` import side effects (which
pull in ``code_puppy.callbacks`` and friends that may be unavailable in a clean
build environment). To keep that path-load safe, this module must import
**nothing from** ``code_puppy`` — only the standard library. Please keep it
that way.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterator

__all__ = [
    "SHIPPED_MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "HASH_ALGORITHM",
    "normalize_newlines",
    "compute_content_hash",
    "compute_plugin_hash",
    "iter_builtin_plugin_dirs",
    "generate_shipped_manifest",
    "write_shipped_manifest",
    "load_shipped_manifest",
]

# The manifest file ships inside the package, beside this module, so it is
# importlib-readable at runtime from any installed wheel.
SHIPPED_MANIFEST_FILENAME = "_shipped_manifest.json"

# Bump when the manifest schema changes in a backward-incompatible way.
MANIFEST_VERSION = 1

# Identifies the hashing scheme so a future change is detectable from the data.
# "sha256-nl" == sha256 over newline-normalized bytes (CRLF/CR collapsed to LF).
HASH_ALGORITHM = "sha256-nl"

# Files/dirs that never contribute to a plugin's content hash. ``__pycache__``
# and compiled bytecode are build noise; the manifest itself is an output.
_IGNORED_DIR_NAMES = {"__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd"}
_IGNORED_FILE_NAMES = {SHIPPED_MANIFEST_FILENAME, ".DS_Store"}


def normalize_newlines(data: bytes) -> bytes:
    """Collapse Windows (CRLF) and classic-Mac (CR) line endings to Unix (LF).

    Operates on raw bytes so it is encoding-agnostic. This is the heart of the
    L4 mitigation: the same logical content hashes identically regardless of
    the checkout's line-ending convention.
    """
    # Order matters: handle CRLF first so the lone-CR pass doesn't double-count.
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def compute_content_hash(data: bytes) -> str:
    """Return the newline-normalized sha256 hexdigest of *data*."""
    return hashlib.sha256(normalize_newlines(data)).hexdigest()


def iter_builtin_plugin_dirs(plugins_dir: Path) -> list[Path]:
    """Return the builtin plugin directories under *plugins_dir*, sorted.

    A directory counts as a plugin if it has a ``register_callbacks.py`` or an
    ``__init__.py`` entry point (mirrors ``_scan_plugin_names`` in the loader).
    Names starting with ``_`` or ``.`` are skipped (``__pycache__``, etc.).
    """
    if not plugins_dir.is_dir():
        return []
    found: list[Path] = []
    for item in sorted(plugins_dir.iterdir(), key=lambda p: p.name):
        if not item.is_dir():
            continue
        if item.name.startswith("_") or item.name.startswith("."):
            continue
        if (item / "register_callbacks.py").exists() or (item / "__init__.py").exists():
            found.append(item)
    return found


def _iter_plugin_files(plugin_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(relative_posix_path, file_path)`` for every hashable file.

    Sorted by relative POSIX path for cross-platform determinism — the hash
    must not depend on filesystem iteration order or path separators.
    """
    files: list[tuple[str, Path]] = []
    for path in plugin_dir.rglob("*"):
        if not path.is_file():
            continue
        if any(
            part in _IGNORED_DIR_NAMES for part in path.relative_to(plugin_dir).parts
        ):
            continue
        if path.suffix in _IGNORED_SUFFIXES or path.name in _IGNORED_FILE_NAMES:
            continue
        rel = path.relative_to(plugin_dir).as_posix()
        files.append((rel, path))
    files.sort(key=lambda pair: pair[0])
    yield from files


def compute_plugin_hash(plugin_dir: Path) -> str:
    """Return a single newline-normalized sha256 over a whole plugin directory.

    The digest folds in each file's relative POSIX path **and** its normalized
    content, so it is sensitive to renames, additions, and deletions as well as
    edits — yet stable across CRLF/LF and across operating systems.
    """
    h = hashlib.sha256()
    for rel, path in _iter_plugin_files(plugin_dir):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(normalize_newlines(path.read_bytes()))
        h.update(b"\0")
    return h.hexdigest()


def generate_shipped_manifest(
    plugins_dir: Path,
    package_version: str | None = None,
) -> dict:
    """Build the shipped-manifest dict for the builtin plugins in *plugins_dir*.

    Returns a JSON-serializable dict::

        {
          "manifest_version": 1,
          "algorithm": "sha256-nl",
          "package_version": "0.0.573" | None,
          "plugins": {"<name>": "<hexdigest>", ...}
        }

    ``package_version`` lets the runtime sync take a fast-path (skip work when
    the installed wheel version is unchanged — see epic E3 / bead E3.3).
    """
    plugins = {
        plugin_dir.name: compute_plugin_hash(plugin_dir)
        for plugin_dir in iter_builtin_plugin_dirs(plugins_dir)
    }
    return {
        "manifest_version": MANIFEST_VERSION,
        "algorithm": HASH_ALGORITHM,
        "package_version": package_version,
        "plugins": plugins,
    }


def write_shipped_manifest(
    output_path: Path,
    plugins_dir: Path | None = None,
    package_version: str | None = None,
) -> Path:
    """Generate the manifest and write it to *output_path* as pretty JSON.

    *plugins_dir* defaults to this module's own directory (the builtin plugins
    package). Returns the path written. The trailing newline keeps the file
    diff-friendly and POSIX-text-clean.
    """
    if plugins_dir is None:
        plugins_dir = Path(__file__).resolve().parent
    manifest = generate_shipped_manifest(plugins_dir, package_version=package_version)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def load_shipped_manifest(plugins_dir: Path | None = None) -> dict | None:
    """Read the shipped manifest from *plugins_dir* (default: this package).

    Returns the parsed dict, or ``None`` when the manifest is absent (e.g. a
    bare source checkout that was never packaged) — callers must treat a
    missing manifest as "nothing to sync", never as an error.
    """
    if plugins_dir is None:
        plugins_dir = Path(__file__).resolve().parent
    manifest_path = Path(plugins_dir) / SHIPPED_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _detect_package_version() -> str | None:
    """Best-effort package version for standalone CLI regeneration."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("code-puppy")
        except PackageNotFoundError:
            return None
    except Exception:
        return None


if __name__ == "__main__":  # pragma: no cover - manual regeneration entrypoint
    here = Path(__file__).resolve().parent
    written = write_shipped_manifest(
        here / SHIPPED_MANIFEST_FILENAME,
        plugins_dir=here,
        package_version=_detect_package_version(),
    )
    print(f"Wrote shipped manifest: {written}")
