"""Small HTTP client for Beat's local Cortex API calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class CortexAPIError(RuntimeError):
    """Raised when cortex-api returns an error response."""


class CortexAPI:
    """Synchronous JSON client with the same local JWT boundary as CLI tools."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        project: str | None = None,
        default_agent: str | None = None,
        admin_token: str | None = None,
        customer: str | None = None,
        jwt_secret: str | None = None,
        timeout: int = 60,
    ):
        self.base_url = (
            base_url
            or os.environ.get("CORTEX_API_URL")
            or os.environ.get("CORTEX_API_BASE")
            or os.environ.get("CORTEX_API")
            or "http://localhost:8501"
        ).rstrip("/")
        self.project = project or os.environ.get("CORTEX_PROJECT", "kaidera")  # fitness:allow-literal env-overridable dogfood default
        self.default_agent = (
            default_agent
            or os.environ.get("BEAT_CORTEX_AGENT")
            or "beat"
        )
        self.admin_token = (
            admin_token if admin_token is not None else os.environ.get("CORTEX_ADMIN_TOKEN", "")
        )
        self.customer = customer or os.environ.get("CORTEX_CUSTOMER", "local")
        self.jwt_secret = (
            jwt_secret if jwt_secret is not None else os.environ.get("CORTEX_JWT_SECRET", "")
        )
        self.timeout = timeout

    @staticmethod
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    def _jwt(self, agent: str) -> str:
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "iss": "local-cortex",
            "aud": "cortex-api",
            "sub": agent,
            "customer": self.customer,
            "project": self.project,
            "iat": now,
            "exp": now + 3600,
        }
        segments = [
            self._b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            self._b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
        signing_input = ".".join(segments).encode("ascii")
        signature = hmac.new(
            self.jwt_secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        return ".".join(segments + [self._b64url(signature)])

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        agent = (agent or self.default_agent).lower().strip()
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params, doseq=True)
        url = f"{self.base_url}{path}{query}"

        data = None
        headers = {
            "Accept": "application/json",
            "X-Project": self.project,
            "X-Agent-Name": agent,
        }
        if self.admin_token:
            headers["X-Cortex-Admin-Token"] = self.admin_token
        if self.jwt_secret:
            headers["Authorization"] = f"Bearer {self._jwt(agent)}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CortexAPIError(
                f"{method.upper()} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise CortexAPIError(f"{method.upper()} {path} failed: {exc}") from exc

        if not body:
            return {}
        return json.loads(body)
