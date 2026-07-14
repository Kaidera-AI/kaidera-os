from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.main import _runstate_restart_status


class FakeRunStateStore:
    def __init__(self, rows):
        self.rows = rows
        self.projects = []

    async def list_active(self, project=None):
        self.projects.append(project)
        return self.rows


@pytest.mark.asyncio
async def test_runstate_restart_status_degrades_without_store():
    result = await _runstate_restart_status(None, "demo", 123)
    assert result["ok"] is False
    assert result["store"] == "degraded"
    assert result["active"] == []


@pytest.mark.asyncio
async def test_runstate_restart_status_classifies_detached_and_request_lived_runs():
    store = FakeRunStateStore([
        SimpleNamespace(
            run_id="worker-1",
            project="demo",
            agent="builder",
            handoff_id="h1",
            status="running",
            lease_owner="worker",
            pid=777,
            heartbeat_at="2026-06-24T10:00:00Z",
            updated_at="2026-06-24T10:00:01Z",
        ),
        SimpleNamespace(
            run_id="chat-1",
            project="demo",
            agent="lead",
            handoff_id=None,
            status="running",
            lease_owner="chat",
            pid=122,
            heartbeat_at=None,
            updated_at="2026-06-24T10:00:02Z",
        ),
        SimpleNamespace(
            run_id="chat-2",
            project="demo",
            agent="lead",
            handoff_id=None,
            status="running",
            lease_owner="chat",
            pid=123,
            heartbeat_at=None,
            updated_at="2026-06-24T10:00:03Z",
        ),
    ])

    result = await _runstate_restart_status(store, "demo", 123)

    assert result["ok"] is True
    assert store.projects == ["demo"]
    assert result["counts"] == {
        "active": 3,
        "restart_survivable": 1,
        "request_lived": 2,
        "needs_reconcile": 1,
    }
    rows = {row["run_id"]: row for row in result["active"]}
    assert rows["worker-1"]["lifecycle"] == "restart_survivable"
    assert rows["worker-1"]["restart_survivable"] is True
    assert rows["chat-1"]["lifecycle"] == "needs_reconcile"
    assert rows["chat-1"]["needs_reconcile"] is True
    assert rows["chat-2"]["lifecycle"] == "live_request"


@pytest.mark.asyncio
async def test_runstate_restart_status_handles_store_error():
    class BrokenStore:
        async def list_active(self, project=None):
            raise RuntimeError("appdb down")

    result = await _runstate_restart_status(BrokenStore(), "demo", 123)
    assert result["ok"] is False
    assert result["store"] == "error"
    assert result["error"] == "appdb down"
