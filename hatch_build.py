"""Hatchling build hook: emit the shipped plugin manifest into the wheel (E3.2).

At packaging time this regenerates ``code_puppy/plugins/_shipped_manifest.json``
— a map of every builtin plugin name to a newline-normalized sha256 — and
force-includes it in the build so it ships inside the wheel (and sdist). The
runtime hash-aware sync engine (epic E3) reads this manifest to detect upstream
changes without tripping over CRLF/LF differences (closes L4).

The generator module is loaded **by file path** rather than imported as
``code_puppy.plugins.shipped_manifest`` on purpose: a normal import would run
``code_puppy/plugins/__init__.py`` (and its ``code_puppy.callbacks`` imports),
which need not be importable in a clean build environment. ``shipped_manifest``
is deliberately stdlib-only so this path-load is safe.
"""

import importlib.util
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_ROOT = Path(__file__).resolve().parent
_PLUGINS_DIR = _ROOT / "code_puppy" / "plugins"
_GENERATOR_PATH = _PLUGINS_DIR / "shipped_manifest.py"


def _load_generator():
    """Load shipped_manifest.py by path without importing the code_puppy pkg."""
    spec = importlib.util.spec_from_file_location(
        "_cp_shipped_manifest_gen", _GENERATOR_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Cannot load shipped-manifest generator: {_GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShippedManifestBuildHook(BuildHookInterface):
    """Generate + force-include ``_shipped_manifest.json`` at build time."""

    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        gen = _load_generator()
        out_path = _PLUGINS_DIR / gen.SHIPPED_MANIFEST_FILENAME
        gen.write_shipped_manifest(
            out_path,
            plugins_dir=_PLUGINS_DIR,
            package_version=self.metadata.version,
        )
        rel = f"code_puppy/plugins/{gen.SHIPPED_MANIFEST_FILENAME}"
        build_data.setdefault("force_include", {})[str(out_path)] = rel
        self.app.display_info(f"shipped-manifest: emitted {rel}")
