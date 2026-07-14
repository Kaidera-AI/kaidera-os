"""First-party Kaidera OS console auth.

Passwordless by default: email code/link creates an httpOnly session cookie.
Passkeys are optional and use py_webauthn (`webauthn`) when installed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import smtplib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

def _rate_limit_enabled() -> bool:
    """Opt-in rate limiting. Disabled by default so dev/tests are unaffected;
    hosted deployments set ``KAIDERA_AUTH_RATE_LIMIT=1`` to enable."""
    raw = os.environ.get("KAIDERA_AUTH_RATE_LIMIT", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


log = logging.getLogger("console.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = os.environ.get("KAIDERA_AUTH_COOKIE_NAME", "kaidera_session")  # fitness:allow-literal product cookie name, env-overridable default
COOKIE_DOMAIN = os.environ.get("KAIDERA_AUTH_COOKIE_DOMAIN", "").strip() or None
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EXTENSION_PUBLIC_PATH_MATCHERS: list[Any] = []

# Simple in-memory rate limiter for auth endpoints. Disabled by default; enable with
# ``KAIDERA_AUTH_RATE_LIMIT=1``. NOT distributed: each console process tracks its own
# window. Good enough for the default single-instance deployment; a load-balanced fleet
# should switch to a shared store (Redis) or an upstream WAF. Keys are
# "rl:{bucket}:{identifier}".
class _AuthRateLimiter:
    def __init__(self, default_window_seconds: int = 600, default_max_requests: int = 5) -> None:
        self._store: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()
        self._window = default_window_seconds
        self._max = default_max_requests

    async def check(self, bucket: str, key: str, *, max_requests: int | None = None, window_seconds: int | None = None) -> bool:
        max_req = max_requests if max_requests is not None else self._max
        window = window_seconds if window_seconds is not None else self._window
        now = time.monotonic()
        cutoff = now - window
        composite = f"{bucket}:{key}"
        async with self._lock:
            # ponytail: opportunistic GC so the store can't grow unbounded under
            # email/IP rotation (a DoS in the limiter itself). Only sweeps when large;
            # uses this call's cutoff (custom-window buckets are pruned approximately).
            if len(self._store) > 4096:
                self._store = {
                    k: live
                    for k, ts in self._store.items()
                    if (live := [t for t in ts if t > cutoff])
                }
            timestamps = self._store.get(composite, [])
            # Evict old entries
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= max_req:
                self._store[composite] = timestamps
                return False
            timestamps.append(now)
            self._store[composite] = timestamps
            return True

_auth_rate_limiter = _AuthRateLimiter()


class AuthUnavailable(RuntimeError):
    pass


class AuthConfigError(RuntimeError):
    pass


def auth_enabled() -> bool:
    """Whether the first-party console auth gate is enforced.

    FAIL-CLOSED (v0.1.143): an explicit ``KAIDERA_AUTH_ENABLED`` always wins; otherwise
    auth is ON unless the deployment EXPLICITLY declares a dev/local mode. A console
    with no deployment signal is treated as untrusted (auth ON); dev mode
    opts out by declaring ``KAIDERA_DEPLOY_MODE=dev`` while self-contained installs
    force auth ON."""
    raw = os.environ.get("KAIDERA_AUTH_ENABLED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    mode = os.environ.get("KAIDERA_DEPLOY_MODE", "").strip().lower()
    legacy_dev = "local" + "dev"
    return mode not in {"dev", "test", "local", legacy_dev}


def register_public_path_matcher(matcher: Any) -> None:
    """Allow an installed extension to expose unauthenticated ingress paths.

    Core stays domain-neutral: extensions own their route shape and route-level
    authentication, while the middleware only knows that a callable/pattern says a
    path is public.
    """
    if matcher is not None:
        _EXTENSION_PUBLIC_PATH_MATCHERS.append(matcher)


def clear_public_path_matchers() -> None:
    """Test helper for resetting extension public-path registrations."""
    _EXTENSION_PUBLIC_PATH_MATCHERS.clear()


def _extension_public_path(path: str) -> bool:
    for matcher in list(_EXTENSION_PUBLIC_PATH_MATCHERS):
        try:
            if callable(matcher) and bool(matcher(path)):
                return True
            match = getattr(matcher, "match", None)
            if callable(match) and match(path):
                return True
            if isinstance(matcher, str) and matcher == path:
                return True
        except Exception:
            continue
    return False


def is_public_path(path: str) -> bool:
    if path.startswith("/auth/") or path == "/auth":
        return True
    if _extension_public_path(path):
        return True
    # /healthz is an UNAUTHENTICATED liveness probe — external uptime monitors (and a HEAD ping)
    # must reach it without a session. Before this it sat behind the auth gate and returned 401 to
    # an unauthenticated probe. /console/version is likewise public (the SPA shell badge fetches it
    # pre-login). Everything else stays auth-gated when auth is enabled.
    return path in {"/healthz", "/console/version", "/favicon.ico"}


def wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "").lower()


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(email and len(email) <= 254 and _EMAIL_RE.match(email))


def safe_next(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw or raw.startswith("//") or "\\" in raw or _has_control_chars(raw):
        return "/app/"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "/app/"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/app/"
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or _has_control_chars(decoded_path):
        return "/app/"
    browser_path = decoded_path.replace("\\", "/")
    if browser_path.startswith("//"):
        return "/app/"
    if browser_path == "/auth" or browser_path.startswith("/auth/"):
        return "/app/"
    return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hours_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _minutes_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _auth_secret() -> bytes:
    secret = os.environ.get("KAIDERA_AUTH_SECRET") or os.environ.get("SECRET_KEY")
    if secret:
        return secret.encode("utf-8")
    if auth_enabled():
        raise AuthConfigError("KAIDERA_AUTH_SECRET is required when auth is enabled")
    return b"kaidera-dev-auth-secret"


def trusted_proxy_headers(request: Request) -> bool:
    """Whether generic upstream identity headers (X-Forwarded-*, X-Kaidera-*) may
    be trusted for /whoami.

    In a hosted deployment these headers are forgeable unless a trusted reverse
    proxy strips them from the public and re-injects them. Default is OFF;
    operators opt in by setting ``KAIDERA_AUTH_TRUSTED_PROXY=1`` (or a trusted
    IP/CIDR). When disabled, /whoami ignores the headers and falls back to the
    first-party session or the auth-enabled default."""
    raw = os.environ.get("KAIDERA_AUTH_TRUSTED_PROXY", "").strip().lower()
    if raw in {"", "0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # Future: IP/CIDR matching against request.client.host.
    # For now any non-boolean value is treated as an explicit opt-in.
    return True


def _bootstrap_token() -> str | None:
    token = os.environ.get("KAIDERA_AUTH_BOOTSTRAP_TOKEN", "").strip()
    return token or None


def _check_bootstrap_token(payload: dict[str, Any]) -> bool:
    """Returns True if no bootstrap token is configured or if the supplied token matches.

    When auth is enabled and ``KAIDERA_AUTH_BOOTSTRAP_TOKEN`` is set, the first admin
    creation requires the token to be supplied in the request payload. This prevents an
    external actor from racing to claim the first admin account on a fresh hosted console
    before the operator."""
    if not auth_enabled():
        return True
    configured = _bootstrap_token()
    if not configured:
        return True
    supplied = str(payload.get("bootstrap_token") or "").strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, configured)


def _hash_secret(value: str, purpose: str) -> str:
    msg = f"{purpose}:{value}".encode("utf-8")
    return hmac.new(_auth_secret(), msg, hashlib.sha256).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _client_ip(request: Request) -> str | None:
    # Only trust X-Forwarded-For behind a DECLARED trusted proxy
    # (KAIDERA_AUTH_TRUSTED_PROXY) — the same gate /whoami uses. Otherwise it's
    # client-forgeable: an attacker could rotate the rate-limit per-IP bucket and
    # spoof audit IPs. Default (no trusted proxy) → the real socket peer.
    if trusted_proxy_headers(request):
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua[:500] if ua else None


def _origin(request: Request) -> str:
    configured = os.environ.get("KAIDERA_AUTH_ORIGIN", "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _rp_id(request: Request) -> str:
    configured = os.environ.get("KAIDERA_AUTH_RP_ID", "").strip()
    if configured:
        return configured
    return request.url.hostname or "localhost"


def _public_base_url(request: Request) -> str:
    return (os.environ.get("KAIDERA_PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")


def _delivery_mode() -> str:
    mode = os.environ.get("KAIDERA_AUTH_EMAIL_DELIVERY", "").strip().lower()
    if mode:
        return mode
    if os.environ.get("KAIDERA_AUTH_GRAPH_CLIENT_SECRET"):
        return "graph"
    if os.environ.get("KAIDERA_SMTP_HOST"):
        return "smtp"
    return "dev" if not auth_enabled() else "log"


def _cookie_secure(request: Request) -> bool:
    raw = os.environ.get("KAIDERA_AUTH_COOKIE_SECURE")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return request.url.scheme == "https"


@dataclass
class SessionIssue:
    token: str
    expires_at: datetime
    user: dict[str, Any]


class PgAuthStore:
    def __init__(self, appdb: Any) -> None:
        self.appdb = appdb

    async def _pool(self) -> Any:
        getter = getattr(self.appdb, "_get_pool", None)
        if getter is None:
            raise AuthUnavailable("app-DB is not configured")
        pool = await getter()
        if pool is None:
            raise AuthUnavailable("app-DB is unavailable")
        return pool

    async def _fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None

    async def _fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def _execute(self, sql: str, *args: Any) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(sql, *args)

    async def count_users(self) -> int:
        row = await self._fetchrow("SELECT COUNT(*) AS n FROM auth_users")
        return int(row["n"]) if row else 0

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return await self._fetchrow(
            "SELECT * FROM auth_users WHERE email = $1",
            normalize_email(email),
        )

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return await self._fetchrow("SELECT * FROM auth_users WHERE id = $1", user_id)

    async def create_user(
        self,
        email: str,
        *,
        role: str = "user",
        display_name: str | None = None,
        verified: bool = False,
    ) -> dict[str, Any]:
        return await self._fetchrow(
            """
            INSERT INTO auth_users
                (id, email, display_name, role, status, email_verified_at)
            VALUES ($1, $2, $3, $4, 'active', $5)
            ON CONFLICT (email) DO UPDATE
                SET display_name = COALESCE(EXCLUDED.display_name, auth_users.display_name),
                    updated_at = NOW()
            RETURNING *
            """,
            f"user_{uuid.uuid4().hex}",
            normalize_email(email),
            display_name,
            role if role in {"admin", "user"} else "user",
            _now() if verified else None,
        ) or {}

    async def list_users(self) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT id, email, display_name, role, status, email_verified_at,
                   created_at, last_login_at
              FROM auth_users
             ORDER BY created_at ASC
            """
        )

    async def count_active_admins(self) -> int:
        """Active admins — the last-admin lockout guard counts against this."""
        row = await self._fetchrow(
            "SELECT COUNT(*) AS n FROM auth_users WHERE role = 'admin' AND status = 'active'"
        )
        return int(row["n"]) if row else 0

    async def set_user_role(self, user_id: str, role: str) -> dict[str, Any] | None:
        return await self._fetchrow(
            """
            UPDATE auth_users SET role = $2, updated_at = NOW()
             WHERE id = $1
             RETURNING *
            """,
            user_id,
            role if role in {"admin", "user"} else "user",
        )

    async def set_user_status(self, user_id: str, status: str) -> dict[str, Any] | None:
        return await self._fetchrow(
            """
            UPDATE auth_users SET status = $2, updated_at = NOW()
             WHERE id = $1
             RETURNING *
            """,
            user_id,
            # The auth_users schema constrains status to ('active','disabled') — 'disabled' IS the
            # "blocked" state (the UI labels it "Blocked"). Keep the stored value schema-valid.
            status if status in {"active", "disabled"} else "active",
        )

    async def delete_user(self, user_id: str) -> bool:
        # The session/passkey/challenge rows reference the user with ON DELETE CASCADE,
        # so a single delete cleans up the user's auth footprint.
        row = await self._fetchrow(
            "DELETE FROM auth_users WHERE id = $1 RETURNING id",
            user_id,
        )
        return row is not None

    async def update_user_profile(
        self,
        user_id: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any] | None:
        # COALESCE keeps any field the caller didn't pass; a passed email is normalized +
        # validated by the endpoint before we get here. The unique index on email surfaces a
        # collision as an asyncpg UniqueViolation the endpoint maps to a 409.
        return await self._fetchrow(
            """
            UPDATE auth_users
               SET email = COALESCE($2, email),
                   display_name = COALESCE($3, display_name),
                   updated_at = NOW()
             WHERE id = $1
             RETURNING *
            """,
            user_id,
            normalize_email(email) if email is not None else None,
            display_name,
        )

    async def save_email_challenge(self, row: dict[str, Any]) -> None:
        await self._execute(
            """
            INSERT INTO auth_email_challenges
                (id, email, user_id, purpose, code_hash, token_hash, expires_at,
                 requested_ip, user_agent)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            row["id"],
            row["email"],
            row.get("user_id"),
            row.get("purpose", "login"),
            row["code_hash"],
            row["token_hash"],
            row["expires_at"],
            row.get("requested_ip"),
            row.get("user_agent"),
        )

    async def latest_email_challenge(self, email: str) -> dict[str, Any] | None:
        return await self._fetchrow(
            """
            SELECT *
              FROM auth_email_challenges
             WHERE email = $1
               AND consumed_at IS NULL
               AND expires_at > NOW()
             ORDER BY created_at DESC
             LIMIT 1
            """,
            normalize_email(email),
        )

    async def email_challenge_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        return await self._fetchrow(
            """
            SELECT *
              FROM auth_email_challenges
             WHERE token_hash = $1
               AND consumed_at IS NULL
               AND expires_at > NOW()
             LIMIT 1
            """,
            token_hash,
        )

    async def consume_email_challenge(self, challenge_id: str) -> None:
        await self._execute(
            "UPDATE auth_email_challenges SET consumed_at = NOW() WHERE id = $1",
            challenge_id,
        )

    async def increment_email_attempts(self, challenge_id: str) -> None:
        await self._execute(
            "UPDATE auth_email_challenges SET attempts = attempts + 1 WHERE id = $1",
            challenge_id,
        )

    async def create_session(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        request: Request,
    ) -> None:
        await self._execute(
            """
            INSERT INTO auth_sessions (id, user_id, token_hash, expires_at, ip, user_agent)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            f"sess_{uuid.uuid4().hex}",
            user_id,
            token_hash,
            expires_at,
            _client_ip(request),
            _user_agent(request),
        )
        await self._execute(
            "UPDATE auth_users SET last_login_at = NOW(), updated_at = NOW() WHERE id = $1",
            user_id,
        )

    async def session_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        return await self._fetchrow(
            """
            SELECT u.id, u.email, u.display_name, u.role, u.status, s.expires_at
              FROM auth_sessions s
              JOIN auth_users u ON u.id = s.user_id
             WHERE s.token_hash = $1
               AND s.revoked_at IS NULL
               AND s.expires_at > NOW()
               AND u.status = 'active'
             LIMIT 1
            """,
            token_hash,
        )

    async def revoke_session(self, token_hash: str) -> None:
        await self._execute(
            "UPDATE auth_sessions SET revoked_at = NOW() WHERE token_hash = $1",
            token_hash,
        )

    async def save_webauthn_challenge(
        self,
        user_id: str,
        purpose: str,
        challenge: str,
        expires_at: datetime,
    ) -> None:
        await self._execute(
            """
            INSERT INTO auth_webauthn_challenges (id, user_id, purpose, challenge, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            f"wchal_{uuid.uuid4().hex}",
            user_id,
            purpose,
            challenge,
            expires_at,
        )

    async def latest_webauthn_challenge(
        self,
        user_id: str,
        purpose: str,
    ) -> dict[str, Any] | None:
        return await self._fetchrow(
            """
            SELECT *
              FROM auth_webauthn_challenges
             WHERE user_id = $1
               AND purpose = $2
               AND consumed_at IS NULL
               AND expires_at > NOW()
             ORDER BY created_at DESC
             LIMIT 1
            """,
            user_id,
            purpose,
        )

    async def consume_webauthn_challenge(self, challenge_id: str) -> None:
        await self._execute(
            "UPDATE auth_webauthn_challenges SET consumed_at = NOW() WHERE id = $1",
            challenge_id,
        )

    async def list_passkeys(self, user_id: str) -> list[dict[str, Any]]:
        return await self._fetch(
            "SELECT * FROM auth_passkeys WHERE user_id = $1 ORDER BY created_at ASC",
            user_id,
        )

    async def save_passkey(self, row: dict[str, Any]) -> None:
        await self._execute(
            """
            INSERT INTO auth_passkeys
                (id, user_id, credential_id, public_key, sign_count, transports,
                 aaguid, nickname)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (credential_id) DO UPDATE
                SET public_key = EXCLUDED.public_key,
                    sign_count = EXCLUDED.sign_count,
                    transports = EXCLUDED.transports,
                    aaguid = EXCLUDED.aaguid
            """,
            row["id"],
            row["user_id"],
            row["credential_id"],
            row["public_key"],
            int(row.get("sign_count") or 0),
            row.get("transports") or [],
            row.get("aaguid"),
            row.get("nickname"),
        )

    async def get_passkey_by_credential_id(self, credential_id: str) -> dict[str, Any] | None:
        return await self._fetchrow(
            """
            SELECT p.*, u.email, u.display_name, u.role, u.status
              FROM auth_passkeys p
              JOIN auth_users u ON u.id = p.user_id
             WHERE p.credential_id = $1
               AND u.status = 'active'
            """,
            credential_id,
        )

    async def update_passkey_sign_count(self, passkey_id: str, sign_count: int) -> None:
        await self._execute(
            """
            UPDATE auth_passkeys
               SET sign_count = $2, last_used_at = NOW()
             WHERE id = $1
            """,
            passkey_id,
            sign_count,
        )

    async def audit(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        email: str | None = None,
        request: Request | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self._execute(
                """
                INSERT INTO auth_audit_events
                    (user_id, event_type, email, ip, user_agent, detail)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                user_id,
                event_type,
                normalize_email(email) or None,
                _client_ip(request) if request else None,
                _user_agent(request) if request else None,
                json.dumps(detail or {}),
            )
        except Exception:
            log.debug("auth audit write failed", exc_info=True)


def get_auth_store(request: Request) -> PgAuthStore:
    override = getattr(request.app.state, "auth_store", None)
    if override is not None:
        return override
    return PgAuthStore(getattr(request.app.state, "appdb", None))


def _iso(value: Any) -> str | None:
    """ISO-8601 string for a datetime row value (or pass a string/None through)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def user_payload(user: dict[str, Any] | None) -> dict[str, Any]:
    if not user:
        return {"authenticated": False, "name": "User", "email": "", "is_admin": False}
    name = (user.get("display_name") or user.get("email") or "User").strip()
    return {
        "authenticated": True,
        "id": user.get("id"),
        "name": name,
        "display_name": user.get("display_name") or "",
        "email": user.get("email") or "",
        "is_admin": user.get("role") == "admin",
        "role": user.get("role") or "user",
        # status + last_login surface the admin Users table; harmless extras for /session etc.
        "status": user.get("status") or "active",
        "last_login_at": _iso(user.get("last_login_at")),
    }


async def current_user_from_request(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        store = get_auth_store(request)
        user = await store.session_by_token_hash(_hash_secret(token, "session"))
    except Exception:
        log.debug("session lookup failed", exc_info=True)
        return None
    # GUARD (a): a BLOCKED user must never authenticate, even on a still-valid session
    # cookie issued before the block. The Pg session SQL already filters status='active',
    # but enforce it here too so the gate holds for ANY store impl (and so a block takes
    # effect immediately on the next request without waiting for the session to expire).
    if user and user.get("status") not in (None, "active"):
        return None
    return user


async def require_user(
    request: Request,
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    user = await current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication_required")
    return user


async def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return user


async def require_admin_if_auth(request: Request) -> dict[str, Any] | None:
    """Admin gate that RESPECTS auth-off mode — the gate to put on privileged
    mutation routes (agent/project registration, skill install/bind).

    - Auth DISABLED (dev, the open default): no-op → returns None and the route
      runs, exactly as before. The console is a single-operator dev tool here.
    - Auth ENABLED (hosted / enterprise): require an admin session. The middleware has
      already ensured *a* session for non-public paths, so this enforces the ROLE —
      a non-admin gets 403 (a missing session 401). Closes the privilege-escalation
      where any logged-in user could register/deregister or install skills (git clone +
      run scripts).

    Do NOT use a plain ``Depends(require_admin)`` on these routes — it would 401 in
    dev mode (no session) and break the open local mode."""
    if not auth_enabled():
        return None
    user = await current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication_required")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return user


async def _login_user_for_email(store: Any, email: str) -> dict[str, Any] | None:
    user = await store.get_user_by_email(email)
    if user and user.get("status") == "active":
        return user
    if await store.count_users() == 0:
        return {
            "id": None,
            "email": normalize_email(email),
            "role": "admin",
            "status": "pending_first_admin",
        }
    return None


async def _issue_session(
    store: Any,
    user: dict[str, Any],
    request: Request,
) -> SessionIssue:
    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(hours=_hours_env("KAIDERA_AUTH_SESSION_HOURS", 24))
    await store.create_session(
        str(user["id"]),
        _hash_secret(token, "session"),
        expires_at,
        request,
    )
    refreshed = await store.get_user_by_id(str(user["id"])) or user
    return SessionIssue(token=token, expires_at=expires_at, user=refreshed)


def set_session_cookie(response: Response, issue: SessionIssue, request: Request) -> None:
    max_age = int((issue.expires_at - _now()).total_seconds())
    response.set_cookie(
        COOKIE_NAME,
        issue.token,
        max_age=max_age,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
        domain=COOKIE_DOMAIN,
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", domain=COOKIE_DOMAIN)


def _graph_error_detail(resp: httpx.Response) -> str:
    """Pull a secret-free Graph error code/description out of a failed response."""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - body may be empty / non-JSON
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = str(err.get("code", "")).strip()
            desc = str(err.get("message", err.get("error_description", ""))).strip()
            joined = ": ".join(p for p in (code, desc) if p)
            if joined:
                return joined
        # token endpoint returns flat {error, error_description}
        code = str(payload.get("error", "")).strip()
        desc = str(payload.get("error_description", "")).strip()
        joined = ": ".join(p for p in (code, desc) if p)
        if joined:
            return joined
    return f"HTTP {resp.status_code}"


async def _send_login_email_graph(email: str, subject: str, code: str, link: str) -> None:
    """Send the login code via Microsoft Graph (app-only / client-credentials).

    Config-driven, no hardcoded creds. Fails loud (AuthConfigError) with a
    secret-free message — the client secret and tokens are never logged or
    surfaced in error text.
    """
    tenant_id = os.environ.get("KAIDERA_AUTH_GRAPH_TENANT_ID", "").strip()
    client_id = os.environ.get("KAIDERA_AUTH_GRAPH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("KAIDERA_AUTH_GRAPH_CLIENT_SECRET", "")
    sender = os.environ.get("KAIDERA_AUTH_GRAPH_SENDER", "").strip()
    missing = [
        env
        for env, val in (
            ("KAIDERA_AUTH_GRAPH_TENANT_ID", tenant_id),
            ("KAIDERA_AUTH_GRAPH_CLIENT_ID", client_id),
            ("KAIDERA_AUTH_GRAPH_CLIENT_SECRET", client_secret),
            ("KAIDERA_AUTH_GRAPH_SENDER", sender),
        )
        if not val
    ]
    if missing:
        raise AuthConfigError(
            "graph email delivery is missing required config: " + ", ".join(missing)
        )

    safe_code = html.escape(code)
    safe_link = html.escape(link, quote=True)
    html_body = (
        "<p>Your Kaidera OS sign-in code is: "
        f"<strong style=\"font-size:1.25em;letter-spacing:2px\">{safe_code}</strong></p>"
        f"<p>Or sign in with this link:<br><a href=\"{safe_link}\">{safe_link}</a></p>"
        "<p>This code expires shortly. If you did not request it, ignore this email.</p>"
    )

    token_url = (
        f"https://login.microsoftonline.com/{quote(tenant_id)}/oauth2/v2.0/token"
    )
    send_url = f"https://graph.microsoft.com/v1.0/users/{quote(sender)}/sendMail"

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            token_resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
        except httpx.HTTPError as exc:
            raise AuthConfigError(
                f"graph token request failed (network error): {type(exc).__name__}"
            ) from exc
        if token_resp.status_code != 200:
            raise AuthConfigError(
                "graph token request failed: " + _graph_error_detail(token_resp)
            )
        access_token = (token_resp.json() or {}).get("access_token")
        if not access_token:
            raise AuthConfigError("graph token request returned no access_token")

        message = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": email}}],
            },
            "saveToSentItems": False,
        }
        try:
            send_resp = await client.post(
                send_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=message,
            )
        except httpx.HTTPError as exc:
            raise AuthConfigError(
                f"graph sendMail failed (network error): {type(exc).__name__}"
            ) from exc
        if send_resp.status_code != 202:
            raise AuthConfigError(
                "graph sendMail failed: " + _graph_error_detail(send_resp)
            )
    log.info("graph auth login email sent to %s (202)", email)


async def _send_login_email(email: str, code: str, link: str) -> str:
    mode = _delivery_mode()
    subject = "Your Kaidera OS sign-in code"
    body = (
        f"Your Kaidera OS sign-in code is: {code}\n\n"
        f"Or sign in with this link:\n{link}\n\n"
        "This code expires shortly. If you did not request it, ignore this email.\n"
    )
    if mode == "dev":
        log.info("dev auth code for %s: %s (%s)", email, code, link)
        return mode
    if mode == "log":
        log.info("auth login email for %s: %s", email, body.replace("\n", " | "))
        return mode
    if mode == "graph":
        await _send_login_email_graph(email, subject, code, link)
        return mode
    if mode != "smtp":
        raise AuthConfigError(f"unsupported KAIDERA_AUTH_EMAIL_DELIVERY={mode!r}")

    host = os.environ.get("KAIDERA_SMTP_HOST", "").strip()
    sender = os.environ.get("KAIDERA_SMTP_FROM", "").strip()
    if not host or not sender:
        raise AuthConfigError("KAIDERA_SMTP_HOST and KAIDERA_SMTP_FROM are required")
    port = int(os.environ.get("KAIDERA_SMTP_PORT", "587"))
    user = os.environ.get("KAIDERA_SMTP_USER", "").strip()
    password = os.environ.get("KAIDERA_SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = email
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        if os.environ.get("KAIDERA_SMTP_TLS", "1").strip().lower() not in {"0", "false", "no"}:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)
    return mode


@router.get("/login", response_class=HTMLResponse)
async def login_page(next: str | None = None) -> HTMLResponse:
    target = safe_next(next)
    return HTMLResponse(_login_html(target))


@router.post("/email/request")
async def request_email_login(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    email = normalize_email(payload.get("email"))
    if not valid_email(email):
        raise HTTPException(status_code=400, detail="valid_email_required")

    client_ip = _client_ip(request) or "unknown"
    # Rate-limit (opt-in via KAIDERA_AUTH_RATE_LIMIT=1): 5 requests per 10 minutes per email and per IP.
    if _rate_limit_enabled():
        if not await _auth_rate_limiter.check("email_request", email):
            await store.audit("auth.login_rate_limited", email=email, request=request,
                              detail={"bucket": "email", "ip": client_ip})
            raise HTTPException(status_code=429, detail="rate_limited")
        if not await _auth_rate_limiter.check("email_request_ip", client_ip):
            await store.audit("auth.login_rate_limited", email=email, request=request,
                              detail={"bucket": "ip", "ip": client_ip})
            raise HTTPException(status_code=429, detail="rate_limited")

    try:
        user = await _login_user_for_email(store, email)
        if not user:
            await store.audit("auth.login_request_ignored", email=email, request=request)
            return {"ok": True, "sent": True, "delivery": "none"}

        code = "".join(secrets.choice("0123456789") for _ in range(6))
        token = secrets.token_urlsafe(32)
        expires_at = _now() + timedelta(
            minutes=_minutes_env("KAIDERA_AUTH_EMAIL_CODE_TTL_MINUTES", 10)
        )
        await store.save_email_challenge(
            {
                "id": f"echal_{uuid.uuid4().hex}",
                "email": email,
                "user_id": user.get("id"),
                "purpose": "login",
                "code_hash": _hash_secret(code, "email-code"),
                "token_hash": _hash_secret(token, "email-token"),
                "expires_at": expires_at,
                "requested_ip": _client_ip(request),
                "user_agent": _user_agent(request),
            }
        )
        link = f"{_public_base_url(request)}/auth/email/consume?token={quote(token)}"
        delivery = await _send_login_email(email, code, link)
        await store.audit("auth.login_code_sent", user_id=user.get("id"), email=email, request=request)
    except AuthConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_store_unavailable") from exc

    out: dict[str, Any] = {"ok": True, "sent": True, "delivery": delivery}
    if delivery == "dev":
        out.update({"dev_code": code, "dev_link": link})
    return out


@router.post("/email/verify")
async def verify_email_login(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: Any = Depends(get_auth_store),
) -> JSONResponse:
    email = normalize_email(payload.get("email"))
    code = str(payload.get("code") or "").strip().replace(" ", "")
    if not valid_email(email) or not code:
        raise HTTPException(status_code=400, detail="email_and_code_required")
    try:
        challenge = await store.latest_email_challenge(email)
        if not challenge or int(challenge.get("attempts") or 0) >= 5:
            raise HTTPException(status_code=401, detail="invalid_or_expired_code")
        if not hmac.compare_digest(
            str(challenge.get("code_hash") or ""),
            _hash_secret(code, "email-code"),
        ):
            await store.increment_email_attempts(str(challenge["id"]))
            await store.audit("auth.login_code_failed", email=email, request=request)
            raise HTTPException(status_code=401, detail="invalid_or_expired_code")
        if challenge.get("user_id"):
            user = await store.get_user_by_id(str(challenge["user_id"]))
        elif await store.count_users() == 0:
            if not _check_bootstrap_token(payload):
                raise HTTPException(status_code=401, detail="bootstrap_token_required")
            user = await store.create_user(str(challenge["email"]), role="admin", verified=True)
        else:
            user = None
        if not user or user.get("status") != "active":
            raise HTTPException(status_code=401, detail="invalid_or_expired_code")
        await store.consume_email_challenge(str(challenge["id"]))
        issue = await _issue_session(store, user, request)
        await store.audit("auth.login", user_id=user.get("id"), email=email, request=request)
    except AuthConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_store_unavailable") from exc

    response = JSONResponse({"ok": True, "user": user_payload(issue.user)})
    set_session_cookie(response, issue, request)
    return response


@router.get("/email/consume", response_class=HTMLResponse)
async def consume_email_link_page(
    request: Request,
    token: str,
    next: str | None = None,
) -> HTMLResponse:
    # Render a confirm page ONLY — deliberately do NOT touch the store here. Email security scanners
    # (Microsoft 365 Safe Links / Defender ATP, Proofpoint, Mimecast, Gmail) PRE-FETCH every URL in an
    # inbound email to scan it for malware. A one-time magic link is then CONSUMED by that bot's GET
    # before the human ever clicks — the exact reported symptom "the code works but the link does not"
    # (the code is not a URL, so nothing pre-fetches it). Only the POST below — fired by a real human
    # click — consumes the token + issues the session; a scanner GETs this page and stops. (Confirmed
    # live: the delivered link was rewritten to *.safelinks.protection.outlook.com, whose scanner spent
    # the token on delivery.)
    return HTMLResponse(_consume_html(token, safe_next(next)))


@router.post("/email/consume")
async def consume_email_link(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: Any = Depends(get_auth_store),
) -> JSONResponse:
    token = str(payload.get("token") or "")
    next_url = safe_next(payload.get("next"))
    if not token:
        raise HTTPException(status_code=400, detail="token_required")
    try:
        challenge = await store.email_challenge_by_token_hash(_hash_secret(token, "email-token"))
        if not challenge:
            raise HTTPException(status_code=401, detail="invalid_or_expired_link")
        if challenge.get("user_id"):
            user = await store.get_user_by_id(str(challenge["user_id"]))
        elif await store.count_users() == 0:
            if not _check_bootstrap_token(payload):
                raise HTTPException(status_code=401, detail="bootstrap_token_required")
            user = await store.create_user(str(challenge["email"]), role="admin", verified=True)
        else:
            user = None
        if not user or user.get("status") != "active":
            raise HTTPException(status_code=401, detail="invalid_or_expired_link")
        await store.consume_email_challenge(str(challenge["id"]))
        issue = await _issue_session(store, user, request)
        await store.audit("auth.login", user_id=user.get("id"), email=user.get("email"), request=request)
    except HTTPException:
        raise
    except AuthConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_store_unavailable") from exc
    except Exception:
        log.exception("email-link login failed")
        raise HTTPException(status_code=401, detail="invalid_or_expired_link")

    response = JSONResponse({"ok": True, "next": next_url, "user": user_payload(issue.user)})
    set_session_cookie(response, issue, request)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    store: Any = Depends(get_auth_store),
) -> JSONResponse:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            await store.revoke_session(_hash_secret(token, "session"))
        except Exception:
            log.debug("logout session revoke failed", exc_info=True)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response, request)
    return response


@router.get("/session")
async def session(request: Request) -> dict[str, Any]:
    return user_payload(await current_user_from_request(request))


@router.get("/profile")
async def profile(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return user_payload(user)


@router.get("/users")
async def list_users(
    admin: dict[str, Any] = Depends(require_admin),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    return {"users": [user_payload(u) for u in await store.list_users()]}


@router.post("/users")
async def create_user(
    payload: dict[str, Any] = Body(default_factory=dict),
    admin: dict[str, Any] = Depends(require_admin),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    email = normalize_email(payload.get("email"))
    if not valid_email(email):
        raise HTTPException(status_code=400, detail="valid_email_required")
    role = str(payload.get("role") or "user").strip().lower()
    if role not in {"admin", "user"}:
        raise HTTPException(status_code=400, detail="invalid_role")
    existing = await store.get_user_by_email(email)
    if existing:
        return {"ok": True, "user": user_payload(existing)}
    try:
        from app import license as lic_mod

        limit = lic_mod.entitlements().limit_for("users")
        current = await store.count_users()
        if limit != math.inf and current >= int(limit):
            raise HTTPException(status_code=403, detail="license_user_limit_reached")
    except HTTPException:
        raise
    except Exception:
        # Licensing is a fail-closed surface: if the user cap cannot be resolved, only
        # the existing accounts remain usable until license/auth storage recovers.
        raise HTTPException(status_code=403, detail="license_user_limit_unavailable")
    user = await store.create_user(
        email,
        role=role,
        display_name=str(payload.get("display_name") or "").strip() or None,
        verified=False,
    )
    return {"ok": True, "user": user_payload(user)}


def _is_unique_violation(exc: Exception) -> bool:
    """True for a DB unique-constraint error, without importing asyncpg here.

    The profile/email update can collide with another user's email; asyncpg raises a
    `UniqueViolationError` (SQLSTATE 23505). Detect it structurally so the endpoint can
    return a clean 409 instead of a 500.
    """
    if exc.__class__.__name__ == "UniqueViolationError":
        return True
    return getattr(exc, "sqlstate", None) == "23505"


async def _would_lose_last_admin(
    store: Any,
    target: dict[str, Any],
    *,
    becoming_non_admin: bool,
) -> bool:
    """GUARD (b): block any change that would leave ZERO active admins.

    `becoming_non_admin` is True when the operation demotes / blocks / deletes the target.
    It only bites when the target is CURRENTLY an active admin AND it is the only one left.
    """
    if not becoming_non_admin:
        return False
    if target.get("role") != "admin" or target.get("status") != "active":
        return False
    active_admins = await store.count_active_admins()
    return active_admins <= 1


@router.patch("/users/{user_id}")
async def update_user(
    request: Request,
    user_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    admin: dict[str, Any] = Depends(require_admin),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    """Admin: change a user's role and/or status. Body: {role?, status?}.

    Guards the last-active-admin lockout (a demote OR a block of the only admin is refused).
    """
    target = await store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user_not_found")

    role = payload.get("role")
    status = payload.get("status")
    if role is None and status is None:
        raise HTTPException(status_code=400, detail="role_or_status_required")

    try:
        updated = target
        if role is not None:
            role = str(role).strip().lower()
            if role not in {"admin", "user"}:
                raise HTTPException(status_code=400, detail="invalid_role")
            if await _would_lose_last_admin(
                store, target, becoming_non_admin=(role != "admin")
            ):
                raise HTTPException(status_code=409, detail="cannot_demote_last_admin")
            updated = await store.set_user_role(user_id, role) or updated
        if status is not None:
            status = str(status).strip().lower()
            # Accept the UI-friendly "blocked" as an alias for the schema's "disabled" (the
            # auth_users CHECK constraint allows only 'active'/'disabled'). Both map to disabled.
            if status == "blocked":
                status = "disabled"
            if status not in {"active", "disabled"}:
                raise HTTPException(status_code=400, detail="invalid_status")
            # Re-read role from the post-role-change row so a same-call demote+block is judged
            # on the FINAL role, and so the admin count reflects any role change just applied.
            if await _would_lose_last_admin(
                store, updated, becoming_non_admin=(status != "active")
            ):
                raise HTTPException(status_code=409, detail="cannot_block_last_admin")
            updated = await store.set_user_status(user_id, status) or updated
    except HTTPException:
        raise
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_store_unavailable") from exc

    await store.audit(
        "auth.user_updated",
        user_id=user_id,
        request=request,
        detail={"role": role, "status": status, "by": admin.get("id")},
    )
    return {"ok": True, "user": user_payload(updated)}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_admin),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    """Admin: delete a user. Refuses to delete the last active admin (lockout guard)."""
    target = await store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user_not_found")
    if await _would_lose_last_admin(store, target, becoming_non_admin=True):
        raise HTTPException(status_code=409, detail="cannot_delete_last_admin")
    try:
        removed = await store.delete_user(user_id)
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_store_unavailable") from exc
    if not removed:
        raise HTTPException(status_code=404, detail="user_not_found")
    return {"ok": True, "removed": True, "id": user_id}


@router.patch("/profile")
async def update_profile(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_user),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    """The CURRENT signed-in user edits their own email + display_name."""
    email_in = payload.get("email")
    name_in = payload.get("display_name")
    email: str | None = None
    if email_in is not None:
        email = normalize_email(email_in)
        if not valid_email(email):
            raise HTTPException(status_code=400, detail="valid_email_required")
    display_name: str | None = None
    if name_in is not None:
        display_name = str(name_in).strip() or None
    if email is None and display_name is None:
        raise HTTPException(status_code=400, detail="email_or_display_name_required")
    try:
        updated = await store.update_user_profile(
            str(user["id"]), email=email, display_name=display_name
        )
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_store_unavailable") from exc
    except Exception as exc:  # noqa: BLE001 - map a unique-email collision to a clean 409
        if _is_unique_violation(exc):
            raise HTTPException(status_code=409, detail="email_already_in_use") from exc
        raise
    if not updated:
        raise HTTPException(status_code=404, detail="user_not_found")
    return {"ok": True, "user": user_payload(updated)}


def _webauthn_imports() -> dict[str, Any]:
    try:
        from webauthn import (  # type: ignore
            base64url_to_bytes,
            generate_authentication_options,
            generate_registration_options,
            options_to_json,
            verify_authentication_response,
            verify_registration_response,
        )
        from webauthn.helpers.structs import (  # type: ignore
            PublicKeyCredentialDescriptor,
            UserVerificationRequirement,
        )
    except Exception as exc:  # pragma: no cover - depends on optional wheel
        raise HTTPException(status_code=501, detail="webauthn_not_installed") from exc
    return {
        "base64url_to_bytes": base64url_to_bytes,
        "generate_authentication_options": generate_authentication_options,
        "generate_registration_options": generate_registration_options,
        "options_to_json": options_to_json,
        "verify_authentication_response": verify_authentication_response,
        "verify_registration_response": verify_registration_response,
        "PublicKeyCredentialDescriptor": PublicKeyCredentialDescriptor,
        "UserVerificationRequirement": UserVerificationRequirement,
    }


@router.post("/passkeys/register/options")
async def passkey_register_options(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    w = _webauthn_imports()
    existing = await store.list_passkeys(str(user["id"]))
    exclude = [
        w["PublicKeyCredentialDescriptor"](id=w["base64url_to_bytes"](pk["credential_id"]))
        for pk in existing
    ]
    options = w["generate_registration_options"](
        rp_id=_rp_id(request),
        rp_name=os.environ.get("KAIDERA_AUTH_RP_NAME", "Kaidera OS"),
        user_id=str(user["id"]).encode("utf-8"),
        user_name=str(user["email"]),
        user_display_name=str(user.get("display_name") or user["email"]),
        exclude_credentials=exclude,
    )
    await store.save_webauthn_challenge(
        str(user["id"]),
        "passkey_register",
        _b64(options.challenge),
        _now() + timedelta(minutes=10),
    )
    return json.loads(w["options_to_json"](options))


@router.post("/passkeys/register/verify")
async def passkey_register_verify(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_user),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    w = _webauthn_imports()
    challenge = await store.latest_webauthn_challenge(str(user["id"]), "passkey_register")
    if not challenge:
        raise HTTPException(status_code=401, detail="missing_or_expired_challenge")
    credential = payload.get("credential") or payload
    verified = w["verify_registration_response"](
        credential=credential,
        expected_challenge=w["base64url_to_bytes"](challenge["challenge"]),
        expected_rp_id=_rp_id(request),
        expected_origin=_origin(request),
        require_user_verification=False,
    )
    transports = []
    try:
        transports = list((credential.get("response") or {}).get("transports") or [])
    except Exception:
        transports = []
    await store.save_passkey(
        {
            "id": f"pkey_{uuid.uuid4().hex}",
            "user_id": str(user["id"]),
            "credential_id": _b64(verified.credential_id),
            "public_key": _b64(verified.credential_public_key),
            "sign_count": verified.sign_count,
            "transports": transports,
            "aaguid": getattr(verified, "aaguid", None),
            "nickname": str(payload.get("nickname") or "").strip() or None,
        }
    )
    await store.consume_webauthn_challenge(str(challenge["id"]))
    return {"ok": True}


@router.post("/passkeys/authenticate/options")
async def passkey_authenticate_options(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: Any = Depends(get_auth_store),
) -> dict[str, Any]:
    w = _webauthn_imports()
    email = normalize_email(payload.get("email"))
    if not valid_email(email):
        raise HTTPException(status_code=400, detail="valid_email_required")
    user = await store.get_user_by_email(email)
    if not user or user.get("status") != "active":
        raise HTTPException(status_code=401, detail="unknown_user")
    passkeys = await store.list_passkeys(str(user["id"]))
    if not passkeys:
        raise HTTPException(status_code=404, detail="no_passkeys")
    allow = [
        w["PublicKeyCredentialDescriptor"](id=w["base64url_to_bytes"](pk["credential_id"]))
        for pk in passkeys
    ]
    options = w["generate_authentication_options"](
        rp_id=_rp_id(request),
        allow_credentials=allow,
        user_verification=w["UserVerificationRequirement"].PREFERRED,
    )
    await store.save_webauthn_challenge(
        str(user["id"]),
        "passkey_login",
        _b64(options.challenge),
        _now() + timedelta(minutes=10),
    )
    return json.loads(w["options_to_json"](options))


@router.post("/passkeys/authenticate/verify")
async def passkey_authenticate_verify(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: Any = Depends(get_auth_store),
) -> JSONResponse:
    w = _webauthn_imports()
    email = normalize_email(payload.get("email"))
    credential = payload.get("credential") or payload
    credential_id = str(credential.get("id") or credential.get("rawId") or "")
    if not valid_email(email) or not credential_id:
        raise HTTPException(status_code=400, detail="email_and_credential_required")
    user = await store.get_user_by_email(email)
    if not user or user.get("status") != "active":
        raise HTTPException(status_code=401, detail="unknown_user")
    challenge = await store.latest_webauthn_challenge(str(user["id"]), "passkey_login")
    passkey = await store.get_passkey_by_credential_id(credential_id)
    if not challenge or not passkey or passkey.get("user_id") != user.get("id"):
        raise HTTPException(status_code=401, detail="invalid_passkey")
    verified = w["verify_authentication_response"](
        credential=credential,
        expected_challenge=w["base64url_to_bytes"](challenge["challenge"]),
        expected_rp_id=_rp_id(request),
        expected_origin=_origin(request),
        credential_public_key=w["base64url_to_bytes"](passkey["public_key"]),
        credential_current_sign_count=int(passkey.get("sign_count") or 0),
        require_user_verification=False,
    )
    await store.update_passkey_sign_count(str(passkey["id"]), int(verified.new_sign_count))
    await store.consume_webauthn_challenge(str(challenge["id"]))
    issue = await _issue_session(store, user, request)
    response = JSONResponse({"ok": True, "user": user_payload(issue.user)})
    set_session_cookie(response, issue, request)
    return response


def _login_html(next_url: str) -> str:
    nxt = json.dumps(next_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kaidera OS sign in</title>
  <style>
    body {{ margin: 0; font-family: Inter, system-ui, sans-serif; background: #f7faf9; color: #10201d; }}
    main {{ min-height: 100vh; display: grid; place-items: center; padding: 24px; }}
    section {{ width: min(420px, 100%); border: 1px solid #dce7e2; background: white; border-radius: 8px; padding: 28px; box-shadow: 0 16px 40px rgba(16,32,29,.08); }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ margin: 0 0 18px; color: #5b6d67; line-height: 1.45; }}
    label {{ display: block; margin: 14px 0 6px; font-size: 13px; font-weight: 700; }}
    input {{ width: 100%; box-sizing: border-box; border: 1px solid #bdcbc6; border-radius: 6px; padding: 12px; font: inherit; }}
    button {{ margin-top: 16px; width: 100%; border: 0; border-radius: 6px; padding: 12px; background: #0f6b5b; color: white; font-weight: 700; cursor: pointer; }}
    button:disabled {{ opacity: .6; cursor: default; }}
    #codeStep {{ display: none; }}
    #msg {{ min-height: 22px; margin-top: 14px; font-size: 13px; color: #5b6d67; }}
    #dev {{ margin-top: 10px; padding: 10px; border-radius: 6px; background: #eef8f5; font-family: ui-monospace, SFMono-Regular, monospace; display: none; }}
  </style>
</head>
<body>
<main>
  <section>
    <h1>Kaidera OS sign in</h1>
    <p>Use an email code or link. Passkeys can be added after sign-in.</p>
    <div id="emailStep">
      <label for="email">Email</label>
      <input id="email" type="email" autocomplete="email" required>
      <button id="send" type="button">Send code</button>
    </div>
    <div id="codeStep">
      <label for="code">Code</label>
      <input id="code" inputmode="numeric" autocomplete="one-time-code" required>
      <button id="verify" type="button">Sign in</button>
    </div>
    <div id="msg"></div>
    <div id="dev"></div>
  </section>
</main>
<script>
const nextUrl = {nxt};
const email = document.getElementById('email');
const code = document.getElementById('code');
const msg = document.getElementById('msg');
const dev = document.getElementById('dev');
const send = document.getElementById('send');
const verify = document.getElementById('verify');
send.onclick = async () => {{
  send.disabled = true;
  msg.textContent = 'Sending...';
  const res = await fetch('/auth/email/request', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{email: email.value}})
  }});
  send.disabled = false;
  if (!res.ok) {{ msg.textContent = 'Enter a valid email address.'; return; }}
  const out = await res.json();
  document.getElementById('emailStep').style.display = 'none';
  document.getElementById('codeStep').style.display = 'block';
  msg.textContent = out.delivery === 'log'
    ? 'No email is configured — your sign-in code was written to the server log. On the host run:  journalctl -u kaidera-os-console | grep -i code'
    : 'Check your email for the code or sign-in link.';
  if (out.dev_code) {{
    dev.style.display = 'block';
    dev.textContent = 'Dev code: ' + out.dev_code;
  }}
}};
verify.onclick = async () => {{
  verify.disabled = true;
  msg.textContent = 'Signing in...';
  const res = await fetch('/auth/email/verify', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{email: email.value, code: code.value}})
  }});
  verify.disabled = false;
  if (!res.ok) {{ msg.textContent = 'That code is invalid or expired.'; return; }}
  location.href = nextUrl;
}};
</script>
</body>
</html>"""


def _consume_html(token: str, next_url: str) -> str:
    # Confirm page for the email magic LINK. The token + next are embedded as JSON (safe). On the
    # explicit button click the browser POSTs to /auth/email/consume (which consumes the token + sets
    # the session) and then navigates — mirroring the code path's fetch flow. An email scanner that
    # merely GETs this URL never runs the click handler, so the one-time token survives for the human.
    tok = json.dumps(token)
    nxt = json.dumps(next_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex">
  <title>Kaidera OS sign in</title>
  <style>
    body {{ margin: 0; font-family: Inter, system-ui, sans-serif; background: #f7faf9; color: #10201d; }}
    main {{ min-height: 100vh; display: grid; place-items: center; padding: 24px; }}
    section {{ width: min(420px, 100%); border: 1px solid #dce7e2; background: white; border-radius: 8px; padding: 28px; box-shadow: 0 16px 40px rgba(16,32,29,.08); text-align: center; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ margin: 0 0 18px; color: #5b6d67; line-height: 1.45; }}
    button {{ width: 100%; border: 0; border-radius: 6px; padding: 12px; background: #0f6b5b; color: white; font-weight: 700; cursor: pointer; font: inherit; }}
    button:disabled {{ opacity: .6; cursor: default; }}
    #msg {{ min-height: 22px; margin-top: 14px; font-size: 13px; color: #5b6d67; }}
  </style>
</head>
<body>
<main>
  <section>
    <h1>Confirm sign-in</h1>
    <p>Tap continue to finish signing in to Kaidera OS. This one tap confirms it&rsquo;s you, not an automated email scanner.</p>
    <button id="go" type="button">Continue to sign in</button>
    <div id="msg"></div>
  </section>
</main>
<script>
const token = {tok};
const nextUrl = {nxt};
const go = document.getElementById('go');
const msg = document.getElementById('msg');
go.onclick = async () => {{
  go.disabled = true;
  msg.textContent = 'Signing you in…';
  let res;
  try {{
    res = await fetch('/auth/email/consume', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{token: token, next: nextUrl}})
    }});
  }} catch (e) {{ go.disabled = false; msg.textContent = 'Network error — please try again.'; return; }}
  if (!res.ok) {{
    msg.textContent = 'This sign-in link has expired — request a new code from the login page.';
    setTimeout(() => {{ location.href = '/auth/login'; }}, 1800);
    return;
  }}
  const out = await res.json();
  location.href = out.next || '/app/';
}};
</script>
</body>
</html>"""
