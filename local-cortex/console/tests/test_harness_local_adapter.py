"""Harness-service Increment 1 — tests for the LocalHarnessAdapter (the port seam).

`app/adapters/harness_local.py` implements the pure `HarnessPort`
(`app/domain/harness.py`) over the EXISTING host-side `subprocess.Popen` spawn —
the SAME spawn the orchestrator's `_dispatch_run` does today (argv
`[script, agent, handoff_id, project, run_id]`, `stdout=DEVNULL`, `stderr=PIPE`,
`text=True`, `start_new_session=True`, then `proc.communicate(timeout=…)`,
rc→outcome). It is a behaviour-preserving extraction, so a later increment can
swap in a RemoteHarnessAdapter (POST to the host harness-service) with no change to
the orchestrator.

These tests inject a FAKE `popen` (so nothing real is spawned) and assert:
  * the adapter SATISFIES `HarnessPort` (structural isinstance),
  * the argv is built in the exact order + the spawn kwargs match the host spawn
    (`stdout=DEVNULL`, `stderr=PIPE`, `text=True`, `start_new_session=True`),
  * rc=0 → `SpawnHandle(accepted=True, exit_code=0, stderr_tail=…)`,
  * `OSError` on spawn → `SpawnHandle(accepted=False, error=…)` (NEVER raises),
  * `TimeoutExpired` → the worker process group is killed +
    `SpawnHandle(accepted=True, exit_code=-1)`,
  * `cancel_run` → False (best-effort no-op for I1), and
  * the script + run_timeout default to the orchestrator's constants.
"""

from __future__ import annotations

import os
import subprocess

import pytest


class _FakePopen:
    """Stand-in for subprocess.Popen. Records the argv + kwargs it was launched with
    and returns a scripted exit code; can simulate an OSError on construct, or a
    TimeoutExpired / non-zero rc on communicate. Mirrors the _FakeProc in
    test_orchestrator_spawn.py so the two assert the SAME spawn contract."""

    last_argv: list[str] | None = None
    last_kwargs: dict | None = None
    next_rc: int = 0
    next_stderr: str = ""
    raise_timeout: bool = False
    raise_oserror: bool = False
    killed: bool = False
    waited: bool = False

    def __init__(self, argv, **kwargs):
        if _FakePopen.raise_oserror:
            raise OSError("No such file or directory: 'run-agent'")
        _FakePopen.last_argv = list(argv)
        _FakePopen.last_kwargs = dict(kwargs)
        self.returncode = None

    def communicate(self, timeout=None):
        if _FakePopen.raise_timeout:
            raise subprocess.TimeoutExpired(cmd="run-agent", timeout=timeout)
        self.returncode = _FakePopen.next_rc
        return (None, _FakePopen.next_stderr)

    def wait(self):
        _FakePopen.waited = True
        if self.returncode is None:
            self.returncode = _FakePopen.next_rc
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        _FakePopen.killed = True
        self.returncode = -9


def _reset(*, rc=0, stderr="", timeout=False, oserror=False):
    _FakePopen.last_argv = None
    _FakePopen.last_kwargs = None
    _FakePopen.next_rc = rc
    _FakePopen.next_stderr = stderr
    _FakePopen.raise_timeout = timeout
    _FakePopen.raise_oserror = oserror
    _FakePopen.killed = False
    _FakePopen.waited = False


def _make_adapter(**overrides):
    from app.adapters.harness_local import LocalHarnessAdapter

    kwargs = dict(run_agent_script="/x/run-agent", popen=_FakePopen)
    kwargs.update(overrides)
    return LocalHarnessAdapter(**kwargs)


def _req(**overrides):
    from app.domain.harness import SpawnRequest

    base = dict(
        run_id="run-1",
        project="kaidera-os",
        agent="worker-a",
        handoff_id="h-123",
    )
    base.update(overrides)
    return SpawnRequest(**base)


def test_adapter_satisfies_harness_port():
    from app.domain.harness import HarnessPort

    adapter = _make_adapter()
    assert isinstance(adapter, HarnessPort), "LocalHarnessAdapter must satisfy HarnessPort"


@pytest.mark.asyncio
async def test_spawn_rc0_is_accepted_with_exit_code_and_argv_and_kwargs():
    _reset(rc=0, stderr="all good")
    adapter = _make_adapter()

    handle = await adapter.spawn_run(_req())

    # argv: the EXACT host-spawn order [script, agent, handoff_id, project, run_id].
    assert _FakePopen.last_argv == ["/x/run-agent", "worker-a", "h-123", "kaidera-os", "run-1"]
    # spawn kwargs match the host spawn byte-for-byte.
    kw = _FakePopen.last_kwargs or {}
    assert kw.get("stdout") == subprocess.DEVNULL
    assert kw.get("stderr") == subprocess.PIPE
    assert kw.get("text") is True
    assert kw.get("start_new_session") is True
    # handle: accepted + the worker's exit code + the stderr tail.
    assert handle.run_id == "run-1"
    assert handle.accepted is True
    assert handle.exit_code == 0
    assert handle.stderr_tail == "all good"
    assert handle.error is None


@pytest.mark.asyncio
async def test_spawn_scopes_worker_to_project_repo_root(tmp_path):
    """repo_root is the project-isolation guard: worker cwd/env must follow the
    selected project, not the console process cwd."""
    _reset(rc=0)
    adapter = _make_adapter()
    repo_root = str(tmp_path)

    await adapter.spawn_run(_req(project="dxb", repo_root=repo_root))

    kw = _FakePopen.last_kwargs or {}
    assert kw.get("cwd") == repo_root
    env = kw.get("env") or {}
    assert env.get("CORTEX_PROJECT") == "dxb"
    assert env.get("KAIDERA_AGENT_WORKSPACE") == repo_root
    assert env.get("PATH", "").startswith(os.path.join(repo_root, ".agents", "scripts"))


@pytest.mark.asyncio
async def test_spawn_nonzero_rc_is_accepted_with_that_exit_code():
    _reset(rc=2, stderr="could not claim")
    adapter = _make_adapter()

    handle = await adapter.spawn_run(_req())

    # The adapter is rc-agnostic: it reports accepted=True + whatever rc the worker
    # returned (the orchestrator maps 0/2/other to outcomes — not the adapter).
    assert handle.accepted is True
    assert handle.exit_code == 2
    assert handle.stderr_tail == "could not claim"


@pytest.mark.asyncio
async def test_spawn_oserror_is_rejected_not_raised():
    _reset(oserror=True)
    adapter = _make_adapter()

    handle = await adapter.spawn_run(_req())

    # An OSError on spawn (e.g. the script is missing / not executable) is NEVER
    # raised — it is reported as accepted=False + the error string (fire-and-forget).
    assert handle.accepted is False
    assert handle.exit_code is None
    assert handle.error and "No such file" in handle.error


@pytest.mark.asyncio
async def test_spawn_timeout_kills_proc_and_reports_exit_code_minus_one():
    _reset(timeout=True)
    adapter = _make_adapter()

    handle = await adapter.spawn_run(_req())

    # On timeout the adapter kills the child and reports accepted=True (it DID
    # spawn) with exit_code=-1 (the overrun marker the orchestrator maps to error).
    assert _FakePopen.killed is True
    assert handle.accepted is True
    assert handle.exit_code == -1
    assert handle.error and "timed out" in handle.error.lower()


@pytest.mark.asyncio
async def test_spawn_timeout_kills_entire_worker_process_group(monkeypatch):
    """The detached worker is a session leader; timeout must stop its tool children."""
    killed: list[tuple[int, int]] = []

    class GroupPopen(_FakePopen):
        def __init__(self, argv, **kwargs):
            super().__init__(argv, **kwargs)
            self.pid = 4242

    def killpg(pid, sig):
        killed.append((pid, sig))

    import app.adapters.harness_local as harness_local

    _reset(timeout=True)
    monkeypatch.setattr(harness_local.os, "killpg", killpg)
    adapter = _make_adapter(popen=GroupPopen)

    handle = await adapter.spawn_run(_req())

    assert killed == [(4242, harness_local.signal.SIGKILL)]
    assert _FakePopen.killed is False
    assert handle.exit_code == -1


@pytest.mark.asyncio
async def test_spawn_passes_run_timeout_through_to_communicate():
    """The per-request run_timeout_s (else the adapter default) is what
    communicate() is given — proven by a tiny recording popen."""
    seen: dict = {}

    class RecordingPopen(_FakePopen):
        def communicate(self, timeout=None):
            seen["timeout"] = timeout
            self.returncode = 0
            return (None, "")

    _reset(rc=0)
    adapter = _make_adapter(popen=RecordingPopen, run_timeout_s=900.0)
    await adapter.spawn_run(_req(run_timeout_s=42.0))
    assert seen["timeout"] == 42.0


@pytest.mark.asyncio
async def test_stderr_tail_is_truncated_to_last_chars():
    """A huge stderr is truncated to the tail (the orchestrator keeps ~300 chars)."""
    big = "x" * 5000
    _reset(rc=1, stderr=big)
    adapter = _make_adapter()

    handle = await adapter.spawn_run(_req())

    assert handle.stderr_tail is not None
    assert len(handle.stderr_tail) <= 300
    # It is the TAIL (the end of the stream, where the error message lands).
    assert handle.stderr_tail == big[-len(handle.stderr_tail):]


@pytest.mark.asyncio
async def test_cancel_run_is_noop_false():
    adapter = _make_adapter()
    assert await adapter.cancel_run("run-1") is False


@pytest.mark.asyncio
async def test_spawn_chat_is_noop_not_accepted():
    """The LOCAL adapter does NOT spawn a chat runner: in local/legacy mode the chat
    route runs `stream_chat` IN-PROCESS (the CLIs are present on the host where the
    console runs), so there is no host seam to cross. spawn_chat is a structural
    no-op that returns accepted=False (NEVER raises), so the chat route's remote
    branch is never taken under the local adapter — it keeps the in-process path."""
    from app.domain.harness import ChatSpawnRequest

    spawned: list = []

    def _recording_popen(argv, **kwargs):
        spawned.append(list(argv))
        return _FakePopen(argv, **kwargs)

    _reset()  # clears _FakePopen class state
    adapter = _make_adapter(popen=_recording_popen)
    handle = await adapter.spawn_chat(
        ChatSpawnRequest(run_id="crun-1", project="kaidera-os", agent="kai", message="hi")
    )
    assert handle.accepted is False
    assert handle.run_id == "crun-1"
    # No process was spawned for chat by the local adapter (it is a structural no-op).
    assert spawned == []


def test_defaults_to_orchestrator_constants():
    """With no script/timeout injected, the adapter binds the orchestrator's
    RUN_AGENT_SCRIPT + RUN_TIMEOUT_S defaults (so production wiring needs no args)."""
    import app.orchestrator as orch
    from app.adapters.harness_local import LocalHarnessAdapter

    adapter = LocalHarnessAdapter()
    assert adapter._run_agent_script == orch.RUN_AGENT_SCRIPT
    assert adapter._run_timeout_s == orch.RUN_TIMEOUT_S
    # The real subprocess.Popen is bound by default (no fake injected).
    assert adapter._popen is subprocess.Popen
