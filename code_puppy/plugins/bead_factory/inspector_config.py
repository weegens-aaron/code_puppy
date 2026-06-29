"""Persisted configuration for build-mode LLM inspectors.

Each inspector is a (name, model, prompt, enabled) tuple. Inspectors live in a
JSON file at ``$XDG_DATA_HOME/code_puppy/inspectors.json`` so users can
configure multiple verifiers -- for example, one inspector that checks tests
pass, another that checks docs are updated, etc. The build loop fans these out
in parallel and only declares success when *every* enabled inspector reports no
remediation notes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field

from code_puppy.config import DATA_DIR

logger = logging.getLogger(__name__)

INSPECTORS_FILE = os.path.join(DATA_DIR, "inspectors.json")

_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


DEFAULT_INSPECTOR_PROMPT = """\
You are Code Puppy's build-completion inspector.

Decide whether the user's build is verifiably complete based on the
implementor's latest response and (optionally) its message history.

Rules:
- You are not the implementation agent.
- Never modify files. You may use read-only tools if inspection helps.
- Never ask the user questions.
- Return the structured output exactly as requested by the runtime.
- Be strict. If completion is uncertain, mark incomplete and provide
  concrete remediation notes the implementor can act on next turn.
- For trivial conversational builds, decide based on whether the latest
  response satisfies the request.
- For coding builds, prefer concrete verification: passing tests,
  successful commands, file inspection.
"""


@dataclass
class InspectorConfig:
    """One configured inspector."""

    name: str
    model: str
    prompt: str = DEFAULT_INSPECTOR_PROMPT
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InspectorConfig":
        return cls(
            name=str(data.get("name", "")),
            model=str(data.get("model", "")),
            prompt=str(data.get("prompt") or DEFAULT_INSPECTOR_PROMPT),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class InspectorRegistry:
    """In-memory snapshot of configured inspectors."""

    inspectors: list[InspectorConfig] = field(default_factory=list)

    def names(self) -> list[str]:
        return [i.name for i in self.inspectors]

    def enabled(self) -> list[InspectorConfig]:
        return [i for i in self.inspectors if i.enabled]

    def find(self, name: str) -> InspectorConfig | None:
        for i in self.inspectors:
            if i.name == name:
                return i
        return None


def validate_name(name: str) -> str | None:
    """Return an error string if the name is invalid, else None."""
    if not name:
        return "Name must not be empty."
    if not _NAME_RE.match(name):
        return (
            "Name must be 1-64 chars, letters/digits/underscore/hyphen only "
            "(no spaces)."
        )
    return None


def load_inspectors() -> InspectorRegistry:
    """Load inspectors from disk. Returns an empty registry if file missing."""
    if not os.path.exists(INSPECTORS_FILE):
        return InspectorRegistry()

    try:
        with open(INSPECTORS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load inspectors from %s: %s", INSPECTORS_FILE, exc)
        return InspectorRegistry()

    raw_inspectors = data.get("inspectors") if isinstance(data, dict) else None
    if not isinstance(raw_inspectors, list):
        return InspectorRegistry()

    inspectors: list[InspectorConfig] = []
    seen_names: set[str] = set()
    for item in raw_inspectors:
        if not isinstance(item, dict):
            continue
        try:
            inspector = InspectorConfig.from_dict(item)
        except Exception as exc:
            logger.warning("Skipping invalid inspector entry: %s", exc)
            continue
        if validate_name(inspector.name) is not None:
            logger.warning("Skipping inspector with invalid name: %r", inspector.name)
            continue
        if inspector.name in seen_names:
            logger.warning("Skipping duplicate inspector name: %r", inspector.name)
            continue
        if not inspector.model:
            logger.warning("Skipping inspector %r with no model", inspector.name)
            continue
        seen_names.add(inspector.name)
        inspectors.append(inspector)

    return InspectorRegistry(inspectors=inspectors)


def save_inspectors(registry: InspectorRegistry) -> None:
    """Persist the registry to disk atomically."""
    os.makedirs(os.path.dirname(INSPECTORS_FILE), exist_ok=True)
    payload = {"inspectors": [i.to_dict() for i in registry.inspectors]}
    tmp_path = f"{INSPECTORS_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, INSPECTORS_FILE)


def add_inspector(inspector: InspectorConfig) -> None:
    """Add an inspector. Raises ValueError on name conflict or validation."""
    err = validate_name(inspector.name)
    if err:
        raise ValueError(err)
    if not inspector.model:
        raise ValueError("Model must not be empty.")

    registry = load_inspectors()
    if registry.find(inspector.name) is not None:
        raise ValueError(f"An inspector named {inspector.name!r} already exists.")
    registry.inspectors.append(inspector)
    save_inspectors(registry)


def update_inspector(
    name: str,
    /,
    *,
    new_name: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Update fields of an existing inspector.

    ``name`` is positional-only so it doesn't collide with the ``new_name``
    kwarg used to rename an inspector. Pass ``None`` for any field to leave it
    unchanged.
    """
    registry = load_inspectors()
    existing = registry.find(name)
    if existing is None:
        raise ValueError(f"No inspector named {name!r}.")

    if new_name is not None and new_name != existing.name:
        err = validate_name(new_name)
        if err:
            raise ValueError(err)
        if registry.find(new_name) is not None:
            raise ValueError(f"An inspector named {new_name!r} already exists.")
        existing.name = new_name

    if model is not None:
        if not model:
            raise ValueError("Model must not be empty.")
        existing.model = model

    if prompt is not None:
        existing.prompt = prompt or DEFAULT_INSPECTOR_PROMPT

    if enabled is not None:
        existing.enabled = bool(enabled)

    save_inspectors(registry)


def delete_inspector(name: str) -> bool:
    """Remove an inspector. Returns True if it existed."""
    registry = load_inspectors()
    before = len(registry.inspectors)
    registry.inspectors = [i for i in registry.inspectors if i.name != name]
    if len(registry.inspectors) == before:
        return False
    save_inspectors(registry)
    return True


def toggle_inspector(name: str) -> bool | None:
    """Flip the enabled flag. Returns the new state, or None if missing."""
    registry = load_inspectors()
    inspector = registry.find(name)
    if inspector is None:
        return None
    inspector.enabled = not inspector.enabled
    save_inspectors(registry)
    return inspector.enabled


def get_enabled_inspectors_or_default(fallback_model: str) -> list[InspectorConfig]:
    """Return the list of enabled inspectors, or a single default inspector.

    If the user has configured inspectors via /inspectors, those are used.
    Otherwise we synthesize a single default inspector using ``fallback_model``
    and the standard build-inspector prompt so the build loop works
    out-of-the-box.
    """
    registry = load_inspectors()
    enabled = registry.enabled()
    if enabled:
        return enabled
    return [
        InspectorConfig(
            name="default",
            model=fallback_model,
            prompt=DEFAULT_INSPECTOR_PROMPT,
            enabled=True,
        )
    ]
