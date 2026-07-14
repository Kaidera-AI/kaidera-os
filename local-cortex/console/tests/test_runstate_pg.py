"""T3 — tests for the RunStatePort Pg adapter (`app/adapters/runstate_pg.py`).

The adapter implements the pure `RunStatePort` Protocol (`app/domain/runstate.py`,
T2) over the app-DB asyncpg pool (the same `AppDB` pool from `app/appdb.py` — it
does NOT open a second pool). It is the imperative shell: it MAY touch asyncpg;
the domain port stays pure.

These tests run against a THROWAWAY database (reusing T1's pattern —
`test_runstate_migration.py`): an admin connection to the live `harness_app`
CREATEs a scratch DB, applies `.agents/data/appdb/2026-06-05-runstate.sql` to it,
points an `AppDB` (its asyncpg pool) at the scratch DB, and DROPs the scratch DB
on teardown. **The live `harness_app` is only ever used to CREATE/DROP the scratch
DB — never written by the adapter under test.**

Coverage:
  * structural conformance — the adapter satisfies `RunStatePort`,
  * start_run → returns a uuid + a `queued` row,
  * append_output × N → ordered spans, seq monotonic, idempotent on (run_id, seq),
  * set_status → ok/error transitions + tokens/cost + ended_at stamp,
  * heartbeat → bumps heartbeat_at (+ optional tokens/cost/pid),
  * get_run → header + assembled spans (in seq order),
  * list_active → only queued|running (project-scoped, newest-first),
  * recent / by_handoff,
  * the per-run total-chars cap (append_output stops accumulating past the cap),
  * the recent-runs prune (rows beyond N-most-recent-per-project are reclaimed,
    cascading to run_span),
  * GRACEFUL-DEGRADE — pointed at a dead DSN, every method returns empty/None/no-op
    and NEVER raises into the caller (house law, mirrors appdb.py:147-192).

Skips (does NOT fail) when the app-DB Postgres at localhost:5500 is unreachable,
so the suite still runs on a machine without the harness-appdb container up.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

# asyncpg is the adapter's driver; skip the whole module if it (or the DB) is absent.
asyncpg = pytest.importorskip("asyncpg")

from app.adapters.runstate_pg import RunStatePgStore  # noqa: E402
from app.appdb import AppDB  # noqa: E402
from app.domain.runstate import RunRecord, RunStatePort  # noqa: E402

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

# The scratch database this test owns end-to-end (distinct from T1's so the two
# suites can run concurrently without colliding).
SCRATCH_DB = "runstate_pg_test"

# The migrations the scratch DB is built from (tests[0] → console[1] →
# local-cortex[2] → <repo root>[3]), applied in lexical order exactly as the
# harness-appdb-migrate one-shot does — the runstate tables, then the additive
# chat-session column (so start_run/recent can read+write run_state.session_id).
_APPDB = Path(__file__).resolve().parents[3] / ".agents" / "data" / "appdb"
MIGRATION = _APPDB / "2026-06-05-runstate.sql"
CHAT_SESSION_MIGRATION = _APPDB / "2026-06-07-chat-session.sql"
# Explain capability: run_state.metadata JSONB sidecar (so set_status/_record_from_row
# can write+read run_state.metadata). Applied in lexical order, as the migrate one-shot.
METADATA_MIGRATION = _APPDB / "2026-06-07-runstate-metadata.sql"


def _admin_pool_dsn() -> str:
    return APPDB_DSN


def _scratch_dsn() -> str:
    """Same server/creds as APPDB_DSN, but the scratch dbname."""
    base, _, _db = APPDB_DSN.rpartition("/")
    return f"{base}/{SCRATCH_DB}"


async def _admin_conn():
    """A short-timeout asyncpg connection to the live harness_app DB, used ONLY to
    CREATE/DROP the scratch DB. Returns None when the server is unreachable."""
    try:
        return await asyncpg.connect(dsn=_admin_pool_dsn(), timeout=3)
    except Exception:
        return None


@pytest.fixture()
async def store():
    """Create a fresh `runstate_pg_test` DB, apply the migration, yield a
    RunStatePgStore whose AppDB pool is pointed at it, and DROP the scratch DB on
    teardown. Skips the whole test when the app-DB server is unreachable."""
    admin = await _admin_conn()
    if admin is None:
        pytest.skip(
            "app-DB Postgres (localhost:5500) unreachable — skipping RunState Pg "
            "adapter tests (needs the harness-appdb container up)"
        )

    try:
        # Drop a leftover from an aborted run, then create fresh. (CREATE/DROP
        # DATABASE can't run inside a txn; asyncpg.execute is autocommit here.)
        await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
        await admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    except Exception as exc:  # pragma: no cover - env/permission guard
        await admin.close()
        pytest.skip(f"could not create scratch DB (insufficient perms?): {exc}")

    # Apply the migrations to the scratch DB on a throwaway direct connection, in
    # lexical order (runstate tables, then the additive session_id column).
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
        # Always drop the scratch DB; terminate stray backends first.
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


# ── structural conformance ───────────────────────────────────────────────────


def test_adapter_satisfies_port():
    """The adapter is structurally a RunStatePort (the swap-in contract)."""
    appdb = AppDB(dsn=DEAD_DSN)
    st = RunStatePgStore(appdb)
    assert isinstance(st, RunStatePort)


def test_adapter_does_not_open_a_second_pool():
    """The adapter reuses the AppDB pool — it holds the SAME AppDB instance it was
    given (no private pool of its own)."""
    appdb = AppDB(dsn=DEAD_DSN)
    st = RunStatePgStore(appdb)
    assert st._appdb is appdb


# ── start_run ────────────────────────────────────────────────────────────────


async def test_start_run_returns_uuid_and_queued_row(store):
    run_id = str(uuid.uuid4())
    rec = await store.start_run(
        run_id=run_id,
        project="proj",
        agent="agt",
        agent_display="Agt",
        handoff_id="hoff-1",
        harness="claude-code",
        model="opus",
        pid=4321,
    )
    assert isinstance(rec, RunRecord)
    assert rec.run_id == run_id
    # run_id is a uuid4 the caller generated.
    assert uuid.UUID(rec.run_id).version == 4
    assert rec.status == "queued"
    assert rec.project == "proj"
    assert rec.agent == "agt"
    assert rec.handoff_id == "hoff-1"
    assert rec.harness == "claude-code"
    assert rec.model == "opus"
    assert rec.pid == 4321

    # It is durably readable back.
    again = await store.get_run(run_id)
    assert again is not None
    assert again.run_id == run_id
    assert again.status == "queued"


async def test_start_run_is_idempotent_on_run_id(store):
    """Re-opening the same run_id is safe (UPSERT), not a duplicate-key crash."""
    run_id = str(uuid.uuid4())
    await store.start_run(run_id=run_id, project="proj", agent="agt")
    # Second call must not raise and must keep a single row.
    rec = await store.start_run(run_id=run_id, project="proj", agent="agt2")
    assert rec.run_id == run_id
    got = await store.get_run(run_id)
    assert got is not None


# ── append_output ────────────────────────────────────────────────────────────


async def test_append_output_orders_spans_seq_monotonic(store):
    run_id = str(uuid.uuid4())
    await store.start_run(run_id=run_id, project="proj", agent="agt")
    await store.append_output(run_id, seq=1, kind="think", text="a")
    await store.append_output(run_id, seq=2, kind="tool", text="b")
    await store.append_output(run_id, seq=3, kind="output", text="c")

    rec = await store.get_run(run_id)
    assert rec is not None
    seqs = [s.seq for s in rec.spans]
    assert seqs == [1, 2, 3], f"spans must be seq-ordered, got {seqs}"
    assert [s.kind for s in rec.spans] == ["think", "tool", "output"]
    assert [s.text for s in rec.spans] == ["a", "b", "c"]


async def test_append_output_idempotent_on_run_id_seq(store):
    """A re-delivered (run_id, seq) is a NO-OP — never a duplicate, never a crash
    (the UNIQUE(run_id, seq) collision-mitigation)."""
    run_id = str(uuid.uuid4())
    await store.start_run(run_id=run_id, project="proj", agent="agt")
    await store.append_output(run_id, seq=1, kind="output", text="first")
    # Same seq again with different text — must be ignored, original kept.
    await store.append_output(run_id, seq=1, kind="output", text="SECOND")

    rec = await store.get_run(run_id)
    assert rec is not None
    assert len(rec.spans) == 1, "duplicate seq must not create a second span"
    assert rec.spans[0].text == "first", "first write wins; re-delivery is a no-op"


# ── set_status ───────────────────────────────────────────────────────────────


async def test_set_status_running_then_ok_with_tokens(store):
    run_id = str(uuid.uuid4())
    await store.start_run(run_id=run_id, project="proj", agent="agt")
    await store.set_status(run_id, "running")
    rec = await store.get_run(run_id)
    assert rec is not None and rec.status == "running"
    assert rec.ended_at is None, "a non-terminal status must not stamp ended_at"

    # tokens/cost arrive on the run header via heartbeat; status walks to ok.
    await store.heartbeat(run_id, tokens_in=10, tokens_out=20, cost_est_usd=0.5)
    await store.set_status(run_id, "ok")
    done = await store.get_run(run_id)
    assert done is not None
    assert done.status == "ok"
    assert done.ended_at is not None, "a terminal status stamps ended_at"
    assert done.tokens_in == 10
    assert done.tokens_out == 20
    assert float(done.cost_est_usd) == 0.5


async def test_set_status_error_carries_detail(store):
    run_id = str(uuid.uuid4())
    await store.start_run(run_id=run_id, project="proj", agent="agt")
    await store.set_status(run_id, "error", error="boom")
    rec = await store.get_run(run_id)
    assert rec is not None
    assert rec.status == "error"
    assert rec.error == "boom"
    assert rec.ended_at is not None


# ── heartbeat ────────────────────────────────────────────────────────────────


async def test_heartbeat_bumps_heartbeat_at(store):
    run_id = str(uuid.uuid4())
    await store.start_run(run_id=run_id, project="proj", agent="agt")
    before = await store.get_run(run_id)
    assert before is not None
    assert before.heartbeat_at is None, "no heartbeat until the worker pings"

    await store.heartbeat(run_id, pid=999, tokens_in=5)
    after = await store.get_run(run_id)
    assert after is not None
    assert after.heartbeat_at is not None, "heartbeat must stamp heartbeat_at"
    assert after.pid == 999
    assert after.tokens_in == 5


# ── readers ──────────────────────────────────────────────────────────────────


async def test_list_active_shows_only_queued_or_running(store):
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    c = str(uuid.uuid4())
    await store.start_run(run_id=a, project="proj", agent="agt")  # queued
    await store.start_run(run_id=b, project="proj", agent="agt")
    await store.set_status(b, "running")
    await store.start_run(run_id=c, project="proj", agent="agt")
    await store.set_status(c, "ok")  # terminal — excluded

    active = await store.list_active(project="proj")
    ids = {r.run_id for r in active}
    assert a in ids and b in ids, "queued + running runs are active"
    assert c not in ids, "a terminal (ok) run is NOT active"
    # Headers only (no hydrated body) on the list view.
    assert all(r.spans == [] for r in active)


async def test_list_active_project_scoped(store):
    here = str(uuid.uuid4())
    other = str(uuid.uuid4())
    await store.start_run(run_id=here, project="proj", agent="agt")
    await store.start_run(run_id=other, project="elsewhere", agent="agt")

    scoped = await store.list_active(project="proj")
    ids = {r.run_id for r in scoped}
    assert here in ids
    assert other not in ids, "list_active must honour the project filter"


async def test_recent_newest_first_headers_only(store):
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for rid in ids:
        await store.start_run(run_id=rid, project="proj", agent="agt")

    recent = await store.recent(project="proj", limit=10)
    got = [r.run_id for r in recent]
    # Newest-first → reverse insertion order.
    assert got[:3] == list(reversed(ids)), f"recent must be newest-first: {got}"
    assert all(r.spans == [] for r in recent), "recent returns headers only"


async def test_recent_respects_limit(store):
    for _ in range(5):
        await store.start_run(run_id=str(uuid.uuid4()), project="proj", agent="agt")
    recent = await store.recent(project="proj", limit=2)
    assert len(recent) == 2


async def test_recent_filters_by_lease_owner(store):
    """`recent(lease_owner=…)` scopes the read to runs holding that lease — the explain
    gallery enumerates `lease_owner='explain'` runs through it (NOT Cortex search). The
    filter is newest-first, header-only, and surfaces the run's `metadata` sidecar so
    the gallery can read each run's `artifact_id`. None (the default) is unchanged."""
    # Two explain runs (each stamped with an artifact_id sidecar) + a non-explain run.
    e1 = str(uuid.uuid4())
    e2 = str(uuid.uuid4())
    other = str(uuid.uuid4())
    await store.start_run(run_id=e1, project="proj", agent="console", lease_owner="explain")
    await store.set_status(e1, "ok", metadata={"capability": "explain", "artifact_id": "art-1"})
    await store.start_run(run_id=e2, project="proj", agent="console", lease_owner="explain")
    await store.set_status(e2, "ok", metadata={"capability": "explain", "artifact_id": "art-2"})
    # A normal autonomous run (no explain lease) must be excluded.
    await store.start_run(run_id=other, project="proj", agent="agt")

    explains = await store.recent(project="proj", limit=50, lease_owner="explain")
    ids = [r.run_id for r in explains]
    assert other not in ids, "recent(lease_owner='explain') must exclude non-explain runs"
    assert set(ids) == {e1, e2}
    # newest-first: e2 opened after e1.
    assert ids[0] == e2 and ids[1] == e1
    assert all(r.spans == [] for r in explains), "lease-scoped recent returns headers only"
    # the metadata sidecar (artifact_id) rides on the header so the gallery reads it.
    by_id = {r.run_id: r for r in explains}
    assert by_id[e1].metadata == {"capability": "explain", "artifact_id": "art-1"}
    assert by_id[e2].metadata == {"capability": "explain", "artifact_id": "art-2"}

    # No-lease recent is unchanged (sees ALL three runs).
    everything = await store.recent(project="proj", limit=50)
    assert {r.run_id for r in everything} >= {e1, e2, other}


async def test_by_handoff_returns_latest_with_body(store):
    hoff = "hoff-xyz"
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    await store.start_run(run_id=first, project="proj", agent="agt", handoff_id=hoff)
    await store.append_output(first, seq=1, kind="output", text="old")
    await store.start_run(run_id=second, project="proj", agent="agt", handoff_id=hoff)
    await store.append_output(second, seq=1, kind="output", text="new")

    rec = await store.by_handoff(hoff)
    assert rec is not None
    assert rec.run_id == second, "latest run for a handoff wins"
    assert [s.text for s in rec.spans] == ["new"], "by_handoff hydrates the body"


async def test_by_handoff_unknown_returns_none(store):
    assert await store.by_handoff("no-such-handoff") is None


async def test_get_run_unknown_returns_none(store):
    assert await store.get_run(str(uuid.uuid4())) is None


# ── the per-run total-chars cap ──────────────────────────────────────────────


async def test_append_output_enforces_per_run_char_cap(store, monkeypatch):
    """A run can never grow unbounded: once the per-run char total hits the cap,
    further appended text is dropped (the SQL port of the in-memory byte cap)."""
    import app.adapters.runstate_pg as mod

    # Shrink the cap for a fast test.
    monkeypatch.setattr(mod, "RUN_MAX_CHARS", 10, raising=False)

    run_id = str(uuid.uuid4())
    await store.start_run(run_id=run_id, project="proj", agent="agt")
    await store.append_output(run_id, seq=1, kind="output", text="12345")  # 5
    await store.append_output(run_id, seq=2, kind="output", text="67890")  # 10 (== cap)
    await store.append_output(run_id, seq=3, kind="output", text="OVERFLOW")  # dropped

    rec = await store.get_run(run_id)
    assert rec is not None
    total = sum(len(s.text) for s in rec.spans)
    assert total <= 10, f"per-run chars must be capped at 10, got {total}"
    # The overflow span must not have landed.
    assert all("OVERFLOW" not in s.text for s in rec.spans)


# ── the recent-runs prune (bounding run_state per project) ───────────────────


async def test_prune_keeps_n_most_recent_per_project(store, monkeypatch):
    """prune_old trims run_state to the N-most-recent rows per project and cascades
    to run_span (the SQL port of the in-memory deque(maxlen=N))."""
    import app.adapters.runstate_pg as mod

    monkeypatch.setattr(mod, "RUN_MAX_RUNS", 3, raising=False)

    ids = []
    for _ in range(5):
        rid = str(uuid.uuid4())
        ids.append(rid)
        await store.start_run(run_id=rid, project="proj", agent="agt")
        await store.append_output(rid, seq=1, kind="output", text="x")

    pruned = await store.prune_old(project="proj")
    assert pruned >= 2, "two oldest runs (5 - keep 3) should be reclaimed"

    remaining = await store.recent(project="proj", limit=50)
    rem_ids = {r.run_id for r in remaining}
    assert len(rem_ids) == 3, "only the 3 newest runs survive"
    # The newest 3 survive; the 2 oldest are gone.
    assert set(ids[2:]) == rem_ids
    assert ids[0] not in rem_ids and ids[1] not in rem_ids

    # Cascade: the oldest run's spans went with it.
    gone = await store.get_run(ids[0])
    assert gone is None


async def test_prune_old_scopes_per_project(store, monkeypatch):
    """Pruning one project never touches another project's runs."""
    import app.adapters.runstate_pg as mod

    monkeypatch.setattr(mod, "RUN_MAX_RUNS", 1, raising=False)

    keep_other = str(uuid.uuid4())
    await store.start_run(run_id=keep_other, project="other", agent="agt")

    for _ in range(3):
        await store.start_run(run_id=str(uuid.uuid4()), project="proj", agent="agt")

    await store.prune_old(project="proj")

    other_runs = await store.recent(project="other", limit=10)
    assert keep_other in {r.run_id for r in other_runs}, (
        "pruning 'proj' must not reclaim 'other' runs"
    )


async def test_prune_old_scopes_per_project_and_lease_owner(store, monkeypatch):
    """Chat/explain/worker lanes must not evict each other's evidence."""
    import app.adapters.runstate_pg as mod

    monkeypatch.setattr(mod, "RUN_MAX_RUNS", 1, raising=False)

    worker_ids = []
    chat_ids = []
    for _ in range(2):
        rid = str(uuid.uuid4())
        worker_ids.append(rid)
        await store.start_run(run_id=rid, project="proj", agent="worker", lease_owner="worker")
    for _ in range(2):
        rid = str(uuid.uuid4())
        chat_ids.append(rid)
        await store.start_run(run_id=rid, project="proj", agent="lead", lease_owner="chat")

    await store.prune_old(project="proj")

    remaining = await store.recent(project="proj", limit=20)
    rem_ids = {r.run_id for r in remaining}
    assert worker_ids[1] in rem_ids
    assert chat_ids[1] in rem_ids
    assert worker_ids[0] not in rem_ids
    assert chat_ids[0] not in rem_ids


# ── GRACEFUL-DEGRADE (house law: a down app-DB never raises into a caller) ────


async def test_graceful_degrade_all_methods_no_raise():
    """Pointed at a dead DSN, every method returns empty/None/no-op and NEVER
    raises (mirrors appdb.py:147-192). A down app-DB can't break a run."""
    appdb = AppDB(dsn=DEAD_DSN)
    st = RunStatePgStore(appdb)
    run_id = str(uuid.uuid4())

    # Writers: no-op, no raise. start_run still returns a RunRecord (the in-memory
    # shape the caller can use) even though the DB write didn't land.
    rec = await st.start_run(run_id=run_id, project="proj", agent="agt")
    assert isinstance(rec, RunRecord)
    assert rec.run_id == run_id

    await st.append_output(run_id, seq=1, kind="output", text="x")  # no raise
    await st.set_status(run_id, "running")  # no raise
    await st.heartbeat(run_id, pid=1)  # no raise

    # Readers: empty/None, no raise.
    assert await st.get_run(run_id) is None
    assert await st.list_active(project="proj") == []
    assert await st.recent(project="proj") == []
    assert await st.by_handoff("hoff") is None
    assert await st.prune_old(project="proj") == 0

    await appdb.aclose()


# ── subscribe (T4 — LISTEN run_state_events) ─────────────────────────────────
#
# subscribe() is now IMPLEMENTED (T4). The live-push behaviour (a NOTIFY wake
# yields a run_id, project-filtering) is covered in test_runstate_subscribe.py
# against the throwaway DB. Here we only assert the GRACEFUL-DEGRADE contract that
# belongs with the rest of this suite's dead-DSN checks: a down app-DB ends the
# generator cleanly (StopAsyncIteration), never raising into the consumer.


async def test_subscribe_dead_dsn_ends_cleanly():
    """subscribe() against a down app-DB yields nothing and ends cleanly (no raise)
    — the SSE layer (T8) can't be broken by a dead DB. (Live-push behaviour is in
    test_runstate_subscribe.py.)"""
    appdb = AppDB(dsn=DEAD_DSN)
    st = RunStatePgStore(appdb)
    try:
        agen = st.subscribe(project="proj")
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
    finally:
        await appdb.aclose()
