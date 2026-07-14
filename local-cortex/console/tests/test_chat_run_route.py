"""Milestone 1 T10 — interactive chat (`POST /agents/{p}/{a}/chat`, `agent_chat`)
writes to the RunState SSOT store, so an interactive run shows up on the SAME live
surface (the T8 `/runstate/stream` pane) as an autonomous one.

The wired cycle MIRRORS the detached worker / Approve&Run (T6/T9), MINUS the
claim/complete — interactive chat has NO handoff, so there is nothing to claim or
complete:
  * `start_run(run_id=<uuid4>, handoff_id=None, lease_owner='chat')` opens the row;
  * `set_status(run_id,'running')` at start;
  * `append_output(run_id, seq, kind, text)` per streamed delta/result;
  * on done → `set_status(run_id,'ok', tokens…/cost…)`; on a harness error →
    `set_status(run_id,'error', error=…)`.
  * EVERY store call graceful-degrades — a None / raising store must NOT crash the
    route; the reply still streams. (The ~/.cortex-feed feed write was removed at T12;
    the store spans ARE the live surface now.)

We drive the route function directly and drain its `EventSourceResponse.body_iterator`
(the same idiom as `test_dispatch_run_route.py`), with a fake store, so no ASGI stack
/ live DB is needed.

CRITICAL CONTRAST WITH T9 (Approve & Run): chat NEVER touches the handoff lifecycle.
There is no `cortex.claim_handoff` / `cortex.complete_handoff` here — a chat is a
free-standing run. These tests pin that: `lease_owner == 'chat'`, `handoff_id is None`,
and the cortex client only ever resolves the agent (get_project/get_agents).
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

import app.main as main_mod


def test_chat_skill_delivery_reads_structured_boot_output(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return type("Result", (), {"stdout": '{"persona":{"skills":[]}}'})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    system = main_mod._system_with_delivered_skills(
        "base system",
        "marlow",
        "marketing",
        "check inbox",
    )

    assert system == "base system"
    assert calls == [["cortex-boot", "marlow"]]


# ---------------------------------------------------------------------------
#  Fakes (structural — records the lifecycle calls the route makes)
# ---------------------------------------------------------------------------

class FakeCortex:
    """Resolves the agent/project the chat route needs. Records EVERY call so the
    tests can prove chat never claims/completes (it only resolves the agent)."""

    def __init__(self):
        self.calls: list[str] = []

    async def get_project(self, project_key):
        self.calls.append("get_project")
        return {"project_id": "11111111-2222-4333-8444-555555555555"}

    async def get_agents(self, project_key):
        self.calls.append("get_agents")
        return [{"name": "kai", "display_name": "Kai", "role": "pm"}]


class FakeStore:
    """Records the RunStatePort calls the chat route makes (structural RunStatePort).

    `raising=True` forces every method to raise so the graceful-degrade path is
    exercised (a down store must never crash the chat)."""

    def __init__(self, *, raising=False):
        self.raising = raising
        self.started: list[dict] = []
        self.statuses: list[dict] = []
        self.spans: list[dict] = []
        self.heartbeats: list[dict] = []

    async def start_run(self, *, run_id, project, agent, agent_display=None,
                        handoff_id=None, harness=None, model=None, pid=None,
                        lease_owner=None, session_id=None):
        if self.raising:
            raise RuntimeError("store down")
        self.started.append({
            "run_id": run_id, "project": project, "agent": agent,
            "handoff_id": handoff_id, "lease_owner": lease_owner,
            "harness": harness, "model": model, "session_id": session_id,
        })
        return type("Rec", (), {"run_id": run_id})()

    # Multi-turn chat (Inc B): the read path the route threads history through. By
    # default no prior turns (empty session) → the route stays single-shot. A test can
    # subclass and override `recent`/`get_run` to script a conversation.
    async def recent(self, project=None, limit=20, *, session_id=None):
        if self.raising:
            raise RuntimeError("store down")
        return []

    async def get_run(self, run_id):
        if self.raising:
            raise RuntimeError("store down")
        return None

    async def set_status(self, run_id, status, *, error=None, metadata=None):
        # SIGNATURE MIRRORS the real adapter (RunStatePgStore.set_status): status/error/
        # metadata ONLY — NOT tokens/cost (those land on the header via heartbeat). A
        # looser fake here masked the production TypeError (tokens→set_status → run
        # stuck 'running').
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

    `harness_port` (Increment 4) is the chat-dispatch seam: when set AND
    HARNESS_SPAWN_MODE=remote, the route POSTs the chat to the host service instead of
    running `stream_chat` in-process. Defaults to None so the EXISTING in-process tests
    (`_Req(store)`) keep taking the in-process path byte-for-byte."""

    def __init__(self, store, *, harness_port=None):
        self.app = type("App", (), {})()
        self.app.state = type("State", (), {})()
        self.app.state.runstate = store
        self.app.state.appdb = None  # _record_run_usage is stubbed in tests
        self.app.state.harness_port = harness_port
        self.app.state.local_run_tasks = {}


def _events_ok():
    return [
        {"type": "delta", "text": "Thinking"},
        {"type": "delta", "text": " about it."},
        {"type": "result", "text": "All done.", "tokens_in": 9, "tokens_out": 4, "cost_usd": 0.002},
        {"type": "done"},
    ]


def _install_common(monkeypatch, cortex, *, events=None, form=None):
    """Wire the route's collaborators: cortex, the posted form, the harness stream,
    settings helpers, routing, and a no-op usage recorder."""
    monkeypatch.setattr(main_mod, "_cortex", lambda req: cortex)

    posted = form if form is not None else {"message": "hello there"}

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
    monkeypatch.setattr(main_mod, "_chat_routing_for", lambda agent, project: ("claude-code", "opus", "max"))

    async def _fake_record(*a, **k):
        return None
    monkeypatch.setattr(main_mod, "_record_run_usage", _fake_record)

    # Stub the never-forget LTM write by DEFAULT so no test spawns a real `cortex-log`
    # subprocess (hermetic boundary: no live Cortex writes from tests). The dedicated
    # LTM tests re-patch this via `_capture_persist` to assert the call / force a raise.
    async def _noop_persist(*a, **k):
        return None
    monkeypatch.setattr(main_mod.chat_ltm_module, "persist_chat_turn", _noop_persist)


async def _drain(resp):
    """Exhaust an EventSourceResponse body, returning the yielded frame dicts."""
    frames = []
    async for frame in resp.body_iterator:
        frames.append(frame)
    await asyncio.sleep(0)
    return frames


# ---------------------------------------------------------------------------
#  Reasoning-forward (audit fix) — the in-process chat route resolves the agent's
#  effective reasoning/effort via `_chat_routing_for` and MUST forward it to
#  `stream_chat(reasoning=…)`, exactly as the detached `chat_run.chat_one` does.
#  The bug: the route dropped it, so the pi lane ran at the provider default
#  instead of the operator-configured level. We mock the runner and assert the
#  kwarg lands. (`_chat_routing_for` is stubbed to ("claude-code","opus","max")
#  in `_install_common`, so the resolved reasoning is "max".)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_forwards_resolved_reasoning_to_stream_chat(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    # Spy that captures stream_chat's kwargs (reasoning explicitly named, not via
    # **kw, so a DROPPED kwarg is recorded as missing — the bug this pins).
    seen: dict = {}

    async def _spy_stream(msg, *, model=None, system=None, harness=None, reasoning=None, **kw):
        seen["reasoning"] = reasoning
        seen["model"] = model
        seen["harness"] = harness
        for ev in _events_ok():
            yield ev
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    assert seen.get("reasoning") == "max", (
        "the chat route must forward the resolved reasoning to stream_chat "
        "(chat_run.chat_one already does this; agent_chat must too)"
    )
    # The other resolved routing fields still flow (regression guard).
    assert seen.get("model") == "opus"
    assert seen.get("harness") == "claude-code"


# ---------------------------------------------------------------------------
#  Happy path — running → spans → ok, lease_owner='chat', NO claim/complete,
#  reply still streams.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_writes_running_spans_ok_to_store(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    # 1. start_run opened a run row with the CHAT lease, NO handoff, + a uuid4 run_id.
    assert len(store.started) == 1, "interactive chat must open ONE run_state row"
    started = store.started[0]
    run_id = started["run_id"]
    assert started["lease_owner"] == "chat", "interactive chat rows use lease_owner='chat'"
    assert started["handoff_id"] is None, "interactive chat has NO handoff to attach"
    assert started["project"] == "kaidera-os"
    # uuid4 shape (36 chars, 4 dashes) — a real pre-created id, not a placeholder.
    assert len(run_id) == 36 and run_id.count("-") == 4

    # 2. status walked running → ok (terminal ok), all on the SAME run_id.
    statuses = [(s["run_id"], s["status"]) for s in store.statuses]
    assert statuses[0] == (run_id, "running"), "first status must be 'running'"
    assert statuses[-1] == (run_id, "ok"), "a clean chat ends 'ok'"
    assert all(rid == run_id for rid, _ in statuses)
    # terminal telemetry lands on the run HEADER via heartbeat (the adapter contract —
    # set_status carries NO tokens); the status row itself is a clean 'ok'.
    totals = [h for h in store.heartbeats
              if h.get("tokens_in") is not None or h.get("cost_est_usd") is not None]
    assert totals, "expected a final heartbeat carrying the turn's token/cost totals"
    assert totals[-1]["tokens_in"] == 9 and totals[-1]["tokens_out"] == 4

    # 3. spans were written for the streamed output, on the SAME run_id, strictly
    #    increasing seq, capturing the streamed text.
    assert store.spans, "chat output must be appended to the store as spans"
    assert all(sp["run_id"] == run_id for sp in store.spans)
    seqs = [sp["seq"] for sp in store.spans]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq must be unique + ordered"
    joined = "".join(sp["text"] for sp in store.spans)
    assert "Thinking" in joined and "All done." in joined

    # 4. NO claim / complete: chat is a free-standing run; the cortex client only
    #    resolved the agent (no claim_handoff/complete_handoff even exist here).
    assert cortex.calls and all(c in ("get_project", "get_agents") for c in cortex.calls), (
        "interactive chat must NOT claim or complete any handoff"
    )

    # 5. The POST control stream detaches immediately; live output follows
    #    /runstate/stream from the background task.
    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_chat_pins_run_id_via_run_frame(monkeypatch):
    """The chat surfaces the run_id (a `run` frame) so the T8 `/runstate/stream` pane
    can follow THIS interactive run live — same mechanism as Approve & Run."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    run_id = store.started[0]["run_id"]
    run_frames = [f for f in frames if f.get("event") == "run"]
    assert run_frames, "a 'run' frame must surface the run_id so the SSE pane can follow it"
    assert json.loads(run_frames[0]["data"]).get("run_id") == run_id


@pytest.mark.asyncio
async def test_chat_with_store_detaches_local_run_from_control_stream(monkeypatch):
    """Store-backed local chat returns run+done while the harness continues in the
    app-local task registry."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_stream(msg, *, model=None, system=None, harness=None, **kw):
        started.set()
        await release.wait()
        yield {"type": "result", "text": "late reply", "tokens_in": 1, "tokens_out": 1}
        yield {"type": "done"}

    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _blocking_stream)

    req = _Req(store)
    resp = await main_mod.agent_chat(req, "kaidera-os", "kai")
    frames = await _drain(resp)

    run_id = store.started[0]["run_id"]
    assert started.is_set(), "background task should have started"
    assert run_id in req.app.state.local_run_tasks
    assert [f.get("event") for f in frames] == ["run", "done"]
    assert not [f for f in frames if f.get("event") in ("delta", "result")]

    release.set()
    await asyncio.gather(*req.app.state.local_run_tasks.values(), return_exceptions=True)
    assert [s["status"] for s in store.statuses][-1] == "ok"


@pytest.mark.asyncio
async def test_chat_route_records_visibility_spans_without_direct_streaming(monkeypatch):
    """Thinking/tool/tasks/sub-agent visibility lands in run-state, not POST deltas."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex, events=[
        {"type": "thinking", "text": "planning"},
        {"type": "tool", "name": "run_bash", "text": "run_bash: pwd"},
        {"type": "tasks", "items": [{"content": "Ask clarifying question", "status": "pending"}]},
        {"type": "subagent", "label": "qa"},
        {"type": "delta", "text": "Answer"},
        {"type": "result", "text": "Answer", "tokens_in": 1, "tokens_out": 1},
        {"type": "done"},
    ])

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert not [f for f in frames if f.get("event") in ("delta", "thinking", "tool", "tasks", "subagent")]
    assert not [f for f in frames if f.get("event") == "result"]
    spans = [(s["kind"], s["text"]) for s in store.spans]
    assert ("thinking", "planning") in spans
    assert ("tool", "run_bash: pwd") in spans
    assert any(kind == "tasks" and "Ask clarifying question" in text for kind, text in spans)
    assert ("subagent", "qa") in spans


# ---------------------------------------------------------------------------
#  Run error — terminal status 'error' carrying the harness message.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_run_error_sets_error_status(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex, events=[
        {"type": "delta", "text": "partial"},
        {"type": "error", "message": "model not available", "category": "model_unavailable"},
        {"type": "done"},
    ])

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    statuses = [s["status"] for s in store.statuses]
    assert "running" in statuses
    assert statuses[-1] == "error", "a harness error must set terminal status 'error'"
    err = [s for s in store.statuses if s["status"] == "error"][0]
    assert err["error"] and "model not available" in err["error"]
    # The detached control stream closes cleanly; the error is in run-state.
    assert not [f for f in frames if f.get("event") == "error"]
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_chat_stream_exception_sets_error_status_and_done(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    async def _boom_stream(msg, *, model=None, system=None, harness=None, reasoning=None, **kw):
        yield {"type": "delta", "text": "partial"}
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _boom_stream)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    statuses = [s["status"] for s in store.statuses]
    assert statuses[0] == "running"
    assert statuses[-1] == "error"
    err_status = [s for s in store.statuses if s["status"] == "error"][0]
    assert "runner exploded" in (err_status["error"] or "")
    assert not [f for f in frames if f.get("event") == "error"]
    assert any(f.get("event") == "done" for f in frames)


# ---------------------------------------------------------------------------
#  Graceful-degrade — a RAISING store never crashes the route; reply still streams.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_survives_raising_store(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore(raising=True)  # every store call raises
    _install_common(monkeypatch, cortex)

    # Must NOT raise — the route swallows store failures.
    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_chat_survives_none_store(monkeypatch):
    """A None run-state store (store failed to construct / app-DB down) must not crash
    the chat — the reply still streams."""
    cortex = FakeCortex()
    _install_common(monkeypatch, cortex)

    resp = await main_mod.agent_chat(_Req(None), "kaidera-os", "kai")
    frames = await _drain(resp)

    body = "".join(
        json.loads(f["data"]).get("text", "")
        for f in frames if f.get("event") in ("delta", "result")
    )
    assert "All done." in body
    assert any(f.get("event") == "done" for f in frames)


# ---------------------------------------------------------------------------
#  Unknown agent — unchanged early error, NO store row opened.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_unknown_agent_errors_without_opening_run(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "nobody")
    frames = await _drain(resp)

    assert store.started == [], "an unknown agent must not open a run_state row"
    assert store.statuses == []
    err = [f for f in frames if f.get("event") == "error"]
    assert err and json.loads(err[0]["data"]).get("category") == "unknown_agent"


# ---------------------------------------------------------------------------
#  Increment 4 — the REMOTE chat seam (ADDITIVE + FLAGGED).
#
#  When HARNESS_SPAWN_MODE=remote AND a harness_port is wired on app.state, the chat
#  route POSTs the chat turn to the host harness-service (via harness_port.spawn_chat)
#  instead of running stream_chat in-process: it PRE-CREATES the run_state row (so the
#  UI follows the reply via /runstate/stream), surfaces the run_id via the `run`
#  frame, and does NOT call the in-process harness_runner.stream_chat. With the flag
#  unset/local the EXISTING in-process path runs byte-for-byte (the tests above, all
#  passing _Req(store) with harness_port=None, prove it).
# ---------------------------------------------------------------------------

class FakeHarnessPort:
    """Structural HarnessPort recording the ChatSpawnRequest the route hands it.
    `accepted` scripts the returned SpawnHandle; spawn_run/cancel_run exist so it
    satisfies the whole port surface (the route only calls spawn_chat)."""

    def __init__(self, *, accepted=True):
        self.accepted = accepted
        self.chat_requests: list = []

    async def spawn_chat(self, request):
        self.chat_requests.append(request)
        from app.domain.harness import SpawnHandle
        return SpawnHandle(run_id=request.run_id, accepted=self.accepted,
                           exit_code=None,
                           error=None if self.accepted else "service down")

    async def spawn_run(self, request):  # pragma: no cover - not used by chat
        from app.domain.harness import SpawnHandle
        return SpawnHandle(run_id=request.run_id, accepted=True, exit_code=0)

    async def cancel_run(self, run_id):  # pragma: no cover - not used by chat
        return False


@pytest.mark.asyncio
async def test_chat_remote_mode_calls_spawn_chat_not_stream_chat(monkeypatch):
    """HARNESS_SPAWN_MODE=remote + a harness_port → the route calls
    harness_port.spawn_chat with a ChatSpawnRequest carrying (project, agent, message)
    + the pre-created run_id, and does NOT run stream_chat in-process."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    # If the in-process path were taken, this would record a call — assert it is NOT.
    stream_calls: list = []

    async def _spy_stream(msg, *, model=None, system=None, harness=None, **kw):
        stream_calls.append(msg)
        if False:  # pragma: no cover - generator shape only; never yields here
            yield {}
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    port = FakeHarnessPort(accepted=True)
    resp = await main_mod.agent_chat(_Req(store, harness_port=port), "kaidera-os", "kai")
    frames = await _drain(resp)

    # 1. The remote seam was used: spawn_chat called once with the chat fields.
    assert len(port.chat_requests) == 1, "remote mode must call harness_port.spawn_chat"
    req = port.chat_requests[0]
    assert req.project == "kaidera-os"
    assert req.agent == "kai"
    assert req.message == "hello there"
    # The pre-created run row's id flows into the ChatSpawnRequest.
    assert store.started, "remote mode still pre-creates the run_state row"
    assert req.run_id == store.started[0]["run_id"]
    assert store.started[0]["lease_owner"] == "chat"
    assert store.started[0]["handoff_id"] is None

    # 2. The in-process harness stream was NOT invoked.
    assert stream_calls == [], "remote mode must NOT run stream_chat in-process"

    # 3. The run_id is surfaced so the /runstate/stream pane can follow the reply.
    run_frames = [f for f in frames if f.get("event") == "run"]
    assert run_frames
    assert json.loads(run_frames[0]["data"]).get("run_id") == req.run_id
    # A done frame closes the SSE (the reply itself arrives via /runstate/stream).
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_chat_remote_mode_rejected_spawn_sets_error_and_still_closes(monkeypatch):
    """If the host service rejects the chat spawn (accepted=False), the route marks the
    run errored + emits an error frame, and never raises (graceful-degrade)."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    port = FakeHarnessPort(accepted=False)
    resp = await main_mod.agent_chat(_Req(store, harness_port=port), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert len(port.chat_requests) == 1
    # A rejected spawn → terminal error status on the pre-created run.
    statuses = [s["status"] for s in store.statuses]
    assert statuses and statuses[-1] == "error"
    assert any(f.get("event") == "error" for f in frames)
    assert any(f.get("event") == "done" for f in frames)


@pytest.mark.asyncio
async def test_chat_remote_flag_but_no_port_uses_in_process(monkeypatch):
    """Flag set to remote but NO harness_port wired (factory failed-closed to None) →
    the route falls back to the in-process stream_chat path (no spawn_chat to call)."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    # harness_port=None (default) → in-process path; the reply streams as before.
    resp = await main_mod.agent_chat(_Req(store, harness_port=None), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)
    # In-process path → terminal ok on the store.
    assert [s["status"] for s in store.statuses][-1] == "ok"


@pytest.mark.asyncio
async def test_chat_flag_unset_with_port_still_in_process(monkeypatch):
    """A harness_port wired but HARNESS_SPAWN_MODE unset → the route takes the
    in-process path (the flag, not the mere presence of a port, gates the remote
    seam). spawn_chat is NOT called."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.delenv("HARNESS_SPAWN_MODE", raising=False)

    port = FakeHarnessPort(accepted=True)
    resp = await main_mod.agent_chat(_Req(store, harness_port=port), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert port.chat_requests == [], "flag unset → must NOT call spawn_chat"
    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)


# ---------------------------------------------------------------------------
#  Multi-turn chat (feature-gap step 6, Inc B) — the route reads `session_id` from the
#  posted form and threads it: it persists session_id on the run row, writes the user
#  message as an `input` span, threads prior turns into the prompt (in-process), AND
#  carries session_id into the ChatSpawnRequest (remote). With NO session_id the route
#  is byte-for-byte the single-shot path (every test above proves it).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_route_reads_session_id_and_writes_input_span(monkeypatch):
    """The posted form's session_id lands on start_run AND the user message is stored
    as an `input` span (so a later turn in this session can rebuild it)."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex, form={"message": "what is 2+2?", "session_id": "sess-9"})

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    # 1. session_id persisted on the run row.
    assert store.started[0]["session_id"] == "sess-9"

    # 2. The user message is stored as an `input` span (kind='input'), written first.
    input_spans = [s for s in store.spans if s["kind"] == "input"]
    assert input_spans, "the user message must be stored as an 'input' span"
    assert input_spans[0]["text"] == "what is 2+2?"
    assert input_spans[0]["seq"] == 1, "the input span is written before the reply"
    # 3. The reply output spans still follow.
    assert [s for s in store.spans if s["kind"] == "output"], "reply spans still written"


@pytest.mark.asyncio
async def test_chat_route_threads_prior_turns_into_prompt(monkeypatch):
    """When the session has prior turns, the in-process route enriches the prompt with a
    [Previous conversation] block before calling stream_chat."""
    from app.domain.runstate import RunRecord, RunSpan

    cortex = FakeCortex()

    class _HistStore(FakeStore):
        async def recent(self, project=None, limit=20, *, session_id=None):
            return [RunRecord(
                run_id="prev", project="kaidera-os", agent="kai", lease_owner="chat",
                session_id="sess-9", status="ok",
                spans=[RunSpan(seq=1, kind="input", text="my name is Amad"),
                       RunSpan(seq=2, kind="output", text="Hello Amad.")],
            )]

    store = _HistStore()
    _install_common(monkeypatch, cortex, form={"message": "what is my name?", "session_id": "sess-9"})

    # Spy stream_chat to capture the (possibly enriched) prompt it receives.
    seen: dict = {}

    async def _spy_stream(msg, *, model=None, system=None, harness=None, reasoning=None, **kw):
        seen["prompt"] = msg
        for ev in _events_ok():
            yield ev
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    assert "[Previous conversation]" in seen["prompt"]
    assert "my name is Amad" in seen["prompt"] and "Hello Amad." in seen["prompt"]
    assert seen["prompt"].rstrip().endswith("what is my name?")


@pytest.mark.asyncio
async def test_chat_route_no_session_id_is_single_shot(monkeypatch):
    """No session_id in the form → session_id=None on the row AND the prompt reaches
    stream_chat UNCHANGED (single-shot — today's behaviour preserved)."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)  # default form: {"message": "hello there"}

    seen: dict = {}

    async def _spy_stream(msg, *, model=None, system=None, harness=None, reasoning=None, **kw):
        seen["prompt"] = msg
        for ev in _events_ok():
            yield ev
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    assert store.started[0]["session_id"] is None
    assert seen["prompt"] == "hello there", "no session → prompt unchanged (single-shot)"


@pytest.mark.asyncio
async def test_chat_remote_mode_carries_session_id_into_spawn_request(monkeypatch):
    """In REMOTE mode the route carries session_id into the ChatSpawnRequest, so the
    host chat runner threads the conversation (and writes the input span host-side)."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex, form={"message": "hello there", "session_id": "sess-remote"})
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    port = FakeHarnessPort(accepted=True)
    resp = await main_mod.agent_chat(_Req(store, harness_port=port), "kaidera-os", "kai")
    await _drain(resp)

    assert len(port.chat_requests) == 1
    req = port.chat_requests[0]
    assert getattr(req, "session_id", None) == "sess-remote", (
        "the ChatSpawnRequest must carry session_id so the host runner threads it"
    )
    # The run row still carries it too.
    assert store.started[0]["session_id"] == "sess-remote"


# ---------------------------------------------------------------------------
#  Never-forget LTM write (step 5) — the IN-PROCESS chat route persists a COMPLETED
#  turn to Cortex LTM (the same gap the detached chat_run.chat_one closes). The route
#  calls chat_ltm.persist_chat_turn with the cli_log writer, carrying the agent, the
#  run_id, the user message, and the reply. We monkeypatch persist_chat_turn to capture
#  the call (and, in the degrade test, to raise) — no real cortex-log subprocess runs.
# ---------------------------------------------------------------------------

def _capture_persist(monkeypatch):
    """Patch `main.chat_ltm_module.persist_chat_turn` to record its call; returns the
    capture list. The route should pass (log_fn, agent, run_id, message, reply, project)."""
    seen: list[dict] = []

    async def _fake_persist(log_fn, agent, run_id, message, reply, project=None, **kw):
        seen.append({
            "log_fn": log_fn, "agent": agent, "run_id": run_id,
            "message": message, "reply": reply, "project": project,
        })

    monkeypatch.setattr(main_mod.chat_ltm_module, "persist_chat_turn", _fake_persist)
    return seen


@pytest.mark.asyncio
async def test_chat_inprocess_persists_completed_turn_to_ltm(monkeypatch):
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    seen = _capture_persist(monkeypatch)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)

    # The completed turn was persisted to Cortex LTM exactly once.
    assert len(seen) == 1, "a completed in-process chat turn must persist to Cortex LTM"
    call = seen[0]
    # The writer is the non-blocking cli_log bound to this project's workspace.
    assert getattr(call["log_fn"], "func", None) is main_mod.chat_ltm_module.cli_log
    assert "workspace" in call["log_fn"].keywords
    assert "kai" in (call["agent"] or ""), "the agent identity is carried into the LTM write"
    assert call["message"] == "hello there", "the user message must be persisted"
    assert call["reply"] and "All done." in call["reply"], "the reply must be persisted"
    # The run_id matches the pre-created store row (so memory ties to the same run).
    assert call["run_id"] == store.started[0]["run_id"]
    assert call["project"] == "kaidera-os"


@pytest.mark.asyncio
async def test_chat_inprocess_ltm_failure_does_not_break_chat(monkeypatch):
    """GRACEFUL-DEGRADE: a raising LTM write must NOT break the chat — the reply still
    streams and the run-state still reaches terminal ok."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)

    async def _boom(*a, **k):
        raise RuntimeError("cortex down")
    monkeypatch.setattr(main_mod.chat_ltm_module, "persist_chat_turn", _boom)

    # Must NOT raise.
    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    frames = await _drain(resp)

    assert not [f for f in frames if f.get("event") in ("delta", "result")]
    assert any(f.get("event") == "done" for f in frames)
    # The run-state SSOT write is unaffected — terminal ok still landed.
    assert [s["status"] for s in store.statuses][-1] == "ok"


@pytest.mark.asyncio
async def test_chat_inprocess_no_ltm_write_on_harness_error(monkeypatch):
    """An in-process harness-ERROR turn writes NO LTM entry (no reply worth remembering)
    — only completed turns persist."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex, events=[
        {"type": "error", "message": "model not available", "category": "model_unavailable"},
        {"type": "done"},
    ])
    seen = _capture_persist(monkeypatch)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    assert seen == [], "a harness-error turn must not write a chat LTM entry"


@pytest.mark.asyncio
async def test_chat_remote_mode_does_not_write_ltm_in_process(monkeypatch):
    """In REMOTE mode the host chat runner owns the LTM write (chat_run.chat_one) — the
    in-process route must NOT also persist (no double-write); it returns before the
    in-process completion path."""
    cortex = FakeCortex()
    store = FakeStore()
    _install_common(monkeypatch, cortex)
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    seen = _capture_persist(monkeypatch)

    port = FakeHarnessPort(accepted=True)
    resp = await main_mod.agent_chat(_Req(store, harness_port=port), "kaidera-os", "kai")
    await _drain(resp)

    assert seen == [], "remote mode must not write LTM in-process (the host runner does)"
