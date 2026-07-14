-- 002_profile_bundles_harness_artifacts_memory_sync_events.sql
-- Local-first validation schema for the harness-agnostic Cortex bootstrap
-- contract. Identity v2 clean cutover uses project_id as the scope key; the old
-- customer/project hex columns are not part of greenfield schemas.

BEGIN;

CREATE TABLE IF NOT EXISTS profile_bundles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES cortex_projects(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    version INT NOT NULL,
    content_hash TEXT NOT NULL,
    source_paths JSONB NOT NULL,
    rendered_markdown TEXT NOT NULL,
    rendered_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, agent_name, version)
);

CREATE INDEX IF NOT EXISTS idx_profile_bundles_scope_agent
    ON profile_bundles (project_id, lower(agent_name));
CREATE INDEX IF NOT EXISTS idx_profile_bundles_hash
    ON profile_bundles (content_hash);

CREATE TABLE IF NOT EXISTS harness_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES cortex_projects(id) ON DELETE CASCADE,
    harness TEXT NOT NULL,
    path TEXT NOT NULL,
    generated_from_hash TEXT NOT NULL,
    last_compiled_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('current', 'stale', 'error')),
    UNIQUE (project_id, harness, path)
);

CREATE INDEX IF NOT EXISTS idx_harness_artifacts_project_harness
    ON harness_artifacts (project_id, harness);
CREATE INDEX IF NOT EXISTS idx_harness_artifacts_hash
    ON harness_artifacts (generated_from_hash);
CREATE INDEX IF NOT EXISTS idx_harness_artifacts_status
    ON harness_artifacts (status);

CREATE TABLE IF NOT EXISTS memory_sync_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES cortex_projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('to-cortex', 'to-harness')),
    content_hash TEXT NOT NULL,
    conflict_policy TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('accepted', 'rejected', 'inbox')),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_sync_events_scope_created
    ON memory_sync_events (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_sync_events_source_target
    ON memory_sync_events (source, target);
CREATE INDEX IF NOT EXISTS idx_memory_sync_events_result
    ON memory_sync_events (result);

COMMIT;
