"""T3 — RunStatePort Pg adapter: the run-state SINGLE-SOURCE-OF-TRUTH store.

This is the imperative shell (`app/adapters/`) that IMPLEMENTS the pure
`RunStatePort` Protocol (`app/domain/runstate.py`, T2) over the app-DB asyncpg
pool. Arrows point inward (ratified design §3): the domain port stays pure; only
this adapter touches I/O (asyncpg). It backs the `run_state` + `run_span` tables
from `.agents/data/appdb/2026-06-05-runstate.sql` (T1).

REUSES THE SHARED POOL: the adapter is constructed with the app's single
`appdb.AppDB` instance and borrows ITS asyncpg pool (`AppDB._get_pool()`). It does
NOT open a second pool — one connection pool for the whole app-DB (usage telemetry
+ settings + run-state), per the plan.

GRACEFUL-DEGRADE IS MANDATORY (house law, mirrors appdb.py:147-192): the app-DB is
an OPTIONAL dependency. Every method swallows DB failures — a down/half-up app-DB
returns empty / None / a no-op and NEVER raises into a caller. A worker writing
spans to a dead DB must not crash the run; the run + the Cortex LTM audit trail
proceed regardless. NOTHING here raises into a run path.

BOUNDING (the SQL port of the in-memory `TranscriptStore` caps, orchestrator.py):
  * a per-run total-chars guard in `append_output` (RUN_MAX_CHARS) — one run can
    never grow unbounded; once the cap is hit, further text is dropped,
  * a periodic `prune_old` that trims `run_state` to the N-most-recent rows per
    project + lease owner (RUN_MAX_RUNS), cascading to `run_span` via the FK
    ON DELETE CASCADE.

CONCURRENCY: `run_id` is a caller-supplied uuid4 (generated in the orchestrator).
`append_output` assigns the writer-chosen `seq`; `UNIQUE(run_id, seq)` makes a
concurrent / re-delivered double-write an idempotent no-op (ON CONFLICT DO NOTHING),
never corruption.

`subscribe()` (T4) is the live-push path: it LISTENs on `run_state_events` (the
NOTIFY bus the T1 trigger fires) and yields each changed run's run_id as a WAKE
signal (the app-DB twin of `cortex_client.stream_events`). It holds ONE dedicated
OFF-POOL connection for the LISTEN (via `AppDB.connect()`, NEVER a pooled slot — a
LISTEN connection is held for the whole subscription, so borrowing it from the
small shared pool would starve the transactional writers and freeze chats) and
graceful-degrades like the rest of the adapter — a down/dropped DB ends the
generator cleanly, never raising.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

# Largest metadata sidecar we will persist on a run (bytes of the JSON-encoded dict).
# The sidecar is meant for tiny facts (e.g. {"artifact_id": "<uuid>"}); this cap stops
# a writer accidentally stuffing a large blob into the per-run column. Over the cap →
# the metadata is dropped (the status transition still lands). Env-overridable; not a
# per-project literal, just a tunable bound (mirrors RUN_MAX_CHARS's discipline).
_RUNSTATE_METADATA_MAX_BYTES = 16 * 1024

# Max seconds a writer waits for a free POOLED connection before giving up. Without
# a bound, `pool.acquire()` waits FOREVER when every slot is checked out — the bug
# that froze chats at "thinking". On timeout the asyncio.TimeoutError is caught by
# each writer's try/except and the call degrades to the in-memory fallback (the run
# proceeds; only durable run-state is skipped that beat). Belt-and-suspenders now
# that the long-lived LISTEN subscriber no longer borrows a pooled slot.
_ACQUIRE_TIMEOUT = 5.0

from app.appdb import AppDB
from app.domain.runstate import RunRecord, RunSpan

log = logging.getLogger("console.runstate_pg")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Read a clamped int from the environment (mirrors orchestrator._env_int).
    Keeps the caps config-driven — no project literal, just a tunable bound."""
    try:
        return max(lo, min(hi, int(os.environ.get(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


# Per-run total-chars cap — the SQL twin of orchestrator.TRANSCRIPT_MAX_BYTES
# (32 KiB default). Once a run's accumulated span text reaches this, further
# appended text is dropped so one run can never grow unbounded.
RUN_MAX_CHARS = _env_int("RUNSTATE_MAX_CHARS", 32 * 1024, 4 * 1024, 1024 * 1024)

# Recent-runs cap per PROJECT + lease owner. Autonomous projects can burn through
# 20 rows in hours, so the default preserves useful evidence while remaining bounded.
RUN_MAX_RUNS = _env_int("RUNSTATE_MAX_RUNS", 500, 10, 10000)

# How often (seconds) the periodic `prune_runstate_forever` sweep trims run_state /
# run_span back to RUN_MAX_RUNS per project. Hourly default: frequent enough to keep
# the tables bounded on a long-running console, light enough to be negligible. Env-
# overridable, clamped (min 60s so a bad value can't hot-spin, max 24h). Not a
# per-project literal — just a tunable bound, like the caps above.
RUNSTATE_PRUNE_INTERVAL_S = _env_int("RUNSTATE_PRUNE_INTERVAL_S", 60 * 60, 60, 24 * 60 * 60)

# Request-lived runs are owned by the console HTTP request in local mode. A
# console restart means the old request cannot still write a terminal status.
_REQUEST_LIVED_LEASES = ("chat", "approve_run")

# Legacy rows created before we stamped pid can only be judged by age. Keep the
# window conservative so remote host-service runs have time to finish.
RUN_REQUEST_LEGACY_STALE_S = _env_int(
    "RUNSTATE_REQUEST_LEGACY_STALE_S", 30 * 60, 60, 24 * 60 * 60
)

# The app-DB NOTIFY channel the T1 migration's trigger fires on every run_state /
# run_span change (the app-DB twin of Cortex's `cortex_events` bus). It MUST match
# the channel in `notify_run_state()` (.agents/data/appdb/2026-06-05-runstate.sql).
# This is a fixed bus name, not a per-project literal — `subscribe()` LISTENs on it
# and filters the payload's `project` field in Python.
RUN_STATE_CHANNEL = "run_state_events"

# How often (seconds) `subscribe()` wakes from an idle queue.get() to re-check that
# its listener connection is still alive. This is ONLY a liveness re-check tick — a
# real NOTIFY returns immediately (the stream stays event-driven, not polling). It
# bounds the worst case where the listener connection drops while the queue is empty
# so the generator ends cleanly (graceful-degrade floor) instead of blocking forever.
_SUBSCRIBE_LIVENESS_TICK = _env_int("RUNSTATE_SUBSCRIBE_TICK_S", 5, 1, 60)


def _iso(v: Any) -> Optional[str]:
    """A timestamptz (datetime) → ISO-8601 string for the dependency-free DTO
    layer. None stays None; a str passes through."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:  # pragma: no cover - defensive
        return str(v)


def _int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _float(v: Any) -> Optional[float]:
    """asyncpg returns Decimal for NUMERIC; the DTO carries a plain float."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _jsonb(v: Any) -> Optional[dict]:
    """A run_state.metadata JSONB column → a plain dict for the DTO.

    asyncpg returns a JSONB column as a str (no codec registered on the shared pool),
    so we json-decode it; if a build DID register a dict codec it passes through. A
    non-dict / unparseable value degrades to None (the column is only ever written as a
    json object by `set_status`). Never raises."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, (str, bytes, bytearray)):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):  # pragma: no cover - defensive
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _record_from_row(row: Any, *, spans: Optional[list[RunSpan]] = None) -> RunRecord:
    """Map a run_state asyncpg Record → a RunRecord DTO (timestamps as ISO
    strings, NUMERIC as float). `spans` hydrates the body (empty for header-only
    list views)."""
    return RunRecord(
        run_id=row["run_id"],
        project=row["project"],
        agent=row["agent"],
        agent_display=row["agent_display"],
        handoff_id=row["handoff_id"],
        harness=row["harness"],
        model=row["model"],
        session_id=row["session_id"],
        status=row["status"],
        error=row["error"],
        pid=_int(row["pid"]),
        lease_owner=row["lease_owner"],
        tokens_in=_int(row["tokens_in"]),
        tokens_out=_int(row["tokens_out"]),
        cost_est_usd=_float(row["cost_est_usd"]),
        started_at=_iso(row["started_at"]),
        updated_at=_iso(row["updated_at"]),
        heartbeat_at=_iso(row["heartbeat_at"]),
        ended_at=_iso(row["ended_at"]),
        metadata=_jsonb(row["metadata"]),
        spans=spans if spans is not None else [],
    )


# The run_state header columns, in DTO order — one source of truth for the SELECTs.
_HEADER_COLS = (
    "run_id, project, agent, agent_display, handoff_id, harness, model, "
    "session_id, status, error, pid, lease_owner, tokens_in, tokens_out, "
    "cost_est_usd, started_at, updated_at, heartbeat_at, ended_at, metadata"
)


class RunStatePgStore:
    """asyncpg-backed `RunStatePort` over the app-DB run_state / run_span tables.

    Constructed with the shared `appdb.AppDB` (whose pool it borrows — no second
    pool). Async; every method graceful-degrades (a down app-DB → empty / None /
    no-op, never a raise). Satisfies the `RunStatePort` Protocol structurally."""

    def __init__(self, appdb: AppDB) -> None:
        self._appdb = appdb

    # -- pool access (reuses the shared AppDB pool; never opens its own) --------

    async def _pool(self) -> Any | None:
        """The shared app-DB pool (None when asyncpg is missing or the DB is
        unreachable — the degrade signal)."""
        return await self._appdb._get_pool()

    # -- writers ---------------------------------------------------------------

    async def start_run(
        self,
        *,
        run_id: str,
        project: str,
        agent: str,
        agent_display: Optional[str] = None,
        handoff_id: Optional[str] = None,
        harness: Optional[str] = None,
        model: Optional[str] = None,
        pid: Optional[int] = None,
        lease_owner: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> RunRecord:
        """Open (UPSERT) a run row with status='queued' and return it. Idempotent
        on run_id — re-opening an existing run is safe (the caller pre-creates the
        uuid4 so the detached worker can write the same row).

        `session_id` (OPTIONAL, multi-turn chat) is the per-conversation grouping
        key — persisted so the chat path can later read a conversation's recent turns
        (`recent(session_id=…)`); None for the worker / single-shot chat (NULL column).

        DEGRADE: if the app-DB is down the write is a no-op, but we STILL return an
        in-memory RunRecord with the requested header so the caller has the shape
        (the run proceeds; only durable state is lost)."""
        proj = (project or "").strip().lower()
        agt = (agent or "").strip().lower()
        fallback = RunRecord(
            run_id=run_id,
            project=proj,
            agent=agt,
            agent_display=agent_display,
            handoff_id=handoff_id,
            harness=harness,
            model=model,
            session_id=session_id,
            status="queued",
            pid=pid,
            lease_owner=lease_owner,
        )
        pool = await self._pool()
        if pool is None:
            return fallback
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO run_state
                        (run_id, project, agent, agent_display, handoff_id,
                         harness, model, status, pid, lease_owner, session_id,
                         started_at, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,'queued',$8,$9,$10, now(), now())
                    ON CONFLICT (run_id) DO UPDATE
                       SET project       = EXCLUDED.project,
                           agent         = EXCLUDED.agent,
                           agent_display = EXCLUDED.agent_display,
                           handoff_id    = EXCLUDED.handoff_id,
                           harness       = EXCLUDED.harness,
                           model         = EXCLUDED.model,
                           pid           = EXCLUDED.pid,
                           lease_owner   = EXCLUDED.lease_owner,
                           session_id    = EXCLUDED.session_id,
                           updated_at    = now()
                    RETURNING {_HEADER_COLS}
                    """,
                    run_id, proj, agt, agent_display, handoff_id,
                    harness, model, _int(pid), lease_owner, session_id,
                )
            if row is not None:
                return _record_from_row(row)
            return fallback
        except Exception as exc:
            log.warning("run_state start_run failed (degraded, run proceeds): %s", exc)
            return fallback

    async def append_output(
        self,
        run_id: str,
        *,
        seq: int,
        kind: str,
        text: str,
    ) -> None:
        """Append one transcript span (a run_span row). Idempotent on
        (run_id, seq) — a concurrent / re-delivered span is ON CONFLICT DO NOTHING
        (a no-op, never a duplicate or corruption).

        Per-run total-chars guard: the INSERT only lands while the run's existing
        span-text total is under RUN_MAX_CHARS (checked in the same statement so
        concurrent appends can't race past the cap). At/over the cap the append is
        silently dropped — one run can never grow unbounded. Also bumps the run's
        updated_at. DEGRADE: a down app-DB makes this a no-op (never raises)."""
        if not text:
            return
        pool = await self._pool()
        if pool is None:
            return
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                # The cap check + insert in ONE statement: the CTE sums the run's
                # current span chars; the INSERT ... SELECT only emits a row while
                # that sum is below the cap. ON CONFLICT(run_id,seq) absorbs a
                # double-write. Then stamp updated_at (cheap, always safe).
                await conn.execute(
                    """
                    WITH used AS (
                        SELECT COALESCE(SUM(char_length(text)), 0) AS chars
                          FROM run_span
                         WHERE run_id = $1
                    )
                    INSERT INTO run_span (run_id, seq, kind, text)
                    SELECT $1, $2, $3, $4
                      FROM used
                     WHERE used.chars < $5
                    ON CONFLICT (run_id, seq) DO NOTHING
                    """,
                    run_id, _int(seq), kind, text, RUN_MAX_CHARS,
                )
                await conn.execute(
                    "UPDATE run_state SET updated_at = now() WHERE run_id = $1",
                    run_id,
                )
        except Exception as exc:
            log.warning("run_span append_output failed (degraded): %s", exc)
            return

    async def set_status(
        self,
        run_id: str,
        status: str,
        *,
        error: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Transition status (queued → running → ok | error). Stamps `ended_at`
        on a terminal status (ok/error) and clears it otherwise. DEGRADE: no-op
        on a down app-DB.

        `metadata` (OPTIONAL, additive) stamps the run_state.metadata JSONB sidecar —
        e.g. the Explain run stamps `{"artifact_id": …}` on success. COALESCE-applied,
        so None (the default) leaves the existing column untouched; existing callers are
        byte-for-byte unchanged. An over-cap or non-serializable dict is dropped (logged)
        — the status transition still lands."""
        pool = await self._pool()
        if pool is None:
            return
        terminal = status in ("ok", "error")
        meta_json: Optional[str] = None
        if metadata is not None:
            try:
                encoded = json.dumps(metadata)
            except (TypeError, ValueError):
                log.warning("run_state set_status metadata not JSON-serializable; dropped")
                encoded = None
            if encoded is not None and len(encoded.encode("utf-8")) <= _RUNSTATE_METADATA_MAX_BYTES:
                meta_json = encoded
            elif encoded is not None:
                log.warning(
                    "run_state set_status metadata over %d bytes; dropped",
                    _RUNSTATE_METADATA_MAX_BYTES,
                )
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                await conn.execute(
                    """
                    UPDATE run_state
                       SET status     = $2,
                           error      = $3,
                           ended_at   = CASE WHEN $4 THEN now() ELSE NULL END,
                           metadata   = COALESCE($5::jsonb, metadata),
                           updated_at = now()
                     WHERE run_id = $1
                    """,
                    run_id, status, error, terminal, meta_json,
                )
        except Exception as exc:
            log.warning("run_state set_status failed (degraded): %s", exc)
            return

    async def heartbeat(
        self,
        run_id: str,
        *,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        cost_est_usd: Optional[float] = None,
        pid: Optional[int] = None,
    ) -> None:
        """Bump `heartbeat_at = now()` (the liveness signal the watchdog reads) and
        COALESCE-update the running token/cost totals + pid (a None arg leaves the
        existing value untouched). DEGRADE: no-op on a down app-DB."""
        pool = await self._pool()
        if pool is None:
            return
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                await conn.execute(
                    """
                    UPDATE run_state
                       SET heartbeat_at = now(),
                           tokens_in    = COALESCE($2, tokens_in),
                           tokens_out   = COALESCE($3, tokens_out),
                           cost_est_usd = COALESCE($4, cost_est_usd),
                           pid          = COALESCE($5, pid),
                           updated_at   = now()
                     WHERE run_id = $1
                    """,
                    run_id, _int(tokens_in), _int(tokens_out),
                    cost_est_usd, _int(pid),
                )
        except Exception as exc:
            log.warning("run_state heartbeat failed (degraded): %s", exc)
            return

    async def abandon_stale_request_lived_runs(
        self,
        *,
        current_pid: Optional[int],
        legacy_stale_s: Optional[int] = None,
    ) -> int:
        """Close request-lived rows that cannot complete after a console restart.

        `chat` and local `approve_run` execute inside the console process, so a
        queued/running row stamped with an old pid is abandoned as soon as a new
        process boots. Older rows did not stamp pid; those are cleaned only after
        `legacy_stale_s` so remote host-service runs are not cut off mid-flight.
        """
        pool = await self._pool()
        if pool is None:
            return 0
        pid = _int(current_pid)
        stale_s = max(
            60,
            min(24 * 60 * 60, _int(legacy_stale_s) or RUN_REQUEST_LEGACY_STALE_S),
        )
        message = (
            "run abandoned by console restart before terminal status; "
            "safe to retry"
        )
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                rows = await conn.fetch(
                    """
                    UPDATE run_state
                       SET status     = 'error',
                           error      = CASE
                                          WHEN error IS NULL OR error = '' THEN $3
                                          ELSE error
                                        END,
                           ended_at   = now(),
                           updated_at = now()
                     WHERE status IN ('queued', 'running')
                       AND lease_owner = ANY($1::text[])
                       AND (
                             (pid IS NOT NULL AND $2::int IS NOT NULL AND pid <> $2::int)
                          OR (pid IS NULL AND updated_at < now() - ($4::int * interval '1 second'))
                       )
                     RETURNING run_id
                    """,
                    list(_REQUEST_LIVED_LEASES), pid, message, stale_s,
                )
            return len(rows)
        except Exception as exc:
            log.warning("run_state abandon stale request-lived runs failed (degraded): %s", exc)
            return 0

    # -- readers (one read model feeds both first-paint HTTP and the SSE push) --

    async def get_run(self, run_id: str) -> Optional[RunRecord]:
        """One run WITH its hydrated spans (seq-ordered), or None if unknown / the
        app-DB is down."""
        pool = await self._pool()
        if pool is None:
            return None
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                header = await conn.fetchrow(
                    f"SELECT {_HEADER_COLS} FROM run_state WHERE run_id = $1",
                    run_id,
                )
                if header is None:
                    return None
                span_rows = await conn.fetch(
                    "SELECT seq, kind, text, ts FROM run_span "
                    "WHERE run_id = $1 ORDER BY seq",
                    run_id,
                )
            spans = [
                RunSpan(
                    seq=_int(r["seq"]) or 0,
                    kind=r["kind"],
                    text=r["text"] or "",
                    ts=_iso(r["ts"]),
                )
                for r in span_rows
            ]
            return _record_from_row(header, spans=spans)
        except Exception as exc:
            log.warning("run_state get_run failed (degraded): %s", exc)
            return None

    async def list_active(
        self, project: Optional[str] = None
    ) -> list[RunRecord]:
        """Runs currently queued|running (HEADERS only, newest-first), optionally
        project-scoped. [] when the app-DB is down."""
        pool = await self._pool()
        if pool is None:
            return []
        proj = (project or "").strip().lower() or None
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT {_HEADER_COLS}
                      FROM run_state
                     WHERE status IN ('queued','running')
                       AND ($1::text IS NULL OR project = $1)
                     ORDER BY started_at DESC
                    """,
                    proj,
                )
            return [_record_from_row(r) for r in rows]
        except Exception as exc:
            log.warning("run_state list_active failed (degraded): %s", exc)
            return []

    async def recent(
        self,
        project: Optional[str] = None,
        limit: int = 20,
        *,
        session_id: Optional[str] = None,
        lease_owner: Optional[str] = None,
    ) -> list[RunRecord]:
        """Recent run HEADERS (no body), newest-first, optionally project-scoped.
        [] when the app-DB is down.

        `session_id` (OPTIONAL, multi-turn chat) scopes the read to ONE conversation
        — "the recent turns of this chat session", newest-first. None (the default)
        keeps the existing project-wide recent-runs behaviour byte-for-byte. The
        predicate is bound, not interpolated (no injection), and lands on the partial
        ix_run_state_session index.

        `lease_owner` (OPTIONAL, additive) scopes the read to runs holding that lease
        — e.g. the Explain gallery enumerates `lease_owner='explain'` runs (the
        run_state-as-source-of-truth gallery path, replacing the unreliable Cortex
        content search). None (the default) is lease-agnostic. Bound (not interpolated)
        like the others; both filters AND-compose. The returned headers carry the
        `metadata` sidecar (artifact_id etc.), so the gallery can label each run."""
        pool = await self._pool()
        if pool is None:
            return []
        proj = (project or "").strip().lower() or None
        sess = (session_id or "").strip() or None
        lease = (lease_owner or "").strip() or None
        lim = max(1, min(int(limit or 20), 500))
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT {_HEADER_COLS}
                      FROM run_state
                     WHERE ($1::text IS NULL OR project = $1)
                       AND ($2::text IS NULL OR session_id = $2)
                       AND ($3::text IS NULL OR lease_owner = $3)
                     ORDER BY started_at DESC
                     LIMIT {lim}
                    """,
                    proj, sess, lease,
                )
            return [_record_from_row(r) for r in rows]
        except Exception as exc:
            log.warning("run_state recent failed (degraded): %s", exc)
            return []

    async def by_handoff(self, handoff_id: str) -> Optional[RunRecord]:
        """The latest run (WITH body) for a handoff id, or None. Newest run for a
        handoff wins (the crew view lands on a handoff; the watchdog reads its
        heartbeat_at). None when the app-DB is down."""
        if not handoff_id:
            return None
        pool = await self._pool()
        if pool is None:
            return None
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                header = await conn.fetchrow(
                    f"""
                    SELECT {_HEADER_COLS}
                      FROM run_state
                     WHERE handoff_id = $1
                     ORDER BY started_at DESC
                     LIMIT 1
                    """,
                    handoff_id,
                )
                if header is None:
                    return None
                span_rows = await conn.fetch(
                    "SELECT seq, kind, text, ts FROM run_span "
                    "WHERE run_id = $1 ORDER BY seq",
                    header["run_id"],
                )
            spans = [
                RunSpan(
                    seq=_int(r["seq"]) or 0,
                    kind=r["kind"],
                    text=r["text"] or "",
                    ts=_iso(r["ts"]),
                )
                for r in span_rows
            ]
            return _record_from_row(header, spans=spans)
        except Exception as exc:
            log.warning("run_state by_handoff failed (degraded): %s", exc)
            return None

    # -- bounding (the SQL port of the in-memory deque(maxlen) cap) -------------

    async def prune_old(self, project: Optional[str] = None) -> int:
        """Trim run_state to the RUN_MAX_RUNS newest rows per project/lease owner
        (cascading to run_span via the FK ON DELETE CASCADE). Scoped to one project when given,
        else swept across all projects. Returns the number of run_state rows
        deleted (0 when the app-DB is down — a no-op, never a raise).

        Called periodically (not on the hot append path) so a long-running app
        never accumulates unbounded run history — the durable twin of the
        in-memory deque(maxlen=N) eviction."""
        pool = await self._pool()
        if pool is None:
            return 0
        proj = (project or "").strip().lower() or None
        try:
            async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                # Rank newest-first within each project + lease owner; delete beyond
                # RUN_MAX_RUNS. This keeps worker/chat/explain evidence from evicting
                # each other during autonomous runs.
                deleted = await conn.fetch(
                    """
                    WITH ranked AS (
                        SELECT run_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY project, COALESCE(lease_owner, '')
                                   ORDER BY started_at DESC
                               ) AS rn
                          FROM run_state
                         WHERE ($1::text IS NULL OR project = $1)
                    )
                    DELETE FROM run_state
                     WHERE run_id IN (
                           SELECT run_id FROM ranked WHERE rn > $2
                     )
                    RETURNING run_id
                    """,
                    proj, RUN_MAX_RUNS,
                )
            return len(deleted)
        except Exception as exc:
            log.warning("run_state prune_old failed (degraded): %s", exc)
            return 0

    # -- live push (T4 — LISTEN run_state_events) -------------------------------

    async def subscribe(
        self, project: Optional[str] = None
    ) -> AsyncIterator[str]:
        """LISTEN on the app-DB `run_state_events` bus and yield each changed run's
        `run_id` (optionally filtered to `project`). The app-DB twin of
        `cortex_client.stream_events`: the T1 trigger fires
        `pg_notify('run_state_events', json{run_id, project})` on every run_state /
        run_span change, and this parks on that NOTIFY and re-emits the run_id.

        WAKE-ONLY (load-bearing): the yielded run_id is a wake signal, NOT state.
        The caller (the SSE layer, T8) re-reads `get_run`/`list_active` on each wake,
        so first-paint and the live push read the SAME model and cannot disagree.

        DEDICATED OFF-POOL LISTENER: this opens ONE connection of its OWN (via
        `AppDB.connect()`, NOT a pooled slot) and HOLDS it for the generator's whole
        lifetime (a LISTEN only delivers on the connection that issued it — a
        per-call acquire/release would drop the subscription). Crucially it does NOT
        borrow from the small shared pool: a LISTEN held for the whole subscription
        would otherwise starve the transactional writers (a handful of open SSE panes
        once consumed every pooled slot, so the next chat's `start_run` waited forever
        for a free connection and the chat froze at "thinking"). The dedicated
        connection is CLOSED when the generator ends (consumer breaks / GC /
        `aclose()`). `add_listener` pushes payloads into an asyncio.Queue that the
        loop drains — the Postgres notification dispatcher and our yield loop are
        decoupled (a slow consumer back-pressures via the queue, it can't stall the
        protocol-level reader).

        GRACEFUL-DEGRADE (house law): a down app-DB (connect() returns None) or a
        dropped / failed listener connection makes the generator END CLEANLY (StopAsyncIteration)
        — it NEVER raises into the consumer. Reconnect-on-drop is intentionally out of
        scope for T4 (T8's SSE channel re-subscribes); ending cleanly is the floor, so
        a dead DB can't break the SSE layer. The project filter is normalised
        (lower-cased, blank→None) to match the lower-cased `project` start_run stores.
        """
        wanted = (project or "").strip().lower() or None

        # DEDICATED OFF-POOL connection: a LISTEN holds ONE connection for the
        # subscriber's whole lifetime. Borrowing that from the small shared pool
        # starves the transactional writers — a handful of open `/runstate/stream`
        # SSE panes once consumed every pooled slot, so the next chat's `start_run`
        # waited forever for a free connection and the chat froze at "thinking". So
        # the subscriber opens its OWN connection (the pool stays free for short
        # writes). DEGRADE: `connect()` returns None on a down/absent app-DB →
        # nothing to listen on; end cleanly (no raise), exactly as before.
        conn = await self._appdb.connect()
        if conn is None:
            return

        # The notification dispatcher (asyncpg) runs the callback on its own task and
        # hands us {run_id, project}; we hop it onto a queue the yield-loop drains.
        queue: asyncio.Queue[tuple[Optional[str], Optional[str]]] = asyncio.Queue()

        def _on_notify(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
            """asyncpg LISTEN callback: parse the JSON wake {run_id, project} and
            enqueue it. A malformed / non-JSON payload is dropped (never crashes the
            dispatcher) — the bus only ever carries our own json_build_object."""
            try:
                data = json.loads(payload)
                run_id = data.get("run_id")
                proj = data.get("project")
            except (ValueError, AttributeError):  # pragma: no cover - defensive
                return
            if run_id:
                queue.put_nowait((str(run_id), proj))

        listening = False
        try:
            try:
                await conn.add_listener(RUN_STATE_CHANNEL, _on_notify)
                listening = True
            except Exception as exc:
                # DEGRADE: couldn't establish the LISTEN → end cleanly (no raise).
                log.warning("run_state subscribe LISTEN failed (degraded): %s", exc)
                return

            while True:
                # A dropped listener connection must end the stream cleanly rather
                # than block forever on an empty queue (no NOTIFY can ever arrive on
                # a closed conn again). So we bail the instant the connection is
                # closed — both BEFORE waiting and, via a bounded wait, if it drops
                # WHILE we're parked on an empty queue. The bound is only a liveness
                # re-check tick; a real wake still returns immediately (event-driven,
                # not polling).
                #
                # `is_closed()` itself can raise InterfaceError if the pool reclaimed
                # the connection out from under us (e.g. the pool is being closed) —
                # that IS a drop, so treat any failure here as "end cleanly", never a
                # raise into the consumer.
                try:
                    if conn.is_closed():
                        return
                except Exception:
                    return
                try:
                    run_id, proj = await asyncio.wait_for(
                        queue.get(), timeout=_SUBSCRIBE_LIVENESS_TICK
                    )
                except asyncio.TimeoutError:
                    # No wake this tick — loop to re-check conn liveness, then wait
                    # again. (Not a drop; the stream stays open.)
                    continue
                except (asyncio.CancelledError, GeneratorExit):
                    # Consumer went away / task cancelled — stop cleanly.
                    raise
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("run_state subscribe wait failed (degraded): %s", exc)
                    return
                # Project filter: drop wakes for other projects (None = all).
                if wanted is not None:
                    if ((proj or "").strip().lower() or None) != wanted:
                        continue
                yield run_id
        finally:
            # Always tear down: remove the listener and CLOSE the dedicated
            # connection (it is NOT pooled — the subscriber owns its lifetime).
            # Best-effort — teardown errors are swallowed so closing the generator
            # never raises.
            if listening:
                try:
                    await conn.remove_listener(RUN_STATE_CHANNEL, _on_notify)
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass
            try:
                await conn.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass


async def prune_runstate_forever(
    store: Optional["RunStatePgStore"],
    *,
    interval_s: int = RUNSTATE_PRUNE_INTERVAL_S,
    sleep: Any = None,
    log: Any = None,
    _max_iters: Optional[int] = None,
) -> None:
    """Periodic best-effort sweep that calls ``store.prune_old()`` so a long-running
    console keeps run_state / run_span bounded to RUN_MAX_RUNS newest rows per
    project + lease owner (cascading to run_span via the FK). This is the
    SCHEDULER for the bounding the
    adapter already implements: ``prune_old`` was otherwise dead code, so the tables
    grew UNBOUNDED — the durable twin of the in-memory deque(maxlen) eviction.

    Prunes once on start (immediately reclaims any backlog) then every ``interval_s``
    seconds. NEVER raises and NEVER blocks startup — a None ``store`` (app-DB down /
    store failed to construct) is a clean no-op, and a prune failure is logged and the
    loop waits for the next tick (graceful-degrade, house law). Mirrors
    ``providers.refresh_catalog_forever``; ``sleep`` / ``_max_iters`` are the unit-test
    injection seams (production uses the real ``asyncio.sleep`` and loops forever)."""
    sleep = sleep or asyncio.sleep
    n = 0
    while _max_iters is None or n < _max_iters:
        if store is not None:
            try:
                deleted = await store.prune_old()
                if log is not None and deleted:
                    log.info("run-state prune: trimmed %d old run(s)", deleted)
            except Exception as exc:  # never let the loop die — retry next tick
                if log is not None:
                    log.warning(
                        "run-state prune failed (retrying next tick): %s", exc
                    )
        await sleep(interval_s)
        n += 1


__all__ = [
    "RunStatePgStore",
    "RUN_MAX_CHARS",
    "RUN_MAX_RUNS",
    "RUNSTATE_PRUNE_INTERVAL_S",
    "RUN_STATE_CHANNEL",
    "prune_runstate_forever",
]
