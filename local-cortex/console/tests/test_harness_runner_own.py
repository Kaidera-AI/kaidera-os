from __future__ import annotations

import os

import pytest

from app import harness_runner as hr


def _resolver(cfg, key):
    return cfg.get(key, "")


def test_own_target_uses_provider_prefixed_value():
    provider, native, key = hr._own_target(
        "openrouter/openai/gpt-5.5",
        {"openrouter_api_key": "or-key"},
        {},
        _resolver,
    )

    assert (provider, native, key) == ("openrouter", "openai/gpt-5.5", "or-key")


def test_own_target_can_use_pi_auth_resolved_ollama_key(tmp_path, monkeypatch):
    from app import providers

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"ollama-cloud":{"type":"api_key","key":"ollama-from-pi"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_AUTH_FILE", str(auth))
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(providers, "_env_file_value", lambda _var: "")

    provider, native, key = hr._own_target(
        "ollama-cloud/qwen3-coder:480b",
        {},
        {},
        providers._resolve_provider_key,
    )

    assert (provider, native, key) == (
        "ollama-cloud",
        "qwen3-coder:480b",
        "ollama-from-pi",
    )


def test_openai_compat_payload_maps_ollama_thinking_to_reasoning_effort():
    payload = hr._openai_compat_payload(
        "ollama-cloud",
        "qwen3-coder:480b",
        "ping",
        "system",
        "high",
    )

    assert payload["reasoning_effort"] == "high"
    assert payload["model"] == "qwen3-coder:480b"


def test_tool_agent_reasoning_settings_use_native_request_fields():
    from app import kaidera_agent

    openai_settings = {}
    kaidera_agent._apply_reasoning_settings(
        openai_settings,
        "openrouter",
        {"reasoning": {"effort": "ultra"}},
    )
    assert openai_settings == {"extra_body": {"reasoning": {"effort": "ultra"}}}

    anthropic_settings = {}
    kaidera_agent._apply_reasoning_settings(
        anthropic_settings,
        "anthropic",
        {"thinking": {"type": "adaptive"}, "reasoning_effort": "max"},
    )
    assert anthropic_settings == {
        "anthropic_thinking": {"type": "adaptive"},
        "anthropic_effort": "max",
    }


def test_own_target_falls_back_to_openrouter_for_legacy_slug_without_direct_key():
    provider, native, key = hr._own_target(
        "anthropic/claude-opus-4.8",
        {"openrouter_api_key": "or-key"},
        {},
        _resolver,
    )

    assert (provider, native, key) == (
        "openrouter",
        "anthropic/claude-opus-4.8",
        "or-key",
    )


@pytest.mark.asyncio
async def test_stream_chat_routes_kaidera_to_agent(monkeypatch):
    # kaidera (any non-codex provider) now runs the REAL tool-using agent
    # (app/kaidera_agent.py), NOT the single-shot _kaidera_complete lane.
    from app import kaidera_agent

    monkeypatch.setattr(
        hr, "_own_runtime_config",
        lambda: ({"openrouter_api_key": "or-key"}, {}, _resolver),
    )

    captured: dict = {}

    async def fake_agent(**kwargs):
        captured.update(kwargs)
        yield {"type": "session", "session_id": None, "model": kwargs["model"]}
        yield {"type": "tool", "name": "run_bash", "text": "run_bash(echo hi)"}
        yield {"type": "result", "text": "did it", "cost_usd": None,
               "session_id": None, "tokens_in": 5, "tokens_out": 7}
        yield {"type": "done"}

    monkeypatch.setattr(kaidera_agent, "stream_kaidera_agent", fake_agent)

    async def _must_not_run(*_a, **_k):
        raise AssertionError("the single-shot lane must not run for a kaidera agent")

    monkeypatch.setattr(hr, "_kaidera_complete", _must_not_run)

    events = [
        ev
        async for ev in hr.stream_chat(
            "ping",
            model="openrouter/openai/gpt-5.5",
            system="system",
            harness="kaidera",
            reasoning="high",
        )
    ]

    # The resolved provider/model/base_url were threaded into the agent.
    assert captured["provider"] == "openrouter"
    assert captured["model"] == "openai/gpt-5.5"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["reasoning_fields"] == {"reasoning": {"effort": "high"}}
    # The agent's tool + result events flow straight through stream_chat.
    assert any(e["type"] == "tool" for e in events)
    result = next(e for e in events if e["type"] == "result")
    assert result["text"] == "did it"
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_stream_chat_no_tools_routes_directly_to_singleshot(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake_complete(prompt, model, system, thinking, workspace=None):
        calls.append(
            {
                "prompt": prompt,
                "model": model,
                "system": system,
                "thinking": thinking,
                "workspace": workspace,
            }
        )
        return {"text": "single shot reply", "tokens_in": 1, "tokens_out": 2, "reasoning": "short plan"}

    monkeypatch.setattr(hr, "_kaidera_complete", fake_complete)

    events = [
        ev
        async for ev in hr.stream_chat(
            "ping",
            model="openrouter/openai/gpt-5.5",
            system="system",
            harness="kaidera-no-tools",
            reasoning="high",
            workspace="/tmp/dxb",
        )
    ]

    assert calls == [
        {
            "prompt": "ping",
            "model": "openrouter/openai/gpt-5.5",
            "system": "system",
            "thinking": "high",
            "workspace": "/tmp/dxb",
        }
    ]
    assert [e["type"] for e in events] == ["session", "thinking", "result", "done"]
    assert events[2]["text"] == "single shot reply"


@pytest.mark.asyncio
async def test_stream_kaidera_empty_agent_result_falls_back_to_singleshot(monkeypatch):
    from app import kaidera_agent

    monkeypatch.setattr(
        hr, "_own_runtime_config",
        lambda: ({"openrouter_api_key": "or-key"}, {}, _resolver),
    )

    async def fake_agent(**kwargs):
        yield {"type": "session", "session_id": "agent-session", "model": kwargs["model"]}
        yield {"type": "thinking", "text": "I should inspect tools"}
        yield {"type": "result", "text": "", "cost_usd": None, "session_id": "agent-session"}
        yield {"type": "done"}

    async def fake_complete(prompt, model, system, thinking, workspace=None):
        return {"text": "fallback reply", "tokens_in": 1, "tokens_out": 2, "reasoning": ""}

    monkeypatch.setattr(kaidera_agent, "stream_kaidera_agent", fake_agent)
    monkeypatch.setattr(hr, "_kaidera_complete", fake_complete)

    events = [
        ev
        async for ev in hr.stream_chat(
            "ping",
            model="openrouter/openai/gpt-5.5",
            system="system",
            harness="kaidera",
            reasoning="high",
        )
    ]

    assert [e["type"] for e in events].count("session") == 1
    assert any(e["type"] == "thinking" for e in events)
    result = next(e for e in events if e["type"] == "result")
    assert result["text"] == "fallback reply"
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_stream_kaidera_reports_missing_key(monkeypatch):
    # A resolved provider with no configured key yields a clean, non-crashing
    # provider_not_configured error (before any agent/model call).
    monkeypatch.setattr(hr, "_own_runtime_config", lambda: ({}, {}, _resolver))

    events = [ev async for ev in hr._stream_kaidera("ping", model="openrouter/openai/gpt-5.5")]

    assert events[1]["type"] == "error"
    assert events[1]["category"] == "provider_not_configured"
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_codex_subscription_stays_single_shot(monkeypatch):
    # codex-subscription keeps the single-shot OAuth path (Pydantic AI can't reach
    # the ChatGPT backend), so _kaidera_complete IS still used for that provider.
    monkeypatch.setattr(hr, "_own_runtime_config", lambda: ({}, {}, _resolver))
    monkeypatch.setattr(
        hr, "_own_target",
        lambda *_a: ("codex-subscription", "gpt-5.5-codex", "codex-oauth"),
    )

    calls = []

    async def fake_complete(prompt, model, system, thinking, workspace=None):
        calls.append((prompt, model))
        return {"text": "codex says hi", "tokens_in": 1, "tokens_out": 2}

    monkeypatch.setattr(hr, "_kaidera_complete", fake_complete)

    events = [ev async for ev in hr._stream_kaidera("ping", model="codex-subscription/gpt-5.5-codex")]

    assert calls and calls[0][0] == "ping"
    result = next(e for e in events if e["type"] == "result")
    assert result["text"] == "codex says hi"
    assert events[-1] == {"type": "done"}


# ---------------------------------------------------------------------------
#  B1/B4 — reasoning APPLY (per-provider native param) + OUTPUT surfacing
#  through the live single-shot call path (httpx mocked; nothing real spawns).
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _CaptureClient:
    """An httpx.AsyncClient stand-in that records the posted body + returns a
    canned response, so we can assert the EXACT reasoning fields the runner sends
    on the live path without any network call."""

    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _CaptureClient.captured = {"url": url, "headers": headers, "json": json}
        return _CaptureClient._response


def _drive_complete(monkeypatch, provider, native_model, response_payload, thinking):
    import httpx

    monkeypatch.setattr(hr, "_own_runtime_config", lambda: ({"k": "key"}, {}, _resolver))
    monkeypatch.setattr(hr, "_own_target", lambda *_a: (provider, native_model, "key"))
    _CaptureClient._response = _FakeResp(response_payload)
    monkeypatch.setattr(httpx, "AsyncClient", _CaptureClient)


@pytest.mark.asyncio
async def test_kaidera_complete_anthropic_direct_now_emits_thinking(monkeypatch):
    """The Anthropic-direct path previously emitted NO thinking (the biggest gap).
    It now merges the adaptive block + reasoning_effort for a real level."""
    _drive_complete(
        monkeypatch, "anthropic", "claude-opus-4-8",
        {"content": [{"type": "thinking", "thinking": "let me think"},
                     {"type": "text", "text": "the answer"}],
         "usage": {"input_tokens": 3, "output_tokens": 5}},
        thinking="max",
    )
    out = await hr._kaidera_complete("ping", "anthropic/claude-opus-4-8", None, "max")

    body = _CaptureClient.captured["json"]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["reasoning_effort"] == "max"
    assert "budget_tokens" not in body  # the legacy field 400s on 4.7+
    # B4: the thinking content block is surfaced back.
    assert out["reasoning"] == "let me think"
    assert out["text"] == "the answer"


@pytest.mark.asyncio
async def test_kaidera_complete_anthropic_off_sends_no_thinking(monkeypatch):
    _drive_complete(
        monkeypatch, "anthropic", "claude-opus-4-8",
        {"content": [{"type": "text", "text": "hi"}]},
        thinking="off",
    )
    await hr._kaidera_complete("ping", "anthropic/claude-opus-4-8", None, "off")
    body = _CaptureClient.captured["json"]
    assert "thinking" not in body and "reasoning_effort" not in body


@pytest.mark.asyncio
async def test_kaidera_complete_openai_compat_sends_reasoning_effort(monkeypatch):
    _drive_complete(
        monkeypatch, "openai", "gpt-5.5",
        {"choices": [{"message": {"content": "hi"}}]},
        thinking="high",
    )
    await hr._kaidera_complete("ping", "openai/gpt-5.5", None, "high")
    assert _CaptureClient.captured["json"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_kaidera_complete_grok4_sends_no_param_400_guard(monkeypatch):
    """SAFETY: grok-4 rejects reasoning_effort → the live path sends NOTHING."""
    _drive_complete(
        monkeypatch, "xai", "grok-4-0709",
        {"choices": [{"message": {"content": "hi"}}]},
        thinking="high",
    )
    await hr._kaidera_complete("ping", "xai/grok-4-0709", None, "high")
    assert "reasoning_effort" not in _CaptureClient.captured["json"]


@pytest.mark.asyncio
async def test_kaidera_complete_b4_surfaces_reasoning_content(monkeypatch):
    _drive_complete(
        monkeypatch, "deepseek", "deepseek-v4",
        {"choices": [{"message": {"content": "ans", "reasoning_content": "chain"}}]},
        thinking="on",
    )
    out = await hr._kaidera_complete("ping", "deepseek/deepseek-v4", None, "on")
    assert out["reasoning"] == "chain"
    # and the toggle body is correct.
    assert _CaptureClient.captured["json"]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_singleshot_emits_thinking_event_before_result(monkeypatch):
    """B4: the single-shot stream yields a `thinking` event (when the provider
    returned reasoning) BEFORE the result, same shape as the agent path."""
    async def fake_complete(prompt, model, system, thinking, workspace=None):
        return {"text": "ans", "tokens_in": 1, "tokens_out": 2, "reasoning": "my thinking"}

    monkeypatch.setattr(hr, "_kaidera_complete", fake_complete)
    events = [ev async for ev in hr._stream_kaidera_singleshot("ping", "openai/gpt-5.5", None, "high")]
    types = [e["type"] for e in events]
    assert "thinking" in types
    think_i = types.index("thinking")
    result_i = types.index("result")
    assert think_i < result_i
    assert events[think_i]["text"] == "my thinking"


# --- multi-project workspace threading (chat runs in the selected project's folder) ---


def test_apply_project_workspace_prepends_project_agents_scripts_and_scopes(monkeypatch):
    """The console is a multi-project UI: a chat with an agent in project X must run
    the harness IN project X's repo_root + with project X's `.agents/scripts` first on
    PATH + CORTEX_PROJECT=X, so the Cortex CLI workspace-path isolation guard resolves
    the agent under the SELECTED project (not the console's own workspace)."""
    from app import harness_runner as hr

    env = hr._apply_project_workspace(
        {"PATH": "/usr/bin:/bin"},
        project_key="marketing",
        workspace="/repos/marketing",
    )
    assert env["CORTEX_PROJECT"] == "marketing"
    assert env["CORTEX_API_URL"] == "http://127.0.0.1:8501"
    assert env["KAIDERA_AGENT_WORKSPACE"] == "/repos/marketing"
    # the project's .agents/scripts is FIRST on PATH (so cortex-* resolves to it)
    assert env["PATH"].split(os.pathsep)[0] == "/repos/marketing/.agents/scripts"


def test_apply_project_workspace_scopes_project_when_workspace_absent():
    """Legacy/single-project path (no workspace) does not prepend PATH or set
    KAIDERA_AGENT_WORKSPACE. A bare project_key still scopes CORTEX_PROJECT, and
    agent-facing Cortex CLIs get a loopback API URL when the parent env is blank."""
    from app import harness_runner as hr

    env = hr._apply_project_workspace({"PATH": "/usr/bin"}, project_key="kaidera-os", workspace=None)
    assert env["PATH"] == "/usr/bin"
    assert "KAIDERA_AGENT_WORKSPACE" not in env
    assert env["CORTEX_PROJECT"] == "kaidera-os"
    assert env["CORTEX_API_URL"] == "http://127.0.0.1:8501"
