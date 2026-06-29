"""Map a bead's free-form ``execution_*`` metadata onto the serial drive.

Coverage-audit gap FB-8 (``bead_chain-9n3``, swarms#2): bd carries a
small, *unenforced* execution vocabulary in each bead's free-form
``metadata`` JSON — the canonical keys are ``execution_parallel_group``,
``execution_agent_type``, ``execution_model``, ``execution_effort`` and
``execution_mode`` (set via ``bd update --set-metadata k=v``). bd does
**not** special-case them; they are a shared contract between bead
authors and orchestrators. bead-chain historically read **none** of
them, so an author could not shape even the single ``/goal`` pass the
chain runs per bead.

This module closes the *serial-compatible* slice of that gap. It maps
the three keys that have a sensible one-worker meaning onto code-puppy's
own knobs, right before the ``/goal`` loop is armed:

    execution_effort     → reasoning budget   (config.set_openai_reasoning_effort)
    execution_model      → model select       (config.set_model_name)
    execution_agent_type → agent select       (config.set_default_agent)

``execution_parallel_group`` and ``execution_mode`` are deliberately
**not** acted on: bead-chain is a one-bead-at-a-time serial driver
(single-in_progress invariant), so parallel grouping is meaningless and
the run mode is always ``goal``. They — and any other key — fall through
the "unknown keys ignored" path.

Design notes
------------
* **Pure core, soft-fail shell.** :func:`extract_execution_hints` is a
  pure dict→dict filter (trivially testable); :func:`apply_execution_hints`
  is the impure orchestrator that calls the config setters and soft-fails
  per hint — a bad value (e.g. an invalid reasoning effort) is logged and
  skipped, never raised, so one fat-fingered hint can't strand the chain.
* **No auto-restore (YAGNI / acceptance contract).** The bead's
  acceptance criteria are "recognized keys influence the invocation;
  unknown keys ignored; *absent metadata → no change*". We honour that
  literally: a bead with no hints leaves whatever the previous bead (or
  the user) selected untouched. We do not snapshot/restore config around
  each bead — same persistence semantics as a user typing ``/model`` or
  ``/agent`` mid-session. If per-bead isolation is ever wanted that's a
  separate, larger feature.
* **``bd ready`` omits ``metadata``.** Verified on this bd build: the
  ``bd ready --json`` record bead-chain drives with does *not* carry a
  top-level ``metadata`` field, but ``bd show <id> --json`` does (parsed
  to a dict). So :func:`_resolve_metadata` uses the cached dict's
  ``metadata`` when present and otherwise re-fetches via :func:`beads.show`.
"""

from __future__ import annotations

import json
from typing import Any

from code_puppy import config
from code_puppy.messaging import emit_warning

from .beads import BeadsError
from .beads_reads import show

__all__ = [
    "extract_execution_hints",
    "apply_execution_hints",
]

# Recognized ``execution_*`` keys → (config setter attribute, human label).
#
# Setters are stored by *name* and resolved via ``getattr(config, name)``
# at apply time, not bound at import: that keeps tests able to monkeypatch
# ``code_puppy.config`` cleanly, and means a setter that vanishes under
# code-puppy version drift degrades to a silent no-op rather than an
# import error that would break the whole plugin.
#
# Only the three serial-compatible keys live here. Adding a future
# recognized key (with a sensible single-worker mapping) is a one-line
# edit — mirroring the EXCLUDED_TYPES / RECOVERABLE_STATUSES pattern. DRY.
_RECOGNIZED_HINTS: dict[str, tuple[str, str]] = {
    "execution_effort": ("set_openai_reasoning_effort", "reasoning effort"),
    "execution_model": ("set_model_name", "model"),
    "execution_agent_type": ("set_default_agent", "agent"),
}


def _coerce_metadata(raw: Any) -> dict[str, Any]:
    """Return a dict view of a bead's ``metadata`` field, or ``{}``.

    bd emits ``metadata`` as a parsed JSON object on this build, but
    older / other versions (and the per-edge ``dependencies[].metadata``)
    stringify it (``"{}"``). Accept both shapes, plus the absent / None /
    non-dict-JSON / garbage cases — all of which collapse to ``{}`` so the
    caller never has to branch. Pure function.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_execution_hints(metadata: Any) -> dict[str, str]:
    """Return the recognized, non-empty ``execution_*`` hints in ``metadata``.

    Pure function — the testable core of the feature.

    Contract:

    * Only keys in :data:`_RECOGNIZED_HINTS` survive. Unknown
      ``execution_*`` keys (e.g. ``execution_parallel_group``,
      ``execution_mode``) and every non-execution key are dropped.
    * Values are stringified and stripped; empty / whitespace-only
      values are discarded (an author who sets ``execution_model=`` to
      blank means "no preference", not "select the empty model").
    * Absent / non-dict / garbage metadata → ``{}`` (no hints).

    Returns a plain ``{recognized_key: value}`` dict, preserving the
    canonical ``execution_*`` key names so callers can look the setter
    back up in :data:`_RECOGNIZED_HINTS`.
    """
    meta = _coerce_metadata(metadata)
    hints: dict[str, str] = {}
    for key in _RECOGNIZED_HINTS:
        if key not in meta:
            continue
        value = str(meta[key]).strip()
        if value:
            hints[key] = value
    return hints


def _resolve_metadata(bead: dict[str, Any]) -> Any:
    """Return the bead's raw ``metadata`` field, re-fetching if needed.

    ``bd ready --json`` (the record bead-chain drives with) omits
    ``metadata``; ``bd show <id> --json`` carries it. So:

    * if the cached dict already has a ``metadata`` key (e.g. it came
      from :func:`beads.show`), use it as-is — no extra subprocess;
    * otherwise re-fetch via :func:`beads.show` keyed on the bead id.

    Soft-fails to ``None`` (→ no hints) on any bd infrastructure error
    or a missing / id-less bead: a transient ``bd`` blip must never
    strand the chain over an optional enhancement.
    """
    if "metadata" in bead:
        return bead.get("metadata")
    bead_id = str(bead.get("id", "")).strip()
    if not bead_id:
        return None
    try:
        full = show(bead_id)
    except BeadsError:
        return None
    if not full:
        return None
    return full.get("metadata")


def apply_execution_hints(bead: dict[str, Any] | None) -> list[str]:
    """Apply a bead's recognized ``execution_*`` hints to the /goal drive.

    Reads ``execution_effort`` / ``execution_model`` / ``execution_agent_type``
    from the bead's free-form ``metadata`` and maps each onto code-puppy's
    serial knobs (reasoning effort / model select / agent select) so they
    shape the single ``/goal`` pass bead-chain runs for this bead.

    Returns a list of human-readable ``"label → value"`` strings naming
    what was applied (for the caller to log); ``[]`` means nothing
    changed.

    Contract (matches the FB-8 acceptance criteria):

    * Recognized, non-empty keys influence the next ``/goal`` invocation.
    * Unknown keys are ignored; absent / garbage metadata → no change
      (``[]``).
    * Soft-fails *per hint*: a setter that rejects a value (e.g. an
      invalid reasoning effort) is logged via :func:`emit_warning` and
      skipped — never raised. Other valid hints in the same bead still
      apply, and the chain never stalls on a bad hint.
    """
    if not isinstance(bead, dict):
        return []

    hints = extract_execution_hints(_resolve_metadata(bead))
    if not hints:
        return []

    applied: list[str] = []
    for key, value in hints.items():
        setter_name, label = _RECOGNIZED_HINTS[key]
        setter = getattr(config, setter_name, None)
        if not callable(setter):
            # Setter gone under code-puppy version drift — skip silently;
            # this hint simply isn't supportable on this build.
            continue
        try:
            setter(value)
        except (KeyError, ValueError, OSError) as exc:
            # Soft-fail per hint, never strand the chain. We catch only the
            # exceptions a setter legitimately raises for a bad/unusable
            # value or a config-persistence failure:
            #   * ValueError  — value-rejected (e.g. invalid reasoning effort),
            #   * KeyError    — unknown key in a setter's lookup table,
            #   * OSError     — config-file write failed (set_model_name et al).
            # Programming errors (TypeError, AttributeError, ...) are NOT
            # caught — they signal a bug in the contract, not a bad hint, and
            # should surface loudly rather than be silently swallowed.
            emit_warning(
                f" bead-chain: ignoring {key}={value!r} — couldn't set {label}: {exc}"
            )
            continue
        applied.append(f"{label} → {value}")
    return applied
