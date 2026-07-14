from __future__ import annotations

from app import edition
from app import providers


def test_public_source_is_immutable_open_source() -> None:
    assert edition.edition() == "open-source"
    assert edition.is_open_source()


def test_only_manifold_is_visible_even_when_environment_requests_dev(monkeypatch) -> None:
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    assert providers.visible_providers() == ["kaidera-manifold"]


def test_unknown_provider_key_never_resolves() -> None:
    assert providers._resolve_provider_key(
        {"third_party_api_key": "secret"},
        "third_party_api_key",
    ) == ""
