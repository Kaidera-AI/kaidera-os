"""Harness-service → worker ENV + stderr diagnosability (run-state visibility fix).

THE BUG: a HOST worker spawned by the harness-service inherits the service's env. If
that env carries the CONTAINER app-DB DSN (``harness-appdb:5432``), the worker can't
reach the app-DB from the host, so every run-state write silently no-ops and the run
sticks at ``queued`` (no pid, no spans) — the console can't SHOW it. Separately, the
worker's stderr was ``DEVNULL`` (the PIPE-deadlock fix), so the connect FAILURE was
invisible.

THE FIX (asserted here):
  1. Every spawn (``/spawn`` · ``/chat`` · ``/explain``) passes an ``env`` to Popen that
     FORCES ``HARNESS_APPDB_DSN`` to the HOST DSN (``host_appdb_dsn()``), so a worker can
     never keep an inherited container DSN — the run-state writes LAND.
  2. Every spawn routes the worker's stderr to a per-run logfile under a sandboxed
     ``~/.harness-runs/<run_id>.stderr`` (an open file fd → the OS writes straight to the
     file; nothing to drain → no 64 KB PIPE deadlock), strictly better than DEVNULL:
     the next failure is diagnosable WITHOUT re-introducing the deadlock.
"""
from __future__ import annotations

import io

import httpx
import pytest

TOKEN = "svc-secret"  # fitness:allow-literal test fixture, not a real secret
SCRIPT = "/fake/run-agent"
CHAT_SCRIPT = "/fake/run-chat"
EXPLAIN_SCRIPT = "/fake/run-explain"


class _FakeProc:
    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class _FakePopenFactory:
    """Records the last spawn's argv + kwargs (incl. env + stderr)."""

    def __init__(self):
        self.last: _FakeProc | None = None
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        self.last = _FakeProc(argv, **kwargs)
        return self.last


def _make_app(runs_dir, *, token=TOKEN):
    from app.harness_service import create_app

    popen = _FakePopenFactory()
    app = create_app(
        token=token, popen=popen,
        run_agent_script=SCRIPT, run_chat_script=CHAT_SCRIPT,
        run_explain_script=EXPLAIN_SCRIPT,
    )
    return app, popen


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc.test")


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "harness-runs"
    monkeypatch.setenv("HARNESS_RUNS_DIR", str(d))
    return d


# ---------------------------------------------------------------------------
#  ENV: the worker is forced onto the HOST app-DB DSN.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_forces_host_appdb_dsn_even_when_container_dsn_inherited(
    runs_dir, monkeypatch
):
    """REGRESSION for the exact production failure: the service env carries the CONTAINER
    DSN, but the spawned worker's env carries the HOST DSN (so its run-state writes land)."""
    monkeypatch.setenv(
        "HARNESS_APPDB_DSN", "postgresql://harness:harness@harness-appdb:5432/harness_app"
    )
    app, popen = _make_app(runs_dir)
    async with _client(app) as c:
        resp = await c.post(
            "/spawn", headers=_auth(),
            json=dict(run_id="run-1", project="proj-x", agent="worker-a", handoff_id="h-1"),
        )
    assert resp.status_code == 202
    env = popen.last.kwargs.get("env")
    assert env is not None, "spawn must pass an explicit env to the worker"
    dsn = env.get("HARNESS_APPDB_DSN", "")
    assert "harness-appdb" not in dsn, "worker must NOT inherit the container DSN"
    assert "localhost:5500" in dsn, "worker must be forced onto the host app-DB DSN"


@pytest.mark.asyncio
async def test_spawn_env_preserves_other_vars(runs_dir, monkeypatch):
    """The forced env is a COPY of the process env with only HARNESS_APPDB_DSN overridden
    — other vars (PATH, tokens, etc.) still pass through to the worker."""
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keepme")
    app, popen = _make_app(runs_dir)
    async with _client(app) as c:
        await c.post(
            "/spawn", headers=_auth(),
            json=dict(run_id="run-2", project="proj-x", agent="w", handoff_id="h-2"),
        )
    env = popen.last.kwargs.get("env")
    assert env.get("SOME_UNRELATED_VAR") == "keepme"


@pytest.mark.asyncio
async def test_chat_forces_host_appdb_dsn(runs_dir, monkeypatch):
    monkeypatch.setenv(
        "HARNESS_APPDB_DSN", "postgresql://harness:harness@harness-appdb:5432/harness_app"
    )
    app, popen = _make_app(runs_dir)
    async with _client(app) as c:
        resp = await c.post(
            "/chat", headers=_auth(),
            json=dict(run_id="crun-1", project="proj-x", agent="kai", message="hi"),
        )
    assert resp.status_code == 202
    env = popen.last.kwargs.get("env")
    assert "harness-appdb" not in env.get("HARNESS_APPDB_DSN", "")
    assert "localhost:5500" in env.get("HARNESS_APPDB_DSN", "")


@pytest.mark.asyncio
async def test_explain_forces_host_appdb_dsn(runs_dir, monkeypatch):
    monkeypatch.setenv(
        "HARNESS_APPDB_DSN", "postgresql://harness:harness@harness-appdb:5432/harness_app"
    )
    app, popen = _make_app(runs_dir)
    async with _client(app) as c:
        resp = await c.post(
            "/explain", headers=_auth(),
            json=dict(run_id="erun-1", project="proj-x", agent="kai",
                      kind="file", repo="/abs/repo", path="m.py"),
        )
    assert resp.status_code == 202
    env = popen.last.kwargs.get("env")
    assert "harness-appdb" not in env.get("HARNESS_APPDB_DSN", "")
    assert "localhost:5500" in env.get("HARNESS_APPDB_DSN", "")


# ---------------------------------------------------------------------------
#  STDERR: routed to a per-run logfile (diagnosable, no PIPE deadlock, not DEVNULL).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_stderr_is_a_per_run_logfile_not_devnull(runs_dir):
    """The worker's stderr goes to an OPEN FILE (a writable fd) under the runs dir,
    keyed by run_id — NOT subprocess.DEVNULL. A real file fd is drained by the OS, so
    there's no 64 KB PIPE deadlock, and the worker's errors are now readable."""
    import subprocess

    app, popen = _make_app(runs_dir)
    async with _client(app) as c:
        await c.post(
            "/spawn", headers=_auth(),
            json=dict(run_id="run-log", project="proj-x", agent="w", handoff_id="h-3"),
        )
    stderr = popen.last.kwargs.get("stderr")
    assert stderr is not subprocess.DEVNULL, "stderr must be diagnosable, not DEVNULL"
    # It's a writable file object, and the file lives under the runs dir keyed by run_id.
    assert hasattr(stderr, "write")
    assert hasattr(stderr, "fileno")
    name = getattr(stderr, "name", "")
    assert "run-log" in str(name)
    assert str(runs_dir) in str(name)
    # The file actually exists on disk (was opened).
    assert (runs_dir / "run-log.stderr").exists()


@pytest.mark.asyncio
async def test_runs_dir_created_if_missing(runs_dir):
    """The runs dir is created on demand (the operator need not pre-make it)."""
    assert not runs_dir.exists()
    app, popen = _make_app(runs_dir)
    async with _client(app) as c:
        await c.post(
            "/spawn", headers=_auth(),
            json=dict(run_id="run-mk", project="p", agent="w", handoff_id="h"),
        )
    assert runs_dir.is_dir()


@pytest.mark.asyncio
async def test_chat_and_explain_also_log_stderr_to_file(runs_dir):
    import subprocess

    app, popen = _make_app(runs_dir)
    async with _client(app) as c:
        await c.post(
            "/chat", headers=_auth(),
            json=dict(run_id="crun-log", project="p", agent="kai", message="hi"),
        )
        chat_stderr = popen.last.kwargs.get("stderr")
        await c.post(
            "/explain", headers=_auth(),
            json=dict(run_id="erun-log", project="p", agent="kai",
                      kind="file", repo="/abs/repo", path="m.py"),
        )
        explain_stderr = popen.last.kwargs.get("stderr")
    for st, rid in ((chat_stderr, "crun-log"), (explain_stderr, "erun-log")):
        assert st is not subprocess.DEVNULL
        assert rid in str(getattr(st, "name", ""))
