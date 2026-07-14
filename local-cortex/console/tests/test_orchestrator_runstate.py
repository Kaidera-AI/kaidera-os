"""T5/T12 — the orchestrator pre-creates the run row + passes the run_id.

Milestone 1 (RunState SSOT). BEFORE spawning the detached worker, `_dispatch_run`:
  * calls `runstate.start_run(project=…, agent=…, handoff_id=…, harness=…, model=…,
    lease_owner="orchestrator")` → a uuid4 run_id, and
  * passes that run_id to the worker as a NEW argv[4]:
    [RUN_AGENT_SCRIPT, target_name, hid, project_key, run_id].

The store is the ONE live-state path: the in-memory `transcripts.start_run(...)`
dual-write was REMOVED at T12 (the orchestrator no longer holds an in-memory
transcript store at all). The store is graceful-degrade: a None/failing store must
not break the spawn.

These pin the contract with the child process MOCKED (no live run), matching
test_orchestrator_spawn.py.
"""
from __future__ import annotations

import subprocess

import pytest

import app.orchestrator as orch
from app.orchestrator import Orchestrator


class _FakeProc:
    """Stand-in for subprocess.Popen — records the argv and returns rc=0."""

    last_argv: list[str] | None = None

    def __init__(self, argv, **kwargs):
        _FakeProc.last_argv = list(argv)
        self.kwargs = kwargs
        self.returncode = None

    def communicate(self, timeout=None):
        self.returncode = 0
        return (None, "")

    def wait(self):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeRunState:
    """Records start_run and hands back a fixed run_id (so the test can assert it
    flows into the spawn argv). Structurally a RunStatePort writer."""

    def __init__(self, run_id="run-fixed-id", fail=False):
        self._run_id = run_id
        self._fail = fail
        self.start_calls: list[dict] = []

    async def start_run(self, **kw):
        self.start_calls.append(kw)
        if self._fail:
            raise RuntimeError("store down")
        # Mirror RunStatePgStore: return an object exposing .run_id.
        from app.domain.runstate import RunRecord
        return RunRecord(run_id=self._run_id, project=kw.get("project"),
                         agent=kw.get("agent"), status="queued")


def _make_orch(runstate):
    """An Orchestrator with inert stubs + an injected runstate port. _dispatch_run
    only touches chat_routing_for (suppressed), the self-built transcripts/feed/
    inflight, and now runstate."""
    async def _noop_pm_beat(project_key: str, *, reason: str) -> None:
        return None

    o = Orchestrator(
        cortex=object(),
        appdb=object(),
        harness_runner=object(),
        chat_routing_for=lambda agent, project: ("pi", "gpt-5.3-codex-spark", "high"),
        record_usage=None,
        find_agent=lambda agents, name: None,
        resolve_target=lambda handoff, agents: None,
        classify_interactive=lambda agent, desig: False,
        project_identity=lambda cortex, project: None,
        agent_view=lambda a: a,
        runstate=runstate,
    )
    o._pm_beat = _noop_pm_beat  # type: ignore[method-assign]
    return o


def _feed_kinds(o, project="kaidera-os"):
    return [e.get("kind") for e in o.feed.recent(project)]


@pytest.mark.asyncio
async def test_dispatch_precreates_runstate_and_passes_run_id(monkeypatch):
    """_dispatch_run calls runstate.start_run with the run header AND threads the
    returned run_id into the worker argv as argv[4]."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    rs = FakeRunState(run_id="run-XYZ")
    o = _make_orch(rs)
    o._inflight["kaidera-os"] = 1
    handoff = {"id": "h-abc12345", "summary": "do the thing"}
    target = {"name": "bob", "display_name": "Bob"}

    await o._dispatch_run("kaidera-os", handoff, target)

    # start_run was called once with the run header (project/agent/handoff_id +
    # lease_owner='orchestrator'; harness/model resolved best-effort).
    assert len(rs.start_calls) == 1
    call = rs.start_calls[0]
    assert call["project"] == "kaidera-os"
    assert call["agent"] == "bob"
    assert call["handoff_id"] == "h-abc12345"
    assert call["lease_owner"] == "orchestrator"
    assert call.get("harness") == "pi"

    # the run_id is passed to the worker as the NEW argv[4].
    assert _FakeProc.last_argv == [
        orch.RUN_AGENT_SCRIPT, "bob", "h-abc12345", "kaidera-os", "run-XYZ"
    ]


@pytest.mark.asyncio
async def test_dispatch_writes_only_the_store_no_in_memory_transcript(monkeypatch):
    """T12: the store is the ONLY live-state write — the in-memory transcript store
    (and its dual-write) was removed. The store write still happens, the run still
    completes, and the orchestrator no longer exposes a `transcripts` attribute."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    rs = FakeRunState()
    o = _make_orch(rs)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-store1", "summary": "s"}, {"name": "bob"})

    # the store write happened (the single, durable live-state write).
    assert len(rs.start_calls) == 1
    assert rs.start_calls[0]["handoff_id"] == "h-store1"
    assert rs.start_calls[0]["agent"] == "bob"
    # the in-memory transcript store is GONE — no proxy left on the orchestrator.
    assert not hasattr(o, "transcripts")
    assert not hasattr(o, "recent_runs")
    assert not hasattr(o, "latest_run")
    # slot released, completed outcome recorded (existing behaviour intact).
    assert o._inflight["kaidera-os"] == 0
    assert "completed" in _feed_kinds(o)


@pytest.mark.asyncio
async def test_dispatch_degrades_when_runstate_fails(monkeypatch):
    """GRACEFUL-DEGRADE: if start_run RAISES, the spawn STILL happens (the run
    proceeds). The worker just doesn't get a run_id (no argv[4]) — store writes are
    skipped worker-side, exactly the no-run_id back-compat path."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    rs = FakeRunState(fail=True)
    o = _make_orch(rs)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-deg1", "summary": "s"}, {"name": "bob"})

    # spawn happened despite the store failure (run is never blocked by the store).
    assert _FakeProc.last_argv is not None
    assert _FakeProc.last_argv[:4] == [orch.RUN_AGENT_SCRIPT, "bob", "h-deg1", "kaidera-os"]
    # no run_id appended (start_run failed → nothing to pass).
    assert len(_FakeProc.last_argv) == 4
    # the run still completed + released its slot.
    assert o._inflight["kaidera-os"] == 0
    assert "completed" in _feed_kinds(o)


@pytest.mark.asyncio
async def test_dispatch_without_runstate_injected_is_legacy_argv(monkeypatch):
    """BACK-COMPAT: an Orchestrator constructed with no runstate (runstate=None)
    behaves exactly as before — the 4-arg legacy spawn, no store writes."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    o = _make_orch(None)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-legacy1", "summary": "s"}, {"name": "bob"})

    assert _FakeProc.last_argv == [
        orch.RUN_AGENT_SCRIPT, "bob", "h-legacy1", "kaidera-os"
    ]
    assert o._inflight["kaidera-os"] == 0
    assert "completed" in _feed_kinds(o)
