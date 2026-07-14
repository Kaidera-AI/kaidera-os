"""Layer-2 harness gate (app/harness.py): PUBLIC offers only kaidera + license-granted
harnesses; DEV offers all. The runtime backstop lives in main._chat_routing_for."""

from __future__ import annotations

from app import harness


def test_all_harnesses_in_dev(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    assert harness.visible_harness_order() == harness.HARNESS_ORDER
    assert {o["value"] for o in harness.harness_options()} == set(harness.HARNESS_ORDER)


def test_only_kaidera_in_public_free_tier(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    assert harness.visible_harness_order() == ["kaidera"]
    assert [o["value"] for o in harness.harness_options()] == ["kaidera"]


def test_license_unlocks_named_harnesses_in_public(monkeypatch):
    from app import license as lic

    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY",
                       lic.generate_license("DXB", days=365, features=["harness:claude-code"]))
    vis = harness.visible_harness_order()
    assert "claude-code" in vis and "kaidera" in vis
    assert "codex" not in vis  # not granted
