"""Track A step 2 — the first feature-module carve: `app/analytics/`.

The analytics feature (usage + est.-cost breakdowns — model / model×provider /
per-agent / cost-by-agent / cost-by-project, plus the slim Cortex KPI row) is
lifted out of `app/main.py`'s blob into a clean vertical module behind the
`OperationalStorePort` (the operational data source). This establishes the carve
PATTERN the remaining modules (agents / settings / dispatch / runs) follow.

The module has three parts and these tests pin each:

  * `app/analytics/service.py` — the feature LOGIC, depending ONLY on
    `OperationalStorePort` (+ a thin model-label/cost formatter callback), NOT on
    `app.main`, the concrete `appdb`/`adapters`, or httpx/fastapi/psycopg2/asyncpg.
    Pure-ish: takes the port, returns dicts. → tested against a FAKE port (no DB):
        1. it SATISFIES nothing structurally itself but CONSUMES the port surface;
        2. `usage_cost(...)` shapes the expected model/provider/agent/cost rollups
           from the fake port's pre-aggregated rows (the metric logic moved from
           `main._analytics_usage_cost`), and
        3. it graceful-degrades — a port reporting `available()==False` (or empty
           rows) yields the 'store not connected' / empty state, never raises.

  * `app/analytics/api.py` — a FastAPI `APIRouter` (the imperative shell — MAY
    import fastapi) whose `GET /analytics/{project}/usage` constructs the service
    over the port (resolved from `app.state` via `Depends`) and returns the JSON.
    → tested by driving the route function directly with a fake port (no ASGI /
    live DB), the same idiom as `test_dispatch_run_route.py`.

These tests are written BEFORE the implementation (strict TDD) and match the
existing fake-driven, no-DB style (`test_adapters_wrap.py`,
`test_dispatch_run_route.py`).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
#  Fake OperationalStorePort — serves scripted, pre-aggregated usage rows.
# ---------------------------------------------------------------------------


class FakeOpStore:
    """Structural `OperationalStorePort` stand-in for the analytics service.

    Only the usage/analytics READ methods the service calls are implemented (the
    settings/flags half of the port is irrelevant here). Each returns scripted
    pre-aggregated rows so the service's SHAPING logic is exercised with no DB.
    `connected` drives `available()` (the store-liveness the view surfaces)."""

    def __init__(
        self,
        *,
        by_model=None,
        by_model_provider=None,
        by_agent=None,
        by_project=None,
        connected=True,
    ):
        self._by_model = by_model if by_model is not None else []
        self._by_model_provider = (
            by_model_provider if by_model_provider is not None else []
        )
        self._by_agent = by_agent if by_agent is not None else []
        self._by_project = by_project if by_project is not None else {}
        self._connected = connected
        self.calls: list[str] = []

    def available(self) -> bool:
        self.calls.append("available")
        return self._connected

    async def usage_by_model(self, project):
        self.calls.append("usage_by_model")
        return list(self._by_model)

    async def usage_by_model_provider(self, project):
        self.calls.append("usage_by_model_provider")
        return list(self._by_model_provider)

    async def usage_by_agent(self, project):
        self.calls.append("usage_by_agent")
        return list(self._by_agent)

    async def usage_by_project(self, project):
        self.calls.append("usage_by_project")
        return dict(self._by_project)


# A realistic pre-aggregated dataset (the shape AppDB.usage_by_* returns).
SAMPLE_BY_MODEL = [
    {"model": "claude-opus-4.8", "provider": "anthropic", "tokens": 1_200_000,
     "tokens_in": 800_000, "tokens_out": 400_000, "cost": 4.5, "runs": 10},
    {"model": "gpt-5.3-codex", "provider": "openai", "tokens": 300_000,
     "tokens_in": 200_000, "tokens_out": 100_000, "cost": 1.0, "runs": 4},
]
SAMPLE_BY_MODEL_PROVIDER = [
    {"model": "claude-opus-4.8", "provider": "anthropic", "tokens": 1_200_000,
     "tokens_in": 800_000, "tokens_out": 400_000, "cost": 4.5, "runs": 10},
    {"model": "gpt-5.3-codex", "provider": "openai", "tokens": 300_000,
     "tokens_in": 200_000, "tokens_out": 100_000, "cost": 1.0, "runs": 4},
]
SAMPLE_BY_AGENT = [
    {"agent": "ren", "model": "claude-opus-4.8", "provider": "anthropic",
     "tokens": 1_000_000, "tokens_in": 700_000, "tokens_out": 300_000,
     "cost": 4.0, "runs": 8},
    {"agent": "kai", "model": "gpt-5.3-codex", "provider": "openai",
     "tokens": 500_000, "tokens_in": 300_000, "tokens_out": 200_000,
     "cost": 1.5, "runs": 6},
    # an agent whose cost was recorded as 0 → must read as "no cost" (n/a)
    {"agent": "bob", "model": "claude-opus-4.8", "provider": "anthropic",
     "tokens": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "runs": 0},
]
SAMPLE_BY_PROJECT = {
    "tokens": 1_500_000, "tokens_in": 1_000_000, "tokens_out": 500_000,
    "cost": 5.5, "runs": 14, "agents": 2, "models": 2,
}

# Roster display-name rows (the shape Cortex get_agents returns); the service uses
# them only to map the bare agent name → a display name.
SAMPLE_AGENTS = [
    {"name": "ren", "capabilities": {"display_name": "Ren"}},
    {"name": "kai", "capabilities": {"display_name": "Kai"}},
]


# ---------------------------------------------------------------------------
#  service.py — the metric logic moved out of main.py (port-only dependency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_usage_cost_shapes_expected_rollups():
    """`AnalyticsService.usage_cost` computes the model/provider/agent/cost
    rollups from the port's pre-aggregated rows — the substance of the view."""
    from app.analytics.service import AnalyticsService

    store = FakeOpStore(
        by_model=SAMPLE_BY_MODEL,
        by_model_provider=SAMPLE_BY_MODEL_PROVIDER,
        by_agent=SAMPLE_BY_AGENT,
        by_project=SAMPLE_BY_PROJECT,
        connected=True,
    )
    svc = AnalyticsService(store=store)
    usage = await svc.usage_cost("kaidera-os", agents=SAMPLE_AGENTS)

    # store-liveness + project totals
    assert usage["store_connected"] is True
    assert usage["total_runs"] == 14
    assert usage["total_tokens"] == 1_500_000
    assert usage["total_tokens_h"] == "1.50M"

    # by-model: two priced models, bars + table present, sorted tokens desc
    assert usage["model_count"] == 2
    assert usage["by_model_table"][0]["model"] == "claude-opus-4.8"
    assert usage["by_model_bars"][0]["pct"] == 100  # top bar fills the track
    assert usage["by_model_bars"][0]["label"] == "claude-opus-4.8"

    # by model×provider: grouped under each provider, provider totals
    provs = {p["provider"]: p for p in usage["by_provider"]}
    assert set(provs) == {"anthropic", "openai"}
    assert provs["anthropic"]["tokens"] == 1_200_000
    assert provs["anthropic"]["models"][0]["model"] == "claude-opus-4.8"
    assert usage["provider_count"] == 2

    # per-agent rows: display-name mapped, zero-cost agent reads n/a
    rows = {r["agent"]: r for r in usage["rows"]}
    assert rows["ren"]["display"] == "Ren"
    assert rows["ren"]["cost"] == 4.0
    assert rows["bob"]["cost"] is None            # recorded-zero → no cost
    assert rows["bob"]["cost_h"] == "n/a"
    assert usage["agents_with_usage"] == 2        # bob has 0 tokens
    assert usage["priced_agent_count"] == 2       # ren + kai priced

    # cost-by-agent: sorted by cost desc (priced first)
    assert usage["cost_rows"][0]["agent"] == "ren"

    # cost-by-project (Σ stored cost)
    assert usage["project_cost"] == 5.5

    # it actually CONSULTED the port (all four usage reads + availability)
    assert "usage_by_model" in store.calls
    assert "usage_by_model_provider" in store.calls
    assert "usage_by_agent" in store.calls
    assert "usage_by_project" in store.calls
    assert "available" in store.calls


@pytest.mark.asyncio
async def test_service_graceful_when_store_down():
    """A store reporting available()==False (and empty rows) yields the
    'store not connected' empty state — never raises (the house law)."""
    from app.analytics.service import AnalyticsService

    store = FakeOpStore(connected=False)  # all rows empty, not connected
    svc = AnalyticsService(store=store)
    usage = await svc.usage_cost("kaidera-os", agents=[])

    assert usage["store_connected"] is False
    assert usage["total_runs"] == 0
    assert usage["total_tokens"] == 0
    assert usage["rows"] == []
    assert usage["by_model_table"] == []
    assert usage["by_provider"] == []
    assert usage["cost_rows"] == []
    assert usage["project_cost"] is None
    assert usage["project_cost_h"] == "n/a"


@pytest.mark.asyncio
async def test_service_empty_but_connected():
    """Connected-but-empty (no runs yet) → store_connected True, zero totals,
    empty breakdowns (the 'no usage recorded yet' state, distinct from down)."""
    from app.analytics.service import AnalyticsService

    store = FakeOpStore(
        by_project={"tokens": 0, "cost": 0.0, "runs": 0}, connected=True
    )
    svc = AnalyticsService(store=store)
    usage = await svc.usage_cost("kaidera-os", agents=SAMPLE_AGENTS)

    assert usage["store_connected"] is True
    assert usage["total_runs"] == 0
    assert usage["rows"] == []
    assert usage["project_cost"] is None


def test_service_depends_only_on_port_not_outward():
    """GUARD: `app/analytics/service.py` imports NOTHING outward (no fastapi /
    httpx / subprocess / psycopg2 / asyncpg) and does NOT reach for `app.main`
    or the concrete `app.appdb` / `app.adapters` — only the domain port.

    Parsed via `ast` (a name in a comment/docstring can't fool it), mirroring
    `test_ports_purity.py`. This is the module-isolation rule the `.importlinter`
    independence contract also enforces at the graph level."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "app" / "analytics" / "service.py"
    ).read_text()
    tree = ast.parse(src)
    top: set[str] = set()
    dotted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top.add(a.name.split(".")[0])
                dotted.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top.add(node.module.split(".")[0])
                dotted.add(node.module)

    forbidden = {"fastapi", "starlette", "httpx", "subprocess", "psycopg2", "asyncpg"}
    assert not (top & forbidden), (
        f"service.py must not import outward I/O libs, got: {sorted(top & forbidden)}"
    )
    # No reaching back into the blob or the concrete adapters/db from the service.
    assert "app.main" not in dotted, "service.py must not import app.main"
    assert not any(
        m == "app.appdb" or m.startswith("app.adapters") for m in dotted
    ), "service.py must depend on the domain port, not the concrete appdb/adapters"


# ---------------------------------------------------------------------------
#  api.py — the FastAPI router (imperative shell; constructs svc over the port)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_usage_endpoint_returns_service_data():
    """Driving the `GET /analytics/{project}/usage` handler directly with a fake
    port returns the service's shaped usage dict (no ASGI / live DB)."""
    from app.analytics import api as analytics_api

    store = FakeOpStore(
        by_model=SAMPLE_BY_MODEL,
        by_model_provider=SAMPLE_BY_MODEL_PROVIDER,
        by_agent=SAMPLE_BY_AGENT,
        by_project=SAMPLE_BY_PROJECT,
        connected=True,
    )
    # The endpoint receives the port via Depends in production; here we pass it
    # directly (the handler is a plain async function of (project, store)).
    result = await analytics_api.usage_endpoint("kaidera-os", store=store)

    assert result["project"] == "kaidera-os"
    assert result["store_connected"] is True
    assert result["total_runs"] == 14
    assert result["model_count"] == 2
    assert any(r["agent"] == "ren" for r in result["rows"])


def test_router_is_apirouter_with_usage_route():
    """`app.analytics.api.router` is a FastAPI APIRouter exposing the usage path
    under the module's prefix (so `main` can `include_router` it additively)."""
    from fastapi import APIRouter

    from app.analytics.api import router

    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    assert "/analytics/{project}/usage" in paths


def test_module_exports_service_and_router():
    """`app.analytics` re-exports the service + router (the module's public face)."""
    import app.analytics as analytics

    assert hasattr(analytics, "AnalyticsService")
    assert hasattr(analytics, "router")
