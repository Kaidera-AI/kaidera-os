"""Dynamic reasoning effort for the public Manifold inference lane."""

from __future__ import annotations

import pytest

from app import reasoning as R


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("medium", "medium"),
        ("med", "medium"),
        ("MED", "medium"),
        ("xhi", "xhigh"),
        ("maximum", "max"),
        ("on", "_on_"),
        ("true", "_on_"),
        ("", ""),
        ("off", ""),
        ("disabled", ""),
    ],
)
def test_normalize_level(raw, expected):
    assert R.normalize_level(raw) == expected


def test_only_manifold_connector_is_known():
    assert R.connector_known("kaidera-manifold") is True
    assert R.connector_known("direct-provider") is False
    assert R.curated_levels("kaidera-manifold", "vendor/model") == []


def test_effort_is_emitted_only_from_live_model_metadata():
    payload = {"model": "vendor/model", "messages": []}
    R.apply_reasoning(
        "kaidera-manifold",
        "vendor/model",
        "ultra",
        payload,
        available_levels=["low", "ultra", "future"],
    )
    assert payload["reasoning_effort"] == "ultra"

    no_metadata = {"model": "vendor/model", "messages": []}
    R.apply_reasoning("kaidera-manifold", "vendor/model", "high", no_metadata)
    assert "reasoning_effort" not in no_metadata


def test_future_provider_defined_effort_passes_through_when_advertised():
    assert R.resolve_level(
        "kaidera-manifold",
        "vendor/model",
        "future",
        available_levels=["low", "future"],
    ) == "future"


def test_known_effort_clamps_to_nearest_advertised_level():
    assert R.resolve_level(
        "kaidera-manifold",
        "vendor/model",
        "ultra",
        available_levels=["low", "high"],
    ) == "high"
    assert R.resolve_level(
        "kaidera-manifold",
        "vendor/model",
        "minimal",
        available_levels=["low", "high"],
    ) == "low"


def test_bare_on_prefers_medium_or_nearest_default():
    assert R.resolve_level(
        "kaidera-manifold",
        "vendor/model",
        "on",
        available_levels=["low", "medium", "high"],
    ) == "medium"
    assert R.resolve_level(
        "kaidera-manifold",
        "vendor/model",
        "on",
        available_levels=["low", "high"],
    ) == "low"


def test_off_unknown_provider_and_empty_ladder_emit_nothing():
    for provider, level, available in (
        ("kaidera-manifold", "off", ["low", "high"]),
        ("kaidera-manifold", "high", []),
        ("direct-provider", "high", ["low", "high"]),
    ):
        payload = {}
        R.apply_reasoning(
            provider,
            "vendor/model",
            level,
            payload,
            available_levels=available,
        )
        assert payload == {}


def test_extract_reasoning_text_from_manifold_response():
    content = {"choices": [{"message": {"reasoning_content": "step-by-step"}}]}
    alternate = {"choices": [{"message": {"reasoning": "thinking"}}]}
    assert R.extract_reasoning_text("kaidera-manifold", content) == "step-by-step"
    assert R.extract_reasoning_text("kaidera-manifold", alternate) == "thinking"
    assert R.extract_reasoning_text("direct-provider", content) == ""
    assert R.extract_reasoning_text("kaidera-manifold", {}) == ""
