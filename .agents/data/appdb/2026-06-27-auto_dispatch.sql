-- Kaidera OS App-DB migration (2026-06-27) — add auto_dispatch to agent_settings.
--
-- Designation now means UI/runtime category:
--   interactive = chat-capable lead
--   autonomous  = non-interactive AI worker
--
-- Some real projects need their interactive lead to also execute queued work
-- under app-managed automation. This column stores that explicit per-agent
-- permission as "true" / "false" in the console-local override layer.
--
-- Apply (once the container is up):
--   docker exec -i harness-appdb psql -U harness -d harness_app < .agents/data/appdb/2026-06-27-auto_dispatch.sql
--
-- Idempotent: safe to re-run.

ALTER TABLE agent_settings
    ADD COLUMN IF NOT EXISTS auto_dispatch TEXT;
