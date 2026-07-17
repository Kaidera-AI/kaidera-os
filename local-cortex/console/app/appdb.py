"""App-DB data layer (E007 / DATA_SEPARATION.md) — the OPERATIONAL store.

A small async (asyncpg) layer over the separate **app-DB** container
(`harness-appdb`, postgres:17-alpine on host port 5500, DB `harness_app`) that
holds everything that is NOT Cortex: per-run **usage/token telemetry**, the
resolved model/provider, and an **estimated cost**. Cortex (`cortex-pg` + the
API) stays agent memory + coordination only — this module never touches it.

Two responsibilities:
  * WRITE — `record_usage(...)` inserts one `usage_events` row per agent/chat/
    harness run (called fire-and-forget from the chat/dispatch telemetry path).
  * READ  — `usage_by_model` / `usage_by_model_provider` / `usage_by_agent` /
    `usage_by_project` feed the Analytics view straight from `usage_events`
    (replacing the old Cortex `/history` token-frame derivation).

GRACEFUL-WHEN-DOWN (hard requirement): the app-DB is an OPTIONAL dependency. The
console + the live chat MUST keep working when the container isn't up. Every
function here degrades cleanly:
  * the pool is created lazily and a connect failure is swallowed (logged once),
  * `record_usage` returns False instead of raising — a telemetry write can
    never break a chat turn,
  * every read returns an empty result (and `available()` reports False), so the
    Analytics view shows a "usage store not connected / no usage yet" state
    rather than a 500.
NOTHING in this module raises into a request path.

DSN: env `HARNESS_APPDB_DSN`, default
`postgresql://harness:harness@localhost:5500/harness_app`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

try:  # asyncpg is the only hard dep this module adds; tolerate its absence too.
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover - import-time environment guard
    asyncpg = None  # type: ignore

log = logging.getLogger("console.appdb")

# The loopback DSN a HOST-side process uses to reach the app-DB (the published host
# port of the harness-appdb container). The container itself reaches the DB at
# `harness-appdb:5432`; the host reaches the SAME Postgres at `localhost:5500`.
_HOST_APPDB_DSN_DEFAULT = "postgresql://harness:harness@localhost:5500/harness_app"

# The in-container hostname(s) that are UNREACHABLE from the host. When a host-side
# process inherits a DSN pointing at one of these, host_appdb_dsn() rewrites the
# host:port to the loopback published port so the connection actually lands. Not a
# project literal — it's the compose service name + the docker host-gateway alias.
_CONTAINER_HOSTS = ("harness-appdb", "host.docker.internal")
_HOST_LOOPBACK = "localhost:5500"

# Connection string for the SEPARATE app-DB container (NOT cortex-pg). Defaults
# to the loopback harness-appdb on host port 5500.
APPDB_DSN = os.environ.get("HARNESS_APPDB_DSN", _HOST_APPDB_DSN_DEFAULT)


def host_appdb_dsn() -> str:
    """The DSN a HOST-side process (the autonomous worker / the chat-runner the host
    harness-service spawns) MUST use to reach the app-DB.

    WHY THIS EXISTS — the run-state visibility fix: the worker runs on the HOST but
    inherits the harness-service's environment. If that env carries the in-CONTAINER
    DSN (``harness-appdb:5432``, the orchestrator's value), the host can't resolve that
    hostname → asyncpg connect fails → every run-state write silently no-ops (graceful
    degrade) → the run sticks at ``queued`` with no pid/spans and the console can't SHOW
    it. The harness-service forces THIS value into every spawned worker's env so the
    worker can never keep a container DSN.

    Resolution order:
      1. ``HARNESS_APPDB_DSN_HOST`` if set — the operator's explicit host override;
      2. ``HARNESS_APPDB_DSN`` rewritten to the loopback published port when it points at
         an in-container host (``harness-appdb`` / the docker host-gateway alias) — the
         host can't reach that hostname, so swap host:port for ``localhost:5500``;
      3. ``HARNESS_APPDB_DSN`` as-is when it's already a host-reachable DSN;
      4. the loopback default (``postgresql://harness:harness@localhost:5500/harness_app``).
    Never raises — a malformed DSN falls back to the loopback default."""
    override = os.environ.get("HARNESS_APPDB_DSN_HOST", "").strip()
    if override:
        return override
    raw = os.environ.get("HARNESS_APPDB_DSN", "").strip()
    if not raw:
        return _HOST_APPDB_DSN_DEFAULT
    try:
        return _rewrite_container_host(raw)
    except Exception:  # pragma: no cover - defensive: never block a spawn on a bad DSN
        return _HOST_APPDB_DSN_DEFAULT


def _rewrite_container_host(dsn: str) -> str:
    """If ``dsn``'s host is an in-container alias the host can't reach, swap the host:port
    segment for the loopback published port; otherwise return ``dsn`` unchanged. Operates
    on the ``...@host:port/...`` segment only (creds, db path, and any query preserved)."""
    if "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, tail = rest.rsplit("@", 1)  # tail = host[:port]/db?query
    # Split the authority (host[:port]) from the path/query remainder.
    slash = tail.find("/")
    authority = tail if slash < 0 else tail[:slash]
    remainder = "" if slash < 0 else tail[slash:]
    host = authority.split(":", 1)[0]
    if host in _CONTAINER_HOSTS:
        return f"{scheme}://{creds}@{_HOST_LOOPBACK}{remainder}"
    return dsn

# Pool sizing: the pool is for SHORT transactional work only (run-state writes,
# usage telemetry, reads). Long-lived LISTEN/NOTIFY subscribers get their OWN
# dedicated connection (see `connect()`), NOT a pooled slot — otherwise a handful
# of open `/runstate/stream` SSE panes hold every slot and the next chat's
# `start_run` blocks forever on `pool.acquire()`, freezing the chat at "thinking".
# A short command timeout + a bounded acquire keep a degraded/half-up DB from ever
# hanging a request; the pool is no longer starved by listeners, so it has headroom.
_POOL_MIN = 1
_POOL_MAX = 16
_CONNECT_TIMEOUT = 3.0   # seconds to establish a connection
_COMMAND_TIMEOUT = 5.0   # per-query ceiling
_ACQUIRE_TIMEOUT = 5.0   # max wait for a free POOLED connection → else degrade (never hang)


class AppDB:
    """Lazy, fault-tolerant asyncpg pool over the app-DB.

    One instance is created on app startup and shared (see app.main lifespan).
    The pool is built on first use; if the DB is unreachable the instance stays
    usable and simply reports `available() == False` until a later call succeeds
    (so bringing the container up mid-session starts working with no restart)."""

    def __init__(self, dsn: str = APPDB_DSN) -> None:
        self.dsn = dsn
        self._pool: Any | None = None
        self._lock = asyncio.Lock()
        # True only after a successful connect; flips back to False on a failure
        # so the UI can show a "not connected" state without probing.
        self._ok = False
        # Don't spam the log on every reconnect attempt while the DB is down.
        self._warned = False

    # -- pool lifecycle -----------------------------------------------------

    async def _get_pool(self) -> Any | None:
        """Return the connection pool, creating it on first use. Returns None
        (never raises) when asyncpg is missing or the DB can't be reached."""
        if asyncpg is None:
            return None
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:  # another task built it while we waited
                return self._pool
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self.dsn,
                    min_size=_POOL_MIN,
                    max_size=_POOL_MAX,
                    timeout=_CONNECT_TIMEOUT,
                    command_timeout=_COMMAND_TIMEOUT,
                )
                self._ok = True
                self._warned = False
                log.info("app-DB pool connected (%s)", _safe_dsn(self.dsn))
            except Exception as exc:  # connection refused / auth / DNS / timeout
                self._pool = None
                self._ok = False
                if not self._warned:
                    log.warning(
                        "app-DB unreachable — usage telemetry/analytics degraded "
                        "(console keeps working). %s",
                        exc,
                    )
                    self._warned = True
            return self._pool

    async def connect(self) -> Any | None:
        """Open a DEDICATED standalone connection (NOT from the pool), for a
        long-lived borrower like a LISTEN/NOTIFY subscriber that must hold one
        connection for its whole lifetime. Routing such a borrower through the
        small shared pool starves the transactional writers — a handful of open
        `/runstate/stream` SSE panes once consumed every pooled slot and froze
        chats at "thinking" (start_run waiting forever for a free connection). So
        a subscriber gets its OWN connection; the pool stays free for short work.

        Returns None (never raises) when asyncpg is missing or the DB is
        unreachable — the caller degrades. NO command_timeout: a LISTEN connection
        parks idle by design and must not be reaped mid-subscription. The CALLER
        owns the lifetime and must `await conn.close()` when done."""
        if asyncpg is None:
            return None
        try:
            return await asyncpg.connect(dsn=self.dsn, timeout=_CONNECT_TIMEOUT)
        except Exception as exc:  # refused / auth / DNS / timeout
            log.warning("app-DB dedicated connect failed (degraded): %s", exc)
            return None

    def available(self) -> bool:
        """Best-effort liveness flag for the UI: True once a connect has
        succeeded. Cheap + non-blocking (reflects the last attempt). The
        Analytics view reads this to choose 'connected' vs 'not connected'."""
        return self._ok and self._pool is not None

    async def aclose(self) -> None:
        """Close the pool on app shutdown (no-op if never built)."""
        pool = self._pool
        self._pool = None
        self._ok = False
        if pool is not None:
            try:
                await pool.close()
            except Exception:  # pragma: no cover - defensive
                pass

    async def ping(self) -> bool:
        """Actively probe the DB (SELECT 1). Updates `available()`. Returns
        True/False, never raises — used by the optional startup warm-up."""
        pool = await self._get_pool()
        if pool is None:
            return False
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            self._ok = True
            return True
        except Exception:
            self._ok = False
            return False

    # -- write path ---------------------------------------------------------

    async def record_usage(
        self,
        project: str | None,
        agent: str | None,
        harness: str | None,
        model: str | None,
        provider: str | None,
        tokens_in: int | None,
        tokens_out: int | None,
        cost_est: float | None,
    ) -> bool:
        """Insert ONE usage_events row (the per-run usage capture). Returns True
        on a successful write, False if the app-DB is down / the write failed.

        NEVER raises — a telemetry failure must not break the chat. Callers fire
        this and ignore the result (or log it), so a degraded app-DB is invisible
        to the live chat path."""
        pool = await self._get_pool()
        if pool is None:
            return False
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO usage_events
                        (project, agent, harness, model, provider,
                         tokens_in, tokens_out, cost_est_usd)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    project,
                    agent,
                    harness,
                    model,
                    provider,
                    _as_int(tokens_in),
                    _as_int(tokens_out),
                    _as_decimal(cost_est),
                )
            self._ok = True
            return True
        except Exception as exc:
            # A write failure flips availability so a transient outage shows in
            # the UI, but it is otherwise swallowed (the chat must not break).
            self._ok = False
            log.warning("app-DB usage write failed (ignored): %s", exc)
            return False

    # -- read path (analytics) ---------------------------------------------
    #
    # Each helper scopes to a project, sums tokens, and SUMS the stored
    # cost_est_usd (so the read side never needs the pricing catalog — the
    # estimate was computed + persisted at write time). All degrade to [] when
    # the DB is down.

    async def usage_by_model(self, project: str) -> list[dict[str, Any]]:
        """Per-model usage for a project: [{model, provider, tokens_in,
        tokens_out, tokens, cost, runs}] sorted by tokens desc. [] when down."""
        rows = await self._fetch(
            """
            SELECT model,
                   MIN(provider)                       AS provider,
                   COALESCE(SUM(tokens_in), 0)         AS tokens_in,
                   COALESCE(SUM(tokens_out), 0)        AS tokens_out,
                   COALESCE(SUM(COALESCE(tokens_in,0) + COALESCE(tokens_out,0)), 0) AS tokens,
                   COALESCE(SUM(cost_est_usd), 0)      AS cost,
                   COUNT(*)                            AS runs
              FROM usage_events
             WHERE project = $1
             GROUP BY model
             ORDER BY tokens DESC
            """,
            project,
        )
        return [_row_to_model(r) for r in rows]

    async def usage_by_model_provider(self, project: str) -> list[dict[str, Any]]:
        """Per model×provider usage for a project: [{model, provider, tokens,
        tokens_in, tokens_out, cost, runs}] sorted by tokens desc. [] when down."""
        rows = await self._fetch(
            """
            SELECT model,
                   provider,
                   COALESCE(SUM(tokens_in), 0)         AS tokens_in,
                   COALESCE(SUM(tokens_out), 0)        AS tokens_out,
                   COALESCE(SUM(COALESCE(tokens_in,0) + COALESCE(tokens_out,0)), 0) AS tokens,
                   COALESCE(SUM(cost_est_usd), 0)      AS cost,
                   COUNT(*)                            AS runs
              FROM usage_events
             WHERE project = $1
             GROUP BY model, provider
             ORDER BY tokens DESC
            """,
            project,
        )
        return [_row_to_model(r) for r in rows]

    async def usage_by_agent(self, project: str) -> list[dict[str, Any]]:
        """Per-agent usage for a project: [{agent, model, provider, tokens,
        tokens_in, tokens_out, cost, runs}] sorted by tokens desc. The model/
        provider shown is the agent's most-used (by token sum). [] when down."""
        rows = await self._fetch(
            """
            WITH per AS (
                SELECT agent,
                       model,
                       MIN(provider) AS provider,
                       COALESCE(SUM(COALESCE(tokens_in,0) + COALESCE(tokens_out,0)), 0) AS tokens,
                       COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       COALESCE(SUM(cost_est_usd), 0) AS cost,
                       COUNT(*) AS runs
                  FROM usage_events
                 WHERE project = $1
                 GROUP BY agent, model
            ),
            ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY agent ORDER BY tokens DESC) AS rn
                  FROM per
            ),
            totals AS (
                SELECT agent,
                       SUM(tokens)     AS tokens,
                       SUM(tokens_in)  AS tokens_in,
                       SUM(tokens_out) AS tokens_out,
                       SUM(cost)       AS cost,
                       SUM(runs)       AS runs
                  FROM per
                 GROUP BY agent
            )
            SELECT t.agent,
                   r.model,
                   r.provider,
                   t.tokens,
                   t.tokens_in,
                   t.tokens_out,
                   t.cost,
                   t.runs
              FROM totals t
              JOIN ranked r ON r.agent = t.agent AND r.rn = 1
             ORDER BY t.tokens DESC
            """,
            project,
        )
        return [_row_to_agent(r) for r in rows]

    async def usage_by_project(self, project: str) -> dict[str, Any]:
        """Project-wide totals: {tokens, tokens_in, tokens_out, cost, runs,
        agents, models}. Zeroed-but-present when down (caller checks
        available())."""
        rows = await self._fetch(
            """
            SELECT COALESCE(SUM(COALESCE(tokens_in,0) + COALESCE(tokens_out,0)), 0) AS tokens,
                   COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(cost_est_usd), 0) AS cost,
                   COUNT(*)                       AS runs,
                   COUNT(DISTINCT agent)          AS agents,
                   COUNT(DISTINCT model)          AS models
              FROM usage_events
             WHERE project = $1
            """,
            project,
        )
        if not rows:
            return {
                "tokens": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
                "runs": 0, "agents": 0, "models": 0,
            }
        r = rows[0]
        return {
            "tokens": _as_int(r["tokens"]) or 0,
            "tokens_in": _as_int(r["tokens_in"]) or 0,
            "tokens_out": _as_int(r["tokens_out"]) or 0,
            "cost": _as_float(r["cost"]) or 0.0,
            "runs": _as_int(r["runs"]) or 0,
            "agents": _as_int(r["agents"]) or 0,
            "models": _as_int(r["models"]) or 0,
        }

    # -- orchestration plan (E007 Phase 1.5 — wave-based dependency sequencing) --
    #
    # The autonomous orchestrator (Cole's loop) reads a project's wave plan here to
    # decide which pending handoffs may dispatch: only the LOWEST wave (per epic)
    # that still has incomplete handoffs. A handoff with NO row is wave 0 (Phase-1
    # behaviour — dispatched immediately). This read degrades to {} when the DB is
    # down, which the loop treats as "no plan" → every handoff is wave 0 (Phase 1).

    async def orchestration_plan(self, project: str) -> dict[str, dict[str, Any]]:
        """Return a project's whole wave plan as {handoff_id: {epic, wave}}.

        Used by the orchestrator loop to gate dispatch by wave. Empty {} when the
        DB is down OR the project has no plan rows — in BOTH cases the loop treats
        every candidate handoff as wave 0 (exactly the Phase-1 dispatch-immediately
        behaviour), so a degraded app-DB can never STRAND a handoff, only fall back
        to ungated Phase-1 dispatch. Never raises."""
        rows = await self._fetch(
            """
            SELECT handoff_id, epic, wave
              FROM handoff_orchestration
             WHERE project = $1
            """,
            (project or "").strip().lower(),
        )
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            hid = str(r["handoff_id"] or "").strip()
            if not hid:
                continue
            out[hid] = {
                "epic": (r["epic"] or None),
                "wave": _as_int(r["wave"]) or 0,
            }
        return out

    # -- automation feeders (scheduled jobs) --------------------------------
    #
    # These are operational triggers that emit Cortex handoffs. The trigger
    # definitions live here; the agent memory/work itself remains in Cortex.

    async def list_scheduled_jobs(self, project: str) -> list[dict[str, Any]]:
        """Return scheduled jobs for one project. [] when the app-DB is down."""
        rows = await self._fetch(
            """
            SELECT project, id, name, enabled, schedule, payload,
                   next_run_at, last_run_at, last_status, last_error,
                   created_at, updated_at
              FROM scheduled_jobs
             WHERE project = $1
             ORDER BY enabled DESC, next_run_at NULLS LAST, name
            """,
            (project or "").strip().lower(),
        )
        return [_row_to_scheduled_job(r) for r in rows]

    async def due_scheduled_jobs(
        self, project: str, now: datetime | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return enabled jobs whose next_run_at is due. [] when down.

        The query is intentionally scoped to one project: the orchestrator already
        reconciles only the ON projects, so a disabled project cannot schedule
        itself into execution.
        """
        due_at = now or datetime.now(timezone.utc)
        rows = await self._fetch(
            """
            SELECT project, id, name, enabled, schedule, payload,
                   next_run_at, last_run_at, last_status, last_error,
                   created_at, updated_at
              FROM scheduled_jobs
             WHERE project = $1
               AND enabled = TRUE
               AND next_run_at IS NOT NULL
               AND next_run_at <= $2
             ORDER BY next_run_at ASC
             LIMIT $3
            """,
            (project or "").strip().lower(),
            due_at,
            max(1, min(int(limit or 20), 100)),
        )
        return [_row_to_scheduled_job(r) for r in rows]

    async def upsert_scheduled_job(
        self,
        *,
        project: str,
        job_id: str,
        name: str,
        enabled: bool,
        schedule: dict[str, Any],
        payload: dict[str, Any],
        next_run_at: datetime | None,
    ) -> dict[str, Any] | None:
        """Create/update a scheduled job. None when the app-DB is unavailable."""
        pool = await self._get_pool()
        if pool is None:
            return None
        project_key = (project or "").strip().lower()
        clean_id = (job_id or "").strip().lower()
        if not project_key or not clean_id:
            return None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO scheduled_jobs
                        (project, id, name, enabled, schedule, payload, next_run_at)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)
                    ON CONFLICT (project, id) DO UPDATE
                       SET name = EXCLUDED.name,
                           enabled = EXCLUDED.enabled,
                           schedule = EXCLUDED.schedule,
                           payload = EXCLUDED.payload,
                           next_run_at = EXCLUDED.next_run_at,
                           updated_at = now()
                    RETURNING project, id, name, enabled, schedule, payload,
                              next_run_at, last_run_at, last_status, last_error,
                              created_at, updated_at
                    """,
                    project_key,
                    clean_id,
                    (name or clean_id).strip(),
                    bool(enabled),
                    json.dumps(schedule or {}),
                    json.dumps(payload or {}),
                    next_run_at,
                )
            self._ok = True
            return _row_to_scheduled_job(row) if row else None
        except Exception as exc:
            self._ok = False
            log.warning("app-DB scheduled job upsert failed (ignored): %s", exc)
            return None

    async def delete_scheduled_job(self, *, project: str, job_id: str) -> bool:
        """Delete one scheduled job. False when unavailable or no row matched."""
        pool = await self._get_pool()
        if pool is None:
            return False
        project_key = (project or "").strip().lower()
        clean_id = (job_id or "").strip().lower()
        if not project_key or not clean_id:
            return False
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    """
                    DELETE FROM scheduled_jobs
                     WHERE project = $1 AND id = $2
                    """,
                    project_key,
                    clean_id,
                )
            self._ok = True
            return str(result).endswith(" 1")
        except Exception as exc:
            self._ok = False
            log.warning("app-DB scheduled job delete failed (ignored): %s", exc)
            return False

    async def mark_scheduled_job_run(
        self,
        *,
        project: str,
        job_id: str,
        status: str,
        next_run_at: datetime | None,
        error: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """Persist one run attempt result. False when the app-DB is down."""
        pool = await self._get_pool()
        if pool is None:
            return False
        project_key = (project or "").strip().lower()
        clean_id = (job_id or "").strip().lower()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE scheduled_jobs
                       SET last_run_at = now(),
                           last_status = $3,
                           last_error = $4,
                           next_run_at = $5,
                           enabled = COALESCE($6, enabled),
                           updated_at = now()
                     WHERE project = $1 AND id = $2
                    """,
                    project_key,
                    clean_id,
                    (status or "").strip() or None,
                    (error or "").strip() or None,
                    next_run_at,
                    enabled,
                )
            self._ok = True
            return True
        except Exception as exc:
            self._ok = False
            log.warning("app-DB scheduled job run update failed (ignored): %s", exc)
            return False

    # -- internal -----------------------------------------------------------

    async def _fetch(self, sql: str, *args: Any) -> list[Any]:
        """Run a read query, returning the rows ([] when the DB is down or the
        query fails). Never raises into the caller."""
        pool = await self._get_pool()
        if pool is None:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
            self._ok = True
            return list(rows)
        except Exception as exc:
            self._ok = False
            log.warning("app-DB read failed (degraded): %s", exc)
            return []


# ---------------------------------------------------------------------------
#  Coercion helpers — asyncpg returns Decimal for NUMERIC; the rest of the app
#  works in plain ints/floats. Keep the conversions defensive (None-safe).
# ---------------------------------------------------------------------------

def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_decimal(v: Any) -> Any:
    """Pass a numeric cost straight through (asyncpg adapts float→NUMERIC).
    None stays None (a run with no derivable cost stores NULL)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _json_obj(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _iso(v: Any) -> str | None:
    if isinstance(v, datetime):
        return v.isoformat()
    return None


def _row_to_scheduled_job(r: Any) -> dict[str, Any]:
    return {
        "project": str(r["project"]),
        "id": str(r["id"]),
        "name": str(r["name"]),
        "enabled": bool(r["enabled"]),
        "schedule": _json_obj(r["schedule"]),
        "payload": _json_obj(r["payload"]),
        "next_run_at": _iso(r["next_run_at"]),
        "last_run_at": _iso(r["last_run_at"]),
        "last_status": r["last_status"],
        "last_error": r["last_error"],
        "created_at": _iso(r["created_at"]),
        "updated_at": _iso(r["updated_at"]),
    }


def _row_to_model(r: Any) -> dict[str, Any]:
    return {
        "model": r["model"],
        "provider": r["provider"],
        "tokens": _as_int(r["tokens"]) or 0,
        "tokens_in": _as_int(r["tokens_in"]) or 0,
        "tokens_out": _as_int(r["tokens_out"]) or 0,
        "cost": _as_float(r["cost"]) or 0.0,
        "runs": _as_int(r["runs"]) or 0,
    }


def _row_to_agent(r: Any) -> dict[str, Any]:
    return {
        "agent": r["agent"],
        "model": r["model"],
        "provider": r["provider"],
        "tokens": _as_int(r["tokens"]) or 0,
        "tokens_in": _as_int(r["tokens_in"]) or 0,
        "tokens_out": _as_int(r["tokens_out"]) or 0,
        "cost": _as_float(r["cost"]) or 0.0,
        "runs": _as_int(r["runs"]) or 0,
    }


def _safe_dsn(dsn: str) -> str:
    """Redact the password from a DSN for logging (postgresql://u:***@host/db)."""
    try:
        if "@" in dsn and "://" in dsn:
            scheme, rest = dsn.split("://", 1)
            creds, tail = rest.split("@", 1)
            if ":" in creds:
                user = creds.split(":", 1)[0]
                return f"{scheme}://{user}:***@{tail}"
        return dsn
    except Exception:  # pragma: no cover - defensive
        return "app-DB"


# ===========================================================================
#  SETTINGS STORE (E007) — the console's settings ALSO live in the app-DB.
#
#  This is the SECOND app-DB responsibility (the first is usage telemetry
#  above): persisting all console settings and per-agent overrides.
#  per-agent harness/model/reasoning/designation/role overrides — into the
#  durable app-DB so they survive a server restart from an operational store
#  (config/settings.local.json becomes a fallback/seed only). Agent harness/
#  model routing is OPERATIONAL, so it belongs here, NOT in Cortex.
#
#  Tables (see .agents/data/appdb/2026-06-01-settings.sql):
#    * app_settings(key, value JSONB)        — System fields + side blobs
#    * agent_settings(project, agent, ...)    — per-agent override columns
#
#  WHY A SEPARATE SYNC LAYER: app/settings.py exposes a SYNCHRONOUS public API
#  (load/save/load_agent_overrides/...) called from sync code paths. The async
#  AppDB pool above can't serve those without restructuring every caller, so this
#  uses a small lazy psycopg2 connection. It mirrors AppDB's graceful-degradation
#  contract EXACTLY: if psycopg2 is missing OR the DB is unreachable OR a query
#  fails, every read returns the UNAVAILABLE sentinel and every write returns
#  False — so app/settings.py transparently falls back to the JSON file and the
#  console NEVER crashes when the app-DB is down.
# ===========================================================================

try:  # psycopg2 is the sync driver for the settings store; tolerate its absence.
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
except Exception:  # pragma: no cover - import-time environment guard
    psycopg2 = None  # type: ignore


# Sentinel meaning "the app-DB could not answer" (distinct from a real value of
# None / {} / []). app/settings.py checks `is UNAVAILABLE` to decide whether to
# fall back to the JSON file. Never stored, never serialised.
class _Unavailable:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<appdb settings UNAVAILABLE>"

    def __bool__(self) -> bool:  # falsy, but identity is what callers test
        return False


UNAVAILABLE = _Unavailable()

# The override columns on agent_settings, in a fixed order (mirrors
# settings.AGENT_OVERRIDE_FIELDS; kept here so this module has no import-time
# dependency on app.settings — settings.py imports appdb, not the reverse).
_AGENT_OVERRIDE_COLS = (
    "harness", "model", "reasoning", "designation", "role", "role_aliases",
    "auto_dispatch",
)
# Legacy column sets — used as a GRACEFUL FALLBACK when a deployment's app-DB has
# not yet received the latest additive agent_settings migrations. Without this
# fallback, a missing column makes load_agent_overrides/get_agent_override return
# UNAVAILABLE, which cascades through settings._db_read_raw → None → JSON-file
# fallback and HIDES every provider API key from the console (the "saved key shows
# as not set" data-loss bug). Falling back keeps overrides readable (just without
# the new field) so the rest of the settings store stays live.
_AGENT_OVERRIDE_SELECTS: tuple[tuple[str, ...], ...] = (
    _AGENT_OVERRIDE_COLS,
    ("harness", "model", "reasoning", "designation", "role", "role_aliases"),
    ("harness", "model", "reasoning", "designation", "role"),
)

# Short connect/query ceilings so a degraded/half-up DB never hangs a request.
_SETTINGS_CONNECT_TIMEOUT = 3  # seconds
_SETTINGS_STATEMENT_TIMEOUT_MS = 1500
_SETTINGS_LOCK_TIMEOUT_MS = 500


class SettingsDB:
    """Lazy, fault-tolerant SYNC (psycopg2) accessor for the settings tables.

    One module-level instance (`settings_db`) is shared. The connection is opened
    on first use and reused; on any failure it is dropped and the next call
    retries (so bringing the container up mid-session recovers with no restart).
    Mirrors AppDB's degrade-don't-crash contract: reads return UNAVAILABLE, writes
    return False, NOTHING raises into app/settings.py."""

    def __init__(self, dsn: str = APPDB_DSN) -> None:
        self.dsn = dsn
        self._conn: Any | None = None
        self._warned = False

    # -- connection lifecycle ----------------------------------------------

    def _get_conn(self) -> Any | None:
        """Return a live autocommit connection, (re)opening it on demand. Returns
        None (never raises) when psycopg2 is missing or the DB is unreachable."""
        if psycopg2 is None:
            return None
        conn = self._conn
        if conn is not None:
            if getattr(conn, "closed", 1) == 0:
                return conn
            self._conn = None  # stale/closed — fall through and reopen
        try:
            conn = psycopg2.connect(
                self.dsn,
                connect_timeout=_SETTINGS_CONNECT_TIMEOUT,
                options=(
                    f"-c statement_timeout={_SETTINGS_STATEMENT_TIMEOUT_MS} "
                    f"-c lock_timeout={_SETTINGS_LOCK_TIMEOUT_MS}"
                ),
            )
            conn.autocommit = True  # each settings write is its own txn
            self._conn = conn
            self._warned = False
            log.info("app-DB settings connection ready (%s)", _safe_dsn(self.dsn))
            return conn
        except Exception as exc:  # refused / auth / DNS / timeout
            self._conn = None
            if not self._warned:
                log.warning(
                    "app-DB settings unreachable — settings fall back to the local "
                    "JSON file (console keeps working). %s",
                    exc,
                )
                self._warned = True
            return None

    def _drop_conn(self) -> None:
        """Close + forget the connection after a query error so the next call
        reconnects cleanly (a half-broken connection never lingers)."""
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def available(self) -> bool:
        """True if a connection can be established right now (probes lazily)."""
        return self._get_conn() is not None

    def close(self) -> None:
        """Close the connection (no-op if never opened). For app shutdown."""
        self._drop_conn()

    # -- app_settings (System config + side blobs) --------------------------

    def load_app_settings(self) -> dict[str, Any] | _Unavailable:
        """Return the WHOLE app_settings map {key: value} (value already JSON-
        decoded to its native type). UNAVAILABLE when the DB can't answer."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM app_settings")
                rows = cur.fetchall()
            # psycopg2 decodes JSONB → native Python objects automatically.
            return {str(k): v for (k, v) in rows}
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB settings read failed (degraded): %s", exc)
            return UNAVAILABLE

    def upsert_app_settings(self, items: dict[str, Any]) -> bool:
        """Upsert many app_settings key→value rows in ONE transaction. Values are
        JSON-encoded. Returns True on success, False (degraded) otherwise."""
        if not items:
            return True
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            import json as _json

            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (%s, %s::jsonb, now())
                    ON CONFLICT (key) DO UPDATE
                       SET value = EXCLUDED.value, updated_at = now()
                    """,
                    [(str(k), _json.dumps(v)) for k, v in items.items()],
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB settings write failed (ignored): %s", exc)
            return False

    def delete_app_setting(self, key: str) -> bool:
        """Delete one app_settings row by key. True on success (incl. no-op),
        False when the DB can't answer."""
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app_settings WHERE key = %s", (str(key),))
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB settings delete failed (ignored): %s", exc)
            return False

    def has_any_app_settings(self) -> bool | _Unavailable:
        """True if app_settings has ≥1 row (used to decide whether the one-time
        JSON→app-DB import still needs to run). UNAVAILABLE when down."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app_settings LIMIT 1")
                return cur.fetchone() is not None
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB settings probe failed (degraded): %s", exc)
            return UNAVAILABLE

    # -- agent_settings (per-agent overrides) -------------------------------

    def load_agent_overrides(self) -> dict[str, dict[str, str]] | _Unavailable:
        """Return ALL per-agent overrides as {"{project}:{agent}": {field: str}}.

        Only non-NULL/non-empty override columns are included for each agent (so
        the shape matches the old JSON blob exactly). UNAVAILABLE when down.
        RESILIENT to missing additive columns: retries older SELECT shapes so a
        not-yet-migrated app-DB never degrades the whole settings read."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        last_exc: Exception | None = None
        rows = None
        cols: tuple[str, ...] | None = None
        for candidate in _AGENT_OVERRIDE_SELECTS:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT project, agent, {', '.join(candidate)} FROM agent_settings")
                    rows = cur.fetchall()
                cols = candidate
                break
            except Exception as exc:
                last_exc = exc
                self._drop_conn()
                conn = self._get_conn()
                if conn is None:
                    break
        if rows is None or cols is None:
            log.warning("app-DB agent-settings read failed (degraded): %s", last_exc)
            return UNAVAILABLE
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            project, agent = row[0], row[1]
            entry: dict[str, str] = {}
            for col, val in zip(cols, row[2:]):
                if val is not None and str(val).strip():
                    entry[col] = str(val)
            key = f"{(project or '').strip().lower()}:{(agent or '').strip().lower()}"
            if entry:
                out[key] = entry
        return out

    def get_agent_override(
        self, project: str, agent: str
    ) -> dict[str, str] | _Unavailable:
        """One agent's override dict ({} if no row / all-NULL). UNAVAILABLE when
        down. project/agent are matched case-insensitively (stored lower-cased).
        RESILIENT to missing additive columns (see load_agent_overrides)."""
        proj = (project or "").strip().lower()
        name = (agent or "").strip().lower()
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        last_exc: Exception | None = None
        row = None
        cols: tuple[str, ...] | None = None
        found_shape = False
        for candidate in _AGENT_OVERRIDE_SELECTS:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {', '.join(candidate)} "
                        "FROM agent_settings WHERE project = %s AND agent = %s",
                        (proj, name),
                    )
                    row = cur.fetchone()
                cols = candidate
                found_shape = True
                break
            except Exception as exc:
                last_exc = exc
                self._drop_conn()
                conn = self._get_conn()
                if conn is None:
                    break
        if not found_shape or cols is None:
            log.warning("app-DB agent-settings read failed (degraded): %s", last_exc)
            return UNAVAILABLE
        if row is None:
            return {}
        entry: dict[str, str] = {}
        for col, val in zip(cols, row):
            if val is not None and str(val).strip():
                entry[col] = str(val)
        return entry

    def save_agent_override(
        self, project: str, agent: str, entry: dict[str, str]
    ) -> bool:
        """Persist one agent's COMPLETE override row (UPSERT; absent fields stored
        NULL). An empty `entry` DELETEs the row (no overrides left). The caller
        (settings.save_agent_override) has already cleaned/validated `entry`.
        Returns True on success, False (degraded) otherwise."""
        conn = self._get_conn()
        if conn is None:
            return False
        proj = (project or "").strip().lower()
        name = (agent or "").strip().lower()
        try:
            if not entry:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM agent_settings WHERE project = %s AND agent = %s",
                        (proj, name),
                    )
                return True
            with conn.cursor() as cur:
                # Best-effort online migration for older local app-DBs. The SQL is
                # idempotent and mirrors the checked-in migration.
                cur.execute("ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS auto_dispatch TEXT")
                vals = [entry.get(col) or None for col in _AGENT_OVERRIDE_COLS]
                cur.execute(
                    """
                    INSERT INTO agent_settings
                        (project, agent, harness, model, reasoning, designation,
                         role, role_aliases, auto_dispatch, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (project, agent) DO UPDATE
                       SET harness = EXCLUDED.harness,
                           model = EXCLUDED.model,
                           reasoning = EXCLUDED.reasoning,
                           designation = EXCLUDED.designation,
                           role = EXCLUDED.role,
                           role_aliases = EXCLUDED.role_aliases,
                           auto_dispatch = EXCLUDED.auto_dispatch,
                           updated_at = now()
                    """,
                    (proj, name, *vals),
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB agent-settings write failed (ignored): %s", exc)
            return False

    # -- project_autonomy (autonomous-dispatch master switch) ---------------
    #
    # The per-project kill-switch for the autonomous orchestrator (Cole's loop).
    # SHIP-DARK contract: an absent row (and an unreachable DB) BOTH mean OFF, so
    # the loop can never spin up a project the operator didn't explicitly enable.
    # Reads degrade to the safe default (OFF / empty set); writes return False.

    def get_project_autonomy(self, project: str) -> bool | _Unavailable:
        """Whether autonomous dispatch is ON for one project. No row → False
        (the ship-dark default). UNAVAILABLE only when the DB can't answer — the
        caller (settings.is_project_autonomous) maps UNAVAILABLE to OFF too, so a
        degraded DB never accidentally enables autonomy."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT enabled FROM project_autonomy WHERE project = %s",
                    ((project or "").strip().lower(),),
                )
                row = cur.fetchone()
            return bool(row[0]) if row is not None else False
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB autonomy read failed (degraded → OFF): %s", exc)
            return UNAVAILABLE

    def set_project_autonomy(
        self, project: str, enabled: bool, updated_by: str | None = None
    ) -> bool:
        """Flip the autonomous-dispatch switch for one project (UPSERT). Returns
        True on success, False when the DB can't answer (the toggle then reports
        the write failed and the UI re-reads the unchanged state)."""
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO project_autonomy (project, enabled, updated_by, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (project) DO UPDATE
                       SET enabled = EXCLUDED.enabled,
                           updated_by = EXCLUDED.updated_by,
                           updated_at = now()
                    """,
                    ((project or "").strip().lower(), bool(enabled),
                     (updated_by or None)),
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB autonomy write failed (ignored): %s", exc)
            return False

    def list_autonomous_projects(self) -> list[str] | _Unavailable:
        """The set of projects with autonomy ON (enabled = TRUE), as lower-cased
        keys. The orchestrator loop reconciles against THIS — an empty list (the
        ship-dark default, since no row is seeded) means the loop is a no-op.
        UNAVAILABLE when the DB is down (the loop then treats it as empty → idle)."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT project FROM project_autonomy WHERE enabled = TRUE"
                )
                rows = cur.fetchall()
            return [str(r[0]).strip().lower() for r in rows if r and r[0]]
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB autonomy list failed (degraded → empty): %s", exc)
            return UNAVAILABLE

    # -- project_propose_mode (propose-mode training-wheels gate) ---------------
    #
    # When propose_mode is ON for a project, Dispatch PARKS each ready handoff as
    # "awaiting approval" instead of auto-spawning it. The human operator then
    # clicks Approve in the Dispatch view to let the next sweep spawn it. This is
    # the training-wheels safety gate (Inc 1 of the PM Relentless Beat feature).
    # Fail-safe contract mirrors project_autonomy: no row / DB down → False (gate
    # is OFF, i.e. auto-spawn — existing behaviour unchanged for all existing
    # projects that never set this flag).

    def get_project_propose_mode(self, project: str) -> bool | _Unavailable:
        """Whether propose-mode is ON for one project. No row → False (default:
        auto-spawn, existing behaviour). UNAVAILABLE only when the DB can't
        answer — the caller (settings.is_propose_mode) maps UNAVAILABLE to
        False too, so a degraded DB never accidentally blocks a dispatch."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT enabled FROM project_propose_mode WHERE project = %s",
                    ((project or "").strip().lower(),),
                )
                row = cur.fetchone()
            return bool(row[0]) if row is not None else False
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB propose_mode read failed (degraded → OFF): %s", exc)
            return UNAVAILABLE

    def set_project_propose_mode(
        self, project: str, enabled: bool, updated_by: str | None = None
    ) -> bool:
        """Flip the propose-mode gate for one project (UPSERT). Returns True on
        success, False when the DB can't answer."""
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO project_propose_mode (project, enabled, updated_by, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (project) DO UPDATE
                       SET enabled = EXCLUDED.enabled,
                           updated_by = EXCLUDED.updated_by,
                           updated_at = now()
                    """,
                    ((project or "").strip().lower(), bool(enabled),
                     (updated_by or None)),
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB propose_mode write failed (ignored): %s", exc)
            return False

    # -- pending_approval (handoffs gated by propose-mode, awaiting human OK) --
    #
    # When propose_mode is ON, _maybe_dispatch writes status='awaiting' here
    # instead of spawning. The approve route flips the same row to 'approved';
    # the next Dispatch sweep reads that status and spawns normally. Records are
    # keyed by (project, handoff_id). Idempotent set/clear: set is an UPSERT
    # (safe to call twice), clear is a DELETE (safe to call on an absent row).

    def set_awaiting_approval(self, project: str, handoff_id: str) -> bool:
        """Park a handoff as 'awaiting approval' for a propose-mode project.
        UPSERT — safe to call if the record already exists. Returns True on
        success, False when the DB can't answer."""
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_approval (project, handoff_id, status, created_at)
                    VALUES (%s, %s, 'awaiting', now())
                    ON CONFLICT (project, handoff_id) DO UPDATE
                       SET status = 'awaiting',
                           created_at = pending_approval.created_at
                    """,
                    (
                        (project or "").strip().lower(),
                        (handoff_id or "").strip(),
                    ),
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB pending_approval set failed (ignored): %s", exc)
            return False

    def clear_awaiting_approval(self, project: str, handoff_id: str) -> bool:
        """Remove the awaiting-approval record for one handoff (approve it). A
        no-op (returns True) when no row exists. Returns False only when the DB
        can't answer."""
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM pending_approval WHERE project = %s AND handoff_id = %s",
                    (
                        (project or "").strip().lower(),
                        (handoff_id or "").strip(),
                    ),
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB pending_approval clear failed (ignored): %s", exc)
            return False

    def is_awaiting_approval(self, project: str, handoff_id: str) -> bool | _Unavailable:
        """True if this handoff is currently parked awaiting approval. False
        when the row is absent. UNAVAILABLE when the DB can't answer."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pending_approval WHERE project = %s AND handoff_id = %s",
                    (
                        (project or "").strip().lower(),
                        (handoff_id or "").strip(),
                    ),
                )
                row = cur.fetchone()
            return row is not None
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB pending_approval check failed (degraded): %s", exc)
            return UNAVAILABLE

    def list_awaiting_approval(self, project: str) -> list[str] | _Unavailable:
        """Return handoff_ids with status='awaiting' for a project, ordered by
        created_at (oldest first). Only 'awaiting' rows are returned — 'approved'
        rows are already cleared from the UI queue. UNAVAILABLE when the DB can't
        answer."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT handoff_id FROM pending_approval "
                    # Tolerate legacy NULL-status rows parked before set_awaiting_approval
                    # stamped status='awaiting' — else those handoffs silently never appear.
                    "WHERE project = %s AND (status IS NULL OR status = 'awaiting') "
                    "ORDER BY created_at",
                    ((project or "").strip().lower(),),
                )
                rows = cur.fetchall()
            return [str(r[0]) for r in rows if r and r[0]]
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB pending_approval list failed (degraded): %s", exc)
            return UNAVAILABLE

    def get_approval_status(
        self, project: str, handoff_id: str
    ) -> str | None | _Unavailable:
        """Return the approval status for one handoff: 'awaiting', 'approved', or
        None (no row). UNAVAILABLE when the DB can't answer.

        None means the handoff has never been parked (gate should write it);
        'awaiting' means parked, waiting for operator approval;
        'approved' means the operator clicked Approve — the next sweep spawns."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM pending_approval "
                    "WHERE project = %s AND handoff_id = %s",
                    (
                        (project or "").strip().lower(),
                        (handoff_id or "").strip(),
                    ),
                )
                row = cur.fetchone()
            return str(row[0]) if row is not None else None
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB pending_approval status read failed (degraded): %s", exc)
            return UNAVAILABLE

    def set_approval_status(
        self, project: str, handoff_id: str, status: str
    ) -> bool:
        """Set the approval status for one handoff (UPSERT on the status column).
        `status` should be 'awaiting' or 'approved'. Returns True on success,
        False when the DB can't answer. Safe to call multiple times (idempotent)."""
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_approval (project, handoff_id, status, created_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (project, handoff_id) DO UPDATE
                       SET status = EXCLUDED.status
                    """,
                    (
                        (project or "").strip().lower(),
                        (handoff_id or "").strip(),
                        str(status),
                    ),
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB pending_approval status write failed (ignored): %s", exc)
            return False

    # -- handoff_orchestration (wave plan) — SYNC accessors for planning CLI -----
    #
    # The `cole-plan` helper (console/scripts/cole-plan) records a handoff's
    # epic/wave and prints a project's planned DAG. It runs as a standalone process
    # and reuses THIS sync (psycopg2) accessor so the plan write goes through the
    # same graceful-degrade store as the rest of the settings layer. The async
    # AppDB.orchestration_plan above is the loop's read path; these are the CLI's.

    def upsert_handoff_plan(
        self, handoff_id: str, project: str | None, epic: str | None, wave: int
    ) -> bool:
        """Record (UPSERT) one handoff's epic/wave plan row. Returns True on a
        successful persist, False when the DB can't answer. A blank handoff_id is
        rejected (False). `wave` is clamped to >= 0 (a negative wave is meaningless;
        wave 0 = dispatch-immediately). Re-recording the same handoff updates its
        epic/wave in place (so the operator can re-plan)."""
        hid = (handoff_id or "").strip()
        if not hid:
            return False
        conn = self._get_conn()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO handoff_orchestration
                        (handoff_id, project, epic, wave, created_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (handoff_id) DO UPDATE
                       SET project = EXCLUDED.project,
                           epic = EXCLUDED.epic,
                           wave = EXCLUDED.wave
                    """,
                    (
                        hid,
                        (project or "").strip().lower() or None,
                        (epic or "").strip() or None,
                        max(0, int(wave)),
                    ),
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB orchestration write failed (ignored): %s", exc)
            return False

    def list_handoff_plan(
        self, project: str | None = None, epic: str | None = None
    ) -> list[dict[str, Any]] | _Unavailable:
        """Return the wave-plan rows (optionally scoped to a project and/or epic),
        ordered by epic then wave then handoff_id. Each row is
        {handoff_id, project, epic, wave}. UNAVAILABLE when the DB can't answer
        (the CLI prints a clear 'app-DB unavailable' message). Used by
        `cole-plan --show` to print the planned DAG."""
        conn = self._get_conn()
        if conn is None:
            return UNAVAILABLE
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("project = %s")
            params.append((project or "").strip().lower())
        if epic:
            clauses.append("epic = %s")
            params.append((epic or "").strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT handoff_id, project, epic, wave FROM handoff_orchestration"
                    + where
                    + " ORDER BY epic NULLS FIRST, wave, handoff_id",
                    tuple(params),
                )
                rows = cur.fetchall()
            return [
                {
                    "handoff_id": r[0],
                    "project": r[1],
                    "epic": r[2],
                    "wave": int(r[3]) if r[3] is not None else 0,
                }
                for r in rows
            ]
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB orchestration list failed (degraded): %s", exc)
            return UNAVAILABLE

    def replace_all_agent_overrides(
        self, blob: dict[str, dict[str, str]]
    ) -> bool:
        """Bulk-import a whole {"{project}:{agent}": {field}} map (used ONCE by the
        JSON→app-DB migration). Upserts every entry; does not delete rows absent
        from `blob` (idempotent + additive). Returns True on success."""
        if not blob:
            return True
        conn = self._get_conn()
        if conn is None:
            return False
        rows: list[tuple] = []
        for key, entry in blob.items():
            if not isinstance(entry, dict) or not entry:
                continue
            proj, _, name = str(key).partition(":")
            proj = proj.strip().lower()
            name = name.strip().lower()
            if not proj or not name:
                continue
            vals = [entry.get(col) or None for col in _AGENT_OVERRIDE_COLS]
            rows.append((proj, name, *vals))
        if not rows:
            return True
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS auto_dispatch TEXT")
                cur.executemany(
                    """
                    INSERT INTO agent_settings
                        (project, agent, harness, model, reasoning, designation,
                         role, role_aliases, auto_dispatch, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (project, agent) DO UPDATE
                       SET harness = EXCLUDED.harness,
                           model = EXCLUDED.model,
                           reasoning = EXCLUDED.reasoning,
                           designation = EXCLUDED.designation,
                           role = EXCLUDED.role,
                           role_aliases = EXCLUDED.role_aliases,
                           auto_dispatch = EXCLUDED.auto_dispatch,
                           updated_at = now()
                    """,
                    rows,
                )
            return True
        except Exception as exc:
            self._drop_conn()
            log.warning("app-DB agent-settings bulk import failed (ignored): %s", exc)
            return False


# Module-level shared settings store (lazy connection). app/settings.py uses this
# as its primary backend, falling back to the local JSON file when it reports
# UNAVAILABLE (DB down / psycopg2 missing).
settings_db = SettingsDB()
