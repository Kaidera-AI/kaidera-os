"""Ed25519 (asymmetric) signing + grace + not-before — the Phase-1 hardening for public
distribution. The platform signs with a private key; the app verifies with the embedded
public key. HMAC stays for the interim, rejectable via KAIDERA_OS_LICENSE_REQUIRE_ED25519."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import license as lic


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub_pem = (
        priv.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub_pem


def test_ed25519_roundtrip(monkeypatch):
    priv, pub = _keypair()
    monkeypatch.setenv("KAIDERA_OS_LICENSE_VERIFY_KEY", pub)
    tok = lic.generate_license("Platform", days=365, alg="ed25519",
                               ed25519_private_key=priv, features=["harness:*"])
    claims = lic.verify_license(tok)
    assert claims and claims["customer"] == "Platform" and claims["alg"] == "ed25519"


def test_ed25519_wrong_key_rejected(monkeypatch):
    priv, _ = _keypair()
    _, other_pub = _keypair()
    monkeypatch.setenv("KAIDERA_OS_LICENSE_VERIFY_KEY", other_pub)  # not the signer's pair
    tok = lic.generate_license("Platform", days=365, alg="ed25519", ed25519_private_key=priv)
    assert lic.verify_license(tok) is None


def test_require_ed25519_rejects_hmac(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_LICENSE_REQUIRE_ED25519", "1")
    assert lic.verify_license(lic.generate_license("Acme", days=365)) is None  # hmac forbidden


def test_require_ed25519_accepts_ed25519(monkeypatch):
    priv, pub = _keypair()
    monkeypatch.setenv("KAIDERA_OS_LICENSE_VERIFY_KEY", pub)
    monkeypatch.setenv("KAIDERA_OS_LICENSE_REQUIRE_ED25519", "1")
    tok = lic.generate_license("Platform", days=365, alg="ed25519", ed25519_private_key=priv)
    assert lic.verify_license(tok) is not None


def test_grace_window_verifies_then_expires(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    priv, pub = _keypair()
    monkeypatch.setenv("KAIDERA_OS_LICENSE_VERIFY_KEY", pub)
    tok = lic.generate_license("Lapse", days=1, grace_days=7, now=1_000_000, alg="ed25519", ed25519_private_key=priv,
                               features=["harness:claude-code", "workers:9"])
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY", tok)

    at_grace = 1_000_000 + 3 * 86400          # past valid_until (day 1), within grace (day 8)
    assert lic.verify_license(tok, now=at_grace) is not None
    ent = lic.entitlements(now=at_grace)
    assert ent.valid and ent.in_grace
    assert ent.has_harness("claude-code") and ent.limit_for("workers") == 9  # still granted in grace

    past_grace = 1_000_000 + 10 * 86400        # past grace_until → expired → free tier
    assert lic.verify_license(tok, now=past_grace) is None
    ent2 = lic.entitlements(now=past_grace)
    assert not ent2.has_harness("claude-code") and ent2.limit_for("workers") == 4


def test_nbf_not_yet_valid(monkeypatch):
    tok = lic.generate_license("Future", days=30, now=1_000_000, nbf=1_000_000 + 10 * 86400)
    assert lic.verify_license(tok, now=1_000_000) is None             # before nbf
    assert lic.verify_license(tok, now=1_000_000 + 11 * 86400) is not None  # after nbf
