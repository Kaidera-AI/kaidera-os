"""Default model selection for external CLI harnesses."""

from __future__ import annotations

from app import harness as h


def test_default_uses_first_external_model(monkeypatch):
    monkeypatch.setattr(
        h,
        "harness_model_options",
        lambda harness: [{"value": "current", "label": "Current"}],
    )

    assert h.harness_default_model("claude-code") == "current"


def test_codex_prefers_cli_recommended_model(monkeypatch):
    monkeypatch.setattr(
        h,
        "harness_model_options",
        lambda harness: [
            {"value": "older", "label": "Older"},
            {"value": "recommended", "label": "Recommended", "is_default": True},
        ],
    )

    assert h.harness_default_model("codex") == "recommended"


def test_unknown_harness_has_no_default(monkeypatch):
    monkeypatch.setattr(h, "harness_model_options", lambda harness: [])

    assert h.harness_default_model("unknown") is None
