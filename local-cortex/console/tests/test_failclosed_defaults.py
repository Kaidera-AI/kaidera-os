"""Fail-CLOSED security defaults (v0.1.143) — the two money-paths Kai's deep-QA flagged:

1. auth_enabled(): an UNSET deploy signal must mean auth ON (a hosted console that forgot
   the env can't silently run open). Only explicit dev/local modes opt out; product/hosted
   modes stay auth ON unless KAIDERA_AUTH_ENABLED=0 is set deliberately.
2. the AUTONOMOUS propose-mode gate: an UNREADABLE propose state (app-DB down) must GATE
   (hold for approval), never auto-spawn unapproved work. The interactive read stays fail-safe-OFF.
"""

from __future__ import annotations

from app import appdb, auth, settings


def test_auth_enabled_fails_closed_when_nothing_set(monkeypatch):
    monkeypatch.delenv("KAIDERA_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("KAIDERA_DEPLOY_MODE", raising=False)
    assert auth.auth_enabled() is True  # no signal → untrusted → auth ON


def test_auth_off_only_on_explicit_local_mode(monkeypatch):
    monkeypatch.delenv("KAIDERA_AUTH_ENABLED", raising=False)
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "local")
    assert auth.auth_enabled() is False
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "kaidera-os")
    assert auth.auth_enabled() is True
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    assert auth.auth_enabled() is True


def test_explicit_auth_enabled_always_wins(monkeypatch):
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")  # would be ON…
    monkeypatch.setenv("KAIDERA_AUTH_ENABLED", "0")             # …but explicit OFF wins
    assert auth.auth_enabled() is False


class _FakeDB:
    def __init__(self, v):
        self.v = v

    def get_project_propose_mode(self, key):
        return self.v


def test_propose_gate_fails_closed_on_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "_db", _FakeDB(appdb.UNAVAILABLE))
    assert settings.is_propose_mode_gate("p") is True   # can't confirm → GATE
    assert settings.is_propose_mode("p") is False        # interactive stays fail-safe OFF


def test_propose_gate_passes_explicit_states(monkeypatch):
    monkeypatch.setattr(settings, "_db", _FakeDB(True))
    assert settings.is_propose_mode_gate("p") is True    # explicit ON → gate
    monkeypatch.setattr(settings, "_db", _FakeDB(False))
    assert settings.is_propose_mode_gate("p") is False   # explicit OFF → auto-spawn
