"""Console History route tests (`app/history/api.py`).

The console-side cross-agent activity-timeline JSON surface — the clean backend for the
SPA `HistoryView`. ONE READ-ONLY endpoint:

  * `GET /history/{project}?limit=N&include_decisions=1` → `{events, decisions, agent_count}`:
      - `events`     — the reverse-chronological cross-agent timeline from Cortex
                       `/history`, each raw row run through the PORTED summarizer (a clean
                       readable line, NEVER the raw noisy tool-call JSON). Newest-first.
      - `decisions`  — optional recent-decisions feed from Cortex `/search` (decisions/lessons mix).
      - `agent_count`— distinct agents on the project roster.

Driven via an in-process httpx ASGITransport over a minimal app that mounts the router,
with a FAKE cortex on `app.state.cortex` (a scripted `get_history` + `search` + `get_roster`)
— NO live Cortex, nothing spawned. The summariser is the legacy `main._summarize_history_row`
PORTED into `app.history.shape`, so these assertions check the shaped line, not raw content.
"""

from __future__ import annotations

import json
import asyncio

import pytest
from fastapi import FastAPI

from app.history.api import router as history_router


class FakeCortexForHistory:
    """A minimal CortexClient stand-in: scriptable get_history + search + get_roster.

    `history_rows` is the raw `messages` list `/history` returns (each
    {when, agent_name, role, content}); `search_rows` is the `/search` results list
    (each {text, source, category, ...}); `roster_rows` is the `/roster` agents list.
    Any of the three may be set to raise to exercise the graceful-degrade path.
    Records the calls it was asked (project/limit/query)."""

    def __init__(
        self,
        *,
        history_rows=None,
        search_rows=None,
        roster_rows=None,
        history_raises=False,
        search_raises=False,
        roster_raises=False,
        history_delay=0.0,
        roster_delay=0.0,
    ):
        self._history = history_rows if history_rows is not None else []
        self._search = search_rows if search_rows is not None else []
        self._roster = roster_rows if roster_rows is not None else []
        self._history_raises = history_raises
        self._search_raises = search_raises
        self._roster_raises = roster_raises
        self._history_delay = history_delay
        self._roster_delay = roster_delay
        self.history_calls = []
        self.search_calls = []
        self.roster_calls = []

    async def get_history(self, project_key, limit=200):
        self.history_calls.append({"project": project_key, "limit": limit})
        if self._history_delay:
            await asyncio.sleep(self._history_delay)
        if self._history_raises:
            raise RuntimeError("cortex history down")
        return self._history

    async def search(self, project_key, query, limit=12, *, rerank=True):
        self.search_calls.append(
            {"project": project_key, "query": query, "limit": limit, "rerank": rerank}
        )
        if self._search_raises:
            raise RuntimeError("cortex search down")
        return self._search

    async def get_roster(self, project_key):
        self.roster_calls.append({"project": project_key})
        if self._roster_delay:
            await asyncio.sleep(self._roster_delay)
        if self._roster_raises:
            raise RuntimeError("cortex roster down")
        return self._roster


def _fc(name: str, *, arguments: dict | None = None, **extra) -> str:
    """Build a function_call content blob (the noisy JSON the summariser must clean)."""
    obj = {"type": "function_call", "name": name, **extra}
    if arguments is not None:
        obj["arguments"] = json.dumps(arguments)
    return json.dumps(obj)


# A representative /history window (newest-first, as the live API returns it): a plain
# message, a tool call (exec_command with a cmd), a reasoning frame, and a token_count
# frame that the summariser DROPS (it surfaces in the header readout, not the timeline).
_HISTORY = [
    {"when": "2026-06-07T12:00:30Z", "agent_name": "ada", "role": "assistant",
     "content": "shipped the history endpoint and verified the tests"},
    {"when": "2026-06-07T12:00:20Z", "agent_name": "ivy", "role": "assistant",
     "content": _fc("exec_command", arguments={"cmd": "pytest -q"})},
    {"when": "2026-06-07T12:00:10Z", "agent_name": "ada", "role": "assistant",
     "content": json.dumps({"type": "reasoning"})},
    {"when": "2026-06-07T12:00:05Z", "agent_name": "ivy", "role": "assistant",
     "content": json.dumps({"type": "token_count",
                            "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 5}}})},
]

_SEARCH = [
    {"text": "decided to graceful-degrade the history endpoint to empty lists",
     "source": "decisions", "category": "architecture", "relevance": 0.9},
    {"text": "lesson: always summarise the noisy /history content before rendering",
     "source": "lessons", "category": "ux", "relevance": 0.8},
]

_ROSTER = [
    {"name": "ada", "role": "full-stack"},
    {"name": "ivy", "role": "qa"},
    {"name": "max", "role": "pm"},
]


def _make_app(*, cortex=None):
    app = FastAPI()
    app.include_router(history_router)
    app.state.cortex = cortex if cortex is not None else FakeCortexForHistory()
    return app


def _client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.test")


# ---------------------------------------------------------------------------
#  GET /history/{project} — events timeline (summarised + reverse-chronological)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_events_are_summarised_not_raw():
    cortex = FakeCortexForHistory(history_rows=_HISTORY, search_rows=_SEARCH, roster_rows=_ROSTER)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")
    assert resp.status_code == 200
    data = resp.json()

    events = data["events"]
    # The token_count frame is DROPPED (it is not a timeline row) → 3 events from 4 raw rows.
    assert len(events) == 3

    # Each event carries the who/what/when shape, summarised — never the raw JSON.
    for ev in events:
        assert ev["agent"]
        assert ev["summary"]
        assert "ts" in ev and "ts_ago" in ev
        # The raw noisy markers never leak into the readable summary.
        assert "function_call" not in ev["summary"]
        assert "token_count" not in ev["summary"]

    # The tool row summarises to a readable action line (the ported summariser).
    tool_ev = next(e for e in events if e.get("kind") == "tool")
    assert "exec_command" in tool_ev["summary"]
    # The plain message row reads as a clean say line.
    say_ev = next(e for e in events if e.get("kind") == "say")
    assert "shipped the history endpoint" in say_ev["summary"]


@pytest.mark.asyncio
async def test_history_events_are_reverse_chronological():
    """events come back NEWEST-FIRST (reverse-chronological), matching the legacy timeline."""
    cortex = FakeCortexForHistory(history_rows=_HISTORY, search_rows=_SEARCH, roster_rows=_ROSTER)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")
    events = resp.json()["events"]
    ts_list = [e["ts"] for e in events]
    # Strictly non-increasing timestamps (newest at index 0).
    assert ts_list == sorted(ts_list, reverse=True)
    # The newest row (12:00:30) is first; it carries a relative-age label.
    assert events[0]["ts"] == "2026-06-07T12:00:30Z"
    assert events[0]["ts_ago"]  # a non-empty 'how long ago' label


@pytest.mark.asyncio
async def test_history_default_skips_decisions_search_for_hot_poll_path():
    cortex = FakeCortexForHistory(history_rows=_HISTORY, search_rows=_SEARCH, roster_rows=_ROSTER)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 3
    assert data["decisions"] == []
    assert cortex.search_calls == []


@pytest.mark.asyncio
async def test_history_decisions_feed_shaped_when_requested():
    cortex = FakeCortexForHistory(history_rows=_HISTORY, search_rows=_SEARCH, roster_rows=_ROSTER)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os", params={"include_decisions": "1"})
    decisions = resp.json()["decisions"]
    assert len(decisions) == 2
    first = decisions[0]
    # Each decision row carries a readable summary + its source layer.
    assert "graceful-degrade the history endpoint" in first["summary"]
    assert first["source"] == "decisions"
    # The decisions feed seeded the /search call (a non-blank query).
    assert cortex.search_calls and cortex.search_calls[0]["query"].strip()
    assert cortex.search_calls[0]["rerank"] is False


@pytest.mark.asyncio
async def test_history_agent_count_from_roster():
    cortex = FakeCortexForHistory(history_rows=_HISTORY, search_rows=_SEARCH, roster_rows=_ROSTER)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")
    # agent_count is the roster size (3 agents), NOT just the distinct agents in the window.
    assert resp.json()["agent_count"] == 3
    assert cortex.roster_calls and cortex.roster_calls[0]["project"] == "kaidera-os"


@pytest.mark.asyncio
async def test_history_respects_limit_param():
    cortex = FakeCortexForHistory(history_rows=_HISTORY, search_rows=_SEARCH, roster_rows=_ROSTER)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        await c.get("/history/kaidera-os", params={"limit": 42})
    # The limit is forwarded to the Cortex get_history window.
    assert cortex.history_calls[0]["limit"] == 42


@pytest.mark.asyncio
async def test_history_caps_event_rows():
    """A history window larger than the display cap renders at most the cap (newest kept)."""
    big = [
        {"when": f"2026-06-07T12:{i // 60:02d}:{i % 60:02d}Z", "agent_name": "ada",
         "role": "assistant", "content": f"message number {i}"}
        for i in range(200)
    ]
    cortex = FakeCortexForHistory(history_rows=big, search_rows=[], roster_rows=_ROSTER)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")
    events = resp.json()["events"]
    # Bounded — never the whole window (the legacy _HISTORY_MAX cap, ported).
    from app.history.shape import HISTORY_EVENT_CAP
    assert len(events) == HISTORY_EVENT_CAP


# ---------------------------------------------------------------------------
#  Graceful degrade — a down/empty Cortex never 500s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_degrades_to_empty_on_down_cortex():
    """get_history/search/roster all RAISE → empty lists + zero count, HTTP 200 (never 500)."""
    cortex = FakeCortexForHistory(history_raises=True, search_raises=True, roster_raises=True)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")
    assert resp.status_code == 200
    data = resp.json()
    assert data["events"] == []
    assert data["decisions"] == []
    assert data["agent_count"] == 0


@pytest.mark.asyncio
async def test_history_empty_payload_is_clean_empty():
    """An empty (but reachable) Cortex → empty events/decisions, zero count, 200."""
    cortex = FakeCortexForHistory(history_rows=[], search_rows=[], roster_rows=[])
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")
    assert resp.status_code == 200
    data = resp.json()
    assert data["events"] == []
    assert data["decisions"] == []
    assert data["agent_count"] == 0


@pytest.mark.asyncio
async def test_history_no_cortex_on_state_degrades():
    """If app.state.cortex is None (the client failed to construct) the route still
    answers an empty payload, never a 500/AttributeError."""
    app = FastAPI()
    app.include_router(history_router)
    app.state.cortex = None
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")
    assert resp.status_code == 200
    data = resp.json()
    assert data["events"] == []
    assert data["decisions"] == []
    assert data["agent_count"] == 0


@pytest.mark.asyncio
async def test_history_partial_degrade_keeps_other_sections():
    """One section down (search raises) must NOT blank the others — events + roster still shape."""
    cortex = FakeCortexForHistory(history_rows=_HISTORY, search_raises=True, roster_rows=_ROSTER)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os", params={"include_decisions": "1"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 3        # timeline intact
    assert data["decisions"] == []         # the down section degrades alone
    assert data["agent_count"] == 3        # roster intact


@pytest.mark.asyncio
async def test_history_timeboxes_slow_hot_reads(monkeypatch):
    """A slow history or roster read degrades that section instead of blocking the route."""
    from app.history import api as history_api

    monkeypatch.setattr(history_api, "_READ_TIMEOUT_S", 0.01)
    cortex = FakeCortexForHistory(
        history_rows=_HISTORY,
        roster_rows=_ROSTER,
        history_delay=0.1,
        roster_delay=0.1,
    )
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/history/kaidera-os")

    assert resp.status_code == 200
    data = resp.json()
    assert data["events"] == []
    assert data["decisions"] == []
    assert data["agent_count"] == 0
