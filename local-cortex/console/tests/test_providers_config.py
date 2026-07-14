"""Track 2 — provider-keys CANONICALIZATION: the Providers tab becomes the ONE
home for provider keys/config (de-dup the System/Providers/raw-editor triplication).

The CTO flagged that the 12 provider API keys lived in THREE places at once: the
System schema (the `providers` group), the read-only Providers tab, AND the raw
App-settings editor. This canonicalizes: the Providers tab is the single control
surface for provider keys; System keeps only the NON-provider settings.

This file pins the NEW backend pieces that make the Providers tab the control
surface, plus the de-dup assertions:

  1. The pure `build_providers_config(built_ins, customs)` view — per-provider
     `{name, label, key_is_set, is_custom, base_url?, testable?}`, NEVER a raw key.
  2. `GET /settings/{project}/providers/config` — the endpoint that returns it
     (built-in provider key-presence via the schema/store + the masked custom list).
  3. DE-DUP: the System SCHEMA no longer carries the 12 provider secret fields
     (asserted gone from `system-schema`); the System app-settings surface excludes
     the provider secret keys from the raw editor (they have a canonical home now).

STRICT TDD + the settings_module style: the pure service is driven with FAKES
(no live providers / store), the api shell wires the concretes. The LOAD-BEARING
contract stays secret-masking: a provider's raw key NEVER appears — only
`key_is_set` (a bool) + (for customs) the masked display.

Written BEFORE the implementation.
"""

from __future__ import annotations

import pytest


# A secret value the masking contract must NEVER leak into the config view JSON.
SECRET_VALUE = "sk-ant-SUPER-SECRET-do-not-leak-0123456789"


# ---------------------------------------------------------------------------
#  1. build_providers_config — the pure per-provider config view.
# ---------------------------------------------------------------------------


def test_build_providers_config_shape():
    """`build_providers_config(built_ins, customs)` returns
    `{providers:[{name,label,key_is_set,is_custom,base_url,testable}]}` — built-ins
    first (in the order given), then the custom providers, each carrying its
    key-presence + a testable flag, and NEVER a raw key."""
    from app.settings_module import service as svc

    built_ins = [
        {"name": "anthropic", "label": "Anthropic", "key_is_set": True, "testable": True},
        {"name": "openai", "label": "OpenAI", "key_is_set": False, "testable": True},
        {"name": "bedrock", "label": "Amazon Bedrock", "key_is_set": False, "testable": False},
    ]
    customs = [
        {"id": "together-ai", "name": "Together AI", "base_url": "https://api.together.xyz/v1",
         "has_key": True, "key_display": "•••• set"},
    ]

    out = svc.build_providers_config(built_ins, customs)
    assert set(out) == {"providers"}
    rows = out["providers"]
    by_name = {r["name"]: r for r in rows}

    # built-ins present with their key-presence + testable + is_custom=False
    assert by_name["anthropic"]["key_is_set"] is True
    assert by_name["anthropic"]["is_custom"] is False
    assert by_name["anthropic"]["testable"] is True
    assert by_name["anthropic"]["label"] == "Anthropic"
    assert by_name["openai"]["key_is_set"] is False
    assert by_name["bedrock"]["testable"] is False

    # the custom provider is present, flagged is_custom, with its base_url + masked state
    cust = next(r for r in rows if r["name"] == "Together AI")
    assert cust["is_custom"] is True
    assert cust["key_is_set"] is True
    assert cust["base_url"] == "https://api.together.xyz/v1"
    # the test target reference for a custom provider is the `custom:<id>` form
    assert cust.get("provider_ref") == "custom:together-ai"

    # built-ins order is preserved, customs come after the built-ins
    names = [r["name"] for r in rows]
    assert names.index("anthropic") < names.index("openai") < names.index("Together AI")


def test_build_providers_config_never_carries_raw_key():
    """The config view NEVER carries a raw key — even if a caller mistakenly passed
    one through, the builder only emits the documented (masked/boolean) fields."""
    import json

    from app.settings_module import service as svc

    # A hostile input that smuggles a raw key field into both a built-in + a custom.
    built_ins = [{"name": "anthropic", "label": "Anthropic", "key_is_set": True,
                  "testable": True, "api_key": SECRET_VALUE}]
    customs = [{"id": "x", "name": "X", "base_url": "u", "has_key": True,
                "key_display": "•••• set", "api_key": SECRET_VALUE}]

    out = svc.build_providers_config(built_ins, customs)
    blob = json.dumps(out)
    assert SECRET_VALUE not in blob
    # but the presence boolean is still truthful
    rows = {r["name"]: r for r in out["providers"]}
    assert rows["anthropic"]["key_is_set"] is True
    assert rows["X"]["key_is_set"] is True


def test_build_providers_config_provider_ref_for_builtin():
    """A built-in provider's `provider_ref` is its secret-key field name (e.g.
    `anthropic_api_key`) so the Test button + the secret write address the right
    field — the canonical write target for that provider's key."""
    from app.settings_module import service as svc

    built_ins = [{"name": "anthropic", "label": "Anthropic", "key_is_set": True,
                  "testable": True, "key_field": "anthropic_api_key"}]
    out = svc.build_providers_config(built_ins, [])
    row = out["providers"][0]
    assert row["provider_ref"] == "anthropic_api_key"
    assert row["key_field"] == "anthropic_api_key"


# ---------------------------------------------------------------------------
#  2. GET /settings/{project}/providers/config — the endpoint.
# ---------------------------------------------------------------------------


class FakeProviderConfigSource:
    """Stand-in for the built-in provider key-presence source (the SAME info the
    `providers` module computes from the store/env). Returns the list of built-in
    provider dicts the endpoint folds into the config view — NEVER a raw key."""

    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]
        self.calls = 0

    def builtin_provider_config(self, values):
        self.calls += 1
        # echo the scripted rows (the real impl reads `values`/env for key-presence)
        return [dict(r) for r in self._rows]


class FakeCustomStore:
    """Stand-in for the masked custom-provider view (the SAME surface app.settings
    exposes). Never echoes a raw api_key."""

    def __init__(self, rows=None):
        self._rows = [dict(r) for r in (rows or [])]

    def view_custom_providers(self):
        return [dict(r) for r in self._rows]


@pytest.mark.asyncio
async def test_router_providers_config_endpoint_shape():
    """`GET /settings/{project}/providers/config` returns the per-provider config
    view (built-in key-presence + the masked custom list) — `key_is_set` per
    provider, NEVER a raw key. Includes `project` + `store_connected`."""
    import json

    from tests.test_settings_module import FakeOpStore

    from app.settings_module import api as settings_api

    cfg_source = FakeProviderConfigSource([
        {"name": "anthropic", "label": "Anthropic", "key_is_set": True,
         "testable": True, "key_field": "anthropic_api_key"},
        {"name": "openai", "label": "OpenAI", "key_is_set": False,
         "testable": True, "key_field": "openai_api_key"},
    ])
    customs = FakeCustomStore(rows=[
        {"id": "together-ai", "name": "Together AI", "base_url": "https://api.together.xyz/v1",
         "has_key": True, "key_display": "•••• set"},
    ])
    store = FakeOpStore(app_settings={"anthropic_api_key": SECRET_VALUE})

    result = await settings_api.providers_config_endpoint(
        "kaidera-os", store=store, cfg_source=cfg_source, custom_store=customs,
    )

    assert result["project"] == "kaidera-os"
    assert result["store_connected"] is True
    rows = {r["name"]: r for r in result["providers"]}
    assert rows["anthropic"]["key_is_set"] is True
    assert rows["openai"]["key_is_set"] is False
    assert "Together AI" in rows
    assert rows["Together AI"]["is_custom"] is True
    # the secret value never leaks into the config JSON
    assert SECRET_VALUE not in json.dumps(result)


@pytest.mark.asyncio
async def test_router_providers_config_degrades_when_store_down():
    """A down store still yields the built-in provider list (key-presence falls back
    to env/.env via the source) + the custom list — `store_connected=false`, never a
    500."""
    from tests.test_settings_module import FakeOpStore

    from app.settings_module import api as settings_api

    cfg_source = FakeProviderConfigSource([
        {"name": "anthropic", "label": "Anthropic", "key_is_set": False,
         "testable": True, "key_field": "anthropic_api_key"},
    ])
    result = await settings_api.providers_config_endpoint(
        "kaidera-os", store=FakeOpStore(down=True),
        cfg_source=cfg_source, custom_store=FakeCustomStore(),
    )
    assert result["store_connected"] is False
    assert [r["name"] for r in result["providers"]] == ["anthropic"]


def test_providers_config_route_registered_and_collision_free():
    """`GET /settings/{project}/providers/config` is registered (a distinct LEAF
    under the module's JSON shape) — it can't shadow the `providers` catalog leaf
    (different trailing segment) nor any live HTML route."""
    from app.settings_module.api import router

    paths = {r.path for r in router.routes}
    assert "/settings/{project}/providers/config" in paths
    # the sibling catalog leaf still exists + is distinct
    assert "/settings/{project}/providers" in paths

    def methods_for(path):
        for r in router.routes:
            if r.path == path:
                return getattr(r, "methods", set())
        return set()

    assert "GET" in methods_for("/settings/{project}/providers/config")


# ---------------------------------------------------------------------------
#  3. DE-DUP — the System schema no longer owns the provider secrets, and the raw
#     editor excludes them (a provider key is set/edited ONLY in Providers now).
# ---------------------------------------------------------------------------


def test_system_schema_no_longer_lists_provider_secrets():
    """CANONICALIZATION: the System SCHEMA no longer carries the 12 provider secret
    fields (nor the other provider credential fields) — they have a canonical home
    in the Providers tab. The System schema keeps the NON-provider settings
    (Cortex-connection, harness, app preferences)."""
    from app import settings as settings_store

    schema_keys = {f["key"] for g in settings_store.SCHEMA for f in g["fields"]}

    # the 12 canonical provider secret keys are GONE from System
    gone = {
        "anthropic_api_key", "openai_api_key", "openrouter_api_key",
        "fireworks_api_key", "groq_api_key", "siliconflow_api_key",
        "dashscope_api_key", "deepseek_api_key", "together_api_key",
        "cohere_api_key", "inception_api_key", "moonshot_api_key",
    }
    assert not (schema_keys & gone), (
        f"provider secrets must be OUT of the System schema: {sorted(schema_keys & gone)}"
    )

    # the whole provider-credential family is gone (account ids + AWS creds + the
    # extra provider secrets) — no provider group survives in System
    provider_family = {
        "fireworks_account_id", "perplexity_api_key", "xai_api_key",
        "aws_access_key_id", "aws_secret_access_key", "aws_region",
    }
    assert not (schema_keys & provider_family)
    assert not any(g["id"] == "providers" for g in settings_store.SCHEMA)

    # the NON-provider System settings that survive the wire-or-remove audit are
    # STILL present (each is actually READ at runtime): the Cortex connection +
    # default project, the default harness, and the autonomy-autostart switch.
    for kept in ("cortex_base_url", "cortex_default_project", "harness_default",
                 "harness_autostart"):
        assert kept in schema_keys

    # the display-only fields that NOTHING read are GONE (the S5/S7 audit): a stale
    # theme toggle (the SPA is always glass-dark), a global poll knob (the SPA uses
    # fixed per-surface cadences), and the vestigial scripts path (workers self-derive).
    for retired in ("poll_interval_secs", "theme", "harness_scripts_path"):
        assert retired not in schema_keys


def test_provider_secret_keys_helper_lists_the_canonical_set():
    """`settings.provider_secret_keys()` enumerates the provider-credential keys
    that NO LONGER live in System (the canonical de-dup set) — so the raw
    App-settings editor + any filter can exclude them in ONE place."""
    from app import settings as settings_store

    keys = set(settings_store.provider_secret_keys())
    # the 12 canonical provider secrets are all in the exclusion set
    for k in ("anthropic_api_key", "openai_api_key", "openrouter_api_key",
              "fireworks_api_key", "groq_api_key", "siliconflow_api_key",
              "dashscope_api_key", "deepseek_api_key", "together_api_key",
              "cohere_api_key", "inception_api_key", "moonshot_api_key"):
        assert k in keys


def test_app_settings_endpoint_excludes_provider_secrets_from_raw_editor():
    """The System app-settings surface (the raw key→value editor) EXCLUDES the
    provider secret keys — they have a canonical home in Providers, so they must NOT
    appear in the raw editor (no triple-duplication). The underlying store is
    unchanged; only the SURFACED keys are filtered."""
    from app import settings as settings_store

    raw = {
        "anthropic_api_key": SECRET_VALUE,   # a provider secret — must be filtered
        "openrouter_api_key": "sk-or-x",     # a provider secret — must be filtered
        "theme": "dark",                     # a NON-provider setting — must remain
        "poll_interval_secs": 30,            # a NON-provider setting — must remain
    }
    filtered = settings_store.filter_non_provider_settings(raw)

    assert "anthropic_api_key" not in filtered
    assert "openrouter_api_key" not in filtered
    assert filtered["theme"] == "dark"
    assert filtered["poll_interval_secs"] == 30
    # and the secret value is nowhere in the filtered surface
    import json
    assert SECRET_VALUE not in json.dumps(filtered)
