"""Security for the settings WRITE surface (v0.1.142): SSRF guard on operator-supplied
provider URLs + the admin gate (require_admin_if_auth) on every settings mutation route.

The SSRF guard is deliberately NARROW: it blocks ONLY the cloud-metadata endpoint
(the classic SSRF target) and ALLOWS localhost/LAN so local LLM servers (ollama, vLLM)
keep working. The admin gate's logic is covered in test_require_admin_if_auth.py; here we
prove the SSRF path end-to-end through the custom-provider add handler.
"""

from __future__ import annotations

import pytest

from app.settings_module.api import _provider_url_blocked, custom_provider_add_endpoint


def test_ssrf_helper_blocks_metadata_allows_local():
    assert _provider_url_blocked("https://169.254.169.254/latest/meta-data")
    assert _provider_url_blocked("http://metadata.google.internal/computeMetadata")
    # public + local are allowed (local LLM servers legitimately live on localhost/LAN).
    assert _provider_url_blocked("https://api.openai.com/v1") is None
    assert _provider_url_blocked("http://localhost:11434") is None
    assert _provider_url_blocked("http://192.168.1.50:8000") is None
    assert _provider_url_blocked("") is None


class _FakeCPStore:
    def __init__(self):
        self.added: list[tuple[str, str]] = []

    def add_custom_provider(self, name, base_url, api_key):
        self.added.append((name, base_url))
        return {"name": name}

    def view_custom_providers(self):
        return []


@pytest.mark.asyncio
async def test_custom_provider_add_rejects_metadata_url_before_persist():
    store = _FakeCPStore()
    res = await custom_provider_add_endpoint(
        "p", {"name": "evil", "base_url": "http://169.254.169.254/", "api_key": "x"},
        store=store, _admin=None,
    )
    assert res["ok"] is False
    assert "metadata" in (res["error"] or "").lower()
    assert store.added == []  # the SSRF guard fired BEFORE the write


@pytest.mark.asyncio
async def test_custom_provider_add_allows_local_llm_url():
    store = _FakeCPStore()
    res = await custom_provider_add_endpoint(
        "p", {"name": "ollama", "base_url": "http://localhost:11434", "api_key": ""},
        store=store, _admin=None,
    )
    assert res["ok"] is True
    assert store.added and store.added[0][1] == "http://localhost:11434"
