"""Resolve the effective Cortex runtime profile for local Beat consumers."""

from __future__ import annotations

import json
import os
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _yaml_value(path: Path, section: str, key: str) -> str:
    if not path.exists():
        return ""

    current = ""
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not raw_line.startswith((" ", "\t")) and line.endswith(":"):
            current = line[:-1].strip()
            continue
        if current != section or ":" not in line:
            continue
        found_key, value = line.split(":", 1)
        if found_key.strip() == key:
            return value.strip().strip("\"'")
    return ""


def _normalize_project(value: str, root: Path) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if re.fullmatch(r"[a-z][a-z0-9-]{1,63}", normalized):
        return normalized
    fallback = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-")
    if re.fullmatch(r"[a-z][a-z0-9-]{1,63}", fallback):
        return fallback
    return "local-cortex"


def _identity_base(value: str) -> str:
    return re.split(r"[:@]", str(value or "").lower(), maxsplit=1)[0]


def _workspace_project(root: Path, project_key: str) -> dict[str, Any]:
    workspace_path = root / ".agents" / "config" / "workspace.json"
    if not workspace_path.exists():
        return {}
    try:
        data = json.loads(workspace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    for project in data.get("projects", []):
        if str(project.get("key") or "").lower() == project_key:
            return project if isinstance(project, dict) else {}
    return {}


def _fetch_runtime_profile(api_url: str, project_key: str, timeout: float) -> dict[str, Any]:
    encoded_project = urllib.parse.quote(project_key, safe="")
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/projects/{encoded_project}/runtime",
        headers={
            "Accept": "application/json",
            "X-Project": project_key,
            "X-Agent-Name": os.environ.get("BEAT_CORTEX_AGENT", "beat"),
            "X-Cortex-Admin-Token": os.environ.get("CORTEX_ADMIN_TOKEN", ""),
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _coerce_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def load_runtime_profile(root: Path | None = None, *, timeout: float = 2.0) -> dict[str, Any]:
    """Return the best available project runtime profile.

    The Cortex API is authoritative when reachable. Local runtime/workspace files
    are only bootstrap fallbacks so launchd and fresh-package setup can start the
    stack before the API answers.
    """

    root = (root or Path(__file__).resolve().parents[2]).resolve()
    runtime_path = Path(os.environ.get("CORTEX_RUNTIME_CONFIG", root / ".agents" / "config" / "runtime.yaml"))

    runtime_project = _yaml_value(runtime_path, "project", "name")
    project_key = _normalize_project(os.environ.get("CORTEX_PROJECT") or runtime_project, root)  # fitness:allow-literal false-match: root
    api_url = (
        os.environ.get("CORTEX_API_URL")
        or os.environ.get("CORTEX_API_BASE")
        or os.environ.get("CORTEX_API")
        or "http://localhost:8501"
    ).rstrip("/")

    api_profile: dict[str, Any] = {}
    try:
        api_profile = _fetch_runtime_profile(api_url, project_key, timeout)
    except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        api_profile = {}

    workspace_profile = _workspace_project(root, project_key)
    beat_profile = api_profile.get("beat") if isinstance(api_profile.get("beat"), dict) else {}
    workspace_beat = workspace_profile.get("beat") if isinstance(workspace_profile.get("beat"), dict) else {}

    repo_root = (
        os.environ.get("CORTEX_WORKSPACE_ROOT")
        or str(api_profile.get("repo_root") or "")  # fitness:allow-literal false-match: root
        or str(workspace_profile.get("roots", [{}])[0].get("path") if workspace_profile.get("roots") else "")  # fitness:allow-literal false-match: root
        or str(root)  # fitness:allow-literal false-match: root
    )
    beat_agent_name = (
        os.environ.get("BEAT_CORTEX_AGENT_NAME")
        or str(beat_profile.get("agent") or "")
        or str(workspace_beat.get("orchestrator_agent") or "")
        or "beat"
    ).lower()
    beat_agent_base = _identity_base(beat_agent_name) or "beat"
    beat_agent = (
        os.environ.get("BEAT_CORTEX_AGENT")
        or str(beat_profile.get("agent_id") or "")
        or f"{beat_agent_base}@{project_key}"
    ).lower()
    launchd_label = (
        os.environ.get("BEAT_LAUNCHD_LABEL")
        or str(beat_profile.get("launchd_label") or "")
        or str(workspace_beat.get("launchd_label") or "")
        or f"com.cortex.{project_key}.beat"
    )
    cadence_minutes = _coerce_int(
        beat_profile.get("cadence_minutes") or workspace_beat.get("cadence_minutes"),
        25,
    )

    return {
        "source": "api" if api_profile else "bootstrap-fallback",
        "project_key": project_key,
        "project_id": str(api_profile.get("project_id") or workspace_profile.get("project_id") or ""),
        "display_name": str(api_profile.get("display_name") or workspace_profile.get("display_name") or project_key),
        "repo_root": repo_root,  # fitness:allow-literal false-match: root
        "api_url": str(api_profile.get("api_url") or api_url),
        "agents": api_profile.get("agents") if isinstance(api_profile.get("agents"), list) else [],
        "beat_agent": beat_agent,
        "beat_agent_name": beat_agent_base,
        "beat_launchd_label": launchd_label,
        "beat_plist_name": f"{launchd_label}.plist",
        "beat_cadence_minutes": cadence_minutes,
        "beat_cadence_seconds": cadence_minutes * 60,
    }


def shell_exports(profile: dict[str, Any]) -> str:
    keys = {
        "CORTEX_PROJECT": profile["project_key"],
        "CORTEX_WORKSPACE_ROOT": profile["repo_root"],  # fitness:allow-literal false-match: root
        "CORTEX_API_URL": profile["api_url"],
        "BEAT_CORTEX_AGENT": profile["beat_agent"],
        "BEAT_CORTEX_AGENT_NAME": profile["beat_agent_name"],
        "BEAT_LAUNCHD_LABEL": profile["beat_launchd_label"],
        "BEAT_PLIST_NAME": profile["beat_plist_name"],
        "BEAT_START_INTERVAL": str(profile["beat_cadence_seconds"]),
    }
    return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in keys.items())
