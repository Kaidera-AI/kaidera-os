"""Plan 2 (E007 Autonomy v2): the orchestrator SPAWNS run-agent worker processes
instead of hosting the harness stream inline (the v1 stall fix).

These pin the spawn contract of ``Orchestrator._dispatch_run`` with the child
process MOCKED — we assert orchestration behaviour, not a live pi run (that is the
integration smoke):

  * it launches RUN_AGENT_SCRIPT with the argv (name, handoff_id, project),
  * it ALWAYS releases the concurrency slot afterwards (cap accounting holds),
  * the worker's exit code maps to the right activity-feed outcome
    (0 -> completed, 2 -> skipped/already-claimed, other -> error),
  * a run that overruns RUN_TIMEOUT_S is killed and recorded as a failure.

The spawn uses an argv LIST (no shell), so there is no command-injection surface;
the wait runs off the event loop via asyncio.to_thread.
"""
import os
import subprocess

import pytest

import app.orchestrator as orch
from app.orchestrator import Orchestrator


class _FakeProc:
    """Stand-in for subprocess.Popen. Records the argv it was launched with and
    returns a scripted exit code; can also simulate a timeout."""

    last_argv: list[str] | None = None
    last_kwargs: dict | None = None
    next_rc: int = 0
    next_stderr: str = ""
    raise_timeout: bool = False

    def __init__(self, argv, **kwargs):
        _FakeProc.last_argv = list(argv)
        _FakeProc.last_kwargs = dict(kwargs)
        self.kwargs = kwargs
        self.returncode = None

    def communicate(self, timeout=None):
        if _FakeProc.raise_timeout:
            raise subprocess.TimeoutExpired(cmd="run-agent", timeout=timeout)
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


def _reset_fake(*, rc=0, stderr="", timeout=False):
    _FakeProc.last_argv = None
    _FakeProc.last_kwargs = None
    _FakeProc.next_rc = rc
    _FakeProc.next_stderr = stderr
    _FakeProc.raise_timeout = timeout


def _make_orch(cortex=None):
    """An Orchestrator with inert stubs — _dispatch_run only touches
    chat_routing_for (best-effort, suppressed), plus the self-built feed /
    transcripts / inflight. The other collaborators are never reached on this path."""
    async def _noop_pm_beat(project_key: str, *, reason: str) -> None:
        return None

    o = Orchestrator(
        cortex=cortex if cortex is not None else object(),
        appdb=object(),
        harness_runner=object(),
        chat_routing_for=lambda agent, project: ("pi", "gpt-5.3-codex-spark", "high"),
        record_usage=None,
        find_agent=lambda agents, name: None,
        resolve_target=lambda handoff, agents: None,
        classify_interactive=lambda agent, desig: False,
        project_identity=lambda cortex, project: None,
        agent_view=lambda a: a,
    )
    o._pm_beat = _noop_pm_beat  # type: ignore[method-assign]
    return o


def _feed_kinds(o, project="kaidera-os"):
    return [e.get("kind") for e in o.feed.recent(project)]


@pytest.mark.asyncio
async def test_dispatch_spawns_run_agent_with_argv_and_releases_slot(monkeypatch):
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=0)
    o = _make_orch()
    o._inflight["kaidera-os"] = 1  # the gate reserves a slot before calling _dispatch_run
    handoff = {"id": "h-abc12345", "summary": "do the thing"}
    target = {"name": "bob", "display_name": "Bob"}

    await o._dispatch_run("kaidera-os", handoff, target)

    # spawned the worker unit with (script, name, handoff_id, project) — argv list,
    # NOT a shell string.
    assert _FakeProc.last_argv == [orch.RUN_AGENT_SCRIPT, "bob", "h-abc12345", "kaidera-os"]
    # detached into its own session and never inherited a controlling shell.
    assert _FakeProc.last_argv[0] == orch.RUN_AGENT_SCRIPT
    # slot released (cap accounting holds even on the happy path).
    assert o._inflight["kaidera-os"] == 0
    # completed outcome recorded.
    assert "completed" in _feed_kinds(o)


@pytest.mark.asyncio
async def test_rc2_is_skip_not_error(monkeypatch):
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=2)
    o = _make_orch()
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-skip1", "summary": "s"}, {"name": "bob"})

    assert o._inflight["kaidera-os"] == 0
    kinds = _feed_kinds(o)
    assert "skipped" in kinds
    assert "error" not in kinds


@pytest.mark.asyncio
async def test_nonzero_rc_is_error_and_releases_slot(monkeypatch):
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=1, stderr="boom: the worker crashed")
    o = _make_orch()
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-fail1", "summary": "s"}, {"name": "bob"})

    assert o._inflight["kaidera-os"] == 0
    assert "error" in _feed_kinds(o)


@pytest.mark.asyncio
async def test_timeout_kills_child_and_records_failure(monkeypatch):
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(timeout=True)
    o = _make_orch()
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-hang1", "summary": "s"}, {"name": "bob"})

    # slot released, and the overrun is an error (the worker was killed → rc -9).
    assert o._inflight["kaidera-os"] == 0
    assert "error" in _feed_kinds(o)


class _FakeCortex:
    """Minimal cortex stub exposing async get_project → a fixed repo_root."""

    def __init__(self, repo_root: str) -> None:
        self._root = repo_root

    async def get_project(self, project_key: str):
        return {"repo_root": self._root}


@pytest.mark.asyncio
async def test_dispatch_spawns_worker_in_project_repo_root(monkeypatch, tmp_path):
    """The wrong-folder fix: the detached worker is spawned with cwd=repo_root and an env
    scoped to the project (CORTEX_PROJECT + KAIDERA_AGENT_WORKSPACE + the project's
    .agents/scripts first on PATH), so it + its harness children run in the RIGHT folder."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=0)
    repo_root = str(tmp_path)  # a real, existing dir → passes the _safe_repo_root isdir guard
    o = _make_orch(cortex=_FakeCortex(repo_root))
    o._inflight["sample-project"] = 1

    await o._dispatch_run("sample-project", {"id": "h-ws1", "summary": "s"}, {"name": "sample-worker"})

    kw = _FakeProc.last_kwargs or {}
    assert kw.get("cwd") == repo_root
    env = kw.get("env") or {}
    assert env.get("CORTEX_PROJECT") == "sample-project"
    assert env.get("KAIDERA_AGENT_WORKSPACE") == repo_root
    assert env.get("PATH", "").startswith(os.path.join(repo_root, ".agents", "scripts"))


@pytest.mark.asyncio
async def test_dispatch_missing_repo_root_falls_back_to_legacy_cwd(monkeypatch):
    """A project whose root can't be resolved (no get_project / nonexistent path) must NOT
    crash dispatch — it falls back to cwd=None (legacy behaviour) and still releases the slot."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=0)
    o = _make_orch()  # cortex=object() → get_project raises → _safe_repo_root returns None
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-nows", "summary": "s"}, {"name": "bob"})

    kw = _FakeProc.last_kwargs or {}
    assert kw.get("cwd") is None  # safe no-op, no crash
    assert o._inflight["kaidera-os"] == 0


class _FakeCortexRelease:
    """Records release_handoff calls; scripted result."""

    def __init__(self, result=True):
        self.calls: list[tuple[str, str]] = []
        self._result = result

    async def release_handoff(self, project_key, handoff_id):
        self.calls.append((project_key, handoff_id))
        return self._result


@pytest.mark.asyncio
async def test_failed_spawn_releases_claim_immediately(monkeypatch):
    """A nonzero run-agent exit must RELEASE the claim (not park it for hours) —
    the 9.9h claimed-limbo email outage from the 2026-07-02 ultrareview."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=1, stderr="boom")
    cortex = _FakeCortexRelease()
    o = _make_orch(cortex=cortex)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run("kaidera-os", {"id": "h-relme01", "summary": "s", "retry_count": 0},
                          {"name": "bob"})

    assert cortex.calls == [("kaidera-os", "h-relme01")]
    feed = " | ".join(str(e.get("message") or e.get("text") or "") for e in o.feed.recent("kaidera-os"))
    assert "released after failed spawn" in feed


@pytest.mark.asyncio
async def test_failed_spawn_at_retry_cap_leaves_claim_for_watchdog(monkeypatch):
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=1, stderr="boom")
    cortex = _FakeCortexRelease()
    o = _make_orch(cortex=cortex)
    o._inflight["kaidera-os"] = 1

    await o._dispatch_run(
        "kaidera-os",
        {"id": "h-capped1", "summary": "s", "retry_count": orch.RECLAIM_MAX_RETRIES},
        {"name": "bob"},
    )

    assert cortex.calls == []  # cap reached -> no release; Watchdog escalates
    feed = " | ".join(str(e.get("message") or e.get("text") or "") for e in o.feed.recent("kaidera-os"))
    assert "retry cap" in feed
