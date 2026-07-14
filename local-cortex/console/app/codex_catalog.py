"""Codex CLI model catalog bridge.

Codex subscription models and reasoning levels change independently of Kaidera OS.
The supported integration surface is ``codex app-server``: after the standard
initialize handshake, ``model/list`` returns picker-visible models together with
their per-model effort ladders.  This module keeps that discovery off the request
model-shaping layer and provides the same host bridge used by the PI catalog when
the console itself runs in a container.

Discovery is read-only, short-lived, cached, and fail-soft.  An unavailable or old
Codex CLI returns ``[]`` so callers can use the curated fallback in ``app.harness``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from typing import Any

import httpx

from .cli_resolver import resolve_latest_executable

CODEX_PROGRAM = "codex"
CODEX_LIST_TIMEOUT_S = float(os.environ.get("HARNESS_CODEX_LIST_TIMEOUT_S", "8"))
CODEX_CATALOG_CACHE_SECONDS = float(
    os.environ.get("HARNESS_CODEX_CATALOG_CACHE_S", "300")
)

_catalog_cache: dict[str, Any] = {"models": None, "expires": 0.0}
_SUBSCRIPTION_API_ENV_KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY")
_MAX_CATALOG_PAGES = 10
_APP_SERVER_STREAM_LIMIT = 4 * 1024 * 1024
def _resolve_codex_program(program: str) -> str:
    """Choose the newest installed Codex CLI, independent of PATH ordering."""
    return resolve_latest_executable(program, env=_codex_child_env())


def parse_codex_model_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize one app-server ``model/list`` result into picker options.

    Hidden and malformed rows are ignored.  Model order is preserved because the
    server places its recommended default first.  Each row carries the exact effort
    ladder advertised for that model; clients must not assume one global Codex enum.
    """
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, dict) or raw.get("hidden") is True:
            continue
        value = str(raw.get("value") or raw.get("model") or raw.get("id") or "").strip()
        if not value or value in seen:
            continue

        efforts: list[str] = []
        for option in raw.get("reasoning_levels") or raw.get("supportedReasoningEfforts") or []:
            if isinstance(option, dict):
                effort = str(option.get("reasoningEffort") or "").strip()
            else:
                effort = str(option or "").strip()
            if effort and effort not in efforts:
                efforts.append(effort)

        modalities = [
            str(item).strip()
            for item in (raw.get("input_modalities") or raw.get("inputModalities") or ["text", "image"])
            if str(item).strip()
        ]
        seen.add(value)
        out.append(
            {
                "value": value,
                "label": str(raw.get("label") or raw.get("displayName") or raw.get("id") or value).strip(),
                "reasoning_levels": efforts,
                "default_reasoning": str(
                    raw.get("default_reasoning") or raw.get("defaultReasoningEffort") or ""
                ).strip(),
                "is_default": bool(raw.get("is_default") or raw.get("isDefault")),
                "input_modalities": modalities,
                "description": str(raw.get("description") or "").strip(),
            }
        )
    return out


def cached_codex_model_options() -> list[dict[str, Any]]:
    """Return a defensive copy of the current live cache, or ``[]``."""
    models = _catalog_cache.get("models")
    if not isinstance(models, list):
        return []
    return [
        {
            **row,
            "reasoning_levels": list(row.get("reasoning_levels") or []),
            "input_modalities": list(row.get("input_modalities") or []),
        }
        for row in models
        if isinstance(row, dict)
    ]


def _remote_mode() -> bool:
    return os.environ.get("HARNESS_SPAWN_MODE", "").strip().lower() == "remote"


def _harness_base_url() -> str:
    host = os.environ.get("HARNESS_SERVICE_HOST", "host.docker.internal")
    port = os.environ.get("HARNESS_SERVICE_PORT", "8766")
    return f"http://{host}:{port}"


def _harness_headers() -> dict[str, str]:
    token = (os.environ.get("HARNESS_SERVICE_TOKEN", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _codex_child_env() -> dict[str, str]:
    """Force ChatGPT subscription auth rather than a metered API key."""
    env = dict(os.environ)
    for key in _SUBSCRIPTION_API_ENV_KEYS:
        env.pop(key, None)
    return env


async def _send(proc: asyncio.subprocess.Process, message: dict[str, Any]) -> bool:
    if proc.stdin is None:
        return False
    try:
        proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await proc.stdin.drain()
        return True
    except (BrokenPipeError, ConnectionError, OSError):
        return False


async def _read_response(
    proc: asyncio.subprocess.Process,
    request_id: int,
    deadline: float,
) -> dict[str, Any] | None:
    """Read JSONL frames until the matching response arrives or time expires."""
    if proc.stdout is None:
        return None
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except (asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
            return None
        if not line:
            return None
        try:
            frame = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(frame, dict) and frame.get("id") == request_id:
            return frame


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None:
        proc.stdin.close()
        with suppress(BrokenPipeError, ConnectionError, OSError):
            await proc.stdin.wait_closed()
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()


async def _list_via_cli(
    program: str = CODEX_PROGRAM,
    timeout_s: float = CODEX_LIST_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Query ``codex app-server`` using its stable initialize + model/list flow."""
    resolved_program = await asyncio.to_thread(_resolve_codex_program, program)
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved_program,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_codex_child_env(),
            limit=_APP_SERVER_STREAM_LIMIT,
        )
    except (FileNotFoundError, OSError):
        return []

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, timeout_s)
    models: list[dict[str, Any]] = []
    try:
        initialized = await _send(
            proc,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "kaidera-os",
                        "title": "Kaidera OS",
                        "version": "1",
                    },
                    # model/list is currently a v2 experimental method. Codex
                    # accepts initialize without this flag but then withholds the
                    # response, which looks like an empty catalog until timeout.
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        if not initialized:
            return []
        response = await _read_response(proc, 1, deadline)
        if not response or response.get("error") or not isinstance(response.get("result"), dict):
            return []
        if not await _send(proc, {"method": "initialized", "params": {}}):
            return []

        cursor: str | None = None
        request_id = 2
        for _ in range(_MAX_CATALOG_PAGES):
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            if not await _send(
                proc,
                {"method": "model/list", "id": request_id, "params": params},
            ):
                return []
            frame = await _read_response(proc, request_id, deadline)
            result = frame.get("result") if isinstance(frame, dict) else None
            if not isinstance(result, dict) or frame.get("error"):
                return []
            models.extend(parse_codex_model_list(result))
            cursor_value = result.get("nextCursor")
            cursor = str(cursor_value).strip() if cursor_value else None
            if not cursor:
                break
            request_id += 1
    finally:
        await _stop(proc)

    # De-duplicate across pages while retaining the server's recommended order.
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in models:
        value = str(row.get("value") or "")
        if value and value not in seen:
            seen.add(value)
            deduped.append(row)
    return deduped


async def _list_via_bridge(timeout_s: float) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=3.0)
        ) as client:
            response = await client.get(
                f"{_harness_base_url()}/models/codex",
                headers=_harness_headers(),
            )
        if response.status_code != 200:
            return []
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    return parse_codex_model_list(models) if isinstance(models, list) else []


async def list_codex_model_options(
    *,
    program: str = CODEX_PROGRAM,
    timeout_s: float = CODEX_LIST_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Return the current picker-visible Codex catalog, degrading to stale/empty."""
    now = time.monotonic()
    cached = cached_codex_model_options()
    if cached and now < float(_catalog_cache.get("expires") or 0.0):
        return cached

    models = (
        await _list_via_bridge(timeout_s)
        if _remote_mode()
        else await _list_via_cli(program, timeout_s)
    )
    if models:
        _catalog_cache["models"] = models
        _catalog_cache["expires"] = time.monotonic() + CODEX_CATALOG_CACHE_SECONDS
        return cached_codex_model_options()
    return cached


__all__ = [
    "cached_codex_model_options",
    "list_codex_model_options",
    "parse_codex_model_list",
]
