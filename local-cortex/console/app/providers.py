"""Provider model-catalog data layer (R4b) — read-only, dynamic.

Builds the "Providers & Models" catalog: for every configured provider (key in
the console-local settings store, app.settings), live-fetch its model list, and
ALWAYS fetch OpenRouter's public `/api/v1/models` (no auth) as the richest
source + the cross-reference supplement that fills gaps — chiefly pricing for
the providers whose list endpoints omit it (Anthropic / OpenAI / Fireworks).

Follows the provider model API research notes:
  * Unified schema per model:
      {provider, id, display_name, type, context_window, max_output,
       reasoning_levels[], price_in_per_mtok, price_out_per_mtok, source}
    (plus a couple of optional carry-throughs: input/output modalities,
     cache_read_per_mtok, deprecated).
  * Per-provider list endpoints + auth headers (see _PROVIDER_FETCHERS).
  * Pricing normalization: OpenRouter prices are USD/token strings → ×1e6 for
    per-Mtok. Anthropic/OpenAI/Fireworks carry NO price in their API → filled
    from the OpenRouter cross-reference (`source` becomes "merged" or
    "supplement").
  * Reasoning: Anthropic exposes the full effort tree
    (capabilities.effort.{low,medium,high,max,xhigh} + thinking); OpenRouter
    flattens to a single `reasoning` flag (`supported_parameters ∋ reasoning`)
    → rendered as ["supported"].

Caching: the whole catalog is cached in-memory for ~15 min (TTL) so repeated
renders don't refetch. A network failure degrades to the last good cache (or an
empty catalog) plus a human note — it NEVER raises into the request path.

Read-only: nothing here writes Cortex or any provider; it only GETs public /
key-scoped list endpoints.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import httpx

from . import platform_config
from . import providers_env
from . import settings as settings_store

# ---------------------------------------------------------------------------
#  Tunables
# ---------------------------------------------------------------------------

# Catalog cache lifetime. The research says "live on page load, cache ~15 min".
CACHE_TTL_SECONDS = 15 * 60

# Proactive catalog refresh every 12h. The 15-min TTL only refreshes on access; this loop
# force-rebuilds twice a day so new models + prices + per-model reasoning levels land (and
# the cache/app-DB stays correct) even when nobody opens the picker — far cheaper than a
# per-call provider fetch from the harness. See refresh_catalog_forever().
CATALOG_REFRESH_INTERVAL_SECONDS = 12 * 60 * 60

# Per-request network budget. Provider list endpoints are remote, so this is a
# real (modest) network timeout — a slow/blocked provider degrades to "no live
# rows" for that provider rather than hanging the page.
_HTTP_TIMEOUT = httpx.Timeout(12.0, connect=6.0)

# A neutral UA — some endpoints 403 a bare python-httpx default.
_UA = "kaidera-os-console/0.1 (providers-catalog)"

# OpenRouter public model list — no auth, always fetched (richest + supplement).
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Provider display order + brand label for the grouped UI. The first block is the
# live-list providers (their own model API is fetched); the second block is the
# catalog-only providers that DON'T expose a reliable live list here (their models
# resolve via their API at call time) — they still render as a group with an honest
# key-presence status + a note, but no fetched rows.
PROVIDER_ORDER = [
    # Kaidera AI Manifold — the hosted inference service. The ONLY provider exposed in the
    # PUBLIC edition (see visible_providers + app.edition). Auth is a narrow
    # platform-minted inference key from the license customer surface.
    "kaidera-manifold",
    "anthropic",
    "openai",
    "openrouter",
    "fireworks",
    "groq",
    "siliconflow",
    "dashscope",
    "alibaba-cloud",
    "deepseek",
    "together",
    "bedrock",
    "cohere",
    "nvidia",
    "inception",
    "moonshot",
    "perplexity",
    "xai",
    "ollama-cloud",
]
PROVIDER_LABEL = {
    "kaidera-manifold": "Kaidera AI Manifold",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "fireworks": "Fireworks",
    "groq": "Groq",
    "siliconflow": "SiliconFlow",
    "dashscope": "Alibaba (DashScope)",
    "alibaba-cloud": "Alibaba Cloud",
    "deepseek": "DeepSeek",
    "together": "Together AI",
    "bedrock": "Amazon Bedrock",
    "cohere": "Cohere",
    "nvidia": "NVIDIA NIM",
    "inception": "Inception",
    "moonshot": "Moonshot AI",
    "perplexity": "Perplexity",
    "xai": "xAI",
    "ollama-cloud": "Ollama Cloud",
}

# The providers exposed in the PUBLIC edition (redistributable + open-source). Locking
# this to Manifold is the user's "providers restricted ONLY programmatically" rule. It
# is EDITION-gated, NEVER license-gated — no token can re-expose another provider. See
# app/edition.py + the fitness gate scripts/fitness/check-edition-not-license-gated.sh.
PUBLIC_EDITION_PROVIDERS = frozenset({"kaidera-manifold"})


def visible_providers() -> list[str]:
    """PROVIDER_ORDER filtered for the active edition. PUBLIC -> Manifold only; DEV ->
    the full catalog. The single seam the provider endpoints + config iterate, so the
    lockdown can't be bypassed by a settings write naming another provider."""
    try:
        from app import edition
        if edition.is_public():
            return [p for p in PROVIDER_ORDER if p in PUBLIC_EDITION_PROVIDERS]
    except Exception:
        pass
    return list(PROVIDER_ORDER)


# Provider -> the settings-store key(s) whose presence marks it "configured". A key
# counts as present if it's in the console store, process env, local-cortex/.env, or
# a PI API-key login where applicable (read-only) — so the status reflects what the
# system ACTUALLY runs with, not just the sandbox store. For Bedrock the pair is the
# AWS SigV4 credential, not a Bearer key.
_PROVIDER_SETTING_KEYS: dict[str, tuple[str, ...]] = {
    # Kaidera AI Manifold — the platform's OpenAI-compatible /v1 edge. License login
    # requests and stores the narrow inference key; runtime use is gated by the signed
    # `manifold_access` feature. BOTH the key AND the project id are required:
    # the /v1 edge returns 400 missing_project_id without the `X-Project-Id` header, so
    # the provider is not "configured" until the platform session supplies a project UUID.
    "kaidera-manifold": ("kaidera_manifold_api_key", "kaidera_manifold_project_id"),
    "anthropic": ("anthropic_api_key",),
    "openai": ("openai_api_key",),
    "openrouter": ("openrouter_api_key",),
    # Fireworks can list serverless models with just the inference API key; the
    # account id is optional and only used for the richer account-scoped list.
    "fireworks": ("fireworks_api_key",),
    "groq": ("groq_api_key",),
    "siliconflow": ("siliconflow_api_key",),
    "dashscope": ("dashscope_api_key",),
    "alibaba-cloud": ("alibaba_cloud_api_key",),
    "deepseek": ("deepseek_api_key",),
    "together": ("together_api_key",),
    "cohere": ("cohere_api_key",),
    "nvidia": ("nvidia_api_key",),
    "inception": ("inception_api_key",),
    "moonshot": ("moonshot_api_key",),
    "perplexity": ("perplexity_api_key",),
    "xai": ("xai_api_key",),
    # Ollama Cloud — a single Bearer API key (the OpenAI-compatible hosted API).
    "ollama-cloud": ("ollama_cloud_api_key",),
    # Bedrock needs BOTH halves of the SigV4 credential pair to be "configured".
    "bedrock": ("aws_access_key_id", "aws_secret_access_key"),
}

# settings-store key -> the REAL environment variable the harness/.env uses, so a
# provider can read as configured off its .env even when the console store is empty.
# CANONICAL home is app/providers_env.py — re-exported here (same dict object, one
# source of truth) so the module-attribute name `providers._SETTING_ENV_VAR` other
# code/tests subscript keeps resolving.
_SETTING_ENV_VAR = providers_env._SETTING_ENV_VAR
_SETTING_ENV_ALIASES = providers_env._SETTING_ENV_ALIASES

_PI_AUTH_PROVIDER_FOR_SETTING: dict[str, str] = {
    "anthropic_api_key": "anthropic",
    "openai_api_key": "openai",
    "openrouter_api_key": "openrouter",
    "fireworks_api_key": "fireworks",
    "groq_api_key": "groq",
    "siliconflow_api_key": "siliconflow",
    "dashscope_api_key": "dashscope",
    "alibaba_cloud_api_key": "alibaba-cloud",
    "deepseek_api_key": "deepseek",
    "together_api_key": "together",
    "cohere_api_key": "cohere",
    "nvidia_api_key": "nvidia",
    "inception_api_key": "inception",
    "moonshot_api_key": "moonshot",
    "perplexity_api_key": "perplexity",
    "xai_api_key": "xai",
    "ollama_cloud_api_key": "ollama-cloud",
}

_PI_BRIDGE_PROVIDERS = frozenset({
    "fireworks",
    "ollama-cloud",
    "openrouter",
})
_PI_BRIDGE_CACHE_SECONDS = 30
_pi_bridge_cache: dict[str, Any] = {"groups": None, "expires": 0.0}


# ── Low-level env/auth helpers — CANONICAL home is app/providers_env.py ───────────
# These module-private names stay (other code + tests patch/call them by attribute,
# e.g. `monkeypatch.setattr(providers, "_env_file_value", ...)`); the bodies just
# delegate to the one shared copy so a change to HOW the host reads provider auth is
# made in exactly ONE place. Behaviour is byte-identical to the pre-carve copies.


def _env_vars_for_setting(setting_key: str) -> tuple[str, ...]:
    """The real env-var name(s) a settings key resolves to (alias-aware)."""
    return providers_env.env_vars_for(setting_key)


def _pi_auth_file() -> Path:
    """PI's host-side auth file. `PI_AUTH_FILE` lets tests/ops override the path."""
    return providers_env.pi_auth_file()


def _pi_auth_api_key(provider: str) -> str:
    """Read one provider API key from PI's auth.json without ever exposing it
    (read-only, self-contained-gated). See app/providers_env.pi_auth_api_key."""
    return providers_env.pi_auth_api_key(provider)


def _env_file_value(var: str) -> str:
    """Best-effort, read-only read of `var` from local-cortex/.env (self-contained
    mode short-circuits). See app/providers_env.env_file_value."""
    return providers_env.env_file_value(var)


def _harness_base_url() -> str:
    """Base URL of the host harness-service. See app/providers_env.harness_base_url."""
    return providers_env.harness_base_url()


def _harness_headers() -> dict[str, str]:
    """Bearer headers for the host harness-service. See app/providers_env.harness_headers."""
    return providers_env.harness_headers()


def _pi_bridge_groups_from_payload(data: Any) -> list[dict[str, Any]]:
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return []
    return [g for g in groups if isinstance(g, dict)]


def _cache_pi_bridge_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _pi_bridge_cache["groups"] = list(groups)
    _pi_bridge_cache["expires"] = time.time() + _PI_BRIDGE_CACHE_SECONDS
    return groups


def _cached_pi_bridge_groups() -> list[dict[str, Any]] | None:
    groups = _pi_bridge_cache.get("groups")
    if isinstance(groups, list) and time.time() < float(_pi_bridge_cache.get("expires") or 0.0):
        return [g for g in groups if isinstance(g, dict)]
    return None


def _setting_present(cfg: dict[str, Any], setting_key: str) -> bool:
    """True if `setting_key` has a real value in the console store OR the process
    env OR local-cortex/.env OR PI auth.json (read-only). The honest "is this
    configured" check."""
    if (cfg.get(setting_key) or "").strip():
        return True
    for var in _env_vars_for_setting(setting_key):
        if (os.environ.get(var) or "").strip():
            return True
        if _env_file_value(var):
            return True
    pi_provider = _PI_AUTH_PROVIDER_FOR_SETTING.get(setting_key)
    if pi_provider and _pi_auth_api_key(pi_provider):
        return True
    return False


def _provider_key_present(name: str, cfg: dict[str, Any]) -> bool:
    """True if a provider counts as configured: ALL of its required setting keys
    are present (store/env/.env). For most providers that's a single API key; for
    Bedrock it's the access-key-id + secret pair. OpenRouter included — its key is
    real (it lives in the .env), so we no longer pretend it "needs no key"."""
    keys = _PROVIDER_SETTING_KEYS.get(name)
    if not keys:
        return False
    if name == "kaidera-manifold":
        try:
            from app import edition
            from app import license as lic_mod  # fitness:allow-manifold-entitlement
            if not edition.is_dev() and not lic_mod.entitlements().has_advanced("manifold_access"):  # fitness:allow-manifold-entitlement
                return False
        except Exception:
            return False
    return all(_setting_present(cfg, k) for k in keys)


def _primary_key_field(name: str) -> str:
    """The provider's PRIMARY secret-key setting field — the canonical write/test
    target for that provider's key (e.g. `anthropic_api_key`). Prefers the first
    required key whose name ends in `_api_key` / `_secret_access_key`; else the
    first required key; "" for a provider with no required keys."""
    keys = _PROVIDER_SETTING_KEYS.get(name) or ()
    for k in keys:
        if k.endswith("_api_key") or k.endswith("_secret_access_key"):
            return k
    return keys[0] if keys else ""


# Canonical API base URL per preconfigured provider — surfaced (read-only) in the
# Providers tab so the operator SEES the URL is already built in and only needs to
# paste a key (no manual endpoint config). These mirror the kaidera chat targets
# (`harness_runner._OWN_OPENAI_COMPAT_CHAT`) minus the `/chat/completions` path.
# Providers reached via a vendor SDK / non-OpenAI-compat path omit this (no URL shown).
_PROVIDER_DISPLAY_BASE_URL: dict[str, str] = {
    "kaidera-manifold": platform_config.manifold_base_url(),
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "groq": "https://api.groq.com/openai/v1",
    "siliconflow": "https://api.siliconflow.com/v1",
    "dashscope": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "alibaba-cloud": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "together": "https://api.together.xyz/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "moonshot": "https://api.moonshot.ai/v1",
    "xai": "https://api.x.ai/v1",
    "ollama-cloud": "https://ollama.com/v1",
}


def builtin_provider_config(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The PRECONFIGURED (built-in) providers + each one's key-presence + label +
    test target — the data the Providers tab's config view folds in. NEVER carries a
    raw key (only the boolean presence).

    For every provider in `PROVIDER_ORDER`, emit:
      {name, label, key_is_set, testable, key_field, base_url}
    where `key_is_set` is the honest configured-status (`_provider_key_present`:
    store OR env OR local-cortex/.env), `key_field` is its primary secret-key
    setting (`anthropic_api_key`, …), and `testable` reflects whether the read-only
    key probe can validate it (Bedrock SigV4 + the Perplexity public list cannot).

    `cfg` defaults to the current console settings (`settings.load()`); pass an
    explicit values map to test/score against a specific store snapshot. Read-only;
    never raises."""
    from . import provider_check  # lazy — avoid an import cycle at module load

    values = cfg if isinstance(cfg, dict) else settings_store.load_with_secrets()  # load() drops provider keys
    out: list[dict[str, Any]] = []
    for name in visible_providers():  # EDITION gate: PUBLIC -> Manifold only
        key_field = _primary_key_field(name)
        key_is_set = _provider_key_present(name, values)
        if not key_is_set:
            key_is_set = _pi_bridge_provider_present(name)
        out.append(
            {
                "name": name,
                "label": PROVIDER_LABEL.get(name, name.title()),
                "key_is_set": key_is_set,
                "testable": provider_check.is_testable(key_field) if key_field else False,
                "key_field": key_field,
                # Pre-filled endpoint so the operator only pastes a key (None → not shown).
                "base_url": _PROVIDER_DISPLAY_BASE_URL.get(name),
            }
        )
    return out


# ---------------------------------------------------------------------------
#  Unified model record
# ---------------------------------------------------------------------------

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
    """One UnifiedModel dict (research schema). Kept as a plain dict so the
    template iterates it directly with no model-class import."""
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


# ---------------------------------------------------------------------------
#  Small parse helpers (defensive — provider payloads vary / can be partial)
# ---------------------------------------------------------------------------

def _to_int(v: Any) -> int | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _pi_bridge_group_models(group: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert host PI `/models/pi` rows into provider catalog model rows.

    The host bridge returns model metadata, never credentials. We keep the source
    marked as `pi-bridge` so the UI can distinguish a provider login inherited
    from PI's extension auth from a key stored directly in the console.
    """
    provider = str(group.get("provider") or "").strip()
    if provider not in _PI_BRIDGE_PROVIDERS:
        return []
    rows: list[dict[str, Any]] = []
    for row in group.get("rows") or []:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not mid:
            continue
        input_modalities = ["text"]
        if bool(row.get("image")):
            input_modalities.append("image")
        rows.append(
            _model(
                provider=provider,
                id=str(mid),
                display_name=str(row.get("display_name") or mid),
                type=str(row.get("type") or "chat"),
                context_window=_to_int(row.get("context_window")),
                max_output=_to_int(row.get("max_output")),
                reasoning_levels=["supported"] if bool(row.get("reasoning")) else [],
                source="pi-bridge",
                input_modalities=input_modalities,
                output_modalities=["text"],
            )
        )
    return rows


async def _fetch_pi_bridge_groups(force: bool = False) -> list[dict[str, Any]]:
    """Fetch provider groups from the host PI catalog bridge.

    The console container does not have PI's host-side extension auth files. This
    asks the host harness-service for PI's model groups and consumes only model IDs
    and metadata, never raw API keys.
    """
    if not force:
        cached = _cached_pi_bridge_groups()
        if cached is not None:
            return cached
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=1.5)) as client:
            resp = await client.get(
                f"{_harness_base_url()}/models/pi",
                headers=_harness_headers(),
            )
        if resp.status_code != 200:
            return []
        groups = _pi_bridge_groups_from_payload(resp.json())
    except (httpx.HTTPError, ValueError):
        return []
    return _cache_pi_bridge_groups(groups)


def _fetch_pi_bridge_groups_sync(force: bool = False) -> list[dict[str, Any]]:
    """Synchronous twin for the Providers-config key-presence view."""
    if not force:
        cached = _cached_pi_bridge_groups()
        if cached is not None:
            return cached
    try:
        with httpx.Client(timeout=httpx.Timeout(2.0, connect=0.75)) as client:
            resp = client.get(
                f"{_harness_base_url()}/models/pi",
                headers=_harness_headers(),
            )
        if resp.status_code != 200:
            return []
        groups = _pi_bridge_groups_from_payload(resp.json())
    except (httpx.HTTPError, ValueError):
        return []
    return _cache_pi_bridge_groups(groups)


async def _pi_bridge_provider_group(provider: str) -> dict[str, Any] | None:
    provider = (provider or "").strip()
    if provider not in _PI_BRIDGE_PROVIDERS:
        return None
    for group in await _fetch_pi_bridge_groups():
        if str(group.get("provider") or "") == provider and (group.get("rows") or []):
            return group
    return None


def _pi_bridge_provider_present(provider: str) -> bool:
    provider = (provider or "").strip()
    if provider not in _PI_BRIDGE_PROVIDERS:
        return False
    for group in _fetch_pi_bridge_groups_sync():
        if str(group.get("provider") or "") == provider and (group.get("rows") or []):
            return True
    return False


def _price_per_mtok_from_token_str(v: Any) -> float | None:
    """OpenRouter pricing values are USD-per-TOKEN strings (e.g. "0.0000003").
    Multiply by 1e6 for USD-per-Mtok. "0"/""/None → None (unknown, not free)."""
    if v is None:
        return None
    try:
        per_token = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if per_token <= 0:
        return None
    return per_token * 1_000_000.0


def _short_name_from_id(model_id: str) -> str:
    """Fallback display name: take the slug after the last '/'."""
    return model_id.rsplit("/", 1)[-1] if model_id else model_id


# ---------------------------------------------------------------------------
#  OpenRouter — public list (richest source + the cross-reference supplement)
# ---------------------------------------------------------------------------

def _classify_openrouter_type(arch: dict[str, Any]) -> str:
    """Best-effort model `type` from an OpenRouter `architecture` block. Output
    modalities drive it: text→chat, image→image, audio→audio. Embeddings are not
    listed by OpenRouter, so everything here is a generative model."""
    out = [m.lower() for m in (arch.get("output_modalities") or [])]
    if "image" in out:
        return "image"
    if "audio" in out:
        return "audio"
    return "chat"


# canonical effort order for sorting OpenRouter's supported_efforts low→high.
_OR_EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def _openrouter_reasoning(m: dict[str, Any], sup: list[Any]) -> list[str]:
    """OpenRouter per-model reasoning levels.

    Prefers the REAL effort ladder from `reasoning.supported_efforts[]` (B2: the
    live levels — we previously only read the `supported_parameters ∋ reasoning`
    boolean and rendered ["supported"]). Falls back to ["supported"] when the
    model advertises the `reasoning` param but no explicit effort list, and to []
    when it advertises neither. `reasoning` may live at the row root or under a
    `reasoning`/`reasoning_config` block depending on the API revision."""
    block = m.get("reasoning") or m.get("reasoning_config") or {}
    efforts: Any = None
    if isinstance(block, dict):
        efforts = block.get("supported_efforts") or block.get("efforts")
    if isinstance(efforts, list) and efforts:
        normalized: list[str] = []
        for raw in efforts:
            if isinstance(raw, dict):
                value = raw.get("reasoningEffort") or raw.get("effort") or raw.get("value")
            else:
                value = raw
            effort = str(value or "").strip().lower()
            if effort and effort not in normalized:
                normalized.append(effort)
        present = [e for e in _OR_EFFORT_ORDER if e in normalized]
        extras = [e for e in normalized if e not in _OR_EFFORT_ORDER]
        levels = present + extras
        if levels:
            return levels
    return ["supported"] if "reasoning" in (sup or []) else []


def _parse_openrouter(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse OpenRouter's `/api/v1/models` into UnifiedModel rows.

    Per-row fields used: id, name, context_length, top_provider.{context_length,
    max_completion_tokens}, pricing.{prompt, completion, input_cache_read}
    (USD/token strings → ×1e6), architecture.{modality, input/output_modalities},
    supported_parameters (the `reasoning` flag), created/expiration_date."""
    rows: list[dict[str, Any]] = []
    for m in payload.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("canonical_slug")
        if not mid:
            continue
        arch = m.get("architecture") or {}
        top = m.get("top_provider") or {}
        pricing = m.get("pricing") or {}
        sup = m.get("supported_parameters") or []

        ctx = _to_int(m.get("context_length")) or _to_int(top.get("context_length"))
        max_out = _to_int(top.get("max_completion_tokens"))
        reasoning = _openrouter_reasoning(m, sup)

        rows.append(
            _model(
                provider="openrouter",
                id=mid,
                display_name=m.get("name") or _short_name_from_id(mid),
                type=_classify_openrouter_type(arch),
                context_window=ctx,
                max_output=max_out,
                reasoning_levels=reasoning,
                price_in_per_mtok=_price_per_mtok_from_token_str(pricing.get("prompt")),
                price_out_per_mtok=_price_per_mtok_from_token_str(pricing.get("completion")),
                source="live",
                input_modalities=arch.get("input_modalities") or [],
                output_modalities=arch.get("output_modalities") or [],
                cache_read_per_mtok=_price_per_mtok_from_token_str(
                    pricing.get("input_cache_read")
                ),
                deprecated=bool(m.get("expiration_date")),
            )
        )
    return rows


def _build_openrouter_xref(or_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index OpenRouter rows for the cross-reference supplement.

    OpenRouter ids are namespaced first-party slugs (`anthropic/claude-…`,
    `openai/gpt-…`). To fill an Anthropic/OpenAI/Fireworks model's missing
    pricing we look up its native id against this index under several keys:
      * the full slug ("anthropic/claude-opus-4.8")
      * the bare model part ("claude-opus-4.8")
      * a punctuation-normalized bare part ("claude-opus-4-8")
    so a native id like "claude-opus-4-8-20251115" can still find its OR row by
    longest-prefix on the normalized bare key.
    """
    xref: dict[str, dict[str, Any]] = {}
    for r in or_rows:
        mid = r["id"]
        bare = _short_name_from_id(mid).lower()
        xref.setdefault(mid.lower(), r)
        xref.setdefault(bare, r)
        xref.setdefault(_norm_key(bare), r)
    return xref


def _norm_key(s: str) -> str:
    """Lowercase + collapse '.'/':' to '-' so "claude-3.5" ~ "claude-3-5"."""
    return s.lower().replace(".", "-").replace(":", "-")


def _xref_lookup(
    native_id: str, xref: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the OpenRouter row that best matches a provider-native model id.

    Tries exact bare/normalized hits first, then a longest-prefix match on the
    normalized bare key (so "claude-opus-4-8-20251115" matches the OR slug
    "claude-opus-4-8"). Returns the OR UnifiedModel row or None."""
    bare = _short_name_from_id(native_id).lower()
    for key in (native_id.lower(), bare, _norm_key(bare)):
        if key in xref:
            return xref[key]
    # longest-prefix on the normalized bare id (handles dated suffixes).
    nb = _norm_key(bare)
    best: tuple[int, dict[str, Any]] | None = None
    for key, row in xref.items():
        if nb.startswith(key) and len(key) >= 6:
            if best is None or len(key) > best[0]:
                best = (len(key), row)
    return best[1] if best else None


def _apply_supplement(
    row: dict[str, Any], xref: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Fill a live row's NULL pricing/context/reasoning from its OpenRouter
    cross-reference match, and bump `source` to "merged" (live + supplement
    filled gaps) or "supplement" (live row had essentially nothing). Live values
    always win; the supplement only fills what's missing."""
    match = _xref_lookup(row["id"], xref)
    if not match:
        return row

    filled = False
    for field in ("price_in_per_mtok", "price_out_per_mtok", "cache_read_per_mtok"):
        if row.get(field) is None and match.get(field) is not None:
            row[field] = match[field]
            filled = True
    if row.get("context_window") is None and match.get("context_window") is not None:
        row["context_window"] = match["context_window"]
        filled = True
    if row.get("max_output") is None and match.get("max_output") is not None:
        row["max_output"] = match["max_output"]
        filled = True
    if not row.get("reasoning_levels") and match.get("reasoning_levels"):
        row["reasoning_levels"] = list(match["reasoning_levels"])
        filled = True
    if not row.get("input_modalities") and match.get("input_modalities"):
        row["input_modalities"] = list(match["input_modalities"])
    if not row.get("output_modalities") and match.get("output_modalities"):
        row["output_modalities"] = list(match["output_modalities"])

    if filled:
        # If the live row carried no metadata of its own (e.g. OpenAI, which only
        # returns id/created/owned_by), call it a pure supplement; otherwise merged.
        row["source"] = "merged"
    return row


def _apply_curated_reasoning(row: dict[str, Any]) -> dict[str, Any]:
    """B2 discovery fallback: fill a row's `reasoning_levels` from the per-provider
    connector registry (app.reasoning) — the curated model→levels map sourced from
    doc 15 §3 — when neither the live API nor the OpenRouter xref gave a real
    ladder. Refreshed with the catalog cron exactly like the OR price xref.

    Priority (live wins): a row that already carries explicit levels (Anthropic
    effort tree, OpenRouter supported_efforts) is LEFT ALONE. Only a row with no
    levels, or the bare ["supported"] placeholder (reasons but the ladder is
    unknown), is upgraded to the connector's curated ladder. If the connector
    says the model does NOT reason, an existing stale ["supported"] is cleared."""
    from app import reasoning as _reasoning

    provider = str(row.get("provider") or "")
    model = str(row.get("id") or "")
    current = row.get("reasoning_levels") or []
    has_real_ladder = bool(current) and current != ["supported"]
    if has_real_ladder:
        return row  # live/xref already provided concrete levels → keep them.

    if not _reasoning.connector_known(provider):
        # provider not in the registry → we have NO authoritative knowledge; leave
        # the row exactly as the live API / xref left it (never clear its data).
        return row

    if not _reasoning.reasons(provider, model):
        # connector knows this isn't a reasoning model → drop any stale placeholder.
        if current == ["supported"]:
            row["reasoning_levels"] = []
        return row

    curated = _reasoning.curated_levels(provider, model)
    if curated:
        row["reasoning_levels"] = list(curated)
    elif not current:
        # reasons but only a binary toggle (no ladder) → mark it supported so the
        # UI shows it as reasoning-capable (the apply core emits the toggle).
        row["reasoning_levels"] = ["supported"]
    return row


# ---------------------------------------------------------------------------
#  Anthropic — GET /v1/models (rich: capabilities tree incl. effort + thinking)
# ---------------------------------------------------------------------------

# Anthropic effort tiers, in the canonical low→high order the research lists.
_ANTHROPIC_EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max", "ultra"]


def _anthropic_reasoning(caps: dict[str, Any]) -> list[str]:
    """Extract Anthropic reasoning tiers from a model's `capabilities` block.

    Prefers the explicit `capabilities.effort` map (keys are the tier names);
    falls back to a single ["supported"] when only a `thinking` flag is present.
    """
    effort = caps.get("effort")
    if isinstance(effort, dict) and effort:
        present = [t for t in _ANTHROPIC_EFFORT_ORDER if t in effort]
        # include any non-canonical extras, preserving discovery order
        extras = [k for k in effort.keys() if k not in _ANTHROPIC_EFFORT_ORDER]
        levels = present + extras
        if levels:
            return levels
    if caps.get("thinking") or caps.get("extended_thinking"):
        return ["supported"]
    return []


def _parse_anthropic(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Anthropic `/v1/models` into UnifiedModel rows.

    Anthropic returns `{data:[{id, display_name, created_at, type:"model",
    capabilities:{...}}]}`. Pricing is NOT in the API (filled later from the
    OpenRouter xref). Reasoning comes from capabilities.effort/thinking. The
    capability tree may also carry context/output token caps; we read them when
    present and otherwise leave NULL for the supplement."""
    rows: list[dict[str, Any]] = []
    for m in payload.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        caps = m.get("capabilities") or {}
        # context / output windows live under a few possible capability keys.
        ctx = (
            _to_int(caps.get("max_context_window_tokens"))
            or _to_int(caps.get("context_window"))
            or _to_int(caps.get("max_input_tokens"))
        )
        max_out = (
            _to_int(caps.get("max_output_tokens"))
            or _to_int(caps.get("max_tokens"))
        )
        rows.append(
            _model(
                provider="anthropic",
                id=mid,
                display_name=m.get("display_name") or _short_name_from_id(mid),
                type="chat",
                context_window=ctx,
                max_output=max_out,
                reasoning_levels=_anthropic_reasoning(caps),
                price_in_per_mtok=None,  # not in API → supplement
                price_out_per_mtok=None,
                source="live",
            )
        )
    return rows


# ---------------------------------------------------------------------------
#  OpenAI — GET /v1/models (poorest: id/created/owned_by only)
# ---------------------------------------------------------------------------

def _classify_openai_type(model_id: str) -> str:
    """Heuristic `type` from an OpenAI model id (the API gives no type field).
    embedding / tts / transcribe / image / moderation / realtime else chat."""
    mid = model_id.lower()
    if "embedding" in mid:
        return "embedding"
    if "tts" in mid or mid.startswith("tts"):
        return "audio"
    if "whisper" in mid or "transcribe" in mid:  # fitness:allow-literal model-id substring check
        return "audio"
    if "dall-e" in mid or "image" in mid:
        return "image"
    if "moderation" in mid:
        return "moderation"
    if "realtime" in mid:
        return "realtime"
    return "chat"


def _parse_openai(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse OpenAI `/v1/models` into UnifiedModel rows.

    The endpoint is the poorest: `{data:[{id, created, owned_by, object}]}`.
    Everything except id/type is NULL here and filled from the OpenRouter xref
    where a matching `openai/<id>` slug exists; rows with no match render with
    the metadata blank (UI tags them so the gap is honest)."""
    rows: list[dict[str, Any]] = []
    for m in payload.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        rows.append(
            _model(
                provider="openai",
                id=mid,
                display_name=_short_name_from_id(mid),
                type=_classify_openai_type(mid),
                source="live",
            )
        )
    return rows


# ---------------------------------------------------------------------------
#  Fireworks — GET /v1/accounts/{account_id}/models (control-plane; medium)
# ---------------------------------------------------------------------------

def _parse_fireworks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Fireworks' account model list into UnifiedModel rows.

    The control-plane API returns `{models:[{name, displayName,
    contextLength, supportsImageInput, supportsTools, ...}]}` (shapes vary;
    some deployments return `{data:[...]}`). Pricing + reasoning are NOT in the
    API → filled from the OpenRouter xref. `name` is a resource path like
    `accounts/<acct>/models/<model>`; we surface the trailing model id."""
    items = payload.get("models")
    if not isinstance(items, list):
        items = payload.get("data") or []
    rows: list[dict[str, Any]] = []
    for m in items:
        if not isinstance(m, dict):
            continue
        raw_name = m.get("name") or m.get("id") or ""
        if not raw_name:
            continue
        # The full resource path (accounts/<acct>/models/<slug>) IS the id the
        # Fireworks openai-compat inference endpoint requires — never shorten it.
        mid = raw_name
        short = _short_name_from_id(raw_name)
        ctx = (
            _to_int(m.get("contextLength"))
            or _to_int(m.get("context_length"))
            or _to_int(m.get("maxContextWindow"))
        )
        in_mod = ["text"]
        if m.get("supportsImageInput"):
            in_mod = ["text", "image"]
        rows.append(
            _model(
                provider="fireworks",
                id=mid,
                display_name=m.get("displayName") or m.get("display_name") or short,
                type="chat",
                context_window=ctx,
                reasoning_levels=[],  # not in API → supplement
                source="live",
                input_modalities=in_mod,
            )
        )
    return rows


# ---------------------------------------------------------------------------
#  Live fetchers (per provider) — each returns (rows, note-or-None)
# ---------------------------------------------------------------------------

async def _fetch_json(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """GET a JSON endpoint, raising httpx.HTTPError on transport/status issues
    and ValueError on a non-JSON body. Callers wrap this to turn failures into a
    graceful per-provider note."""
    resp = await client.get(url, headers=headers or {})
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("unexpected (non-object) response")
    return data


async def _fetch_openrouter(client: httpx.AsyncClient, _: dict[str, Any]):
    """OpenRouter public list — no auth. Always called (richest + supplement)."""
    data = await _fetch_json(client, OPENROUTER_MODELS_URL)
    return _parse_openrouter(data)


async def _fetch_anthropic(client: httpx.AsyncClient, cfg: dict[str, Any]):
    key = _resolve_provider_key(cfg, "anthropic_api_key")
    if not key:
        return None  # not configured → skip (not an error)
    data = await _fetch_json(
        client,
        "https://api.anthropic.com/v1/models?limit=1000",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    return _parse_anthropic(data)


async def _fetch_openai(client: httpx.AsyncClient, cfg: dict[str, Any]):
    key = _resolve_provider_key(cfg, "openai_api_key")
    if not key:
        return None
    data = await _fetch_json(
        client,
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    return _parse_openai(data)


async def _fetch_fireworks(client: httpx.AsyncClient, cfg: dict[str, Any]):
    key = _resolve_provider_key(cfg, "fireworks_api_key")
    account = (cfg.get("fireworks_account_id") or "").strip()
    if not key:
        return None
    if account:
        data = await _fetch_json(
            client,
            f"https://api.fireworks.ai/v1/accounts/{account}/models?pageSize=200",
            headers={"Authorization": f"Bearer {key}"},
        )
        return _parse_fireworks(data)
    # No explicit account → list the PUBLIC serverless catalog. The openai-compat
    # /inference/v1/models endpoint only returns the handful of account-scoped models;
    # the full serverless set lives under the public `fireworks` account, filtered to
    # the serverless-runnable subset (control-plane API; ~15-20 models, single page).
    data = await _fetch_json(
        client,
        "https://api.fireworks.ai/v1/accounts/fireworks/models"
        "?pageSize=200&filter=supports_serverless%20%3D%20true",
        headers={"Authorization": f"Bearer {key}"},
    )
    return _parse_fireworks(data)


# ---------------------------------------------------------------------------
#  Account balance / credits (#133) — per provider, WHERE the provider exposes a
#  programmatic balance endpoint. Most LLM providers do NOT (OpenAI deprecated
#  theirs; Anthropic/Groq/Fireworks/Ollama have none) → those simply show no
#  balance. Where one exists, fetch it best-effort and attach a small
#  {amount, currency, display, detail?} to that provider's catalog group. NEVER
#  raises into the catalog build (any failure → None → no balance shown).
# ---------------------------------------------------------------------------

def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _balance(amount: float, currency: str, *, detail: str | None = None) -> dict[str, Any]:
    """A display-ready balance record. `display` is the formatted headline the UI
    shows; `detail` (optional) is a secondary line like usage-of-total."""
    cur = (currency or "USD").upper()
    sym = {"USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£"}.get(cur, "")
    amt = round(amount, 2)
    display = f"{sym}{amt:,.2f}" if sym else f"{amt:,.2f} {cur}"
    out: dict[str, Any] = {"amount": amt, "currency": cur, "display": display}
    if detail:
        out["detail"] = detail
    return out


def _parse_openrouter_balance(data: dict[str, Any]) -> dict[str, Any] | None:
    # GET /api/v1/credits → {"data": {"total_credits": N, "total_usage": N}}
    d = data.get("data") if isinstance(data.get("data"), dict) else None
    if not d:
        return None
    total, used = _as_float(d.get("total_credits")), _as_float(d.get("total_usage"))
    if total is None and used is None:
        return None
    remaining = (total or 0.0) - (used or 0.0)
    return _balance(remaining, "USD", detail=f"${used or 0:,.2f} of ${total or 0:,.2f} used")


def _parse_deepseek_balance(data: dict[str, Any]) -> dict[str, Any] | None:
    # GET /user/balance → {"is_available":bool,"balance_infos":[{"currency","total_balance"}]}
    infos = data.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return None
    info = next((i for i in infos if isinstance(i, dict) and i.get("currency") == "USD"), None) or infos[0]
    if not isinstance(info, dict):
        return None
    amount = _as_float(info.get("total_balance"))
    return _balance(amount, str(info.get("currency") or "USD")) if amount is not None else None


def _parse_moonshot_balance(data: dict[str, Any]) -> dict[str, Any] | None:
    # GET /v1/users/me/balance → {"data": {"available_balance": N, ...}}
    d = data.get("data") if isinstance(data.get("data"), dict) else None
    if not d:
        return None
    amount = _as_float(d.get("available_balance"))
    return _balance(amount, "USD") if amount is not None else None


# provider -> (balance URL, the key field for Bearer auth, parser)
_BALANCE_ENDPOINTS: dict[str, tuple[str, str, Any]] = {
    "openrouter": ("https://openrouter.ai/api/v1/credits", "openrouter_api_key", _parse_openrouter_balance),
    "deepseek": ("https://api.deepseek.com/user/balance", "deepseek_api_key", _parse_deepseek_balance),
    "moonshot": ("https://api.moonshot.ai/v1/users/me/balance", "moonshot_api_key", _parse_moonshot_balance),
}


async def _fetch_balance(client: httpx.AsyncClient, name: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """One provider's account balance, or None (no endpoint / not configured / any
    error). Bearer auth from the provider's resolved key. Never raises."""
    spec = _BALANCE_ENDPOINTS.get(name)
    if not spec:
        return None
    url, key_field, parser = spec
    key = _resolve_provider_key(cfg, key_field)
    if not key:
        return None
    try:
        return parser(await _fetch_json(client, url, headers={"Authorization": f"Bearer {key}"}))
    except (httpx.HTTPError, ValueError):
        return None


# ---------------------------------------------------------------------------
#  OpenAI-compatible providers (Groq / SiliconFlow / DeepSeek / Together /
#  Moonshot / xAI) — all serve a key-scoped GET /models in the OpenAI
#  `{data:[{id, created, owned_by}]}` shape. One parser + one fetcher factory
#  cover them all; pricing/context/reasoning gaps are backfilled from the
#  OpenRouter cross-reference exactly like OpenAI's own (poor) list.
# ---------------------------------------------------------------------------

# provider -> (settings key, GET /models URL). The key is RESOLVED store→env→.env
# (via _resolve_provider_key) so a provider whose key lives only in the real .env
# still lists — same honesty as the configured-status check.
_OPENAI_COMPAT: dict[str, tuple[str, str]] = {
    "groq": ("groq_api_key", "https://api.groq.com/openai/v1/models"),
    "siliconflow": ("siliconflow_api_key", "https://api.siliconflow.com/v1/models"),
    "deepseek": ("deepseek_api_key", "https://api.deepseek.com/v1/models"),
    "together": ("together_api_key", "https://api.together.xyz/v1/models"),
    # NVIDIA NIM hosted catalog uses the OpenAI-compatible model-list surface.
    "nvidia": ("nvidia_api_key", "https://integrate.api.nvidia.com/v1/models"),
    "moonshot": ("moonshot_api_key", "https://api.moonshot.ai/v1/models"),
    "xai": ("xai_api_key", "https://api.x.ai/v1/models"),
    "ollama-cloud": ("ollama_cloud_api_key", "https://ollama.com/v1/models"),
    # Alibaba Cloud / Model Studio — OpenAI-compatible; compatible-mode /models serves
    # the live list (verified: 145 Qwen/GLM/DeepSeek models). Same URL the key-test probes.
    "alibaba-cloud": (
        "alibaba_cloud_api_key",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
    ),
}


def _resolve_provider_key(cfg: dict[str, Any], setting_key: str) -> str:
    """Pick the key to USE for a live fetch: console store first, then the process
    env, then local-cortex/.env (read-only). Returns "" when none is found (the
    fetcher then skips — not an error). Mirrors the configured-status resolution so
    'shows configured' and 'actually fetches' stay consistent."""
    # EDITION backstop (programmatic, NOT license): PUBLIC exposes only Manifold, so
    # refuse to resolve a key for any other provider — a hand-edited settings row or a
    # stray .env can't smuggle anthropic/openai/etc. back in at runtime.
    try:
        from app import edition
        if edition.is_public():
            non_manifold = {
                k for prov, keys in _PROVIDER_SETTING_KEYS.items()
                if prov not in PUBLIC_EDITION_PROVIDERS for k in keys
            }
            if setting_key in non_manifold:
                return ""
            if setting_key == "kaidera_manifold_api_key":
                from app import license as lic_mod  # fitness:allow-manifold-entitlement
                if not lic_mod.entitlements().has_advanced("manifold_access"):  # fitness:allow-manifold-entitlement
                    return ""
    except Exception:
        if setting_key == "kaidera_manifold_api_key":
            return ""
    val = (cfg.get(setting_key) or "").strip()
    if val:
        return val
    for var in _env_vars_for_setting(setting_key):
        env_val = (os.environ.get(var) or "").strip()
        if env_val:
            return env_val
        file_val = _env_file_value(var)
        if file_val:
            return file_val
    pi_provider = _PI_AUTH_PROVIDER_FOR_SETTING.get(setting_key)
    if pi_provider:
        pi_val = _pi_auth_api_key(pi_provider)
        if pi_val:
            return pi_val
    return ""


def _parse_openai_compat(provider: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a generic OpenAI-compatible `{data:[{id, ...}]}` model list. Type is
    heuristically classified from the id (same logic as OpenAI); everything else is
    NULL → filled from the OpenRouter xref where a match exists."""
    rows: list[dict[str, Any]] = []
    for m in payload.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        supported = m.get("supported_parameters") or []
        rows.append(
            _model(
                provider=provider,
                id=str(mid),
                display_name=_short_name_from_id(str(mid)),
                type=_classify_openai_type(str(mid)),
                reasoning_levels=_openrouter_reasoning(m, supported),
                source="live",
            )
        )
    return rows


async def _fetch_manifold(client: httpx.AsyncClient, cfg: dict[str, Any]):
    """Fetch the platform edge's authenticated OpenAI-compatible model list."""
    key = _resolve_provider_key(cfg, "kaidera_manifold_api_key")
    project_id = _resolve_provider_key(cfg, "kaidera_manifold_project_id")
    if not key or not project_id:
        return None
    base_url = platform_config.manifold_base_url(
        str(cfg.get("kaidera_manifold_base_url") or "")
    )
    if not base_url:
        return None
    data = await _fetch_json(
        client,
        f"{base_url}/models",
        headers={
            "Authorization": f"Bearer {key}",
            "X-Project-Id": project_id,
        },
    )
    return _parse_openai_compat("kaidera-manifold", data)


def _make_openai_compat_fetcher(provider: str):
    """Build a fetcher for one OpenAI-compatible provider (closes over its name).
    Resolves the key store→env→.env; returns None (skip) when no key is found."""
    setting_key, url = _OPENAI_COMPAT[provider]

    async def _fetch(client: httpx.AsyncClient, cfg: dict[str, Any]):
        key = _resolve_provider_key(cfg, setting_key)
        if not key:
            return None  # not configured anywhere → skip (not an error)
        data = await _fetch_json(client, url, headers={"Authorization": f"Bearer {key}"})
        return _parse_openai_compat(provider, data)

    return _fetch


# ---------------------------------------------------------------------------
#  Cohere — NATIVE v2 list (not OpenAI-compatible): GET /v2/models returns
#  `{models:[{name, endpoints[], context_length, ...}]}` with a Bearer key.
# ---------------------------------------------------------------------------

def _parse_cohere(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Cohere's `/v2/models` into UnifiedModel rows.

    Cohere returns `{models:[{name, endpoints:[...], context_length,
    default_endpoints, ...}], next_page_token}`. `name` is the model id; `endpoints`
    drives a coarse type (embed→embedding, rerank→rerank, else chat). Pricing/
    reasoning are not in the API (Cohere isn't on OpenRouter either) → left NULL."""
    rows: list[dict[str, Any]] = []
    for m in payload.get("models") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("name")
        if not mid:
            continue
        endpoints = [str(e).lower() for e in (m.get("endpoints") or [])]
        if "embed" in endpoints:
            mtype = "embedding"
        elif "rerank" in endpoints:
            mtype = "rerank"
        else:
            mtype = "chat"
        rows.append(
            _model(
                provider="cohere",
                id=str(mid),
                display_name=str(mid),
                type=mtype,
                context_window=_to_int(m.get("context_length")),
                source="live",
            )
        )
    return rows


async def _fetch_cohere(client: httpx.AsyncClient, cfg: dict[str, Any]):
    key = _resolve_provider_key(cfg, "cohere_api_key")
    if not key:
        return None
    data = await _fetch_json(
        client,
        "https://api.cohere.com/v2/models?page_size=1000",
        headers={"Authorization": f"Bearer {key}"},
    )
    return _parse_cohere(data)


# provider -> (fetcher, "requires" hint shown when not configured). Only the
# providers with a usable live model API appear here; the catalog-only providers
# (DashScope / Inception / Perplexity / Bedrock) are NOT fetched — they render a
# key-presence status + note via _CATALOG_ONLY below.
_PROVIDER_FETCHERS = {
    "kaidera-manifold": (_fetch_manifold, "Manifold inference key + project id"),
    "openrouter": (_fetch_openrouter, None),
    "anthropic": (_fetch_anthropic, "Anthropic API key"),
    "openai": (_fetch_openai, "OpenAI API key"),
    "fireworks": (_fetch_fireworks, "Fireworks API key"),
    "groq": (_make_openai_compat_fetcher("groq"), "Groq API key"),
    "siliconflow": (_make_openai_compat_fetcher("siliconflow"), "SiliconFlow API key"),
    "deepseek": (_make_openai_compat_fetcher("deepseek"), "DeepSeek API key"),
    "together": (_make_openai_compat_fetcher("together"), "Together AI API key"),
    "nvidia": (_make_openai_compat_fetcher("nvidia"), "NVIDIA API key"),
    "moonshot": (_make_openai_compat_fetcher("moonshot"), "Moonshot AI API key"),
    "xai": (_make_openai_compat_fetcher("xai"), "xAI API key"),
    "ollama-cloud": (_make_openai_compat_fetcher("ollama-cloud"), "Ollama Cloud API key"),
    "alibaba-cloud": (_make_openai_compat_fetcher("alibaba-cloud"), "Alibaba Cloud API key"),
    "cohere": (_fetch_cohere, "Cohere API key"),
}

# Catalog-only providers: NO reliable live model-list API is wired here, so they
# render as a group with an honest key-presence status + a note (no fetched rows,
# never fabricated ids). Each maps provider -> {requires, note_configured}:
#   * dashscope  — compatible-mode /models is not documented; models resolve via API.
#   * inception  — only chat/completions is documented; no confirmed /models list.
#   * perplexity — its /models endpoint is public/undocumented-for-listing; models
#                  are a known fixed set used at request time.
#   * bedrock    — runtime/model catalog integration still does not enumerate rows
#                  here; the Providers tab can validate credentials via a signed
#                  ListFoundationModels probe in provider_check.
_CATALOG_ONLY: dict[str, dict[str, str]] = {
    "dashscope": {
        "requires": "Alibaba DashScope API key",
        "note_configured": "Configured — Qwen models resolve via the DashScope API "
        "(no live model-list wired here).",
    },
    "inception": {
        "requires": "Inception API key",
        "note_configured": "Configured — Mercury models resolve via the Inception "
        "API (no confirmed model-list endpoint to enumerate here).",
    },
    "perplexity": {
        "requires": "Perplexity API key",
        "note_configured": "Configured — Sonar models are used via the Perplexity "
        "API at request time (no key-scoped model list to enumerate here).",
    },
    "bedrock": {
        "requires": "AWS access key ID + secret",
        "note_configured": "Configured — use Test to validate Bedrock SigV4 "
        "credentials; model catalog rows are not enumerated here yet.",
    },
}


# ---------------------------------------------------------------------------
#  Catalog assembly
# ---------------------------------------------------------------------------

def _sort_models(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable, readable per-provider order: chat first, then by display name."""
    type_rank = {"chat": 0, "image": 1, "audio": 2, "embedding": 3}
    return sorted(
        rows,
        key=lambda r: (
            type_rank.get(r.get("type"), 9),
            (r.get("display_name") or r.get("id") or "").lower(),
        ),
    )


async def _build_catalog(cfg: dict[str, Any]) -> dict[str, Any]:
    """Fetch every provider concurrently, merge the OpenRouter supplement into
    the key-only providers, and shape the grouped catalog the UI renders.

    Returns:
        {
          "groups": [ {provider, label, count, configured, note, models:[...]} ],
          "fetched_at": float (epoch),
          "total": int,
          "notes": [str, ...],          # provider-level fetch issues
          "openrouter_count": int,
        }
    Never raises — a provider that errors contributes an empty group + a note.
    """
    notes: list[str] = []
    raw: dict[str, list[dict[str, Any]] | None] = {}
    errors: dict[str, str] = {}

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True
    ) as client:
        async def run(name: str):
            fetcher, _hint = _PROVIDER_FETCHERS[name]
            try:
                return name, await fetcher(client, cfg), None
            except (httpx.HTTPError, ValueError) as exc:
                return name, None, _explain(name, exc)

        results = await asyncio.gather(
            *(run(n) for n in _PROVIDER_FETCHERS), return_exceptions=True
        )
        # Account balances (#133): best-effort + concurrent, only for providers with a
        # balance endpoint. _fetch_balance returns None on any issue (no key, no endpoint,
        # error), so this never breaks the catalog build.
        bal_results = await asyncio.gather(
            *(_fetch_balance(client, n, cfg) for n in _BALANCE_ENDPOINTS),
            return_exceptions=True,
        )

    balances: dict[str, dict[str, Any]] = {
        n: r for n, r in zip(_BALANCE_ENDPOINTS, bal_results) if isinstance(r, dict)
    }

    for res in results:
        if isinstance(res, BaseException):
            # Defensive — gather(return_exceptions) should prevent this branch.
            continue
        name, rows, err = res
        raw[name] = rows
        if err:
            errors[name] = err

    # OpenRouter rows power both their own group AND the supplement xref.
    or_rows = raw.get("openrouter") or []
    xref = _build_openrouter_xref(or_rows)
    if "openrouter" in errors:
        notes.append(f"OpenRouter: {errors['openrouter']} (catalog + pricing supplement unavailable)")
    pi_bridge_rows = {
        str(group.get("provider") or ""): _pi_bridge_group_models(group)
        for group in await _fetch_pi_bridge_groups()
        if isinstance(group, dict)
    }

    groups: list[dict[str, Any]] = []
    total = 0
    for name in PROVIDER_ORDER:
        # Honest configured-status: a real key present in the console store OR the
        # process env OR local-cortex/.env (read-only). This is the SINGLE source of
        # the "configured" flag now — no provider is hard-coded to "no key needed".
        # OpenRouter included: its key really exists (in the .env), so it shows
        # "configured (key from .env/environment)", not the old misleading pill.
        bridged_rows = pi_bridge_rows.get(name) or []
        key_present = _provider_key_present(name, cfg) or bool(bridged_rows)
        note: str | None = None

        # Catalog-only providers (no live model API wired here): no fetch happened,
        # so render the key-presence status + an honest note, with no fabricated rows.
        if name in _CATALOG_ONLY:
            meta = _CATALOG_ONLY[name]
            if key_present:
                note = meta["note_configured"]
            else:
                note = f"Not configured — add the {meta['requires']} in System."
            groups.append(
                {
                    "provider": name,
                    "label": PROVIDER_LABEL.get(name, name.title()),
                    "count": 0,
                    "configured": key_present,
                    "note": note,
                    "balance": balances.get(name),
                    "models": [],
                }
            )
            continue

        # Fetcher providers.
        _fetcher, hint = _PROVIDER_FETCHERS.get(name, (None, None))
        rows = raw.get(name)
        configured = key_present

        if name in errors:
            # A key is present but the live fetch failed — keep it "configured"
            # (the key/account exists) and surface the fetch error as the note.
            if bridged_rows:
                rows = bridged_rows
                note = "Configured via PI extension login — model list from host PI."
            else:
                note = errors[name]
                rows = []
        elif rows is None:
            # The fetcher skipped (no key in the console store, or for Fireworks no
            # key+account). It can still be configured via env/.env (key_present);
            # if so, say models load via the API rather than "not configured".
            if bridged_rows:
                rows = bridged_rows
                note = "Configured via PI extension login — model list from host PI."
            else:
                rows = []
                if key_present:
                    note = (
                        "Configured (key from .env/environment) — model list isn't "
                        "fetched for this provider here."
                    )
                else:
                    note = (
                        f"Not configured — add the {hint} in System." if hint else None
                    )
        elif not rows and bridged_rows:
            rows = bridged_rows
            note = "Configured via PI extension login — model list from host PI."

        # Fill pricing/context/reasoning gaps for the key-only providers from
        # the OpenRouter cross-reference (OpenRouter rows are already complete).
        if name != "openrouter" and rows:
            rows = [_apply_supplement(dict(r), xref) for r in rows]

        # B2: as the LAST step, fill any still-empty `reasoning_levels` from the
        # connector registry's curated per-model map (live API + OR xref win; the
        # curated map is the fallback for providers whose /models is silent on
        # reasoning). Applies to every provider INCLUDING openrouter (a bare
        # ["supported"] there gets a concrete ladder when the connector knows one).
        if rows:
            rows = [_apply_curated_reasoning(dict(r)) for r in rows]

        rows = _sort_models(rows)
        total += len(rows)
        groups.append(
            {
                "provider": name,
                "label": PROVIDER_LABEL.get(name, name.title()),
                "count": len(rows),
                "configured": configured,
                "note": note,
                "balance": balances.get(name),
                "models": rows,
            }
        )

    return {
        "groups": groups,
        "fetched_at": time.time(),
        "total": total,
        "notes": notes,
        "openrouter_count": len(or_rows),
    }


def _explain(provider: str, exc: Exception) -> str:
    """Turn a fetch exception into a short, human, non-leaky note for the UI."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"auth rejected (HTTP {code}) — check the {PROVIDER_LABEL.get(provider, provider)} key in System."
        if code == 404:
            return "endpoint not found (HTTP 404) — check provider config."
        return f"provider returned HTTP {code}."
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out."
    if isinstance(exc, httpx.HTTPError):
        return "network error reaching the provider."
    return "unexpected response."


# ---------------------------------------------------------------------------
#  In-memory cache (~15 min TTL) + public accessor
# ---------------------------------------------------------------------------

# Process-wide cache. Keyed by a snapshot of the provider config so a key change
# in System (which alters which providers fetch) invalidates the cache naturally.
_cache: dict[str, Any] = {
    "catalog": None,   # last good catalog dict
    "expires": 0.0,    # epoch when the cache goes stale
    "config_key": None,  # config fingerprint the cache was built under
}
_lock = asyncio.Lock()


def _config_fingerprint(cfg: dict[str, Any]) -> str:
    """A cheap fingerprint over the provider-relevant settings. We never include
    raw secret VALUES — only whether each provider is configured (a boolean from
    _provider_key_present, which reads store/env/.env) — so the change that matters
    (configured ↔ not, for ANY provider) flips the fingerprint without ever caching
    a secret in the cache key. The non-secret Fireworks account id is folded in via
    its presence already (part of the fireworks required-keys set)."""
    parts = [
        f"{name}:{'1' if _provider_key_present(name, cfg) else '0'}"
        for name in PROVIDER_ORDER
    ]
    return "|".join(parts)


async def get_catalog(force: bool = False) -> dict[str, Any]:
    """Return the provider/model catalog, using the ~15-min in-memory cache.

    Reads the current provider config from the console-local settings store,
    fetches live (OpenRouter always; key-configured providers too), merges, and
    caches. On a fetch that yields nothing AND a prior good cache exists, the
    cache is retained (so a transient outage doesn't blank a working page).

    `force=True` bypasses the TTL (used by an explicit refresh). Never raises.
    """
    # load_with_secrets(), NOT load(): the catalog's per-provider fetchers resolve a key from this
    # cfg, and normalize() inside load() DROPS provider keys — so with bare load() every keyed
    # provider (ollama-cloud, fireworks, …) saw no key and returned no models, leaving only
    # OpenRouter (keyless public list) in the catalog. Using load_with_secrets() also means the
    # fingerprint below changes when a key is added, so configuring a provider re-fetches its models.
    cfg = settings_store.load_with_secrets()
    fp = _config_fingerprint(cfg)
    now = time.time()

    cached = _cache.get("catalog")
    if (
        not force
        and cached is not None
        and _cache.get("config_key") == fp
        and now < _cache.get("expires", 0.0)
    ):
        return _with_cache_meta(cached, now, cached=True)

    async with _lock:
        # Re-check inside the lock (another request may have just refreshed).
        cached = _cache.get("catalog")
        if (
            not force
            and cached is not None
            and _cache.get("config_key") == fp
            and time.time() < _cache.get("expires", 0.0)
        ):
            return _with_cache_meta(cached, time.time(), cached=True)

        catalog = await _build_catalog(cfg)

        # If the live build came back totally empty (e.g. even OpenRouter was
        # unreachable) but we still hold a usable cache, keep serving it with a
        # staleness note rather than regressing to nothing.
        if catalog.get("total", 0) == 0 and catalog.get("openrouter_count", 0) == 0:
            if cached is not None and cached.get("total", 0) > 0:
                stale = dict(cached)
                notes = list(stale.get("notes") or [])
                notes.insert(0, "Live refresh failed — showing the last cached catalog.")
                stale["notes"] = notes
                return _with_cache_meta(stale, time.time(), cached=True)

        _cache["catalog"] = catalog
        _cache["config_key"] = fp
        _cache["expires"] = time.time() + CACHE_TTL_SECONDS
        return _with_cache_meta(catalog, time.time(), cached=False)


async def refresh_catalog_forever(
    *,
    interval_s: int = CATALOG_REFRESH_INTERVAL_SECONDS,
    get: Any = None,
    sleep: Any = None,
    log: Any = None,
    _max_iters: int | None = None,
) -> None:
    """Periodic background loop that force-refreshes the model/price catalog.

    Refreshes once on start (warms the cache for the first request) then every
    ``interval_s`` seconds, force-bypassing the 15-min access TTL so new models and
    price changes land even when nobody opens the picker. Never raises — a fetch failure
    is logged and the loop waits for the next tick (the on-access TTL still covers
    freshness in the meantime). ``get``/``sleep``/``_max_iters`` are injection seams for
    the unit test; production uses the real ``get_catalog`` and ``asyncio.sleep``.

    ponytail: refresh-then-sleep means a flapping console re-fetches on each boot; fine
    here (one fetch per process start, get_catalog serves stale on failure). Add a
    persisted "last refreshed" gate if restart frequency ever makes that matter.
    """
    get = get or get_catalog
    sleep = sleep or asyncio.sleep
    n = 0
    while _max_iters is None or n < _max_iters:
        try:
            cat = await get(force=True)
            if log is not None:
                log.info("catalog daily refresh: %s models live", cat.get("total", "?"))
        except Exception as exc:  # never let the loop die — retry next tick
            if log is not None:
                log.warning(
                    "catalog daily refresh failed (retrying next tick): %s", exc
                )
        await sleep(interval_s)
        n += 1


def _with_cache_meta(catalog: dict[str, Any], now: float, cached: bool) -> dict[str, Any]:
    """Attach render-time cache metadata (age + freshness) without mutating the
    stored catalog. The template shows 'updated Ns ago · cached/live'."""
    out = dict(catalog)
    fetched = out.get("fetched_at") or now
    age = max(0, int(now - fetched))
    out["age_seconds"] = age
    out["age_human"] = _humanize_age(age)
    out["from_cache"] = cached
    out["ttl_seconds"] = CACHE_TTL_SECONDS
    return out


def _humanize_age(seconds: int) -> str:
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    mins = seconds // 60
    return f"{mins}m ago"


def reset_cache() -> None:
    """Drop the cached catalog (used by tests / a hard refresh path)."""
    _cache["catalog"] = None
    _cache["expires"] = 0.0
    _cache["config_key"] = None


def cached_reasoning_levels(provider: str, model: str | None) -> list[str] | None:
    """Return cached live effort metadata for one provider model.

    ``None`` means no cached model row is available and callers may use their
    curated outage fallback. An explicit ``[]`` is authoritative: the live row
    advertises no selectable reasoning levels.
    """
    catalog = _cache.get("catalog")
    if not isinstance(catalog, dict) or not model:
        return None
    provider_key = (provider or "").strip().lower()
    model_id = str(model).strip()
    for group in catalog.get("groups") or []:
        if not isinstance(group, dict):
            continue
        if str(group.get("provider") or "").strip().lower() != provider_key:
            continue
        for row in group.get("models") or []:
            if not isinstance(row, dict) or str(row.get("id") or "") != model_id:
                continue
            if "reasoning_levels" not in row:
                return None
            return list(row.get("reasoning_levels") or [])
    return None


# ---------------------------------------------------------------------------
#  View model — shape the catalog into render-ready strings (no template logic)
# ---------------------------------------------------------------------------
#
# Per-brand inline SVG marks (stroke/fill="currentColor" so they inherit the
# heading color). Clean lettermark-style glyphs — NOT plain letter boxes —
# matched to the console's mono/teal language. Anthropic gets its starburst,
# OpenAI its knot, OpenRouter a routing-fork, Fireworks a flame.

_BRAND_SVG: dict[str, str] = {
    "anthropic": (
        '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
        '<path d="M14.6 3h-2.9L17.9 21h3.1L14.6 3zm-7.2 0L1 21h3.16l1.27-3.5h6.6L13.3 21h3.16'
        'L10.06 3H7.4zm-1.1 11.6 2.18-6 2.18 6H6.3z"/></svg>'
    ),
    "openai": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
        'aria-hidden="true"><path d="M12 3.2a3 3 0 0 1 2.6 1.5 3 3 0 0 1 3.4 4.2 3 3 0 0 1 '
        '-1 4.9 3 3 0 0 1-2.6 4.6A3 3 0 0 1 12 20.8a3 3 0 0 1-2.6-1.4 3 3 0 0 1-3.4-4.2 3 3 0 '
        '0 1 1-4.9 3 3 0 0 1 2.6-4.6A3 3 0 0 1 12 3.2z"/><path d="M12 8.4 15 10v3.6L12 15.6 9 '
        '13.6V10l3-1.6z"/></svg>'
    ),
    "openrouter": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><circle cx="5" cy="12" r="2.2"/><circle cx="19" cy="6" r="2.2"/>'
        '<circle cx="19" cy="18" r="2.2"/><path d="M7.1 11 17 6.4M7.1 13 17 17.6"/></svg>'
    ),
    "fireworks": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
        'aria-hidden="true"><path d="M13 2.5c.6 3-1.2 4.3-2.6 5.6C8.7 9.7 7 11.2 7 14a5 5 0 0 0 '
        '10 .2c0-2-1-3.6-1.8-4.7-.3 .8-.9 1.4-1.7 1.6.6-2.6-.2-5.6-2.5-8.6z"/>'
        '<path d="M12 14.2c.4 1 1.3 1.3 1.3 2.3a1.3 1.3 0 0 1-2.6 0c0-1 .9-1.4 1.3-2.3z"/></svg>'
    ),
    # New providers — distinct, lettermark-free geometric marks in the same stroke
    # language as the originals (so the catalog reads consistently).
    "groq": (  # fast-forward chevrons (speed)
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'aria-hidden="true"><path d="M4 6l6 6-6 6M12 6l6 6-6 6"/></svg>'
    ),
    "siliconflow": (  # silicon die / chip
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5"/>'
        '<path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3"/></svg>'
    ),
    "dashscope": (  # cloud (Alibaba Cloud / Model Studio)
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><path d="M7 18a4 4 0 0 1-.5-7.97 5.5 5.5 0 0 1 10.6-1.06A3.75 3.75 0 0 1 17 18z"/></svg>'
    ),
    "alibaba-cloud": (  # cloud (Alibaba Cloud / Model Studio)
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><path d="M7 18a4 4 0 0 1-.5-7.97 5.5 5.5 0 0 1 10.6-1.06A3.75 3.75 0 0 1 17 18z"/></svg>'
    ),
    "deepseek": (  # diving whale arc
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><path d="M3 9c4 0 4 7 9 7s6-5 9-9"/><path d="M16 7c1 .4 2 1.2 2.5 2.3"/>'
        '<circle cx="7.5" cy="11.5" r=".9" fill="currentColor" stroke="none"/></svg>'
    ),
    "together": (  # interlocking links (together)
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><circle cx="9" cy="12" r="5"/><circle cx="15" cy="12" r="5"/></svg>'
    ),
    "bedrock": (  # layered rock strata
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 12l9 4 9-4M3 17l9 4 9-4"/></svg>'
    ),
    "cohere": (  # connected nodes (coherence)
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><circle cx="6" cy="6" r="2.2"/><circle cx="18" cy="6" r="2.2"/>'
        '<circle cx="12" cy="18" r="2.2"/><path d="M7.6 7.6 10.8 16M16.4 7.6 13.2 16M8 6h8"/></svg>'
    ),
    "inception": (  # nested squares (recursion / inception)
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
        'aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/>'
        '<rect x="7" y="7" width="10" height="10" rx="1.5"/><rect x="10.5" y="10.5" width="3" height="3" rx=".6"/></svg>'
    ),
    "moonshot": (  # crescent moon
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.3 6.3 0 0 0 10.5 10.5z"/></svg>'
    ),
    "perplexity": (  # query spark / search pulse
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M11 7v8M7 11h8M16.5 16.5 21 21"/></svg>'
    ),
    "xai": (  # x crossbars
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
        'aria-hidden="true"><path d="M5 5l14 14M19 5 5 19"/></svg>'
    ),
}

# A neutral fallback glyph for any provider without a specific brand mark, so a
# heading never renders an empty mark box.
_BRAND_SVG_FALLBACK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
    'aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v10M7 12h10"/></svg>'
)


def brand_svg(provider: str) -> str:
    """Inline SVG mark for a provider heading. Falls back to a neutral glyph for a
    provider without a specific mark (never an empty mark)."""
    return _BRAND_SVG.get(provider, _BRAND_SVG_FALLBACK)


def _fmt_price(v: float | None) -> str:
    """USD per-Mtok → a compact '$3.00' / '$0.15' / '$0.075' string, or '—'."""
    if v is None:
        return "—"
    if v == 0:
        return "free"
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:.3f}".rstrip("0").rstrip(".")
    # very small per-Mtok prices — keep 4 sig figs without trailing zero noise
    return f"${v:.4f}".rstrip("0").rstrip(".")


def _fmt_window(n: int | None) -> str:
    """Token window → '200K' / '1M' / '1.05M', or '—' when unknown."""
    if n is None:
        return "—"
    if n >= 1_000_000:
        m = n / 1_000_000
        return f"{m:.0f}M" if abs(m - round(m)) < 0.05 else f"{m:.2f}M"
    if n >= 1_000:
        k = n / 1_000
        return f"{k:.0f}K" if abs(k - round(k)) < 0.05 else f"{k:.1f}K"
    return str(n)


def _fmt_reasoning(levels: list[str]) -> str:
    """Reasoning tiers → a short label: 'low · med · high · max', 'yes', or '—'."""
    if not levels:
        return "—"
    if levels == ["supported"]:
        return "yes"
    short = {"medium": "med", "xhigh": "xhi"}
    return " · ".join(short.get(x, x) for x in levels)


# Per-row source → small tag {label, css} the template renders verbatim.
_SOURCE_TAG = {
    "live": {"label": "live", "css": "live"},
    "merged": {"label": "live + suppl.", "css": "merged"},
    "supplement": {"label": "supplement", "css": "supplement"},
}


def _row_view(m: dict[str, Any]) -> dict[str, Any]:
    """Flatten one UnifiedModel into the exact fields the table row renders."""
    return {
        "id": m["id"],
        "display_name": m.get("display_name") or m["id"],
        "type": m.get("type") or "chat",
        "reasoning": _fmt_reasoning(m.get("reasoning_levels") or []),
        "has_reasoning": bool(m.get("reasoning_levels")),
        # raw per-model levels (B3): the kaidera agent-config reasoning dropdown
        # reads THIS to show the selected model's own levels instead of the fixed
        # per-harness set. ["supported"] = reasons but no selectable ladder.
        "reasoning_levels": list(m.get("reasoning_levels") or []),
        "price_in": _fmt_price(m.get("price_in_per_mtok")),
        "price_out": _fmt_price(m.get("price_out_per_mtok")),
        "window": _fmt_window(m.get("context_window")),
        "max_output": _fmt_window(m.get("max_output")),
        "source": m.get("source") or "live",
        "source_tag": _SOURCE_TAG.get(m.get("source") or "live", _SOURCE_TAG["live"]),
        "deprecated": bool(m.get("deprecated")),
    }


def view_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw catalog (from get_catalog) into a render-ready view model.

    Each group gains its brand SVG + a flattened, display-formatted `rows` list;
    the catalog gains a 'configured providers' count for the header. The template
    iterates this with zero formatting logic of its own."""
    groups_out: list[dict[str, Any]] = []
    configured_n = 0
    for g in catalog.get("groups", []):
        if g.get("configured"):
            configured_n += 1
        groups_out.append(
            {
                "provider": g["provider"],
                "label": g["label"],
                "count": g["count"],
                "configured": g.get("configured", False),
                "note": g.get("note"),
                "balance": g.get("balance"),
                "brand_svg": brand_svg(g["provider"]),
                "rows": [_row_view(m) for m in g.get("models", [])],
            }
        )
    return {
        "groups": groups_out,
        "total": catalog.get("total", 0),
        "configured_count": configured_n,
        "notes": catalog.get("notes") or [],
        "age_human": catalog.get("age_human", ""),
        "from_cache": catalog.get("from_cache", False),
        "openrouter_count": catalog.get("openrouter_count", 0),
    }


# ---------------------------------------------------------------------------
#  Model → provider + pricing resolver (R7 Analytics)
# ---------------------------------------------------------------------------
#
# The Analytics view needs, for an arbitrary agent-configured model id (which can
# be a bare native id like "claude-opus-4-7" / "gpt-5.5", a dated id like
# "claude-haiku-4-5-20251001", or a claude-code subscription alias like "opus"),
# the model's UPSTREAM PROVIDER and per-Mtok pricing. We resolve both off the
# already-built OpenRouter cross-reference (the same machinery that fills the
# key-only providers' pricing): OpenRouter slugs are namespaced
# "<provider>/<model>", so the slug prefix IS the authoritative upstream provider
# (the matched row's own `.provider` is always "openrouter" — its source — so we
# must read the prefix, NOT that field). Pricing comes straight off the matched
# row. Nothing here fetches; it reuses the cached catalog the caller passes in.

# claude-code subscription shorthands (harness.HARNESS_MODELS["claude-code"]) are
# NOT catalog ids, so the xref can't match them. Map the bare aliases to a
# representative current OpenRouter slug so a subscription-lane agent configured
# as "opus"/"sonnet"/"haiku" still resolves to a provider + indicative price.
# (Best-effort indicative pricing — the subscription lane is not metered per call.)
_ALIAS_TO_SLUG: dict[str, str] = {
    "opus": "anthropic/claude-opus-4.8",
    "sonnet": "anthropic/claude-sonnet-4.6",
    "haiku": "anthropic/claude-haiku-4.5",
}

# Fallback provider inference from a bare model id when the OpenRouter xref has
# no match at all (e.g. an offline catalog). Keeps the by-provider grouping
# honest-ish rather than dumping everything into "other".
_PROVIDER_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("claude", "anthropic"), "anthropic"),
    (("gpt", "o1", "o3", "o4", "openai", "davinci", "codex"), "openai"),
    (("gemini", "google", "palm"), "google"),
    (("llama", "meta"), "meta"),
    (("mistral", "mixtral", "magistral"), "mistral"),
    (("qwen",), "qwen"),
    (("deepseek",), "deepseek"),
    (("grok",), "x-ai"),
    (("gemma",), "google"),
    (("kimi", "moonshot"), "moonshotai"),
]

# Display labels for upstream providers (superset of PROVIDER_LABEL — the OR
# slug namespace is broader than the four list-endpoint providers).
_UPSTREAM_LABEL: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "meta-llama": "Meta",
    "meta": "Meta",
    "mistralai": "Mistral",
    "mistral": "Mistral",
    "qwen": "Qwen",
    "deepseek": "DeepSeek",
    "x-ai": "xAI",
    "moonshotai": "Moonshot",
    "fireworks": "Fireworks",
    "openrouter": "OpenRouter",
}


def provider_label(provider: str | None) -> str:
    """Human label for an upstream provider key (slug namespace or hint)."""
    if not provider:
        return "—"
    return _UPSTREAM_LABEL.get(provider, provider.replace("-", " ").title())


def _provider_from_match(matched_id: str | None, native_id: str) -> str | None:
    """Upstream provider for a resolved model. Prefers the OpenRouter slug prefix
    of the MATCHED id ("anthropic/claude-…" → "anthropic"); else a bare-id hint."""
    if matched_id and "/" in matched_id:
        return matched_id.split("/", 1)[0].lower()
    low = (native_id or "").lower()
    for needles, prov in _PROVIDER_HINTS:
        if any(n in low for n in needles):
            return prov
    return None


def pricing_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a reusable model-resolution index from a raw catalog (get_catalog()).

    Returns the OpenRouter cross-reference map (native-id/slug/normalized keys →
    UnifiedModel row) that resolve_model() looks up against. Built once per
    Analytics render and shared across every agent lookup. Empty dict if the
    OpenRouter list was unavailable (every resolve then returns the no-data shape)."""
    or_rows = [
        m
        for g in catalog.get("groups", [])
        if g.get("provider") == "openrouter"
        for m in g.get("models", [])
    ]
    return _build_openrouter_xref(or_rows)


def resolve_model(
    model_id: str | None, index: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Resolve an agent-configured model id to its upstream provider + per-Mtok
    pricing, using a pricing_index().

    Returns:
        {
          "model_id": the input id (or None),
          "provider": upstream provider key (slug prefix / hint) or None,
          "provider_label": display label,
          "price_in_per_mtok": float | None,
          "price_out_per_mtok": float | None,
          "matched_id": the OpenRouter slug we matched (or None),
          "resolved": bool,            # did we find a catalog match at all
          "priced": bool,              # do we have BOTH in+out prices
        }
    Unknown / unmatched ids degrade to provider/price None (caller shows n/a)."""
    mid = (model_id or "").strip()
    if not mid:
        return _empty_resolution(None)

    # claude-code subscription aliases → representative slug before lookup.
    lookup_id = _ALIAS_TO_SLUG.get(mid.lower(), mid)
    match = _xref_lookup(lookup_id, index) if index else None

    matched_id = match["id"] if match else (
        lookup_id if lookup_id != mid else None  # alias gives a slug even w/o a row
    )
    provider = _provider_from_match(matched_id, mid)
    price_in = match.get("price_in_per_mtok") if match else None
    price_out = match.get("price_out_per_mtok") if match else None

    return {
        "model_id": mid,
        "provider": provider,
        "provider_label": provider_label(provider),
        "price_in_per_mtok": price_in,
        "price_out_per_mtok": price_out,
        "matched_id": match["id"] if match else None,
        "resolved": match is not None,
        "priced": price_in is not None and price_out is not None,
    }


def _empty_resolution(model_id: str | None) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "provider": None,
        "provider_label": "—",
        "price_in_per_mtok": None,
        "price_out_per_mtok": None,
        "matched_id": None,
        "resolved": False,
        "priced": False,
    }


def fmt_cost(v: float | None) -> str:
    """USD cost → a compact '$12.40' / '$0.83' / '$0.0021' string, or 'n/a'.

    Distinct from _fmt_price (per-Mtok rates): this is an absolute dollar amount,
    so we keep cents for normal values and extend precision for sub-cent sums."""
    if v is None:
        return "n/a"
    if v == 0:
        return "$0.00"
    if v >= 0.01:
        return f"${v:,.2f}"
    if v >= 0.0001:
        return f"${v:.4f}".rstrip("0").rstrip(".")
    return "<$0.0001"
