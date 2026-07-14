"""Codex subscription (OpenAI/ChatGPT) login helpers.

Inc 4b of the self-contained redesign (`docs/2026-06-13-codex-oauth-design.md`). Lets the
**kaidera kaidera** use the ChatGPT codex subscription directly: the app runs the headless
**device-code** OAuth flow, stores the token bundle in the **app-DB** (`app_settings` key
`codex_oauth`, app-owned — never `~/.pi`), and hands the access token to the harness as a Bearer.

SPLIT OF CONFIDENCE (be honest):
  * STORAGE + expiry + bearer resolution below are exact + unit-tested.
  * The OAuth HTTP shapes (device-code endpoints, PKCE exchange, refresh, the id_token account
    claim) MIRROR the public `openai/codex` CLI (`codex-rs/login/src/device_code_auth.rs`) but are
    an UNDOCUMENTED internal surface — they need a live verification pass against a real codex
    subscription token before they can be relied on. Each such function is marked `LIVE-UNVERIFIED`.
  * Current supported operator login is the Codex CLI device flow (`codex login --device-auth`),
    because the old direct `auth.openai.com/deviceauth/usercode` endpoint now returns 403.

Self-contained note: no `deploy_mode` gate is needed here — this path is ALWAYS app-DB-first (it has
no host fallback to gate), which is exactly the self-contained guarantee.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import appdb as _appdb

_db = _appdb.settings_db
_UNAVAILABLE = _appdb.UNAVAILABLE

# -- Resolved constants (public in openai/codex; see the design doc) -----------------------------
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
DEVICE_USERCODE_URL = f"{AUTH_BASE_URL}/deviceauth/usercode"  # POST {client_id} -> user code
DEVICE_TOKEN_URL = f"{AUTH_BASE_URL}/deviceauth/token"        # POST {device_auth_id, user_code}
OAUTH_TOKEN_URL = f"{AUTH_BASE_URL}/oauth/token"              # PKCE exchange + refresh_token grant

#: app_settings key holding the OAuth token bundle (added to settings.PROVIDER_SECRET_KEYS).
CODEX_OAUTH_KEY = "codex_oauth"
#: refresh the access token when it's within this many seconds of expiry.
REFRESH_MARGIN_S = 300
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
CODEX_CLI_DEVICE_URL = "https://auth.openai.com/codex/device"
_CODEX_LOGIN_TIMEOUT_S = 10.0
_CODEX_STATUS_TIMEOUT_S = 8.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_DEVICE_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device")
_DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})+\b")
_LOGIN_FLOWS: dict[str, dict[str, Any]] = {}


# ============================== storage (exact, tested) =========================================

def load_codex_oauth_blob() -> dict[str, Any] | None:
    """The stored `{access_token, refresh_token, expires_at, chatgpt_account_id, ...}` bundle, or
    None when absent / the app-DB is unavailable."""
    m = _db.load_app_settings()
    if m is _UNAVAILABLE or not isinstance(m, dict):
        return None
    v = m.get(CODEX_OAUTH_KEY)
    return v if isinstance(v, dict) else None


def save_codex_oauth_blob(blob: dict[str, Any]) -> bool:
    """Persist the token bundle to the app-DB (JSONB). False when the DB can't answer."""
    return bool(_db.upsert_app_settings({CODEX_OAUTH_KEY: dict(blob)}))


def clear_codex_oauth_blob() -> bool:
    """Log out — delete the stored bundle."""
    return bool(_db.delete_app_setting(CODEX_OAUTH_KEY))


# ============================== pure logic (exact, tested) ======================================

def _now() -> float:
    return time.time()


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _bounded_tail(lines: list[str], line: str, *, cap: int = 40) -> None:
    s = _strip_ansi(line).strip()
    if s:
        lines.append(s)
        del lines[:-cap]


def _codex_cli_path() -> str:
    """Best-effort path to the Codex CLI.

    Launchd/packaged app environments often have a much thinner PATH than an
    interactive shell, so check the common install locations as well as PATH.
    """
    candidates = [
        shutil.which("codex"),
        str(Path.home() / ".npm-global" / "bin" / "codex"),
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def _codex_child_env() -> dict[str, str]:
    """Environment for Codex CLI subprocesses launched from a packaged app.

    The macOS LaunchAgent runner may have a minimal PATH, while the npm Codex
    shim uses `#!/usr/bin/env node`. Include the Codex bin directory and common
    Node install directories so device login works outside an interactive shell.
    """
    env = os.environ.copy()
    dirs: list[str] = []
    exe = _codex_cli_path()
    if exe:
        dirs.append(str(Path(exe).parent))
    candidates = [
        Path.home() / ".npm-global" / "bin",
        Path.home() / ".volta" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        candidates.extend(sorted(nvm_root.glob("v*/bin"), reverse=True))
    dirs.extend(str(p) for p in candidates if p.exists())
    dirs.extend(p for p in (env.get("PATH") or "").split(":") if p)
    env["PATH"] = ":".join(dict.fromkeys(dirs))
    return env


def _parse_codex_device_output(text: str) -> tuple[str, str]:
    clean = _strip_ansi(text)
    url = CODEX_CLI_DEVICE_URL if _DEVICE_URL_RE.search(clean) else ""
    code_match = _DEVICE_CODE_RE.search(clean)
    return url, code_match.group(0) if code_match else ""


def codex_cli_status() -> dict[str, Any]:
    """Return current Codex CLI login state without exposing credentials."""
    exe = _codex_cli_path()
    if not exe:
        return {
            "available": False,
            "logged_in": False,
            "auth_method": "",
            "message": "codex CLI not found on PATH.",
        }
    try:
        res = subprocess.run(
            [exe, "login", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_CODEX_STATUS_TIMEOUT_S,
            check=False,
            env=_codex_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": True, "logged_in": False, "auth_method": "", "message": str(exc)}

    msg = _strip_ansi(res.stdout or "").strip()
    low = msg.lower()
    method = "chatgpt" if "chatgpt" in low else ("api_key" if "api key" in low or "api-key" in low else "")
    return {
        "available": True,
        "logged_in": res.returncode == 0,
        "auth_method": method,
        "message": msg,
    }


def logout_codex_cli() -> dict[str, Any]:
    """Run `codex logout` when the CLI is available; never exposes credential material."""
    exe = _codex_cli_path()
    if not exe:
        return {"available": False, "ok": True, "message": "codex CLI not found."}
    try:
        res = subprocess.run(
            [exe, "logout"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_CODEX_STATUS_TIMEOUT_S,
            check=False,
            env=_codex_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": True, "ok": False, "message": str(exc)}
    return {
        "available": True,
        "ok": res.returncode == 0,
        "message": _strip_ansi(res.stdout or "").strip(),
    }


def is_logged_in() -> bool:
    """True when a usable bundle exists (has a non-empty access_token)."""
    blob = load_codex_oauth_blob()
    return bool(blob and str(blob.get("access_token") or "").strip())


def account_id(blob: dict[str, Any] | None = None) -> str:
    """The `chatgpt-account-id` header value (stored at login from the id_token)."""
    blob = blob if blob is not None else load_codex_oauth_blob()
    return str((blob or {}).get("chatgpt_account_id") or "").strip()


def needs_refresh(blob: dict[str, Any], margin_s: int = REFRESH_MARGIN_S) -> bool:
    """True when the access token is missing an expiry or within `margin_s` of expiring."""
    try:
        exp = float(blob.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        return True
    return exp <= 0.0 or (exp - _now()) < margin_s


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def account_id_from_id_token(id_token: str) -> str:
    """Best-effort: pull the ChatGPT account id from the id_token JWT claims. The exact claim path
    is part of the LIVE-UNVERIFIED surface — we probe the known shapes openai/codex uses."""
    try:
        payload = json.loads(_b64url_decode(id_token.split(".")[1]))
    except (ValueError, IndexError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    # direct claims first, then the namespaced `https://api.openai.com/auth` object.
    for k in ("chatgpt_account_id", "account_id"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        for k in ("chatgpt_account_id", "account_id"):
            v = auth.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _blob_from_token_response(data: dict[str, Any], *, keep: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shape a token endpoint response into the stored bundle. `keep` carries forward fields a
    refresh response may omit (e.g. refresh_token, chatgpt_account_id)."""
    keep = keep or {}
    try:
        ttl = float(data.get("expires_in") or 0.0)
    except (TypeError, ValueError):
        ttl = 0.0
    id_token = str(data.get("id_token") or "")
    acct = account_id_from_id_token(id_token) if id_token else str(keep.get("chatgpt_account_id") or "")
    return {
        "access_token": str(data.get("access_token") or ""),
        "refresh_token": str(data.get("refresh_token") or keep.get("refresh_token") or ""),
        "expires_at": (_now() + ttl) if ttl > 0 else 0.0,
        "token_type": str(data.get("token_type") or "Bearer"),
        "scope": str(data.get("scope") or keep.get("scope") or ""),
        "chatgpt_account_id": acct or str(keep.get("chatgpt_account_id") or ""),
        "acquired_at": _now(),
    }


# ============================== async bearer (tested via stubbed refresh) ========================

async def get_codex_oauth_bearer(cfg: dict[str, Any] | None = None) -> str:
    """Return a usable access token for the codex-subscription harness lane, refreshing first when
    it's near expiry. Empty string when not logged in. MUST be awaited from the async harness path —
    refresh is an async HTTP call, so this can't live in the sync `_own_provider_key` resolver."""
    blob = load_codex_oauth_blob()
    if not blob or not str(blob.get("access_token") or "").strip():
        return ""
    if needs_refresh(blob) and str(blob.get("refresh_token") or "").strip():
        refreshed = await refresh_codex_oauth_token(blob)
        if refreshed:
            blob = refreshed
    return str(blob.get("access_token") or "").strip()


# ============================== Codex CLI device login (supported path) ==========================

async def _watch_login_flow(flow_id: str) -> None:
    flow = _LOGIN_FLOWS.get(flow_id)
    if not flow:
        return
    proc: asyncio.subprocess.Process = flow["proc"]
    out = proc.stdout
    if out is not None:
        while True:
            raw = await out.readline()
            if not raw:
                break
            _bounded_tail(flow["tail"], raw.decode("utf-8", "replace"))
    flow["returncode"] = await proc.wait()
    flow["finished_at"] = _now()


def _cleanup_login_flows() -> None:
    cutoff = _now() - 1800
    for flow_id, flow in list(_LOGIN_FLOWS.items()):
        if flow.get("finished_at") and float(flow["finished_at"]) < cutoff:
            _LOGIN_FLOWS.pop(flow_id, None)


async def start_cli_device_flow() -> dict[str, Any]:
    """Start the supported `codex login --device-auth` flow and keep it alive.

    The Codex CLI owns the current device-auth implementation and stores the
    resulting ChatGPT credentials in the Codex auth cache/keychain. We parse only
    the public operator instructions (URL + one-time code) and keep the process
    running so it can poll until the user authorizes in the browser.
    """
    _cleanup_login_flows()
    exe = _codex_cli_path()
    if not exe:
        raise RuntimeError("codex CLI not found; install Codex CLI, then retry login.")

    proc = await asyncio.create_subprocess_exec(
        exe,
        "login",
        "--device-auth",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_codex_child_env(),
    )
    assert proc.stdout is not None

    tail: list[str] = []
    captured = ""
    deadline = _now() + _CODEX_LOGIN_TIMEOUT_S
    url = code = ""
    while _now() < deadline:
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=max(0.1, deadline - _now()))
        except asyncio.TimeoutError:
            break
        if not raw:
            break
        line = raw.decode("utf-8", "replace")
        captured += line
        _bounded_tail(tail, line)
        url, code = _parse_codex_device_output(captured)
        if url and code:
            flow_id = str(uuid.uuid4())
            _LOGIN_FLOWS[flow_id] = {
                "proc": proc,
                "tail": tail,
                "returncode": None,
                "started_at": _now(),
                "finished_at": None,
                "user_code": code,
                "verification_uri": url,
            }
            asyncio.create_task(_watch_login_flow(flow_id))
            return {
                "device_auth_id": flow_id,
                "user_code": code,
                "verification_uri": url,
                "interval": 2,
                "expires_in": 900,
                "method": "codex_cli",
            }

    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    detail = " ".join(tail)[-400:] if tail else "no output"
    raise RuntimeError(f"codex login --device-auth did not produce a device code: {detail}")


# ============================== OAuth HTTP (LIVE-UNVERIFIED) =====================================
# These mirror openai/codex `codex-rs/login/src/device_code_auth.rs`. The endpoints are correct;
# the exact request/response field names + the PKCE exchange are an undocumented internal surface
# and MUST be confirmed against a live codex subscription before this is wired to a real login.

async def refresh_codex_oauth_token(blob: dict[str, Any]) -> dict[str, Any] | None:
    """LIVE-UNVERIFIED. Exchange the refresh_token for a fresh access token + persist it."""
    refresh = str(blob.get("refresh_token") or "").strip()
    if not refresh:
        return None
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                OAUTH_TOKEN_URL,
                json={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT_ID},
            )
        if resp.status_code != 200:
            return None
        new_blob = _blob_from_token_response(resp.json(), keep=blob)
    except (httpx.HTTPError, ValueError):
        return None
    if not new_blob.get("access_token"):
        return None
    save_codex_oauth_blob(new_blob)
    return new_blob


async def start_device_flow() -> dict[str, Any]:
    """Begin the user-facing Codex subscription login flow.

    Prefer the supported Codex CLI device-auth implementation. The legacy direct
    HTTP flow below remains available only as app-owned-token plumbing for future
    verification; the live service currently rejects that usercode endpoint with
    403, so the UI must not depend on it.
    """
    return await start_cli_device_flow()


async def poll_device_flow(device_auth_id: str, user_code: str) -> dict[str, Any]:
    """LIVE-UNVERIFIED. Poll once for completion. Returns {"status": "pending"|"done"|"error", ...}.
    On done, persists the bundle and reports logged-in. 403/404 = still pending (per the source)."""
    flow = _LOGIN_FLOWS.get((device_auth_id or "").strip())
    if flow is not None:
        status = codex_cli_status()
        if status.get("logged_in"):
            return {
                "status": "done",
                "method": "codex_cli",
                "auth_method": status.get("auth_method") or "",
                "message": status.get("message") or "",
            }
        if flow.get("returncode") is None:
            return {"status": "pending"}
        tail = " ".join(flow.get("tail") or [])[-400:]
        message = tail or status.get("message") or "codex login ended before credentials were available"
        return {"status": "error", "message": message}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                DEVICE_TOKEN_URL, json={"device_auth_id": device_auth_id, "user_code": user_code}
            )
    except httpx.HTTPError as exc:
        return {"status": "error", "message": f"device poll failed: {exc}"}
    if resp.status_code in (403, 404):
        return {"status": "pending"}
    if resp.status_code // 100 != 2:
        return {"status": "error", "message": f"device poll HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except ValueError:
        return {"status": "error", "message": "device poll returned non-JSON"}
    # The source returns an authorization_code + PKCE code_verifier here, then exchanges for tokens.
    code = str((data or {}).get("authorization_code") or "")
    verifier = str((data or {}).get("code_verifier") or "")
    if not code:
        return {"status": "error", "message": "device poll missing authorization_code"}
    blob = await _exchange_code_for_tokens(code, verifier)
    if not blob:
        return {"status": "error", "message": "token exchange failed"}
    save_codex_oauth_blob(blob)
    return {"status": "done"}


async def _exchange_code_for_tokens(authorization_code: str, code_verifier: str) -> dict[str, Any] | None:
    """LIVE-UNVERIFIED. PKCE auth-code exchange → token bundle."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                OAUTH_TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "code_verifier": code_verifier,
                    "client_id": CLIENT_ID,
                },
            )
        if resp.status_code != 200:
            return None
        blob = _blob_from_token_response(resp.json())
    except (httpx.HTTPError, ValueError):
        return None
    return blob if blob.get("access_token") else None


__all__ = [
    "CLIENT_ID", "AUTH_BASE_URL", "OAUTH_TOKEN_URL", "CODEX_OAUTH_KEY", "REFRESH_MARGIN_S",
    "load_codex_oauth_blob", "save_codex_oauth_blob", "clear_codex_oauth_blob",
    "is_logged_in", "account_id", "account_id_from_id_token", "needs_refresh",
    "codex_cli_status", "logout_codex_cli", "start_cli_device_flow",
    "get_codex_oauth_bearer", "refresh_codex_oauth_token", "start_device_flow", "poll_device_flow",
]
