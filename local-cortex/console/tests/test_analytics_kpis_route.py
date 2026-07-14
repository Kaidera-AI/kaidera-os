"""Console Analytics-KPIS route tests (`app/analytics/api.py` → `GET /analytics/{project}/kpis`).

The Analytics view's slim headline KPI strip (Events/24h · Active tasks · Decisions · recent
Tokens) needs a JSON surface so the SPA `AnalyticsView` can render it. The legacy HTML built
these server-side (`main._analytics_view` headline block) from Cortex `/state` + the recent-
decisions count + the App-DB project token total, but exposed NO JSON for them — the existing
`/analytics/{p}/usage` covers ONLY the tokens/cost breakdown, not the KPI counters. This is the
missing JSON KPI surface.

  * `GET /analytics/{project}/kpis` → `{project, events_24h, active_tasks, pending_handoffs,
    decisions_recent, window_days, tokens_recent, tokens_recent_h}`:
      - the three /state counters survive as null when /state is unreachable (the SPA renders
        'n/a' — NEVER fabricated zeros),
      - `decisions_recent` is the count of decisions in the trailing window,
      - `tokens_recent`/`tokens_recent_h` come from the App-DB project token rollup (the same
        source the usage view's 'Tokens · recent' KPI uses).

Driven via an in-process httpx ASGITransport over a minimal app mounting the analytics router,
with a FAKE cortex (scripted get_state + get_decisions_recent_count) + a FAKE store (a project
token total) on app.state — NO live Cortex / DB. Each read graceful-degrades, so the route
NEVER 500s.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.analytics.api import router as analytics_router


class FakeCortexForKpis:
    """A minimal CortexClient stand-in: scriptable get_state + get_decisions_recent_count.
    Either may raise to exercise graceful-degrade. Records the projects asked."""

    def __init__(self, *, state=None, decisions_recent=0, state_raises=False, dec_raises=False):
        self._state = state if state is not None else {}
        self._decisions_recent = decisions_recent
        self._state_raises = state_raises
        self._dec_raises = dec_raises
        self.state_calls = []
        self.dec_calls = []

    async def get_state(self, project_key):
        self.state_calls.append(project_key)
        if self._state_raises:
            raise RuntimeError("cortex state down")
        return self._state

    async def get_decisions_recent_count(self, project_key, since):
        self.dec_calls.append({"project": project_key, "since": since})
        if self._dec_raises:
            raise RuntimeError("cortex decisions down")
        return self._decisions_recent


class FakeStoreForKpis:
    """A minimal OperationalStorePort stand-in: a project token+runs rollup + availability.
    `usage_by_project` mirrors the real port's shape ({tokens, cost, runs})."""

    def __init__(self, *, tokens=0, runs=0, available=True, raises=False):
        self._tokens = tokens
        self._runs = runs
        self._available = available
        self._raises = raises

    def available(self):
        return self._available

    async def usage_by_project(self, project):
        if self._raises:
            raise RuntimeError("store down")
        return {"tokens": self._tokens, "cost": None, "runs": self._runs}


_STATE = {"summary": {"active_tasks": 5, "pending_handoffs": 3, "events_24h": 21}}


def _make_app(*, cortex=None, store=None):
    app = FastAPI()
    app.include_router(analytics_router)
    app.state.cortex = cortex if cortex is not None else FakeCortexForKpis()
    # the analytics deps resolve the opstore off app.state.opstore (the kpis route uses it for
    # the token rollup); a None falls back to wrapping appdb — we set the fake directly.
    app.state.opstore = store if store is not None else FakeStoreForKpis()
    return app


def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.test")


# ---------------------------------------------------------------------------
#  GET /analytics/{project}/kpis — the headline KPI strip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kpis_route_returns_headline_counters():
    cortex = FakeCortexForKpis(state=_STATE, decisions_recent=12)
    store = FakeStoreForKpis(tokens=1_240_000, runs=5)
    app = _make_app(cortex=cortex, store=store)
    async with _client(app) as c:
        resp = await c.get("/analytics/demo/kpis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "demo"
    assert data["events_24h"] == 21
    assert data["active_tasks"] == 5
    assert data["pending_handoffs"] == 3
    assert data["decisions_recent"] == 12
    assert data["window_days"] >= 1
    # recent tokens from the App-DB project rollup (humanised too).
    assert data["tokens_recent"] == 1_240_000
    assert data["tokens_recent_h"] == "1.24M"
    # the decisions count was queried over the trailing window (a since arg was passed).
    assert cortex.dec_calls and cortex.dec_calls[0]["since"]


@pytest.mark.asyncio
async def test_kpis_route_tokens_none_when_no_usage():
    """No recorded usage → tokens_recent 0 + a null humanised label (the SPA shows 'n/a')."""
    cortex = FakeCortexForKpis(state=_STATE, decisions_recent=0)
    store = FakeStoreForKpis(tokens=0, runs=0)
    app = _make_app(cortex=cortex, store=store)
    async with _client(app) as c:
        resp = await c.get("/analytics/demo/kpis")
    data = resp.json()
    assert data["tokens_recent"] == 0
    assert data["tokens_recent_h"] is None


# ---------------------------------------------------------------------------
#  Graceful-degrade — a down/None Cortex / store never 500s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kpis_route_degrades_on_down_cortex():
    """get_state + decisions both RAISE → null counters, HTTP 200 (never fabricated zeros)."""
    cortex = FakeCortexForKpis(state_raises=True, dec_raises=True)
    store = FakeStoreForKpis(tokens=0, runs=0)
    app = _make_app(cortex=cortex, store=store)
    async with _client(app) as c:
        resp = await c.get("/analytics/demo/kpis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["events_24h"] is None
    assert data["active_tasks"] is None
    assert data["pending_handoffs"] is None
    assert data["decisions_recent"] is None


@pytest.mark.asyncio
async def test_kpis_route_degrades_on_down_store():
    """The token store raising must NOT 500 — tokens degrade to 0/null, the KPIs still answer."""
    cortex = FakeCortexForKpis(state=_STATE, decisions_recent=4)
    store = FakeStoreForKpis(raises=True)
    app = _make_app(cortex=cortex, store=store)
    async with _client(app) as c:
        resp = await c.get("/analytics/demo/kpis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_tasks"] == 5           # the Cortex KPIs are intact
    assert data["tokens_recent"] == 0          # the down store degrades alone
    assert data["tokens_recent_h"] is None


@pytest.mark.asyncio
async def test_kpis_route_no_cortex_on_state_degrades():
    """app.state.cortex is None → still answers null counters, never a 500/AttributeError."""
    app = FastAPI()
    app.include_router(analytics_router)
    app.state.cortex = None
    app.state.opstore = FakeStoreForKpis(tokens=0, runs=0)
    async with _client(app) as c:
        resp = await c.get("/analytics/demo/kpis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["events_24h"] is None
    assert data["decisions_recent"] is None


def test_kpis_route_is_collision_free():
    """The `/analytics/{project}/kpis` leaf is distinct from the existing
    `/analytics/{project}/usage` route — strictly additive (route introspection)."""
    paths = {getattr(r, "path", None) for r in analytics_router.routes}
    assert "/analytics/{project}/kpis" in paths
    assert "/analytics/{project}/usage" in paths
