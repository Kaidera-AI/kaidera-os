-- Kaidera OS App-DB migration (2026-06-02) — HANDOFF ORCHESTRATION PLAN (WAVES).
-- E007 / Phase 1.5 of the autonomous orchestrator ("Cole's loop").
--
-- This adds the DEPENDENCY-SEQUENCING plan that lets Cole run an epic's handoffs
-- in WAVE order: parallel WITHIN a wave (capped), sequential ACROSS waves (a wave
-- is ~ an increment). Each row tags ONE handoff with the epic it belongs to and
-- the wave number it runs in. Cole reads a project's plan and dispatches only the
-- LOWEST wave (per epic) that still has incomplete handoffs; it advances to the
-- next wave only when every handoff in the current wave is complete in Cortex.
--
-- It is OPERATIONAL orchestration state (it shapes the console's runtime dispatch
-- ordering, not agent memory), so it lives in the app-DB next to settings +
-- usage telemetry + project_autonomy — NOT in Cortex (cortex-pg stays pure agent
-- memory). The handoffs themselves (status/target/summary) stay in Cortex; this
-- table only carries the wave grouping the operator assigns.
--
-- HARD SAFEGUARD — PRESERVES PHASE 1: a handoff with NO row here is treated as
-- WAVE 0 by the loop and dispatched immediately (the existing Phase-1 behaviour
-- for ad-hoc handoffs is unchanged). No row is seeded; an empty table means every
-- handoff is wave 0, i.e. exactly Phase 1. A wave > 0 row NEVER dispatches before
-- its prior waves complete.
--
-- Container: harness-appdb (postgres:17-alpine) on host port 5500, DB harness_app.
-- This is NOT cortex-pg and NOT the Kaidera AI platform DB. Local machine only.
--
-- Apply (once the container is up):
--   docker exec -i harness-appdb psql -U harness -d harness_app < .agents/data/appdb/2026-06-02-orchestration.sql
--
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS; no seed rows).

-- ---------------------------------------------------------------------------
--  handoff_orchestration — the per-handoff wave/epic tag (the dependency plan).
--
--  One row per handoff the operator has placed into a wave. The handoff_id is the
--  Cortex handoff UUID (the same id the dispatch funnel keys on). `wave` is the
--  ordering rank within its epic: lower waves run (and must complete) before
--  higher waves. `epic` groups handoffs so multiple epics sequence independently
--  (epic E007 wave 2 does not wait on epic E006). `project` scopes the plan to a
--  project (the loop reads one project's rows at a time).
--
--  Defaulting `wave` to 0 means even a bare INSERT lands a handoff in wave 0 (the
--  "dispatch immediately, no dependency" lane) — fail-safe toward Phase-1 behaviour.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS handoff_orchestration (
    handoff_id  TEXT PRIMARY KEY,                 -- Cortex handoff UUID (the dispatch key)
    project     TEXT,                             -- project key
    epic        TEXT,                             -- epic id the handoff belongs to (e.g. "E007")
    wave        INT NOT NULL DEFAULT 0,           -- ordering rank within the epic (0 = run immediately)
    created_at  TIMESTAMPTZ DEFAULT now()         -- when the plan row was recorded
);

-- Read-path index for "read a project's whole plan" (the loop's per-project scan
-- + the `cole-plan --show` DAG print) and the per-(project,epic,wave) grouping.
CREATE INDEX IF NOT EXISTS idx_handoff_orchestration_project
    ON handoff_orchestration (project);
CREATE INDEX IF NOT EXISTS idx_handoff_orchestration_project_epic_wave
    ON handoff_orchestration (project, epic, wave);
