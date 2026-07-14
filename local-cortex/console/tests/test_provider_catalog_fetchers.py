from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_fireworks_fetcher_uses_inference_models_when_no_account(monkeypatch):
    from app import providers

    calls = []

    async def fake_fetch_json(_client, url, headers=None):
        calls.append((url, headers or {}))
        return {"data": [{"id": "accounts/fireworks/models/kimi-k2p6"}]}

    monkeypatch.setattr(providers, "_fetch_json", fake_fetch_json)

    rows = await providers._fetch_fireworks(object(), {"fireworks_api_key": "fw-key"})

    assert calls == [
        (
            "https://api.fireworks.ai/v1/accounts/fireworks/models"
            "?pageSize=200&filter=supports_serverless%20%3D%20true",
            {"Authorization": "Bearer fw-key"},
        )
    ]
    assert rows[0]["provider"] == "fireworks"
    assert rows[0]["id"] == "accounts/fireworks/models/kimi-k2p6"


@pytest.mark.asyncio
async def test_fireworks_fetcher_uses_account_models_when_account_present(monkeypatch):
    from app import providers

    calls = []

    async def fake_fetch_json(_client, url, headers=None):
        calls.append((url, headers or {}))
        return {"models": [{"name": "accounts/me/models/custom-a", "displayName": "Custom A"}]}

    monkeypatch.setattr(providers, "_fetch_json", fake_fetch_json)

    rows = await providers._fetch_fireworks(
        object(),
        {"fireworks_api_key": "fw-key", "fireworks_account_id": "me"},
    )

    assert calls == [
        (
            "https://api.fireworks.ai/v1/accounts/me/models?pageSize=200",
            {"Authorization": "Bearer fw-key"},
        )
    ]
    assert rows[0]["id"] == "accounts/me/models/custom-a"
    assert rows[0]["display_name"] == "Custom A"


def test_resolve_provider_key_accepts_ollama_api_key_alias(monkeypatch):
    from app import providers

    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-alias-key")
    monkeypatch.setattr(providers, "_env_file_value", lambda _var: "")

    assert providers._resolve_provider_key({}, "ollama_cloud_api_key") == "ollama-alias-key"

@pytest.mark.asyncio
async def test_refresh_catalog_forever_force_refreshes_each_tick():
    """The daily loop force-refreshes (bypassing the TTL) once per tick and stops cleanly."""
    from app import providers

    calls = []

    async def fake_get(force=False):
        calls.append(force)
        return {"total": 7}

    async def fake_sleep(_s):  # no real wait
        return None

    await providers.refresh_catalog_forever(
        get=fake_get, sleep=fake_sleep, _max_iters=3
    )

    assert calls == [True, True, True]  # always force=True, once per tick


@pytest.mark.asyncio
async def test_refresh_catalog_forever_survives_a_fetch_error():
    """A failing refresh is swallowed — the loop keeps ticking, never crashes the console."""
    from app import providers

    ticks = []

    async def boom(force=False):
        ticks.append(force)
        raise RuntimeError("provider down")

    async def fake_sleep(_s):
        return None

    # Must return normally (not raise) despite every get() raising.
    await providers.refresh_catalog_forever(
        get=boom, sleep=fake_sleep, _max_iters=2
    )

    assert ticks == [True, True]  # both ticks ran past the exception

# -- #133 account balance parsers --------------------------------------------

def test_parse_openrouter_balance_remaining():
    from app import providers
    # The verified live shape: remaining = total_credits - total_usage.
    b = providers._parse_openrouter_balance({"data": {"total_credits": 410, "total_usage": 146.15}})
    assert b["amount"] == 263.85
    assert b["currency"] == "USD"
    assert b["display"] == "$263.85"
    assert "146" in b["detail"] and "410" in b["detail"]


def test_parse_openrouter_balance_junk_is_none():
    from app import providers
    assert providers._parse_openrouter_balance({}) is None
    assert providers._parse_openrouter_balance({"data": {}}) is None
    assert providers._parse_openrouter_balance({"data": "nope"}) is None


def test_parse_deepseek_balance_prefers_usd():
    from app import providers
    b = providers._parse_deepseek_balance({
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "700.00"},
            {"currency": "USD", "total_balance": "98.40"},
        ],
    })
    assert b["amount"] == 98.4 and b["currency"] == "USD" and b["display"] == "$98.40"


def test_parse_moonshot_balance():
    from app import providers
    b = providers._parse_moonshot_balance({"data": {"available_balance": 49.6}})
    assert b["amount"] == 49.6 and b["display"] == "$49.60"
    assert providers._parse_moonshot_balance({"data": {}}) is None


@pytest.mark.asyncio
async def test_fetch_balance_no_endpoint_or_no_key_is_none(monkeypatch):
    from app import providers
    # A provider with no balance endpoint → None (never calls out).
    assert await providers._fetch_balance(object(), "anthropic", {}) is None
    # Has an endpoint but the key resolves empty → None, no network call (object() client
    # would crash if reached). monkeypatch the resolver so it's deterministic, not .env-dependent.
    monkeypatch.setattr(providers, "_resolve_provider_key", lambda cfg, field: "")
    assert await providers._fetch_balance(object(), "openrouter", {}) is None


# ---------------------------------------------------------------------------
#  B2 DISCOVERY — per-model reasoning levels (live OpenRouter + curated map)
# ---------------------------------------------------------------------------

def test_openrouter_reasoning_prefers_supported_efforts():
    """OpenRouter's REAL effort ladder (reasoning.supported_efforts[]) wins over
    the bare `reasoning` boolean — sorted into canonical low→high order."""
    from app import providers

    m = {"id": "x/y", "reasoning": {"supported_efforts": ["high", "low", "medium"]}}
    assert providers._openrouter_reasoning(m, ["reasoning"]) == ["low", "medium", "high"]


def test_openrouter_reasoning_falls_back_to_boolean_then_empty():
    from app import providers

    assert providers._openrouter_reasoning({"id": "x/y"}, ["reasoning"]) == ["supported"]
    assert providers._openrouter_reasoning({"id": "x/y"}, ["tools"]) == []


def test_parse_openrouter_carries_supported_efforts_levels():
    from app import providers

    rows = providers._parse_openrouter(
        {"data": [{"id": "openai/gpt-5.5", "name": "GPT-5.5",
                   "reasoning": {"supported_efforts": ["low", "high", "medium"]}}]}
    )
    assert rows[0]["reasoning_levels"] == ["low", "medium", "high"]


def test_apply_curated_reasoning_fills_empty_from_connector_map():
    """A row with no live levels gets the connector registry's curated ladder."""
    from app import providers

    r = providers._apply_curated_reasoning({"provider": "openai", "id": "gpt-5.5", "reasoning_levels": []})
    assert r["reasoning_levels"] == ["minimal", "low", "medium", "high", "xhigh"]


def test_apply_curated_reasoning_clears_stale_placeholder_for_known_non_reasoner():
    from app import providers

    # grok-4 is a known non-reasoner → a stale ["supported"] is cleared to [].
    r = providers._apply_curated_reasoning(
        {"provider": "xai", "id": "grok-4-0709", "reasoning_levels": ["supported"]}
    )
    assert r["reasoning_levels"] == []


def test_apply_curated_reasoning_leaves_unknown_provider_untouched():
    from app import providers

    # cohere isn't in the registry → never clear its data (no authority to).
    r = providers._apply_curated_reasoning(
        {"provider": "cohere", "id": "command-r", "reasoning_levels": ["supported"]}
    )
    assert r["reasoning_levels"] == ["supported"]


def test_apply_curated_reasoning_keeps_live_ladder():
    from app import providers

    # a row that already has concrete levels (Anthropic effort tree) is left alone.
    r = providers._apply_curated_reasoning(
        {"provider": "anthropic", "id": "claude-opus-4-8",
         "reasoning_levels": ["low", "medium", "high", "max", "xhigh"]}
    )
    assert r["reasoning_levels"] == ["low", "medium", "high", "max", "xhigh"]


def test_apply_curated_reasoning_toggle_provider_marks_supported():
    from app import providers

    # deepseek is a binary-toggle provider (no ladder) → mark it supported so the
    # UI shows it as reasoning-capable.
    r = providers._apply_curated_reasoning({"provider": "deepseek", "id": "deepseek-v4", "reasoning_levels": []})
    assert r["reasoning_levels"] == ["supported"]


def test_row_view_exposes_raw_reasoning_levels_for_b3():
    """_row_view must carry the RAW per-model levels (B3 dropdown), not just the
    formatted display string."""
    from app import providers

    rv = providers._row_view({"id": "gpt-5.5", "reasoning_levels": ["low", "medium", "high"]})
    assert rv["reasoning_levels"] == ["low", "medium", "high"]
    assert rv["has_reasoning"] is True


# -- Alibaba Cloud provider: key persists + live /models (v0.1.163 fix) ---------

def test_alibaba_cloud_key_persists_and_is_testable():
    """Issue 1: the UI-saved key must round-trip — it has to be a known secret
    (else load_with_secrets drops it and the key-test says 'no key stored')."""
    from app import settings
    assert "alibaba_cloud_api_key" in settings.PROVIDER_SECRET_KEYS


def test_alibaba_cloud_fetches_live_models_not_catalog_only():
    """Issue 2: alibaba-cloud is wired as a live OpenAI-compatible /models fetcher
    (moved OUT of _CATALOG_ONLY), so its models appear in the catalog."""
    from app import providers
    assert "alibaba-cloud" in providers._OPENAI_COMPAT
    assert "alibaba-cloud" in providers._PROVIDER_FETCHERS
    assert "alibaba-cloud" not in providers._CATALOG_ONLY
    setting_key, url = providers._OPENAI_COMPAT["alibaba-cloud"]
    assert setting_key == "alibaba_cloud_api_key"
    assert url.endswith("/compatible-mode/v1/models")
