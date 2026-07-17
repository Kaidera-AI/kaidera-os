"""Provider-neutral model helpers for the community edition.

Kaidera OS Community executes external CLI harnesses. Their model and effort
catalogs are discovered by ``claude_catalog``, ``codex_catalog``, and
``pi_catalog``; this module only supplies empty pricing/catalog compatibility for
older analytics and template code. It performs no network calls and handles no
credentials.
"""

from __future__ import annotations

import asyncio
from typing import Any


def empty_catalog() -> dict[str, Any]:
    return {
        "groups": [],
        "errors": [],
        "fetched_at": None,
        "cached": True,
        "note": "Models are discovered from external harness CLIs.",
    }


async def get_catalog(*, force: bool = False) -> dict[str, Any]:
    _ = force
    return empty_catalog()


def view_catalog(catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    value = catalog or empty_catalog()
    return {
        "groups": list(value.get("groups") or []),
        "errors": list(value.get("errors") or []),
        "fetched_at": value.get("fetched_at"),
        "cached": True,
        "note": value.get("note") or "Models are discovered from external harness CLIs.",
    }


def pricing_index(catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    _ = catalog
    return {}


def resolve_model(model: str | None, index: dict[str, Any] | None = None) -> dict[str, Any]:
    _ = index
    model_id = str(model or "").strip() or None
    return {
        "model_id": model_id,
        "provider": None,
        "provider_label": "External harness",
        "price_in_per_mtok": None,
        "price_out_per_mtok": None,
        "resolved": bool(model_id),
        "priced": False,
    }


def provider_label(value: object) -> str:
    text = str(value or "").strip()
    return text.replace("-", " ").title() if text else "-"


def fmt_cost(value: object) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "-"


async def refresh_catalog_forever(*, stop: asyncio.Event | None = None) -> None:
    if stop is None:
        return
    await stop.wait()


__all__ = [
    "empty_catalog",
    "fmt_cost",
    "get_catalog",
    "pricing_index",
    "provider_label",
    "refresh_catalog_forever",
    "resolve_model",
    "view_catalog",
]
