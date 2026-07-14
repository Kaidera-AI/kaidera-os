-- Kaidera OS App-DB migration (2026-06-07) — CHAT MULTI-TURN SESSION threading.
-- Feature-gap step 6, Increment B (multi-turn conversation context for chat).
--
-- WHAT + WHY: interactive chat turns are RunState rows (lease_owner='chat'); each
-- turn today is a SINGLE-SHOT call — the harness sees only the current message, with
-- NO memory of earlier turns in the same conversation (claude-code `-p` and pi each
-- treat every call as a NEW session). This migration adds the ONE column that lets the
-- console group a conversation's turns so the next turn can thread the prior turns'
-- (user, assistant) text into the prompt:
--
--   run_state.session_id — a stable per-conversation uuid the SPA mints (one per
--     agent-detail chat session) and sends on every chat POST. Turns sharing a
--     session_id form one conversation; load_session_history reads the recent
--     lease_owner='chat' rows filtered by session_id to rebuild the thread.
--
-- ADDITIVE + BACKWARD-COMPATIBLE: the column is NULLABLE with no default. A turn with
-- NO session_id (today's behaviour, the legacy `api.chat(project, agent, message)`
-- path) stores session_id=NULL → no history → single-shot, identical to today. Only a
-- turn that carries a session_id participates in threading.
--
-- Container: harness-appdb (postgres:17-alpine) on host port 5500, DB harness_app.
-- This is NOT cortex-pg and NOT the Kaidera AI platform DB. Local machine only.
--
-- Apply: the harness-appdb-migrate one-shot applies every .agents/data/appdb/*.sql in
-- lexical order, idempotently, on deploy (this sorts AFTER 2026-06-05-runstate.sql, so
-- run_state already exists). Manual re-apply is safe too:
--   docker exec -i harness-appdb psql -U harness -d harness_app \
--     < .agents/data/appdb/2026-06-07-chat-session.sql
--
-- Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS —
-- safe to re-run; a no-op once converged.

-- ---------------------------------------------------------------------------
--  run_state.session_id — the per-conversation grouping key for chat turns.
--
--  VARCHAR(36) holds a uuid4 string (the SPA mints `crypto.randomUUID()`); NULL for
--  every non-session turn (the safe default = single-shot, preserving existing
--  behaviour). Lives on run_state next to lease_owner so a chat conversation's turns
--  are grouped by (project, agent, session_id) without a second table.
-- ---------------------------------------------------------------------------
ALTER TABLE run_state ADD COLUMN IF NOT EXISTS session_id VARCHAR(36);

-- Read-path index for load_session_history: "find a conversation's recent chat turns"
-- (filtered by session_id, newest-first). PARTIAL (WHERE session_id IS NOT NULL) so it
-- only indexes the rows that actually participate in threading — the overwhelming
-- majority of NULL (single-shot / autonomous) rows stay out of the index, keeping it
-- small. The query orders by started_at; the planner pairs this with the existing
-- (project, started_at DESC) index for the recent-turns scan.
CREATE INDEX IF NOT EXISTS ix_run_state_session
    ON run_state (session_id)
    WHERE session_id IS NOT NULL;
