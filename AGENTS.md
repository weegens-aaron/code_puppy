# Contributing to Code Puppy

> **Golden rule:** nearly all new functionality should be a **plugin** under `code_puppy/plugins/`
> that hooks into core via `code_puppy/callbacks.py`. Don't edit `code_puppy/command_line/`.

## How Plugins Work

Plugins are discovered from three tiers, loaded in order:

| Tier | Location | When to use |
|------|----------|-------------|
| **Builtin** | `code_puppy/plugins/<name>/register_callbacks.py` | Core functionality shipped with Code Puppy |
| **User** | `~/.code_puppy/plugins/<name>/register_callbacks.py` | Personal plugins, applied to every project |
| **Project** | `<CWD>/.code_puppy/plugins/<name>/register_callbacks.py` | Repo-specific plugins, shared with your team via git |

All three tiers use the same pattern — drop a `register_callbacks.py` in a named subdirectory:

```python
from code_puppy.callbacks import register_callback

def _on_startup():
    print("my_feature loaded!")

register_callback("startup", _on_startup)
```

That's it. The plugin loader auto-discovers `register_callbacks.py` in subdirs.

### Internal imports MUST be relative

When a plugin imports its own sibling modules, **always use a relative import**:

```python
# ✅ DO THIS — relocatable
from .detector import detect_force_push
from .config import get_skill_directories
from . import state

# ❌ NOT THIS — pins the plugin to the builtin location
from code_puppy.plugins.force_push_guard.detector import detect_force_push
```

**Why:** a builtin loads as `code_puppy.plugins.<name>`, but the same plugin
dropped into the user/project tiers loads as `user_plugins.<name>` /
`project_plugins.<name>`. An absolute self-import (`code_puppy.plugins.<name>.…`)
hardcodes the builtin namespace, so the plugin breaks the moment it's relocated
(ejected) to another tier. A relative import resolves against whatever parent
package the loader built, so the **exact same files relocate across all three
tiers with zero rewrites** (closes liability L1).

This applies to *internal* imports only. Imports of **core** (`from
code_puppy.messaging import …`) and of a **different** plugin
(`from code_puppy.plugins.other_plugin import …`) stay absolute — relocating
cross-plugin dependencies is a separate concern. A regression test
(`tests/plugins/test_builtin_import_convention.py`) AST-scans every builtin and
fails the build if a self-referential absolute import sneaks back in.

### Project Plugins

Project plugins live at `<CWD>/.code_puppy/plugins/<name>/register_callbacks.py`.
This mirrors the project-level discovery already used by agents (`<CWD>/.code_puppy/agents/`)
and skills (`<CWD>/.code_puppy/skills/`).

**Key details:**

- **Directory must be created intentionally.** Code Puppy will never auto-create
  `.code_puppy/plugins/` — your team opts in by creating it.
- **Load order is builtin → user → project.** Project plugins load last, giving
  them highest precedence for override-style hooks.
- **Project wins on name collision.** If a project plugin shares a name with a
  user plugin, only the project copy loads (the user plugin is skipped). This
  matches how agents deduplicate — `discover_json_agents()` overwrites user
  agents with project agents of the same name. A warning is logged when a
  project plugin shadows a builtin.
- **Module namespace isolation.** Project plugins use `project_plugins.<name>.register_callbacks`
  in `sys.modules`, so they never collide with user plugins at the import level.

### Tier-collision policy (single source of truth)

When the **same plugin name** appears in more than one tier, exactly ONE copy
loads and registers callbacks — the rest are **fully suppressed** (never
imported, never fired). The precedence order is `builtin < user < project`
(highest wins). Every name-clash pair is resolved by
[`code_puppy/plugins/precedence.py`](code_puppy/plugins/precedence.py), which is
the one place that decides winners:

| Collision pair | Winner | Loser (fully suppressed) |
|----------------|--------|--------------------------|
| builtin vs user | user | builtin |
| builtin vs ejected/project | project | builtin |
| user vs project | project | user |
| builtin vs user vs project | project | builtin **and** user |

An *ejected* plugin is a builtin copied out (externalized) to the user/project
tier; the loader treats it as an owned copy, so the same rule applies. The
loader in `plugins/__init__.py` only *applies* `resolve_tier_skips()` — it never
re-derives precedence, so docs and behavior can't drift.

## Available Hooks

`register_callback("<hook>", func)` — deduplicated, async hooks accept sync or async functions.

| Hook | When | Signature |
|------|------|-----------|
| `startup` | App boot | `() -> None` |
| `shutdown` | Graceful exit | `() -> None` |
| `invoke_agent` | Sub-agent invoked | `(*args, **kwargs) -> None` |
| `agent_exception` | Unhandled agent error | `(exception, *args, **kwargs) -> None` |
| `agent_run_start` | Before agent task | `(agent_name, model_name, session_id=None) -> None` |
| `agent_run_end` | After agent run | `(agent_name, model_name, session_id=None, success=True, error=None, response_text=None, metadata=None) -> None` |
| `load_prompt` | System prompt assembly | `() -> str \| None` |
| `run_shell_command` | Before shell exec | `(context, command, cwd=None, timeout=60) -> dict \| None` (return `{"blocked": True}` to block) |
| `file_permission` | Before file op | `(context, file_path, operation, ...) -> bool` |
| `pre_tool_call` | Before tool executes | `(tool_name, tool_args, context=None) -> Any` |
| `post_tool_call` | After tool finishes | `(tool_name, tool_args, result, duration_ms, context=None) -> Any` |
| `custom_command` | Unknown `/slash` cmd | `(command, name) -> True \| str \| None` |
| `custom_command_help` | `/help` menu | `() -> list[tuple[str, str]]` |
| `register_tools` | Tool registration | `() -> list[dict]` with `{"name": str, "register_func": callable}` |
| `register_agent_tools` | Advertise tools to an agent's available list | `(agent_name: str \| None) -> list[str]` — tool names from `TOOL_REGISTRY` to merge into the agent's hardcoded `get_available_tools()` |
| `register_agents` | Agent catalogue | `() -> list[dict]` with `{"name": str, "class": type}` |
| `register_model_type` | Custom model type | `() -> list[dict]` with `{"type": str, "handler": callable}` |
| `register_skills` | Skill catalogue | `() -> list[dict]` with `{"name": str, "skill_md" \| "skill_md_path" \| "frontmatter"+"body"}` |
| `load_model_config` | Patch model config | `(*args, **kwargs) -> Any` |
| `load_models_config` | Inject models | `() -> dict` |
| `load_model_descriptions` | Inject description overlays | `() -> dict[str, str]` |
| `get_model_system_prompt` | Per-model prompt | `(model_name, default_prompt, user_prompt) -> dict \| None` |
| `stream_event` | Response streaming | `(event_type, event_data, agent_session_id=None) -> None` |
| `pre_mcp_autostart` | Before bound MCP servers auto-start | `(agent_name, server_names) -> None` (refresh tokens / mint creds here) |

Full list + rarely-used hooks: see `code_puppy/callbacks.py` source.

## Rules

1. **Plugins over core** — if a hook exists for it, use it
2. **One `register_callbacks.py` per plugin** — register at module scope
3. **600-line hard cap** — split into submodules
4. **Fail gracefully** — never crash the app
5. **Return `None` from commands you don't own**
6. **Always run linters - `ruff check --fix`, `ruff format .`
7. **NEVER ALLOW A CLAUDE CO-AUTHOR COMMIT**

