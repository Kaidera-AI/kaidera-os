"""Manifold-only model catalog for Kaidera OS Open Source.

The community source deliberately contains only the managed Manifold adapter. It
talks to Kaidera AI
Manifold's OpenAI-compatible edge with a Manifold inference credential supplied
by the operator. Missing configuration or network failures return an empty,
disabled provider group instead of crashing the console.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from . import platform_config
from . import providers_env
from . import settings as settings_store

CACHE_TTL_SECONDS = 15 * 60
CATALOG_REFRESH_INTERVAL_SECONDS = 12 * 60 * 60
_HTTP_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
_UA = "kaidera-os-open-source/0.1"

MANIFOLD_PROVIDER = "kaidera-manifold"
PROVIDER_ORDER = [MANIFOLD_PROVIDER]
PROVIDER_LABEL = {MANIFOLD_PROVIDER: "Kaidera AI Manifold"}
_PROVIDER_SETTING_KEYS = {
    MANIFOLD_PROVIDER: (
        "kaidera_manifold_api_key",
        "kaidera_manifold_project_id",
    )
}
_SETTING_ENV_VAR = providers_env._SETTING_ENV_VAR
_SETTING_ENV_ALIASES = providers_env._SETTING_ENV_ALIASES


def visible_providers() -> list[str]:
    return list(PROVIDER_ORDER)


def _env_vars_for_setting(setting_key: str) -> tuple[str, ...]:
    return providers_env.env_vars_for(setting_key)


def _env_file_value(var: str) -> str:
    return providers_env.env_file_value(var)


def _resolve_provider_key(cfg: dict[str, Any], setting_key: str) -> str:
    """Resolve one Manifold setting; unknown provider fields always fail closed."""
    if setting_key not in _SETTING_ENV_VAR:
        return ""
    direct = str(cfg.get(setting_key) or "").strip()
    if direct:
        return direct
    for var in _env_vars_for_setting(setting_key):
        value = str(os.environ.get(var) or "").strip() or _env_file_value(var)
        if value:
            return value
    return ""


def _provider_key_present(name: str, cfg: dict[str, Any]) -> bool:
    if name != MANIFOLD_PROVIDER:
        return False
    return all(_resolve_provider_key(cfg, key) for key in _PROVIDER_SETTING_KEYS[name])


def builtin_provider_config(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    values = cfg if isinstance(cfg, dict) else settings_store.load_with_secrets()
    return [
        {
            "name": MANIFOLD_PROVIDER,
            "label": PROVIDER_LABEL[MANIFOLD_PROVIDER],
            "key_is_set": _provider_key_present(MANIFOLD_PROVIDER, values),
            "testable": True,
            "key_field": "kaidera_manifold_api_key",
            "provider_ref": "kaidera_manifold_api_key",
            "base_url": platform_config.manifold_base_url(
                str(values.get("kaidera_manifold_base_url") or "")
            ),
            "project_id": _resolve_provider_key(values, "kaidera_manifold_project_id"),
        }
    ]


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _model(
    *,
    provider: str,
    id: str,
    display_name: str | None = None,
    type: str = "chat",
    context_window: int | None = None,
    max_output: int | None = None,
    reasoning_levels: list[str] | None = None,
    price_in_per_mtok: float | None = None,
    price_out_per_mtok: float | None = None,
    source: str = "live",
    input_modalities: list[str] | None = None,
    output_modalities: list[str] | None = None,
    cache_read_per_mtok: float | None = None,
    deprecated: bool = False,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "id": id,
        "display_name": display_name or id,
        "type": type,
        "context_window": context_window,
        "max_output": max_output,
        "reasoning_levels": reasoning_levels or [],
        "price_in_per_mtok": price_in_per_mtok,
        "price_out_per_mtok": price_out_per_mtok,
        "source": source,
        "input_modalities": input_modalities or [],
        "output_modalities": output_modalities or [],
        "cache_read_per_mtok": cache_read_per_mtok,
        "deprecated": deprecated,
    }


def _effort_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("reasoningEffort") or value.get("value") or value.get("effort")
    return str(value or "").strip()


def _reasoning_levels(row: dict[str, Any]) -> list[str]:
    reasoning = row.get("reasoning")
    candidates: Any = None
    if isinstance(reasoning, dict):
        candidates = reasoning.get("supported_efforts") or reasoning.get("efforts")
    candidates = candidates or row.get("reasoning_efforts") or row.get("supported_efforts")
    levels: list[str] = []
    if isinstance(candidates, list):
        for item in candidates:
            level = _effort_name(item)
            if level and level not in levels:
                levels.append(level)
    if levels:
        return levels
    parameters = row.get("supported_parameters") or []
    if isinstance(parameters, list) and "reasoning" in parameters:
        return ["supported"]
    return []


def _per_mtok(row: dict[str, Any], direction: str) -> float | None:
    direct = _to_float(
        row.get(f"price_{direction}_per_mtok")
        or row.get(f"{direction}_price_per_mtok")
    )
    if direct is not None:
        return direct
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return None
    raw = _to_float(pricing.get(direction) or pricing.get(f"{direction}_per_token"))
    return raw * 1_000_000 if raw is not None else None


def _parse_openai_compat(provider: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if provider != MANIFOLD_PROVIDER:
        return []
    rows: list[dict[str, Any]] = []
    for raw in payload.get("data") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        model_id = str(raw["id"])
        rows.append(
            _model(
                provider=MANIFOLD_PROVIDER,
                id=model_id,
                display_name=str(raw.get("display_name") or raw.get("name") or model_id),
                type=str(raw.get("type") or "chat"),
                context_window=_to_int(raw.get("context_window") or raw.get("context_length")),
                max_output=_to_int(raw.get("max_output") or raw.get("max_output_tokens")),
                reasoning_levels=_reasoning_levels(raw),
                price_in_per_mtok=_per_mtok(raw, "input"),
                price_out_per_mtok=_per_mtok(raw, "output"),
                input_modalities=list(raw.get("input_modalities") or []),
                output_modalities=list(raw.get("output_modalities") or []),
                deprecated=bool(raw.get("deprecated", False)),
            )
        )
    return rows


async def _fetch_manifold(
    client: httpx.AsyncClient,
    cfg: dict[str, Any],
) -> list[dict[str, Any]] | None:
    key = _resolve_provider_key(cfg, "kaidera_manifold_api_key")
    project_id = _resolve_provider_key(cfg, "kaidera_manifold_project_id")
    base_url = platform_config.manifold_base_url(
        str(cfg.get("kaidera_manifold_base_url") or "")
    )
    if not key or not project_id or not base_url:
        return None
    response = await client.get(
        f"{base_url.rstrip('/')}/models",
        headers={
            "Authorization": f"Bearer {key}",
            "X-Project-Id": project_id,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Manifold model catalog is not an object")
    return _parse_openai_compat(MANIFOLD_PROVIDER, payload)


def _sort_models(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get("display_name") or row.get("id") or "").lower())


async def _build_catalog(cfg: dict[str, Any]) -> dict[str, Any]:
    configured = _provider_key_present(MANIFOLD_PROVIDER, cfg)
    rows: list[dict[str, Any]] = []
    note: str | None = None
    if not configured:
        note = "Manifold is disabled until its inference key and project id are configured."
    elif not platform_config.manifold_base_url(str(cfg.get("kaidera_manifold_base_url") or "")):
        note = "Manifold is disabled until KAIDERA_MANIFOLD_BASE_URL is configured."
    else:
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                headers={"User-Agent": _UA},
                follow_redirects=True,
            ) as client:
                rows = _sort_models(await _fetch_manifold(client, cfg) or [])
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            note = (
                f"Manifold rejected the configured credential (HTTP {code})."
                if code in (401, 403)
                else f"Manifold model catalog returned HTTP {code}."
            )
        except (httpx.HTTPError, ValueError):
            note = "Manifold model catalog is temporarily unavailable."

    return {
        "groups": [
            {
                "provider": MANIFOLD_PROVIDER,
                "label": PROVIDER_LABEL[MANIFOLD_PROVIDER],
                "count": len(rows),
                "configured": configured,
                "note": note,
                "balance": None,
                "models": rows,
            }
        ],
        "fetched_at": time.time(),
        "total": len(rows),
        "notes": [note] if configured and note else [],
    }


_cache: dict[str, Any] = {"catalog": None, "expires": 0.0, "config_key": None}
_lock = asyncio.Lock()


def _config_fingerprint(cfg: dict[str, Any]) -> str:
    return "|".join(
        "1" if _resolve_provider_key(cfg, key) else "0"
        for key in (*_PROVIDER_SETTING_KEYS[MANIFOLD_PROVIDER], "kaidera_manifold_base_url")
    )


def _with_cache_meta(catalog: dict[str, Any], now: float, *, cached: bool) -> dict[str, Any]:
    out = dict(catalog)
    age = max(0, int(now - float(out.get("fetched_at") or now)))
    out.update(
        {
            "age_seconds": age,
            "age_human": "just now" if age < 5 else f"{age}s ago" if age < 60 else f"{age // 60}m ago",
            "from_cache": cached,
            "ttl_seconds": CACHE_TTL_SECONDS,
        }
    )
    return out


async def get_catalog(force: bool = False) -> dict[str, Any]:
    cfg = settings_store.load_with_secrets()
    fingerprint = _config_fingerprint(cfg)
    now = time.time()
    cached = _cache.get("catalog")
    if (
        not force
        and isinstance(cached, dict)
        and _cache.get("config_key") == fingerprint
        and now < float(_cache.get("expires") or 0)
    ):
        return _with_cache_meta(cached, now, cached=True)

    async with _lock:
        cached = _cache.get("catalog")
        if (
            not force
            and isinstance(cached, dict)
            and _cache.get("config_key") == fingerprint
            and time.time() < float(_cache.get("expires") or 0)
        ):
            return _with_cache_meta(cached, time.time(), cached=True)
        catalog = await _build_catalog(cfg)
        if catalog.get("total", 0) == 0 and isinstance(cached, dict) and cached.get("total", 0) > 0:
            stale = dict(cached)
            stale["notes"] = ["Live refresh failed; showing the last cached catalog."]
            return _with_cache_meta(stale, time.time(), cached=True)
        _cache.update(
            {
                "catalog": catalog,
                "config_key": fingerprint,
                "expires": time.time() + CACHE_TTL_SECONDS,
            }
        )
        return _with_cache_meta(catalog, time.time(), cached=False)


async def refresh_catalog_forever(
    *,
    interval_s: int = CATALOG_REFRESH_INTERVAL_SECONDS,
    get: Any = None,
    sleep: Any = None,
    log: Any = None,
    _max_iters: int | None = None,
) -> None:
    get = get or get_catalog
    sleep = sleep or asyncio.sleep
    iteration = 0
    while _max_iters is None or iteration < _max_iters:
        try:
            catalog = await get(force=True)
            if log is not None:
                log.info("Manifold catalog refresh: %s models", catalog.get("total", 0))
        except Exception as exc:  # pragma: no cover - defensive background-loop guard
            if log is not None:
                log.warning("Manifold catalog refresh failed: %s", exc)
        await sleep(interval_s)
        iteration += 1


def reset_cache() -> None:
    _cache.update({"catalog": None, "expires": 0.0, "config_key": None})


def cached_reasoning_levels(provider: str, model: str | None) -> list[str] | None:
    catalog = _cache.get("catalog")
    if provider != MANIFOLD_PROVIDER or not isinstance(catalog, dict) or not model:
        return None
    for group in catalog.get("groups") or []:
        if group.get("provider") != MANIFOLD_PROVIDER:
            continue
        for row in group.get("models") or []:
            if row.get("id") == model:
                return list(row.get("reasoning_levels") or [])
    return None


_BRAND_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
    'aria-hidden="true"><path d="M4 12h16M12 4v16"/><circle cx="12" cy="12" r="8"/></svg>'
)


def brand_svg(provider: str) -> str:
    return _BRAND_SVG


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "free"
    return f"${value:,.2f}" if value >= 1 else f"${value:.4f}".rstrip("0").rstrip(".")


def _fmt_window(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if value >= 1_000:
        return f"{value / 1_000:g}K"
    return str(value)


def _row_view(model: dict[str, Any]) -> dict[str, Any]:
    levels = list(model.get("reasoning_levels") or [])
    return {
        "id": model["id"],
        "display_name": model.get("display_name") or model["id"],
        "type": model.get("type") or "chat",
        "reasoning": " / ".join(levels) if levels else "-",
        "has_reasoning": bool(levels),
        "reasoning_levels": levels,
        "price_in": _fmt_price(model.get("price_in_per_mtok")),
        "price_out": _fmt_price(model.get("price_out_per_mtok")),
        "window": _fmt_window(model.get("context_window")),
        "max_output": _fmt_window(model.get("max_output")),
        "source": "live",
        "source_tag": {"label": "live", "css": "live"},
        "deprecated": bool(model.get("deprecated")),
    }


def view_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    groups = []
    for group in catalog.get("groups") or []:
        groups.append(
            {
                **group,
                "brand_svg": brand_svg(str(group.get("provider") or "")),
                "rows": [_row_view(row) for row in group.get("models") or []],
            }
        )
    return {
        "groups": groups,
        "total": catalog.get("total", 0),
        "configured_count": sum(1 for group in groups if group.get("configured")),
        "notes": catalog.get("notes") or [],
        "age_human": catalog.get("age_human", ""),
        "from_cache": catalog.get("from_cache", False),
    }


def pricing_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for group in catalog.get("groups") or []:
        if group.get("provider") != MANIFOLD_PROVIDER:
            continue
        for row in group.get("models") or []:
            model_id = str(row.get("id") or "")
            if model_id:
                index[model_id] = row
                index[f"{MANIFOLD_PROVIDER}/{model_id}"] = row
    return index


def provider_label(provider: str | None) -> str:
    if not provider:
        return "-"
    return PROVIDER_LABEL.get(provider, provider.replace("-", " ").title())


def resolve_model(model_id: str | None, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    original = str(model_id or "").strip()
    if not original:
        return _empty_resolution(None)
    native = original.split("/", 1)[1] if original.startswith(f"{MANIFOLD_PROVIDER}/") else original
    match = index.get(original) or index.get(native)
    return {
        "model_id": original,
        "provider": MANIFOLD_PROVIDER if match else None,
        "provider_label": provider_label(MANIFOLD_PROVIDER if match else None),
        "price_in_per_mtok": match.get("price_in_per_mtok") if match else None,
        "price_out_per_mtok": match.get("price_out_per_mtok") if match else None,
        "matched_id": match.get("id") if match else None,
        "resolved": match is not None,
        "priced": bool(
            match
            and match.get("price_in_per_mtok") is not None
            and match.get("price_out_per_mtok") is not None
        ),
    }


def _empty_resolution(model_id: str | None) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "provider": None,
        "provider_label": "-",
        "price_in_per_mtok": None,
        "price_out_per_mtok": None,
        "matched_id": None,
        "resolved": False,
        "priced": False,
    }


def fmt_cost(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value == 0:
        return "$0.00"
    if value >= 0.01:
        return f"${value:,.2f}"
    if value >= 0.0001:
        return f"${value:.4f}".rstrip("0").rstrip(".")
    return "<$0.0001"
