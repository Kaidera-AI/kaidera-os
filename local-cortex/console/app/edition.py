"""Immutable product identity for the AGPL Kaidera OS source tree."""

from __future__ import annotations

OPEN_SOURCE = "open-source"


def edition() -> str:
    return OPEN_SOURCE


def is_open_source() -> bool:
    return True


__all__ = [
    "OPEN_SOURCE",
    "edition",
    "is_open_source",
]
