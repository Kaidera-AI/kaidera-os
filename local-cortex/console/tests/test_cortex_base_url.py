"""Cortex API base-URL resolution.

Regression guard for the env-var-name bug that blanked the whole Cortex surface
inside Docker: the client read only the legacy ``CORTEX_BASE_URL`` env (which
nothing sets), so a containerized console silently pinned to ``localhost:8501``
and every project-scoped read degraded to ``[]``. The fix reads ``CORTEX_API_URL``
first — the convention the compose file, ``.envrc`` and the boot probe all use —
so the same image is correct on the host (localhost) and in the container
(cortex-api).
"""

import app.cortex_client as cc


def test_prefers_cortex_api_url(monkeypatch):
    monkeypatch.setenv("CORTEX_API_URL", "http://cortex-api:8501")
    monkeypatch.setenv("CORTEX_BASE_URL", "http://legacy:9999")
    assert cc._resolve_base_url() == "http://cortex-api:8501"


def test_falls_back_to_legacy_base_url(monkeypatch):
    monkeypatch.delenv("CORTEX_API_URL", raising=False)
    monkeypatch.setenv("CORTEX_BASE_URL", "http://legacy:8501")
    assert cc._resolve_base_url() == "http://legacy:8501"


def test_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("CORTEX_API_URL", raising=False)
    monkeypatch.delenv("CORTEX_BASE_URL", raising=False)
    assert cc._resolve_base_url() == "http://localhost:8501"


def test_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("CORTEX_API_URL", "http://cortex-api:8501/")
    assert cc._resolve_base_url() == "http://cortex-api:8501"
