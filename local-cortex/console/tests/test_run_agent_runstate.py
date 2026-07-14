"""T6 — the detached worker writes run-state + heartbeat to the store.

Milestone 1 (RunState SSOT). The worker (`run_agent.run_one`) is a DETACHED
subprocess that talks to the app-DB DIRECTLY (it must NOT depend on the console).
It writes the run's live state to the `RunStatePort` (the in-memory `feed_log`
display feed was REMOVED at T12 — the store IS the live surface now):

  * at start                       → set_status(run_id, "running")
  * per thinking/tool/output span  → append_output(run_id, kind=..., text=...)
  * a ~10s background heartbeat     → heartbeat(run_id, pid=os.getpid())  ★ the new signal
  * on success                     → set_status(run_id, "ok", tokens…/cost…)
  * on failure                     → set_status(run_id, "error", error=…)

CRITICAL INVARIANTS (both load-bearing):
  * Cortex AUDIT UNCHANGED — every existing cortex.log() call must STILL fire (the
    durable audit trail STARTED/STEP/TRANSCRIPT/COMPLETED is untouched).
  * GRACEFUL-DEGRADE — a store that RAISES (or is None / no run_id) must never break
    the run; run_one still completes and the handoff is still completed.

These run against a FAKE `RunStatePort` (fast; no DB), matching the FakeCortex /
FakeRunner style in tests/conftest.py.
"""
from __future__ import annotations

import asyncio

import pytest

import app.run_agent as ra
from tests.conftest import FakeCortex, FakeRunner


class FakeRunState:
    """Records the RunStatePort calls run_one makes (structural RunStatePort).

    Captures status transitions, appended spans, and heartbeats so a test can
    assert the running → spans → heartbeat → terminal lifecycle. `start_run` is a
    convenience (the worker only uses set_status/append_output/heartbeat — the
    orchestrator pre-creates the row in T5), but it is present so the same fake can
    stand in anywhere the port is expected."""

    def __init__(self) -> None:
        self.statuses: list[dict] = []      # [{run_id, status, error, tokens_in, ...}]
        self.spans: list[dict] = []         # [{run_id, kind, text}]
        self.heartbeats: list[dict] = []    # [{run_id, pid, tokens_in, ...}]
        self.started: list[dict] = []

    async def start_run(self, **kw):
        self.started.append(kw)
        return None

    async def set_status(self, run_id, status, *, error=None, metadata=None):
        # SIGNATURE MUST MIRROR the real adapter (RunStatePgStore.set_status): it takes
        # ONLY error + metadata — NOT tokens/cost (those land on the run header via
        # heartbeat). A looser fake here is what let the production TypeError —
        # set_status('ok', tokens_in=…) → run stuck 'running' — pass the suite.
        self.statuses.append({
            "run_id": run_id, "status": status, "error": error, "metadata": metadata,
        })

    async def append_output(self, run_id, *, seq=None, kind, text):
        self.spans.append({"run_id": run_id, "seq": seq, "kind": kind, "text": text})

    async def heartbeat(self, run_id, *, tokens_in=None, tokens_out=None,
                        cost_est_usd=None, pid=None):
        self.heartbeats.append({
            "run_id": run_id, "pid": pid, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "cost_est_usd": cost_est_usd,
        })


class RaisingRunState:
    """A RunStatePort whose every method RAISES — proves graceful-degrade (the
    worker must swallow store failures; the run + Cortex audit proceed)."""

    async def start_run(self, **kw):
        raise RuntimeError("store down")

    async def set_status(self, *a, **k):
        raise RuntimeError("store down")

    async def append_output(self, *a, **k):
        raise RuntimeError("store down")

    async def heartbeat(self, *a, **k):
        raise RuntimeError("store down")


def _events_ok():
    return [
        {"type": "thinking", "text": "Let me think."},
        {"type": "tool", "name": "Bash", "text": "Bash(ls -la)"},
        {"type": "delta", "text": "Done."},
        {"type": "result", "text": "", "tokens_in": 11, "tokens_out": 5, "cost_usd": 0.002},
        {"type": "done"},
    ]


# ---------------------------------------------------------------------------
#  Happy path — running → spans → terminal ok, with the existing audit intact.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_writes_runstate_lifecycle(monkeypatch):
    runstate = FakeRunState()
    cortex = FakeCortex()
    runner = FakeRunner(_events_ok())
    routing = lambda agent, project: ("pi", "gpt-5.3", "high")

    res = await ra.run_one(
        "bob", "h-rs01", "kaidera-os",
        cortex=cortex, runner=runner, routing=routing,
        task_summary="do the thing",
        runstate=runstate, run_id="run-abc",
    )
    assert res.status == "completed"

    # status walks running → ok, all on the pre-created run_id.
    statuses = [(s["run_id"], s["status"]) for s in runstate.statuses]
    assert statuses[0] == ("run-abc", "running")
    assert statuses[-1][1] == "ok"
    assert all(rid == "run-abc" for rid, _ in statuses)

    # terminal telemetry totals land on the run HEADER via heartbeat (the adapter's
    # tested contract — set_status carries NO tokens), BEFORE the status flips to ok.
    totals = [h for h in runstate.heartbeats
              if h.get("tokens_in") is not None or h.get("cost_est_usd") is not None]
    assert totals, "expected a final heartbeat carrying the run's token/cost totals"
    final = totals[-1]
    assert final["tokens_in"] == 11 and final["tokens_out"] == 5
    assert final["cost_est_usd"] == 0.002

    # spans captured for thinking / tool / output (each with its kind).
    kinds = {sp["kind"] for sp in runstate.spans}
    assert {"thinking", "tool", "output"} <= kinds
    assert all(sp["run_id"] == "run-abc" for sp in runstate.spans)
    # the output span carried the reply text.
    assert any(sp["kind"] == "output" and "Done." in sp["text"] for sp in runstate.spans)


# ---------------------------------------------------------------------------
#  Failure path — a harness error transitions the store to 'error' with detail.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_writes_error_status_on_harness_error(monkeypatch):
    runstate = FakeRunState()
    cortex = FakeCortex()
    runner = FakeRunner([
        {"type": "delta", "text": "partial"},
        {"type": "error", "message": "model not available"},
        {"type": "done"},
    ])
    routing = lambda agent, project: ("pi", "gpt-5.3", "high")

    res = await ra.run_one(
        "bob", "h-rs02", "kaidera-os",
        cortex=cortex, runner=runner, routing=routing,
        task_summary="x", runstate=runstate, run_id="run-err",
    )
    assert res.status == "failed"

    statuses = [s["status"] for s in runstate.statuses]
    assert statuses[0] == "running"
    assert statuses[-1] == "error"
    err = [s for s in runstate.statuses if s["status"] == "error"][0]
    assert err["error"] == "model not available"


# ---------------------------------------------------------------------------
#  The heartbeat — the key new liveness signal.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_heartbeats_while_streaming(monkeypatch):
    """A background heartbeat task bumps heartbeat(run_id, pid=…) on a cadence while
    the harness streams. We drive the interval to ~0 and make the stream span a few
    event-loop ticks so at least one heartbeat lands; pid is the running process."""
    import os

    # Force a tiny heartbeat interval so the test never sleeps for real.
    monkeypatch.setattr(ra, "HEARTBEAT_INTERVAL_S", 0.01, raising=False)

    class SlowRunner:
        """Yields events with an await between them so the heartbeat task can run."""
        last_call = None

        async def stream_chat(
            self,
            message,
            *,
            model=None,
            system=None,
            harness=None,
            reasoning=None,
            run_context=None,
        ):
            SlowRunner.last_call = {"harness": harness, "run_context": run_context}
            for ev in _events_ok():
                await asyncio.sleep(0.02)  # let the heartbeat task tick
                yield ev

    runstate = FakeRunState()
    cortex = FakeCortex()
    routing = lambda agent, project: ("pi", "gpt-5.3", "high")

    res = await ra.run_one(
        "bob", "h-rs03", "kaidera-os",
        cortex=cortex, runner=SlowRunner(), routing=routing,
        task_summary="x", runstate=runstate, run_id="run-hb",
    )
    assert res.status == "completed"
    assert len(runstate.heartbeats) >= 1, "expected at least one heartbeat while streaming"
    assert all(hb["run_id"] == "run-hb" for hb in runstate.heartbeats)
    assert runstate.heartbeats[0]["pid"] == os.getpid()


# ---------------------------------------------------------------------------
#  GRACEFUL-DEGRADE — a raising store, or no run_id, never breaks the run.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_survives_raising_runstate(monkeypatch):
    """If every store write RAISES, run_one STILL completes (and completes the
    handoff). Store writes are best-effort; the run + the Cortex audit proceed."""
    monkeypatch.setattr(ra, "HEARTBEAT_INTERVAL_S", 0.01, raising=False)
    cortex = FakeCortex()
    runner = FakeRunner(_events_ok())
    routing = lambda agent, project: ("pi", "gpt-5.3", "high")

    res = await ra.run_one(
        "bob", "h-rs04", "kaidera-os",
        cortex=cortex, runner=runner, routing=routing,
        task_summary="x", runstate=RaisingRunState(), run_id="run-raise",
    )
    assert res.status == "completed"
    # Cortex audit still fired (additive contract): COMPLETED was logged.
    assert any("COMPLETED" in c[1]["summary"] for c in cortex.calls if c[0] == "log")


@pytest.mark.asyncio
async def test_worker_skips_store_when_no_run_id(monkeypatch):
    """Back-compat: with no run_id (legacy spawn argv), the worker skips ALL store
    writes cleanly — no status, no spans, no heartbeat — and still runs + completes."""
    runstate = FakeRunState()
    cortex = FakeCortex()
    runner = FakeRunner(_events_ok())
    routing = lambda agent, project: ("pi", "gpt-5.3", "high")

    res = await ra.run_one(
        "bob", "h-rs05", "kaidera-os",
        cortex=cortex, runner=runner, routing=routing,
        task_summary="x", runstate=runstate, run_id=None,
    )
    assert res.status == "completed"
    assert runstate.statuses == []
    assert runstate.spans == []
    assert runstate.heartbeats == []


@pytest.mark.asyncio
async def test_worker_runstate_cortex_audit_unchanged(monkeypatch):
    """REGRESSION: the store writes must NOT change the existing cortex.log audit.
    With a run_id + store present, every existing Cortex marker still fires AND the
    store captures the run's spans (the store is the live display surface now — the
    ~/.cortex-feed feed was removed at T12)."""
    runstate = FakeRunState()
    cortex = FakeCortex()
    runner = FakeRunner(_events_ok())
    routing = lambda agent, project: ("pi", "gpt-5.3", "high")

    res = await ra.run_one(
        "kai", "h-rs06", "kaidera-os",
        cortex=cortex, runner=runner, routing=routing,
        task_summary="do the thing", runstate=runstate, run_id="run-add",
    )
    assert res.status == "completed"

    # Cortex audit unchanged.
    summaries = [c[1]["summary"] for c in cortex.calls if c[0] == "log"]
    assert any("STARTED" in s for s in summaries)
    assert any("STEP" in s for s in summaries)
    assert any("TRANSCRIPT" in s for s in summaries)
    assert any("COMPLETED" in s for s in summaries)
    # The store path fired AND captured the run's spans (the live display surface).
    assert runstate.statuses and runstate.spans
    span_kinds = {sp["kind"] for sp in runstate.spans}
    assert {"thinking", "tool", "output"} <= span_kinds


@pytest.mark.asyncio
async def test_worker_no_store_writes_when_claim_fails(monkeypatch):
    """If the worker cannot claim the handoff, it returns 'skipped' BEFORE running —
    no 'running' status should be written for a run that never started."""
    runstate = FakeRunState()
    cortex = FakeCortex(claim_ok=False)
    routing = lambda agent, project: ("pi", "gpt-5.3", "high")

    res = await ra.run_one(
        "bob", "h-rs07", "kaidera-os",
        cortex=cortex, runner=FakeRunner(_events_ok()), routing=routing,
        task_summary="x", runstate=runstate, run_id="run-noclaim",
    )
    assert res.status == "skipped"
    assert all(s["status"] != "running" for s in runstate.statuses)
    assert runstate.spans == []
