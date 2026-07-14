"""W1a — the platform-minted Kaidera AI Manifold API-key path + routing.

Layer C: the platform license customer surface mints a Manifold inference key, the app
stores it in the provider runtime setting, and PUBLIC edition still exposes Manifold only.
"""
import httpx
import pytest

from app import providers
from app import providers_env
from app import settings as settings_store
from app import harness_runner as hr


def test_manifold_requires_key_and_project_id():
    # Both are required — the /v1 edge 400s without the X-Project-Id header, so the
    # provider is not "configured" until the customer supplies key AND project uuid.
    assert providers._PROVIDER_SETTING_KEYS["kaidera-manifold"] == (
        "kaidera_manifold_api_key",
        "kaidera_manifold_project_id",
    )


def test_manifold_models_preserve_live_effort_levels(monkeypatch):
    rows = providers._parse_openai_compat(
        "kaidera-manifold",
        {
            "data": [
                {
                    "id": "provider/reasoning-model",
                    "reasoning": {"supported_efforts": ["low", "high", "ultra"]},
                    "supported_parameters": ["reasoning"],
                }
            ]
        },
    )
    assert rows[0]["reasoning_levels"] == ["low", "high", "ultra"]

    monkeypatch.setattr(
        providers,
        "_cache",
        {"catalog": {"groups": [{"provider": "kaidera-manifold", "models": rows}]}},
    )
    assert providers.cached_reasoning_levels(
        "kaidera-manifold", "provider/reasoning-model"
    ) == ["low", "high", "ultra"]


def test_openai_compatible_efforts_accept_structured_rows():
    rows = providers._parse_openai_compat(
        "kaidera-manifold",
        {
            "data": [{
                "id": "provider/future",
                "reasoning": {
                    "supported_efforts": [
                        {"reasoningEffort": "low"},
                        {"value": "ultra"},
                        {"effort": "future"},
                    ]
                },
            }]
        },
    )
    assert rows[0]["reasoning_levels"] == ["low", "ultra", "future"]


@pytest.mark.asyncio
async def test_manifold_catalog_fetch_uses_key_project_and_configured_base(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"data": [{"id": "provider/model"}]})

    monkeypatch.setattr(
        providers,
        "_resolve_provider_key",
        lambda cfg, field: str(cfg.get(field) or ""),
    )
    cfg = {
        "kaidera_manifold_api_key": "mfld-test",
        "kaidera_manifold_project_id": "project-test",
        "kaidera_manifold_base_url": "https://manifold.example/v1/",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await providers._fetch_manifold(client, cfg)

    request = captured["request"]
    assert str(request.url) == "https://manifold.example/v1/models"
    assert request.headers["Authorization"] == "Bearer mfld-test"
    assert request.headers["X-Project-Id"] == "project-test"
    assert rows and rows[0]["id"] == "provider/model"


def test_manifold_key_maps_to_env_var():
    assert providers_env._SETTING_ENV_VAR["kaidera_manifold_api_key"] == "KAIDERA_MANIFOLD_API_KEY"
    assert providers_env._SETTING_ENV_VAR["kaidera_manifold_base_url"] == "KAIDERA_MANIFOLD_BASE_URL"
    assert providers_env._SETTING_ENV_VAR["kaidera_manifold_project_id"] == "KAIDERA_MANIFOLD_PROJECT_ID"
    assert "kaidera_manifold_project_id" in settings_store.PROVIDER_SECRET_KEYS


def test_manifold_project_id_resolves_from_cfg_then_env(monkeypatch):
    monkeypatch.delenv("KAIDERA_MANIFOLD_PROJECT_ID", raising=False)
    assert hr._manifold_project_id({"kaidera_manifold_project_id": "  proj-cfg  "}) == "proj-cfg"
    assert hr._manifold_project_id({}) == ""
    monkeypatch.setenv("KAIDERA_MANIFOLD_PROJECT_ID", "proj-env")
    assert hr._manifold_project_id({}) == "proj-env"


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _CaptureClient:
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _CaptureClient.captured = {"url": url, "headers": headers or {}, "json": json}
        return _FakeResp(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


@pytest.mark.asyncio
async def test_manifold_call_sends_x_project_id_header(monkeypatch):
    monkeypatch.setenv("KAIDERA_MANIFOLD_BASE_URL", "https://platform.example/v1")
    cfg = {
        "kaidera_manifold_api_key": "mfld_live_v1_test",
        "kaidera_manifold_project_id": "proj-uuid-123",
    }
    monkeypatch.setattr(hr, "_own_runtime_config", lambda: (cfg, {}, lambda c, k: c.get(k, "")))
    monkeypatch.setattr(
        hr, "_own_target", lambda *a, **k: ("kaidera-manifold", "some-model", "mfld_live_v1_test")
    )
    monkeypatch.setattr(hr.httpx, "AsyncClient", _CaptureClient)

    await hr._kaidera_complete("hello", "some-model", None, None)

    sent = _CaptureClient.captured["headers"]
    assert sent.get("X-Project-Id") == "proj-uuid-123"
    assert sent.get("Authorization") == "Bearer mfld_live_v1_test"
    assert _CaptureClient.captured["url"].endswith("/v1/chat/completions")


@pytest.mark.asyncio
async def test_manifold_call_fails_closed_without_project_id(monkeypatch):
    monkeypatch.delenv("KAIDERA_MANIFOLD_PROJECT_ID", raising=False)
    cfg = {"kaidera_manifold_api_key": "mfld_live_v1_test"}  # no project id
    monkeypatch.setattr(hr, "_own_runtime_config", lambda: (cfg, {}, lambda c, k: c.get(k, "")))
    monkeypatch.setattr(
        hr, "_own_target", lambda *a, **k: ("kaidera-manifold", "some-model", "mfld_live_v1_test")
    )

    with pytest.raises(hr._OwnHarnessError) as exc:
        await hr._kaidera_complete("hello", "some-model", None, None)
    assert exc.value.category == "provider_not_configured"
    assert "project id" in exc.value.message.lower()


@pytest.mark.asyncio
async def test_manifold_tool_agent_sends_x_project_id_header(monkeypatch):
    from app import kaidera_agent

    captured = {}
    cfg = {
        "kaidera_manifold_api_key": "mfld_live_v1_test",
        "kaidera_manifold_project_id": "proj-uuid-123",
    }
    monkeypatch.setattr(hr, "_own_runtime_config", lambda: (cfg, {}, lambda c, k: c.get(k, "")))
    monkeypatch.setattr(
        hr, "_own_target", lambda *a, **k: ("kaidera-manifold", "some-model", "mfld_live_v1_test")
    )

    async def fake_agent(**kwargs):
        captured.update(kwargs)
        yield {"type": "session", "session_id": None, "model": "some-model"}
        yield {"type": "result", "text": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(kaidera_agent, "stream_kaidera_agent", fake_agent)
    events = [event async for event in hr._stream_kaidera("hello", "some-model")]

    assert captured["extra_headers"] == {"X-Project-Id": "proj-uuid-123"}
    assert any(event.get("type") == "result" and event.get("text") == "ok" for event in events)


@pytest.mark.asyncio
async def test_manifold_tool_agent_disables_cleanly_without_project_id(monkeypatch):
    cfg = {"kaidera_manifold_api_key": "mfld_live_v1_test"}
    monkeypatch.setattr(hr, "_own_runtime_config", lambda: (cfg, {}, lambda c, k: c.get(k, "")))
    monkeypatch.setattr(
        hr, "_own_target", lambda *a, **k: ("kaidera-manifold", "some-model", "mfld_live_v1_test")
    )

    events = [event async for event in hr._stream_kaidera("hello", "some-model")]

    assert [event["type"] for event in events] == ["session", "error", "done"]
    assert events[1]["category"] == "provider_not_configured"


def test_public_edition_still_manifold_only(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    assert providers.visible_providers() == ["kaidera-manifold"]


def test_own_harness_resolves_manifold_key_and_endpoint():
    setting_key, base_url = hr._OWN_OPENAI_COMPAT_CHAT["kaidera-manifold"]
    assert setting_key == "kaidera_manifold_api_key"
    assert hr.MANIFOLD_BASE_URL == ""
    assert base_url == ""
    # the platform-minted key resolves through the own-harness provider-key path
    cfg = {"kaidera_manifold_api_key": "mf-test-key"}
    key = hr._own_provider_key("kaidera-manifold", cfg, {}, lambda c, k: c.get(k, ""))
    assert key == "mf-test-key"


def test_manifold_unconfigured_resolves_empty():
    # no platform key -> empty (the lane degrades, never authenticates with a stale key)
    key = hr._own_provider_key("kaidera-manifold", {}, {}, lambda c, k: c.get(k, ""))
    assert key == ""


def test_public_manifold_key_requires_signed_manifold_access(monkeypatch, ed25519_public_license):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    cfg = {"kaidera_manifold_api_key": "mf-test-key"}
    assert hr._own_provider_key("kaidera-manifold", cfg, {}, lambda c, k: c.get(k, "")) == ""

    ed25519_public_license("Acme", days=365, features=["manifold_access"])
    assert hr._own_provider_key("kaidera-manifold", cfg, {}, lambda c, k: c.get(k, "")) == "mf-test-key"
