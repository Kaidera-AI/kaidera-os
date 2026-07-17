"""PI CLI model catalog bridge.

The console container cannot run ``pi`` because the CLI and OAuth/provider state live
on the host. The host harness-service can, so this module is deliberately host-safe:
it shells ``pi --list-models`` without a shell, parses the table it prints, and returns
the same provider-group shape the SPA config catalog already understands.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from collections import OrderedDict
from typing import Any

import httpx

from .cli_resolver import resolve_latest_executable

PI_PROGRAM = "pi"
# `pi --list-models` is a node CLI: a FRESH spawn pays a ~6-8s V8 cold-start (parse +
# JIT-compile the bundled JS + module file opens) — confirmed by sampling. It's only
# sub-second when re-run from a warm shell. So the fetch is slow by nature; the timeout
# must clear the real cold-start, and (crucially) it must run in the BACKGROUND, never
# in a request path. 30s leaves generous headroom over the observed 6-8s.
PI_LIST_TIMEOUT_S = float(os.environ.get("HARNESS_PI_LIST_TIMEOUT_S", "30"))
OPENAI_CODEX_PROVIDER = "openai-codex"

# Stale-while-revalidate cache for the PI model list (it changes rarely). Reads NEVER
# block on the slow `pi` cold-start: a cold/stale cache serves the last value (or [],
# which the service layer renders as the fixed PI fallback) IMMEDIATELY and kicks ONE
# background refresh. The live list lands within a few seconds, shown on the next read.
# This is what fixed the AI-Worker config popup hanging ~8s on every open.
_PI_CATALOG_CACHE_SECONDS = float(os.environ.get("HARNESS_PI_CATALOG_CACHE_S", "300"))
_pi_catalog_cache: dict[str, Any] = {"groups": None, "expires": 0.0}
_pi_refresh_task: "asyncio.Task[None] | None" = None  # single in-flight bg refresh

_PROVIDER_LABELS = {
    "fireworks": "Fireworks",
    "ollama-cloud": "Ollama Cloud",
    "openai-codex": "OpenAI Codex",
}


def cached_pi_model_groups() -> list[dict[str, Any]]:
    """Defensive copy of the current PI catalog cache for runtime validation."""
    groups = _pi_catalog_cache.get("groups")
    if not isinstance(groups, list):
        return []
    out: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        rows: list[dict[str, Any]] = []
        for row in group.get("rows") or []:
            if not isinstance(row, dict):
                continue
            copied = dict(row)
            if "reasoning_levels" in row:
                copied["reasoning_levels"] = list(row.get("reasoning_levels") or [])
            rows.append(copied)
        out.append({**group, "rows": rows})
    return out


def _pi_model_value(provider: str, model: str) -> str:
    """The saved model value the runner can pass back to PI.

    PI accepts ``provider/model`` values via ``--model``. Keep OpenAI-Codex values
    bare for backward compatibility with existing saved PI overrides and the fixed
    fallback list.
    """
    provider = (provider or "").strip()
    model = (model or "").strip()
    if provider == OPENAI_CODEX_PROVIDER:
        return model
    return f"{provider}/{model}" if provider else model


def parse_pi_thinking_levels(text: str) -> list[str]:
    """Read the installed PI CLI's ``--thinking`` choices from ``pi --help``."""
    match = re.search(
        r"--thinking\s+<level>[^\n]*?Set thinking level:\s*([^\n]+)",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return []
    raw = match.group(1).strip()
    # Current PI prints: "off, minimal, low, medium, high, xhigh".
    return [
        token.strip().lower()
        for token in raw.split(",")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", token.strip())
    ]


def parse_pi_list_models(
    text: str,
    thinking_levels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse ``pi --list-models`` table output into provider catalog groups.

    Expected rows are whitespace columns:
    ``provider model context max-out thinking images``. The command currently writes
    the table to stderr on this host, so callers should pass stdout+stderr combined.
    Unknown/malformed lines are skipped; no parse error escapes to the route.
    """
    by_provider: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("provider "):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        provider, model, context, max_out, thinking, images = parts[:6]
        if not provider or not model:
            continue
        supports_reasoning = thinking.lower() == "yes"
        row = {
            "id": _pi_model_value(provider, model),
            "display_name": model,
            "type": "chat",
            "context_window": context,
            "max_output": max_out,
            "reasoning": supports_reasoning,
            "image": images.lower() == "yes",
        }
        if thinking_levels is not None:
            row["reasoning_levels"] = list(thinking_levels) if supports_reasoning else []
        by_provider.setdefault(provider, []).append(row)

    groups: list[dict[str, Any]] = []
    for provider, rows in by_provider.items():
        if not rows:
            continue
        groups.append(
            {
                "provider": provider,
                "label": _PROVIDER_LABELS.get(provider, provider.replace("-", " ").title()),
                "count": len(rows),
                "configured": True,
                "rows": rows,
            }
        )
    return groups


def _remote_mode() -> bool:
    """True when the console runs CONTAINERIZED against the host harness-service.

    In remote mode the ``pi`` CLI is absent from this process (it lives on the host),
    so the catalog must come from the host bridge, not a local subprocess.
    """
    return os.environ.get("HARNESS_SPAWN_MODE", "").strip().lower() == "remote"


def _harness_base_url() -> str:
    host = os.environ.get("HARNESS_SERVICE_HOST", "host.docker.internal")
    port = os.environ.get("HARNESS_SERVICE_PORT", "8766")
    return f"http://{host}:{port}"


def _harness_headers() -> dict[str, str]:
    token = (os.environ.get("HARNESS_SERVICE_TOKEN", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _list_pi_model_groups_via_bridge(timeout_s: float) -> list[dict[str, Any]]:
    """Fetch PI model groups from the host harness-service ``/models/pi`` bridge.

    The containerized console has no ``pi`` CLI, so it asks the host service for
    the same model-group shape produced locally. It consumes only model ids and
    metadata and degrades to ``[]``.
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=3.0)
        ) as client:
            resp = await client.get(
                f"{_harness_base_url()}/models/pi",
                headers=_harness_headers(),
            )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    groups = data.get("groups") if isinstance(data, dict) else None
    return [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []


async def list_pi_model_groups(
    *,
    program: str = PI_PROGRAM,
    timeout_s: float = PI_LIST_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Return PI model groups, degrading to ``[]``.

    SINGLE SOURCE for live PI models. In remote/container mode the ``pi`` CLI is not in
    this process, so we fetch the host harness-service ``/models/pi`` bridge; on the host
    (local mode) we shell ``pi --list-models`` directly. Either way the shape is the
    same provider-group catalog the SPA config picker consumes.

    Read-only and token-safe: never reads PI auth files, never logs raw command output,
    and strips API credentials from the discovery subprocess.

    STALE-WHILE-REVALIDATE (`_PI_CATALOG_CACHE_SECONDS`): a read returns the cached list
    instantly and NEVER blocks on the ~6-8s `pi` cold-start. A cold/stale cache returns the
    last value (or [] the very first time — the service layer then shows the fixed PI
    fallback) and triggers ONE background refresh; the fresh list shows on the next read.
    """
    now = time.monotonic()
    cached = _pi_catalog_cache["groups"]
    if cached is None or now >= float(_pi_catalog_cache["expires"]):
        _kick_background_refresh(program, timeout_s)  # non-blocking; single-flight
    return cached_pi_model_groups() if cached is not None else []


def _kick_background_refresh(program: str, timeout_s: float) -> None:
    """Start ONE background `pi` catalog refresh if none is in flight. No-op without a
    running loop (a sync caller) — the next async read starts it."""
    global _pi_refresh_task
    if _pi_refresh_task is not None and not _pi_refresh_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _pi_refresh_task = loop.create_task(_refresh_pi_catalog(program, timeout_s))


async def _refresh_pi_catalog(program: str, timeout_s: float) -> None:
    """Fetch the live PI groups (bridge or thread-shelled CLI) and fill the cache. Runs in
    the background; degrades to a no-op on empty so a transient failure retries next read."""
    if _remote_mode():
        groups = await _list_pi_model_groups_via_bridge(timeout_s)
    else:
        groups = await _list_pi_model_groups_via_cli(program, timeout_s)
    if groups:  # only cache a real result; empty (failure/timeout) leaves the prior value
        _pi_catalog_cache["groups"] = list(groups)
        _pi_catalog_cache["expires"] = time.monotonic() + _PI_CATALOG_CACHE_SECONDS


def _catalog_env() -> dict[str, str]:
    """Return a discovery environment without API credentials."""
    blocked = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    suffixes = ("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN")
    return {
        key: value
        for key, value in os.environ.items()
        if key not in blocked and not key.endswith(suffixes)
    }


def _shell_pi_list_models(program: str, timeout_s: float) -> str:
    """Blocking `pi --list-models` → combined stdout+stderr text. Degrades to "".

    Runs in a WORKER THREAD (see caller). pi writes its table to stderr on this host,
    so both streams are captured and joined.
    """
    try:
        proc = subprocess.run(
            [program, "--list-models"],
            capture_output=True,
            timeout=timeout_s,
            env=_catalog_env(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    return b"\n".join([proc.stdout or b"", proc.stderr or b""]).decode("utf-8", "replace")


def _shell_pi_help(program: str, timeout_s: float) -> str:
    """Blocking ``pi --help`` used only to discover the current effort enum."""
    try:
        proc = subprocess.run(
            [program, "--help"],
            capture_output=True,
            timeout=timeout_s,
            env=_catalog_env(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    return b"\n".join([proc.stdout or b"", proc.stderr or b""]).decode("utf-8", "replace")


def _resolve_pi_program(program: str) -> str:
    return resolve_latest_executable(program, env=_catalog_env())


async def _list_pi_model_groups_via_cli(program: str, timeout_s: float) -> list[dict[str, Any]]:
    """NATIVE mode: shell `pi --list-models` IN A THREAD and parse it. Degrades to [].

    Why a thread and not `asyncio.create_subprocess_exec`: under the live console's busy
    event loop (SSE streams + many SPA sockets) the loop was too slow to drain the asyncio
    subprocess pipes / reap the child, so `communicate()` hit the timeout and returned EMPTY
    on EVERY call (pi exits in <1.5s standalone) — which, since empties aren't cached, made
    the config popup hang ~8s forever. A blocking `subprocess.run` in a worker thread reads
    the pipes with OS reads, fully off the event loop, immune to that starvation. (Uncached —
    the caller `list_pi_model_groups` owns the TTL cache + single-flight lock.)
    """
    resolved_program = await asyncio.to_thread(_resolve_pi_program, program)
    text, help_text = await asyncio.gather(
        asyncio.to_thread(_shell_pi_list_models, resolved_program, timeout_s),
        asyncio.to_thread(_shell_pi_help, resolved_program, timeout_s),
    )
    levels = parse_pi_thinking_levels(help_text)
    return parse_pi_list_models(text, levels or None) or []

__all__ = [
    "cached_pi_model_groups",
    "parse_pi_list_models",
    "parse_pi_thinking_levels",
    "list_pi_model_groups",
]
