"""Regression tests for puppy-viu.1.2 — builtin import-style normalization.

Acceptance criteria proven here:

1. **Convention guard** — no builtin plugin imports its *own* sibling modules
   with an absolute ``code_puppy.plugins.<self>.…`` path. Internal imports must
   be relative so the plugin is relocatable. This test AST-scans every builtin
   and fails if a self-referential absolute import sneaks back in.

2. **Relocation proof** — a builtin copied VERBATIM into a fake user-plugins
   directory loads through the user tier (where it becomes ``user_plugins.<name>``)
   with its relative imports resolving into that namespace — i.e. it imports
   *unmodified* outside the builtin package. Closes liability L1.
"""

import ast
import shutil
import sys
from pathlib import Path

import pytest

import code_puppy.plugins as plugins_pkg
from code_puppy.plugins import _load_user_plugins

BUILTIN_DIR = Path(plugins_pkg.__file__).parent


def _builtin_plugin_names() -> list[str]:
    names = []
    for item in BUILTIN_DIR.iterdir():
        if item.is_dir() and not item.name.startswith(("_", ".")):
            if (item / "register_callbacks.py").exists() or (
                item / "__init__.py"
            ).exists():
                names.append(item.name)
    return sorted(names)


def _self_absolute_imports(py: Path, plugin: str) -> list[str]:
    """Return offending ``from code_puppy.plugins.<self>.…`` import lines."""
    self_pkg = f"code_puppy.plugins.{plugin}"
    self_sub = self_pkg + "."
    tree = ast.parse(py.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == self_pkg or node.module.startswith(self_sub):
                bad.append(f"{py.name}:{node.lineno}: from {node.module} import …")
    return bad


def test_no_builtin_uses_self_referential_absolute_import():
    """Every builtin uses relative imports for its own sibling modules."""
    offenders: list[str] = []
    for plugin in _builtin_plugin_names():
        plugin_dir = BUILTIN_DIR / plugin
        for py in plugin_dir.rglob("*.py"):
            offenders.extend(_self_absolute_imports(py, plugin))

    assert not offenders, (
        "Builtin plugins must import their own modules relatively "
        "(e.g. `from .foo import x`) so they relocate to the user/project "
        "tiers unmodified. Offending absolute self-imports:\n  "
        + "\n  ".join(offenders)
    )


@pytest.fixture
def clean_user_ns():
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("user_plugins"):
            sys.modules.pop(name, None)


# Multi-module builtins with non-trivial internal import chains. shell_safety
# is intentionally excluded: its manifest.py should_load() gate would skip it
# under the default safety level, which is unrelated to import resolution.
_RELOCATION_TARGETS = [
    "context_indicator",  # register_callbacks -> .usage (module scope)
    "frontend_emitter",  # register_callbacks -> .emitter -> .session_context
    "prune",  # register_callbacks -> .prune_menu -> .prune_model/.prune_render
    "force_push_guard",  # register_callbacks -> .detector
    "destructive_command_guard",  # register_callbacks -> .detector
]


def test_builtin_copied_to_user_dir_imports_unmodified(tmp_path, clean_user_ns):
    """Copy builtins verbatim into a user dir; relative imports must resolve."""
    user_dir = tmp_path / "user_plugins"
    user_dir.mkdir()
    for name in _RELOCATION_TARGETS:
        shutil.copytree(BUILTIN_DIR / name, user_dir / name)

    loaded = _load_user_plugins(user_dir, skip_names=set())

    for name in _RELOCATION_TARGETS:
        assert name in loaded, f"{name} failed to load from the user tier"
        mod = sys.modules.get(f"user_plugins.{name}.register_callbacks")
        assert mod is not None, f"user_plugins.{name}.register_callbacks missing"

    # Deep proof: emitter.py's `from .session_context import …` must have
    # resolved a submodule->submodule relative import into the user namespace.
    assert "user_plugins.frontend_emitter.emitter" in sys.modules
    assert "user_plugins.frontend_emitter.session_context" in sys.modules
