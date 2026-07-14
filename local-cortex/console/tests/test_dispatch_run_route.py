"""Milestone 1 T9 — "Approve & Run" (`POST /dispatch/{project}/run`, `dispatch_run`)
becomes a REAL run: claim → stream-into-store → complete (was a dead surface that
streamed a reply but claimed nothing, wrote nowhere, and never completed).

The rewired cycle (mirrors the detached worker `run_agent.run_one`, T6):
  * CLAIM the handoff first; a FAILED claim emits a clear error frame and STOPS —
    an unclaimable handoff must NOT run.
  * `start_run` in the store (lease_owner='approve_run', a uuid4 run_id) and surface
    `?run=<run_id>` so the T8 SSE pane follows the run live.
  * stream into the store: `set_status(run_id,'running')` at start, then
    `append_output(run_id, kind, text)` per event — WHILE keeping the user-facing
    SSE stream working (the human still sees the reply live).
  * on success → `set_status(run_id,'ok', tokens…)` + `complete_handoff`;
    on a run error → `set_status(run_id,'error', error=…)` and do NOT complete
    (the handoff stays claimed for the watchdog).
  * EVERY store/cortex call graceful-degrades — a down store/API must not crash the
    route (the reply still streams).

We drive the route function directly and drain its `EventSourceResponse.body_iterator`
(the same idiom as `test_chat_run_route.py`), with fakes for cortex + the store, so
no ASGI stack / live DB is needed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import app.main as main_mod


# ---------------------------------------------------------------------------
#  Fakes (structural — records the lifecycle calls the route makes)
# ---------------------------------------------------------------------------

class FakeCortex:
    """Records claim/complete + serves the agent/project resolution the route does.

    `claim_ok` scripts the claim outcome; `complete_ok` the completion outcome.
    `raise_on` (a set of {'claim','complete'}) forces that call to RAISE so the
    graceful-degrade path can be exercised."""

    def __init__(self, *, claim_ok=True, complete_ok=True, raise_on=frozenset()):
        self.claim_ok = claim_ok
        self.complete_ok = complete_ok
        self.raise_on = set(raise_on)
        self.calls: list[tuple[str, dict]] = []

    async def get_project(self, project_key):
        return {"project_id": "11111111-2222-4333-8444-555555555555"}

    async def get_agents(self, project_key):
        return [{"name": "kai", "display_name": "Kai", "role": "pm"}]

    async def claim_handoff(self, project_key, handoff_id, agent):
        self.calls.append(("claim", {"project": project_key, "handoff_id": handoff_id, "agent": agent}))
        if "claim" in self.raise_on:
            raise RuntimeError("cortex down (claim)")
        return self.claim_ok

    async def complete_handoff(self, project_key, handoff_id, agent):
        self.calls.append(("complete", {"project": project_key, "handoff_id": handoff_id, "agent": agent}))
        if "complete" in self.raise_on:
            raise RuntimeError("cortex down (complete)")
        return self.complete_ok


class FakeStore:
    """Records the RunStatePort calls the route makes (structural RunStatePort)."""

    def __init__(self, *, raising=False):
        self.raising = raising
        self.started: list[dict] = []
        self.statuses: list[dict] = []
        self.spans: list[dict] = []
        self.heartbeats: list[dict] = []

    async def start_run(self, *, run_id, project, agent, agent_display=None,
                        handoff_id=None, harness=None, model=None, pid=None,
                        lease_owner=None):
        if self.raising:
            raise RuntimeError("store down")
        self.started.append({
            "run_id": run_id, "project": project, "agent": agent,
            "handoff_id": handoff_id, "lease_owner": lease_owner,
            "harness": harness, "model": model,
        })
        # The adapter returns a RunRecord; the route only needs .run_id back.
        return type("Rec", (), {"run_id": run_id})()

    async def set_status(self, run_id, status, *, error=None, metadata=None):
        # SIGNATURE MIRRORS the real adapter: status/error/metadata ONLY — tokens/cost
        # land on the header via heartbeat (a looser fake masked the production
        # tokens→set_status TypeError that pinned the run 'running').
        if self.raising:
            raise RuntimeError("store down")
        self.statuses.append({
            "run_id": run_id, "status": status, "error": error, "metadata": metadata,
        })

    async def append_output(self, run_id, *, seq, kind, text):
        if self.raising:
            raise RuntimeError("store down")
        self.spans.append({"run_id": run_id, "seq": seq, "kind": kind, "text": text})

    async def heartbeat(self, run_id, *, tokens_in=None, tokens_out=None,
                        cost_est_usd=None, pid=None):
        if self.raising:
            raise RuntimeError("store down")
        self.heartbeats.append({
            "run_id": run_id, "pid": pid, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "cost_est_usd": cost_est_usd,
        })


class _Req:
    """Minimal fake Request carrying app.state for `_runstate(request)` /
    `_record_run_usage(request, ...)`. The route reads `_cortex`/`_read_posted_form`
    via monkeypatched module helpers, so this only needs `.app.state`.

    `harness_port` (harness-service bridge) is the dispatch-spawn seam: when set AND
    HARNESS_SPAWN_MODE=remote, the route spawns the WORKER on the host harness-service
    (via `harness_port.spawn_run`) instead of running `run_agent`'s lifecycle
    in-process — exactly like the orchestrator's auto-dispatch. Default None → the
    EXISTING in-process path (the local/legacy mode)."""

    def __init__(self, store, *, harness_port=None):
        self.app = type("App", (), {})()
        self.app.state = type("State", (), {})()
        self.app.state.runstate = store
        self.app.state.appdb = None  # _record_run_usage is stubbed in tests
        self.app.state.harness_port = harness_port
        self.app.state.local_run_tasks = {}


def _events_ok():
    return [
        {"type": "delta", "text": "Working"},
        {"type": "delta", "text": " on it."},
        {"type": "result", "text": "Done.", "tokens_in": 12, "tokens_out": 7, "cost_usd": 0.003},
        {"type": "done"},
    ]


def _install_common(monkeypatch, cortex, *, events=None, form=None):
    """Wire up the route's collaborators: cortex, the posted form, the harness
    stream, settings helpers, routing, and a no-op usage recorder. Returns nothing;
    the caller supplies `cortex` + the FakeStore on the request."""
    monkeypatch.setattr(main_mod, "_cortex", lambda req: cortex)

    posted = form if form is not None else {
        "summary": "do the work", "handoff_id": "h-run-1", "handoff_compound": "h-run-1:test1",
    }

    async def _fake_form(req):
        return posted
    monkeypatch.setattr(main_mod, "_read_posted_form", _fake_form)

    evs = events if events is not None else _events_ok()

    async def _fake_stream(msg, *, model=None, system=None, harness=None, **kw):
        for ev in evs:
            yield ev
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _fake_stream)

    monkeypatch.setattr(main_mod.settings_store, "get_agent_override", lambda p, a: {})
    monkeypatch.setattr(main_mod.settings_store, "normalize_designation", lambda d: "interactive")
    monkeypatch.setattr(main_mod.settings_store, "is_propose_mode", lambda p: False)
    monkeypatch.setattr(main_mod.settings_store, "get_approval_status", lambda p, h: None)
    monkeypatch.setattr(main_mod, "_chat_routing_for", lambda agent, project: ("claude-code", "opus", "max"))

    async def _fake_record(*a, **k):
        return None
    monkeypatch.setattr(main_mod, "_record_run_usage", _fake_record)


async def _drain(resp):
    """Exhaust an EventSourceResponse body, returning the yielded frame dicts."""
    frames = []
    async for frame in resp.body_iterator:
        frames.append(frame)
    await asyncio.sleep(0)
    return frames


# ---------------------------------------------------------------------------
#  Happy path — claim → running → spans → ok → complete, reply still streams.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_run_claims_streams_to_store_and_completes(monkeypatch):
    cortex = FakeCortex(claim_ok=True, complete_ok=True)
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    # 1. CLAIM happened, and BEFORE completion (claim is the first cortex call).
    kinds = [c[0] for c in cortex.calls]
    assert "claim" in kinds, "Approve & Run must CLAIM the handoff"
    assert kinds.index("claim") < kinds.index("complete"), "claim must precede complete"
    claim = [c for c in cortex.calls if c[0] == "claim"][0][1]
    assert claim["handoff_id"] == "h-run-1"
    assert claim["project"] == "kaidera-os"

    # 2. start_run opened a run row with the approve_run lease + a uuid4 run_id.
    assert len(store.started) == 1
    started = store.started[0]
    run_id = started["run_id"]
    assert started["lease_owner"] == "approve_run"
    assert started["handoff_id"] == "h-run-1"
    # uuid4 shape (36 chars, 4 dashes) — a real pre-created id, not a placeholder.
    assert len(run_id) == 36 and run_id.count("-") == 4

    # 3. status walked running → ok (terminal ok), all on the SAME run_id.
    statuses = [(s["run_id"], s["status"]) for s in store.statuses]
    assert statuses[0] == (run_id, "running")
    assert statuses[-1] == (run_id, "ok")
    assert all(rid == run_id for rid, _ in statuses)
    # terminal telemetry lands on the run HEADER via heartbeat (the adapter contract —
    # set_status carries NO tokens); the status row itself is a clean 'ok'.
    totals = [h for h in store.heartbeats
              if h.get("tokens_in") is not None or h.get("cost_est_usd") is not None]
    assert totals, "expected a final heartbeat carrying the run's token/cost totals"
    assert totals[-1]["tokens_in"] == 12 and totals[-1]["tokens_out"] == 7

    # 4. spans were written for the streamed output, on the SAME run_id, with
    #    strictly increasing seq.
    assert store.spans, "output must be appended to the store as spans"
    assert all(sp["run_id"] == run_id for sp in store.spans)
    seqs = [sp["seq"] for sp in store.spans]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq must be unique + ordered"
    assert any("Working" in sp["text"] or "Done." in sp["text"] for sp in store.spans)

    # 5. COMPLETED on success.
    assert any(c[0] == "complete" and c[1]["handoff_id"] == "h-run-1" for c in cortex.calls)

    # 6. The control stream detaches immediately; live output follows run-state.
    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)

    # 7. The pane is told which run to follow (?run=<run_id>) via a dedicated frame.
    run_frames = [f for f in frames if f.get("event") == "run"]
    assert run_frames, "a 'run' frame must surface the run_id so the SSE pane can follow it"
    assert json.loads(run_frames[0]["data"]).get("run_id") == run_id


@pytest.mark.asyncio
async def test_dispatch_run_with_store_detaches_local_run_from_control_stream(monkeypatch):
    """Store-backed local Approve & Run claims/opens the row, then returns run+done
    while the harness continues in the app-local task registry."""
    cortex = FakeCortex(claim_ok=True, complete_ok=True)
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_stream(msg, *, model=None, system=None, harness=None, **kw):
        started.set()
        await release.wait()
        yield {"type": "result", "text": "late done", "tokens_in": 1, "tokens_out": 1}
        yield {"type": "done"}

    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _blocking_stream)

    req = _Req(store)
    resp = await main_mod.dispatch_run(req, "kaidera-os", "kai")
    frames = await _drain(resp)

    run_id = store.started[0]["run_id"]
    assert any(c[0] == "claim" for c in cortex.calls)
    assert started.is_set(), "background task should have started"
    assert run_id in req.app.state.local_run_tasks
    assert [f.get("event") for f in frames] == ["run", "done"]
    assert not [f for f in frames if f.get("event") in ("delta", "result")]

    release.set()
    await asyncio.gather(*req.app.state.local_run_tasks.values(), return_exceptions=True)
    assert [s["status"] for s in store.statuses][-1] == "ok"
    assert any(c[0] == "complete" for c in cortex.calls)


# ---------------------------------------------------------------------------
#  Reasoning-forward (audit fix) — Approve & Run resolves the agent's effective
#  reasoning/effort via `_chat_routing_for` and MUST forward it to
#  `stream_chat(reasoning=…)`, just like the detached `chat_run.chat_one`. The bug
#  dropped it, so a dispatched run ran at the provider default instead of the
#  operator-configured level. (`_chat_routing_for` is stubbed to
#  ("claude-code","opus","max") in `_install_common`, so resolved reasoning="max".)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_run_forwards_resolved_reasoning_to_stream_chat(monkeypatch):
    cortex = FakeCortex(claim_ok=True, complete_ok=True)
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    seen: dict = {}

    async def _spy_stream(msg, *, model=None, system=None, harness=None, reasoning=None, **kw):
        seen["reasoning"] = reasoning
        seen["model"] = model
        seen["harness"] = harness
        seen["run_context"] = kw.get("run_context")
        for ev in _events_ok():
            yield ev
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    assert seen.get("reasoning") == "max", (
        "Approve & Run must forward the resolved reasoning to stream_chat "
        "(chat_run.chat_one already does this; dispatch_run must too)"
    )
    assert seen.get("run_context") == "approve_run"
    assert seen.get("model") == "opus"
    assert seen.get("harness") == "claude-code"


# ---------------------------------------------------------------------------
#  Failed claim — error frame, NO run (no store writes, no completion).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_run_failed_claim_errors_without_running(monkeypatch):
    cortex = FakeCortex(claim_ok=False)
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    # Claim was attempted and FAILED → the route must NOT run the harness.
    assert any(c[0] == "claim" for c in cortex.calls)
    # No completion (never ran), and the store saw no running/ok transitions.
    assert all(c[0] != "complete" for c in cortex.calls)
    assert all(s["status"] != "running" for s in store.statuses)
    assert all(s["status"] != "ok" for s in store.statuses)
    assert store.spans == [], "an unclaimable handoff must write no spans"

    # A clear error frame was emitted (and the stream closed with done).
    err = [f for f in frames if f.get("event") == "error"]
    assert err, "a failed claim must emit an error frame"
    payload = json.loads(err[0]["data"])
    assert payload.get("category") == "claim_failed"
    assert any(f.get("event") == "done" for f in frames)


# ---------------------------------------------------------------------------
#  Propose-mode invariant — a direct POST must not bypass approval.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_run_requires_approval_when_propose_mode_on(monkeypatch):
    cortex = FakeCortex(claim_ok=True)
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setattr(main_mod.settings_store, "is_propose_mode", lambda p: True)
    monkeypatch.setattr(main_mod.settings_store, "get_approval_status", lambda p, h: "awaiting")

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert all(c[0] != "claim" for c in cortex.calls)
    assert store.started == []
    err = [f for f in frames if f.get("event") == "error"]
    assert err and json.loads(err[0]["data"]).get("category") == "approval_required"
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_dispatch_run_approved_handoff_proceeds_in_propose_mode(monkeypatch):
    cortex = FakeCortex(claim_ok=True, complete_ok=True)
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setattr(main_mod.settings_store, "is_propose_mode", lambda p: True)
    monkeypatch.setattr(main_mod.settings_store, "get_approval_status", lambda p, h: "approved")

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert any(c[0] == "claim" for c in cortex.calls)
    assert any(c[0] == "complete" for c in cortex.calls)
    assert any(s["status"] == "ok" for s in store.statuses)
    assert any(f.get("event") == "done" for f in frames)


# ---------------------------------------------------------------------------
#  Run error — status 'error', handoff NOT completed (left claimed for watchdog).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_run_run_error_sets_error_and_does_not_complete(monkeypatch):
    cortex = FakeCortex(claim_ok=True)
    store = FakeStore()
    _install_common(monkeypatch, cortex, events=[
        {"type": "delta", "text": "partial"},
        {"type": "error", "message": "model not available"},
        {"type": "done"},
    ])

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    # Claimed + ran, but the run errored.
    assert any(c[0] == "claim" for c in cortex.calls)
    statuses = [s["status"] for s in store.statuses]
    assert "running" in statuses
    assert statuses[-1] == "error", "a run error must set terminal status 'error'"
    err = [s for s in store.statuses if s["status"] == "error"][0]
    assert err["error"] and "model not available" in err["error"]

    # FALSE-COMPLETE GUARD: the handoff must NOT be completed on a failed run.
    assert all(c[0] != "complete" for c in cortex.calls), (
        "a failed run must NOT complete the handoff (leave it claimed for the watchdog)"
    )
    # The detached control stream closes cleanly; the error is in run-state.
    assert not [f for f in frames if f.get("event") == "error"]
    assert any(f.get("event") == "done" for f in frames)


# ---------------------------------------------------------------------------
#  Graceful-degrade — a RAISING store never crashes the route; reply still streams,
#  claim + complete still happen.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_run_survives_raising_store(monkeypatch):
    cortex = FakeCortex(claim_ok=True, complete_ok=True)
    store = FakeStore(raising=True)  # every store call raises
    _install_common(monkeypatch, cortex)

    # Must NOT raise — the route swallows store failures.
    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    # The run still claimed, streamed the reply, and completed the handoff.
    assert any(c[0] == "claim" for c in cortex.calls)
    assert any(c[0] == "complete" for c in cortex.calls)
    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_dispatch_run_survives_none_store(monkeypatch):
    """A None run-state store (store failed to construct / app-DB down) must not
    crash the route — claim/complete + the reply stream still work."""
    cortex = FakeCortex(claim_ok=True, complete_ok=True)
    _install_common(monkeypatch, cortex)

    resp = await main_mod.dispatch_run(_Req(None), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert any(c[0] == "claim" for c in cortex.calls)
    assert any(c[0] == "complete" for c in cortex.calls)
    body = "".join(
        json.loads(f["data"]).get("text", "")
        for f in frames if f.get("event") in ("delta", "result")
    )
    assert "Done." in body


@pytest.mark.asyncio
async def test_dispatch_run_survives_raising_complete(monkeypatch):
    """If `complete_handoff` itself RAISES (cortex blip on completion), the route
    must still finish cleanly — the run already streamed; the watchdog re-completes."""
    cortex = FakeCortex(claim_ok=True, raise_on={"complete"})
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    # It attempted to complete (and that raised) but the stream still closed cleanly.
    assert any(c[0] == "complete" for c in cortex.calls)
    assert any(f.get("event") == "done" for f in frames)
    # The success status was still recorded in the store.
    assert any(s["status"] == "ok" for s in store.statuses)


# ---------------------------------------------------------------------------
#  Early-validation frames unchanged — unknown agent / empty summary still error
#  cleanly WITHOUT claiming or running.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_run_unknown_agent_errors_without_claim(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "nobody")
    frames = await _drain(resp)

    assert all(c[0] != "claim" for c in cortex.calls), "unknown agent must not claim"
    assert store.started == []
    err = [f for f in frames if f.get("event") == "error"]
    assert err and json.loads(err[0]["data"]).get("category") == "unknown_agent"


@pytest.mark.asyncio
async def test_dispatch_run_empty_summary_errors_without_claim(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex, form={
        "summary": "   ", "handoff_id": "h-empty", "handoff_compound": "h-empty:test1",
    })

    resp = await main_mod.dispatch_run(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert all(c[0] != "claim" for c in cortex.calls), "empty summary must not claim"
    assert store.started == []
    err = [f for f in frames if f.get("event") == "error"]
    assert err and json.loads(err[0]["data"]).get("category") == "empty_handoff"


# ---------------------------------------------------------------------------
#  Harness-service BRIDGE (dogfood fix) — Approve & Run must spawn the WORKER on the
#  host harness-service in remote mode, NOT run run_agent's lifecycle in-process. In
#  a containerized console there is no claude/pi CLI, so the in-process path can't
#  execute — the run becomes a ghost (status=running, no host process) and the
#  handoff strands `claimed` forever. So:
#    * HARNESS_SPAWN_MODE=remote + a harness_port wired → the route PRE-CREATES the
#      run_state row, calls harness_port.spawn_run(SpawnRequest(run_id/project/agent/
#      handoff_id)), surfaces the run_id (`run` frame), and does NOT call the
#      in-process stream_chat. The reply arrives via /runstate/stream (the worker
#      writes the SAME run_id's row), so we just close the SSE with `done`.
#    * CLAIM-EXACTLY-ONCE: the route does NOT pre-claim in remote mode — the spawned
#      worker (run_agent.run_one) is the SOLE claimer (matching the orchestrator's
#      auto-dispatch). So in remote mode the route makes NO claim/complete call.
#    * NO-STRAND-ON-FAILURE: a rejected spawn (accepted=False — service down) → the
#      route marks the run errored + emits an error frame (never a ghost `running`),
#      and since it never claimed, the handoff is NOT stranded.
#    * LOCAL/LEGACY (no port, or flag unset) → the EXISTING in-process claim → stream
#      → complete path runs UNCHANGED (the tests above, all passing harness_port=None,
#      prove it).
#  Mirrors agent_chat's remote-bridge fork (test_chat_run_route.py) exactly.
# ---------------------------------------------------------------------------

class FakeHarnessPort:
    """Structural HarnessPort recording the SpawnRequest the route hands it.
    `accepted` scripts the returned SpawnHandle; spawn_chat/cancel_run exist so it
    satisfies the whole port surface (the dispatch route only calls spawn_run)."""

    def __init__(self, *, accepted=True):
        self.accepted = accepted
        self.run_requests: list = []

    async def spawn_run(self, request):
        self.run_requests.append(request)
        from app.domain.harness import SpawnHandle
        # The async "dispatched" shape (exit_code=None): the worker reports its
        # terminal state later via the run-state row it shares.
        return SpawnHandle(run_id=request.run_id, accepted=self.accepted,
                           exit_code=None,
                           error=None if self.accepted else "service down")

    async def spawn_chat(self, request):  # pragma: no cover - not used by dispatch
        from app.domain.harness import SpawnHandle
        return SpawnHandle(run_id=request.run_id, accepted=True, exit_code=None)

    async def cancel_run(self, run_id):  # pragma: no cover - not used by dispatch
        return False


@pytest.mark.asyncio
async def test_dispatch_run_remote_mode_spawns_worker_not_in_process(monkeypatch):
    """HARNESS_SPAWN_MODE=remote + a harness_port → the route calls
    harness_port.spawn_run with a SpawnRequest carrying (project, agent, handoff_id)
    + the pre-created run_id, and does NOT run run_agent's lifecycle in-process
    (no in-process stream_chat, no in-process claim/complete)."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    # If the in-process path were taken, stream_chat would be invoked — assert NOT.
    stream_calls: list = []

    async def _spy_stream(msg, *, model=None, system=None, harness=None, **kw):
        stream_calls.append(msg)
        if False:  # pragma: no cover - generator shape only; never yields here
            yield {}
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    port = FakeHarnessPort(accepted=True)
    resp = await main_mod.dispatch_run(_Req(store, harness_port=port), "kaidera-os", "kai")
    frames = await _drain(resp)

    # 1. The remote seam was used: spawn_run called once with the worker scope.
    assert len(port.run_requests) == 1, "remote mode must call harness_port.spawn_run"
    req = port.run_requests[0]
    assert req.project == "kaidera-os"
    assert req.agent == "kai"
    assert req.handoff_id == "h-run-1"
    # The harness/model resolved via _chat_routing_for ride along (stubbed values).
    assert req.harness == "claude-code"
    assert req.model == "opus"

    # 2. The run_state row was pre-created with the approve_run lease + the SAME
    #    run_id that goes into the SpawnRequest (the worker writes that row).
    assert len(store.started) == 1
    started = store.started[0]
    assert started["lease_owner"] == "approve_run"
    assert started["handoff_id"] == "h-run-1"
    assert req.run_id == started["run_id"]
    assert len(req.run_id) == 36 and req.run_id.count("-") == 4

    # 3. The in-process harness stream was NOT invoked.
    assert stream_calls == [], "remote mode must NOT run stream_chat in-process"

    # 4. CLAIM-EXACTLY-ONCE: the route does NOT claim/complete in remote mode — the
    #    spawned worker is the sole claimer (matches the orchestrator's auto-dispatch).
    assert all(c[0] != "claim" for c in cortex.calls), (
        "remote mode must NOT pre-claim — the worker claims exactly once"
    )
    assert all(c[0] != "complete" for c in cortex.calls), (
        "remote mode must NOT complete in-process — the worker completes"
    )

    # 5. The run_id is surfaced so the /runstate/stream pane follows the reply, and a
    #    `done` frame closes the SSE (the reply itself arrives via the run-state row).
    run_frames = [f for f in frames if f.get("event") == "run"]
    assert run_frames, "a 'run' frame must surface the run_id"
    assert json.loads(run_frames[0]["data"]).get("run_id") == req.run_id
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_dispatch_run_remote_rejected_spawn_errors_without_stranding(monkeypatch):
    """If the host service rejects the spawn (accepted=False — service down), the
    route marks the run errored + emits an error frame (never a ghost `running`), and
    since it NEVER claimed, the handoff is NOT stranded (no claim/complete at all)."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    port = FakeHarnessPort(accepted=False)
    resp = await main_mod.dispatch_run(_Req(store, harness_port=port), "kaidera-os", "kai")
    frames = await _drain(resp)

    # The spawn was attempted (and rejected).
    assert len(port.run_requests) == 1

    # NO-STRAND: the route never claimed, so there is nothing left claimed. (The
    # worker claims; a rejected spawn means it never started → never claimed.)
    assert all(c[0] != "claim" for c in cortex.calls), (
        "a rejected remote spawn must leave the handoff unclaimed (not stranded)"
    )
    assert all(c[0] != "complete" for c in cortex.calls)

    # The run is marked errored (NOT a ghost `running`) — the terminal status is error.
    statuses = [s["status"] for s in store.statuses]
    assert statuses, "the pre-created run must get a terminal status"
    assert statuses[-1] == "error", "a rejected spawn must set terminal status 'error'"
    assert all(s != "running" for s in statuses), "a rejected spawn must NOT leave it running"

    # A clear error frame reached the browser, and the SSE closed.
    err = [f for f in frames if f.get("event") == "error"]
    assert err, "a rejected spawn must emit an error frame (fail loudly)"
    assert json.loads(err[0]["data"]).get("category") == "dispatch_spawn_rejected"
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_dispatch_run_remote_flag_but_no_port_uses_in_process(monkeypatch):
    """Flag set to remote but NO harness_port wired (factory failed-closed to None) →
    the route falls back to the in-process claim → stream → complete path (no
    spawn_run to call); the reply streams as before and the handoff completes."""
    cortex = FakeCortex(claim_ok=True, complete_ok=True)
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    # harness_port=None (default) → in-process path.
    resp = await main_mod.dispatch_run(_Req(store, harness_port=None), "kaidera-os", "kai")
    frames = await _drain(resp)

    # In-process path → it claimed, streamed, and completed.
    assert any(c[0] == "claim" for c in cortex.calls)
    assert any(c[0] == "complete" for c in cortex.calls)
    assert [s["status"] for s in store.statuses][-1] == "ok"
    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_dispatch_run_flag_unset_with_port_still_in_process(monkeypatch):
    """A harness_port wired but HARNESS_SPAWN_MODE unset → the route takes the
    in-process path (the flag, not the mere presence of a port, gates the remote
    seam). spawn_run is NOT called; the in-process claim → stream → complete runs."""
    cortex = FakeCortex(claim_ok=True, complete_ok=True)
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.delenv("HARNESS_SPAWN_MODE", raising=False)

    port = FakeHarnessPort(accepted=True)
    resp = await main_mod.dispatch_run(_Req(store, harness_port=port), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert port.run_requests == [], "flag unset → must NOT call spawn_run"
    # In-process path ran (claimed + completed).
    assert any(c[0] == "claim" for c in cortex.calls)
    assert any(c[0] == "complete" for c in cortex.calls)
    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_dispatch_run_remote_spawn_handle_raises_does_not_strand(monkeypatch):
    """Belt-and-braces: even if spawn_run RAISES (a misbehaving adapter that breaks
    the port's never-raise contract), the route must not crash AND must not strand a
    claim — it never claimed in remote mode, and the run lands terminal `error`."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    class _RaisingPort(FakeHarnessPort):
        async def spawn_run(self, request):
            raise RuntimeError("adapter blew up")

    resp = await main_mod.dispatch_run(_Req(store, harness_port=_RaisingPort()), "kaidera-os", "kai")
    frames = await _drain(resp)

    # Never claimed (remote mode) → nothing stranded.
    assert all(c[0] != "claim" for c in cortex.calls)
    # Terminal error, never a ghost running; an error frame + done closed the SSE.
    statuses = [s["status"] for s in store.statuses]
    assert statuses and statuses[-1] == "error"
    assert all(s != "running" for s in statuses)
    assert any(f.get("event") == "error" for f in frames)
    assert any(f.get("event") == "done" for f in frames)
