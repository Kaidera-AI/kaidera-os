"""Immutable identity for the Kaidera OS Community source tree."""

from __future__ import annotations


COMMUNITY = "community"


def edition() -> str:
    return COMMUNITY


def is_community() -> bool:
    """Community identity is fixed at source; there is no runtime edition switch."""
    return True


__all__ = [
    "COMMUNITY",
    "edition",
    "is_community",
]
