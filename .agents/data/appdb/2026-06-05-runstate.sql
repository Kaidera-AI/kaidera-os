-- Kaidera OS App-DB migration (2026-06-05) — RUN-STATE SINGLE SOURCE OF TRUTH.
-- Core re-architecture, Milestone 1 (RunState SSoT). Ratified design:
-- docs/2026-06-05-core-architecture-design.md · plan:
-- docs/superpowers/plans/2026-06-05-milestone-1-runstate-ssot.md (T1).
--
-- WHAT + WHY: today "what each agent is doing right now" lives in FOUR unsynced
-- stores (in-memory transcript · ~/.cortex-feed side-file · Cortex /history ·
-- chat SSE) that never reconcile, all in process memory (cleared on restart, and
-- never populated for autonomous runs whose worker is spawned stdout=DEVNULL).
-- This migration creates the ONE durable source of truth in the app-DB:
--
--   run_state — one row per run: the header (project/agent/handoff/harness/model),
--     live status, token + cost telemetry, and the load-bearing NEW signals — a
--     `pid` and a `heartbeat_at` the detached worker writes directly, giving the
--     watchdog REAL liveness instead of grepping Cortex CLI text.
--
--   run_span — append-only think/tool/output events for a run (the transcript
--     body), keyed (run_id, seq) so a double-write is a no-op, never corruption.
--
--   notify_run_state() + two AFTER triggers — pg_notify the `run_state_events`
--     bus on every insert/update (the app-DB twin of Cortex's existing
--     `cortex_events` NOTIFY bus). The payload is a tiny JSON {run_id, project}
--     WAKE signal only: SSE + the HTTP route both RE-READ the row, so first-paint
--     and the live push read the same model and cannot disagree.
--
-- Nothing reads this yet (T1 is the foundation); the Pg adapter (T3), the
-- orchestrator/worker writers (T5/T6), and the readers (T7+) land later.
--
-- Container: harness-appdb (postgres:17-alpine) on host port 5500, DB harness_app.
-- This is NOT cortex-pg and NOT the Kaidera AI platform DB. Local machine only.
--
-- Apply (once the container is up):
--   docker exec -i harness-appdb psql -U harness -d harness_app \
--     < .agents/data/appdb/2026-06-05-runstate.sql
-- (M1 T13 adds a `harness-appdb-migrate` one-shot that applies these idempotently.)
--
-- Idempotent: safe to re-run — CREATE ... IF NOT EXISTS for tables/indexes,
-- CREATE OR REPLACE FUNCTION for the trigger fn, and DROP TRIGGER IF EXISTS
-- before each CREATE TRIGGER (so re-applying never double-fires a trigger).

-- ---------------------------------------------------------------------------
--  run_state — ONE row per run (the header + live status + telemetry + liveness).
--
--  run_id is a caller-supplied uuid4 string (the orchestrator pre-creates it and
--  passes it to the detached worker as argv, so the worker writes THIS row's
--  spans + heartbeat directly — no dependency on the console it is making
--  restartable). status walks queued → running → ok|error (a small text enum,
--  not a CHECK, so new states never need a migration). heartbeat_at is the key
--  new signal: the worker bumps it on a cadence; a stale heartbeat_at = a dead
--  run (the watchdog reads this, replacing CLI-text grepping). pid/lease_owner
--  give a process/lease registry for supervision. Token + cost columns mirror
--  usage_events so the run header carries its own running totals.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_state (
    run_id         TEXT PRIMARY KEY,                       -- caller-supplied uuid4
    project        TEXT,                                   -- project key (lower-cased)
    agent          TEXT,                                   -- agent name (lower-cased)
    agent_display  TEXT,                                   -- pretty agent label for the UI
    handoff_id     TEXT,                                   -- Cortex handoff UUID (if dispatched)
    harness        TEXT,                                   -- execution lane (claude-code / pi / ...)
    model          TEXT,                                   -- resolved model id / alias
    status         TEXT NOT NULL DEFAULT 'queued',         -- queued | running | ok | error
    error          TEXT,                                   -- failure detail when status='error'
    pid            INTEGER,                                -- worker OS pid (process registry)
    lease_owner    TEXT,                                   -- who/what holds the run lease
    tokens_in      BIGINT,                                 -- prompt tokens (running total)
    tokens_out     BIGINT,                                 -- completion tokens (running total)
    cost_est_usd   NUMERIC,                                -- estimated cost in USD
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),     -- run opened
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),     -- last write to this row
    heartbeat_at   TIMESTAMPTZ,                            -- last worker liveness ping (THE signal)
    ended_at       TIMESTAMPTZ                             -- run finished (ok|error)
);

-- Recent-runs list + the crew/dashboard "what's live now", newest-first per project.
CREATE INDEX IF NOT EXISTS idx_run_state_project_started
    ON run_state (project, started_at DESC);

-- "Active runs" scans (status in queued|running) and watchdog staleness sweeps.
CREATE INDEX IF NOT EXISTS idx_run_state_status
    ON run_state (status);

-- by_handoff: land the crew view on a handoff and find its (latest) run; the
-- watchdog also looks a run up by handoff_id. Newest run for a handoff wins.
CREATE INDEX IF NOT EXISTS idx_run_state_handoff
    ON run_state (handoff_id, started_at DESC);

-- ---------------------------------------------------------------------------
--  run_span — APPEND-ONLY transcript events for a run (think / tool / output).
--
--  One row per streamed segment. `seq` is a per-run monotonic counter the writer
--  assigns; UNIQUE(run_id, seq) makes a re-delivered segment an idempotent no-op
--  (a double-write across processes = ON CONFLICT DO NOTHING, never corruption,
--  per the run_id-collision mitigation). FK→run_state ON DELETE CASCADE so a
--  run's spans are reclaimed with it (recent-runs trimming stays a single DELETE).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_span (
    id      BIGSERIAL PRIMARY KEY,                          -- surrogate append id
    run_id  TEXT NOT NULL
            REFERENCES run_state (run_id) ON DELETE CASCADE,-- owning run
    seq     INTEGER NOT NULL,                               -- per-run monotonic order
    kind    TEXT NOT NULL,                                  -- 'think' | 'tool' | 'output' | ...
    text    TEXT,                                           -- the segment payload
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),             -- when the segment landed
    UNIQUE (run_id, seq)                                    -- idempotent re-write key
);

-- Read a run's transcript in order (the by_handoff/get_run body fetch). The
-- UNIQUE(run_id, seq) above already indexes (run_id, seq); this explicit index
-- is IF NOT EXISTS so the migration documents the intended read path and stays
-- idempotent even if the unique-constraint index name ever changes.
CREATE INDEX IF NOT EXISTS idx_run_span_run_seq
    ON run_span (run_id, seq);

-- ---------------------------------------------------------------------------
--  notify_run_state() + triggers — the run_state_events NOTIFY bus.
--
--  Fires AFTER every INSERT and UPDATE on run_state and pg_notify's the
--  `run_state_events` channel with a tiny JSON {run_id, project} payload. This is
--  ONLY a wake signal — listeners (the SSE push + the HTTP route) re-read the
--  authoritative row via the port, so the live push and first-paint can never
--  disagree. CREATE OR REPLACE keeps re-apply idempotent; DROP TRIGGER IF EXISTS
--  before each CREATE TRIGGER means a re-run never stacks duplicate triggers.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION notify_run_state() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'run_state_events',
        json_build_object(
            'run_id',  NEW.run_id,
            'project', NEW.project
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS run_state_notify_insert ON run_state;
CREATE TRIGGER run_state_notify_insert
    AFTER INSERT ON run_state
    FOR EACH ROW EXECUTE FUNCTION notify_run_state();

DROP TRIGGER IF EXISTS run_state_notify_update ON run_state;
CREATE TRIGGER run_state_notify_update
    AFTER UPDATE ON run_state
    FOR EACH ROW EXECUTE FUNCTION notify_run_state();
