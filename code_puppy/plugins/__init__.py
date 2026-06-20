import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path

from code_puppy.callbacks import clear_loading_context, set_loading_context

logger = logging.getLogger(__name__)

# User plugins directory
USER_PLUGINS_DIR = Path.home() / ".code_puppy" / "plugins"

# Name of the optional per-plugin manifest module that may declare a load
# predicate. A plugin opts out of unconditional loading by shipping a
# ``manifest.py`` exporting ``should_load() -> bool`` (see _plugin_should_load).
_PLUGIN_MANIFEST_MODULE = "manifest"

# Track if plugins have already been loaded to prevent duplicate registration
_PLUGINS_LOADED = False

# Stores the loaded plugin names by tier after the first load_plugin_callbacks() call.
# Populated once, then read by get_loaded_plugins().
_loaded_plugin_names: dict[str, list[str]] = {"builtin": [], "user": [], "project": []}


def _plugin_should_load(plugin_dir: Path, plugin_name: str) -> bool:
    """Consult a plugin's declarative load predicate, if it ships one.

    A plugin opts out of *unconditional* loading by shipping a ``manifest.py``
    that exports ``should_load() -> bool``. The loader imports that manifest in
    isolation (it must only *declare* the predicate, never register callbacks)
    and skips the plugin when the predicate returns ``False``.

    This replaces the old hardcoded ``if plugin_name == "shell_safety"`` branch:
    the conditional-load gate now travels *with* the plugin, so it works
    identically across the builtin, user, and project tiers and survives a
    plugin being relocated (externalized) out of the wheel.

    Contract:
        * No ``manifest.py`` -> load (default ``True``).
        * ``manifest.py`` without ``should_load`` -> load (default ``True``).
        * Predicate raises -> load (fail-open) and log a warning; a broken
          gate should never silently suppress a plugin.
    """
    manifest_file = plugin_dir / f"{_PLUGIN_MANIFEST_MODULE}.py"
    if not manifest_file.exists():
        return True

    try:
        # Load the manifest under a throwaway namespace so it never collides
        # with the plugin's real modules and is not retained in sys.modules.
        spec = importlib.util.spec_from_file_location(
            f"_code_puppy_plugin_manifests.{plugin_name}", manifest_file
        )
        if spec is None or spec.loader is None:
            return True
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        logger.warning(
            "Load predicate for plugin '%s' failed to import (%s); loading anyway",
            plugin_name,
            e,
        )
        return True

    predicate = getattr(module, "should_load", None)
    if predicate is None:
        return True

    try:
        if not predicate():
            logger.debug(
                "Skipping plugin '%s' - its should_load() predicate returned False",
                plugin_name,
            )
            return False
        return True
    except Exception as e:
        logger.warning(
            "should_load() for plugin '%s' raised (%s); loading anyway",
            plugin_name,
            e,
        )
        return True


def _load_builtin_plugins(
    plugins_dir: Path,
    skip_names: set[str] | None = None,
) -> list[str]:
    """Load built-in plugins from the package plugins directory.

    *skip_names*, when provided, is the set of plugin names that an *owned*
    (user- or project-tier) copy has already claimed.  A builtin whose name
    appears in this set is FULLY SUPPRESSED — it is never imported, so it
    cannot register callbacks.  This implements deterministic precedence
    (project > user > builtin): an ejected/owned copy wins over the builtin
    of the same name, and only one copy ever fires.  Previously both copies
    loaded and fired (warn-only collision handling); see puppy-viu.2.1.

    Returns list of successfully loaded plugin names.
    """
    loaded = []
    skip_names = set(skip_names or ())

    for item in plugins_dir.iterdir():
        if item.is_dir() and not item.name.startswith("_"):
            plugin_name = item.name
            callbacks_file = item / "register_callbacks.py"

            if callbacks_file.exists():
                # An owned (user/project) copy of this name suppresses the
                # builtin entirely — do not import it (deterministic
                # precedence; the owned copy is the single registrant).
                if plugin_name in skip_names:
                    logger.info(
                        "Suppressing builtin plugin '%s' because an owned "
                        "(user/project) copy of the same name takes precedence",
                        plugin_name,
                    )
                    continue

                # Honor the plugin's declarative load predicate (if any).
                if not _plugin_should_load(item, plugin_name):
                    continue

                try:
                    module_name = f"code_puppy.plugins.{plugin_name}.register_callbacks"
                    set_loading_context(plugin_name)
                    importlib.import_module(module_name)
                    loaded.append(plugin_name)
                except ImportError as e:
                    logger.warning(
                        f"Failed to import callbacks from built-in plugin {plugin_name}: {e}"
                    )
                except Exception as e:
                    logger.error(
                        f"Unexpected error loading built-in plugin {plugin_name}: {e}"
                    )
                finally:
                    clear_loading_context()

    return loaded


def _scan_plugin_names(plugins_dir: Path) -> set[str]:
    """Return the set of plugin directory names under *plugins_dir*.

    Only performs a cheap filesystem scan — nothing is imported.  Used to
    pre-detect project plugin names so that ``_load_user_plugins`` can
    skip names that the project tier will supersede (project wins on
    collision, matching the agents dedup strategy).
    """
    names: set[str] = set()
    if not plugins_dir.is_dir():
        return names
    for item in plugins_dir.iterdir():
        if (
            item.is_dir()
            and not item.name.startswith("_")
            and not item.name.startswith(".")
        ):
            # Only count it if it actually has a loadable entry point
            if (item / "register_callbacks.py").exists() or (
                item / "__init__.py"
            ).exists():
                names.add(item.name)
    return names


def _load_user_plugins(
    user_plugins_dir: Path,
    skip_names: set[str] | None = None,
) -> list[str]:
    """Load user plugins from ~/.code_puppy/plugins/.

    Each plugin should be a directory containing a register_callbacks.py file.
    Plugins are loaded by adding their parent to sys.path and importing them.

    *skip_names*, when provided, is a set of plugin names that will be loaded
    from a higher-precedence tier (project plugins).  User plugins whose name
    appears in this set are skipped so that only one copy registers callbacks
    (matching the agents dedup strategy).

    Returns list of successfully loaded plugin names.
    """
    loaded = []
    skip_names = set(skip_names or ())

    if not user_plugins_dir.exists():
        return loaded

    if not user_plugins_dir.is_dir():
        logger.warning(f"User plugins path is not a directory: {user_plugins_dir}")
        return loaded

    # Add user plugins directory to sys.path if not already there
    user_plugins_str = str(user_plugins_dir)
    if user_plugins_str not in sys.path:
        sys.path.insert(0, user_plugins_str)

    for item in user_plugins_dir.iterdir():
        if (
            item.is_dir()
            and not item.name.startswith("_")
            and not item.name.startswith(".")
        ):
            plugin_name = item.name

            if plugin_name in skip_names:
                logger.info(
                    "Skipping user plugin '%s' because a higher-precedence "
                    "plugin with the same name is already loaded or scheduled",
                    plugin_name,
                )
                continue

            # Honor the plugin's declarative load predicate (if any).
            if not _plugin_should_load(item, plugin_name):
                continue

            callbacks_file = item / "register_callbacks.py"

            if callbacks_file.exists():
                try:
                    # Register parent package so relative imports resolve
                    # (shared with the project tier — tiers load identically).
                    _ensure_plugin_package(_USER_PLUGINS_NS, item, plugin_name)

                    module_name = f"{_USER_PLUGINS_NS}.{plugin_name}.register_callbacks"
                    spec = importlib.util.spec_from_file_location(
                        module_name, callbacks_file
                    )
                    if spec is None or spec.loader is None:
                        logger.warning(
                            f"Could not create module spec for user plugin: {plugin_name}"
                        )
                        continue

                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module

                    set_loading_context(plugin_name)
                    try:
                        spec.loader.exec_module(module)
                    finally:
                        clear_loading_context()
                    loaded.append(plugin_name)

                except ImportError as e:
                    logger.warning(
                        f"Failed to import callbacks from user plugin {plugin_name}: {e}"
                    )
                except Exception as e:
                    logger.error(
                        f"Unexpected error loading user plugin {plugin_name}: {e}",
                        exc_info=True,
                    )
            else:
                # Check if there's an __init__.py - might be a simple plugin
                init_file = item / "__init__.py"
                if init_file.exists():
                    try:
                        set_loading_context(plugin_name)
                        try:
                            loaded_ok = _ensure_plugin_package(
                                _USER_PLUGINS_NS, item, plugin_name
                            )
                        finally:
                            clear_loading_context()
                        if loaded_ok:
                            loaded.append(plugin_name)

                    except Exception as e:
                        logger.error(
                            f"Unexpected error loading user plugin {plugin_name}: {e}",
                            exc_info=True,
                        )

    return loaded


# Synthetic top-level namespace packages, one per non-builtin tier.  They give
# user/project plugins a real parent package so relative imports resolve.  The
# builtin tier needs no synthetic namespace — it already lives under the real
# ``code_puppy.plugins`` package.
_USER_PLUGINS_NS = "user_plugins"
_PROJECT_PLUGINS_NS = "project_plugins"


def _ensure_plugin_package(namespace: str, plugin_dir: Path, plugin_name: str) -> bool:
    """Build the synthetic parent package(s) for a tier plugin.

    Ensures two things exist in ``sys.modules`` so that a plugin's
    ``register_callbacks.py`` can use relative imports (``from . import x``):

    1. The top-level tier namespace package (*namespace*, e.g.
       ``"user_plugins"`` or ``"project_plugins"``) — a synthetic namespace
       package with an empty ``__path__``.
    2. The plugin's own parent package (``namespace.plugin_name``) whose
       ``__path__`` points at *plugin_dir* so sibling modules resolve.

    If the plugin ships an ``__init__.py`` it is executed so package-level
    attributes (``__version__``, etc.) are available; otherwise a bare
    namespace module is created — enough for the import machinery to locate
    sibling modules.

    This single helper is shared by BOTH the user and project tiers, honoring
    the AGENTS.md promise that all tiers load identically.  It closes L1: user
    plugins previously had *no* parent package, so ``from . import x`` raised
    ``ModuleNotFoundError``.

    Returns ``True`` if a real ``__init__.py`` was executed (or the package was
    already present), ``False`` if a bare namespace fallback was used (no init,
    or spec/loader was ``None``).
    """
    # 1. Top-level tier namespace package (created once per tier).
    if namespace not in sys.modules:
        ns_pkg = types.ModuleType(namespace)
        ns_pkg.__path__ = []  # namespace package
        ns_pkg.__package__ = namespace
        sys.modules[namespace] = ns_pkg

    # 2. The plugin's own parent package.
    pkg_name = f"{namespace}.{plugin_name}"
    if pkg_name in sys.modules:
        return True

    init_file = plugin_dir / "__init__.py"
    if init_file.exists():
        spec_init = importlib.util.spec_from_file_location(
            pkg_name,
            init_file,
            submodule_search_locations=[str(plugin_dir)],
        )
        if spec_init is None or spec_init.loader is None:
            # Fallback: bare namespace (init exists but can't be loaded)
            pkg_mod = types.ModuleType(pkg_name)
            pkg_mod.__path__ = [str(plugin_dir)]
            pkg_mod.__package__ = pkg_name
            sys.modules[pkg_name] = pkg_mod
            return False

        pkg_mod = importlib.util.module_from_spec(spec_init)
        sys.modules[pkg_name] = pkg_mod
        spec_init.loader.exec_module(pkg_mod)
        return True
    else:
        pkg_mod = types.ModuleType(pkg_name)
        pkg_mod.__path__ = [str(plugin_dir)]
        pkg_mod.__package__ = pkg_name
        sys.modules[pkg_name] = pkg_mod
        return False


def _load_project_plugins(
    project_plugins_dir: Path,
    builtin_names: set[str],
    user_names: set[str],
) -> list[str]:
    """Load project plugins from <CWD>/.code_puppy/plugins/.

    Mirrors _load_user_plugins() but uses a ``project_plugins.`` sys.modules
    namespace and warns on name collisions with builtin or user plugins.

    Before loading each plugin's ``register_callbacks.py``, a synthetic
    parent package is registered in ``sys.modules`` so that relative
    imports (``from . import state``, ``from .utils import …``) resolve
    correctly.

    Returns list of successfully loaded plugin names.
    """
    loaded = []

    if not project_plugins_dir.exists():
        return loaded

    if not project_plugins_dir.is_dir():
        logger.warning(
            f"Project plugins path is not a directory: {project_plugins_dir}"
        )
        return loaded

    project_plugins_str = str(project_plugins_dir)
    if project_plugins_str not in sys.path:
        sys.path.insert(0, project_plugins_str)

    for item in project_plugins_dir.iterdir():
        if (
            item.is_dir()
            and not item.name.startswith("_")
            and not item.name.startswith(".")
        ):
            plugin_name = item.name

            # Warn if a project plugin shadows a builtin. The builtin is
            # already fully suppressed upstream (it was passed into
            # _load_builtin_plugins' skip_names), so only this owned copy
            # registers — the warning is purely informational.
            if plugin_name in builtin_names:
                logger.warning(
                    f"Project plugin '{plugin_name}' shadows builtin plugin "
                    "of the same name (builtin suppressed)"
                )

            # Honor the plugin's declarative load predicate (if any).
            if not _plugin_should_load(item, plugin_name):
                continue

            callbacks_file = item / "register_callbacks.py"

            if callbacks_file.exists():
                try:
                    # Register parent package so relative imports resolve
                    _ensure_plugin_package(_PROJECT_PLUGINS_NS, item, plugin_name)

                    module_name = (
                        f"{_PROJECT_PLUGINS_NS}.{plugin_name}.register_callbacks"
                    )
                    spec = importlib.util.spec_from_file_location(
                        module_name, callbacks_file
                    )
                    if spec is None or spec.loader is None:
                        logger.warning(
                            f"Could not create module spec for project plugin: {plugin_name}"
                        )
                        continue

                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    set_loading_context(plugin_name)
                    try:
                        spec.loader.exec_module(module)
                    finally:
                        clear_loading_context()
                    loaded.append(plugin_name)

                except ImportError as e:
                    logger.warning(
                        f"Failed to import callbacks from project plugin {plugin_name}: {e}"
                    )
                except Exception as e:
                    logger.error(
                        f"Unexpected error loading project plugin {plugin_name}: {e}",
                        exc_info=True,
                    )
            else:
                # Fallback to __init__.py (mirrors user plugin behavior)
                init_file = item / "__init__.py"
                if init_file.exists():
                    try:
                        set_loading_context(plugin_name)
                        try:
                            loaded_ok = _ensure_plugin_package(
                                _PROJECT_PLUGINS_NS, item, plugin_name
                            )
                        finally:
                            clear_loading_context()
                        if loaded_ok:
                            loaded.append(plugin_name)
                        else:
                            logger.warning(
                                f"Could not load __init__.py for project plugin: {plugin_name}"
                            )

                    except Exception as e:
                        logger.error(
                            f"Unexpected error loading project plugin {plugin_name}: {e}",
                            exc_info=True,
                        )

    return loaded


def get_project_plugins_directory() -> Path | None:
    """Get the project-local plugins directory path.

    Looks for a .code_puppy/plugins/ directory in the current working directory.
    Does NOT create the directory if it doesn't exist — the team must create it
    intentionally.

    Returns:
        Path to the project's plugins directory if it exists, or None.
    """
    project_plugins_dir = Path.cwd() / ".code_puppy" / "plugins"
    if project_plugins_dir.is_dir():
        return project_plugins_dir
    return None


def load_plugin_callbacks() -> dict[str, list[str]]:
    """Dynamically load register_callbacks.py from all plugin sources.

    Loads plugins from:
    1. Built-in plugins in the code_puppy/plugins/ directory
    2. User plugins in ~/.code_puppy/plugins/
    3. Project plugins in <CWD>/.code_puppy/plugins/

    Returns dict with 'builtin', 'user', and 'project' keys containing
    lists of loaded plugin names.

    NOTE: This function is idempotent - calling it multiple times will only
    load plugins once. Subsequent calls return empty lists.
    """
    global _PLUGINS_LOADED

    # Prevent duplicate loading - plugins register callbacks at import time,
    # so re-importing would cause duplicate registrations
    if _PLUGINS_LOADED:
        logger.debug("Plugins already loaded, skipping duplicate load")
        return {"builtin": [], "user": [], "project": []}

    plugins_dir = Path(__file__).parent

    # Deterministic precedence: project > user > builtin.
    #
    # Pre-scan the *owned* tiers (user + project) before loading anything so
    # that an owned copy FULLY SUPPRESSES the same-named builtin — the builtin
    # never imports and never fires. Project still wins over user (the user
    # tier skips any name the project tier will supersede), matching the
    # agents dedup strategy. See puppy-viu.2.1.
    project_plugins_dir = get_project_plugins_directory()
    project_plugin_names = (
        _scan_plugin_names(project_plugins_dir)
        if project_plugins_dir is not None
        else set()
    )
    user_plugin_names = _scan_plugin_names(USER_PLUGINS_DIR)

    # Any owned (user or project) copy claims the name away from the builtin,
    # which is then suppressed (and logged) inside _load_builtin_plugins.
    owned_names = user_plugin_names | project_plugin_names

    builtin_loaded = _load_builtin_plugins(plugins_dir, skip_names=owned_names)
    # User skips only names the project tier will supersede (project wins).
    # It no longer skips builtin names: an owned copy now beats the builtin,
    # which has already been suppressed above.
    user_loaded = _load_user_plugins(USER_PLUGINS_DIR, skip_names=project_plugin_names)

    # Load project plugins last (highest precedence)
    project_loaded = []
    if project_plugins_dir is not None:
        logger.info(f"Loading project plugins from {project_plugins_dir}")
        project_loaded = _load_project_plugins(
            project_plugins_dir,
            builtin_names=set(builtin_loaded),
            user_names=set(user_loaded),
        )

    result = {
        "builtin": builtin_loaded,
        "user": user_loaded,
        "project": project_loaded,
    }

    _PLUGINS_LOADED = True
    _loaded_plugin_names.update(result)
    logger.debug(
        f"Loaded plugins: builtin={result['builtin']}, "
        f"user={result['user']}, project={result['project']}"
    )

    return result


def get_loaded_plugins() -> dict[str, list[str]]:
    """Return the loaded plugin names grouped by tier.

    Returns a dict with 'builtin', 'user', and 'project' keys, each
    containing a list of plugin names loaded during startup.  Safe to
    call at any time — returns empty lists before plugins are loaded.
    """
    return dict(_loaded_plugin_names)


def get_user_plugins_dir() -> Path:
    """Return the path to the user plugins directory."""
    return USER_PLUGINS_DIR


def ensure_user_plugins_dir() -> Path:
    """Create the user plugins directory if it doesn't exist.

    Returns the path to the directory.
    """
    USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    return USER_PLUGINS_DIR
