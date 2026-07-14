"""T1 — schema test for the run_state / run_span app-DB migration.

Applies `.agents/data/appdb/2026-06-05-runstate.sql` to a THROWAWAY database
(`runstate_mig_test`, created + dropped here) and asserts the schema it builds:
the two tables, their columns + defaults, the indexes, the `notify_run_state()`
trigger function, and the two AFTER INSERT/UPDATE triggers that pg_notify the
`run_state_events` bus. Then RE-APPLIES the same .sql to prove it is idempotent.

WHY a throwaway DB (never the live `harness_app`): the migration is DDL the
console has never run against the real operational store yet (M1 T1 — "nothing
reads it yet"). Building + tearing down a scratch database keeps this test
hermetic and the live app-DB untouched, exactly as the milestone boundary
requires.

Skips (does NOT fail) when the app-DB Postgres at localhost:5500 is unreachable,
so the suite still runs on a machine without the container up. When the
container IS up (CI / dev box) it runs for real.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import sql  # noqa: E402  (after importorskip)

# The app-DB Postgres (harness-appdb, postgres:17-alpine, host port 5500, DB
# harness_app). Same DSN the console's appdb.py defaults to. Overridable for CI.
# Acceptable as a test-file literal (the no-project-literals gate allow-lists
# tests/); never a project key/hex/path.
APPDB_DSN = os.environ.get(
    "HARNESS_APPDB_DSN",
    "postgresql://harness:harness@localhost:5500/harness_app",
)

# The scratch database this test owns end-to-end.
SCRATCH_DB = "runstate_mig_test"

# The migration under test, resolved from this file's location:
#   tests[0] → console[1] → local-cortex[2] → <repo root>[3].
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / ".agents"
    / "data"
    / "appdb"
    / "2026-06-05-runstate.sql"
)


def _connect(dsn: str):
    """Connect with a short timeout; return None if the DB is unreachable."""
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.autocommit = True
        return conn
    except Exception:
        return None


def _admin_conn():
    """A connection to the default `harness_app` DB used only to CREATE/DROP the
    scratch database (CREATE DATABASE cannot run inside a transaction, hence
    autocommit). Returns None when the server is unreachable."""
    return _connect(APPDB_DSN)


def _apply_sql(dsn: str, sql_text: str) -> None:
    """Apply a whole .sql script to the database at `dsn` (one autocommit
    connection). Raises on any SQL error so an idempotency break is caught."""
    conn = _connect(dsn)
    assert conn is not None, "scratch DB became unreachable mid-test"
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
    finally:
        conn.close()


def _scratch_dsn() -> str:
    """The DSN for the scratch DB (same server/creds, different dbname)."""
    # Swap the trailing /harness_app for /runstate_mig_test.
    base, _, _db = APPDB_DSN.rpartition("/")
    return f"{base}/{SCRATCH_DB}"


@pytest.fixture()
def scratch_db():
    """Create a fresh `runstate_mig_test` DB (drop-if-exists first), yield its
    DSN, and unconditionally DROP it on teardown. Skips the whole test when the
    app-DB server is unreachable (no container) — never fails for that."""
    admin = _admin_conn()
    if admin is None:
        pytest.skip(
            "app-DB Postgres (localhost:5500) unreachable — skipping migration "
            "schema test (needs the harness-appdb container up)"
        )

    ident = sql.Identifier(SCRATCH_DB)
    try:
        with admin.cursor() as cur:
            # Drop any leftover from a previous aborted run, then create fresh.
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(ident)
            )
            cur.execute(sql.SQL("CREATE DATABASE {}").format(ident))
    except Exception as exc:  # pragma: no cover - environment/permission guard
        admin.close()
        pytest.skip(f"could not create scratch DB (insufficient perms?): {exc}")

    try:
        yield _scratch_dsn()
    finally:
        # Always drop the scratch DB. Terminate any stray backends first so the
        # DROP can't be blocked by a lingering connection.
        try:
            with admin.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                      FROM pg_stat_activity
                     WHERE datname = %s AND pid <> pg_backend_pid()
                    """,
                    (SCRATCH_DB,),
                )
                cur.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(ident)
                )
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        admin.close()


def _columns(conn, table: str) -> dict[str, dict]:
    """Map column_name -> {data_type, is_nullable, column_default} for a table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        )
        return {
            r[0]: {"data_type": r[1], "is_nullable": r[2], "column_default": r[3]}
            for r in cur.fetchall()
        }


def _index_names(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def _function_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_proc WHERE proname = %s LIMIT 1", (name,)
        )
        return cur.fetchone() is not None


def _trigger_names(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tgname
              FROM pg_trigger t
              JOIN pg_class c ON c.oid = t.tgrelid
             WHERE c.relname = %s AND NOT t.tgisinternal
            """,
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def test_migration_file_exists():
    """The migration .sql must be present at the agreed app-DB path."""
    assert MIGRATION.is_file(), f"migration missing: {MIGRATION}"


def test_migration_builds_schema(scratch_db):
    """Apply the migration to the scratch DB and assert the full schema, then
    re-apply it and assert it stays green (idempotent)."""
    sql_text = MIGRATION.read_text()

    # ── first apply ──────────────────────────────────────────────────────────
    _apply_sql(scratch_db, sql_text)

    conn = _connect(scratch_db)
    assert conn is not None
    try:
        # -- run_state table + key columns/defaults ---------------------------
        cols = _columns(conn, "run_state")
        assert cols, "run_state table not created"
        expected_cols = {
            "run_id", "project", "agent", "agent_display", "handoff_id",
            "harness", "model", "status", "error", "pid", "lease_owner",
            "tokens_in", "tokens_out", "cost_est_usd",
            "started_at", "updated_at", "heartbeat_at", "ended_at",
        }
        missing = expected_cols - set(cols)
        assert not missing, f"run_state missing columns: {sorted(missing)}"

        # run_id is the PRIMARY KEY (text) and NOT NULL.
        assert cols["run_id"]["is_nullable"] == "NO"
        # status defaults to 'queued'.
        assert "queued" in (cols["status"]["column_default"] or ""), (
            f"status default should be 'queued', got "
            f"{cols['status']['column_default']!r}"
        )
        # the money column is numeric.
        assert cols["cost_est_usd"]["data_type"] == "numeric"
        # timestamps are timestamptz.
        for tcol in ("started_at", "updated_at", "heartbeat_at", "ended_at"):
            assert cols[tcol]["data_type"] == "timestamp with time zone", (
                f"{tcol} should be timestamptz, got {cols[tcol]['data_type']}"
            )

        # run_state PK is on run_id.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.attname
                  FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indrelid
                  JOIN pg_attribute a
                    ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                 WHERE c.relname = 'run_state' AND i.indisprimary
                """
            )
            pk_cols = {r[0] for r in cur.fetchall()}
        assert pk_cols == {"run_id"}, f"run_state PK should be run_id, got {pk_cols}"

        # -- run_span append-only table ---------------------------------------
        span_cols = _columns(conn, "run_span")
        assert span_cols, "run_span table not created"
        for c in ("id", "run_id", "seq", "kind", "text", "ts"):
            assert c in span_cols, f"run_span missing column: {c}"
        # id is BIGSERIAL → bigint with a nextval default.
        assert span_cols["id"]["data_type"] == "bigint"
        assert "nextval" in (span_cols["id"]["column_default"] or "")

        # run_span.run_id has an FK to run_state(run_id) ON DELETE CASCADE.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT confrelid::regclass::text, confdeltype
                  FROM pg_constraint
                 WHERE conrelid = 'run_span'::regclass AND contype = 'f'
                """
            )
            fks = cur.fetchall()
        assert any(
            ref == "run_state" and deltype == "c"  # 'c' = CASCADE
            for (ref, deltype) in fks
        ), f"run_span needs an FK→run_state ON DELETE CASCADE, got {fks}"

        # UNIQUE(run_id, seq) on run_span.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.indkey
                  FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indrelid
                 WHERE c.relname = 'run_span' AND i.indisunique
                """
            )
            unique_indexes = cur.fetchall()
        # There must be at least one unique index spanning two columns (run_id, seq).
        assert any(
            len(str(idx[0]).split()) == 2 for idx in unique_indexes
        ), "run_span needs a UNIQUE(run_id, seq) constraint/index"

        # -- indexes ----------------------------------------------------------
        rs_idx = _index_names(conn, "run_state")
        sp_idx = _index_names(conn, "run_span")
        # At least one secondary index on each table beyond the PK (the plan
        # specifies project/status + handoff lookups on run_state and a
        # (run_id, seq) read index on run_span).
        assert len(rs_idx) >= 2, f"run_state should have secondary indexes: {rs_idx}"
        assert len(sp_idx) >= 1, f"run_span should have an index: {sp_idx}"

        # -- NOTIFY function + triggers ---------------------------------------
        assert _function_exists(conn, "notify_run_state"), (
            "notify_run_state() trigger function not created"
        )
        triggers = _trigger_names(conn, "run_state")
        assert len(triggers) >= 2, (
            f"run_state needs AFTER INSERT + AFTER UPDATE triggers, got {triggers}"
        )
    finally:
        conn.close()

    # ── second apply (idempotency) ───────────────────────────────────────────
    # Re-running the exact same migration must succeed with no error.
    _apply_sql(scratch_db, sql_text)

    # Schema still intact after the re-apply (tables + function + triggers).
    conn = _connect(scratch_db)
    assert conn is not None
    try:
        assert _columns(conn, "run_state"), "run_state vanished after re-apply"
        assert _columns(conn, "run_span"), "run_span vanished after re-apply"
        assert _function_exists(conn, "notify_run_state")
        assert len(_trigger_names(conn, "run_state")) >= 2, (
            "triggers should still exist (and not be duplicated) after re-apply"
        )
    finally:
        conn.close()


def test_notify_fires_on_insert(scratch_db):
    """End-to-end: LISTEN run_state_events, INSERT a run_state row, and assert a
    NOTIFY carrying a JSON payload with the run_id + project is delivered. This
    proves the trigger wiring (not just its existence)."""
    import json
    import select

    _apply_sql(scratch_db, MIGRATION.read_text())

    listener = _connect(scratch_db)
    assert listener is not None
    try:
        with listener.cursor() as cur:
            cur.execute("LISTEN run_state_events")

        writer = _connect(scratch_db)
        assert writer is not None
        try:
            with writer.cursor() as cur:
                cur.execute(
                    "INSERT INTO run_state (run_id, project, agent, status) "
                    "VALUES (%s, %s, %s, %s)",
                    ("run-test-insert", "kaidera-os", "ren", "queued"),
                )
        finally:
            writer.close()

        # Wait briefly for the async NOTIFY to arrive.
        if select.select([listener], [], [], 5) == ([], [], []):
            pytest.fail("no NOTIFY on run_state_events within 5s of an INSERT")
        listener.poll()
        assert listener.notifies, "expected a run_state_events notification"
        note = listener.notifies.pop(0)
        assert note.channel == "run_state_events"
        payload = json.loads(note.payload)
        assert payload.get("run_id") == "run-test-insert"
        assert payload.get("project") == "kaidera-os"
    finally:
        listener.close()
