"""T8 — tests for the `GET /runstate/stream` SSE channel (`app/main.py`).

The route is the live-push half of the RunState SSoT UI: it wraps
`RunStatePgStore.subscribe(project)` (the T4 `LISTEN run_state_events` wake source)
and, on EACH wake (a `run_id`), RE-READS the SAME T7 read model the HTTP first-paint
uses (`_agent_runs_view_store` → `store.recent`/`store.get_run`) and emits an
``event: runstate`` frame carrying that fresh read-model (the transcript partial the
pane swaps in + the structured selected-run fields). Because the SSE push and the
HTTP first-paint render from the IDENTICAL model, they cannot disagree (ratified
design decision #5).

We drive the BARE async generator backing the route (`_runstate_stream_gen`)
directly — the same style as `test_chat_run_route.py` — so we can pull frames
without standing up a full ASGI stack.

The live-wake test reuses T3/T4's THROWAWAY-DB pattern (`test_runstate_pg.py` /
`test_runstate_subscribe.py`): an admin connection to the live `harness_app` CREATEs
a scratch DB, the T1 migration is applied to it (so the NOTIFY trigger fires), an
`AppDB` pool is pointed at the scratch DB, and the scratch DB is DROPped on teardown.
**The live `harness_app` is only ever used to CREATE/DROP the scratch DB — never
written by the code under test.** A distinct scratch DB name keeps this suite
concurrency-safe with the T3/T4 suites.

Coverage:
  * a primed stream, then a `start_run`/`append_output` for THIS agent, makes the
    generator yield a ``runstate`` frame whose data carries the CURRENT read model
    (the agent's selected run + its appended span text) — the wake → re-read → push,
  * GRACEFUL-DEGRADE — a down store (None / dead DSN) makes the generator END
    CLEANLY (no frame, no raise); a dead-DSN `subscribe()` can't 500 the SSE layer.

Skips (does NOT fail) when the app-DB Postgres at localhost:5500 is unreachable, so
the suite still runs on a machine without the harness-appdb container up.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

# asyncpg is the store's driver; skip the whole module if it (or the DB) is absent.
asyncpg = pytest.importorskip("asyncpg")

from app.adapters.runstate_pg import RunStatePgStore  # noqa: E402
from app.appdb import AppDB  # noqa: E402
from app.domain.runstate import RunRecord, RunSpan  # noqa: E402
from app.main import _runstate_stream_gen  # noqa: E402

# The app-DB Postgres (harness-appdb, host port 5500, DB harness_app). Same DSN
# appdb.py defaults to; overridable for CI. A test-file literal is allow-listed by
# the no-project-literals gate (tests/*); it is never baked into the route/adapter.
APPDB_DSN = os.environ.get(
    "HARNESS_APPDB_DSN",
    "postgresql://harness:harness@localhost:5500/harness_app",
)

# A DSN that can never connect — used to prove graceful-degrade.
DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/does_not_exist"

# This suite's OWN scratch DB (distinct from the T3/T4 suites so all three can run
# concurrently without colliding).
SCRATCH_DB = "runstate_stream_test"

# The migrations the scratch DB is built from (tests[0] → console[1] →
# local-cortex[2] → <repo root>[3]). The runstate migration installs the
# run_state_events trigger; the chat-session migration adds run_state.session_id
# (applied in lexical order, as harness-appdb-migrate does).
_APPDB = Path(__file__).resolve().parents[3] / ".agents" / "data" / "appdb"
MIGRATION = _APPDB / "2026-06-05-runstate.sql"
CHAT_SESSION_MIGRATION = _APPDB / "2026-06-07-chat-session.sql"
METADATA_MIGRATION = _APPDB / "2026-06-07-runstate-metadata.sql"

# Bound every "wait for a frame" so a missed wake fails fast (not a hang).
WAKE_TIMEOUT = 5.0


def _scratch_dsn() -> str:
    """Same server/creds as APPDB_DSN, but the scratch dbname."""
    base, _, _db = APPDB_DSN.rpartition("/")
    return f"{base}/{SCRATCH_DB}"


async def _admin_conn():
    """A short-timeout asyncpg connection to the live harness_app DB, used ONLY to
    CREATE/DROP the scratch DB. Returns None when the server is unreachable."""
    try:
        return await asyncpg.connect(dsn=APPDB_DSN, timeout=3)
    except Exception:
        return None


@pytest.fixture()
async def store():
    """Create a fresh `runstate_stream_test` DB, apply the migration (so the NOTIFY
    trigger exists), yield a RunStatePgStore whose AppDB pool is pointed at it, and
    DROP the scratch DB on teardown. Skips when the app-DB is unreachable."""
    admin = await _admin_conn()
    if admin is None:
        pytest.skip(
            "app-DB Postgres (localhost:5500) unreachable — skipping RunState stream "
            "route tests (needs the harness-appdb container up)"
        )

    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
        await admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    except Exception as exc:  # pragma: no cover - env/permission guard
        await admin.close()
        pytest.skip(f"could not create scratch DB (insufficient perms?): {exc}")

    mig_conn = await asyncpg.connect(dsn=_scratch_dsn(), timeout=3)
    try:
        await mig_conn.execute(MIGRATION.read_text())
        await mig_conn.execute(CHAT_SESSION_MIGRATION.read_text())
        await mig_conn.execute(METADATA_MIGRATION.read_text())
    finally:
        await mig_conn.close()

    appdb = AppDB(dsn=_scratch_dsn())
    st = RunStatePgStore(appdb)
    try:
        yield st
    finally:
        await appdb.aclose()
        try:
            await admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                  FROM pg_stat_activity
                 WHERE datname = $1 AND pid <> pg_backend_pid()
                """,
                SCRATCH_DB,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        await admin.close()


async def _prime(agen, timeout: float = WAKE_TIMEOUT) -> "asyncio.Future":
    """Start consuming the SSE generator and return a future for its NEXT yielded
    frame.

    CRITICAL (mirrors test_runstate_subscribe._prime): an async generator's body —
    and therefore the `LISTEN run_state_events` inside `subscribe()` — does not run
    until the first `__anext__()` is awaited. This kicks the first pull off as a
    background task and gives it a beat to establish the LISTEN, so a write emitted
    AFTER awaiting `_prime(...)` is guaranteed to be heard. The future is bounded so
    a missed wake fails fast (TimeoutError) instead of hanging."""
    fut: "asyncio.Future" = asyncio.ensure_future(
        asyncio.wait_for(agen.__anext__(), timeout=timeout)
    )
    await asyncio.sleep(0.4)  # let the body run far enough to issue its LISTEN
    return fut


# ---------------------------------------------------------------------------
#  Reconnect replay — a selected run gets one immediate snapshot frame.
# ---------------------------------------------------------------------------


class FakeReplayStore:
    """No-DB RunStatePort subset for reconnect replay tests."""

    def __init__(self, runs: list[RunRecord]):
        self._runs = {run.run_id: run for run in runs}
        self.calls: list[str] = []
        self.subscribe_projects: list[str | None] = []
        self.subscribe_started = asyncio.Event()
        self.subscribe_closed = False

    async def get_run(self, run_id: str):
        self.calls.append(f"get_run:{run_id}")
        return self._runs.get(run_id)

    async def recent(
        self,
        project=None,
        limit: int = 20,
        *,
        session_id=None,
        lease_owner=None,
    ):
        self.calls.append(f"recent:{project}:{limit}")
        rows = [
            run for run in self._runs.values()
            if project is None or (run.project or "").lower() == str(project).lower()
        ]
        return rows[:limit]

    async def subscribe(self, project=None):
        self.subscribe_projects.append(project)
        self.subscribe_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.subscribe_closed = True
        if False:  # pragma: no cover - pins async-generator shape
            yield ""


async def test_stream_replays_existing_running_run_on_connect_without_agent():
    """`/runstate/stream?run=<id>` emits an initial selected-run snapshot for an
    existing running run, without waiting for a NOTIFY."""
    run = RunRecord(
        run_id="run-reconnect-running",
        project="proj",
        agent="agt",
        agent_display="Agt",
        status="running",
        spans=[RunSpan(seq=1, kind="output", text="already visible")],
    )
    store = FakeReplayStore([run])
    agen = _runstate_stream_gen(store, "proj", run_id=run.run_id)

    try:
        frame = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        assert frame["event"] == "runstate"
        assert store.subscribe_started.is_set(), "subscribe starts before replay read"

        payload = json.loads(frame["data"])
        assert payload["initial"] is True
        assert payload["wake_run_id"] == run.run_id
        assert payload["selected_id"] == run.run_id
        assert payload["project"] == "proj"
        assert payload["agent"] == "agt"
        assert payload["running"] == 1
        assert payload["count"] == 1
        assert payload["selected"]["status"] == "running"
        assert payload["selected"]["body"] == "already visible"
    finally:
        await agen.aclose()

    assert store.subscribe_closed is True


async def test_stream_replays_existing_terminal_selected_run_on_connect():
    """An agent-scoped selected terminal run also gets an immediate snapshot frame,
    so reload/reconnect can show completed output without waiting for a new NOTIFY."""
    run = RunRecord(
        run_id="run-reconnect-terminal",
        project="proj",
        agent="agt",
        agent_display="Agt",
        status="ok",
        ended_at="2026-06-24T12:00:00+00:00",
        spans=[RunSpan(seq=1, kind="output", text="terminal output")],
    )
    store = FakeReplayStore([run])
    agen = _runstate_stream_gen(store, "proj", agent="agt", run_id=run.run_id)

    try:
        frame = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        assert frame["event"] == "runstate"

        payload = json.loads(frame["data"])
        assert payload["initial"] is True
        assert payload["wake_run_id"] == run.run_id
        assert payload["selected_id"] == run.run_id
        assert payload["running"] == 0
        assert payload["count"] == 1
        assert payload["selected"]["status"] == "ok"
        assert payload["selected"]["status_label"] == "completed"
        assert payload["selected"]["body"] == "terminal output"
        assert "terminal output" in payload["html"]
    finally:
        await agen.aclose()


async def test_stream_without_run_filter_still_waits_for_notify():
    """Project/agent streams preserve the old behavior: no initial snapshot frame
    is emitted without a selected run filter."""
    run = RunRecord(
        run_id="run-no-replay",
        project="proj",
        agent="agt",
        status="running",
    )
    store = FakeReplayStore([run])
    agen = _runstate_stream_gen(store, "proj", agent="agt")
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agen.__anext__(), timeout=0.1)
    finally:
        await agen.aclose()


# ── the wake fires → the route re-reads the SAME model → a runstate frame ─────


async def test_stream_emits_runstate_frame_on_state_change(store):
    """A primed `/runstate/stream` generator, then a run's state changes (start_run
    + append_output for THIS agent), makes it emit an ``event: runstate`` frame whose
    data carries the CURRENT read model (the selected run + the span text just
    appended) — the wake → re-read-the-same-model → push contract."""
    run_id = str(uuid.uuid4())
    # Pre-create the run so the selected-run resolution has a row to land on; the
    # later append fires a fresh NOTIFY the primed stream will hear.
    await store.start_run(
        run_id=run_id, project="proj", agent="agt", agent_display="Agt"
    )

    agen = _runstate_stream_gen(store, "proj", agent="agt")
    try:
        frame_fut = await _prime(agen)  # LISTEN is live before we mutate

        # Mutate the run → AFTER-UPDATE trigger → pg_notify('run_state_events').
        await store.append_output(run_id, seq=1, kind="output", text="hello world")

        frame = await frame_fut
        assert frame["event"] == "runstate", "the SSE frame must be an event: runstate"

        payload = json.loads(frame["data"])
        # The frame carries the FRESH read model: the selected run is this run, with
        # the span text we just appended (proving the route re-read after the wake,
        # not a stale snapshot).
        assert payload.get("agent") == "agt"
        sel = payload.get("selected")
        assert sel is not None, "a runstate frame for a known run carries the selected run"
        assert sel.get("run_id") == run_id
        body = sel.get("body") or ""
        assert "hello world" in body, (
            "the pushed read model must reflect the just-appended span (re-read, "
            "not a stale snapshot)"
        )
        # It also carries the rendered transcript partial the pane swaps in, so the
        # SSE push and the HTTP first-paint render from the identical model.
        assert "html" in payload, "the frame carries the rendered transcript partial"
        assert run_id[:8] in payload["html"] or "hello world" in payload["html"]
    finally:
        await agen.aclose()


# ── GRACEFUL-DEGRADE — a down store ends the stream cleanly, never raises ─────


async def test_stream_none_store_ends_cleanly_no_raise():
    """A None store (run-state SSOT failed to construct) makes the generator end
    cleanly — zero frames, no raise. The SSE layer must never 500 on a down store."""
    items: list = []
    async for frame in _runstate_stream_gen(None, "proj", agent="agt"):  # pragma: no cover - body
        items.append(frame)
    assert items == [], "a None-store stream yields nothing and ends cleanly"


async def test_stream_dead_store_ends_cleanly_no_raise():
    """A store pointed at a dead DSN: `subscribe()` ends cleanly (T4 contract), so
    the SSE generator yields nothing and does NOT propagate the connection error —
    a dead app-DB can't break the SSE layer."""
    appdb = AppDB(dsn=DEAD_DSN)
    st = RunStatePgStore(appdb)
    try:
        items: list = []
        # If the underlying subscribe() raised the connect error, this would blow up.
        async for frame in _runstate_stream_gen(st, "proj", agent="agt"):  # pragma: no cover - body
            items.append(frame)
        assert items == [], "a dead-store stream yields nothing and ends cleanly"
    finally:
        await appdb.aclose()


async def test_stream_dead_store_anext_raises_stopasynciteration():
    """The async-generator contract for the degraded path: advancing a dead-store
    stream raises StopAsyncIteration (a clean end), NOT a connection error."""
    appdb = AppDB(dsn=DEAD_DSN)
    st = RunStatePgStore(appdb)
    try:
        agen = _runstate_stream_gen(st, "proj", agent="agt")
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(agen.__anext__(), timeout=WAKE_TIMEOUT)
    finally:
        await appdb.aclose()
