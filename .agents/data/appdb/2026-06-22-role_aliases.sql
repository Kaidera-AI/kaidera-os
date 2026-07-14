-- Kaidera OS App-DB migration (2026-06-22) — add role_aliases to agent_settings.
--
-- The dispatch resolver now supports secondary dispatchable roles (e.g.
-- "creative-multimedia" -> gem) from both the Cortex registry capabilities and
-- the console-local agent_settings override. This column stores a comma-separated
-- list of alias slugs, normalized on read by app.domain.designation.clean_override.
--
-- Apply (once the container is up):
--   docker exec -i harness-appdb psql -U harness -d harness_app < .agents/data/appdb/2026-06-22-role_aliases.sql
--
-- Idempotent: safe to re-run.

ALTER TABLE agent_settings
    ADD COLUMN IF NOT EXISTS role_aliases TEXT;
