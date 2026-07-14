"""Claude Code CLI model and effort discovery.

Claude Code does not currently expose a dedicated model-list command.  Its supported
CLI contract does advertise the accepted model aliases/examples and the current
``--effort`` choices in ``claude --help``.  We parse that read-only output, merge it
with the curated outage fallback in ``app.harness``, and bridge it through the host
harness service when the console is containerized.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from typing import Any

import httpx

from .cli_resolver import resolve_latest_executable

CLAUDE_PROGRAM = "claude"
CLAUDE_LIST_TIMEOUT_S = float(os.environ.get("HARNESS_CLAUDE_LIST_TIMEOUT_S", "8"))
CLAUDE_CATALOG_CACHE_SECONDS = float(
    os.environ.get("HARNESS_CLAUDE_CATALOG_CACHE_S", "300")
)

_catalog_cache: dict[str, Any] = {"models": None, "expires": 0.0}
_SUBSCRIPTION_API_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _option_block(text: str, flag: str) -> str:
    pattern = rf"(?ms)^\s*{re.escape(flag)}\s+.*?(?=^\s{{2,}}(?:--|-[A-Za-z],)|^Commands:)"
    match = re.search(pattern, text or "")
    return match.group(0) if match else ""


def _model_label(value: str) -> str:
    if value in {"opus", "sonnet", "haiku", "fable"}:
        return value.title()
    return value.replace("-", " ").title()


def parse_claude_help(text: str) -> list[dict[str, Any]]:
    """Extract model examples/aliases and the live effort enum from CLI help."""
    effort_block = _option_block(text, "--effort <level>")
    effort_match = re.search(r"\(([^()]*)\)", effort_block)
    efforts = []
    if effort_match:
        efforts = [part.strip() for part in effort_match.group(1).split(",") if part.strip()]

    model_block = _option_block(text, "--model <model>")
    values = re.findall(r"['\"]([A-Za-z0-9][A-Za-z0-9._\-\[\]]*)['\"]", model_block)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(
            {
                "value": value,
                "label": _model_label(value),
                "reasoning_levels": list(efforts),
            }
        )
    return out


def cached_claude_model_options() -> list[dict[str, Any]]:
    models = _catalog_cache.get("models")
    if not isinstance(models, list):
        return []
    return [
        {**row, "reasoning_levels": list(row.get("reasoning_levels") or [])}
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


def _resolve_claude_program(program: str) -> str:
    env = dict(os.environ)
    for key in _SUBSCRIPTION_API_ENV_KEYS:
        env.pop(key, None)
    return resolve_latest_executable(program, env=env)


def _shell_help(program: str, timeout_s: float) -> str:
    env = dict(os.environ)
    for key in _SUBSCRIPTION_API_ENV_KEYS:
        env.pop(key, None)
    program = _resolve_claude_program(program)
    try:
        result = subprocess.run(
            [program, "--help"],
            capture_output=True,
            timeout=timeout_s,
            env=env,
            text=True,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    return "\n".join((result.stdout or "", result.stderr or ""))


async def _list_via_cli(program: str, timeout_s: float) -> list[dict[str, Any]]:
    text = await asyncio.to_thread(_shell_help, program, timeout_s)
    return parse_claude_help(text)


async def _list_via_bridge(timeout_s: float) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=3.0)
        ) as client:
            response = await client.get(
                f"{_harness_base_url()}/models/claude",
                headers=_harness_headers(),
            )
        if response.status_code != 200:
            return []
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return [
        {**row, "reasoning_levels": list(row.get("reasoning_levels") or [])}
        for row in models
        if isinstance(row, dict) and str(row.get("value") or "").strip()
    ]


async def list_claude_model_options(
    *,
    program: str = CLAUDE_PROGRAM,
    timeout_s: float = CLAUDE_LIST_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Return discovered Claude options, degrading to the prior cache/empty."""
    now = time.monotonic()
    cached = cached_claude_model_options()
    if cached and now < float(_catalog_cache.get("expires") or 0.0):
        return cached

    models = (
        await _list_via_bridge(timeout_s)
        if _remote_mode()
        else await _list_via_cli(program, timeout_s)
    )
    if models:
        _catalog_cache["models"] = models
        _catalog_cache["expires"] = time.monotonic() + CLAUDE_CATALOG_CACHE_SECONDS
        return cached_claude_model_options()
    return cached


__all__ = [
    "cached_claude_model_options",
    "list_claude_model_options",
    "parse_claude_help",
]
