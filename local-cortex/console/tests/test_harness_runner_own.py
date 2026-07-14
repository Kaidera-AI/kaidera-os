from __future__ import annotations

import pytest

from app import harness_runner as hr


def _resolver(cfg, key):
    return cfg.get(key, "")


def _manifold_cfg() -> dict[str, str]:
    return {
        "kaidera_manifold_api_key": "mf-key",
        "kaidera_manifold_base_url": "https://edge.example/v1",
        "kaidera_manifold_project_id": "project-123",
    }


def test_own_target_always_routes_through_manifold():
    assert hr._own_target(
        "kaidera-manifold/vendor/model",
        _manifold_cfg(),
        {},
        _resolver,
    ) == ("kaidera-manifold", "vendor/model", "mf-key")
    assert hr._own_target(
        "vendor/model",
        _manifold_cfg(),
        {},
        _resolver,
    ) == ("kaidera-manifold", "vendor/model", "mf-key")


def test_own_provider_key_rejects_non_manifold_names():
    assert hr._own_provider_key("kaidera-manifold", _manifold_cfg(), {}, _resolver) == "mf-key"
    assert hr._own_provider_key("direct-provider", _manifold_cfg(), {}, _resolver) == ""


def test_payload_uses_live_manifold_effort_metadata(monkeypatch):
    from app import providers

    monkeypatch.setattr(
        providers,
        "cached_reasoning_levels",
        lambda provider, model: ["low", "high", "future"],
    )
    payload = hr._openai_compat_payload(
        "kaidera-manifold", "vendor/model", "ping", "system", "future"
    )
    assert payload["reasoning_effort"] == "future"
    assert payload["model"] == "vendor/model"


def test_tool_agent_reasoning_settings_use_openai_compatible_body():
    from app import kaidera_agent

    settings = {}
    kaidera_agent._apply_reasoning_settings(
        settings,
        "kaidera-manifold",
        {"reasoning_effort": "high"},
    )
    assert settings == {"extra_body": {"reasoning_effort": "high"}}
    with pytest.raises(ValueError):
        kaidera_agent._apply_reasoning_settings({}, "direct-provider", {})


@pytest.mark.asyncio
async def test_stream_chat_routes_manifold_to_tool_agent(monkeypatch):
    from app import kaidera_agent, providers

    monkeypatch.setattr(hr, "_own_runtime_config", lambda: (_manifold_cfg(), {}, _resolver))
    monkeypatch.setattr(providers, "cached_reasoning_levels", lambda *_a: ["low", "high"])
    captured: dict = {}

    async def fake_agent(**kwargs):
        captured.update(kwargs)
        yield {"type": "session", "session_id": None, "model": kwargs["model"]}
        yield {"type": "tool", "name": "run_bash", "text": "run_bash(echo hi)"}
        yield {"type": "result", "text": "did it", "tokens_in": 5, "tokens_out": 7}
        yield {"type": "done"}

    monkeypatch.setattr(kaidera_agent, "stream_kaidera_agent", fake_agent)
    events = [
        event
        async for event in hr.stream_chat(
            "ping",
            model="kaidera-manifold/vendor/model",
            system="system",
            harness="kaidera",
            reasoning="high",
        )
    ]

    assert captured["provider"] == "kaidera-manifold"
    assert captured["model"] == "vendor/model"
    assert captured["base_url"] == "https://edge.example/v1"
    assert captured["reasoning_fields"] == {"reasoning_effort": "high"}
    assert captured["extra_headers"] == {"X-Project-Id": "project-123"}
    assert any(event["type"] == "tool" for event in events)
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_stream_kaidera_disables_cleanly_when_configuration_is_incomplete(monkeypatch):
    monkeypatch.setattr(hr, "_own_runtime_config", lambda: ({}, {}, _resolver))
    events = [event async for event in hr._stream_kaidera("ping", model="vendor/model")]
    assert [event["type"] for event in events] == ["session", "error", "done"]
    assert events[1]["category"] == "provider_not_configured"


@pytest.mark.asyncio
async def test_no_tools_lane_uses_manifold_singleshot(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake_complete(prompt, model, system, thinking, workspace=None):
        calls.append({"prompt": prompt, "model": model, "thinking": thinking})
        return {
            "text": "single shot reply",
            "tokens_in": 1,
            "tokens_out": 2,
            "reasoning": "short plan",
        }

    monkeypatch.setattr(hr, "_kaidera_complete", fake_complete)
    events = [
        event
        async for event in hr.stream_chat(
            "ping",
            model="kaidera-manifold/vendor/model",
            harness="kaidera-no-tools",
            reasoning="high",
        )
    ]
    assert calls == [{
        "prompt": "ping",
        "model": "kaidera-manifold/vendor/model",
        "thinking": "high",
    }]
    assert [event["type"] for event in events] == ["session", "thinking", "result", "done"]


@pytest.mark.asyncio
async def test_empty_tool_agent_reply_falls_back_to_singleshot(monkeypatch):
    from app import kaidera_agent

    monkeypatch.setattr(hr, "_own_runtime_config", lambda: (_manifold_cfg(), {}, _resolver))

    async def fake_agent(**kwargs):
        yield {"type": "session", "session_id": "agent-session", "model": kwargs["model"]}
        yield {"type": "thinking", "text": "inspect"}
        yield {"type": "result", "text": ""}
        yield {"type": "done"}

    async def fake_complete(prompt, model, system, thinking, workspace=None):
        return {"text": "fallback reply", "tokens_in": 1, "tokens_out": 2}

    monkeypatch.setattr(kaidera_agent, "stream_kaidera_agent", fake_agent)
    monkeypatch.setattr(hr, "_kaidera_complete", fake_complete)
    events = [event async for event in hr._stream_kaidera("ping", "vendor/model")]
    assert next(event for event in events if event["type"] == "result")["text"] == "fallback reply"
    assert events[-1] == {"type": "done"}
