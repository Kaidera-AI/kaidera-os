"""Edition gate (app/edition.py) + the entitlements read-API (app/license.py).

These lock the keystone the whole licensing epic hangs off: DEV is unrestricted,
PUBLIC defaults to the free tier (kaidera + 1/1/4), and a signed token's `features`
unlock harnesses + capacity — but NEVER providers (edition-only)."""

from __future__ import annotations

import math

from app import edition
from app import license as lic


# --- edition resolution --------------------------------------------------------------

def test_edition_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_EDITION", raising=False)
    monkeypatch.delenv("KAIDERA_DEPLOY_MODE", raising=False)
    assert edition.edition() == "dev"
    assert edition.is_dev() and not edition.is_public()


def test_explicit_env_selects_edition(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    assert edition.is_public() and not edition.is_dev()
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    assert edition.is_dev()


def test_selfcontained_deploy_falls_through_to_public(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_EDITION", raising=False)
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    assert edition.is_public()  # the redistributable is PUBLIC with zero extra wiring


def test_edition_env_overrides_selfcontained(monkeypatch):
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "selfcontained")
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    assert edition.is_dev()  # explicit signal wins over the deploy-mode fallback


# --- entitlements: DEV is unrestricted ----------------------------------------------

def test_dev_edition_is_all_permissive(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    ent = lic.entitlements()
    assert ent.has_harness("claude-code") and ent.has_harness("anything")
    assert ent.limit_for("projects") == math.inf
    assert ent.limit_for("workers") == math.inf
    assert ent.limit_for("users") == math.inf
    assert ent.has_advanced("manifold_access")


# --- entitlements: PUBLIC free tier --------------------------------------------------

def test_public_free_tier_without_license(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    ent = lic.entitlements()
    assert ent.has_harness("kaidera")            # kaidera is always free
    assert not ent.has_harness("claude-code")    # everything else is locked
    assert ent.limit_for("projects") == 1
    assert ent.limit_for("teams") == 1
    assert ent.limit_for("workers") == 4
    assert ent.limit_for("users") == 1
    assert not ent.has_advanced("manifold_access")
    assert ent.valid is False


def test_public_valid_token_unlocks_harness_and_capacity(monkeypatch, ed25519_public_license):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    ed25519_public_license(  # public verifies only Ed25519 platform grants
        "DXB", days=365,
        features=[
            "harness:claude-code", "harness:codex", "projects:5", "workers:10",
            "kaidera_os_max_users:7", "manifold_access",
        ],
    )
    ent = lic.entitlements()
    assert ent.valid and ent.customer == "DXB"
    assert ent.has_harness("claude-code") and ent.has_harness("codex")
    assert ent.has_harness("kaidera")            # still free on top of grants
    assert not ent.has_harness("pi")             # not granted
    assert ent.limit_for("projects") == 5
    assert ent.limit_for("workers") == 10
    assert ent.limit_for("users") == 7
    assert ent.limit_for("teams") == 1           # un-granted falls to the baseline
    assert ent.has_advanced("manifold_access")


def test_wildcard_harness_unlocks_all(monkeypatch, ed25519_public_license):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    ed25519_public_license("Ent", days=365, features=["harness:*"])
    ent = lic.entitlements()
    assert ent.has_harness("claude-code") and ent.has_harness("pi")


def test_capacity_never_drops_below_free_tier(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY",
                       lic.generate_license("Tiny", days=365, features=["workers:2"]))
    # max(free=4, granted=2) → 4; a license can't shrink the baseline.
    assert lic.entitlements().limit_for("workers") == 4


def test_unlimited_capacity_token(monkeypatch, ed25519_public_license):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    ed25519_public_license("Big", days=365, features=["projects:unlimited"])
    assert lic.entitlements().limit_for("projects") == math.inf


def test_provider_feature_is_ignored(monkeypatch):
    # Providers are EDITION-only. A provider:* feature must NOT unlock anything here.
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY",
                       lic.generate_license("Sneaky", days=365, features=["provider:anthropic"]))
    ent = lic.entitlements()
    assert ent.harnesses == frozenset({"kaidera"})   # unchanged from free tier
    assert ent.limit_for("projects") == 1
    assert not ent.has_advanced("anthropic")


def test_expired_token_falls_back_to_free_tier(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    tok = lic.generate_license("Lapsed", days=1, now=1_000_000,
                               features=["harness:claude-code", "projects:9"])
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY", tok)
    ent = lic.entitlements(now=1_000_000 + 5 * 86400)  # well past expiry
    assert not ent.has_harness("claude-code")
    assert ent.limit_for("projects") == 1
    assert ent.limit_for("users") == 1
    assert not ent.has_advanced("manifold_access")
    assert "expired" in ent.reason
