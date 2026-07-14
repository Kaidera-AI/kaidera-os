"""Kaidera OS signed feature-grant verification.

The AGPL community runtime remains usable at its default floor without a grant. A
platform-signed grant can raise capacity or enable named advanced features, but an
absent, expired, revoked, or unverifiable grant falls back to the floor. Separately
operated platform services enforce their own entitlements server-side.

TOKEN FORMAT:  <b64url(claims_json)>.<b64url(sig)>
  claims = {"alg": "hmac"|"ed25519", "customer": str, "features": [str],
            "iss": <epoch>|"kaidera-license-authority",
            "valid_until": <epoch>, "grace_until": <epoch>, "nbf"?, "org_id"?, ...}

TWO SIGNING ALGORITHMS, chosen by the token's `alg` claim:
  * "hmac"    — legacy development/offline HMAC-SHA256 with a symmetric key. Public builds
                reject this form because verifier material cannot prove platform issuance.
  * "ed25519" — ASYMMETRIC: the Kaidera AI platform signs with a private key it alone holds;
                the app verifies with the embedded PUBLIC key (KAIDERA_OS_LICENSE_VERIFY_KEY
                in the Ed25519 build = the public key). This is the REQUIRED hardening
                for public distribution.
Set KAIDERA_OS_LICENSE_REQUIRE_ED25519=1 in the public build to REJECT forgeable HMAC tokens.

GRACE: a token verifies through `grace_until` (>= `valid_until`); entitlements() marks the
window between them `in_grace`. A small clock-skew leeway tolerates a drifting VM clock.

Development mode is exempt: a dev/local deploy never requires a license.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

# The shipped verification key. Kaidera AI SHOULD override this at build/sign time via
# KAIDERA_OS_LICENSE_VERIFY_KEY so the public default can't sign valid licenses. (It is still
# symmetric — see the module docstring's ceiling.)
_DEFAULT_VERIFY_KEY = "kaidera-os-default-unsigned-do-not-ship-without-override"

# Platform grants use the canonical named authority.
PLATFORM_LICENSE_ISSUERS = frozenset({"kaidera-license-authority"})


#: Clock-skew leeway (seconds) on nbf / valid_until / grace_until — a drifting VM clock
#: shouldn't bypass NOR falsely expire a license. Trust SERVER time on online refresh.
_CLOCK_SKEW = 300


def _verify_keys_raw() -> dict[str, str]:
    """Return a mapping of kid -> public key material."""
    def _clean(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in value.items():
            key_id = str(k or "").strip()
            material = str(v or "").strip()
            if key_id and material:
                out[key_id] = material
        return out

    raw = os.environ.get("KAIDERA_OS_LICENSE_VERIFY_KEYS")
    if raw:
        try:
            parsed = _clean(json.loads(raw))
            if parsed:
                return parsed
        except Exception:
            pass
    single = os.environ.get("KAIDERA_OS_LICENSE_VERIFY_KEY")
    if single:
        s = single.strip()
        if s.startswith("{"):
            try:
                parsed = _clean(json.loads(s))
                if parsed:
                    return parsed
            except Exception:
                pass
        return {"default": s}
    return {"default": _DEFAULT_VERIFY_KEY}


def _key(kid: str = "default") -> bytes:
    keys = _verify_keys_raw()
    return keys.get(kid, keys.get("default", _DEFAULT_VERIFY_KEY)).encode("utf-8")


def _require_ed25519() -> bool:
    """PUBLIC distribution sets KAIDERA_OS_LICENSE_REQUIRE_ED25519=1 so HMAC tokens
    (forgeable when verifier material is distributed) are rejected — only platform-signed
    Ed25519 grants verify."""
    try:
        from app import edition
        if edition.is_public():
            return True
    except Exception:
        pass
    return (os.environ.get("KAIDERA_OS_LICENSE_REQUIRE_ED25519") or "").strip().lower() in {"1", "true", "yes"}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _load_ed25519_public_key(material: str):
    """Parse the embedded verify key as an Ed25519 public key — PEM (SubjectPublicKeyInfo)
    or a raw 32-byte key in base64/base64url. Raises on bad material."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    m = (material or "").strip()
    if "BEGIN PUBLIC KEY" in m or "BEGIN PUBLIC" in m:
        return serialization.load_pem_public_key(m.encode("utf-8"))
    raw = _unb64url(m) if ("-" in m or "_" in m) else base64.b64decode(m + "=" * (-len(m) % 4))
    return Ed25519PublicKey.from_public_bytes(raw)


def _ed25519_verify(payload: bytes, sig: bytes, public_material: str) -> bool:
    """True iff `sig` is a valid Ed25519 signature over `payload` under the public key.
    Never raises (bad key / bad sig → False)."""
    try:
        _load_ed25519_public_key(public_material).verify(sig, payload)
        return True
    except Exception:
        return False


def generate_license(customer: str, *, days: int = 365, features: Optional[list[str]] = None,
                     now: Optional[int] = None, key: Optional[bytes] = None,
                     alg: str = "hmac", ed25519_private_key: Any = None,
                     grace_days: int = 0, nbf: Optional[int] = None,
                     org_id: Optional[str] = None, license_id: Optional[str] = None,
                     install_id: Optional[str] = None, channel: Optional[str] = None,
                     kid: Optional[str] = None,
                     issuer: Optional[str] = None,
                     latest_release: Optional[dict[str, Any]] = None,
                     wallet: Optional[dict[str, Any]] = None,
                     addons: Optional[list[dict[str, Any]]] = None) -> str:
    """Sign a license token. `alg='hmac'` (interim, `scripts/kaidera-os-license-gen`) signs
    with the symmetric key; `alg='ed25519'` (platform) signs with `ed25519_private_key`
    (a cryptography Ed25519PrivateKey). `valid_until`=iss+days; the token verifies through
    `grace_until`=valid_until+grace_days. Returns the `payload.sig` string."""
    issued = int(now if now is not None else time.time())
    valid_until = issued + days * 86400
    claims: dict[str, Any] = {
        "v": 2, "alg": alg, "customer": customer,
        "features": sorted(features or ["console", "cortex"]),
        "iss": issued if issuer is None else issuer,
        "exp": valid_until, "valid_until": valid_until,
        "grace_until": valid_until + max(0, grace_days) * 86400,
    }
    for k, val in (("nbf", nbf), ("org_id", org_id), ("license_id", license_id),
                   ("install_id", install_id), ("channel", channel),
                   ("kid", kid),
                   ("latest_release", latest_release), ("wallet", wallet),
                   ("addons", addons)):
        if val is not None:
            claims[k] = val
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if alg == "ed25519":
        if ed25519_private_key is None:
            raise ValueError("ed25519 signing requires ed25519_private_key")
        sig = ed25519_private_key.sign(payload)
    else:
        sig = hmac.new(key or _key(str(claims.get("kid") or "default")), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def verify_license(token: str, *, now: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Return the claims dict if `token` is well-formed, correctly signed (per its `alg`),
    after `nbf`, and within `grace_until` (with skew leeway); else None. A legacy token with
    no `alg` is treated as HMAC; no `valid_until` falls back to `exp`. Never raises."""
    try:
        now_ts = int(now if now is not None else time.time())
        payload_b64, sig_b64 = (token or "").strip().split(".", 1)
        payload = _unb64url(payload_b64)
        sig = _unb64url(sig_b64)
        peek = json.loads(payload.decode("utf-8"))
        if not isinstance(peek, dict):
            return None
        claims = peek
        alg = str(claims.get("alg") or "hmac")

        explicit_kid = claims.get("kid")
        kid = "default" if explicit_kid is None else str(explicit_kid)
        keys = _verify_keys_raw()
        if explicit_kid is not None:
            key_material = keys.get(kid)
            if key_material is None:
                return None
        else:
            key_material = keys.get("default", _DEFAULT_VERIFY_KEY)

        if alg == "ed25519":
            if not _ed25519_verify(payload, sig, key_material):
                return None
        elif alg == "hmac":
            if _require_ed25519():
                return None  # PUBLIC build rejects forgeable HMAC tokens
            if not hmac.compare_digest(hmac.new(key_material.encode("utf-8"), payload, hashlib.sha256).digest(), sig):
                return None
        else:
            return None  # unknown algorithm

        issuer = claims.get("iss")
        if isinstance(issuer, str):
            if issuer not in PLATFORM_LICENSE_ISSUERS:
                return None
        elif not isinstance(issuer, (int, float)) or isinstance(issuer, bool):
            return None

        nbf = int(claims.get("nbf") or 0)
        if nbf and now_ts < nbf - _CLOCK_SKEW:
            return None  # not yet valid
        valid_until = int(claims.get("valid_until") or claims.get("exp") or 0)
        hard_expiry = max(valid_until, int(claims.get("grace_until") or 0))

        try:
            from app import settings as _settings
            raw = _settings._read_raw() or {}
            high_water = int(raw.get("license_high_water_mark") or 0)
            if now_ts < high_water - _CLOCK_SKEW:
                return None  # clock rolled back!

            server_time = claims.get("server_time")
            if server_time and int(server_time) > high_water:
                from app.appdb import AppDB
                AppDB().upsert_app_settings({"license_high_water_mark": int(server_time)})
        except Exception:
            pass

        if hard_expiry and now_ts > hard_expiry + _CLOCK_SKEW:
            return None  # past grace → expired
        return claims
    except Exception:
        return None


def _license_token() -> str:
    """The active license token. KAIDERA_OS_LICENSE_KEY env wins (build/ops override);
    else the app-DB ``license_key`` setting (the paste-in Settings -> License panel),
    so a fresh box can be licensed with no env edit + no restart. Never raises."""
    tok = (os.environ.get("KAIDERA_OS_LICENSE_KEY") or "").strip()
    if tok:
        return tok
    try:
        from app import settings as _settings  # lazy — avoid an import cycle at load
        # license_key lives OUTSIDE the System schema (like provider keys), so read the
        # raw store — load()/normalize() would drop it. Written via upsert_app_settings.
        val = (_settings._read_raw() or {}).get("license_key")
        if isinstance(val, str):
            return val.strip()
    except Exception:
        pass
    return ""


# --- Entitlements: the SINGLE read-API every gate consults ---------------------------
#
# The `features` claim is a namespaced vocabulary:
#   harness:<id>  harness:*        -> unlock a harness ("kaidera" is ALWAYS on, free)
#   projects:N    projects:unlimited
#   teams:N       teams:unlimited
#   workers:M     workers:unlimited
#   users:N       users:unlimited
#   kaidera_os_max_users:N           -> platform package cap aliases
#   manifold_access                -> advanced feature atom
# Providers are still EDITION-limited (PUBLIC exposes only Manifold), but advanced atoms
# gate privileged provider use inside that edition. Every enforcement seam reads
# entitlements()/has_harness/limit_for/has_advanced, so the HMAC->Ed25519 swap and
# offline->online transition live in ONE place.

#: PUBLIC edition free tier when there is no (valid) license.
PUBLIC_FREE_HARNESSES = frozenset({"kaidera"})
PUBLIC_FREE_LIMITS: dict[str, float] = {"projects": 1, "teams": 1, "workers": 4, "users": 1}
_CAPACITY_KINDS = ("projects", "teams", "workers", "users")
PUBLIC_FREE_ADVANCED = frozenset()
HARD_GATE_ENV = "KAIDERA_OS_LICENSE_HARD_GATE"
HARD_GATE_ALLOWED_SURFACES = frozenset({"license", "backup", "export", "support", "auth", "health"})


@dataclass(frozen=True)
class Entitlements:
    valid: bool
    reason: str
    harnesses: frozenset[str]
    limits: dict[str, float]      # ints, or math.inf for unlimited / DEV
    advanced: frozenset[str]
    valid_until: Optional[int]
    grace_until: Optional[int]
    in_grace: bool
    customer: Optional[str]
    org_id: Optional[str]
    #: Prepaid billing balance from the platform grant, e.g.
    #: {"balance": 42.50, "currency": "USD", "as_of": <epoch>}. None when unknown
    #: (free tier / DEV / a token without billing). Displayed by the Billing tab; the
    #: cust-portal owns top-ups + add-on purchases.
    wallet: Optional[dict[str, Any]] = None
    #: Active add-on SKUs from the grant, e.g. [{"sku": "addon:worker", "qty": 2}].
    addons: tuple[dict[str, Any], ...] = ()

    def has_harness(self, harness_id: str) -> bool:
        h = str(harness_id or "").strip().lower()
        return h == "kaidera" or "*" in self.harnesses or h in self.harnesses

    def limit_for(self, kind: str) -> float:
        return self.limits.get(kind, 0)

    def has_advanced(self, atom: str) -> bool:
        name = _feature_key(atom)
        return "*" in self.advanced or name in self.advanced


def _safe_int(s: str, default: float) -> float:
    try:
        return int(s)
    except Exception:
        return default


def _feature_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _feature_limit(raw: Any) -> float | None:
    val = str(raw or "").strip().lower()
    if not val:
        return None
    if val in {"unlimited", "inf", "*"}:
        return math.inf
    parsed = _safe_int(val, -1)
    return parsed if parsed >= 0 else None


_CAPACITY_ALIASES: dict[str, tuple[str, ...]] = {
    "projects": ("projects", "project", "kaidera_os_max_projects"),
    "teams": ("teams", "team", "kaidera_os_max_teams"),
    "workers": ("workers", "worker", "ai_workers", "kaidera_os_max_workers"),
    "users": ("users", "user", "kaidera_os_max_users"),
}

_ADVANCED_ALIASES: dict[str, str] = {
    "manifold_access": "manifold_access",
    "kaidera_os_manifold_access": "manifold_access",
}


def _parse_features(features: list[Any]) -> tuple[set[str], dict[str, float], set[str]]:
    """Fold a token's `features` list onto the free-tier baseline. A license never
    drops capacity BELOW the free tier (max), and `provider:*` is ignored by design."""
    harnesses: set[str] = set(PUBLIC_FREE_HARNESSES)
    limits: dict[str, float] = dict(PUBLIC_FREE_LIMITS)
    advanced: set[str] = set()
    for raw in features or []:
        feature_value: Any = None
        feature_is_advanced = False
        if isinstance(raw, dict):
            if raw.get("enabled") is False:
                continue
            f = str(
                raw.get("feature")
                or raw.get("key")
                or raw.get("code")
                or raw.get("name")
                or raw.get("id")
                or ""
            ).strip()
            feature_value = raw.get("value", raw.get("limit", raw.get("quantity")))
            feature_is_advanced = bool(raw.get("is_advanced"))
        else:
            f = str(raw).strip()
        if not f:
            continue
        if f.startswith("harness:"):
            harnesses.add(f.split(":", 1)[1].strip().lower())
            continue
        if f.startswith("advanced:") or f.startswith("is_advanced:"):
            atom = _ADVANCED_ALIASES.get(_feature_key(f.split(":", 1)[1]), _feature_key(f.split(":", 1)[1]))
            if atom:
                advanced.add(atom)
            continue

        key, sep, val = f.replace("=", ":", 1).partition(":")
        norm_key = _feature_key(key)
        if not sep and feature_value is not None:
            val = str(feature_value)
        if feature_is_advanced or (not sep and norm_key in _ADVANCED_ALIASES):
            atom = _ADVANCED_ALIASES.get(norm_key, norm_key)
            if atom:
                advanced.add(atom)
            continue
        for kind, aliases in _CAPACITY_ALIASES.items():
            if norm_key in aliases:
                parsed = _feature_limit(val)
                if parsed is not None:
                    limits[kind] = math.inf if parsed == math.inf else max(limits[kind], parsed)
                break
    return harnesses, limits, advanced


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def hard_gate_enabled() -> bool:
    """Default-off paid-license hard gate.

    PUBLIC/free-tier installs must remain usable with no license. This flag is only
    the seam for post-platform paid-license expiry/revocation enforcement.
    """
    return _truthy_env(HARD_GATE_ENV)


def _license_revoked() -> bool:
    """True when the online platform has marked the current local grant revoked."""
    if _truthy_env("KAIDERA_OS_LICENSE_REVOKED"):
        return True
    try:
        from app import settings as _settings
        raw = _settings._read_raw() or {}
        value = raw.get("license_revoked")
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def entitlements(now: Optional[int] = None) -> Entitlements:
    """The resolved entitlements for THIS edition. The one place that reads the token.

    DEV edition -> all-permissive (no token needed). PUBLIC edition -> parse the signed
    token's `features`; no/invalid token -> the free tier (kaidera + 1 project / 1 team /
    4 workers / 1 user, no advanced atoms). Never raises."""
    try:
        from app import edition
        if edition.is_dev():
            return Entitlements(
                valid=True, reason="dev edition", harnesses=frozenset({"*"}),
                limits={k: math.inf for k in _CAPACITY_KINDS},
                advanced=frozenset({"*"}),
                valid_until=None, grace_until=None, in_grace=False,
                customer=None, org_id=None,
            )
    except Exception:
        pass

    free = Entitlements(
        valid=False, reason="free tier (no license)",
        harnesses=PUBLIC_FREE_HARNESSES, limits=dict(PUBLIC_FREE_LIMITS),
        advanced=PUBLIC_FREE_ADVANCED,
        valid_until=None, grace_until=None, in_grace=False, customer=None, org_id=None,
    )
    token = _license_token()
    if not token:
        return free
    if _license_revoked():
        return Entitlements(**{**free.__dict__, "reason": "license revoked"})
    claims = verify_license(token, now=now)
    if not claims:
        return Entitlements(**{**free.__dict__, "reason": "license invalid or expired"})

    harnesses, limits, advanced = _parse_features(claims.get("features") or [])
    valid_until = claims.get("valid_until") or claims.get("exp")
    grace_until = claims.get("grace_until")
    now_ts = int(now if now is not None else time.time())
    in_grace = bool(valid_until and grace_until and int(valid_until) < now_ts <= int(grace_until))
    wallet = claims.get("wallet") if isinstance(claims.get("wallet"), dict) else None
    addons = tuple(a for a in (claims.get("addons") or []) if isinstance(a, dict))
    return Entitlements(
        valid=True, reason="in grace (renew soon)" if in_grace else "ok",
        harnesses=frozenset(harnesses), limits=limits,
        advanced=frozenset(advanced),
        valid_until=int(valid_until) if valid_until else None,
        grace_until=int(grace_until) if grace_until else None,
        in_grace=in_grace,
        customer=claims.get("customer"), org_id=claims.get("org_id"),
        wallet=wallet, addons=addons,
    )


def license_required() -> bool:
    """Is a license required for THIS deploy? No for dev/local; yes for hosted/enterprise."""
    # Explicit edition wins over self-contained deploy mode: the local Mac dogfood runs
    # host-isolated (`selfcontained`) while intentionally pinning `KAIDERA_OS_EDITION=dev`.
    if (os.environ.get("KAIDERA_OS_EDITION") or "").strip().lower() == "dev":
        return False
    try:
        from app import deploy_mode
        if deploy_mode.is_selfcontained():
            return True
    except Exception:
        pass
    mode = os.environ.get("KAIDERA_DEPLOY_MODE", "").strip().lower()
    legacy_dev = "local" + "dev"
    return mode not in {"dev", "test", "local", legacy_dev, "kaidera-os"}


def license_status(now: Optional[int] = None) -> dict[str, Any]:
    """The current license posture for the UI/startup/beacon: {required, valid, customer,
    expires, reason}. NEVER raises."""
    required = license_required()
    token = _license_token()
    if not token:
        return {"required": required, "valid": False, "customer": None, "expires": None,
                "reason": "no KAIDERA_OS_LICENSE_KEY set"}
    if _license_revoked():
        return {"required": required, "valid": False, "customer": None, "expires": None,
                "reason": "license revoked"}
    claims = verify_license(token, now=now)
    if not claims:
        return {"required": required, "valid": False, "customer": None, "expires": None,
                "reason": "license invalid or expired"}
    return {"required": required, "valid": True, "customer": claims.get("customer"),
            "expires": claims.get("exp"), "features": claims.get("features", []), "reason": "ok"}


def license_gate_status(now: Optional[int] = None, *, surface: str = "app") -> dict[str, Any]:
    """Return the default-off hard-gate decision for one product surface.

    This does not enforce by itself; routes can call it when the platform service is
    live and the operator explicitly sets ``KAIDERA_OS_LICENSE_HARD_GATE=1``. No-token
    PUBLIC installs continue as the free tier. A present-but-expired or revoked grant
    is what can block product surfaces, while License/Backup/Export/Support remain
    reachable so operators are never trapped away from activation or their data.
    """
    required = license_required()
    enabled = hard_gate_enabled()
    token_present = bool(_license_token())
    revoked = _license_revoked()
    ent = entitlements(now=now)
    surface_key = (surface or "app").strip().lower()
    exception_surface = surface_key in HARD_GATE_ALLOWED_SURFACES

    state = "not_required"
    allowed = True
    reason = "license not required"

    if required and not enabled:
        state = "soft"
        reason = "hard gate disabled"
    elif required and not token_present:
        state = "free_tier"
        reason = "free tier"
    elif required and revoked:
        state = "revoked"
        allowed = exception_surface
        reason = "license revoked"
    elif required and ent.valid and ent.in_grace:
        state = "grace"
        reason = "license in grace period"
    elif required and ent.valid:
        state = "licensed"
        reason = "license valid"
    elif required:
        state = "expired"
        allowed = exception_surface
        reason = "license invalid or expired"

    return {
        "enabled": enabled,
        "required": required,
        "allowed": allowed,
        "surface": surface_key,
        "state": state,
        "reason": reason,
        "token_present": token_present,
        "revoked": revoked,
        "in_grace": ent.in_grace,
        "allowed_surfaces": sorted(HARD_GATE_ALLOWED_SURFACES),
    }


def enforce_at_startup(log) -> dict[str, Any]:
    """Called once at console startup. Logs the license posture and returns the status.
    SOFT gate (v1): a missing/invalid license on a hosted deploy logs a prominent warning
    (and the startup beacon reports it) but does NOT brick the service — operations should
    never go dark on a license hiccup. A HARD refuse is a config flag for later."""
    st = license_status()
    if st["required"] and not st["valid"]:
        log.warning(
            "[license] UNLICENSED signed feature state — %s. The community floor remains "
            "available; configure a license session to enable granted features.", st["reason"],
        )
    elif st["valid"]:
        log.info("[license] licensed to %s (expires %s)", st["customer"], st["expires"])
    return st
