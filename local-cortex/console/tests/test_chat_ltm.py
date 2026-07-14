"""Never-forget memory for interactive chat — the shared chat→Cortex-LTM writer
(`app/chat_ltm.py`), feature-gap step 5 (bucket C #2; the long-standing
`project_ltm_chat_history_gap.md` gap).

The autonomous worker persists its transcript to Cortex LTM
(`run_agent.run_one` → `cortex.log(name, "decision", f"{name} TRANSCRIPT …")`, capped
8000 chars). Interactive chat did NOT — both `chat_run.chat_one` (the detached host
runner) and `agent_chat` (the in-process route) wrote ONLY to the app-DB run-state
store, so a conversation was lost if the app-DB was wiped and the agent had no durable
memory of it.

`chat_ltm.persist_chat_turn(...)` is the reusable core BOTH chat paths call after a
turn completes: it builds a clear `CHAT {run_id}` summary carrying the user message +
the reply, and writes it to Cortex LTM via the SAME mechanism the autonomous run uses
(a `cortex.log(agent, event_type, summary, project)` collaborator). NEVER-FORGET: it
persists the FULL turn — when a turn exceeds a generous per-entry budget it CHUNKS into
sequential `CHAT {run_id} [i/N]` log entries rather than silently truncating (the
autonomous path's 8000-char cap drops the tail; chat must not).

GRACEFUL-DEGRADE (house law): every write is best-effort — a Cortex-down / raising log
collaborator must NOT break the chat (the reply already returned; only the durable LTM
write is lost). Mirrors `run_agent`'s degrade.

These tests use a fake log collaborator (records calls; can be made to raise) — no live
CLI, no DB, no network.
"""
from __future__ import annotations

import pytest

import app.chat_ltm as cl


class FakeLog:
    """Records the `log(agent, event_type, summary, project)` calls a writer makes
    (the SAME interface `run_agent.run_one` calls — see `conftest.FakeCortex.log`).

    `raising=True` makes every call raise so the graceful-degrade path is exercised
    (a down Cortex must never crash the chat)."""

    def __init__(self, *, raising: bool = False) -> None:
        self.raising = raising
        self.calls: list[dict] = []

    async def __call__(self, agent, event_type, summary, project=None):
        if self.raising:
            raise RuntimeError("cortex down")
        self.calls.append(
            {"agent": agent, "event_type": event_type, "summary": summary, "project": project}
        )


# ---------------------------------------------------------------------------
#  build_chat_summaries — the pure summary/chunk builder.
# ---------------------------------------------------------------------------

def test_build_summary_single_entry_shape():
    """A short turn → ONE summary carrying the agent identity, the run_id, the user
    message, and the reply (so the agent's memory + cortex-search can recall it)."""
    out = cl.build_chat_summaries("kai:5872", "rid-1", "what is 2+2?", "It is 4.")
    assert len(out) == 1, "a short turn must be a single un-chunked entry"
    s = out[0]
    assert "kai:5872" in s, "the agent identity must be in the summary"
    assert "CHAT" in s, "the event marker must be 'CHAT' (the chat twin of TRANSCRIPT)"
    assert "rid-1" in s, "the run_id must be in the summary (recall the conversation)"
    assert "what is 2+2?" in s, "the user message must be persisted"
    assert "It is 4." in s, "the agent reply must be persisted"
    # A single entry carries NO [i/N] chunk marker.
    assert "[1/1]" not in s


def test_build_summary_full_turn_not_truncated_when_chunked():
    """NEVER-FORGET: a turn LARGER than the chunk budget is split into multiple
    sequential entries — the FULL content survives (no silent truncation, unlike the
    autonomous 8000-char cap)."""
    big_reply = "X" * (cl.CHAT_LTM_CHUNK_CHARS * 2 + 500)  # well over 2 chunks
    out = cl.build_chat_summaries("kai", "rid-big", "summarise", big_reply)

    assert len(out) >= 3, "a >2x-budget turn must chunk into 3+ entries"
    # Every chunk is itself within the budget (nothing oversized slips through).
    assert all(len(s) <= cl.CHAT_LTM_CHUNK_CHARS for s in out), (
        "each chunk must stay within the per-entry budget"
    )
    # Every chunk carries the run_id + an [i/N] marker so they can be reassembled.
    n = len(out)
    for i, s in enumerate(out, start=1):
        assert "rid-big" in s
        assert f"[{i}/{n}]" in s, "each chunk must carry its [i/N] position"
    # CRITICAL: the FULL reply is recoverable from the concatenated chunk payloads —
    # not one 'X' is dropped (the whole point of chunking vs truncating).
    assert sum(s.count("X") for s in out) == len(big_reply), (
        "chunking must preserve every character — never silently truncate"
    )


def test_build_summary_chunk_count_bounded_for_pathological_input():
    """A pathologically huge turn is still chunked (bounded loop, no infinite split);
    the builder returns a finite list and each entry holds real payload."""
    huge = "Y" * (cl.CHAT_LTM_CHUNK_CHARS * 5)
    out = cl.build_chat_summaries("ren", "rid-huge", "go", huge)
    assert 5 <= len(out) <= 8, "a 5x-budget reply chunks into a small bounded number of entries"
    assert sum(s.count("Y") for s in out) == len(huge)


# ---------------------------------------------------------------------------
#  persist_chat_turn — writes each chunk via the injected log collaborator.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_writes_one_log_for_a_short_turn():
    log = FakeLog()
    await cl.persist_chat_turn(log, "kai", "rid-2", "hi there", "hello back", "kaidera-os")

    assert len(log.calls) == 1, "a short turn writes exactly one LTM log entry"
    call = log.calls[0]
    assert call["agent"] == "kai"
    assert call["event_type"] == "decision", (
        "the chat transcript is a 'decision' event, matching the autonomous TRANSCRIPT write"
    )
    assert call["project"] == "kaidera-os"
    assert "hi there" in call["summary"] and "hello back" in call["summary"]
    assert "rid-2" in call["summary"] and "CHAT" in call["summary"]


@pytest.mark.asyncio
async def test_persist_writes_a_log_per_chunk_in_order():
    log = FakeLog()
    big_reply = "Z" * (cl.CHAT_LTM_CHUNK_CHARS * 2 + 10)
    await cl.persist_chat_turn(log, "kai", "rid-3", "summarise the doc", big_reply, "kaidera-os")

    assert len(log.calls) >= 3, "a chunked turn writes one LTM entry per chunk"
    # All entries are 'decision' on the same agent/project, in [i/N] order.
    assert all(c["event_type"] == "decision" for c in log.calls)
    assert all(c["agent"] == "kai" and c["project"] == "kaidera-os" for c in log.calls)
    n = len(log.calls)
    for i, c in enumerate(log.calls, start=1):
        assert f"[{i}/{n}]" in c["summary"]
    # The full reply is preserved across the written entries.
    assert sum(c["summary"].count("Z") for c in log.calls) == len(big_reply)


# ---------------------------------------------------------------------------
#  Graceful-degrade — a RAISING / None log collaborator never breaks the caller.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_survives_raising_log():
    """A Cortex-down (every log call raises) must NOT propagate — the chat already
    returned its reply; only the durable LTM write is lost (logged-and-swallowed)."""
    log = FakeLog(raising=True)
    # Must NOT raise.
    await cl.persist_chat_turn(log, "kai", "rid-4", "hi", "yo", "kaidera-os")
    assert log.calls == [], "a raising log records nothing but never crashes the caller"


@pytest.mark.asyncio
async def test_persist_survives_none_log():
    """A None log collaborator (no writer available) is a clean no-op, never a crash."""
    await cl.persist_chat_turn(None, "kai", "rid-5", "hi", "yo", "kaidera-os")


@pytest.mark.asyncio
async def test_persist_skips_empty_turn():
    """Nothing to persist (blank message AND blank reply) → no LTM write at all (don't
    spam memory with empty turns)."""
    log = FakeLog()
    await cl.persist_chat_turn(log, "kai", "rid-6", "   ", "", "kaidera-os")
    assert log.calls == [], "an empty turn writes no LTM entry"


@pytest.mark.asyncio
async def test_persist_writes_when_only_reply_present():
    """A reply with an empty user message (e.g. a resumed turn) is still worth
    persisting — the agent's answer is the durable memory."""
    log = FakeLog()
    await cl.persist_chat_turn(log, "kai", "rid-7", "", "the answer is 42", "kaidera-os")
    assert len(log.calls) == 1
    assert "the answer is 42" in log.calls[0]["summary"]


@pytest.mark.asyncio
async def test_persist_uses_bare_cli_agent_but_keeps_compound_identity_in_summary():
    log = FakeLog()

    await cl.persist_chat_turn(
        log,
        "marlow@marketing",
        "rid-compound",
        "go",
        "done",
        "marketing",
    )

    assert log.calls[0]["agent"] == "marlow"
    assert "marlow@marketing CHAT rid-compound" in log.calls[0]["summary"]


@pytest.mark.asyncio
async def test_cli_log_uses_project_workspace_and_explicit_scope(monkeypatch, tmp_path):
    captured = {}

    class Proc:
        async def wait(self):
            return 0

    async def fake_spawn(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Proc()

    program = tmp_path / ".agents" / "scripts" / "cortex-log"
    monkeypatch.setattr(cl, "_cortex_log_program", lambda workspace=None: str(program))
    monkeypatch.setattr(cl.asyncio, "create_subprocess_exec", fake_spawn)

    await cl.cli_log(
        "marlow",
        "decision",
        "marlow@marketing CHAT rid: done",
        "marketing",
        workspace=str(tmp_path),
    )

    assert captured["argv"][:3] == (str(program), "marlow", "decision")
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["env"]["CORTEX_PROJECT"] == "marketing"
    assert captured["kwargs"]["env"]["KAIDERA_AGENT_WORKSPACE"] == str(tmp_path)
    assert captured["kwargs"]["env"]["PATH"].startswith(str(program.parent))
