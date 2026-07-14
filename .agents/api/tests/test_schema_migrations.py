import importlib.util
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_schema_migrations_test",
        API_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MigrationConn:
    def __init__(self, applied=None):
        self.applied = dict(applied or {})
        self.executed_migration_sql = []

    async def fetch(self, sql, *args):
        if "FROM cortex_schema_migrations" in sql:
            return list(self.applied.values())
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def execute(self, sql, *args):
        if "CREATE TABLE IF NOT EXISTS cortex_schema_migrations" in sql:
            return "CREATE TABLE"
        if "ALTER TABLE cortex_schema_migrations OWNER TO postgres" in sql:
            return "ALTER TABLE"
        if "GRANT SELECT ON TABLE cortex_schema_migrations" in sql:
            return "DO"
        if "INSERT INTO cortex_schema_migrations" in sql:
            migration_id, checksum, source_path, applied_by, statement_status, surface_version = args
            self.applied[migration_id] = {
                "migration_id": migration_id,
                "checksum_sha256": checksum,
                "source_path": source_path,
                "applied_by": applied_by,
                "applied_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "statement_status": statement_status,
                "surface_version": surface_version,
            }
            return "INSERT 0 1"
        if "SELECT 1 AS migration_test" in sql:
            self.executed_migration_sql.append(sql)
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")


def write_migration(root: Path, name: str, sql: str = "SELECT 1 AS migration_test;\n") -> Path:
    path = root / name
    path.write_text(sql, encoding="utf-8")
    return path


def applied_row(migration_id: str, path: Path, checksum: str) -> dict:
    return {
        "migration_id": migration_id,
        "checksum_sha256": checksum,
        "source_path": str(path),
        "applied_by": "old-runner",
        "applied_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "statement_status": "SELECT 1",
        "surface_version": "old",
    }


@pytest.mark.asyncio
async def test_apply_schema_migrations_dry_run_lists_pending_without_execution(tmp_path):
    api = load_api_module()
    write_migration(tmp_path, "2026-06-01-alpha.sql")
    write_migration(tmp_path, "2026-06-02-beta.sql")
    conn = MigrationConn()

    result = await api.apply_schema_migrations(conn, dry_run=True, migration_dir=tmp_path)

    assert result["dry_run"] is True
    assert result["applied_count"] == 0
    assert [row["id"] for row in result["results"]] == [
        "2026-06-01-alpha.sql",
        "2026-06-02-beta.sql",
    ]
    assert {row["action"] for row in result["results"]} == {"would_apply"}
    assert conn.executed_migration_sql == []


@pytest.mark.asyncio
async def test_apply_schema_migrations_executes_and_records_ledger(tmp_path):
    api = load_api_module()
    write_migration(tmp_path, "2026-06-01-alpha.sql")
    conn = MigrationConn()

    applied = await api.apply_schema_migrations(
        conn,
        dry_run=False,
        migration_dir=tmp_path,
        applied_by="test-runner",
    )
    rerun = await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert applied["applied_count"] == 1
    assert applied["results"][0]["action"] == "applied"
    assert len(conn.executed_migration_sql) == 1
    assert conn.applied["2026-06-01-alpha.sql"]["applied_by"] == "test-runner"
    assert conn.applied["2026-06-01-alpha.sql"]["surface_version"] == api.CORTEX_SURFACE_VERSION
    assert rerun["applied_count"] == 0
    assert rerun["results"][0]["action"] == "skip_applied"


@pytest.mark.asyncio
async def test_apply_schema_migrations_checksum_mismatch_blocks_apply(tmp_path):
    api = load_api_module()
    path = write_migration(tmp_path, "2026-06-01-alpha.sql")
    conn = MigrationConn(
        {
            "2026-06-01-alpha.sql": {
                "migration_id": "2026-06-01-alpha.sql",
                "checksum_sha256": "not-the-current-checksum",
                "source_path": str(path),
                "applied_by": "old-runner",
                "applied_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "statement_status": "SELECT 1",
                "surface_version": "old",
            }
        }
    )

    with pytest.raises(HTTPException) as exc:
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert exc.value.status_code == 409
    assert conn.executed_migration_sql == []


@pytest.mark.asyncio
async def test_ordered_renamed_migration_uses_legacy_ledger_id_when_checksum_matches(tmp_path):
    api = load_api_module()
    path = write_migration(tmp_path, "2026-06-15-identity-v2-1-foundation.sql")
    checksum = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    conn = MigrationConn(
        {
            "2026-06-15-identity-v2-foundation.sql": applied_row(
                "2026-06-15-identity-v2-foundation.sql",
                path,
                checksum,
            )
        }
    )

    result = await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert result["applied_count"] == 0
    assert result["results"][0]["action"] == "skip_applied"
    assert result["results"][0]["applied_migration_id"] == "2026-06-15-identity-v2-foundation.sql"
    assert conn.executed_migration_sql == []


@pytest.mark.asyncio
async def test_ordered_renamed_migration_legacy_checksum_mismatch_blocks_apply(tmp_path):
    api = load_api_module()
    path = write_migration(tmp_path, "2026-06-15-identity-v2-1-foundation.sql")
    conn = MigrationConn(
        {
            "2026-06-15-identity-v2-foundation.sql": applied_row(
                "2026-06-15-identity-v2-foundation.sql",
                path,
                "not-the-current-checksum",
            )
        }
    )

    with pytest.raises(HTTPException) as exc:
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert exc.value.status_code == 409
    assert conn.executed_migration_sql == []


def test_schema_migration_files_rejects_unsafe_filename(tmp_path):
    api = load_api_module()
    write_migration(tmp_path, "not-versioned.sql")

    with pytest.raises(HTTPException) as exc:
        api.schema_migration_files(tmp_path)

    assert exc.value.status_code == 500
