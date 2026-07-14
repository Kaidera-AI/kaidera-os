-- Kaidera OS App-DB migration (2026-06-27) — Automation feeders.
--
-- Durable scheduled jobs are console operational state. They live in the
-- app-DB, not Cortex. Cortex only receives the resulting handoffs.

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    project      TEXT NOT NULL,
    id           TEXT NOT NULL,
    name         TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    schedule     JSONB NOT NULL,
    payload      JSONB NOT NULL,
    next_run_at  TIMESTAMPTZ,
    last_run_at  TIMESTAMPTZ,
    last_status  TEXT,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project, id)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
    ON scheduled_jobs (project, enabled, next_run_at);
