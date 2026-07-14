"""Deferred security batch for first-party auth (v0.1.141+ follow-on).

Covers:
- trusted-proxy header gating for /whoami (X-Forwarded-* / X-Kaidera-*);
- first-admin bootstrap token hardening;
- simple in-memory rate limiting on email-code requests.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import Request

from app import auth


class _FakeRequest:
    """Minimal Request stand-in for helpers that only read headers/client."""

    def __init__(self, headers: dict[str, str] | None = None, client_host: str | None = "127.0.0.1") -> None:
        self.headers = headers or {}
        self.client = MagicMock(host=client_host) if client_host else None
        self.cookies = {}


# ── trusted proxy headers ──────────────────────────────────────────────────

@pytest.mark.parametrize("env_value,expected", [
    ("", False),
    ("0", False),
    ("false", False),
    ("no", False),
    ("off", False),
    ("1", True),
    ("true", True),
    ("yes", True),
    ("on", True),
    ("10.0.0.0/8", True),  # non-boolean treated as explicit opt-in for now
])
def test_trusted_proxy_headers_env(monkeypatch, env_value, expected):
    monkeypatch.setenv("KAIDERA_AUTH_TRUSTED_PROXY", env_value)
    req = _FakeRequest()
    assert auth.trusted_proxy_headers(req) is expected


def test_trusted_proxy_headers_default_off(monkeypatch):
    monkeypatch.delenv("KAIDERA_AUTH_TRUSTED_PROXY", raising=False)
    req = _FakeRequest()
    assert auth.trusted_proxy_headers(req) is False


def test_extension_ingress_paths_are_public_when_registered():
    """Middleware delegates extension ingress allowlists to extension-owned matchers."""
    auth.clear_public_path_matchers()
    auth.register_public_path_matcher(lambda path: path.startswith("/extensions/example/ingress/"))
    try:
        assert auth.is_public_path("/extensions/example/ingress/event")
        assert not auth.is_public_path("/extensions/example/admin")
    finally:
        auth.clear_public_path_matchers()


@pytest.mark.asyncio
async def test_whoami_ignores_forwarded_headers_without_trusted_proxy(monkeypatch):
    """The security fix: X-Forwarded-* must NOT authenticate unless a trusted proxy is declared."""
    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    monkeypatch.delenv("KAIDERA_AUTH_TRUSTED_PROXY", raising=False)
    req = _FakeRequest(headers={
        "X-Forwarded-Preferred-Username": "attacker",
        "X-Forwarded-Email": "attacker@example.com",
        "X-Forwarded-Groups": "admin",
    })
    # No session; headers ignored; auth is enabled → unauthenticated default.
    assert await _whoami_logic(req) == {
        "authenticated": False,
        "name": "User",
        "email": "",
        "is_admin": False,
        "role": "user",
    }


# Helper mirror of main.py /whoami logic for unit testing without importing main.py.
async def _whoami_logic(request: Request) -> dict:
    user = await auth.current_user_from_request(request)
    if user:
        return auth.user_payload(user)
    h = request.headers
    if auth.trusted_proxy_headers(request):
        name = (
            h.get("X-Kaidera-Name")
            or h.get("X-Forwarded-Preferred-Username")
            or h.get("X-Forwarded-User")
            or h.get("X-Forwarded-Email")
            or ""
        ).strip()
        email = (h.get("X-Kaidera-Email") or h.get("X-Forwarded-Email") or "").strip()
        groups = h.get("X-Kaidera-Groups") or h.get("X-Forwarded-Groups") or ""
        if name or email or groups.strip():
            is_admin = "admin" in {g.strip().lower() for g in groups.split(",")}
            return {
                "authenticated": True,
                "name": name or email or "User",
                "email": email,
                "is_admin": is_admin,
                "role": "admin" if is_admin else "user",
            }
    return {
        "authenticated": not auth.auth_enabled(),
        "name": "User",
        "email": "",
        "is_admin": not auth.auth_enabled(),
        "role": "admin" if not auth.auth_enabled() else "user",
    }


@pytest.mark.asyncio
async def test_whoami_honors_forwarded_headers_when_trusted_proxy_enabled(monkeypatch):
    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    monkeypatch.setenv("KAIDERA_AUTH_TRUSTED_PROXY", "1")
    req = _FakeRequest(headers={
        "X-Forwarded-Preferred-Username": "admin",
        "X-Forwarded-Email": "admin@example.com",
        "X-Forwarded-Groups": "admin",
    })
    payload = await _whoami_logic(req)
    assert payload["authenticated"] is True
    assert payload["email"] == "admin@example.com"
    assert payload["is_admin"] is True


# ── bootstrap token ────────────────────────────────────────────────────────

@pytest.mark.parametrize("configured,supplied,auth_on,expected", [
    (None, "", True, True),       # no token configured → allowed
    ("sekrit", "sekrit", True, True),
    ("sekrit", "wrong", True, False),
    ("sekrit", "", True, False),
    ("sekrit", "wrong", False, True),  # auth disabled → allowed regardless
])
def test_check_bootstrap_token(monkeypatch, configured, supplied, auth_on, expected):
    monkeypatch.setattr(auth, "auth_enabled", lambda: auth_on)
    if configured is not None:
        monkeypatch.setenv("KAIDERA_AUTH_BOOTSTRAP_TOKEN", configured)
    else:
        monkeypatch.delenv("KAIDERA_AUTH_BOOTSTRAP_TOKEN", raising=False)
    assert auth._check_bootstrap_token({"bootstrap_token": supplied}) is expected


# ── rate limiter ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limiter_allows_under_limit():
    limiter = auth._AuthRateLimiter(default_window_seconds=60, default_max_requests=3)
    for _ in range(3):
        assert await limiter.check("test", "key") is True


@pytest.mark.asyncio
async def test_rate_limiter_blocks_over_limit():
    limiter = auth._AuthRateLimiter(default_window_seconds=60, default_max_requests=2)
    assert await limiter.check("test", "key") is True
    assert await limiter.check("test", "key") is True
    assert await limiter.check("test", "key") is False


@pytest.mark.asyncio
async def test_rate_limiter_keys_are_isolated():
    limiter = auth._AuthRateLimiter(default_window_seconds=60, default_max_requests=1)
    assert await limiter.check("test", "a") is True
    assert await limiter.check("test", "b") is True


@pytest.mark.asyncio
async def test_rate_limiter_window_expires():
    limiter = auth._AuthRateLimiter(default_window_seconds=0, default_max_requests=1)
    assert await limiter.check("test", "key") is True
    # Sleep long enough that the 0-second window has elapsed.
    await asyncio.sleep(0.05)
    assert await limiter.check("test", "key") is True
