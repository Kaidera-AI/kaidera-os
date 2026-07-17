"""Track A step 1 — tests for the SDK adapters that WRAP the reused concrete code.

Each adapter in `app/adapters/` implements one pure port (`app/domain/ports.py`)
over the EXISTING concrete module — it is a THIN delegation layer, NOT a rewrite:

  * `cortex_memory.CortexMemoryAdapter`  wraps `cortex_client.CortexClient`  → CortexMemoryPort
  * `llm_harness.HarnessLLMAdapter`      wraps `harness_runner.stream_chat`   → LLMPort
  * `opstore.AppDbOperationalStore`      wraps `appdb.AppDB` + `SettingsDB`   → OperationalStorePort

These tests assert two things per adapter (the contract for this step):
  1. it SATISFIES its port (structural `isinstance` against the runtime_checkable
     Protocol), and
  2. it DELEGATES to the underlying concrete (a fake records the call + args), so
     the wrapper is a faithful 1:1 pass-through, not new behaviour.

The adapters bind the project_key (the console acts as a single low-privilege
reader per project) so the port surface is project-agnostic — the delegation tests
assert the bound project flows through to the underlying CortexClient calls.

The async cases are `async def` tests (pytest-asyncio `auto` mode runs them on the
managed loop) — NOT `asyncio.run(...)` in a sync body, which would churn a fresh
event loop per call and perturb the shared session loop.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
#  Fakes — record the calls the wrappers make to the underlying concrete.
# ---------------------------------------------------------------------------


class FakeCortexClient:
    """Stand-in for cortex_client.CortexClient — records every wrapped call and
    returns scripted values. Signatures mirror the real public methods so a
    delegation mismatch surfaces as a TypeError."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_health(self):
        self.calls.append(("get_health", ()))
        return {"status": "ok"}

    async def search(self, project_key, query, limit=12):
        self.calls.append(("search", (project_key, query, limit)))
        return [{"text": "hit"}]

    async def get_handoffs(self, project_key, status=None):
        self.calls.append(("get_handoffs", (project_key, status)))
        return [{"id": "h1"}]

    async def claim_handoff(self, project_key, handoff_id, agent):
        self.calls.append(("claim_handoff", (project_key, handoff_id, agent)))
        return True

    async def complete_handoff(self, project_key, handoff_id, agent=""):
        self.calls.append(("complete_handoff", (project_key, handoff_id, agent)))
        return True

    async def get_history(self, project_key, limit=200):
        self.calls.append(("get_history", (project_key, limit)))
        return [{"content": "msg"}]


class FakeAppDB:
    """Stand-in for appdb.AppDB (async usage + analytics + autonomy)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def record_usage(self, project, agent, harness, model, provider,
                           tokens_in, tokens_out, cost_est):
        self.calls.append((
            "record_usage",
            (project, agent, harness, model, provider, tokens_in, tokens_out, cost_est),
        ))
        return True

    def available(self):
        self.calls.append(("available", ()))
        return True

    async def usage_by_model(self, project):
        self.calls.append(("usage_by_model", (project,)))
        return [{"model": "opus", "tokens": 1}]

    async def usage_by_model_provider(self, project):
        self.calls.append(("usage_by_model_provider", (project,)))
        return [{"model": "opus", "provider": "anthropic", "tokens": 1}]

    async def usage_by_agent(self, project):
        self.calls.append(("usage_by_agent", (project,)))
        return [{"agent": "ren", "tokens": 1}]

    async def usage_by_project(self, project):
        self.calls.append(("usage_by_project", (project,)))
        return {"tokens": 1}


class FakeSettingsDB:
    """Stand-in for appdb.SettingsDB (sync settings/agent-config/flags)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def load_app_settings(self):
        self.calls.append(("load_app_settings", ()))
        return {"k": "v"}

    def upsert_app_settings(self, items):
        self.calls.append(("upsert_app_settings", (items,)))
        return True

    def load_agent_overrides(self):
        self.calls.append(("load_agent_overrides", ()))
        return {"kaidera-os:ren": {"harness": "pi"}}

    def get_agent_override(self, project, agent):
        self.calls.append(("get_agent_override", (project, agent)))
        return {"harness": "pi"}

    def save_agent_override(self, project, agent, entry):
        self.calls.append(("save_agent_override", (project, agent, entry)))
        return True

    def get_project_autonomy(self, project):
        self.calls.append(("get_project_autonomy", (project,)))
        return True

    def set_project_autonomy(self, project, enabled, updated_by=None):
        self.calls.append(("set_project_autonomy", (project, enabled, updated_by)))
        return True

    def list_autonomous_projects(self):
        self.calls.append(("list_autonomous_projects", ()))
        return ["kaidera-os"]

    def get_project_propose_mode(self, project):
        self.calls.append(("get_project_propose_mode", (project,)))
        return False

    def set_project_propose_mode(self, project, enabled, updated_by=None):
        self.calls.append(("set_project_propose_mode", (project, enabled, updated_by)))
        return True


async def _drain(agen):
    return [ev async for ev in agen]


# ---------------------------------------------------------------------------
#  LLMPort — llm_harness.HarnessLLMAdapter wraps harness_runner.stream_chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_adapter_satisfies_port_and_delegates():
    from app.adapters.llm_harness import HarnessLLMAdapter
    from app.domain.ports import LLMPort

    seen: dict = {}

    async def fake_stream_chat(prompt, model=None, system=None, harness=None,
                               reasoning=None):
        seen.update(
            prompt=prompt, model=model, system=system, harness=harness,
            reasoning=reasoning,
        )
        yield {"type": "delta", "text": "hi"}
        yield {"type": "done"}

    adapter = HarnessLLMAdapter(stream_chat=fake_stream_chat)
    assert isinstance(adapter, LLMPort), "HarnessLLMAdapter must satisfy LLMPort"

    events = await _drain(
        adapter.stream(
            "hello",
            model="opus",
            system="you are ren",
            harness="claude-code",
            reasoning="high",
        )
    )
    # Delegated 1:1 — same kwargs reach stream_chat, same events stream back.
    assert seen == {
        "prompt": "hello",
        "model": "opus",
        "system": "you are ren",
        "harness": "claude-code",
        "reasoning": "high",
    }
    assert events == [{"type": "delta", "text": "hi"}, {"type": "done"}]


def test_llm_adapter_defaults_to_real_runner():
    """With no injected stream_chat, the adapter binds the real
    harness_runner.stream_chat (so production wiring needs no argument)."""
    from app import harness_runner
    from app.adapters.llm_harness import HarnessLLMAdapter

    adapter = HarnessLLMAdapter()
    assert adapter._stream_chat is harness_runner.stream_chat


# ---------------------------------------------------------------------------
#  CortexMemoryPort — cortex_memory.CortexMemoryAdapter wraps CortexClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cortex_memory_adapter_satisfies_port_and_delegates():
    from app.adapters.cortex_memory import CortexMemoryAdapter
    from app.domain.ports import CortexMemoryPort

    fake = FakeCortexClient()
    adapter = CortexMemoryAdapter(fake, project_key="kaidera-os")
    assert isinstance(adapter, CortexMemoryPort)

    # Reads delegate with the bound project flowing through.
    assert await adapter.search("steering") == [{"text": "hit"}]
    assert await adapter.get_handoffs(status="claimed") == [{"id": "h1"}]
    assert await adapter.get_history(limit=50) == [{"content": "msg"}]
    assert await adapter.boot() == {"status": "ok"}

    # The dispatch-lifecycle mutators delegate with project + agent.
    assert await adapter.claim_handoff("h1", "ren") is True
    assert await adapter.complete_handoff("h1", "ren") is True

    names = [c[0] for c in fake.calls]
    assert names == [
        "search", "get_handoffs", "get_history", "get_health",
        "claim_handoff", "complete_handoff",
    ]
    # Bound project_key is injected into the underlying scoped calls.
    assert fake.calls[0] == ("search", ("kaidera-os", "steering", 12))
    assert fake.calls[1] == ("get_handoffs", ("kaidera-os", "claimed"))
    assert fake.calls[4] == ("claim_handoff", ("kaidera-os", "h1", "ren"))


@pytest.mark.asyncio
async def test_cortex_memory_log_is_safe_noop():
    """log() is part of the port surface (callers log decisions/lessons) but the
    read-only console CortexClient has no write-log method — the adapter must
    degrade to a safe no-op (return None), never raise."""
    from app.adapters.cortex_memory import CortexMemoryAdapter

    adapter = CortexMemoryAdapter(FakeCortexClient(), project_key="kaidera-os")
    # Does not raise; returns None (best-effort, console is read-only for logs).
    assert await adapter.log("ren", "decision", "ren:5872 did a thing") is None


# ---------------------------------------------------------------------------
#  OperationalStorePort — opstore.AppDbOperationalStore wraps AppDB + SettingsDB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opstore_satisfies_port_and_delegates():
    from app.adapters.opstore import AppDbOperationalStore
    from app.domain.ports import OperationalStorePort

    appdb = FakeAppDB()
    settings = FakeSettingsDB()
    store = AppDbOperationalStore(appdb=appdb, settings_db=settings)
    assert isinstance(store, OperationalStorePort)

    # async usage/analytics → AppDB
    assert await store.record_usage(
        project="kaidera-os", agent="ren", harness="claude-code", model="opus",
        provider="anthropic", tokens_in=1, tokens_out=2, cost_est=0.01,
    ) is True
    assert store.available() is True
    assert await store.usage_by_model("kaidera-os") == [{"model": "opus", "tokens": 1}]
    assert await store.usage_by_model_provider("kaidera-os") == [
        {"model": "opus", "provider": "anthropic", "tokens": 1}
    ]
    assert await store.usage_by_agent("kaidera-os") == [{"agent": "ren", "tokens": 1}]
    assert await store.usage_by_project("kaidera-os") == {"tokens": 1}

    # sync settings/agent-config/flags → SettingsDB
    assert store.load_app_settings() == {"k": "v"}
    assert store.upsert_app_settings({"a": 1}) is True
    assert store.load_agent_overrides() == {"kaidera-os:ren": {"harness": "pi"}}
    assert store.get_agent_override("kaidera-os", "ren") == {"harness": "pi"}
    assert store.save_agent_override("kaidera-os", "ren", {"harness": "pi"}) is True
    assert store.is_project_autonomous("kaidera-os") is True
    assert store.set_project_autonomy("kaidera-os", True, "ren") is True
    assert store.list_autonomous_projects() == ["kaidera-os"]
    assert store.is_propose_mode("kaidera-os") is False
    assert store.set_propose_mode("kaidera-os", True, "ren") is True

    appdb_calls = [c[0] for c in appdb.calls]
    assert appdb_calls == [
        "record_usage", "available", "usage_by_model", "usage_by_model_provider",
        "usage_by_agent", "usage_by_project",
    ]
    settings_calls = [c[0] for c in settings.calls]
    assert settings_calls == [
        "load_app_settings", "upsert_app_settings", "load_agent_overrides",
        "get_agent_override", "save_agent_override", "get_project_autonomy",
        "set_project_autonomy", "list_autonomous_projects",
        "get_project_propose_mode", "set_project_propose_mode",
    ]


def test_opstore_settings_unavailable_degrades():
    """When SettingsDB returns the UNAVAILABLE sentinel (DB down), the port reads
    degrade to a safe default (False for flags, {} for maps) — never the sentinel,
    never a raise — mirroring app/settings.py's fallback contract."""
    from app.adapters.opstore import AppDbOperationalStore
    from app.appdb import UNAVAILABLE

    class DownSettings(FakeSettingsDB):
        def get_project_autonomy(self, project):
            return UNAVAILABLE

        def get_project_propose_mode(self, project):
            return UNAVAILABLE

        def load_agent_overrides(self):
            return UNAVAILABLE

        def load_app_settings(self):
            return UNAVAILABLE

    store = AppDbOperationalStore(appdb=FakeAppDB(), settings_db=DownSettings())
    assert store.is_project_autonomous("kaidera-os") is False
    assert store.is_propose_mode("kaidera-os") is False
    assert store.load_agent_overrides() == {}
    assert store.load_app_settings() == {}
