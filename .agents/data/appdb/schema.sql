-- Kaidera OS App-DB schema — the lightweight OPERATIONAL store.
-- E007 / DATA_SEPARATION.md: usage/token telemetry, cost/billing, and analytics
-- live HERE, separate from Cortex (cortex-pg is agent memory + coordination only).
--
-- Container: harness-appdb (postgres:17-alpine) on host port 5500, DB harness_app.
-- This is NOT cortex-pg and NOT the Kaidera AI platform DB. Local machine only.
--
-- Apply once the container is up:
--   docker exec -i harness-appdb psql -U harness -d harness_app < .agents/data/appdb/schema.sql
--
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS throughout).

-- ---------------------------------------------------------------------------
--  usage_events — one row per agent/chat/harness run.
--
--  Captures the per-run token usage (in/out), the resolved model + upstream
--  provider, and an estimated USD cost (providers pricing × tokens, or the
--  harness's own reported cost). The Analytics view reads this directly (via
--  app/appdb.py), replacing the old Cortex /history token-frame derivation.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_events (
    id            BIGSERIAL PRIMARY KEY,
    project       TEXT,                      -- project key
    agent         TEXT,                      -- agent name
    harness       TEXT,                      -- execution lane (e.g. "claude-code")
    model         TEXT,                      -- resolved model id / alias
    provider      TEXT,                      -- upstream provider (e.g. "anthropic")
    tokens_in     BIGINT,                    -- prompt/input tokens for the run
    tokens_out    BIGINT,                    -- completion/output tokens for the run
    cost_est_usd  NUMERIC,                   -- estimated USD cost (pricing×tokens or harness-reported)
    ts            TIMESTAMPTZ DEFAULT now()  -- when the run completed
);

-- Read-path indexes for the Analytics breakdowns (by project / project×agent /
-- project×model) and the time-window scans.
CREATE INDEX IF NOT EXISTS idx_usage_events_project        ON usage_events (project);
CREATE INDEX IF NOT EXISTS idx_usage_events_project_agent  ON usage_events (project, agent);
CREATE INDEX IF NOT EXISTS idx_usage_events_project_model  ON usage_events (project, model);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts             ON usage_events (ts);
