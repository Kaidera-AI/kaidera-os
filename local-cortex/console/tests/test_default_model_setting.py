"""The `model_default` System setting drives the kaidera out-of-the-box model.

`harness.harness_default_model("kaidera")` is the single chokepoint every default-fill
path calls for a NEW kaidera agent that declares no model. It now reads the operator's
`model_default` setting LIVE (the per-deployment out-of-the-box default) and falls back
to the built-in `OWN_HARNESS_DEFAULT_MODEL` when unset/blank. A per-agent pick always
wins upstream — this only fills an unconfigured agent.
"""

from __future__ import annotations

from app import harness as h


def test_unset_falls_back_to_builtin(monkeypatch):
    monkeypatch.setattr("app.settings.load", lambda: {})
    assert h.harness_default_model("kaidera") == h.OWN_HARNESS_DEFAULT_MODEL


def test_setting_wins_when_present(monkeypatch):
    monkeypatch.setattr("app.settings.load", lambda: {"model_default": "ollama-cloud/foo-9"})
    assert h.harness_default_model("kaidera") == "ollama-cloud/foo-9"


def test_blank_setting_falls_back(monkeypatch):
    monkeypatch.setattr("app.settings.load", lambda: {"model_default": "   "})
    assert h.harness_default_model("kaidera") == h.OWN_HARNESS_DEFAULT_MODEL


def test_setting_read_failure_degrades_to_builtin(monkeypatch):
    def _boom():
        raise RuntimeError("app-DB down")

    monkeypatch.setattr("app.settings.load", _boom)
    assert h.harness_default_model("kaidera") == h.OWN_HARNESS_DEFAULT_MODEL


def test_model_default_does_not_affect_other_harnesses(monkeypatch):
    # The setting is the kaidera out-of-the-box default; a fixed lane (claude-code) keeps
    # its own first-in-list default regardless.
    monkeypatch.setattr("app.settings.load", lambda: {"model_default": "ollama-cloud/foo-9"})
    cc = h.harness_default_model("claude-code")
    assert cc != "ollama-cloud/foo-9"
