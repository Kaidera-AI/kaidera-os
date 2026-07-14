from __future__ import annotations

import pytest

from app import harness_runner as hr


def test_foreground_context_disables_short_turn_read_timeout():
    assert hr._turn_read_timeout("chat") is None
    assert hr._turn_read_timeout("approve-run") is None
    assert hr._turn_read_timeout("manual") is None


def test_background_context_keeps_finite_turn_read_timeout(monkeypatch):
    monkeypatch.setattr(hr, "TURN_TIMEOUT_S", 120.0)

    assert hr._turn_read_timeout("autonomous") == 120.0
    assert hr._turn_read_timeout("background") == 120.0
    assert hr._turn_read_timeout(None) == 120.0


class _FakeStderr:
    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = object()
        self.stderr = _FakeStderr()
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _CapturingReader:
    timeouts: list[float | None] = []

    def __init__(self, stdout: object) -> None:
        self.stdout = stdout

    def start(self) -> None:
        return None

    async def readline(self, timeout: float | None) -> bytes:
        self.timeouts.append(timeout)
        return b""

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_codex_foreground_uses_disabled_read_timeout(monkeypatch):
    _CapturingReader.timeouts = []

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(hr.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(hr, "_PipeLineReader", _CapturingReader)

    events = [ev async for ev in hr._stream_codex("ping", run_context="chat")]

    assert _CapturingReader.timeouts == [None]
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_codex_autonomous_keeps_finite_default_read_timeout(monkeypatch):
    _CapturingReader.timeouts = []
    monkeypatch.setattr(hr, "TURN_TIMEOUT_S", 120.0)

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(hr.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(hr, "_PipeLineReader", _CapturingReader)

    events = [ev async for ev in hr._stream_codex("ping", run_context="autonomous")]

    assert _CapturingReader.timeouts == [120.0]
    assert events[-1] == {"type": "done"}
