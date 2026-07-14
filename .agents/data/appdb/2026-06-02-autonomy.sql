-- Kaidera OS App-DB migration (2026-06-02) — PER-PROJECT AUTONOMOUS TOGGLE.
-- E007 / Phase 1 of the autonomous orchestrator ("Cole's loop").
--
-- This adds the master kill-switch for the autonomous dispatcher: a per-project
-- boolean that, when TRUE, lets the background orchestrator (Cole, Haiku) pick up
-- new pending handoffs and kick off the target agent unattended. It is an
-- OPERATIONAL flag (it controls the console's runtime behaviour, not agent
-- memory), so it lives in the app-DB next to settings + usage telemetry — NOT in
-- Cortex (cortex-pg stays pure agent memory).
--
-- HARD SAFEGUARD — SHIP DARK: there is NO row inserted here for any project, and
-- the getter treats an absent row (and an unreachable DB) as OFF. So every
-- project is OFF by default; autonomy only turns on when an operator explicitly
-- flips the toggle (which writes enabled = TRUE for that one project).
--
-- Container: harness-appdb (postgres:17-alpine) on host port 5500, DB harness_app.
-- This is NOT cortex-pg and NOT the Kaidera AI platform DB. Local machine only.
--
-- Apply (once the container is up):
--   docker exec -i harness-appdb psql -U harness -d harness_app < .agents/data/appdb/2026-06-02-autonomy.sql
--
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS; no seed rows).

-- ---------------------------------------------------------------------------
--  project_autonomy — the per-project autonomous-dispatch master switch.
--
--  One row per project that has EVER had the toggle touched. The presence of a
--  row is not "on": `enabled` carries the state. The orchestrator loop reconciles
--  the set of ON projects from this table (WHERE enabled = TRUE); a project with
--  no row is OFF (the ship-dark default). Defaulting the column to FALSE means
--  even an accidental bare INSERT lands OFF.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_autonomy (
    project     TEXT PRIMARY KEY,                 -- project key
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,   -- master switch; FALSE = OFF (default)
    updated_by  TEXT,                             -- who last flipped it (operator/agent name)
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
