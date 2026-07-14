"""SPA-surfacing step 4 — the Dispatch ACTIVITY endpoint
(`GET /dispatch/{project}/activity`, `dispatch_activity`).

The SPA Dispatch view surfaces Cole's autonomous-activity ring buffer + the E007
wave-plan strip. The legacy HTML reads these straight off the orchestrator
(`orch.feed.recent(...)` + `orch.status(...)['waves']`) inside `_dispatch_context`,
but that path renders Jinja — there was no JSON endpoint, so the SPA could not reach
the feed/waves. This SMALL additive endpoint exposes EXACTLY that orchestrator state
as JSON, shaped the same way the HTML context sources it.

DESIGN (boundary): the feed + waves are the orchestrator's IN-MEMORY state, read via
`main._orchestrator(request)` → `orch.feed` / `orch.status`. So this endpoint lives in
`app/main.py` (the orchestrator-state reads stay in main, NOT the pure dispatch
module, which must import nothing of the orchestrator). It is strictly additive: a
NEW `/activity` leaf under `/dispatch/{project}` — distinct from `/board` (GET),
`/run` + `/autonomous` (POST) — so it shadows nothing.

GRACEFUL-DEGRADE (house law): when the orchestrator failed to start (`orch is None`)
the endpoint returns the clean idle/empty payload (empty activity, no waves, OFF) —
never a 500. A raising `orch.status` / `orch.feed.recent` likewise degrades.

We drive the route function directly with a fake request carrying a fake
orchestrator on `app.state` (the same no-ASGI idiom as `test_dispatch_run_route.py`).
Written BEFORE the implementation (strict TDD).
"""

from __future__ import annotations

import pytest

import app.main as main_mod


# ---------------------------------------------------------------------------
#  Fakes — a fake orchestrator (feed + status) + a request that carries it.
# ---------------------------------------------------------------------------

class FakeFeed:
    """Structural ActivityFeed — serves scripted recent rows (newest-first)."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = []

    def recent(self, project=None, limit=50):
        self.calls.append((project, limit))
        return [dict(r) for r in self._rows][:limit]


class FakeOrch:
    """Structural orchestrator stand-in: a `feed` + a `status(project)` snapshot.

    `raise_status` / `raise_feed` force that call to RAISE so the route's
    graceful-degrade is exercised."""

    def __init__(self, *, status=None, rows=None, raise_status=False, raise_feed=False):
        self._status = status or {}
        self.feed = _RaisingFeed(rows or []) if raise_feed else FakeFeed(rows or [])
        self._raise_status = raise_status
        self.status_calls = []

    def status(self, project_key=None):
        self.status_calls.append(project_key)
        if self._raise_status:
            raise RuntimeError("status boom")
        return dict(self._status)


class _RaisingFeed:
    def __init__(self, rows):
        self._rows = rows

    def recent(self, project=None, limit=50):
        raise RuntimeError("feed boom")


class FakeState:
    def __init__(self, orch):
        self.orchestrator = orch


class FakeApp:
    def __init__(self, orch):
        self.state = FakeState(orch)


class FakeRequest:
    """Minimal Request: only `.app.state.orchestrator` is read by `_orchestrator`."""

    def __init__(self, orch):
        self.app = FakeApp(orch)


def _waves():
    return {
        "epics": [
            {"epic": "E007", "active_wave": 2, "running": 1, "waiting": 3},
            {"epic": "E006", "active_wave": None, "running": 0, "waiting": 0},
        ],
        "any": True,
    }


def _rows():
    return [
        {
            "seq": 9, "ts": "2026-06-07T10:00:00+00:00", "project": "kaidera-os",
            "kind": "dispatched", "level": "success", "text": "ran kai on h1",
            "agent": "kai", "handoff_id": "h1abc", "handoff_short": "h1abc",
        },
        {
            "seq": 8, "ts": "2026-06-07T09:59:00+00:00", "project": "kaidera-os",
            "kind": "picked_up", "level": "info", "text": "picked up h1",
            "agent": "cole", "handoff_id": "h1abc", "handoff_short": "h1abc",
        },
    ]


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_activity_returns_feed_and_waves_from_orchestrator():
    """The happy path: the endpoint shapes orch.feed.recent + orch.status['waves']
    (and the live autonomy/loop/inflight) into the JSON the SPA renders."""
    orch = FakeOrch(
        status={"loop_running": True, "inflight": 1, "max_concurrent": 3, "waves": _waves()},
        rows=_rows(),
    )
    req = FakeRequest(orch)

    out = await main_mod.dispatch_activity(req, "kaidera-os")

    assert out["project"] == "kaidera-os"
    # The activity feed is surfaced newest-first with the ring-buffer fields.
    assert out["activity_count"] == 2
    assert out["activity"][0]["kind"] == "dispatched"
    assert out["activity"][0]["text"] == "ran kai on h1"
    assert out["activity"][0]["agent"] == "kai"
    assert out["activity"][0]["handoff_short"] == "h1abc"
    # A relative 'ago' is attached (best-effort; non-empty for a real ts).
    assert "ago" in out["activity"][0]
    # The wave-plan strip (E007) — the epics list + an any flag.
    assert out["waves_any"] is True
    assert out["waves"][0]["epic"] == "E007"
    assert out["waves"][0]["active_wave"] == 2
    assert out["waves"][0]["running"] == 1
    assert out["waves"][0]["waiting"] == 3
    # Live loop/autonomy telemetry the strip header shows.
    assert out["loop_running"] is True
    assert out["inflight"] == 1
    assert out["cap"] == 3


@pytest.mark.asyncio
async def test_activity_scopes_feed_to_the_project():
    """The feed is queried for the path project (the ring buffer is multi-project)."""
    orch = FakeOrch(status={"waves": {"epics": [], "any": False}}, rows=_rows())
    req = FakeRequest(orch)

    await main_mod.dispatch_activity(req, "kaidera-os")

    # orch.feed.recent was called scoped to 'kaidera-os'.
    assert orch.feed.calls
    assert orch.feed.calls[0][0] == "kaidera-os"


@pytest.mark.asyncio
async def test_activity_degrades_when_orchestrator_is_none():
    """orch is None (loop failed to start) → the clean idle/empty payload, never a 500."""
    req = FakeRequest(None)

    out = await main_mod.dispatch_activity(req, "kaidera-os")

    assert out["project"] == "kaidera-os"
    assert out["activity"] == []
    assert out["activity_count"] == 0
    assert out["waves"] == []
    assert out["waves_any"] is False
    assert out["loop_running"] is False
    assert out["inflight"] == 0
    assert out["no_orch"] is True


@pytest.mark.asyncio
async def test_activity_degrades_when_status_raises():
    """A raising orch.status must not crash the route — it falls back to empty waves
    + OFF telemetry while still returning the feed it could read."""
    orch = FakeOrch(raise_status=True, rows=_rows())
    req = FakeRequest(orch)

    out = await main_mod.dispatch_activity(req, "kaidera-os")

    assert out["waves"] == []
    assert out["waves_any"] is False
    assert out["loop_running"] is False
    # The feed still came through (status failing is independent of the feed read).
    assert out["activity_count"] == 2


@pytest.mark.asyncio
async def test_activity_degrades_when_feed_raises():
    """A raising orch.feed.recent must not crash the route — empty activity, never 500."""
    orch = FakeOrch(
        status={"loop_running": True, "inflight": 0, "max_concurrent": 3,
                "waves": {"epics": [], "any": False}},
        raise_feed=True,
    )
    req = FakeRequest(orch)

    out = await main_mod.dispatch_activity(req, "kaidera-os")

    assert out["activity"] == []
    assert out["activity_count"] == 0
    # status still read fine.
    assert out["loop_running"] is True
