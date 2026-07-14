"""Multi-turn chat (feature-gap step 6, Inc B) — the session-history reader + the
contextual-prompt composer (`app/chat_history.py`).

`app/chat_history.py` is the PURE-ish app-layer module that turns a chat conversation's
prior turns into prompt context. It depends ONLY on the `RunStatePort` (never an
adapter), so it stays unit-testable with a fake store and free of any per-project
literal:

  * `load_session_history(store, project, agent, session_id, *, max_turns, max_chars)`
    — read the recent `lease_owner='chat'` runs for a session_id, rebuild each turn's
    `(user_message, assistant_reply)` (user from the `input` span, reply from the
    concatenated `output` spans), OLDEST-FIRST, capped by turns AND chars (drop the
    OLDEST beyond the cap). Returns [] for a None / down store (suppress exceptions —
    a chat must degrade to single-shot, never crash).

  * `compose_contextual_prompt(prompt, system, history)` — PURE: prepend a
    `[Previous conversation] … [Current message] <prompt>` block when there's history,
    else return the prompt UNCHANGED.

These tests drive a FAKE store (records calls, returns scripted RunRecords) — no live
harness, no DB, no network.
"""
from __future__ import annotations

import pytest

import app.chat_history as ch
from app.domain.runstate import RunRecord, RunSpan


# ---------------------------------------------------------------------------
#  A fake RunStatePort: `recent(session_id=...)` returns scripted headers
#  (newest-first, like the real adapter), `get_run` hydrates a run's spans.
#  `raising=True` forces both to raise so the degrade path is exercised.
# ---------------------------------------------------------------------------

class FakeHistoryStore:
    def __init__(self, runs: list[RunRecord], *, raising: bool = False):
        # `runs` are oldest→newest as the caller builds them; recent() returns
        # them NEWEST-FIRST (mirroring the adapter's ORDER BY started_at DESC).
        self._runs = runs
        self.raising = raising
        self.recent_calls: list[dict] = []
        self.get_calls: list[str] = []

    async def recent(self, project=None, limit=20, *, session_id=None):
        if self.raising:
            raise RuntimeError("store down")
        self.recent_calls.append({"project": project, "limit": limit, "session_id": session_id})
        # Newest-first headers, scoped to the session (a real adapter filters in SQL;
        # here every scripted run already belongs to the asked session).
        return list(reversed(self._runs))

    async def get_run(self, run_id):
        if self.raising:
            raise RuntimeError("store down")
        self.get_calls.append(run_id)
        for r in self._runs:
            if r.run_id == run_id:
                return r
        return None


def _turn(run_id: str, user: str, reply: str, *, started_at: str = "") -> RunRecord:
    """A completed chat RunRecord: an `input` span (the user message) + `output`
    span(s) (the reply), lease_owner='chat'."""
    spans = [RunSpan(seq=1, kind="input", text=user)]
    # Split the reply across two output spans to prove they concatenate.
    if reply:
        mid = max(1, len(reply) // 2)
        spans.append(RunSpan(seq=2, kind="output", text=reply[:mid]))
        spans.append(RunSpan(seq=3, kind="output", text=reply[mid:]))
    return RunRecord(
        run_id=run_id, project="kaidera-os", agent="ren", lease_owner="chat",
        session_id="sess-1", status="ok", started_at=started_at, spans=spans,
    )


# ── load_session_history — happy path: oldest-first (user, reply) tuples ──────


@pytest.mark.asyncio
async def test_load_history_rebuilds_turns_oldest_first():
    runs = [
        _turn("r1", "first question", "first answer"),
        _turn("r2", "second question", "second answer"),
        _turn("r3", "third question", "third answer"),
    ]
    store = FakeHistoryStore(runs)

    hist = await ch.load_session_history(store, "kaidera-os", "ren", "sess-1")

    # OLDEST-FIRST tuples of (user_message, assistant_reply).
    assert hist == [
        ("first question", "first answer"),
        ("second question", "second answer"),
        ("third question", "third answer"),
    ]
    # The read was scoped to the session.
    assert store.recent_calls[0]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_load_history_concatenates_multiple_output_spans():
    """A reply split across several `output` spans is rejoined in seq order; the
    `input` span supplies the user message (not treated as output)."""
    run = RunRecord(
        run_id="r1", project="kaidera-os", agent="ren", lease_owner="chat",
        session_id="sess-1", status="ok",
        spans=[
            RunSpan(seq=1, kind="input", text="hello"),
            RunSpan(seq=2, kind="output", text="Hi, "),
            RunSpan(seq=3, kind="output", text="there!"),
        ],
    )
    store = FakeHistoryStore([run])
    hist = await ch.load_session_history(store, "kaidera-os", "ren", "sess-1")
    assert hist == [("hello", "Hi, there!")]


# ── caps — turns AND chars, dropping the OLDEST beyond the cap ────────────────


@pytest.mark.asyncio
async def test_load_history_caps_by_turns_keeping_newest():
    runs = [_turn(f"r{i}", f"q{i}", f"a{i}") for i in range(1, 6)]  # 5 turns
    store = FakeHistoryStore(runs)

    hist = await ch.load_session_history(store, "kaidera-os", "ren", "sess-1", max_turns=2)

    # Only the 2 NEWEST turns survive, still oldest-first within the kept window.
    assert hist == [("q4", "a4"), ("q5", "a5")]


@pytest.mark.asyncio
async def test_load_history_caps_by_chars_dropping_oldest():
    # Each turn ~100 chars of body; a small char cap keeps only the newest few.
    runs = [_turn(f"r{i}", "u" * 40, "a" * 60) for i in range(1, 6)]
    store = FakeHistoryStore(runs)

    hist = await ch.load_session_history(
        store, "kaidera-os", "ren", "sess-1", max_turns=8, max_chars=250
    )

    # The char cap drops the OLDEST turns; the kept set fits under the budget and is
    # the NEWEST contiguous window (oldest-first within it).
    assert hist, "at least the newest turn fits"
    total = sum(len(u) + len(a) for u, a in hist)
    assert total <= 250, f"kept history must fit the char cap, got {total}"
    # The newest turn is always present (we keep newest, drop oldest).
    assert hist[-1] == ("u" * 40, "a" * 60)
    # And we dropped at least one oldest turn (5 turns can't all fit in 250 chars).
    assert len(hist) < 5


# ── empty / degrade ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_history_none_store_returns_empty():
    """A None store (app-DB down / not constructed) → [] (single-shot fallback)."""
    assert await ch.load_session_history(None, "kaidera-os", "ren", "sess-1") == []


@pytest.mark.asyncio
async def test_load_history_blank_session_returns_empty():
    """No session_id → no conversation → [] (and the store is never queried)."""
    store = FakeHistoryStore([])
    assert await ch.load_session_history(store, "kaidera-os", "ren", "") == []
    assert await ch.load_session_history(store, "kaidera-os", "ren", None) == []
    assert store.recent_calls == [], "a blank session must not hit the store"


@pytest.mark.asyncio
async def test_load_history_raising_store_returns_empty():
    """A raising store (down DB) is suppressed → [] (a chat degrades to single-shot,
    never crashes)."""
    runs = [_turn("r1", "q", "a")]
    store = FakeHistoryStore(runs, raising=True)
    assert await ch.load_session_history(store, "kaidera-os", "ren", "sess-1") == []


@pytest.mark.asyncio
async def test_load_history_skips_turns_without_both_sides():
    """A turn with NO user input (e.g. the very turn being processed, before its reply,
    or a malformed row) or no reply is skipped — only complete (user, reply) pairs are
    threaded as context."""
    complete = _turn("r1", "real question", "real answer")
    # A run with only an input span (no reply yet) — must be skipped.
    no_reply = RunRecord(
        run_id="r2", project="kaidera-os", agent="ren", lease_owner="chat",
        session_id="sess-1", status="running",
        spans=[RunSpan(seq=1, kind="input", text="pending question")],
    )
    # A run with only output (no input span) — must be skipped.
    no_input = RunRecord(
        run_id="r3", project="kaidera-os", agent="ren", lease_owner="chat",
        session_id="sess-1", status="ok",
        spans=[RunSpan(seq=1, kind="output", text="orphan answer")],
    )
    store = FakeHistoryStore([complete, no_reply, no_input])
    hist = await ch.load_session_history(store, "kaidera-os", "ren", "sess-1")
    assert hist == [("real question", "real answer")]


# ── compose_contextual_prompt — pure ─────────────────────────────────────────


def test_compose_with_history_includes_block_and_current():
    history = [("q1", "a1"), ("q2", "a2")]
    out = ch.compose_contextual_prompt("the new message", "You are Ren.", history)

    assert "[Previous conversation]" in out
    assert "[Current message]" in out
    # Both prior turns are present, labelled User/Assistant.
    assert "User: q1" in out and "Assistant: a1" in out
    assert "User: q2" in out and "Assistant: a2" in out
    # The current message is the tail.
    assert out.rstrip().endswith("the new message")


def test_compose_without_history_returns_prompt_unchanged():
    """No history → the prompt is returned BYTE-FOR-BYTE (single-shot path; no block)."""
    assert ch.compose_contextual_prompt("just this", "You are Ren.", []) == "just this"
    assert ch.compose_contextual_prompt("just this", None, []) == "just this"


def test_compose_orders_turns_oldest_first_then_current_last():
    """The conversation reads top-to-bottom oldest→newest, with the current message
    LAST (the model sees history, then the thing to answer)."""
    out = ch.compose_contextual_prompt("answer me", None, [("old", "o"), ("new", "n")])
    i_old = out.index("User: old")
    i_new = out.index("User: new")
    i_cur = out.index("answer me")
    assert i_old < i_new < i_cur


# ── env-overridable caps + constants ─────────────────────────────────────────


def test_history_cap_constants_exist():
    assert ch.HISTORY_MAX_TURNS == 8
    assert ch.HISTORY_MAX_CHARS == 12000
