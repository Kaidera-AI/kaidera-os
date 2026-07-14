-- 003_artifacts_api_surface.sql
-- Local Cortex L5 artifact tables and enrichment columns.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    customer_id UUID,
    org_id UUID,
    parent_artifact_id UUID REFERENCES artifacts(id) ON DELETE SET NULL,
    modality TEXT,
    source_type TEXT,
    source_file TEXT NOT NULL,
    extraction_method TEXT,
    content_hash TEXT NOT NULL,
    raw_content TEXT,
    section_context TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    embedding VECTOR(2048),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    caption TEXT,
    neighborhood_text TEXT,
    source_doc_metadata JSONB DEFAULT '{}'::jsonb,
    UNIQUE (project, source_file, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts (project);
CREATE INDEX IF NOT EXISTS idx_artifacts_source_file ON artifacts (source_file);
CREATE INDEX IF NOT EXISTS idx_artifacts_modality ON artifacts (modality);
CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts (parent_artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_caption_trgm
    ON artifacts USING GIN (LOWER(COALESCE(caption, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_artifacts_neighborhood_trgm
    ON artifacts USING GIN (LOWER(COALESCE(neighborhood_text, '')) gin_trgm_ops);

ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS caption TEXT;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS neighborhood_text TEXT;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS source_doc_metadata JSONB DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS artifact_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    source_id UUID NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (project, source_id, target_type, target_ref, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_artifact_edges_source ON artifact_edges (source_id);
CREATE INDEX IF NOT EXISTS idx_artifact_edges_target ON artifact_edges (target_type, target_ref);
CREATE INDEX IF NOT EXISTS idx_artifact_edges_project ON artifact_edges (project);
