"""Harness-service Increment 4 — the HOST-runnable chat runner (`app/chat_run.py`).

The interactive chat route (`POST /agents/{p}/{a}/chat`, `agent_chat`) is MODEL (a):
it pre-creates a `run_id`, writes spans + status to the RunState SSOT store, and the
UI reads the reply via `GET /runstate/stream`. The console-image cutover needs that
SAME "run one chat turn" to be runnable on the HOST (which has the harness CLIs +
their OAuth login), so the container never needs the CLI — exactly analogous to
`run_agent.run_one` for autonomous workers.

`chat_run.chat_one(...)` is the reusable core: GIVEN a `(project, agent, message)` and
a pre-created `run_id`, it drives `runner.stream_chat(...)` and writes the SAME
run-state cycle the in-process route writes — MINUS the handoff lifecycle (a chat has
no handoff: nothing to claim/complete; `lease_owner='chat'`, `handoff_id=None`):
  * `start_run(run_id, handoff_id=None, lease_owner='chat')` opens the row,
  * `set_status(run_id,'running')` at start,
  * `append_output(run_id, seq, kind, text)` per streamed delta/result span,
  * on done → `set_status(run_id,'ok', tokens…/cost…)`; on a harness error →
    `set_status(run_id,'error', error=…)`.

GRACEFUL-DEGRADE (house law): every store call is best-effort — a None / raising store
must NOT crash the run (the reply is still produced; only the durable run-state is
lost). This mirrors `run_agent.run_one`'s store discipline.

These run against the FakeRunner (a scripted `stream_chat`) + a fake RunStatePort — no
live CLI, no DB, no network. The runner writes to the app-DB DIRECTLY (like the worker),
so it is the HOST half of the bridge; the container's chat route POSTs to it via the
host harness-service (`POST /chat`).
"""
from __future__ import annotations

import asyncio

import pytest

import app.chat_run as cr
from tests.conftest import FakeRunner


class FakeChatRunState:
    """Records the RunStatePort calls chat_one makes (structural RunStatePort).

    `raising=True` forces every method to raise so the graceful-degrade path is
    exercised (a down store must never crash the chat run)."""

    def __init__(self, *, raising: bool = False) -> None:
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
            "agent_display": agent_display, "handoff_id": handoff_id,
            "lease_owner": lease_owner, "harness": harness, "model": model,
            "session_id": session_id,
        })
        return type("Rec", (), {"run_id": run_id})()

    # Multi-turn chat (Inc B): the reader path the chat threads history through. By
    # default there are no prior turns (empty session), so chat stays single-shot.
    async def recent(self, project=None, limit=20, *, session_id=None):
        if self.raising:
            raise RuntimeError("store down")
        return []

    async def get_run(self, run_id):
        if self.raising:
            raise RuntimeError("store down")
        return None

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


def _events_ok():
    return [
        {"type": "delta", "text": "Thinking"},
        {"type": "delta", "text": " about it."},
        {"type": "result", "text": "All done.", "tokens_in": 9, "tokens_out": 4,
         "cost_usd": 0.002},
        {"type": "done"},
    ]


# ---------------------------------------------------------------------------
#  Happy path: open row (chat lease, NO handoff) → running → spans → ok.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_writes_running_spans_ok_under_run_id():
    runner = FakeRunner(_events_ok())
    store = FakeChatRunState()

    result = await cr.chat_one(
        "kai", "hello there", "kaidera-os",
        run_id="rid-123", runner=runner, runstate=store,
        harness="claude-code", model="opus",
    )

    # 1. The row was opened ONCE with the CHAT lease + NO handoff, on the given run_id.
    assert len(store.started) == 1, "chat_one must open exactly one run_state row"
    started = store.started[0]
    assert started["run_id"] == "rid-123"
    assert started["lease_owner"] == "chat", "interactive chat rows use lease_owner='chat'"
    assert started["handoff_id"] is None, "a chat has NO handoff to attach"
    assert started["project"] == "kaidera-os"
    assert started["agent"] == "kai"

    # 2. status walked running → ok, all on the SAME run_id.
    statuses = [(s["run_id"], s["status"]) for s in store.statuses]
    assert statuses[0] == ("rid-123", "running"), "first status must be 'running'"
    assert statuses[-1] == ("rid-123", "ok"), "a clean chat run ends 'ok'"
    assert all(rid == "rid-123" for rid, _ in statuses)
    # terminal telemetry lands on the run HEADER via heartbeat (the adapter contract —
    # set_status carries NO tokens); the status row itself is a clean 'ok'.
    totals = [h for h in store.heartbeats
              if h.get("tokens_in") is not None or h.get("cost_est_usd") is not None]
    assert totals, "expected a final heartbeat carrying the run's token/cost totals"
    assert totals[-1]["tokens_in"] == 9 and totals[-1]["tokens_out"] == 4
    assert totals[-1]["cost_est_usd"] == 0.002

    # 3. spans captured the streamed text, on the SAME run_id, strictly increasing seq.
    assert store.spans, "chat output must be appended to the store as spans"
    assert all(sp["run_id"] == "rid-123" for sp in store.spans)
    seqs = [sp["seq"] for sp in store.spans]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq unique + ordered"
    joined = "".join(sp["text"] for sp in store.spans)
    assert "Thinking" in joined and "All done." in joined

    # 4. The runner was driven with the message + routing (forwarded, not dropped).
    assert runner.last_call["message"] == "hello there"
    assert runner.last_call["harness"] == "claude-code"
    assert runner.last_call["model"] == "opus"
    assert runner.last_call["run_context"] == "chat"

    # 5. The result reports the completed status + assembled reply text.
    assert result.status == "ok"
    assert "All done." in result.text


# ---------------------------------------------------------------------------
#  Result ECHO de-dup: a streaming harness (claude-code/pi) emits the full
#  reply as deltas AND echoes it in the terminal `result`. The reply must land
#  ONCE — appending the echo would surface "HEAL OKHEAL OK". (Regression.)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_dedups_result_echo_of_streamed_reply():
    echo_events = [
        {"type": "delta", "text": "HEAL"},
        {"type": "delta", "text": " OK"},
        # the harness echoes the SAME full reply in its terminal result frame:
        {"type": "result", "text": "HEAL OK", "tokens_in": 3, "tokens_out": 2,
         "cost_usd": 0.001},
        {"type": "done"},
    ]
    runner = FakeRunner(echo_events)
    store = FakeChatRunState()

    result = await cr.chat_one(
        "kai", "heal please", "kaidera-os",
        run_id="rid-echo", runner=runner, runstate=store,
        harness="claude-code", model="opus",
    )

    out = "".join(sp["text"] for sp in store.spans if sp["kind"] == "output")
    assert out == "HEAL OK", f"echo must NOT double the reply, got {out!r}"
    assert result.text == "HEAL OK", f"assembled reply must be single, got {result.text!r}"


# ---------------------------------------------------------------------------
#  Result-only harness: NO deltas, just a terminal result → its text IS captured
#  (the de-dup guard must not drop the only copy of the reply).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_captures_result_only_reply():
    result_only = [
        {"type": "result", "text": "Just the answer.", "tokens_in": 5, "tokens_out": 3,
         "cost_usd": 0.001},
        {"type": "done"},
    ]
    runner = FakeRunner(result_only)
    store = FakeChatRunState()

    result = await cr.chat_one(
        "kai", "no streaming", "kaidera-os",
        run_id="rid-resultonly", runner=runner, runstate=store,
        harness="claude-code", model="opus",
    )

    out = "".join(sp["text"] for sp in store.spans if sp["kind"] == "output")
    assert out == "Just the answer.", f"result-only text must be captured, got {out!r}"
    assert result.text == "Just the answer."


@pytest.mark.asyncio
async def test_chat_one_records_visibility_spans_from_harness_events():
    """Thinking/tool/tasks/sub-agent events are durable run-state spans, not UI-only."""
    runner = FakeRunner([
        {"type": "thinking", "text": "checking requirements"},
        {"type": "tool", "name": "run_bash", "text": "run_bash: ls"},
        {"type": "tasks", "items": [{"content": "Inspect", "status": "completed"}]},
        {"type": "subagent", "label": "researcher"},
        {"type": "delta", "text": "Visible answer."},
        {"type": "result", "text": "Visible answer.", "tokens_in": 1, "tokens_out": 1},
        {"type": "done"},
    ])
    store = FakeChatRunState()

    result = await cr.chat_one(
        "kai", "show all activity", "kaidera-os",
        run_id="rid-visible", runner=runner, runstate=store,
    )

    assert result.status == "ok"
    spans = [(s["kind"], s["text"]) for s in store.spans]
    assert ("thinking", "checking requirements") in spans
    assert ("tool", "run_bash: ls") in spans
    assert any(kind == "tasks" and "Inspect" in text for kind, text in spans)
    assert ("subagent", "researcher") in spans
    assert ("output", "Visible answer.") in spans


# ---------------------------------------------------------------------------
#  Run error: a harness error → terminal status 'error' with the message.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_run_error_sets_error_status():
    runner = FakeRunner([
        {"type": "delta", "text": "partial"},
        {"type": "error", "message": "model not available", "category": "model_unavailable"},
        {"type": "done"},
    ])
    store = FakeChatRunState()

    result = await cr.chat_one(
        "kai", "hi", "kaidera-os", run_id="rid-err", runner=runner, runstate=store,
    )

    statuses = [s["status"] for s in store.statuses]
    assert "running" in statuses
    assert statuses[-1] == "error", "a harness error must set terminal status 'error'"
    err = [s for s in store.statuses if s["status"] == "error"][0]
    assert err["error"] and "model not available" in err["error"]
    assert result.status == "error"


@pytest.mark.asyncio
async def test_chat_one_cancelled_marks_operator_cancel_not_disconnect():
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingRunner:
        async def stream_chat(self, message, **kwargs):
            started.set()
            await release.wait()
            yield {"type": "done"}

    store = FakeChatRunState()
    task = asyncio.create_task(
        cr.chat_one(
            "kai",
            "hi",
            "kaidera-os",
            run_id="rid-cancel",
            runner=BlockingRunner(),
            runstate=store,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    err = [s for s in store.statuses if s["status"] == "error"][-1]
    assert "operator" in (err["error"] or "")
    assert "disconnect" not in (err["error"] or "").lower()


# ---------------------------------------------------------------------------
#  NO handoff lifecycle: chat_one never claims or completes anything. It now takes
#  an OPTIONAL cortex collaborator, used ONLY for the never-forget LTM write (step 5)
#  — never to claim/complete (a chat has no handoff).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_takes_optional_cortex_and_never_claims():
    """chat_one carries NO `handoff_id` (a chat is free-standing — nothing to
    claim/complete), and its `cortex` collaborator is OPTIONAL (defaults to None) and is
    used ONLY for the never-forget LTM write, never the handoff lifecycle. Proven
    structurally: the signature has `cortex` defaulting to None and no `handoff_id`."""
    import inspect

    sig = inspect.signature(cr.chat_one)
    params = sig.parameters
    assert "handoff_id" not in params, "chat_one must NOT take a handoff_id"
    assert {"message", "run_id", "runner", "runstate"} <= set(params)
    assert "cortex" in params, "chat_one takes an optional cortex (for the LTM write)"
    assert params["cortex"].default is None, "cortex must be OPTIONAL (default None)"


# ---------------------------------------------------------------------------
#  Never-forget LTM write (step 5): a COMPLETED chat turn persists to Cortex LTM via
#  the cortex collaborator's `.log` (the SAME mechanism the autonomous worker uses) —
#  carrying the agent, the run_id, the user message, and the reply.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_persists_completed_turn_to_cortex_ltm():
    from tests.conftest import FakeCortex

    runner = FakeRunner(_events_ok())
    store = FakeChatRunState()
    cortex = FakeCortex()  # records .log(agent, event_type, summary, project) calls

    result = await cr.chat_one(
        "kai", "what is 2+2?", "kaidera-os",
        run_id="rid-ltm", runner=runner, runstate=store, cortex=cortex,
    )
    assert result.status == "ok"

    # Exactly one LTM log for this (short) turn, on the 'log' channel (NOT claim/complete).
    log_calls = [c for c in cortex.calls if c[0] == "log"]
    assert len(log_calls) == 1, "a completed chat turn writes one Cortex LTM entry"
    payload = log_calls[0][1]
    assert payload["event_type"] == "decision", (
        "the chat transcript is a 'decision' event, like the autonomous TRANSCRIPT write"
    )
    assert payload["agent"] == "kai"
    summary = payload["summary"]
    assert "CHAT" in summary and "rid-ltm" in summary
    assert "what is 2+2?" in summary, "the user message must be persisted"
    assert "All done." in summary, "the agent reply must be persisted"

    # CRITICAL: chat NEVER claims/completes — the LTM write is the ONLY cortex call.
    assert all(c[0] == "log" for c in cortex.calls), (
        "interactive chat must NOT claim or complete any handoff — LTM write only"
    )


@pytest.mark.asyncio
async def test_chat_one_does_not_persist_ltm_on_error_turn():
    """Only COMPLETED (ok) turns are persisted — a harness-error turn writes NO LTM
    entry (we don't pollute memory with a failed exchange that produced no reply)."""
    from tests.conftest import FakeCortex

    runner = FakeRunner([
        {"type": "error", "message": "model not available", "category": "model_unavailable"},
        {"type": "done"},
    ])
    cortex = FakeCortex()

    result = await cr.chat_one(
        "kai", "hi", "kaidera-os", run_id="rid-err-ltm",
        runner=runner, runstate=FakeChatRunState(), cortex=cortex,
    )
    assert result.status == "error"
    assert not [c for c in cortex.calls if c[0] == "log"], (
        "an error turn must not write a chat LTM entry"
    )


@pytest.mark.asyncio
async def test_chat_one_survives_raising_cortex_log():
    """GRACEFUL-DEGRADE: a Cortex-down (the .log collaborator raises) must NOT break the
    chat — the reply is still produced and returned (the run-state write is unaffected)."""
    class RaisingCortex:
        async def log(self, agent, event_type, summary, project=None):
            raise RuntimeError("cortex down")

    runner = FakeRunner(_events_ok())
    result = await cr.chat_one(
        "kai", "hi", "kaidera-os", run_id="rid-raise-ltm",
        runner=runner, runstate=FakeChatRunState(), cortex=RaisingCortex(),
    )
    assert result.status == "ok", "a down Cortex must not change the chat outcome"
    assert "All done." in result.text


@pytest.mark.asyncio
async def test_chat_one_no_cortex_is_a_clean_noop():
    """No cortex collaborator (the default) → no LTM write, no crash — the chat still
    runs (back-compat: the run-state store path is unchanged)."""
    runner = FakeRunner(_events_ok())
    result = await cr.chat_one(
        "kai", "hi", "kaidera-os", run_id="rid-no-cortex",
        runner=runner, runstate=FakeChatRunState(),  # no cortex=
    )
    assert result.status == "ok"
    assert "All done." in result.text


# ---------------------------------------------------------------------------
#  Multi-turn chat (feature-gap step 6, Inc B): chat_one persists session_id on the
#  run row AND writes the USER MESSAGE as an `input` span BEFORE streaming, so a later
#  turn in the same session can rebuild the conversation. It ALSO threads prior turns
#  into the prompt via load_session_history + compose_contextual_prompt.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_writes_session_id_and_input_span_first():
    runner = FakeRunner(_events_ok())
    store = FakeChatRunState()

    await cr.chat_one(
        "kai", "what is 2+2?", "kaidera-os",
        run_id="rid-sess", runner=runner, runstate=store,
        harness="claude-code", model="opus", session_id="sess-7",
    )

    # 1. The run row carries the session_id (the conversation grouping key).
    assert store.started[0]["session_id"] == "sess-7"

    # 2. The FIRST span is the user message as kind='input' (so a later turn can read
    #    it back as the user side of this turn).
    assert store.spans, "chat_one must write spans"
    first = store.spans[0]
    assert first["kind"] == "input", "the user message is stored as an 'input' span"
    assert first["text"] == "what is 2+2?"
    assert first["seq"] == 1, "the input span is written BEFORE the reply (seq 1)"

    # 3. The reply output spans follow (seq strictly increasing, kind='output').
    out_spans = [s for s in store.spans if s["kind"] == "output"]
    assert out_spans, "reply output spans still written after the input span"
    assert all(s["seq"] > 1 for s in out_spans)


@pytest.mark.asyncio
async def test_chat_one_threads_prior_turns_into_prompt(monkeypatch):
    """When the session has prior turns, chat_one enriches the prompt with a
    [Previous conversation] block before calling stream_chat (so the harness — which
    treats each call as a new session — sees the conversation)."""
    runner = FakeRunner(_events_ok())

    # A store whose history reader returns one prior turn for this session.
    class _HistStore(FakeChatRunState):
        async def recent(self, project=None, limit=20, *, session_id=None):
            from app.domain.runstate import RunRecord, RunSpan
            return [RunRecord(
                run_id="prev", project="kaidera-os", agent="kai", lease_owner="chat",
                session_id="sess-7", status="ok",
                spans=[RunSpan(seq=1, kind="input", text="my name is Amad"),
                       RunSpan(seq=2, kind="output", text="Nice to meet you, Amad.")],
            )]

    store = _HistStore()
    await cr.chat_one(
        "kai", "what is my name?", "kaidera-os",
        run_id="rid-hist", runner=runner, runstate=store, session_id="sess-7",
    )

    sent = runner.last_call["message"]
    assert "[Previous conversation]" in sent, "prior turns must be threaded into the prompt"
    assert "my name is Amad" in sent and "Nice to meet you, Amad." in sent
    assert "[Current message]" in sent
    assert sent.rstrip().endswith("what is my name?"), "current message is last"


@pytest.mark.asyncio
async def test_chat_one_no_session_is_single_shot_prompt():
    """No session_id → no history → the prompt reaches stream_chat UNCHANGED (the
    single-shot path; the input span is still stored for THIS turn but no prior context
    is prepended)."""
    runner = FakeRunner(_events_ok())
    store = FakeChatRunState()
    await cr.chat_one(
        "kai", "standalone question", "kaidera-os",
        run_id="rid-solo", runner=runner, runstate=store,  # no session_id
    )
    assert runner.last_call["message"] == "standalone question", (
        "with no session the prompt is unchanged (single-shot)"
    )
    assert store.started[0]["session_id"] is None


def test_chat_one_signature_has_session_id():
    """chat_one accepts an OPTIONAL session_id (default None) — additive."""
    import inspect
    p = inspect.signature(cr.chat_one).parameters
    assert "session_id" in p and p["session_id"].default is None


def test_main_reads_session_id_argv(monkeypatch):
    """`run-chat` accepts an optional `--session-id <id>` argv flag and passes it to
    chat_one (so the host chat runner threads the conversation)."""
    seen: dict = {}

    async def _fake_chat_one(name, message, project, *, run_id, runner, runstate,
                             harness=None, model=None, reasoning=None, system=None,
                             cortex=None, session_id=None, attachment_paths=None):
        seen.update({"message": message, "run_id": run_id, "session_id": session_id})
        return cr.ChatResult(status="ok", text="ok")

    monkeypatch.setattr(cr, "chat_one", _fake_chat_one)
    monkeypatch.setattr(cr, "_build_runner_and_routing", lambda: (object(), lambda a, p: ("claude-code", "opus", "max")))
    monkeypatch.setattr(cr, "_build_runstate", lambda: None)
    monkeypatch.setattr(cr, "_build_cortex", lambda project: None)
    monkeypatch.setattr(cr, "_load_chat_system", lambda name, project: "sys")

    rc = cr.main(["kai", "kaidera-os", "rid-cli", "--session-id", "sess-cli", "hello", "world"])
    assert rc == 0
    assert seen["session_id"] == "sess-cli"
    # The message is still the remaining argv (the flag + its value are consumed).
    assert seen["message"] == "hello world"
    assert seen["run_id"] == "rid-cli"


def test_main_without_session_id_argv_passes_none(monkeypatch):
    """No `--session-id` flag → session_id=None (single-shot; back-compat with the
    existing 4-arg argv contract)."""
    seen: dict = {}

    async def _fake_chat_one(name, message, project, *, run_id, runner, runstate,
                             harness=None, model=None, reasoning=None, system=None,
                             cortex=None, session_id=None, attachment_paths=None):
        seen.update({"message": message, "session_id": session_id})
        return cr.ChatResult(status="ok", text="ok")

    monkeypatch.setattr(cr, "chat_one", _fake_chat_one)
    monkeypatch.setattr(cr, "_build_runner_and_routing", lambda: (object(), lambda a, p: ("claude-code", "opus", "max")))
    monkeypatch.setattr(cr, "_build_runstate", lambda: None)
    monkeypatch.setattr(cr, "_build_cortex", lambda project: None)
    monkeypatch.setattr(cr, "_load_chat_system", lambda name, project: "sys")

    rc = cr.main(["kai", "kaidera-os", "rid-cli", "plain message"])
    assert rc == 0
    assert seen["session_id"] is None
    assert seen["message"] == "plain message"


# ---------------------------------------------------------------------------
#  Graceful-degrade: a RAISING store never crashes the run; the reply is still made.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_survives_raising_store():
    runner = FakeRunner(_events_ok())
    store = FakeChatRunState(raising=True)  # every store call raises

    # Must NOT raise — the runner swallows store failures.
    result = await cr.chat_one(
        "kai", "hi", "kaidera-os", run_id="rid-raise", runner=runner, runstate=store,
    )
    assert result.status == "ok", "a down store must not change the run outcome"
    assert "All done." in result.text


@pytest.mark.asyncio
async def test_chat_one_survives_none_store():
    """A None run-state store (store failed to construct / app-DB down) must not crash
    the chat run — the reply is still produced."""
    runner = FakeRunner(_events_ok())

    result = await cr.chat_one(
        "kai", "hi", "kaidera-os", run_id="rid-none", runner=runner, runstate=None,
    )
    assert result.status == "ok"
    assert "All done." in result.text


# ---------------------------------------------------------------------------
#  The CLI entry: argv → asyncio.run(chat_one(...)); usage on too-few args.
# ---------------------------------------------------------------------------

def test_main_usage_on_too_few_args(capsys):
    """`run-chat <agent> <project> <run_id> <message>` — too few args → a non-zero
    usage exit (mirrors run_agent.main)."""
    rc = cr.main(["kai", "kaidera-os"])  # missing run_id + message
    assert rc != 0
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_main_drives_chat_one(monkeypatch):
    """`main` parses argv and drives an async chat run, returning 0 on a clean run.
    We stub the async core + the collaborator builders so no CLI / DB is touched."""
    seen: dict = {}

    async def _fake_chat_one(name, message, project, *, run_id, runner, runstate,
                             harness=None, model=None, reasoning=None, system=None,
                             cortex=None, session_id=None, attachment_paths=None):
        seen.update({"name": name, "message": message, "project": project,
                     "run_id": run_id, "cortex_is_set": cortex is not None})
        return cr.ChatResult(status="ok", text="ok")

    sentinel_cortex = object()
    monkeypatch.setattr(cr, "chat_one", _fake_chat_one)
    monkeypatch.setattr(cr, "_build_runner_and_routing", lambda: (object(), lambda a, p: ("claude-code", "opus", "max")))
    monkeypatch.setattr(cr, "_build_runstate", lambda: None)
    # The CLI builds a Cortex collaborator for the never-forget LTM write (step 5).
    monkeypatch.setattr(cr, "_build_cortex", lambda project: sentinel_cortex)
    # Avoid the real cortex-boot / identity load in the CLI bootstrap.
    monkeypatch.setattr(cr, "_load_chat_system", lambda name, project: "sys")

    rc = cr.main(["kai", "kaidera-os", "rid-cli", "hello", "world"])
    assert rc == 0
    assert seen["name"] == "kai"
    assert seen["project"] == "kaidera-os"
    assert seen["run_id"] == "rid-cli"
    # The message is the remaining argv joined (so multi-word messages work).
    assert seen["message"] == "hello world"
    # The CLI passed the built Cortex collaborator through to chat_one (LTM write path).
    assert seen["cortex_is_set"] is True


# ---------------------------------------------------------------------------
#  CHAT FILE-ATTACHMENTS (feature-gap step 6, Inc A) — chat_one weaves the host
#  attachment files into the prompt, writes an `attachment` span, and the CLI
#  parses `--attachment-paths a,b` into the list it forwards.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_one_inlines_attachment_paths_into_prompt(tmp_path):
    """chat_one(attachment_paths=[...]) inlines each readable file into the message the
    runner sees, and writes an `attachment` span per file."""
    f = tmp_path / "spec.txt"
    f.write_text("the spec body")
    runner = FakeRunner(_events_ok())
    store = FakeChatRunState()

    result = await cr.chat_one(
        "kai", "review the spec", "kaidera-os",
        run_id="rid-att", runner=runner, runstate=store,
        harness="claude-code", model="opus",
        attachment_paths=[str(f)],
    )
    assert result.status == "ok"
    # The runner saw the inlined attachment block + the file content + the message.
    msg = runner.last_call["message"]
    assert "[Attached: spec.txt]" in msg
    assert "the spec body" in msg
    assert "review the spec" in msg
    # An `attachment` span was written (the filename → a transcript chip).
    att_spans = [s for s in store.spans if s["kind"] == "attachment"]
    assert any(s["text"] == "spec.txt" for s in att_spans)


@pytest.mark.asyncio
async def test_chat_one_surfaces_image_path_for_vision_capable_model(tmp_path):
    """Host chat runner: a vision-capable harness/model pair gets an image path block,
    not the fallback note."""
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    runner = FakeRunner(_events_ok())
    store = FakeChatRunState()

    result = await cr.chat_one(
        "kai", "look", "kaidera-os",
        run_id="rid-img-on", runner=runner, runstate=store,
        harness="pi", model="gpt-5.4",
        attachment_paths=[str(f)],
    )

    assert result.status == "ok"
    msg = runner.last_call["message"]
    assert "[Attached image: shot.png]" in msg
    assert "Vision-capable attachment path" in msg
    assert str(f) in msg
    assert "not readable" not in msg.lower()


@pytest.mark.asyncio
async def test_chat_one_keeps_image_fallback_for_text_only_model(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    runner = FakeRunner(_events_ok())
    store = FakeChatRunState()

    await cr.chat_one(
        "kai", "look", "kaidera-os",
        run_id="rid-img-off", runner=runner, runstate=store,
        harness="pi", model="gpt-5.3-codex-spark",
        attachment_paths=[str(f)],
    )

    msg = runner.last_call["message"]
    assert "shot.png" in msg
    assert "not readable" in msg.lower()
    assert "Vision-capable attachment path" not in msg


@pytest.mark.asyncio
async def test_chat_one_no_attachments_prompt_unchanged(tmp_path):
    """No attachment_paths → the prompt is byte-for-byte the message (back-compat)."""
    runner = FakeRunner(_events_ok())
    store = FakeChatRunState()
    await cr.chat_one(
        "kai", "plain ask", "kaidera-os",
        run_id="rid-plain", runner=runner, runstate=store,
        harness="claude-code", model="opus",
    )
    assert runner.last_call["message"] == "plain ask"
    assert all(s["kind"] != "attachment" for s in store.spans)


def test_chat_one_signature_has_attachment_paths():
    import inspect
    params = inspect.signature(cr.chat_one).parameters
    assert "attachment_paths" in params


def test_main_reads_attachment_paths_argv(monkeypatch):
    """`run-chat` accepts `--attachment-paths a,b` and passes the parsed list to
    chat_one (so the host chat runner inlines the host attachment files)."""
    seen: dict = {}

    async def _fake_chat_one(name, message, project, *, run_id, runner, runstate,
                             harness=None, model=None, reasoning=None, system=None,
                             cortex=None, session_id=None, attachment_paths=None):
        seen.update({
            "message": message, "run_id": run_id, "session_id": session_id,
            "attachment_paths": attachment_paths,
        })
        return cr.ChatResult(status="ok", text="ok")

    monkeypatch.setattr(cr, "chat_one", _fake_chat_one)
    monkeypatch.setattr(cr, "_build_runner_and_routing", lambda: (object(), lambda a, p: ("claude-code", "opus", "max")))
    monkeypatch.setattr(cr, "_build_runstate", lambda: None)
    monkeypatch.setattr(cr, "_build_cortex", lambda project: None)
    monkeypatch.setattr(cr, "_load_chat_system", lambda name, project: "sys")

    rc = cr.main([
        "kai", "kaidera-os", "rid-cli",
        "--attachment-paths", "/host/a.txt,/host/b.txt",
        "hello", "world",
    ])
    assert rc == 0
    assert seen["attachment_paths"] == ["/host/a.txt", "/host/b.txt"]
    assert seen["message"] == "hello world"
    assert seen["run_id"] == "rid-cli"


def test_main_without_attachment_paths_passes_none(monkeypatch):
    seen: dict = {}

    async def _fake_chat_one(name, message, project, *, run_id, runner, runstate,
                             harness=None, model=None, reasoning=None, system=None,
                             cortex=None, session_id=None, attachment_paths=None):
        seen["attachment_paths"] = attachment_paths
        return cr.ChatResult(status="ok", text="ok")

    monkeypatch.setattr(cr, "chat_one", _fake_chat_one)
    monkeypatch.setattr(cr, "_build_runner_and_routing", lambda: (object(), lambda a, p: ("claude-code", "opus", "max")))
    monkeypatch.setattr(cr, "_build_runstate", lambda: None)
    monkeypatch.setattr(cr, "_build_cortex", lambda project: None)
    monkeypatch.setattr(cr, "_load_chat_system", lambda name, project: "sys")

    rc = cr.main(["kai", "kaidera-os", "rid-cli", "just a message"])
    assert rc == 0
    assert seen["attachment_paths"] is None
