"""Reasoning-effort handling for the public Manifold inference lane.

Manifold's live model catalog is authoritative. The open-source edition does not
carry provider-specific fallback tables because it does not call providers
directly. A reasoning field is emitted only when the selected Manifold model
advertises a compatible effort level.
"""

from __future__ import annotations

from typing import Any

MANIFOLD_PROVIDER = "kaidera-manifold"
PATTERN_EFFORT = "reasoning_effort"
CONNECTORS: dict[str, dict[str, Any]] = {
    MANIFOLD_PROVIDER: {"pattern": PATTERN_EFFORT, "default_levels": []},
}

_EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
_EFFORT_RANK = {level: index for index, level in enumerate(_EFFORT_ORDER)}
_OFF_TOKENS = frozenset({"", "off", "none", "no", "false", "disabled", "disable"})
_LEVEL_ALIASES = {
    "med": "medium",
    "mid": "medium",
    "moderate": "medium",
    "min": "minimal",
    "xhi": "xhigh",
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "maximum": "max",
    "on": "_on_",
    "true": "_on_",
    "enabled": "_on_",
    "enable": "_on_",
    "yes": "_on_",
}


def normalize_level(level: str | None) -> str:
    """Normalize an effort value while preserving provider-defined future values."""
    value = (level or "").strip().lower()
    if value in _OFF_TOKENS:
        return ""
    return _LEVEL_ALIASES.get(value, value)


def is_off(level: str | None) -> bool:
    return normalize_level(level) == ""


def connector_known(provider: str) -> bool:
    return (provider or "").strip().lower() == MANIFOLD_PROVIDER


def curated_levels(provider: str, model: str | None) -> list[str]:
    """Return no static ladder; Manifold model metadata supplies the real levels."""
    del provider, model
    return []


def reasons(provider: str, model: str | None) -> bool:
    """Manifold may expose reasoning models; capability is resolved from live rows."""
    del model
    return connector_known(provider)


def _advertised_levels(available_levels: list[str] | None) -> list[str]:
    if available_levels is None:
        return []
    levels: list[str] = []
    for raw in available_levels:
        level = normalize_level(str(raw))
        if level in {"", "_on_", "supported"} or level in levels:
            continue
        levels.append(level)
    return levels


def resolve_level(
    provider: str,
    model: str | None,
    level: str | None,
    *,
    available_levels: list[str] | None = None,
) -> str | None:
    """Resolve an operator choice against the selected model's live effort ladder."""
    del model
    if not connector_known(provider):
        return None
    requested = normalize_level(level)
    ladder = _advertised_levels(available_levels)
    if not requested or not ladder:
        return None
    if requested == "_on_":
        if "medium" in ladder:
            return "medium"
        ranked = [item for item in ladder if item in _EFFORT_RANK]
        at_or_below_medium = [
            item for item in ranked if _EFFORT_RANK[item] <= _EFFORT_RANK["medium"]
        ]
        return (at_or_below_medium or ranked or ladder)[-1]
    if requested in ladder:
        return requested
    requested_rank = _EFFORT_RANK.get(requested)
    ranked = sorted(
        ((_EFFORT_RANK[item], item) for item in ladder if item in _EFFORT_RANK),
        key=lambda row: row[0],
    )
    if requested_rank is None or not ranked:
        return None
    at_or_below = [item for rank, item in ranked if rank <= requested_rank]
    return at_or_below[-1] if at_or_below else ranked[0][1]


def apply_reasoning(
    provider: str,
    model: str | None,
    level: str | None,
    payload: dict[str, Any],
    *,
    available_levels: list[str] | None = None,
) -> dict[str, Any]:
    """Add Manifold's OpenAI-compatible effort field when the model advertises it."""
    resolved = resolve_level(
        provider,
        model,
        level,
        available_levels=available_levels,
    )
    if resolved is not None:
        payload[PATTERN_EFFORT] = resolved
    return payload


def extract_reasoning_text(provider: str, data: dict[str, Any]) -> str:
    """Extract reasoning text from a Manifold OpenAI-compatible response."""
    if not connector_known(provider):
        return ""
    try:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return ""
        for field in ("reasoning_content", "reasoning"):
            value = message.get(field)
            if isinstance(value, str) and value.strip():
                return value
    except Exception:
        return ""
    return ""
