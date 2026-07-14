"""Read-only connectivity probe for Kaidera AI Manifold."""

from __future__ import annotations

from typing import Any

import httpx

from . import platform_config
from . import providers
from . import settings as settings_store

MANIFOLD_KEY_FIELD = "kaidera_manifold_api_key"


def is_testable(field: str) -> bool:
    return field == MANIFOLD_KEY_FIELD


async def test_provider(field: str, key: str | None = None) -> dict[str, Any]:
    if field != MANIFOLD_KEY_FIELD:
        return {
            "ok": False,
            "status": "not_supported",
            "message": "This open-source build supports only Kaidera AI Manifold.",
            "label": "Kaidera AI Manifold",
        }

    cfg = settings_store.load_with_secrets()
    if key and key.strip():
        cfg[MANIFOLD_KEY_FIELD] = key.strip()
    api_key = providers._resolve_provider_key(cfg, MANIFOLD_KEY_FIELD)
    project_id = providers._resolve_provider_key(cfg, "kaidera_manifold_project_id")
    base_url = platform_config.manifold_base_url(
        str(cfg.get("kaidera_manifold_base_url") or "")
    )
    if not api_key:
        message = "No Manifold inference key is configured."
    elif not project_id:
        message = "No Manifold project id is configured."
    elif not base_url:
        message = "No Manifold base URL is configured."
    else:
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                response = await client.get(
                    f"{base_url.rstrip('/')}/models",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "X-Project-Id": project_id,
                    },
                )
            response.raise_for_status()
            payload = response.json()
            count = len(payload.get("data") or []) if isinstance(payload, dict) else 0
            return {
                "ok": True,
                "status": "ok",
                "message": f"Reached Kaidera AI Manifold; {count} models available.",
                "label": "Kaidera AI Manifold",
            }
        except httpx.HTTPStatusError as exc:
            message = f"Manifold rejected the credential (HTTP {exc.response.status_code})."
        except (httpx.HTTPError, ValueError):
            message = "Could not reach Kaidera AI Manifold."
    return {
        "ok": False,
        "status": "error",
        "message": message,
        "label": "Kaidera AI Manifold",
    }


__all__ = ["is_testable", "test_provider"]
