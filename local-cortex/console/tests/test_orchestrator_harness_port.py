"""Harness-service Increment 1 — the orchestrator fork through HarnessPort.

This pins the ADDITIVE fork in ``Orchestrator._dispatch_run``: when a
``harness_port`` is injected, the dispatch builds a ``SpawnRequest`` and routes the
spawn through the port (``await harness_port.spawn_run(req)``) instead of the inline
``subprocess.Popen`` — and maps the returned ``SpawnHandle`` to the SAME
activity-feed outcomes the inline path produces (rc 0→completed / 2→skipped /
else→error), with the slot ALWAYS released. A handle that is accepted-but-not-
terminal (``exit_code is None``) is the async "dispatched" shape (the worker reports
its terminal state later via run-state), so the orchestrator does NOT mark it
completed here.

CRITICAL GUARANTEE (proven by ``test_no_harness_port_uses_legacy_inline_spawn``):
with ``harness_port=None`` (the default) the orchestrator takes the EXISTING inline
``subprocess.Popen`` path, byte-for-byte unchanged — ``test_orchestrator_spawn.py``
passes UNMODIFIED. The fork is purely additive.

The HarnessPort is FAKED here (no real spawn) so we assert orchestration behaviour,
not a live run.
"""

from __future__ import annotations

import subprocess

import pytest

import app.orchestrator as orch
from app.domain.harness import SpawnHandle, SpawnRequest
from app.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
#  A fake HarnessPort — records the SpawnRequest it received, returns a scripted
#  SpawnHandle. Satisfies the runtime_checkable HarnessPort structurally.
# ---------------------------------------------------------------------------


class FakeHarnessPort:
    def __init__(self, handle: SpawnHandle | None = None) -> None:
        self.handle = handle
        self.requests: list[SpawnRequest] = []
        self.cancelled: list[str] = []

    async def spawn_run(self, request: SpawnRequest) -> SpawnHandle:
        self.requests.append(request)
        # Default to an accepted+completed-rc0 handle when none scripted.
        return self.handle or SpawnHandle(
            run_id=request.run_id, accepted=True, exit_code=0
        )

    async def cancel_run(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return False


# ---------------------------------------------------------------------------
#  The legacy-path fake proc (mirrors test_orchestrator_spawn._FakeProc) — used
#  ONLY by the harness_port=None test, to prove the inline path is untouched.
# ---------------------------------------------------------------------------


class _FakeProc:
    last_argv: list[str] | None = None
    next_rc: int = 0
    next_stderr: str = ""

    def __init__(self, argv, **kwargs):
        _FakeProc.last_argv = list(argv)
        self.kwargs = kwargs
        self.returncode = None

    def communicate(self, timeout=None):
        self.returncode = _FakeProc.next_rc
        return (None, _FakeProc.next_stderr)

    def wait(self):
        if self.returncode is None:
            self.returncode = _FakeProc.next_rc
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _make_orch(*, harness_port=None, runstate=None):
    """An Orchestrator with inert stubs — _dispatch_run only touches
    chat_routing_for (best-effort, suppressed), plus the self-built feed /
    inflight. The other collaborators are never reached on this path. Mirrors
    test_orchestrator_spawn._make_orch, plus the new harness_port/runstate."""
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
        harness_port=harness_port,
    )
    o._pm_beat = _noop_pm_beat  # type: ignore[method-assign]
    return o


def _feed_kinds(o, project="kaidera-os"):
    return [e.get("kind") for e in o.feed.recent(project)]


# ---------------------------------------------------------------------------
#  The fork: harness_port set → spawn routes through the port.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_calls_spawn_run_with_correct_request():
    port = FakeHarnessPort()
    o = _make_orch(harness_port=port)
    o._inflight["kaidera-os"] = 1
    handoff = {"id": "h-abc12345", "summary": "do the thing"}
    target = {"name": "worker-a", "display_name": "Worker A"}

    await o._dispatch_run("kaidera-os", handoff, target)

    # The port was driven exactly once with a SpawnRequest carrying the run scope.
    assert len(port.requests) == 1
    req = port.requests[0]
    assert isinstance(req, SpawnRequest)
    assert req.project == "kaidera-os"
    assert req.agent == "worker-a"
    assert req.handoff_id == "h-abc12345"
    assert req.run_id  # a non-empty run_id was minted
    # slot released even on the port path.
    assert o._inflight["kaidera-os"] == 0


@pytest.mark.asyncio
async def test_port_rc0_is_completed_and_releases_slot():
    port = FakeHarnessPort(SpawnHandle(run_id="r", accepted=True, exit_code=0))
    o = _make_orch(harness_port=port)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-ok1", "summary": "s"}, {"name": "worker-a"})

    assert o._inflight["kaidera-os"] == 0
    assert "completed" in _feed_kinds(o)


@pytest.mark.asyncio
async def test_port_rc2_is_skipped_not_error():
    port = FakeHarnessPort(SpawnHandle(run_id="r", accepted=True, exit_code=2))
    class FakeRunState:
        def __init__(self):
            self.statuses = []

        async def start_run(self, **kw):
            class _Rec:
                run_id = "r"

            return _Rec()

        async def set_status(self, run_id, status, **kw):
            self.statuses.append((run_id, status, kw))

    rs = FakeRunState()
    o = _make_orch(harness_port=port, runstate=rs)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-skip1", "summary": "s"}, {"name": "worker-a"})

    assert o._inflight["kaidera-os"] == 0
    kinds = _feed_kinds(o)
    assert "skipped" in kinds
    assert "error" not in kinds
    assert rs.statuses == [
        (
            "r",
            "ok",
            {
                "metadata": {
                    "dispatch_outcome": "skipped",
                    "reason": "worker could not claim handoff",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_port_nonzero_rc_is_error_and_releases_slot():
    port = FakeHarnessPort(
        SpawnHandle(run_id="r", accepted=True, exit_code=1, stderr_tail="boom")
    )
    o = _make_orch(harness_port=port)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-fail1", "summary": "s"}, {"name": "worker-a"})

    assert o._inflight["kaidera-os"] == 0
    assert "error" in _feed_kinds(o)


@pytest.mark.asyncio
async def test_port_rejected_is_error_and_releases_slot():
    """accepted=False (the spawn never happened — e.g. the harness-service is down /
    the script is missing) → an error feed line + the slot released."""
    port = FakeHarnessPort(
        SpawnHandle(run_id="r", accepted=False, error="harness unavailable")
    )
    o = _make_orch(harness_port=port)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-rej1", "summary": "s"}, {"name": "worker-a"})

    assert o._inflight["kaidera-os"] == 0
    assert "error" in _feed_kinds(o)


@pytest.mark.asyncio
async def test_port_accepted_without_exit_code_is_dispatched_async():
    """accepted=True + exit_code is None → the async 'dispatched' shape: the worker
    reports its terminal state later via run-state, so the orchestrator records a
    'dispatched' feed outcome (NOT completed) and still releases the slot."""
    port = FakeHarnessPort(SpawnHandle(run_id="r", accepted=True, exit_code=None))
    o = _make_orch(harness_port=port)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-disp1", "summary": "s"}, {"name": "worker-a"})

    assert o._inflight["kaidera-os"] == 0
    kinds = _feed_kinds(o)
    assert "dispatched" in kinds
    # It is NOT prematurely marked completed (the terminal state comes async).
    assert "completed" not in kinds


@pytest.mark.asyncio
async def test_request_carries_runstate_run_id_when_store_present():
    """When a run-state store mints a run_id (the existing T5 behaviour), the SAME
    run_id flows into the SpawnRequest, so the worker writes the same run_state row."""

    class FakeRunState:
        def __init__(self):
            self.started = []

        async def start_run(self, **kw):
            self.started.append(kw)

            class _Rec:
                run_id = "fixed-run-id-123"

            return _Rec()

    rs = FakeRunState()
    port = FakeHarnessPort()
    o = _make_orch(harness_port=port, runstate=rs)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-rid1", "summary": "s"}, {"name": "worker-a"})

    assert len(port.requests) == 1
    assert port.requests[0].run_id == "fixed-run-id-123"
    # The store was still pre-created (T5 preserved).
    assert rs.started and rs.started[0]["agent"] == "worker-a"


# ---------------------------------------------------------------------------
#  THE GUARANTEE: harness_port=None → the legacy inline subprocess.Popen path,
#  byte-for-byte unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_harness_port_uses_legacy_inline_spawn(monkeypatch):
    """With harness_port=None (the default), _dispatch_run takes the EXISTING inline
    subprocess.Popen path — the spawn the unmodified test_orchestrator_spawn.py
    pins. We prove it by monkeypatching orch.subprocess.Popen and asserting the
    inline argv + outcome (the port is never consulted because it is None)."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _FakeProc.last_argv = None
    _FakeProc.next_rc = 0
    _FakeProc.next_stderr = ""

    o = _make_orch(harness_port=None)
    o._inflight["kaidera-os"] = 1
    handoff = {"id": "h-legacy1", "summary": "do the thing"}
    target = {"name": "worker-a", "display_name": "Worker A"}

    await o._dispatch_run("kaidera-os", handoff, target)

    # The inline Popen was used with the legacy 4-arg argv (no run_id, store=None).
    assert _FakeProc.last_argv == [orch.RUN_AGENT_SCRIPT, "worker-a", "h-legacy1", "kaidera-os"]
    assert o._inflight["kaidera-os"] == 0
    assert "completed" in _feed_kinds(o)


def test_make_harness_port_factory():
    """The module-level _make_harness_port() reads HARNESS_SPAWN_MODE:
      * unset / 'legacy' / unknown → None (the default; legacy inline path)
      * 'local'  → a LocalHarnessAdapter (satisfies HarnessPort)
      * 'remote' → a RemoteHarnessAdapter (I2 — now BUILT; satisfies HarnessPort).
    """
    import os

    from app.domain.harness import HarnessPort

    def _with_mode(value):
        prev = os.environ.get("HARNESS_SPAWN_MODE")
        if value is None:
            os.environ.pop("HARNESS_SPAWN_MODE", None)
        else:
            os.environ["HARNESS_SPAWN_MODE"] = value
        try:
            return orch._make_harness_port()
        finally:
            if prev is None:
                os.environ.pop("HARNESS_SPAWN_MODE", None)
            else:
                os.environ["HARNESS_SPAWN_MODE"] = prev

    # Default / unset → legacy (None).
    assert _with_mode(None) is None
    assert _with_mode("legacy") is None
    assert _with_mode("bananas") is None
    # local → a real HarnessPort.
    local = _with_mode("local")
    assert local is not None
    assert isinstance(local, HarnessPort)
    # remote → a real HarnessPort (I2 RemoteHarnessAdapter is now built). Construct
    # only (no socket opened until the first spawn_run/cancel_run); close its client.
    remote = _with_mode("remote")
    assert remote is not None
    assert isinstance(remote, HarnessPort)
    import asyncio

    aclose = getattr(remote, "aclose", None)
    if aclose is not None:
        asyncio.run(aclose())  # close the adapter's httpx client (no socket was opened)
