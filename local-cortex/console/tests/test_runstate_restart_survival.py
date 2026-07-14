"""T13 — THE RESTART-SURVIVAL PROOF (the headline deliverable of Milestone 1).

This is the test the whole milestone was for. M1 moved live run-state out of
process memory into the durable app-DB (`run_state` + `run_span`). The payoff
this proves: **a console restart cannot lose run-state** — because the state
lives in the durable app-DB, not in the process.

How the proof simulates a console restart WITHOUT touching the live `harness_app`:
  1. Build a THROWAWAY scratch DB (reusing the T1/T3 pattern — the live
     `harness_app` is used ONLY to CREATE/DROP the scratch DB, never written by
     the store under test), and apply the run-state migration to it.
  2. Open ONE `RunStatePgStore` over a FIRST `AppDB` pool (connection A) and write
     a run through the real port: `start_run` → `append_output ×N` →
     `set_status('running')` → `heartbeat(...)`. This is "the console, mid-run."
  3. **Simulate the process restart:** `aclose()` the first AppDB pool and drop
     every reference to it. There is now NOTHING in process memory holding the run
     — exactly the state after a `docker compose restart console` (or a crash):
     the old pool/connection is gone, the old process is gone.
  4. Construct a COMPLETELY FRESH `RunStatePgStore` over a SECOND, brand-new
     `AppDB` pool (connection B) on the SAME DSN — the process-restart analogue
     (a new uvicorn process opens a new pool to the same app-DB).
  5. `get_run(run_id)` on the fresh store and assert the run header + ALL its
     spans + its status PERSIST, byte-for-byte. The fresh process re-reads the
     run it never wrote.

If run-state lived in process memory (the pre-M1 in-memory `TranscriptStore`),
step 4's fresh store would know nothing — `get_run` would return None. That it
returns the full run is the proof the SSOT shift delivered.

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
from app.domain.runstate import RunRecord  # noqa: E402

# The app-DB Postgres (harness-appdb, postgres:17-alpine, host port 5500, DB
# harness_app). Same DSN appdb.py defaults to; overridable for CI. A test-file
# literal is allow-listed by the no-project-literals gate (tests/); never baked
# into the adapter, which resolves it from env/the appdb convention.
APPDB_DSN = os.environ.get(
    "HARNESS_APPDB_DSN",
    "postgresql://harness:harness@localhost:5500/harness_app",
)

# A scratch DB this proof owns end-to-end — distinct from the other suites' scratch
# names so all the runstate suites can run concurrently without colliding.
SCRATCH_DB = "runstate_restart_survival_test"

# The migrations the scratch DB is built from (tests[0] → console[1] →
# local-cortex[2] → <repo root>[3]), applied in lexical order (runstate tables,
# then the additive run_state.session_id column) exactly as harness-appdb-migrate does.
_APPDB = Path(__file__).resolve().parents[3] / ".agents" / "data" / "appdb"
MIGRATION = _APPDB / "2026-06-05-runstate.sql"
CHAT_SESSION_MIGRATION = _APPDB / "2026-06-07-chat-session.sql"
METADATA_MIGRATION = _APPDB / "2026-06-07-runstate-metadata.sql"


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
async def scratch_dsn():
    """Create a fresh scratch DB, apply the run-state migration, yield its DSN, and
    DROP it on teardown. The DSN (not a pre-built store) is yielded so the PROOF
    can construct/dispose stores itself — that is the restart simulation.

    Skips the whole test when the app-DB server is unreachable."""
    admin = await _admin_conn()
    if admin is None:
        pytest.skip(
            "app-DB Postgres (localhost:5500) unreachable — skipping restart-"
            "survival proof (needs the harness-appdb container up)"
        )

    try:
        # CREATE/DROP DATABASE can't run inside a txn; asyncpg.execute is autocommit.
        await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
        await admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    except Exception as exc:  # pragma: no cover - env/permission guard
        await admin.close()
        pytest.skip(f"could not create scratch DB (insufficient perms?): {exc}")

    # Apply the migrations to the scratch DB on a throwaway direct connection.
    mig_conn = await asyncpg.connect(dsn=_scratch_dsn(), timeout=3)
    try:
        await mig_conn.execute(MIGRATION.read_text())
        await mig_conn.execute(CHAT_SESSION_MIGRATION.read_text())
        await mig_conn.execute(METADATA_MIGRATION.read_text())
    finally:
        await mig_conn.close()

    try:
        yield _scratch_dsn()
    finally:
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


# ── THE PROOF ────────────────────────────────────────────────────────────────


async def test_run_state_survives_a_console_restart(scratch_dsn):
    """Write a run on store/pool A, dispose it (the restart), then read the run on
    a FRESH store/pool B (same DSN). The run + spans + status MUST persist.

    This is the headline assertion of Milestone 1: run-state lives in the durable
    app-DB, so a console restart cannot lose it.
    """
    run_id = str(uuid.uuid4())
    handoff_id = "hoff-restart-proof"

    # ── 1. "The console, mid-run." Open store A over pool A and write a run. ──
    appdb_a = AppDB(dsn=scratch_dsn)
    store_a = RunStatePgStore(appdb_a)

    rec = await store_a.start_run(
        run_id=run_id,
        project="proj",
        agent="agt",
        agent_display="Agt",
        handoff_id=handoff_id,
        harness="claude-code",
        model="opus",
        pid=4321,
    )
    assert isinstance(rec, RunRecord)
    assert rec.run_id == run_id

    await store_a.append_output(run_id, seq=1, kind="think", text="planning the task")
    await store_a.append_output(run_id, seq=2, kind="tool", text="ran a search")
    await store_a.append_output(run_id, seq=3, kind="output", text="here is the result")
    await store_a.set_status(run_id, "running")
    await store_a.heartbeat(run_id, pid=4321, tokens_in=120, tokens_out=340)

    # Sanity: store A (the "old process") sees its own write before the restart.
    pre = await store_a.get_run(run_id)
    assert pre is not None
    assert pre.status == "running"
    assert [s.text for s in pre.spans] == [
        "planning the task",
        "ran a search",
        "here is the result",
    ]

    # ── 2. SIMULATE THE CONSOLE RESTART. ──
    # Dispose pool A and drop every reference to store A / appdb A. Nothing in
    # process memory now holds the run — the analogue of the old uvicorn process
    # (and its pool/connection) being gone after `docker compose restart console`.
    await appdb_a.aclose()
    del store_a
    del appdb_a

    # ── 3. THE FRESH PROCESS. Construct a brand-new store over a brand-new pool ──
    # on the SAME DSN (a new uvicorn process opening a new pool to the same app-DB).
    appdb_b = AppDB(dsn=scratch_dsn)
    store_b = RunStatePgStore(appdb_b)
    try:
        # ── 4. THE ASSERTION: the fresh process re-reads the run it never wrote. ──
        survived = await store_b.get_run(run_id)
        assert survived is not None, (
            "RESTART LOST THE RUN — get_run returned None on a fresh store/pool. "
            "Run-state did NOT survive the restart (this is the failure M1 fixes)."
        )

        # Header persisted.
        assert survived.run_id == run_id
        assert survived.project == "proj"
        assert survived.agent == "agt"
        assert survived.agent_display == "Agt"
        assert survived.handoff_id == handoff_id
        assert survived.harness == "claude-code"
        assert survived.model == "opus"

        # Status persisted (the live state the UI shows).
        assert survived.status == "running", (
            f"status must survive the restart; got {survived.status!r}"
        )

        # Telemetry from the heartbeat persisted.
        assert survived.pid == 4321
        assert survived.tokens_in == 120
        assert survived.tokens_out == 340
        assert survived.heartbeat_at is not None, (
            "the worker's heartbeat (the watchdog's liveness signal) must survive"
        )

        # ALL spans (the transcript body) persisted, in order.
        assert [s.seq for s in survived.spans] == [1, 2, 3]
        assert [s.kind for s in survived.spans] == ["think", "tool", "output"]
        assert [s.text for s in survived.spans] == [
            "planning the task",
            "ran a search",
            "here is the result",
        ]
    finally:
        await appdb_b.aclose()


async def test_active_run_still_listed_after_restart(scratch_dsn):
    """A run left queued/running survives a restart AND is still discoverable via
    the live-dashboard read path (`list_active` / `by_handoff`) on a fresh store —
    so the post-restart console re-paints exactly what was in flight, not a blank
    board.
    """
    run_id = str(uuid.uuid4())
    handoff_id = "hoff-active-after-restart"

    # Mid-run on pool A: an active (running) run dispatched for a handoff.
    appdb_a = AppDB(dsn=scratch_dsn)
    store_a = RunStatePgStore(appdb_a)
    await store_a.start_run(
        run_id=run_id, project="proj", agent="agt", handoff_id=handoff_id
    )
    await store_a.append_output(run_id, seq=1, kind="output", text="in flight")
    await store_a.set_status(run_id, "running")
    await store_a.heartbeat(run_id, pid=777)

    # Restart: dispose pool A entirely.
    await appdb_a.aclose()
    del store_a
    del appdb_a

    # Fresh process / fresh pool re-discovers the in-flight run.
    appdb_b = AppDB(dsn=scratch_dsn)
    store_b = RunStatePgStore(appdb_b)
    try:
        active = await store_b.list_active(project="proj")
        ids = {r.run_id for r in active}
        assert run_id in ids, (
            "an active run must still be on the live board after a console restart"
        )

        # The crew view lands on the handoff and re-reads its live transcript.
        by_h = await store_b.by_handoff(handoff_id)
        assert by_h is not None
        assert by_h.run_id == run_id
        assert by_h.status == "running"
        assert [s.text for s in by_h.spans] == ["in flight"]
        assert by_h.heartbeat_at is not None, (
            "the watchdog must still read a real heartbeat for this run post-restart"
        )
    finally:
        await appdb_b.aclose()
