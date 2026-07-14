"""Multi-turn chat (feature-gap step 6, Inc B) — the RunStatePort Pg adapter persists
+ filters by `session_id` (`app/adapters/runstate_pg.py`).

Two adapter changes are pinned here, with a FAKE asyncpg pool (no live DB, no network):

  1. `start_run` writes `session_id` into the run_state INSERT — the fake pool captures
     the executed SQL + bound args and asserts session_id rides along (in the column
     list AND as a bound parameter), and the ON CONFLICT path carries it too.

  2. `recent(session_id=...)` adds a session filter — the captured SQL constrains on
     session_id and binds it, so "the recent turns of THIS conversation" is a scoped
     read. `recent()` with no session_id is UNCHANGED (no session predicate bound).

These complement the live-DB suite (`test_runstate_pg.py`, which skips without the
harness-appdb container): here we assert the SQL CONTRACT structurally so it runs
everywhere. Graceful-degrade is unchanged (covered in the live suite's dead-DSN test).
"""
from __future__ import annotations

import pytest

asyncpg = pytest.importorskip("asyncpg")

from app.adapters.runstate_pg import RunStatePgStore  # noqa: E402
from app.appdb import AppDB  # noqa: E402


# ---------------------------------------------------------------------------
#  A fake asyncpg pool/connection that RECORDS every fetch/fetchrow/execute call
#  (the SQL text + the bound args) so we can assert the adapter's SQL contract
#  without a live DB. `fetchrow` returns a dict-like row so `_record_from_row`
#  maps it cleanly.
# ---------------------------------------------------------------------------

class _FakeRow(dict):
    """A mapping that also supports row["col"] like an asyncpg Record."""


class _FakeConn:
    def __init__(self, recorder: list[tuple[str, str, tuple]]):
        self._rec = recorder
        # A canned run_state header row for fetchrow/fetch to return.
        self._header = _FakeRow(
            run_id="rid", project="kaidera-os", agent="ren", agent_display="Ren",
            handoff_id=None, harness="claude-code", model="opus",
            session_id="sess-99", status="queued",
            error=None, pid=None, lease_owner="chat", tokens_in=None, tokens_out=None,
            cost_est_usd=None, started_at=None, updated_at=None, heartbeat_at=None,
            ended_at=None, metadata=None,
        )

    async def fetchrow(self, sql, *args):
        self._rec.append(("fetchrow", sql, args))
        return self._header

    async def fetch(self, sql, *args):
        self._rec.append(("fetch", sql, args))
        return [self._header]

    async def execute(self, sql, *args):
        self._rec.append(("execute", sql, args))
        return "OK"


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, recorder):
        self._conn = _FakeConn(recorder)

    def acquire(self, *, timeout=None):
        # Mirror asyncpg's `Pool.acquire(*, timeout=None)` — the adapter now passes
        # a bounded acquire timeout so a starved pool degrades instead of hanging.
        return _FakeAcquire(self._conn)


def _store_with_fake_pool():
    """A RunStatePgStore whose `_pool()` returns a recording fake pool. Returns
    (store, recorder) — recorder is a list of (method, sql, args) tuples."""
    recorder: list[tuple[str, str, tuple]] = []
    store = RunStatePgStore(AppDB(dsn="postgresql://x:x@127.0.0.1:1/none"))
    pool = _FakePool(recorder)

    async def _fake_pool():
        return pool

    store._pool = _fake_pool  # type: ignore[assignment]
    return store, recorder


# ── start_run writes session_id ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_run_insert_carries_session_id():
    """start_run(session_id=...) binds session_id into the run_state INSERT — both as
    a column and as a bound parameter (so the conversation grouping key persists)."""
    store, rec = _store_with_fake_pool()

    out = await store.start_run(
        run_id="rid", project="kaidera-os", agent="ren",
        lease_owner="chat", session_id="sess-42",
    )
    assert out.run_id == "rid"

    # The INSERT was the fetchrow call; assert session_id is in the SQL + bound args.
    inserts = [c for c in rec if c[0] == "fetchrow"]
    assert inserts, "start_run must execute an INSERT (fetchrow)"
    _, sql, args = inserts[0]
    assert "session_id" in sql, "the run_state INSERT must include the session_id column"
    assert "sess-42" in args, "the session_id value must be a bound parameter"
    # The ON CONFLICT update keeps it fresh too (so a re-opened row carries it).
    assert "session_id" in sql.split("ON CONFLICT", 1)[1], (
        "ON CONFLICT DO UPDATE must also set session_id"
    )


@pytest.mark.asyncio
async def test_start_run_without_session_id_binds_none():
    """A start_run with NO session_id (worker / single-shot chat) binds None — the
    column is still written (additive), just NULL. Back-compat: existing callers."""
    store, rec = _store_with_fake_pool()
    await store.start_run(run_id="rid", project="kaidera-os", agent="ren")

    _, sql, args = [c for c in rec if c[0] == "fetchrow"][0]
    assert "session_id" in sql
    # None is bound for the session_id slot (no conversation) — the row is NULL there.
    assert None in args


# ── recent(session_id=...) filters ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_with_session_id_filters_by_session():
    """recent(session_id=...) adds a session_id predicate to the SELECT and binds the
    value — 'the recent turns of THIS conversation', newest-first."""
    store, rec = _store_with_fake_pool()

    rows = await store.recent(project="kaidera-os", session_id="sess-99", limit=5)
    assert rows, "recent returns the (fake) rows"

    selects = [c for c in rec if c[0] == "fetch"]
    assert selects, "recent must execute a SELECT (fetch)"
    _, sql, args = selects[0]
    assert "session_id" in sql, "recent(session_id=...) must constrain on session_id"
    assert "sess-99" in args, "the session_id value must be bound"


@pytest.mark.asyncio
async def test_recent_without_session_id_has_no_session_predicate():
    """recent() with NO session_id is UNCHANGED — no session_id value is bound (the
    existing recent-runs behaviour, byte-for-byte for non-session callers)."""
    store, rec = _store_with_fake_pool()
    await store.recent(project="kaidera-os", limit=5)

    _, _sql, args = [c for c in rec if c[0] == "fetch"][0]
    # Only the project arg is bound (lower-cased 'kaidera-os'); no session value present.
    assert "sess-99" not in args
