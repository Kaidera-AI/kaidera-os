"""Online license transport for the Kaidera AI platform contract.

This module implements the app-side HTTP seam described in
``docs/2026-06-26-kaidera-os-platform-license-billing-api-contract.md`` plus the
2026-06-29 staging Layer C password-login contract. Legacy activation remains soft for
backward compatibility; the password-login/session path is fail-closed because the
platform is now the source of truth for grants and Manifold inference keys. The only
durable writes are through the caller-provided settings functions, so tests and API
routes can inject the app-DB port.
"""

from __future__ import annotations

import hashlib
import os
import platform
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

import httpx

from app import edition
from app import license as lic
from app import platform_config
from app.version import __version__

PLATFORM_URL_ENV = platform_config.PLATFORM_URL_ENV

LICENSE_KEY = "license_key"
LICENSE_SESSION_TOKEN_KEY = "license_session_token"
LICENSE_SESSION_EXPIRES_AT_KEY = "license_session_expires_at"
LICENSE_SESSION_SCOPES_KEY = "license_session_scopes"
LICENSE_ORG_ID_KEY = "license_org_id"
LICENSE_ID_KEY = "license_id"
INSTALL_ID_KEY = "license_install_id"
MACHINE_SALT_KEY = "license_machine_salt"
LAST_SYNC_KEY = "license_last_sync"
REVOKED_KEY = "license_revoked"
LATEST_RELEASE_KEY = "license_latest_release"
MANIFOLD_API_KEY = "kaidera_manifold_api_key"
MANIFOLD_BASE_URL_KEY = "kaidera_manifold_base_url"
MANIFOLD_PROJECT_ID_KEY = "kaidera_manifold_project_id"

PostJson = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]
GetJson = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]
SaveSettings = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class LicenseTransportResult:
    action: str
    ok: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    stored: bool = False
    grant_valid: bool = False
    install_id: Optional[str] = None
    machine_fp: Optional[str] = None
    revoked: bool = False
    latest_release: Optional[dict[str, Any]] = None
    customer: Optional[str] = None
    org_id: Optional[str] = None
    license_id: Optional[str] = None
    expires_at: Optional[str] = None
    scopes: Optional[list[str]] = None
    manifold_enabled: bool = False
    manifold_key_stored: bool = False
    manifold_project_id_stored: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def platform_url(value: Optional[str] = None) -> str:
    """Resolved platform origin, no trailing slash."""
    return platform_config.platform_url(value)


def _now() -> int:
    return int(time.time())


def _settings(settings: Optional[dict[str, Any]]) -> dict[str, Any]:
    return settings if isinstance(settings, dict) else {}


def get_or_create_install_id(settings: Optional[dict[str, Any]], save: SaveSettings) -> str:
    vals = _settings(settings)
    existing = str(vals.get(INSTALL_ID_KEY) or "").strip()
    if existing:
        return existing
    new_id = str(uuid.uuid4())
    save({INSTALL_ID_KEY: new_id})
    return new_id


def get_or_create_machine_salt(settings: Optional[dict[str, Any]], save: SaveSettings) -> str:
    vals = _settings(settings)
    existing = str(vals.get(MACHINE_SALT_KEY) or "").strip()
    if existing:
        return existing
    salt = uuid.uuid4().hex
    save({MACHINE_SALT_KEY: salt})
    return salt


def machine_fingerprint(settings: Optional[dict[str, Any]], save: SaveSettings) -> str:
    """Stable advisory fingerprint. Salt is random per install and stored locally."""
    salt = get_or_create_machine_salt(settings, save)
    material = "|".join([
        salt,
        platform.system(),
        platform.machine(),
        platform.node(),
        str(uuid.getnode()),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _default_post_json(
    url: str,
    payload: dict[str, Any],
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers or None)
        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text[:240]}
        return resp.status_code, body if isinstance(body, dict) else {"response": body}


async def _default_get_json(
    url: str,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers or None)
        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text[:240]}
        return resp.status_code, body if isinstance(body, dict) else {"response": body}


async def _call_post_json(
    post_json: PostJson,
    url: str,
    payload: dict[str, Any],
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, Any]]:
    try:
        return await post_json(url, payload, headers)
    except TypeError:
        return await post_json(url, payload)


async def _call_get_json(
    get_json: GetJson,
    url: str,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, Any]]:
    try:
        return await get_json(url, headers)
    except TypeError:
        return await get_json(url)


def _active_token(settings: Optional[dict[str, Any]]) -> str:
    return (os.environ.get("KAIDERA_OS_LICENSE_KEY") or str(_settings(settings).get(LICENSE_KEY) or "")).strip()


def _active_session_token(settings: Optional[dict[str, Any]]) -> str:
    return (
        os.environ.get("KAIDERA_OS_LICENSE_SESSION_TOKEN")
        or str(_settings(settings).get(LICENSE_SESSION_TOKEN_KEY) or "")
    ).strip()


def _store_verified_grant(grant: str, claims: dict[str, Any], save: SaveSettings,
                          *, install_id: Optional[str], extra: Optional[dict[str, Any]] = None) -> bool:
    items: dict[str, Any] = {
        LICENSE_KEY: grant,
        LAST_SYNC_KEY: _now(),
        REVOKED_KEY: False,
    }
    if install_id:
        items[INSTALL_ID_KEY] = install_id
    if extra:
        items.update(extra)
    if claims.get("latest_release") and isinstance(claims.get("latest_release"), dict):
        items[LATEST_RELEASE_KEY] = claims["latest_release"]
    return bool(save(items))


def _err(body: dict[str, Any]) -> str:
    return str(body.get("error") or body.get("detail") or body)


def _license_headers(token: str) -> dict[str, str]:
    """The narrow license-session transport required by the platform API."""
    return {"X-Kaidera-OS-License-Token": token}


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _scopes(body: dict[str, Any]) -> list[str]:
    raw = body.get("scopes")
    if isinstance(raw, list):
        return [str(s) for s in raw if str(s or "").strip()]
    if isinstance(raw, str):
        return [s.strip() for s in raw.replace(",", " ").split() if s.strip()]
    return []


def _grant_from_summary(body: dict[str, Any]) -> str:
    for key in (
        "grant",
        "signed_grant",
        "license_grant",
        "kaidera_os_license_grant",
        "customer_grant",
    ):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in ("license", "customer", "subscription", "grant_bundle"):
        nested = body.get(key)
        if isinstance(nested, dict):
            grant = _grant_from_summary(nested)
            if grant:
                return grant
    return ""


def _summary_license(body: dict[str, Any]) -> dict[str, Any]:
    row = body.get("license")
    return row if isinstance(row, dict) else {}


def _summary_license_id(body: dict[str, Any]) -> str:
    row = _summary_license(body)
    return str(row.get("license_id") or body.get("license_id") or "").strip()


def _feature_atom_enabled(claims: dict[str, Any], atom: str) -> bool:
    try:
        _, _, advanced = lic._parse_features(claims.get("features") or [])  # noqa: SLF001 - same module boundary
        return lic._feature_key(atom) in advanced or "*" in advanced  # noqa: SLF001
    except Exception:
        return False


def _find_key_in_manifold_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    for key in ("api_key", "key", "manifold_api_key", "manifold_key", "inference_key", "inference_api_key"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_manifold_key(body: dict[str, Any]) -> str:
    for key in ("manifold_api_key", "manifold_key", "inference_key", "inference_api_key", "key"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key, val in body.items():
        low = str(key).lower()
        if ("manifold" in low or "inference" in low) and isinstance(val, (dict, str)):
            found = _find_key_in_manifold_payload(val)
            if found:
                return found
        if isinstance(val, dict):
            found = _extract_manifold_key(val)
            if found:
                return found
    return ""


def _extract_manifold_project_id(body: dict[str, Any]) -> str:
    for key in ("manifold_project_id", "inference_project_id", "project_id"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key, val in body.items():
        low = str(key).lower()
        if ("manifold" in low or "inference" in low or key in {"key", "credential"}) and isinstance(val, dict):
            found = _extract_manifold_project_id(val)
            if found:
                return found
        if isinstance(val, dict):
            found = _extract_manifold_project_id(val)
            if found:
                return found
    return ""


def _extract_manifold_base_url(body: dict[str, Any]) -> str:
    for key in ("manifold_base_url", "inference_base_url", "base_url", "url"):
        val = body.get(key)
        if isinstance(val, str) and val.strip() and "/v1" in val:
            return val.strip().rstrip("/")
    for key, val in body.items():
        low = str(key).lower()
        if ("manifold" in low or "inference" in low) and isinstance(val, dict):
            found = _extract_manifold_base_url(val)
            if found:
                return found
        if isinstance(val, dict):
            found = _extract_manifold_base_url(val)
            if found:
                return found
    return ""


def _fail_closed(save_settings: SaveSettings) -> None:
    save_settings({
        REVOKED_KEY: True,
        LAST_SYNC_KEY: _now(),
        MANIFOLD_API_KEY: "",
        MANIFOLD_PROJECT_ID_KEY: "",
    })


async def _request_manifold_key(
    token: str,
    *,
    post_json: PostJson,
    base_url: Optional[str],
    install_id: str,
    project_id: str = "",
) -> tuple[int, dict[str, Any]]:
    root = platform_url(base_url)
    # Keep the original staging paths as compatibility fallbacks, then use the live
    # Manifold customer key surface published by /api/openapi.json.
    for suffix in ("manifold-key", "manifold/key"):
        status, body = await _call_post_json(
            post_json,
            f"{root}/api/v1/license/customer/{suffix}",
            {},
            _license_headers(token),
        )
        if status != 404:
            return status, body

    payload: dict[str, Any] = {
        "key_type": "inference",
        "name": f"Kaidera OS {install_id[:8]}",
    }
    if project_id:
        payload["project_id"] = project_id
    return await _call_post_json(
        post_json,
        f"{root}/api/v1/manifold/keys",
        payload,
        {**_license_headers(token), **_bearer_headers(token)},
    )


async def _request_activation(
    token: str,
    *,
    settings: dict[str, Any],
    save_settings: SaveSettings,
    post_json: PostJson,
    base_url: Optional[str],
    include_body_token: bool = False,
) -> tuple[int, dict[str, Any], str, str]:
    install_id = get_or_create_install_id(settings, save_settings)
    machine_fp = machine_fingerprint({**settings, INSTALL_ID_KEY: install_id}, save_settings)
    payload: dict[str, Any] = {
        "install_id": install_id,
        "machine_fp": machine_fp,
        "app_version": __version__,
        "hostname": platform.node(),
        "platform": platform.system(),
        "arch": platform.machine(),
        "edition": edition.edition(),
    }
    if include_body_token:
        payload["org_login_token"] = token

    status, body = await _call_post_json(
        post_json,
        f"{platform_url(base_url)}/api/v1/license/activate",
        payload,
        _license_headers(token),
    )
    if status == 404:
        # Frozen-v1 compatibility for older platform deployments.
        legacy_payload = {**payload, "org_login_token": token}
        status, body = await _call_post_json(
            post_json,
            f"{platform_url(base_url)}/license/activate",
            legacy_payload,
        )
    return status, body, install_id, machine_fp


async def _sync_session_summary(
    token: str,
    *,
    settings: Optional[dict[str, Any]],
    save_settings: SaveSettings,
    get_json: Optional[GetJson] = None,
    post_json: Optional[PostJson] = None,
    base_url: Optional[str] = None,
    action: str = "heartbeat",
    login_body: Optional[dict[str, Any]] = None,
) -> LicenseTransportResult:
    if not token:
        return LicenseTransportResult(action=action, ok=False, error="no license session token")

    get = get_json or _default_get_json
    post = post_json or _default_post_json
    vals = _settings(settings)
    install_id = get_or_create_install_id(vals, save_settings)
    vals = {**vals, INSTALL_ID_KEY: install_id}

    status, body = await _call_get_json(
        get,
        f"{platform_url(base_url)}/api/v1/license/customer/summary",
        _license_headers(token),
    )
    if status != 200:
        _fail_closed(save_settings)
        return LicenseTransportResult(
            action=action, ok=False, status_code=status, error=_err(body),
            install_id=install_id, revoked=True,
        )

    activation_body: dict[str, Any] = {}
    grant = _grant_from_summary(body)
    claims = lic.verify_license(grant) if grant else None
    if not claims:
        activation_status, activation_body, install_id, machine_fp = await _request_activation(
            token,
            settings=vals,
            save_settings=save_settings,
            post_json=post,
            base_url=base_url,
        )
        if activation_status != 200:
            _fail_closed(save_settings)
            return LicenseTransportResult(
                action=action, ok=False, status_code=activation_status,
                error=_err(activation_body), install_id=install_id,
                machine_fp=machine_fp, revoked=True,
            )
        install_id = str(activation_body.get("install_id") or install_id).strip()
        grant = str(activation_body.get("grant") or "").strip()
        claims = lic.verify_license(grant) if grant else None
        if not claims:
            _fail_closed(save_settings)
            return LicenseTransportResult(
                action=action, ok=False, status_code=activation_status,
                error="platform activation did not return a valid signed grant",
                install_id=install_id, machine_fp=machine_fp, revoked=True,
            )

    manifold_enabled = _feature_atom_enabled(claims, "manifold_access")
    sources = {
        "summary": body,
        "activation": activation_body,
        "login": login_body if isinstance(login_body, dict) else {},
    }
    manifold_key = _extract_manifold_key(sources) or str(vals.get(MANIFOLD_API_KEY) or "").strip()
    manifold_project_id = _extract_manifold_project_id(sources) or str(
        vals.get(MANIFOLD_PROJECT_ID_KEY) or os.environ.get("KAIDERA_MANIFOLD_PROJECT_ID") or ""
    ).strip()
    manifold_base_url = _extract_manifold_base_url(sources) or platform_config.manifold_base_url(
        str(vals.get(MANIFOLD_BASE_URL_KEY) or "")
    )
    if manifold_enabled and (not manifold_key or not manifold_project_id):
        key_status, key_body = await _request_manifold_key(
            token,
            post_json=post,
            base_url=base_url,
            install_id=install_id,
            project_id=manifold_project_id,
        )
        if key_status in (200, 201):
            manifold_key = _extract_manifold_key(key_body) or manifold_key
            manifold_project_id = _extract_manifold_project_id(key_body) or manifold_project_id
            manifold_base_url = _extract_manifold_base_url(key_body) or manifold_base_url
        elif key_status in (401, 403):
            manifold_key = ""
            manifold_project_id = ""

    src = login_body if isinstance(login_body, dict) else {}
    expires_at = str(src.get("expires_at") or vals.get(LICENSE_SESSION_EXPIRES_AT_KEY) or "").strip() or None
    org_id = str(
        body.get("org_id") or src.get("org_id") or claims.get("org_id")
        or vals.get(LICENSE_ORG_ID_KEY) or ""
    ).strip() or None
    license_id = str(
        activation_body.get("license_id") or _summary_license_id(body)
        or claims.get("license_id") or vals.get(LICENSE_ID_KEY) or ""
    ).strip() or None
    existing_scopes = vals.get(LICENSE_SESSION_SCOPES_KEY)
    if isinstance(existing_scopes, list):
        stored_scopes = [str(s) for s in existing_scopes if str(s or "").strip()]
    elif isinstance(existing_scopes, str):
        stored_scopes = [s.strip() for s in existing_scopes.replace(",", " ").split() if s.strip()]
    else:
        stored_scopes = []
    scopes = _scopes(src) or stored_scopes
    extra: dict[str, Any] = {
        LICENSE_SESSION_TOKEN_KEY: token,
        LICENSE_SESSION_EXPIRES_AT_KEY: expires_at or "",
        LICENSE_SESSION_SCOPES_KEY: scopes,
        LICENSE_ORG_ID_KEY: org_id or "",
        LICENSE_ID_KEY: license_id or "",
        MANIFOLD_BASE_URL_KEY: manifold_base_url or platform_config.manifold_base_url(),
    }
    if manifold_enabled:
        extra[MANIFOLD_API_KEY] = manifold_key
        extra[MANIFOLD_PROJECT_ID_KEY] = manifold_project_id
    else:
        extra[MANIFOLD_API_KEY] = ""
        extra[MANIFOLD_PROJECT_ID_KEY] = ""

    stored = _store_verified_grant(grant, claims, save_settings, install_id=install_id, extra=extra)
    return LicenseTransportResult(
        action=action, ok=stored, status_code=status,
        error=None if stored else "could not store platform license session",
        stored=stored, grant_valid=True, install_id=install_id,
        customer=claims.get("customer"), org_id=org_id, license_id=license_id,
        expires_at=expires_at, scopes=scopes,
        manifold_enabled=manifold_enabled,
        manifold_key_stored=bool(manifold_enabled and manifold_key and stored),
        manifold_project_id_stored=bool(manifold_enabled and manifold_project_id and stored),
    )


async def login(
    email: str,
    password: str,
    *,
    mfa_code: Optional[str] = None,
    settings: Optional[dict[str, Any]],
    save_settings: SaveSettings,
    post_json: Optional[PostJson] = None,
    get_json: Optional[GetJson] = None,
    base_url: Optional[str] = None,
) -> LicenseTransportResult:
    """Password-login activation via ``POST /api/v1/license/login``.

    Stores the returned narrow license-session token, pulls the customer summary with
    it, verifies the signed grant, and stores the platform-minted Manifold key only when
    the grant includes ``manifold_access``. Password and MFA code are never persisted.
    """
    email_s = (email or "").strip()
    password_s = password or ""
    if not email_s or not password_s:
        return LicenseTransportResult(action="login", ok=False, error="email and password required")
    payload: dict[str, Any] = {"email": email_s, "password": password_s}
    if (mfa_code or "").strip():
        payload["mfa_code"] = str(mfa_code).strip()

    try:
        post = post_json or _default_post_json
        status, body = await _call_post_json(
            post,
            f"{platform_url(base_url)}/api/v1/license/login",
            payload,
        )
        if status != 200:
            _fail_closed(save_settings)
            return LicenseTransportResult(action="login", ok=False, status_code=status, error=_err(body), revoked=True)
        token = str(body.get("license_token") or "").strip()
        if not token:
            _fail_closed(save_settings)
            return LicenseTransportResult(
                action="login", ok=False, status_code=status,
                error="platform did not return a license_token", revoked=True,
            )
        session_values = {
            LICENSE_SESSION_TOKEN_KEY: token,
            LICENSE_SESSION_EXPIRES_AT_KEY: str(body.get("expires_at") or "").strip(),
            LICENSE_SESSION_SCOPES_KEY: _scopes(body),
            LICENSE_ORG_ID_KEY: str(body.get("org_id") or "").strip(),
        }
        if not save_settings(session_values):
            _fail_closed(save_settings)
            return LicenseTransportResult(
                action="login", ok=False, status_code=status,
                error="could not store platform license session", revoked=True,
            )
        return await _sync_session_summary(
            token,
            settings={**_settings(settings), **session_values},
            save_settings=save_settings,
            get_json=get_json,
            post_json=post,
            base_url=base_url,
            action="login",
            login_body=body,
        )
    except Exception as exc:
        _fail_closed(save_settings)
        return LicenseTransportResult(action="login", ok=False, error=f"login failed: {exc}", revoked=True)


async def activate(
    org_login_token: str,
    *,
    settings: Optional[dict[str, Any]],
    save_settings: SaveSettings,
    post_json: Optional[PostJson] = None,
    base_url: Optional[str] = None,
) -> LicenseTransportResult:
    """Call the platform activation surface and store the verified returned grant.

    Soft by design: network/API/signature failures return ``ok=false`` and do not raise.
    """
    token = (org_login_token or "").strip()
    if not token:
        return LicenseTransportResult(action="activate", ok=False, error="org_login_token required")

    try:
        vals = _settings(settings)
        status, body, install_id, machine_fp = await _request_activation(
            token,
            settings=vals,
            save_settings=save_settings,
            post_json=post_json or _default_post_json,
            base_url=base_url,
            include_body_token=True,
        )
        if status != 200:
            return LicenseTransportResult(
                action="activate", ok=False, status_code=status, error=str(body.get("error") or body.get("detail") or body),
                install_id=install_id, machine_fp=machine_fp,
            )
        grant = str(body.get("grant") or "").strip()
        claims = lic.verify_license(grant)
        if not claims:
            return LicenseTransportResult(
                action="activate", ok=False, status_code=status, error="platform returned an invalid grant",
                install_id=install_id, machine_fp=machine_fp,
            )
        returned_install = str(body.get("install_id") or install_id).strip()
        license_id = str(body.get("license_id") or claims.get("license_id") or "").strip() or None
        stored = _store_verified_grant(
            grant,
            claims,
            save_settings,
            install_id=returned_install,
            extra={LICENSE_ID_KEY: license_id or ""},
        )
        return LicenseTransportResult(
            action="activate", ok=stored, status_code=status,
            error=None if stored else "could not store verified grant",
            stored=stored, grant_valid=True, install_id=returned_install,
            machine_fp=machine_fp, customer=claims.get("customer"), license_id=license_id,
        )
    except Exception as exc:
        return LicenseTransportResult(action="activate", ok=False, error=f"activation failed: {exc}")


async def heartbeat(
    *,
    settings: Optional[dict[str, Any]],
    save_settings: SaveSettings,
    post_json: Optional[PostJson] = None,
    get_json: Optional[GetJson] = None,
    base_url: Optional[str] = None,
) -> LicenseTransportResult:
    """Refresh the current verified grant.

    A grant with no ``license_id`` is treated as an offline/manual grant and is not
    heartbeated. Session-backed platform failures fail closed to the public floor.
    """
    session_token = ""
    try:
        vals = _settings(settings)
        session_token = _active_session_token(vals)
        if session_token:
            current = _active_token(vals)
            claims = lic.verify_license(current) if current else None
            license_id = str(
                vals.get(LICENSE_ID_KEY) or (claims or {}).get("license_id") or ""
            ).strip()
            if not claims or not license_id:
                return await _sync_session_summary(
                    session_token,
                    settings=vals,
                    save_settings=save_settings,
                    get_json=get_json,
                    post_json=post_json,
                    base_url=base_url,
                    action="heartbeat",
                )

            install_id = get_or_create_install_id(vals, save_settings)
            machine_fp = machine_fingerprint(
                {**vals, INSTALL_ID_KEY: install_id}, save_settings
            )
            payload = {
                "license_id": license_id,
                "install_id": install_id,
                "machine_fp": machine_fp,
                "current_version": __version__,
            }
            post = post_json or _default_post_json
            status, body = await _call_post_json(
                post,
                f"{platform_url(base_url)}/api/v1/license/heartbeat",
                payload,
                _license_headers(session_token),
            )
            if status == 404:
                status, body = await _call_post_json(
                    post,
                    f"{platform_url(base_url)}/license/heartbeat",
                    payload,
                )
            if status != 200:
                _fail_closed(save_settings)
                return LicenseTransportResult(
                    action="heartbeat", ok=False, status_code=status,
                    error=_err(body), install_id=install_id,
                    machine_fp=machine_fp, license_id=license_id, revoked=True,
                )

            latest = body.get("latest_release") if isinstance(body.get("latest_release"), dict) else None
            if body.get("revoked") is True:
                _fail_closed(save_settings)
                if latest:
                    save_settings({LATEST_RELEASE_KEY: latest})
                return LicenseTransportResult(
                    action="heartbeat", ok=True, status_code=status, stored=True,
                    install_id=install_id, machine_fp=machine_fp,
                    license_id=license_id, revoked=True, latest_release=latest,
                    customer=claims.get("customer"),
                )

            refreshed = str(body.get("grant") or "").strip()
            refreshed_claims = lic.verify_license(refreshed) if refreshed else claims
            if not refreshed_claims:
                _fail_closed(save_settings)
                return LicenseTransportResult(
                    action="heartbeat", ok=False, status_code=status,
                    error="platform returned an invalid grant", install_id=install_id,
                    machine_fp=machine_fp, license_id=license_id, revoked=True,
                )
            extra: dict[str, Any] = {
                LICENSE_ID_KEY: str(refreshed_claims.get("license_id") or license_id),
                REVOKED_KEY: False,
            }
            if latest:
                extra[LATEST_RELEASE_KEY] = latest
            if not _feature_atom_enabled(refreshed_claims, "manifold_access"):
                extra[MANIFOLD_API_KEY] = ""
                extra[MANIFOLD_PROJECT_ID_KEY] = ""
            stored = _store_verified_grant(
                refreshed or current,
                refreshed_claims,
                save_settings,
                install_id=install_id,
                extra=extra,
            )
            return LicenseTransportResult(
                action="heartbeat", ok=stored, status_code=status,
                error=None if stored else "could not store refreshed grant",
                stored=stored, grant_valid=True, install_id=install_id,
                machine_fp=machine_fp, license_id=extra[LICENSE_ID_KEY] or None,
                latest_release=latest, customer=refreshed_claims.get("customer"),
                manifold_enabled=_feature_atom_enabled(refreshed_claims, "manifold_access"),
                manifold_key_stored=bool(vals.get(MANIFOLD_API_KEY)),
                manifold_project_id_stored=bool(vals.get(MANIFOLD_PROJECT_ID_KEY)),
            )
        current = _active_token(vals)
        claims = lic.verify_license(current) if current else None
        if not claims:
            return LicenseTransportResult(action="heartbeat", ok=False, error="no valid license grant to heartbeat")
        license_id = str(claims.get("license_id") or "").strip()
        if not license_id:
            return LicenseTransportResult(action="heartbeat", ok=False, error="current grant has no license_id")

        install_id = get_or_create_install_id(vals, save_settings)
        machine_fp = machine_fingerprint({**vals, INSTALL_ID_KEY: install_id}, save_settings)
        payload = {
            "license_id": license_id,
            "install_id": install_id,
            "machine_fp": machine_fp,
            "current_version": __version__,
        }
        status, body = await _call_post_json(
            post_json or _default_post_json,
            f"{platform_url(base_url)}/license/heartbeat", payload
        )
        if status != 200:
            return LicenseTransportResult(
                action="heartbeat", ok=False, status_code=status,
                error=str(body.get("error") or body.get("detail") or body),
                install_id=install_id, machine_fp=machine_fp,
            )
        if body.get("revoked") is True:
            save_settings({REVOKED_KEY: True, LAST_SYNC_KEY: _now()})
            return LicenseTransportResult(
                action="heartbeat", ok=True, status_code=status, stored=True,
                install_id=install_id, machine_fp=machine_fp, revoked=True,
                latest_release=body.get("latest_release") if isinstance(body.get("latest_release"), dict) else None,
                customer=claims.get("customer"),
            )

        latest = body.get("latest_release") if isinstance(body.get("latest_release"), dict) else None
        grant = str(body.get("grant") or "").strip()
        if not grant:
            stored = bool(save_settings({LAST_SYNC_KEY: _now(), LATEST_RELEASE_KEY: latest} if latest else {LAST_SYNC_KEY: _now()}))
            return LicenseTransportResult(
                action="heartbeat", ok=True, status_code=status, stored=stored,
                grant_valid=True, install_id=install_id, machine_fp=machine_fp,
                latest_release=latest, customer=claims.get("customer"),
            )
        new_claims = lic.verify_license(grant)
        if not new_claims:
            return LicenseTransportResult(
                action="heartbeat", ok=False, status_code=status,
                error="platform returned an invalid grant", install_id=install_id, machine_fp=machine_fp,
            )
        stored = _store_verified_grant(
            grant, new_claims, save_settings, install_id=install_id,
            extra={LATEST_RELEASE_KEY: latest} if latest else None,
        )
        return LicenseTransportResult(
            action="heartbeat", ok=stored, status_code=status,
            error=None if stored else "could not store refreshed grant",
            stored=stored, grant_valid=True, install_id=install_id,
            machine_fp=machine_fp, latest_release=latest, customer=new_claims.get("customer"),
        )
    except Exception as exc:
        if session_token:
            _fail_closed(save_settings)
        return LicenseTransportResult(action="heartbeat", ok=False, error=f"heartbeat failed: {exc}")


async def customer_action(
    action: str,
    *,
    settings: Optional[dict[str, Any]],
    save_settings: SaveSettings,
    post_json: Optional[PostJson] = None,
    get_json: Optional[GetJson] = None,
    base_url: Optional[str] = None,
) -> LicenseTransportResult:
    """Run a license customer action via the stored license-session token.

    Supported actions are the staging self-management verbs: ``restore``, ``enable``,
    and ``expire``. After a successful platform action, the local grant/key posture is
    reloaded from ``GET /api/v1/license/customer/summary`` so RESTORE and expiry both
    flow through the same signed-grant verifier.
    """
    verb = (action or "").strip().lower()
    if verb not in {"restore", "enable", "expire"}:
        return LicenseTransportResult(action=verb or "customer_action", ok=False, error="unsupported license action")
    token = _active_session_token(settings)
    if not token:
        return LicenseTransportResult(action=verb, ok=False, error="no license session token")
    try:
        vals = _settings(settings)
        current = _active_token(vals)
        claims = lic.verify_license(current) if current else None
        license_id = str(
            vals.get(LICENSE_ID_KEY) or (claims or {}).get("license_id") or ""
        ).strip()
        if not license_id:
            summary_status, summary = await _call_get_json(
                get_json or _default_get_json,
                f"{platform_url(base_url)}/api/v1/license/customer/summary",
                _license_headers(token),
            )
            if summary_status != 200:
                _fail_closed(save_settings)
                return LicenseTransportResult(
                    action=verb, ok=False, status_code=summary_status,
                    error=_err(summary), revoked=True,
                )
            license_id = _summary_license_id(summary)
        if not license_id:
            _fail_closed(save_settings)
            return LicenseTransportResult(
                action=verb, ok=False, error="platform summary has no license_id", revoked=True,
            )

        install_id = get_or_create_install_id(vals, save_settings)
        root = platform_url(base_url)
        if verb == "enable":
            path = f"/api/v1/license/customer/licenses/{quote(license_id, safe='')}/seats/{quote(install_id, safe='')}/enable"
        else:
            path = f"/api/v1/license/customer/licenses/{quote(license_id, safe='')}/{verb}"
        post = post_json or _default_post_json
        status, body = await _call_post_json(
            post,
            f"{root}{path}",
            {"reason": f"Kaidera OS operator requested {verb}"},
            _license_headers(token),
        )
        if status != 200:
            _fail_closed(save_settings)
            return LicenseTransportResult(
                action=verb, ok=False, status_code=status, error=_err(body),
                install_id=install_id, license_id=license_id, revoked=True,
            )
        if verb == "expire":
            _fail_closed(save_settings)
            return LicenseTransportResult(
                action=verb, ok=True, status_code=status, stored=True,
                install_id=install_id, license_id=license_id, revoked=True,
            )
        return await _sync_session_summary(
            token,
            settings={**vals, LICENSE_ID_KEY: license_id, INSTALL_ID_KEY: install_id},
            save_settings=save_settings,
            get_json=get_json,
            post_json=post_json,
            base_url=base_url,
            action=verb,
        )
    except Exception as exc:
        _fail_closed(save_settings)
        return LicenseTransportResult(action=verb, ok=False, error=f"{verb} failed: {exc}", revoked=True)


async def releases(
    channel: str = "stable",
    *,
    get_json: Optional[GetJson] = None,
    base_url: Optional[str] = None,
) -> LicenseTransportResult:
    """Fetch the latest platform release metadata for a channel.

    Read-only and soft by design. This is advisory metadata for the existing signed
    update path; it never mutates license state and never gates usage.
    """
    chan = (channel or "stable").strip() or "stable"
    try:
        get = get_json or _default_get_json
        status, body = await _call_get_json(
            get,
            f"{platform_url(base_url)}/api/v1/license/releases/{quote(chan, safe='')}"
        )
        if status == 404:
            status, body = await _call_get_json(
                get,
                f"{platform_url(base_url)}/license/releases/{quote(chan, safe='')}"
            )
        if status != 200:
            return LicenseTransportResult(
                action="releases",
                ok=False,
                status_code=status,
                error=str(body.get("error") or body.get("detail") or body),
            )
        if not isinstance(body, dict):
            return LicenseTransportResult(
                action="releases",
                ok=False,
                status_code=status,
                error="platform returned invalid release metadata",
            )
        return LicenseTransportResult(
            action="releases",
            ok=True,
            status_code=status,
            latest_release=body,
        )
    except Exception as exc:
        return LicenseTransportResult(action="releases", ok=False, error=f"release check failed: {exc}")


OAUTH_CLIENT_ID = "kaidera-os-license"


async def start_device_flow(base_url: Optional[str] = None, post_json: Optional[PostJson] = None) -> dict[str, Any]:
    import secrets
    import base64
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")

    payload = {
        "client_id": OAUTH_CLIENT_ID,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    status, body = await (post_json or _default_post_json)(f"{platform_url(base_url)}/oauth/device_authorization", payload)
    if status != 200:
        return {"ok": False, "error": body.get("error") or str(body)}
    missing = [k for k in ("device_code", "user_code", "verification_uri") if not body.get(k)]
    if missing:
        return {"ok": False, "error": f"device authorization response missing: {', '.join(missing)}"}
    return {
        "ok": True,
        "device_code": body.get("device_code"),
        "user_code": body.get("user_code"),
        "verification_uri": body.get("verification_uri"),
        "interval": body.get("interval", 5),
        "code_verifier": verifier,
    }


async def poll_device_flow(device_code: str, code_verifier: str, base_url: Optional[str] = None, post_json: Optional[PostJson] = None) -> dict[str, Any]:
    payload = {
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "code_verifier": code_verifier,
    }
    status, body = await (post_json or _default_post_json)(f"{platform_url(base_url)}/oauth/token", payload)
    if status in (400, 403, 404, 428):
        err = body.get("error")
        if err == "authorization_pending":
            return {"status": "pending"}
        if err == "slow_down":
            return {"status": "pending", "slow_down": True}
        if err == "expired_token":
            return {"status": "error", "message": "Device login code expired"}
        if err == "access_denied":
            return {"status": "error", "message": "Device login was denied"}

    if status != 200:
        return {"status": "error", "message": str(body.get("error") or body)}

    access_token = body.get("access_token")
    if not access_token:
        return {"status": "error", "message": "No access_token returned"}

    return {"status": "done", "org_login_token": access_token}
