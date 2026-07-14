"""Kaidera AI platform endpoint configuration.

The rebrand does not establish a replacement production hostname. Runtime code
therefore resolves platform endpoints only from explicit configuration instead
of embedding or guessing a domain.
"""

from __future__ import annotations

import os


PLATFORM_URL_ENV = "KAIDERA_OS_PLATFORM_URL"
PORTAL_URL_ENV = "KAIDERA_OS_PORTAL_URL"
MANIFOLD_BASE_URL_ENV = "KAIDERA_MANIFOLD_BASE_URL"


def _url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def platform_url(value: str | None = None) -> str:
    """Return the configured Kaidera AI platform origin, without a trailing slash."""
    return _url(value) or _url(os.environ.get(PLATFORM_URL_ENV))


def portal_url(value: str | None = None) -> str:
    """Return the configured customer portal origin, falling back to the platform."""
    return _url(value) or _url(os.environ.get(PORTAL_URL_ENV)) or platform_url()


def manifold_base_url(value: str | None = None) -> str:
    """Return the configured Manifold API base, deriving ``/v1`` from the platform."""
    configured = _url(value) or _url(os.environ.get(MANIFOLD_BASE_URL_ENV))
    if configured:
        return configured
    origin = platform_url()
    return f"{origin}/v1" if origin else ""


__all__ = [
    "MANIFOLD_BASE_URL_ENV",
    "PLATFORM_URL_ENV",
    "PORTAL_URL_ENV",
    "manifold_base_url",
    "platform_url",
    "portal_url",
]
