"""Schema + round-trip test for the run_state.metadata JSONB migration (Explain).

Applies the runstate migrations IN ORDER (`2026-06-05-runstate.sql` →
`2026-06-07-chat-session.sql` → `2026-06-07-runstate-metadata.sql`) to a THROWAWAY
database, then asserts the `metadata` migration's contract:
  * the `run_state.metadata` column exists and is JSONB + NULLABLE,
  * re-applying the metadata migration is idempotent (ADD COLUMN IF NOT EXISTS),
  * the `RunStatePgStore.set_status(metadata=...)` adapter ROUND-TRIPS the sidecar (a
    later get_run reads back the dict), while `set_status(...)` with no metadata leaves
    an existing sidecar UNTOUCHED (the COALESCE contract).

Skips (does NOT fail) when the app-DB Postgres at localhost:5500 is unreachable, so the
suite still runs on a machine without the container up.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import sql  # noqa: E402

APPDB_DSN = os.environ.get(
    "HARNESS_APPDB_DSN",
    "postgresql://harness:harness@localhost:5500/harness_app",
)
SCRATCH_DB = "runstate_meta_test"

_APPDB = Path(__file__).resolve().parents[3] / ".agents" / "data" / "appdb"
_MIGRATIONS = (
    _APPDB / "2026-06-05-runstate.sql",
    _APPDB / "2026-06-07-chat-session.sql",
    _APPDB / "2026-06-07-runstate-metadata.sql",
)
_METADATA_MIGRATION = _APPDB / "2026-06-07-runstate-metadata.sql"


def _connect(dsn: str):
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.autocommit = True
        return conn
    except Exception:
        return None


def _scratch_dsn() -> str:
    base, _, _db = APPDB_DSN.rpartition("/")
    return f"{base}/{SCRATCH_DB}"


def _apply_sql(dsn: str, sql_text: str) -> None:
    conn = _connect(dsn)
    assert conn is not None, "scratch DB became unreachable mid-test"
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
    finally:
        conn.close()


@pytest.fixture()
def scratch_dsn():
    """Create a fresh scratch DB, apply the three migrations in order, yield the DSN,
    and drop it on teardown. Skips when the app-DB server is unreachable."""
    admin = _connect(APPDB_DSN)
    if admin is None:
        pytest.skip(
            "app-DB Postgres (localhost:5500) unreachable — skipping metadata "
            "migration test (needs the harness-appdb container up)"
        )
    ident = sql.Identifier(SCRATCH_DB)
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(ident))
            cur.execute(sql.SQL("CREATE DATABASE {}").format(ident))
    except Exception as exc:  # pragma: no cover - perms guard
        admin.close()
        pytest.skip(f"could not create scratch DB: {exc}")

    dsn = _scratch_dsn()
    for mig in _MIGRATIONS:
        _apply_sql(dsn, mig.read_text())
    try:
        yield dsn
    finally:
        try:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (SCRATCH_DB,),
                )
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(ident))
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        admin.close()


def _column(conn, table: str, col: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
            """,
            (table, col),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"data_type": row[0], "is_nullable": row[1]}


def test_metadata_column_is_jsonb_nullable(scratch_dsn):
    conn = _connect(scratch_dsn)
    assert conn is not None
    try:
        col = _column(conn, "run_state", "metadata")
    finally:
        conn.close()
    assert col is not None, "run_state.metadata column must exist after the migration"
    assert col["data_type"] == "jsonb"
    assert col["is_nullable"] == "YES"


def test_metadata_migration_is_idempotent(scratch_dsn):
    # Re-applying the metadata migration must be a clean no-op (ADD COLUMN IF NOT EXISTS).
    _apply_sql(scratch_dsn, _METADATA_MIGRATION.read_text())
    conn = _connect(scratch_dsn)
    assert conn is not None
    try:
        col = _column(conn, "run_state", "metadata")
    finally:
        conn.close()
    assert col is not None and col["data_type"] == "jsonb"


@pytest.mark.asyncio
async def test_set_status_metadata_round_trips(scratch_dsn, monkeypatch):
    """The adapter writes + reads back the metadata sidecar; a later set_status with no
    metadata leaves it UNTOUCHED (the COALESCE contract)."""
    monkeypatch.setenv("HARNESS_APPDB_DSN", scratch_dsn)
    monkeypatch.setenv("APPDB_DSN", scratch_dsn)
    from app.appdb import AppDB
    from app.adapters.runstate_pg import RunStatePgStore

    store = RunStatePgStore(AppDB(dsn=scratch_dsn))
    try:
        await store.start_run(
            run_id="rid-meta", project="kaidera-os", agent="kai", lease_owner="explain",
        )
        # Stamp the sidecar on terminal success.
        await store.set_status("rid-meta", "ok", metadata={"artifact_id": "art-xyz",
                                                            "capability": "explain"})
        rec = await store.get_run("rid-meta")
        assert rec is not None
        assert rec.metadata == {"artifact_id": "art-xyz", "capability": "explain"}
        assert rec.status == "ok"

        # A subsequent status change with NO metadata must leave the sidecar intact.
        await store.set_status("rid-meta", "ok")  # no metadata
        rec2 = await store.get_run("rid-meta")
        assert rec2 is not None
        assert rec2.metadata == {"artifact_id": "art-xyz", "capability": "explain"}, (
            "set_status with no metadata must NOT clear the existing sidecar (COALESCE)"
        )
    finally:
        with __import__("contextlib").suppress(Exception):
            await store._appdb.aclose()
