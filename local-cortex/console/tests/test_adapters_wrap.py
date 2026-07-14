"""Track A step 1 — tests for the SDK adapters that WRAP the reused concrete code.

Each adapter in `app/adapters/` implements one pure port (`app/domain/ports.py`)
over the EXISTING concrete module — it is a THIN delegation layer, NOT a rewrite:

  * `cortex_memory.CortexMemoryAdapter`  wraps `cortex_client.CortexClient`  → CortexMemoryPort
  * `llm_harness.HarnessLLMAdapter`      wraps `harness_runner.stream_chat`   → LLMPort
  * `opstore.AppDbOperationalStore`      wraps `appdb.AppDB` + `SettingsDB`   → OperationalStorePort
  * `model_catalog.ProvidersModelCatalog` wraps `providers` (catalog funcs)   → ModelCatalogPort
  * `billing.OperationalStoreBilling`    wraps the usage path (stub)          → BillingPort

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


# ---------------------------------------------------------------------------
#  ModelCatalogPort — model_catalog.ProvidersModelCatalog wraps providers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_catalog_satisfies_port_and_delegates(monkeypatch):
    import app.adapters.model_catalog as mc_mod
    from app.adapters.model_catalog import ProvidersModelCatalog
    from app.domain.ports import CatalogModel, ModelCatalogPort

    raw_catalog = {"groups": []}
    view = {
        "groups": [
            {
                "provider": "anthropic",
                "label": "Anthropic",
                "rows": [
                    {
                        "id": "claude-opus-4.8",
                        "display_name": "Claude Opus 4.8",
                        "type": "chat",
                    }
                ],
            }
        ]
    }

    async def fake_get_catalog(force=False):
        return raw_catalog

    def fake_view_catalog(catalog):
        assert catalog is raw_catalog
        return view

    def fake_pricing_index(catalog):
        return {"idx": True}

    def fake_resolve_model(model_id, index):
        assert index == {"idx": True}
        return {
            "provider": "anthropic",
            "price_in_per_mtok": 15.0,
            "price_out_per_mtok": 75.0,
            "resolved": True,
            "priced": True,
        }

    monkeypatch.setattr(mc_mod.providers, "get_catalog", fake_get_catalog)
    monkeypatch.setattr(mc_mod.providers, "view_catalog", fake_view_catalog)
    monkeypatch.setattr(mc_mod.providers, "pricing_index", fake_pricing_index)
    monkeypatch.setattr(mc_mod.providers, "resolve_model", fake_resolve_model)

    catalog = ProvidersModelCatalog()
    assert isinstance(catalog, ModelCatalogPort)

    models = await catalog.list_models()
    assert len(models) == 1
    m = models[0]
    assert isinstance(m, CatalogModel)
    assert m.provider == "anthropic"
    assert m.id == "claude-opus-4.8"
    assert m.display_name == "Claude Opus 4.8"

    price = await catalog.price_for("opus")
    assert price.provider == "anthropic"
    assert price.price_in_per_mtok == 15.0
    assert price.price_out_per_mtok == 75.0


@pytest.mark.asyncio
async def test_model_catalog_prefers_raw_catalog_rows(monkeypatch):
    """When the raw catalog `groups[].models[]` is populated, list_models reads it
    (the richest numeric source) rather than the formatted view rows."""
    import app.adapters.model_catalog as mc_mod
    from app.adapters.model_catalog import ProvidersModelCatalog

    raw_catalog = {
        "groups": [
            {
                "provider": "anthropic",
                "models": [
                    {
                        "provider": "anthropic",
                        "id": "claude-opus-4.8",
                        "display_name": "Claude Opus 4.8",
                        "type": "chat",
                        "price_in_per_mtok": 15.0,
                        "price_out_per_mtok": 75.0,
                        "context_window": 200000,
                        "reasoning_levels": ["low", "high"],
                        "source": "live",
                    }
                ],
            }
        ]
    }

    async def fake_get_catalog(force=False):
        return raw_catalog

    def boom_view_catalog(catalog):  # must NOT be called when raw rows exist
        raise AssertionError("view_catalog should not be used when raw rows exist")

    monkeypatch.setattr(mc_mod.providers, "get_catalog", fake_get_catalog)
    monkeypatch.setattr(mc_mod.providers, "view_catalog", boom_view_catalog)

    models = await ProvidersModelCatalog().list_models()
    assert len(models) == 1
    m = models[0]
    assert m.price_in_per_mtok == 15.0
    assert m.context_window == 200000
    assert m.reasoning_levels == ["low", "high"]


# ---------------------------------------------------------------------------
#  BillingPort — billing.OperationalStoreBilling over the usage path (stub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_billing_satisfies_port_and_delegates_to_opstore():
    from app.adapters.billing import OperationalStoreBilling
    from app.domain.ports import BillingPort

    appdb = FakeAppDB()
    billing = OperationalStoreBilling(appdb=appdb)
    assert isinstance(billing, BillingPort)

    ok = await billing.record_usage(
        run_id="run-1",
        tokens_in=100,
        tokens_out=50,
        cost=0.02,
        project="kaidera-os",
        agent="ren",
        harness="claude-code",
        model="opus",
        provider="anthropic",
    )
    assert ok is True
    assert len(appdb.calls) == 1
    name, args = appdb.calls[0]
    assert name == "record_usage"
    # tokens + cost flow through to the usage_events write path.
    assert args[5] == 100  # tokens_in
    assert args[6] == 50   # tokens_out
    assert args[7] == 0.02  # cost_est


@pytest.mark.asyncio
async def test_billing_record_usage_never_raises():
    """A down usage path must not break the run — record_usage swallows failures
    and reports False (the BillingPort graceful-degrade contract)."""
    from app.adapters.billing import OperationalStoreBilling

    class BoomDB:
        async def record_usage(self, *a, **k):
            raise RuntimeError("db down")

    billing = OperationalStoreBilling(appdb=BoomDB())
    ok = await billing.record_usage(run_id="r", tokens_in=1, tokens_out=1, cost=0.0)
    assert ok is False
