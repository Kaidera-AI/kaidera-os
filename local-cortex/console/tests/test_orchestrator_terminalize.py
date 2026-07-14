"""Runtime blocker C: terminalize a pre-created run row when the WORKER never started.

A rejected spawn / spawn exception means no worker exists to write the row's terminal
status, so without `_terminalize_stranded_run` the row strands at 'queued' forever (the
run looks perpetually live + the concurrency accounting lies). These prove the helper
sets 'error' on the never-started paths and is a safe no-op otherwise.
"""

from __future__ import annotations

import pytest

from app.orchestrator import Orchestrator


class _FakeRunstate:
    def __init__(self, *, raises=False):
        self.calls: list = []
        self._raises = raises

    async def set_status(self, run_id, status, *, error=None, metadata=None):
        self.calls.append((run_id, status, error))
        if self._raises:
            raise RuntimeError("store down")


def _orch(runstate):
    """Bare Orchestrator with only `_runstate` set (bypass __init__'s wiring)."""
    o = Orchestrator.__new__(Orchestrator)
    o._runstate = runstate
    return o


@pytest.mark.asyncio
async def test_terminalize_sets_error_on_stranded_run():
    rs = _FakeRunstate()
    await _orch(rs)._terminalize_stranded_run("r1", "spawn rejected")
    assert rs.calls == [("r1", "error", "spawn rejected")]


@pytest.mark.asyncio
async def test_terminalize_noop_on_none_run_id():
    rs = _FakeRunstate()
    await _orch(rs)._terminalize_stranded_run(None, "x")
    assert rs.calls == []  # no run row to terminalize


@pytest.mark.asyncio
async def test_terminalize_noop_when_no_store():
    # No runstate store wired → no-op, never raises.
    await _orch(None)._terminalize_stranded_run("r1", "x")


@pytest.mark.asyncio
async def test_terminalize_swallows_store_errors():
    rs = _FakeRunstate(raises=True)
    # A down store must never propagate out of the dispatch path.
    await _orch(rs)._terminalize_stranded_run("r1", "boom")
    assert rs.calls  # attempted
