from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_project_registration_allows_large_key_migrations_to_finish():
    script = (ROOT / ".agents" / "scripts" / "cortex-init-project").read_text()

    assert "CORTEX_PROJECT_REGISTER_TIMEOUT_S:-1800" in script
    assert 'cortex_api_call_admin POST "/projects"' in script
