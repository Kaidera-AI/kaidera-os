"""Shared normalization for harness reasoning and effort values."""

from __future__ import annotations

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
    """Normalize a stored effort string for an external harness."""
    value = (level or "").strip().lower()
    if value in _OFF_TOKENS:
        return ""
    return _LEVEL_ALIASES.get(value, value)


def is_off(level: str | None) -> bool:
    return normalize_level(level) == ""


__all__ = ["normalize_level", "is_off"]
