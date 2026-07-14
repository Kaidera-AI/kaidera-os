-- Kaidera OS App-DB migration (2026-06-04) — PROPOSE-MODE + PENDING-APPROVAL.
-- PM Relentless Beat, Increment 1 (training-wheels safety gate).
--
-- Two tables:
--
--   project_propose_mode — per-project boolean flag. When enabled=TRUE, the
--     Dispatch scheduler parks each ready handoff in `pending_approval` instead
--     of auto-spawning it. The human operator clicks Approve in the Dispatch
--     view; the next sweep then spawns normally. Fail-safe default is FALSE
--     (auto-spawn = existing behaviour), so EVERY existing project is unaffected
--     until an operator explicitly sets the flag.
--
--   pending_approval — one row per handoff that Dispatch has parked for human
--     approval (in a propose-mode project). The `status` column tracks whether
--     the handoff is 'awaiting' (parked, not yet approved) or 'approved'
--     (operator clicked Approve; next Dispatch sweep will spawn it). Rows are
--     NOT deleted on approval — the status column drives the gate instead.
--     list_awaiting_approval only returns rows with status='awaiting', so the
--     UI queue stays clean. Idempotent park (UPSERT on status='awaiting') and
--     idempotent approve (UPDATE to 'approved'; no error on a missing row).
--
-- Container: harness-appdb (postgres:17-alpine) on host port 5500, DB harness_app.
-- This is NOT cortex-pg and NOT the Kaidera AI platform DB. Local machine only.
--
-- Apply (once the container is up):
--   docker exec -i harness-appdb psql -U harness -d harness_app \
--     < .agents/data/appdb/2026-06-04-propose-mode.sql
--
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS; ALTER ... ADD COLUMN IF
-- NOT EXISTS; no seed rows).
--
-- OPERATOR / DEPLOY NOTE (Issue 4 — DB-outage fail-safe behaviour):
--   When the app-DB is unreachable:
--     * is_propose_mode() reads False  → Dispatch auto-spawns (gate is effectively
--       OFF). A DB hiccup silently disables the propose-mode gate.
--     * list_awaiting_approval() returns []  → the Approve queue in the Dispatch
--       view goes empty and the Approve buttons disappear.
--   This is an intentional fail-safe (no dispatch can be stranded), but it means
--   operators won't see the approval queue during a DB outage. Bring the app-DB
--   back up to restore the gate. Monitor 'app-DB settings unreachable' log lines.

-- ---------------------------------------------------------------------------
--  project_propose_mode — per-project propose-mode training-wheels gate.
--
--  Mirrors the shape of project_autonomy exactly: one row per project that has
--  had the flag touched, enabled column carries the state, absent row = FALSE
--  (the safe default, preserving existing auto-spawn behaviour). Defaulting the
--  column to FALSE means even a bare INSERT lands OFF.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_propose_mode (
    project     TEXT PRIMARY KEY,                 -- project key
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,   -- propose-mode gate; FALSE = auto-spawn (default)
    updated_by  TEXT,                             -- who last changed it (operator/agent name)
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
--  pending_approval — handoffs parked by Dispatch awaiting human approval.
--
--  Written by _maybe_dispatch (UPSERT, status='awaiting') when propose_mode is
--  ON for the project. The approve route sets status='approved'; the next
--  Dispatch sweep sees status='approved' and falls through to the normal spawn
--  path. Rows are never deleted — status drives the gate. The (project,
--  handoff_id) pair is the natural key; project scopes lookups to one project's
--  queue. created_at lets the queue be shown oldest-first.
--
--  status values:
--    'awaiting'  — parked, waiting for human approval (the normal post-park state)
--    'approved'  — operator clicked Approve; next sweep will spawn
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_approval (
    project     TEXT        NOT NULL,             -- project key (lower-cased)
    handoff_id  TEXT        NOT NULL,             -- Cortex handoff UUID (the dispatch key)
    status      TEXT        NOT NULL DEFAULT 'awaiting',  -- 'awaiting' | 'approved'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project, handoff_id)
);

-- Add status column to an existing table (idempotent; no-op on a fresh install
-- because CREATE TABLE IF NOT EXISTS already includes it).
ALTER TABLE pending_approval ADD COLUMN IF NOT EXISTS
    status TEXT NOT NULL DEFAULT 'awaiting';

-- Read-path index for "list a project's approval queue" (the Dispatch view +
-- the approve route check). Filtered to status='awaiting' for the queue.
CREATE INDEX IF NOT EXISTS idx_pending_approval_project
    ON pending_approval (project);

CREATE INDEX IF NOT EXISTS idx_pending_approval_project_status
    ON pending_approval (project, status);
