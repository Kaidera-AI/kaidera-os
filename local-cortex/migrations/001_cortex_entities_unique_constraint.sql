-- 001_cortex_entities_unique_constraint.sql
-- Adds the natural unique key on cortex_entities + cortex_relationships.
-- Optional but recommended — lets cortex-extract-entities use a single
-- ON CONFLICT clause instead of the manual UPDATE-then-INSERT-WHERE-NOT-EXISTS dance.
--
-- Apply when ready:
--   PGPASSWORD=$POSTGRES_PASSWORD psql -h localhost -p 5499 \
--     -U $POSTGRES_USER -d $POSTGRES_DB \
--     -f local-cortex/migrations/001_cortex_entities_unique_constraint.sql
--
-- Pre-flight check: verify no duplicates exist first
--   SELECT project, name, entity_type, COUNT(*) FROM cortex_entities
--   GROUP BY project, name, entity_type HAVING COUNT(*) > 1;

BEGIN;

-- Entities: natural key is (project, name, entity_type)
ALTER TABLE cortex_entities
    ADD CONSTRAINT cortex_entities_natural_key
    UNIQUE (project, name, entity_type);

-- Relationships: natural key is (project, source_entity_id, target_entity_id, relationship_type)
-- This prevents duplicate edges of the same type between the same pair of nodes.
ALTER TABLE cortex_relationships
    ADD CONSTRAINT cortex_relationships_natural_key
    UNIQUE (project, source_entity_id, target_entity_id, relationship_type);

COMMIT;
