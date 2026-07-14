"""Layer-1 provider gate (app/providers.py + app/edition.py): the PUBLIC edition
exposes ONLY the Kaidera AI Manifold provider — programmatically, never via a license."""

from __future__ import annotations

from app import providers


def test_visible_providers_full_in_dev(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    vis = providers.visible_providers()
    assert vis == providers.PROVIDER_ORDER
    assert "anthropic" in vis and "kaidera-manifold" in vis


def test_visible_providers_manifold_only_in_public(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    assert providers.visible_providers() == ["kaidera-manifold"]


def test_builtin_provider_config_filters_to_manifold_in_public(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    cfg = {"anthropic_api_key": "sk-should-be-hidden"}
    names = [p["name"] for p in providers.builtin_provider_config(cfg)]
    assert names == ["kaidera-manifold"]


def test_builtin_provider_config_full_in_dev(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    names = [p["name"] for p in providers.builtin_provider_config({})]
    assert "anthropic" in names and "kaidera-manifold" in names


def test_resolve_provider_key_refuses_non_manifold_in_public(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    # A hand-edited settings row carrying an anthropic key must NOT resolve at runtime.
    assert providers._resolve_provider_key({"anthropic_api_key": "sk-x"}, "anthropic_api_key") == ""


def test_resolve_provider_key_allows_in_dev(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    assert providers._resolve_provider_key({"anthropic_api_key": "sk-x"}, "anthropic_api_key") == "sk-x"
