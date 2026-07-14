-- Kaidera OS App-DB migration (2026-06-01) — CONSOLE SETTINGS move into the app-DB.
-- E007 / DATA_SEPARATION.md: the app-DB is the console's OPERATIONAL store
-- (settings + usage/billing). Cortex (cortex-pg) stays pure agent memory.
--
-- This migration moves ALL console settings out of config/settings.local.json
-- and into the app-DB so harness/model routing + system config + custom
-- providers + per-agent overrides survive a server restart from a durable store
-- (the JSON file is kept only as a fallback/seed). Agent harness/model routing is
-- OPERATIONAL (which lane runs an agent), so it lives HERE, not in Cortex.
--
-- Container: harness-appdb (postgres:17-alpine) on host port 5500, DB harness_app.
-- This is NOT cortex-pg and NOT the Kaidera AI platform DB. Local machine only.
--
-- Apply (once the container is up):
--   docker exec -i harness-appdb psql -U harness -d harness_app < .agents/data/appdb/2026-06-01-settings.sql
--
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS throughout).

-- ---------------------------------------------------------------------------
--  app_settings — the console's app/system settings as a small key→JSON store.
--
--  Holds the System-config page fields (Cortex connection, provider API keys,
--  harness paths/flags, app prefs) AND the open-ended side blobs that used to
--  live alongside them in the JSON file:
--    * one row PER System schema field        (key = the field key, e.g.
--      "cortex_base_url", "anthropic_api_key")
--    * one row "custom_providers"             (value = JSON array of providers)
--    * one row "_designation_seed_applied"    (value = JSON true marker)
--
--  Each value is stored as JSONB so a string / bool / int / list round-trips
--  with its type intact (the same shape the JSON file held). Secrets are stored
--  verbatim here exactly as the gitignored JSON file held them — this is a local,
--  single-user store on loopback Postgres; never render the raw value (the app
--  masks secrets in every HTML/serialization path, unchanged).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,          -- setting key (schema field key / side-blob name)
    value       JSONB NOT NULL,            -- the typed value (string/bool/int/list)
    updated_at  TIMESTAMPTZ DEFAULT now()  -- last write
);

-- ---------------------------------------------------------------------------
--  agent_settings — per-agent console overrides, keyed by (project, agent).
--
--  Mirrors the JSON `agent_overrides["{project}:{agent}"]` blob, one row per
--  agent. Each override field is a nullable column; a NULL/absent column means
--  "no override — fall back to the registry value" (same semantics as the JSON
--  blob, where a missing key meant no override). The console layers these over
--  the Cortex registry value for display + classification + harness routing.
--
--    harness      : execution lane (claude-code / codex / kaidera / pi)
--    model        : model id / alias for that harness
--    reasoning    : reasoning / effort level for that harness
--    designation  : "interactive" | "autonomous" (wins over the registry heuristic)
--    role         : free-text role label override
--    role_aliases : comma-separated secondary dispatch roles (added 2026-06-22)
--    auto_dispatch: "true" | "false"; explicit permission for an interactive
--                   lead to execute queued work while remaining chat-capable
--
--  TODO(registry-sync): the eventual source of truth is the Cortex agent
--  registry (capabilities + roster_policy, E006 Inc04). Until that lands these
--  operational overrides live here; syncing them back to Cortex is a follow-up.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_settings (
    project      TEXT NOT NULL,            -- project key
    agent        TEXT NOT NULL,            -- agent name, lower-cased
    harness      TEXT,                     -- override: execution lane
    model        TEXT,                     -- override: model id / alias
    reasoning    TEXT,                     -- override: reasoning / effort
    designation  TEXT,                     -- override: interactive | autonomous
    role         TEXT,                     -- override: free-text role label
    role_aliases TEXT,                     -- override: secondary dispatch roles
    auto_dispatch TEXT,                    -- override: interactive lead may auto-run work
    updated_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (project, agent)
);

-- Lookup helper for "all overrides for a project" (the col-2 grouping + Configure
-- page load every project's agents at once).
CREATE INDEX IF NOT EXISTS idx_agent_settings_project ON agent_settings (project);
