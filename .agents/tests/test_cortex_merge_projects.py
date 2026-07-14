from __future__ import annotations

import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cortex-merge-projects"


def _script_text() -> str:
    return SCRIPT.read_text()


def _index(text: str, needle: str) -> int:
    index = text.find(needle)
    assert index >= 0, f"missing expected SQL fragment: {needle}"
    return index


def test_cortex_merge_projects_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_cortex_merge_projects_counts_all_graph_tables():
    text = _script_text()

    assert "SELECT 'cortex_entities'" in text
    assert "SELECT 'cortex_relationships'" in text


def test_cortex_merge_projects_graph_merge_is_fk_safe():
    text = _script_text()

    entity_resolution = _index(text, "CREATE TEMP TABLE merge_graph_entity_resolution")
    entity_properties = _index(text, "UPDATE cortex_entities canonical")
    relationship_resolution = _index(text, "CREATE TEMP TABLE merge_graph_relationship_resolution")
    relationship_properties = _index(text, "UPDATE cortex_relationships canonical")
    delete_duplicate_relationships = _index(text, "DELETE FROM cortex_relationships r")
    rewrite_relationships = _index(text, "UPDATE cortex_relationships r")
    delete_duplicate_entities = _index(text, "DELETE FROM cortex_entities e")
    move_surviving_entities = _index(text, "UPDATE cortex_entities e")

    assert entity_resolution < entity_properties
    assert entity_properties < relationship_resolution
    assert relationship_resolution < relationship_properties
    assert relationship_properties < delete_duplicate_relationships
    assert delete_duplicate_relationships < rewrite_relationships
    assert rewrite_relationships < delete_duplicate_entities
    assert delete_duplicate_entities < move_surviving_entities


def test_cortex_merge_projects_graph_merge_uses_target_project_natural_keys():
    text = _script_text()

    assert "PARTITION BY name, entity_type" in text
    assert "ORDER BY project_rank, created_at, entity_id" in text
    assert (
        "PARTITION BY canonical_source_entity_id, canonical_target_entity_id, relationship_type"
        in text
    )
    assert "ORDER BY project_rank, created_at, relationship_id" in text
    assert "COALESCE(source_entity.canonical_entity_id, r.source_entity_id)" in text
    assert "COALESCE(target_entity.canonical_entity_id, r.target_entity_id)" in text
