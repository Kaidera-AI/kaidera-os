"""Feature #113 — Ollama Cloud as a PRECONFIGURED provider.

The CTO wants Ollama Cloud in the Providers tab (they'll add the key). It's an
OpenAI-compatible hosted API: the base is `https://ollama.com` with an `/v1`
OpenAI-compatible path + a Bearer key.

This wires it as a BUILT-IN provider (the SAME shape as anthropic/openai/…):
  * `ollama_cloud_api_key` is a secret in `settings.PROVIDER_SECRET_KEYS` (owned by
    the Providers tab — the canonical home; NOT a System-schema field, so it isn't
    duplicated per the v0.1.48 canonicalization);
  * the provider is registered in `providers` (`PROVIDER_ORDER` / `PROVIDER_LABEL` /
    its setting-key + env-var maps) so `builtin_provider_config` emits it with
    key-presence + a Test target;
  * it's bearer-/models-testable via `provider_check` (`https://ollama.com/v1/models`).

It does NOT become its own console HARNESS lane — it's provider-only (keys + catalog),
exactly as the task allows. The model catalog populates from a stored/env/PI-auth key
when set (best-effort, via the OpenAI-compatible /v1/models endpoint).

Written BEFORE the implementation (STRICT TDD).
"""

from __future__ import annotations

import json

import pytest


OLLAMA_KEY = "ollama_cloud_api_key"
# A secret value the masking contract must NEVER leak into any view JSON.
SECRET_VALUE = "ollama-SUPER-SECRET-do-not-leak-9876543210"


# ---------------------------------------------------------------------------
#  1. settings — the secret key is canonical (Providers tab owns it; NOT in System).
# ---------------------------------------------------------------------------


def test_ollama_cloud_key_in_provider_secret_keys():
    """`ollama_cloud_api_key` is in the canonical PROVIDER_SECRET_KEYS set (so the
    Providers tab owns it + the raw editor excludes it)."""
    from app import settings as settings_store

    assert OLLAMA_KEY in settings_store.PROVIDER_SECRET_KEYS
    assert OLLAMA_KEY in settings_store.provider_secret_keys()


def test_ollama_cloud_key_not_a_system_schema_field():
    """CANONICALIZATION (v0.1.48): the provider key lives ONLY in the Providers tab,
    NEVER as a System-schema field — no duplicate surface."""
    from app import settings as settings_store

    schema_keys = {f["key"] for g in settings_store.SCHEMA for f in g["fields"]}
    assert OLLAMA_KEY not in schema_keys


def test_ollama_cloud_key_excluded_from_raw_editor():
    """The raw App-settings editor surface FILTERS OUT the Ollama Cloud key (it has a
    canonical home in Providers) — and the secret value never appears."""
    from app import settings as settings_store

    raw = {OLLAMA_KEY: SECRET_VALUE, "theme": "dark"}
    filtered = settings_store.filter_non_provider_settings(raw)
    assert OLLAMA_KEY not in filtered
    assert filtered["theme"] == "dark"
    assert SECRET_VALUE not in json.dumps(filtered)


# ---------------------------------------------------------------------------
#  2. providers — Ollama Cloud is a registered built-in with key-presence + label.
# ---------------------------------------------------------------------------


def test_ollama_cloud_registered_in_provider_order_and_label():
    """Ollama Cloud is registered in the provider order with its display label."""
    from app import providers

    assert "ollama-cloud" in providers.PROVIDER_ORDER
    assert providers.PROVIDER_LABEL["ollama-cloud"] == "Ollama Cloud"


def test_builtin_provider_config_lists_ollama_cloud_key_not_set(tmp_path, monkeypatch):
    """`builtin_provider_config` emits Ollama Cloud with `key_is_set=False` when no
    key is configured, its `key_field=ollama_cloud_api_key`, and `testable=True` (the
    OpenAI-compatible /v1/models probe can validate it). NEVER a raw key."""
    from app import providers

    # an explicit EMPTY store snapshot + isolated env/auth file → no key anywhere.
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("PI_AUTH_FILE", str(tmp_path / "missing-auth.json"))
    monkeypatch.setattr(providers, "_env_file_value", lambda _var: "")
    monkeypatch.setattr(providers, "_pi_bridge_provider_present", lambda _name: False)
    rows = providers.builtin_provider_config(cfg={})
    by_name = {r["name"]: r for r in rows}
    assert "ollama-cloud" in by_name
    row = by_name["ollama-cloud"]
    assert row["label"] == "Ollama Cloud"
    assert row["key_field"] == OLLAMA_KEY
    assert row["key_is_set"] is False
    assert row["testable"] is True
    assert "api_key" not in row  # never a raw key field


def test_builtin_provider_config_ollama_cloud_key_set_from_store():
    """When the key is present in the store snapshot, Ollama Cloud reads
    `key_is_set=True` — and the raw key never appears in the row."""
    from app import providers

    rows = providers.builtin_provider_config(cfg={OLLAMA_KEY: SECRET_VALUE})
    row = {r["name"]: r for r in rows}["ollama-cloud"]
    assert row["key_is_set"] is True
    assert SECRET_VALUE not in json.dumps(row)


# ---------------------------------------------------------------------------
#  3. provider_check — Ollama Cloud is bearer-/models-testable at the right base.
# ---------------------------------------------------------------------------


def test_ollama_cloud_is_testable():
    """The key field is recognised as a testable built-in (so the Providers tab shows
    its Test button)."""
    from app import provider_check

    assert provider_check.is_testable(OLLAMA_KEY) is True


def test_ollama_cloud_probe_uses_openai_compatible_v1_models_base():
    """The built-in test spec points at the OpenAI-compatible model-list endpoint
    `https://ollama.com/v1/models` with a Bearer key (the verified OpenAI-compat
    surface for Ollama Cloud)."""
    from app import provider_check

    spec = provider_check._BUILTIN[OLLAMA_KEY]
    assert spec["url"] == "https://ollama.com/v1/models"
    assert spec.get("auth", "bearer") == "bearer"
    assert spec["label"] == "Ollama Cloud"


def test_ollama_cloud_env_var_mapping():
    """The settings key maps to the conventional `OLLAMA_CLOUD_API_KEY` env var so a
    key in the real .env reads as configured (mirrors the other providers). The
    Ollama/PI extension alias `OLLAMA_API_KEY` is accepted too."""
    from app import provider_check
    from app import providers

    assert provider_check._ENV_VAR[OLLAMA_KEY] == "OLLAMA_CLOUD_API_KEY"
    assert providers._SETTING_ENV_VAR[OLLAMA_KEY] == "OLLAMA_CLOUD_API_KEY"
    assert "OLLAMA_API_KEY" in provider_check._env_vars_for_field(OLLAMA_KEY)
    assert "OLLAMA_API_KEY" in providers._env_vars_for_setting(OLLAMA_KEY)


# ---------------------------------------------------------------------------
#  4. the providers-config VIEW lists Ollama Cloud (the endpoint-level contract).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_providers_config_view_lists_ollama_cloud():
    """The Providers-config endpoint folds the real `builtin_provider_config` (which
    now includes Ollama Cloud) into the view — Ollama Cloud appears with its
    key-presence + provider_ref (its key field), and no secret leaks."""
    from tests.test_settings_module import FakeOpStore

    from app import providers
    from app.settings_module import api as settings_api

    class _CustomStore:
        def view_custom_providers(self):
            return []

    result = await settings_api.providers_config_endpoint(
        "kaidera-os",
        store=FakeOpStore(app_settings={}),
        cfg_source=providers,  # the REAL source — proves the registration is wired end-to-end
        custom_store=_CustomStore(),
    )
    rows = {r["name"]: r for r in result["providers"]}
    assert "ollama-cloud" in rows
    assert rows["ollama-cloud"]["label"] == "Ollama Cloud"
    assert rows["ollama-cloud"]["provider_ref"] == OLLAMA_KEY
    assert rows["ollama-cloud"]["is_custom"] is False
    assert SECRET_VALUE not in json.dumps(result)


# ---------------------------------------------------------------------------
#  5. the live catalog renders an Ollama Cloud GROUP; when a key is present it
#     enumerates models via the OpenAI-compatible /v1/models endpoint.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_groups_include_ollama_cloud_group():
    """The assembled catalog (`get_catalog`) includes an Ollama Cloud group — honest
    key-presence and a live model list when a usable key is available."""
    from app import providers

    # force a fresh assembly; the live OpenRouter/anthropic fetches degrade offline to
    # empty rows but the GROUP assembly from PROVIDER_ORDER still runs.
    catalog = await providers.get_catalog(force=True)
    groups = {g["provider"]: g for g in catalog.get("groups", [])}
    assert "ollama-cloud" in groups
    assert groups["ollama-cloud"]["label"] == "Ollama Cloud"
    assert isinstance(groups["ollama-cloud"]["models"], list)


def test_ollama_cloud_key_resolves_from_pi_auth_file(tmp_path, monkeypatch):
    """PI's Ollama Cloud extension stores API-key login in auth.json. The console
    can use that host-side login as a key source without exposing the key."""
    from app import providers

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"ollama-cloud": {"type": "api_key", "key": SECRET_VALUE}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_AUTH_FILE", str(auth))
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(providers, "_env_file_value", lambda _var: "")

    assert providers._resolve_provider_key({}, OLLAMA_KEY) == SECRET_VALUE


def test_ollama_cloud_key_resolves_from_pi_oauth_shaped_auth_file(tmp_path, monkeypatch):
    """PI's Ollama Cloud login is OAuth-shaped, but its access token is the API key.
    Kaidera AI should consume that same host login rather than requiring a duplicate
    Providers-tab key."""
    from app import provider_check
    from app import providers
    from app import settings as settings_store

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "ollama-cloud": {
                    "type": "oauth",
                    "access": SECRET_VALUE,
                    "refresh": "same-secret-fallback",
                    "expires": 4102444800000,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_AUTH_FILE", str(auth))
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(settings_store, "_read_raw", lambda: {})
    monkeypatch.setattr(providers, "_env_file_value", lambda _var: "")
    monkeypatch.setattr(provider_check, "_env_file_value", lambda _var: "")

    assert providers._resolve_provider_key({}, OLLAMA_KEY) == SECRET_VALUE
    assert provider_check._resolve_builtin_key(OLLAMA_KEY, None) == SECRET_VALUE
    assert providers._provider_key_present("ollama-cloud", {}) is True


@pytest.mark.asyncio
async def test_ollama_cloud_fetcher_uses_openai_compatible_models_endpoint(monkeypatch):
    """With a resolved key, Ollama Cloud is a real live-list provider for the
    catalog, parsed through the OpenAI-compatible model-list shape."""
    from app import providers

    calls = []

    async def fake_fetch_json(_client, url, headers=None):
        calls.append((url, headers or {}))
        return {"data": [{"id": "qwen3-coder:480b"}]}

    monkeypatch.setattr(providers, "_fetch_json", fake_fetch_json)
    fetcher = providers._make_openai_compat_fetcher("ollama-cloud")

    rows = await fetcher(object(), {OLLAMA_KEY: SECRET_VALUE})

    assert calls == [
        (
            "https://ollama.com/v1/models",
            {"Authorization": f"Bearer {SECRET_VALUE}"},
        )
    ]
    assert rows[0]["provider"] == "ollama-cloud"
    assert rows[0]["id"] == "qwen3-coder:480b"


def test_providers_config_marks_ollama_cloud_set_from_pi_bridge(monkeypatch):
    """Inside Docker the console may not have host ~/.pi mounted. The Providers
    config view should still mark Ollama Cloud configured when the host PI bridge
    reports provider rows."""
    from app import providers

    monkeypatch.setattr(providers, "_provider_key_present", lambda _name, _cfg: False)
    monkeypatch.setattr(
        providers,
        "_pi_bridge_provider_present",
        lambda name: name == "ollama-cloud",
    )

    rows = providers.builtin_provider_config(cfg={})
    row = {r["name"]: r for r in rows}["ollama-cloud"]

    assert row["key_is_set"] is True
    assert SECRET_VALUE not in json.dumps(row)


@pytest.mark.asyncio
async def test_catalog_uses_pi_bridge_models_when_local_key_is_absent(monkeypatch):
    """The Providers catalog should show Ollama Cloud models from the host PI bridge
    when the container has no direct key or host auth file."""
    from app import providers

    async def skipped_fetcher(_client, _cfg):
        return None

    async def fake_pi_groups(force: bool = False):
        return [
            {
                "provider": "ollama-cloud",
                "label": "Ollama Cloud",
                "rows": [
                    {
                        "id": "gpt-oss:120b",
                        "display_name": "gpt-oss:120b",
                        "type": "chat",
                    }
                ],
            }
        ]

    monkeypatch.setattr(
        providers,
        "_PROVIDER_FETCHERS",
        {"ollama-cloud": (skipped_fetcher, "Ollama Cloud API key")},
    )
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["ollama-cloud"])
    monkeypatch.setattr(providers, "_fetch_pi_bridge_groups", fake_pi_groups)
    monkeypatch.setattr(providers, "_provider_key_present", lambda _name, _cfg: False)

    catalog = await providers._build_catalog({})
    group = catalog["groups"][0]

    assert group["provider"] == "ollama-cloud"
    assert group["configured"] is True
    assert group["count"] == 1
    assert group["models"][0]["id"] == "gpt-oss:120b"
    assert group["models"][0]["source"] == "pi-bridge"
    assert SECRET_VALUE not in json.dumps(group)


@pytest.mark.asyncio
async def test_provider_key_test_accepts_pi_bridge_login(monkeypatch):
    """The Providers tab Test button should pass from PI bridge presence even when
    the container cannot resolve a raw key locally."""
    from app import provider_check
    from app import providers

    async def fake_group(provider: str):
        return {"provider": provider, "rows": [{"id": "gpt-oss:120b"}]}

    monkeypatch.setattr(provider_check, "_resolve_builtin_key", lambda _field, _value: "")
    monkeypatch.setattr(providers, "_pi_bridge_provider_group", fake_group)

    result = await provider_check.test_provider(OLLAMA_KEY)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert "PI extension login" in result["message"]
    assert SECRET_VALUE not in json.dumps(result)


# ---------------------------------------------------------------------------
#  6. REGRESSION (v0.1.85) — a key SAVED TO THE STORE must survive the read path.
#
#  The bug: provider keys are persisted RAW (upsert_app_settings) but every reader
#  pulled them through settings.load(), whose normalize() keeps only System-schema
#  keys and DROPS provider keys (they live outside the schema). So a freshly-saved
#  key resolved to "" on read: the Providers Test said "no key stored" right after a
#  successful save, and the kaidera call would authenticate with an empty key.
#  Every test ABOVE passes cfg={KEY: VALUE} DIRECTLY into the readers, bypassing the
#  load()/normalize() funnel — so none of them caught the drop. These exercise it.
# ---------------------------------------------------------------------------


def test_load_drops_provider_key_but_load_with_secrets_keeps_it(monkeypatch):
    """normalize() (inside load()) drops provider keys; load_with_secrets() overlays
    the raw provider-secret rows back, so a consumer sees the saved value. A
    non-provider schema key stays normalized through both."""
    from app import settings as settings_store

    # the raw store exactly as upsert_app_settings persists it: a provider key + a
    # normal schema key, side by side.
    monkeypatch.setattr(
        settings_store,
        "_read_raw",
        lambda: {OLLAMA_KEY: SECRET_VALUE, "harness_default": "claude-code"},
    )

    assert settings_store.load().get(OLLAMA_KEY) is None  # the bug surface
    assert settings_store.load_with_secrets().get(OLLAMA_KEY) == SECRET_VALUE  # fixed
    assert settings_store.load_with_secrets().get("harness_default") == "claude-code"


def test_resolve_builtin_key_finds_a_store_saved_key(monkeypatch):
    """The Providers Test resolves a key that was SAVED TO THE STORE — the exact
    save→test round-trip that was broken. _resolve_builtin_key must read through
    load_with_secrets(), not load(). No typed value, no env/.env/pi-auth: the store
    is the ONLY source."""
    from app import provider_check
    from app import settings as settings_store

    monkeypatch.setattr(settings_store, "_read_raw", lambda: {OLLAMA_KEY: SECRET_VALUE})
    monkeypatch.setattr(provider_check, "_env_file_value", lambda _var: "")
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    assert provider_check._resolve_builtin_key(OLLAMA_KEY, None) == SECRET_VALUE


def test_kaidera_runtime_cfg_carries_store_saved_provider_keys(monkeypatch):
    """The kaidera runtime cfg must carry provider keys (it reads
    load_with_secrets()), or a live call authenticates with an empty key. The catalog
    resolver then pulls the key straight out of that cfg."""
    from app import harness_runner
    from app import settings as settings_store

    monkeypatch.setattr(settings_store, "_read_raw", lambda: {OLLAMA_KEY: SECRET_VALUE})

    cfg, _customs, resolver = harness_runner._own_runtime_config()
    assert cfg.get(OLLAMA_KEY) == SECRET_VALUE
    assert resolver(cfg, OLLAMA_KEY) == SECRET_VALUE
