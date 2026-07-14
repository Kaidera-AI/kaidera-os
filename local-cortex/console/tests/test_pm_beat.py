"""Tests for the PM-beat (Increment 2) — Orchestrator._pm_beat.

Verifies the spawn-as-subprocess contract, debounce (one beat at a time per
project), rate-limiting (poll beats respect PM_BEAT_MIN_INTERVAL_S; completion
beats are not rate-limited), and the completion trigger from _dispatch_run.

Uses the same _FakeProc + monkeypatch pattern as test_orchestrator_spawn.py —
the real Kai harness is never invoked from tests (spawn-don't-host constraint).
"""

import asyncio
import os
import subprocess
import time

import pytest

import app.orchestrator as orch
from app.orchestrator import Orchestrator


def test_pm_beat_script_is_explicit_legacy_hook_by_default():
    """Product PM planning beats are scheduled handoffs now.

    The old script hook remains testable/explicit via ORCH_PM_BEAT_SCRIPT, but a
    clean product install must not auto-run a bundled beat script by default.
    """
    assert orch.PM_BEAT_SCRIPT == "" or os.path.exists(orch.PM_BEAT_SCRIPT)


# ---------------------------------------------------------------------------
# Fake subprocess — mirrors _FakeProc from test_orchestrator_spawn.py.
# ---------------------------------------------------------------------------

class _FakeProc:
    last_argv: list[str] | None = None
    next_rc: int = 0
    next_stderr: str = ""
    raise_timeout: bool = False

    def __init__(self, argv, **kwargs):
        _FakeProc.last_argv = list(argv)
        self.kwargs = kwargs
        self.returncode = None

    def communicate(self, timeout=None):
        if _FakeProc.raise_timeout:
            raise subprocess.TimeoutExpired(cmd=self.kwargs.get("args", "pm-beat"), timeout=timeout)
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
    _FakeProc.next_rc = rc
    _FakeProc.next_stderr = stderr
    _FakeProc.raise_timeout = timeout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orch():
    """Minimal Orchestrator for _pm_beat tests; collaborators are never reached."""
    return Orchestrator(
        cortex=object(),
        appdb=object(),
        harness_runner=object(),
        chat_routing_for=lambda agent, project: ("pi", "gpt-5.5", "high"),
        record_usage=None,
        find_agent=lambda agents, name: None,
        resolve_target=lambda handoff, agents: None,
        classify_interactive=lambda agent, desig: False,
        project_identity=lambda cortex, project: None,
        agent_view=lambda a: a,
    )


def _feed_kinds(o, project="kaidera-os"):
    return [e.get("kind") for e in o.feed.recent(project)]


# ---------------------------------------------------------------------------
# Task 4 tests: _pm_beat spawn, debounce, rate-limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pm_beat_spawns_script_and_releases_inflight(monkeypatch, tmp_path):
    """A fresh beat spawns PM_BEAT_SCRIPT with (script, project_key), records
    the 'info' feed lines, and clears _pm_beat_inflight when done."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    # Point PM_BEAT_SCRIPT at a file that "exists" (os.path.exists check)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    _reset_fake(rc=0)

    o = _make_orch()
    await o._pm_beat("kaidera-os", reason="completion")

    assert _FakeProc.last_argv == [str(fake_script), "kaidera-os"]
    assert "kaidera-os" not in o._pm_beat_inflight  # released after completion
    kinds = _feed_kinds(o)
    assert "info" in kinds


@pytest.mark.asyncio
async def test_pm_beat_debounce_while_inflight(monkeypatch, tmp_path):
    """A second _pm_beat call while one is already in-flight is a no-op.

    We simulate inflight by pre-populating _pm_beat_inflight; the method should
    return without touching subprocess.Popen."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    _reset_fake(rc=0)
    spawn_count = []
    real_popen = orch.subprocess.Popen

    class _CountingProc(_FakeProc):
        def __init__(self, argv, **kwargs):
            spawn_count.append(argv)
            super().__init__(argv, **kwargs)

    monkeypatch.setattr(orch.subprocess, "Popen", _CountingProc)

    o = _make_orch()
    # Mark a beat as already in-flight.
    o._pm_beat_inflight.add("kaidera-os")

    await o._pm_beat("kaidera-os", reason="completion")

    # No subprocess should have been spawned.
    assert len(spawn_count) == 0
    # inflight still has the project (we didn't remove the simulated one).
    assert "kaidera-os" in o._pm_beat_inflight


@pytest.mark.asyncio
async def test_pm_beat_poll_rate_limited(monkeypatch, tmp_path):
    """A poll beat within PM_BEAT_MIN_INTERVAL_S is silently skipped."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    _reset_fake(rc=0)
    spawn_count = []

    class _CountingProc(_FakeProc):
        def __init__(self, argv, **kwargs):
            spawn_count.append(argv)
            super().__init__(argv, **kwargs)

    monkeypatch.setattr(orch.subprocess, "Popen", _CountingProc)
    # Lower the interval to something we can easily be "within".
    monkeypatch.setattr(orch, "PM_BEAT_MIN_INTERVAL_S", 600.0)

    o = _make_orch()
    # Record a beat timestamp as if a beat just ran.
    o._pm_beat_last_ts["kaidera-os"] = time.monotonic()

    await o._pm_beat("kaidera-os", reason="poll")

    assert len(spawn_count) == 0  # no spawn — rate-limited


@pytest.mark.asyncio
async def test_pm_beat_completion_not_rate_limited(monkeypatch, tmp_path):
    """A completion beat bypasses the rate-limit even if a beat just ran."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    _reset_fake(rc=0)
    monkeypatch.setattr(orch, "PM_BEAT_MIN_INTERVAL_S", 600.0)

    o = _make_orch()
    # Simulate a beat that ran very recently.
    o._pm_beat_last_ts["kaidera-os"] = time.monotonic()

    await o._pm_beat("kaidera-os", reason="completion")

    # Should still have spawned — completion beats are never rate-limited.
    assert _FakeProc.last_argv is not None
    assert _FakeProc.last_argv == [str(fake_script), "kaidera-os"]


@pytest.mark.asyncio
async def test_pm_beat_poll_fires_after_interval(monkeypatch, tmp_path):
    """A poll beat fires when the last beat was > PM_BEAT_MIN_INTERVAL_S ago."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    _reset_fake(rc=0)
    monkeypatch.setattr(orch, "PM_BEAT_MIN_INTERVAL_S", 1.0)

    o = _make_orch()
    # Record a timestamp well in the past (2 seconds ago > 1s interval).
    o._pm_beat_last_ts["kaidera-os"] = time.monotonic() - 2.0

    await o._pm_beat("kaidera-os", reason="poll")

    assert _FakeProc.last_argv is not None
    assert _FakeProc.last_argv[1] == "kaidera-os"


@pytest.mark.asyncio
async def test_pm_beat_skips_when_script_missing(monkeypatch, tmp_path):
    """When PM_BEAT_SCRIPT does not exist, the beat is silently skipped (degrades
    gracefully — a stripped redistributable won't crash Dispatch)."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    missing = str(tmp_path / "nonexistent-pm-beat.sh")
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", missing)
    _reset_fake(rc=0)

    o = _make_orch()
    await o._pm_beat("kaidera-os", reason="completion")

    assert _FakeProc.last_argv is None  # nothing spawned


@pytest.mark.asyncio
async def test_pm_beat_nonzero_rc_emits_error_feed(monkeypatch, tmp_path):
    """A non-zero exit from run-pm-beat.sh emits an error feed line and releases
    the inflight slot."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    _reset_fake(rc=1, stderr="something went wrong")

    o = _make_orch()
    await o._pm_beat("kaidera-os", reason="completion")

    assert "kaidera-os" not in o._pm_beat_inflight
    assert "error" in _feed_kinds(o)


# ---------------------------------------------------------------------------
# Task 5 test: completion trigger from _dispatch_run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_run_triggers_pm_beat(monkeypatch, tmp_path):
    """After _dispatch_run completes (worker exits), it fires exactly one PM-beat
    task for the project. We check via the feed: the PM-beat 'info' entry should
    appear alongside the worker 'completed' entry.

    Because _pm_beat is called as a fire-and-forget task inside _dispatch_run, we
    must flush pending tasks after the dispatch_run call.
    """
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    # Worker exits 0; PM-beat exits 0.
    _reset_fake(rc=0)

    o = _make_orch()
    # Pre-populate inflight like _maybe_dispatch does before calling _dispatch_run.
    o._inflight["kaidera-os"] = 1
    handoff = {"id": "h-ab123456", "summary": "do work"}
    target = {"name": "bob", "display_name": "Bob"}

    await o._dispatch_run("kaidera-os", handoff, target)
    # Flush any pending tasks (the pm-beat task created inside _dispatch_run).
    await asyncio.sleep(0)
    # Give the beat task a tick to run its body.
    await asyncio.sleep(0)

    kinds = _feed_kinds(o)
    # Worker completed line.
    assert "completed" in kinds
    # PM-beat info line — spawned as a result of the completion trigger.
    assert "info" in kinds
    # Inflight slot released.
    assert o._inflight["kaidera-os"] == 0


# ---------------------------------------------------------------------------
# Fix-2 tests: cleanup re-trigger, timeout path, first-poll bootstrap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pm_beat_cleanup_retrigger_sequence(monkeypatch, tmp_path):
    """Relentless property: a beat can re-fire after the previous one finishes.

    Sequence:
    1. Run a beat → let it complete.
    2. Assert the project is NOT in _pm_beat_inflight (cleanup ran).
    3. Run a SECOND beat → assert it actually spawns again (not silently skipped).
    """
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    monkeypatch.setattr(orch, "PM_BEAT_MIN_INTERVAL_S", 0.0)  # no rate-limit
    _reset_fake(rc=0)

    o = _make_orch()

    # First beat.
    await o._pm_beat("kaidera-os", reason="completion")
    # Cleanup must have run — project must NOT be in _pm_beat_inflight.
    assert "kaidera-os" not in o._pm_beat_inflight, (
        "_pm_beat_inflight must be cleared after a beat completes"
    )
    first_argv = _FakeProc.last_argv
    assert first_argv is not None, "first beat must have spawned the subprocess"

    # Reset the fake so we can detect a new spawn.
    _FakeProc.last_argv = None

    # Second beat — must fire, not be debounced (inflight is clear).
    await o._pm_beat("kaidera-os", reason="completion")
    assert "kaidera-os" not in o._pm_beat_inflight, (
        "_pm_beat_inflight must be cleared after second beat too"
    )
    assert _FakeProc.last_argv is not None, (
        "second beat must spawn again — relentless property requires re-fire after cleanup"
    )


@pytest.mark.asyncio
async def test_pm_beat_timeout_releases_inflight_and_emits_error(monkeypatch, tmp_path):
    """On subprocess.TimeoutExpired the inflight slot is released and an error
    feed line is emitted (the finally block fires even through the early return)."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    _reset_fake(timeout=True)

    o = _make_orch()
    await o._pm_beat("kaidera-os", reason="completion")

    # The finally block must have run — inflight slot released.
    assert "kaidera-os" not in o._pm_beat_inflight, (
        "_pm_beat_inflight must be discarded on the timeout path"
    )
    # An error feed line must have been emitted for the timeout.
    kinds = _feed_kinds(o)
    assert "error" in kinds, (
        "timeout path must emit an error feed line"
    )


@pytest.mark.asyncio
async def test_pm_beat_first_poll_no_prior_timestamp_fires(monkeypatch, tmp_path):
    """With NO prior timestamp (project absent from _pm_beat_last_ts), a
    reason='poll' beat fires rather than being silently skipped."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    fake_script = tmp_path / "run-pm-beat.sh"
    fake_script.touch()
    monkeypatch.setattr(orch, "PM_BEAT_SCRIPT", str(fake_script))
    # Use a large interval — a fresh project (no prior timestamp) starts at 0.0
    # which means elapsed = now - 0.0 >> interval, so it must fire.
    monkeypatch.setattr(orch, "PM_BEAT_MIN_INTERVAL_S", 600.0)
    _reset_fake(rc=0)

    o = _make_orch()
    # Ensure no prior timestamp exists for this project.
    assert "kaidera-os" not in o._pm_beat_last_ts

    await o._pm_beat("kaidera-os", reason="poll")

    assert _FakeProc.last_argv is not None, (
        "first-ever poll beat must fire when no prior timestamp exists"
    )
    assert _FakeProc.last_argv[1] == "kaidera-os"
