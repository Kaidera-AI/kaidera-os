"""Console Agents-EPICS route tests (`app/agents/api.py` → `GET /agents/{project}/epics`).

The col-2 Active-Epic widget + the project metrics block need a JSON surface so the SPA
`AgentsColumn` can render the per-epic progress bars (+ per-increment mini-bars) and the
metrics drill-in (active tasks / pending tasks / pending handoffs / events-24h). The legacy
HTML built these server-side (`main._epic_view` / `_metrics_view`) but exposed NO JSON; this
is that missing JSON LIST surface, shaped by the PURE `app.agents.epics` helpers (lifted 1:1
from the main builders, so the JSON and the HTML share one source of the shaping).

  * `GET /agents/{project}/epics` → `{project, epic:{mode, epics[], epic_count}, metrics:{...}}`:
      - `epic.mode == 'epics'`  → the shaped, ACTIVE-MAJOR epic stack (each carrying epic_id ·
        title · overall_pct + per-increment {num,label,pct,status,kind}).
      - `epic.mode == 'continuous'` → the 'continuous · no epics' line (no epics / a degraded
        /epics read — NEVER fabricated progress).
      - `metrics` → {active_tasks, pending_tasks, pending_handoffs, events_24h} (a None counter
        survives as null → the SPA renders '—').

Driven via an in-process httpx ASGITransport over a minimal app that mounts the agents router,
with a FAKE cortex on `app.state.cortex` (scripted get_epics + get_state + get_board) — NO live
Cortex, nothing spawned. The three Cortex reads run concurrently + each graceful-degrades, so a
down section blanks alone and the route NEVER 500s.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.agents.api import router as agents_router


class FakeCortexForEpics:
    """A minimal CortexClient stand-in for the epics route: scriptable get_epics + get_state +
    get_board (+ a get_agents so the shared router deps don't blow up if touched). Any of the
    three may raise to exercise graceful-degrade. Records the projects it was asked."""

    def __init__(
        self,
        *,
        epics=None,
        state=None,
        board=None,
        epics_raises=False,
        state_raises=False,
        board_raises=False,
    ):
        self._epics = epics if epics is not None else {"epics": []}
        self._state = state if state is not None else {}
        self._board = board if board is not None else []
        self._epics_raises = epics_raises
        self._state_raises = state_raises
        self._board_raises = board_raises
        self.epics_calls = []
        self.state_calls = []
        self.board_calls = []

    async def get_epics(self, project_key):
        self.epics_calls.append(project_key)
        if self._epics_raises:
            raise RuntimeError("cortex epics down")
        return self._epics

    async def get_state(self, project_key):
        self.state_calls.append(project_key)
        if self._state_raises:
            raise RuntimeError("cortex state down")
        return self._state

    async def get_board(self, project_key):
        self.board_calls.append(project_key)
        if self._board_raises:
            raise RuntimeError("cortex board down")
        return self._board

    async def get_agents(self, project_key):  # pragma: no cover - not used by the epics route
        return []


# A representative /epics payload: one ACTIVE (build) epic with three increments + one
# completed epic — so we can assert the active-major sort + the increment shaping.
_EPICS = {
    "project": "demo",
    "epics": [
        {
            "epic_id": "E007",
            "title": "Console parity",
            "status": "build",
            "overall_pct": 62,
            "increments": [
                {"num": 1, "title": "History view", "status": "done", "pct": 100},
                {"num": 2, "title": "Graph view", "status": "in_progress", "pct": 40},
                {"num": 3, "title": "Roster polish", "status": "todo", "pct": 0},
            ],
        },
        {
            "epic_id": "E006",
            "title": "Registry",
            "status": "done",
            "overall_pct": 100,
            "increments": [],
        },
    ],
}

_STATE = {"summary": {"active_tasks": 4, "pending_handoffs": 2, "events_24h": 17}}

# Two board tasks: one live (in_progress) + one pending (open) → pending_tasks == 1.
_BOARD = [
    {"id": "t1", "status": "in_progress"},
    {"id": "t2", "status": "open"},
]


def _make_app(*, cortex=None):
    app = FastAPI()
    app.include_router(agents_router)
    app.state.cortex = cortex if cortex is not None else FakeCortexForEpics()
    # the shared agents deps resolve an opstore off app.state; the epics route does not use it,
    # but set a harmless sentinel so a touch can't AttributeError.
    app.state.opstore = None
    return app


def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.test")


# ---------------------------------------------------------------------------
#  GET /agents/{project}/epics — the epic stack (mode='epics')
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_epics_route_shapes_active_major_stack():
    cortex = FakeCortexForEpics(epics=_EPICS, state=_STATE, board=_BOARD)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/agents/demo/epics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "demo"

    epic = data["epic"]
    assert epic["mode"] == "epics"
    assert epic["epic_count"] == 2
    # ACTIVE-major sort: the build epic (E007) leads, the completed one (E006) follows.
    ids = [e["epic_id"] for e in epic["epics"]]
    assert ids[0] == "E007"
    lead = epic["epics"][0]
    assert lead["overall_pct"] == 62
    assert lead["is_active"] is True
    # the increments are shaped into {num,label,pct,status,kind}.
    incs = lead["increments"]
    assert len(incs) == 3
    assert incs[0]["label"] == "Inc1"
    assert incs[0]["kind"] == "done"
    assert incs[1]["kind"] == "prog"     # in_progress → teal
    assert incs[2]["kind"] == "todo"     # todo → empty track
    assert incs[1]["pct"] == 40


@pytest.mark.asyncio
async def test_epics_route_carries_metrics_block():
    cortex = FakeCortexForEpics(epics=_EPICS, state=_STATE, board=_BOARD)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/agents/demo/epics")
    metrics = resp.json()["metrics"]
    assert metrics["active_tasks"] == 4
    assert metrics["pending_handoffs"] == 2
    assert metrics["events_24h"] == 17
    # pending_tasks is DERIVED from the board (the open task; the in_progress one is live).
    assert metrics["pending_tasks"] == 1


# ---------------------------------------------------------------------------
#  Continuous-backlog projects (no epics) → mode='continuous'
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_epics_route_continuous_when_no_epics():
    cortex = FakeCortexForEpics(epics={"epics": []}, state=_STATE, board=[])
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/agents/demo/epics")
    assert resp.status_code == 200
    epic = resp.json()["epic"]
    assert epic["mode"] == "continuous"
    assert epic["epics"] == []
    assert epic["epic_count"] == 0
    assert epic["label"]  # a 'continuous · no epics' line


# ---------------------------------------------------------------------------
#  Graceful-degrade — a down/None Cortex never 500s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_epics_route_degrades_to_continuous_on_down_cortex():
    """get_epics/state/board all RAISE → continuous epic + null metrics, HTTP 200 (never 500)."""
    cortex = FakeCortexForEpics(epics_raises=True, state_raises=True, board_raises=True)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/agents/demo/epics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["epic"]["mode"] == "continuous"
    # a degraded /state → null counters (the SPA renders '—'), never fabricated zeros.
    metrics = data["metrics"]
    assert metrics["active_tasks"] is None
    assert metrics["pending_handoffs"] is None
    assert metrics["events_24h"] is None
    assert metrics["pending_tasks"] == 0   # no board rows → zero pending derived


@pytest.mark.asyncio
async def test_epics_route_no_cortex_on_state_degrades():
    """app.state.cortex is None → still answers the clean continuous payload, never a 500."""
    app = FastAPI()
    app.include_router(agents_router)
    app.state.cortex = None
    app.state.opstore = None
    async with _client(app) as c:
        resp = await c.get("/agents/demo/epics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["epic"]["mode"] == "continuous"
    assert data["metrics"]["pending_tasks"] == 0


@pytest.mark.asyncio
async def test_epics_route_partial_degrade_keeps_epics():
    """One section down (board raises) must NOT blank the epics — the stack still shapes."""
    cortex = FakeCortexForEpics(epics=_EPICS, state=_STATE, board_raises=True)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/agents/demo/epics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["epic"]["mode"] == "epics"          # epic stack intact
    assert data["metrics"]["active_tasks"] == 4      # state intact
    assert data["metrics"]["pending_tasks"] == 0     # the down board degrades alone


def test_epics_route_is_collision_free():
    """The THREE-segment `/agents/{project}/epics` leaf is distinct from the HTML
    agent-detail pane (`main` GET /agents/{p}/{a}`) and the JSON `/detail` + `/config-catalog`
    leaves — strictly additive (asserted by route introspection on the agents router)."""
    paths = {getattr(r, "path", None) for r in agents_router.routes}
    assert "/agents/{project}/epics" in paths
    # the sibling JSON leaves keep their distinct shapes.
    assert "/agents/{project}/{agent}/detail" in paths
    assert "/agents/{project}/config-catalog" in paths
