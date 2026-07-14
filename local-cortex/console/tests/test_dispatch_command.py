"""Shared dispatch worker command.

Pins the execution seam both manual remote dispatch and autonomous dispatch use:
pre-create run_state, spawn exactly one HarnessPort request, terminalize when no
worker starts, and never raise on bad adapters.
"""

from __future__ import annotations

import pytest

from app.dispatch.command import DispatchWorkerSpec, dispatch_worker
from app.domain.harness import SpawnHandle, SpawnRequest


class FakeRunState:
    def __init__(self, *, run_id: str = "run-fixed", raising: bool = False) -> None:
        self.run_id = run_id
        self.raising = raising
        self.started: list[dict] = []
        self.statuses: list[dict] = []

    async def start_run(self, **kw):
        self.started.append(kw)
        if self.raising:
            raise RuntimeError("store down")

        return type("Rec", (), {"run_id": self.run_id})()

    async def set_status(self, run_id, status, *, error=None, metadata=None):
        self.statuses.append({"run_id": run_id, "status": status, "error": error})


class FakePort:
    def __init__(self, handle: SpawnHandle | None = None, *, raising: bool = False) -> None:
        self.handle = handle
        self.raising = raising
        self.requests: list[SpawnRequest] = []

    async def spawn_run(self, request: SpawnRequest) -> SpawnHandle:
        self.requests.append(request)
        if self.raising:
            raise RuntimeError("adapter blew up")
        return self.handle or SpawnHandle(run_id=request.run_id, accepted=True, exit_code=0)


@pytest.mark.asyncio
async def test_dispatch_worker_precreates_row_and_spawns_same_run_id():
    rs = FakeRunState(run_id="run-123")
    port = FakePort()

    outcome = await dispatch_worker(
        DispatchWorkerSpec(
            project="proj",
            agent="worker-a",
            agent_display="Worker A",
            handoff_id="h-1",
            harness="claude-code",
            model="opus",
            repo_root="/workspace/proj",
            lease_owner="orchestrator",
            run_timeout_s=42,
        ),
        runstate=rs,
        harness_port=port,
    )

    assert outcome.status == "ok"
    assert outcome.run_id == "run-123"
    assert rs.started[0]["lease_owner"] == "orchestrator"
    assert rs.started[0]["handoff_id"] == "h-1"
    req = port.requests[0]
    assert req.run_id == "run-123"
    assert req.project == "proj"
    assert req.agent == "worker-a"
    assert req.handoff_id == "h-1"
    assert req.harness == "claude-code"
    assert req.model == "opus"
    assert req.repo_root == "/workspace/proj"
    assert req.run_timeout_s == 42


@pytest.mark.asyncio
async def test_dispatch_worker_rejected_spawn_terminalizes_precreated_row():
    rs = FakeRunState(run_id="run-rejected")
    port = FakePort(SpawnHandle(run_id="run-rejected", accepted=False, error="down"))

    outcome = await dispatch_worker(
        DispatchWorkerSpec(project="proj", agent="worker-a", handoff_id="h-2"),
        runstate=rs,
        harness_port=port,
    )

    assert outcome.status == "rejected"
    assert outcome.error == "down"
    assert rs.statuses[-1] == {
        "run_id": "run-rejected",
        "status": "error",
        "error": "down",
    }


@pytest.mark.asyncio
async def test_dispatch_worker_terminal_exit_reinforces_success_status():
    rs = FakeRunState(run_id="run-ok")
    port = FakePort(SpawnHandle(run_id="run-ok", accepted=True, exit_code=0))

    outcome = await dispatch_worker(
        DispatchWorkerSpec(project="proj", agent="worker-a", handoff_id="h-ok"),
        runstate=rs,
        harness_port=port,
    )

    assert outcome.status == "ok"
    assert rs.statuses[-1] == {
        "run_id": "run-ok",
        "status": "ok",
        "error": None,
    }


@pytest.mark.asyncio
async def test_dispatch_worker_timeout_terminalizes_started_row():
    rs = FakeRunState(run_id="run-timeout")
    port = FakePort(
        SpawnHandle(
            run_id="run-timeout",
            accepted=True,
            exit_code=-1,
            error="run-agent timed out after 900s",
        )
    )

    outcome = await dispatch_worker(
        DispatchWorkerSpec(project="proj", agent="worker-a", handoff_id="h-timeout"),
        runstate=rs,
        harness_port=port,
    )

    assert outcome.status == "error"
    assert rs.statuses[-1] == {
        "run_id": "run-timeout",
        "status": "error",
        "error": "run-agent timed out after 900s",
    }


@pytest.mark.asyncio
async def test_dispatch_worker_bad_adapter_does_not_raise_and_terminalizes():
    rs = FakeRunState(run_id="run-bad")
    port = FakePort(raising=True)

    outcome = await dispatch_worker(
        DispatchWorkerSpec(project="proj", agent="worker-a", handoff_id="h-3"),
        runstate=rs,
        harness_port=port,
    )

    assert outcome.status == "rejected"
    assert "adapter blew up" in (outcome.error or "")
    assert rs.statuses[-1]["status"] == "error"
    assert "adapter blew up" in (rs.statuses[-1]["error"] or "")


@pytest.mark.asyncio
async def test_dispatch_worker_store_failure_still_spawns_with_generated_run_id():
    rs = FakeRunState(raising=True)
    port = FakePort()

    outcome = await dispatch_worker(
        DispatchWorkerSpec(project="proj", agent="worker-a", handoff_id="h-4"),
        runstate=rs,
        harness_port=port,
    )

    assert outcome.status == "ok"
    assert outcome.run_id
    assert port.requests[0].run_id == outcome.run_id


@pytest.mark.asyncio
async def test_dispatch_worker_can_preserve_legacy_no_run_id_degrade():
    rs = FakeRunState(raising=True)
    port = FakePort()

    outcome = await dispatch_worker(
        DispatchWorkerSpec(
            project="proj",
            agent="worker-a",
            handoff_id="h-5",
            require_run_id=False,
        ),
        runstate=rs,
        harness_port=port,
    )

    assert outcome.status == "ok"
    assert outcome.run_id == ""
    assert port.requests[0].run_id == ""
