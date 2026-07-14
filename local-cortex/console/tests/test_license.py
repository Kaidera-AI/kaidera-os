"""Kaidera OS license gate (app/license.py): signed-token round-trip, expiry, tamper,
status, and the kaidera-os-exempt requirement check."""

from __future__ import annotations

import json

from app import license as lic


def test_generate_verify_roundtrip():
    tok = lic.generate_license("Acme Corp", days=30, now=1_000_000)
    claims = lic.verify_license(tok, now=1_000_000 + 86400)  # 1 day later
    assert claims and claims["customer"] == "Acme Corp"
    assert "console" in claims["features"]


def test_expired_license_rejected():
    tok = lic.generate_license("Acme", days=1, now=1_000_000)
    assert lic.verify_license(tok, now=1_000_000 + 2 * 86400) is None  # 2 days later → expired


def test_tampered_token_rejected():
    tok = lic.generate_license("Acme", days=30, now=1_000_000)
    payload, sig = tok.split(".", 1)
    # Flip the signature → must fail the constant-time compare.
    assert lic.verify_license(payload + "." + ("A" * len(sig)), now=1_000_000) is None
    # Garbage → None, never raises.
    assert lic.verify_license("not-a-token") is None
    assert lic.verify_license("") is None


def test_wrong_key_rejected(monkeypatch):
    tok = lic.generate_license("Acme", days=30, now=1_000_000)
    monkeypatch.setenv("KAIDERA_OS_LICENSE_VERIFY_KEY", "a-different-signing-secret")
    assert lic.verify_license(tok, now=1_000_000) is None  # signed with the other key


def test_json_verify_keys_support_key_rotation(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_OS_LICENSE_VERIFY_KEYS", json.dumps({
        "default": "old-secret",
        "next": "new-secret",
    }))

    tok = lic.generate_license("Rotated", days=30, now=1_000_000, kid="next")
    claims = lic.verify_license(tok, now=1_000_000)

    assert claims and claims["customer"] == "Rotated"
    assert claims["kid"] == "next"


def test_platform_issuer_accepts_rotated_key_ids(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv(
        "KAIDERA_OS_LICENSE_VERIFY_KEYS",
        json.dumps({
            "kaidera-os-lic-v1": "old-secret",
            "kaidera-os-lic-v2": "new-secret",
        }),
    )

    old_grant = lic.generate_license(
        "Existing Customer",
        days=30,
        now=1_000_000,
        kid="kaidera-os-lic-v1",
        issuer="kaidera-license-authority",
    )
    new_grant = lic.generate_license(
        "New Customer",
        days=30,
        now=1_000_000,
        kid="kaidera-os-lic-v2",
        issuer="kaidera-license-authority",
    )

    assert lic.verify_license(old_grant, now=1_000_000) is not None
    assert lic.verify_license(new_grant, now=1_000_000) is not None


def test_unknown_named_issuer_and_unknown_explicit_kid_fail_closed(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv(
        "KAIDERA_OS_LICENSE_VERIFY_KEYS",
        json.dumps({"default": "default-secret", "known": "known-secret"}),
    )

    unknown_issuer = lic.generate_license(
        "Wrong Issuer",
        days=30,
        now=1_000_000,
        kid="known",
        issuer="attacker-license-authority",
    )
    unknown_kid = lic.generate_license(
        "Wrong Kid",
        days=30,
        now=1_000_000,
        key=b"attacker-secret",
        kid="missing",
        issuer="kaidera-license-authority",
    )
    empty_issuer = lic.generate_license(
        "Empty Issuer",
        days=30,
        now=1_000_000,
        kid="known",
        issuer="",
    )
    empty_kid = lic.generate_license(
        "Empty Kid",
        days=30,
        now=1_000_000,
        key=b"default-secret",
        kid="",
        issuer="kaidera-license-authority",
    )

    assert lic.verify_license(unknown_issuer, now=1_000_000) is None
    assert lic.verify_license(unknown_kid, now=1_000_000) is None
    assert lic.verify_license(empty_issuer, now=1_000_000) is None
    assert lic.verify_license(empty_kid, now=1_000_000) is None


def test_malformed_verify_keys_fall_back_without_crashing(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_OS_LICENSE_VERIFY_KEYS", '["not", "a", "mapping"]')

    tok = lic.generate_license("Fallback", days=30, now=1_000_000)
    claims = lic.verify_license(tok, now=1_000_000)

    assert claims and claims["customer"] == "Fallback"


def test_license_required_is_kaidera_os_exempt(monkeypatch):
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "kaidera-os")
    assert lic.license_required() is False
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    assert lic.license_required() is True
    monkeypatch.delenv("KAIDERA_DEPLOY_MODE", raising=False)
    assert lic.license_required() is True  # unset → required (fail-closed, mirrors auth)


def test_explicit_dev_edition_is_license_exempt_even_when_selfcontained(monkeypatch):
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    assert lic.license_required() is False


def test_license_status_shapes(monkeypatch):
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    st = lic.license_status()
    assert st["required"] is True and st["valid"] is False and "no KAIDERA_OS_LICENSE_KEY" in st["reason"]

    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY", lic.generate_license("Beta Inc", days=365))
    st = lic.license_status()
    assert st["valid"] is True and st["customer"] == "Beta Inc"


def test_enforce_at_startup_is_soft_for_missing_hosted_license(monkeypatch):
    class _Log:
        warnings: list[str] = []

        def warning(self, msg, *args):
            self.warnings.append(msg % args if args else msg)

        def info(self, *_args, **_kwargs):
            raise AssertionError("missing hosted license should not log as licensed")

    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)

    log = _Log()
    status = lic.enforce_at_startup(log)

    assert status["required"] is True
    assert status["valid"] is False
    assert "no KAIDERA_OS_LICENSE_KEY" in status["reason"]
    assert log.warnings
    assert "UNLICENSED" in log.warnings[0]


def test_hard_gate_default_off_allows_missing_hosted_license(monkeypatch):
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    monkeypatch.delenv("KAIDERA_OS_LICENSE_HARD_GATE", raising=False)

    gate = lic.license_gate_status(surface="app")

    assert gate["enabled"] is False
    assert gate["allowed"] is True
    assert gate["state"] == "soft"


def test_hard_gate_enabled_keeps_free_tier_usable_without_token(monkeypatch):
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_HARD_GATE", "1")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)

    gate = lic.license_gate_status(surface="app")

    assert gate["enabled"] is True
    assert gate["allowed"] is True
    assert gate["state"] == "free_tier"


def test_hard_gate_blocks_expired_grant_but_keeps_license_surface_open(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_HARD_GATE", "1")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY", lic.generate_license("Expired", days=1, now=1_000_000))

    blocked = lic.license_gate_status(now=1_000_000 + 3 * 86400, surface="app")
    license_page = lic.license_gate_status(now=1_000_000 + 3 * 86400, surface="license")

    assert blocked["allowed"] is False
    assert blocked["state"] == "expired"
    assert license_page["allowed"] is True
    assert license_page["state"] == "expired"


def test_hard_gate_allows_valid_grace_window(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_HARD_GATE", "1")
    monkeypatch.setenv(
        "KAIDERA_OS_LICENSE_KEY",
        lic.generate_license("Grace", days=1, grace_days=7, now=1_000_000),
    )

    gate = lic.license_gate_status(now=1_000_000 + 3 * 86400, surface="app")

    assert gate["allowed"] is True
    assert gate["state"] == "grace"
    assert gate["in_grace"] is True


def test_revoked_grant_falls_back_to_free_tier_and_hard_gate_blocks(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_HARD_GATE", "1")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_REVOKED", "1")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY", lic.generate_license("Revoked", days=365))

    ent = lic.entitlements()
    gate = lic.license_gate_status(surface="app")

    assert ent.reason == "license revoked"
    assert not ent.has_harness("claude-code")
    assert gate["allowed"] is False
    assert gate["state"] == "revoked"
