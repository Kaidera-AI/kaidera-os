"""T4 — tests for `RunStatePgStore.subscribe()` (LISTEN `run_state_events`).

`subscribe()` is the live-push half of the RunState SSoT: it parks on the app-DB
`run_state_events` NOTIFY bus (the trigger the T1 migration installs fires
`pg_notify('run_state_events', json{run_id, project})` on every run_state /
run_span change) and yields the changed run's `run_id` as a WAKE signal. It is the
app-DB twin of `cortex_client.stream_events` (LISTEN/NOTIFY over HTTP). The caller
(the SSE layer, T8) re-reads `get_run`/`list_active` on each wake, so first-paint
and the live push read the same model and cannot disagree — the NOTIFY carries no
state of its own.

These tests reuse T3's THROWAWAY-DB pattern (`test_runstate_pg.py`): an admin
connection to the live `harness_app` CREATEs a scratch DB, the T1 migration is
applied to it (so the NOTIFY trigger exists), an `AppDB` pool is pointed at the
scratch DB, and the scratch DB is DROPped on teardown. **The live `harness_app` is
only ever used to CREATE/DROP the scratch DB — never written by the code under
test.** A SECOND scratch DB name is used here so this suite can run concurrently
with the T3 suite without colliding.

Coverage:
  * a subscription started, then a `start_run`/`append_output` in ANOTHER
    connection, makes `subscribe` yield the matching `run_id` (the wake fires),
  * PROJECT-FILTERING — an event for project B is NOT yielded to a project-A
    subscriber (and an unfiltered subscriber sees both),
  * GRACEFUL-DEGRADE — pointed at a dead DSN the generator ENDS cleanly
    (StopAsyncIteration), never raising into the consumer.

`asyncio.wait_for` timeouts bound every await so a MISSED notify fails fast
(TimeoutError) instead of hanging the suite forever.

Skips (does NOT fail) when the app-DB Postgres at localhost:5500 is unreachable,
so the suite still runs on a machine without the harness-appdb container up.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

# asyncpg is the adapter's driver; skip the whole module if it (or the DB) is absent.
asyncpg = pytest.importorskip("asyncpg")

from app.adapters.runstate_pg import RunStatePgStore  # noqa: E402
from app.appdb import AppDB  # noqa: E402

# The app-DB Postgres (harness-appdb, postgres:17-alpine, host port 5500, DB
# harness_app). Same DSN appdb.py defaults to; overridable for CI. Acceptable as a
# test-file literal (the no-project-literals gate allow-lists tests/); never baked
# into the adapter, which resolves it from env/the appdb convention.
APPDB_DSN = os.environ.get(
    "HARNESS_APPDB_DSN",
    "postgresql://harness:harness@localhost:5500/harness_app",
)

# A DSN that can never connect — used to prove graceful-degrade.
DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/does_not_exist"

# This suite's OWN scratch DB (distinct from the T3 suite's `runstate_pg_test` so
# the two can run concurrently without colliding).
SCRATCH_DB = "runstate_subscribe_test"

# The migrations the scratch DB is built from (tests[0] → console[1] →
# local-cortex[2] → <repo root>[3]). The runstate migration installs the
# run_state_events trigger; the chat-session migration adds run_state.session_id
# (applied in lexical order, as the harness-appdb-migrate one-shot does) so the
# adapter's start_run INSERT (which now writes session_id) works on the scratch DB.
_APPDB = Path(__file__).resolve().parents[3] / ".agents" / "data" / "appdb"
MIGRATION = _APPDB / "2026-06-05-runstate.sql"
CHAT_SESSION_MIGRATION = _APPDB / "2026-06-07-chat-session.sql"
METADATA_MIGRATION = _APPDB / "2026-06-07-runstate-metadata.sql"

# Bound on every "wait for a notify" so a missed wake fails fast (not a hang).
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
    """Create a fresh `runstate_subscribe_test` DB, apply the migration (so the
    NOTIFY trigger exists), yield a RunStatePgStore whose AppDB pool is pointed at
    it, and DROP the scratch DB on teardown. Skips when the app-DB is unreachable."""
    admin = await _admin_conn()
    if admin is None:
        pytest.skip(
            "app-DB Postgres (localhost:5500) unreachable — skipping RunState "
            "subscribe tests (needs the harness-appdb container up)"
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


async def _prime(agen, timeout: float = WAKE_TIMEOUT) -> "asyncio.Future[str]":
    """Start consuming the subscribe() generator and return a future for its NEXT
    yielded run_id.

    CRITICAL: an async generator's body does NOT run until its first `__anext__()`
    is awaited — so the `LISTEN run_state_events` is only issued once we start
    pulling. This kicks the first pull off as a background task (so the body runs
    and the LISTEN is established) and gives it a short beat to actually take
    effect, THEN returns. A write emitted AFTER awaiting `_prime(...)` is therefore
    guaranteed to be heard. The returned future is bounded by `timeout` so a missed
    NOTIFY fails fast (TimeoutError) instead of hanging. This mirrors how the real
    SSE consumer (T8) drives the generator continuously."""
    fut: "asyncio.Future[str]" = asyncio.ensure_future(
        asyncio.wait_for(agen.__anext__(), timeout=timeout)
    )
    # Let the generator body run far enough to issue its LISTEN before we return.
    await asyncio.sleep(0.3)
    return fut


# ── the wake fires on a run_state change in another connection ────────────────


async def test_subscribe_yields_run_id_on_start_run(store):
    """A primed subscription, then a start_run in ANOTHER connection (the
    orchestrator), makes subscribe() yield that run's run_id — the NOTIFY wake
    reached us."""
    agen = store.subscribe()  # unfiltered
    try:
        wake = await _prime(agen)  # LISTEN is live before we write

        run_id = str(uuid.uuid4())
        # The INSERT fires the AFTER-INSERT trigger → pg_notify('run_state_events').
        await store.start_run(run_id=run_id, project="proj", agent="agt")

        woke = await wake
        assert woke == run_id, "subscribe must yield the changed run's run_id"
    finally:
        await agen.aclose()


async def test_subscribe_yields_run_id_on_append_output(store):
    """append_output bumps run_state.updated_at → AFTER-UPDATE trigger → a wake for
    the same run_id (any change to the run wakes the stream, not just open)."""
    run_id = str(uuid.uuid4())
    await store.start_run(run_id=run_id, project="proj", agent="agt")

    agen = store.subscribe()
    try:
        wake = await _prime(agen)

        # Append in the same store but a different pooled connection → NOTIFY.
        await store.append_output(run_id, seq=1, kind="output", text="hello")

        woke = await wake
        assert woke == run_id
    finally:
        await agen.aclose()


# ── PROJECT-FILTERING — a subscriber only sees its project's wakes ────────────


async def test_subscribe_filters_by_project(store):
    """A project-A subscriber must NOT be woken by a project-B run. We emit B
    first, then A; the FIRST (and only) id the A-subscriber yields must be A's —
    proving B was filtered out, not merely ordered behind A."""
    agen = store.subscribe(project="proj-a")
    try:
        wake = await _prime(agen)

        b_id = str(uuid.uuid4())
        a_id = str(uuid.uuid4())
        # Project B change first — must be filtered out for a project-A listener.
        await store.start_run(run_id=b_id, project="proj-b", agent="agt")
        # Then a project A change — this is the one the A-subscriber should see.
        await store.start_run(run_id=a_id, project="proj-a", agent="agt")

        woke = await wake
        assert woke == a_id, "a project-A subscriber must only yield project-A runs"
        assert woke != b_id, "a project-B run must be filtered out for project A"
    finally:
        await agen.aclose()


async def test_subscribe_unfiltered_sees_all_projects(store):
    """An unfiltered subscriber (project=None) is woken by ANY project's run."""
    agen = store.subscribe()  # no project filter
    try:
        wake = await _prime(agen)

        b_id = str(uuid.uuid4())
        await store.start_run(run_id=b_id, project="proj-b", agent="agt")

        woke = await wake
        assert woke == b_id, "an unfiltered subscriber sees every project's wake"
    finally:
        await agen.aclose()


async def test_subscribe_project_filter_is_case_insensitive(store):
    """Projects are stored lower-cased (start_run lower-cases them), so a filter
    given in mixed case must still match — the subscriber normalises its filter."""
    agen = store.subscribe(project="Proj-A")  # mixed case filter
    try:
        wake = await _prime(agen)

        a_id = str(uuid.uuid4())
        await store.start_run(run_id=a_id, project="proj-a", agent="agt")

        woke = await wake
        assert woke == a_id, "the project filter must be case-insensitive"
    finally:
        await agen.aclose()


# ── GRACEFUL-DEGRADE — a down DB ends the generator cleanly, never raises ─────


async def test_subscribe_dead_dsn_ends_cleanly_no_raise():
    """Pointed at a dead DSN, subscribe() must END (StopAsyncIteration) without
    raising into the consumer — a down app-DB can't break the SSE layer. We assert
    `async for` completes with zero items (and crucially does not propagate the
    connection error)."""
    appdb = AppDB(dsn=DEAD_DSN)
    st = RunStatePgStore(appdb)
    try:
        items: list[str] = []
        # If subscribe() raised the asyncpg connect error, this would blow up.
        async for run_id in st.subscribe(project="proj"):  # pragma: no cover - body
            items.append(run_id)  # unreachable: nothing is ever yielded
        assert items == [], "a dead-DB subscribe yields nothing and ends cleanly"
    finally:
        await appdb.aclose()


async def test_subscribe_dead_dsn_anext_raises_stopasynciteration():
    """The async-generator contract for the degraded path: advancing it raises
    StopAsyncIteration (a clean end), NOT a connection error."""
    appdb = AppDB(dsn=DEAD_DSN)
    st = RunStatePgStore(appdb)
    try:
        agen = st.subscribe(project="proj")
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(agen.__anext__(), timeout=WAKE_TIMEOUT)
    finally:
        await appdb.aclose()


async def test_subscribe_connection_drop_ends_cleanly(store, monkeypatch):
    """If the listener connection DROPS mid-stream (the queue may stay empty
    forever), the generator must END CLEANLY — not block on queue.get() until the
    test times out. The listener now holds its OWN dedicated OFF-POOL connection
    (it no longer borrows a pooled slot — that starved the transactional writers and
    froze chats), so we CAPTURE that connection via `AppDB.connect()` and FORCIBLY
    `terminate()` IT (the realistic 'connection dropped' event), then assert the next
    pull raises StopAsyncIteration (a clean end), never a connection error or a hang.
    (Reconnect-on-drop is T8's job; ending cleanly is the T4 floor.)"""
    import app.adapters.runstate_pg as mod

    # Fast liveness re-check so the drop is noticed promptly (default 5s is too
    # slow for a unit test). Bypasses the env clamp via direct attribute set.
    monkeypatch.setattr(mod, "_SUBSCRIBE_LIVENESS_TICK", 0.2, raising=False)

    # Capture the dedicated listener connection so we can yank IT. The listener
    # opens its connection via `AppDB.connect()` (off-pool), so wrap that to grab
    # the live connection the generator parks on. (Terminating the POOL no longer
    # affects the listener — that's the whole point of the off-pool fix.)
    captured = {}
    real_connect = store._appdb.connect

    async def _capturing_connect():
        conn = await real_connect()
        captured["conn"] = conn
        return conn

    monkeypatch.setattr(store._appdb, "connect", _capturing_connect)

    agen = store.subscribe()
    try:
        wake = await _prime(agen)  # LISTEN is live

        # First, prove it's a working live stream.
        run_id = str(uuid.uuid4())
        await store.start_run(run_id=run_id, project="proj", agent="agt")
        assert await wake == run_id

        # Now YANK the listener's dedicated connection: terminate() abruptly closes
        # it without waiting → is_closed() flips True (or raises 'reclaimed', which
        # the adapter also treats as a clean end). The real dropped-connection event.
        lc = captured.get("conn")
        assert lc is not None
        lc.terminate()

        # The next pull must end cleanly (StopAsyncIteration), bounded so a HANG
        # would surface as a TimeoutError test failure instead.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(agen.__anext__(), timeout=WAKE_TIMEOUT)
    finally:
        await agen.aclose()
