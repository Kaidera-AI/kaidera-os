#!/usr/bin/env python3
"""Project startup wizard for the local Cortex redistributable."""

from __future__ import annotations

import argparse
import difflib
import getpass
import importlib.util
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
VALIDATOR_PATH = SCRIPT_DIR / "validate-cortex-project-config.py"
DEFAULT_API_URL = "http://localhost:8501"
DEFAULT_ADMIN_TOKEN = ""
COLOR_CYCLE = ["green", "red", "blue", "yellow", "magenta", "cyan", "white"]

MODEL_TYPE_HELP = {
    "llm": "Chat/reasoning model for agent work.",
    "embedding": "Embedding model for semantic search over Cortex memory.",
    "reranking": "Reranking model for post-retrieval ordering.",
    "vision": "Optional vision model for image or screenshot understanding.",
    "code": "Optional code-specialized model.",
}

PROVIDER_CATALOG = {
    "openai": {
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "default_base_url": "https://api.openai.com/v1",
        "validation_path": "/models",
        "auth": "bearer",
        "models": {
            "llm": "gpt-5.5",
            "embedding": "text-embedding-3-large",
            "reranking": "provider-reranker-or-local-bge-reranker-large",
        },
    },
    "anthropic": {
        "label": "Anthropic",
        "key_env": "ANTHROPIC_API_KEY",
        "base_url_env": "ANTHROPIC_BASE_URL",
        "default_base_url": "https://api.anthropic.com",
        "validation_path": "/v1/models",
        "auth": "anthropic",
        "models": {"llm": "claude-opus-4-6", "embedding": "local-all-mpnet-base-v2", "reranking": "local-bge-reranker-large"},
    },
    "openrouter": {
        "label": "OpenRouter",
        "key_env": "OPENROUTER_API_KEY",
        "base_url_env": "OPENROUTER_BASE_URL",
        "default_base_url": "https://openrouter.ai/api/v1",
        "validation_path": "/models",
        "auth": "bearer",
        "models": {"llm": "anthropic/claude-sonnet-4.5", "embedding": "local-all-mpnet-base-v2", "reranking": "local-bge-reranker-large"},
    },
    "together": {
        "label": "Together",
        "key_env": "TOGETHER_API_KEY",
        "base_url_env": "TOGETHER_BASE_URL",
        "default_base_url": "https://api.together.xyz/v1",
        "validation_path": "/models",
        "auth": "bearer",
        "models": {"llm": "meta-llama/Llama-3.1-70B-Instruct-Turbo", "embedding": "togethercomputer/m2-bert-80M-8k-retrieval", "reranking": "local-bge-reranker-large"},
    },
    "groq": {
        "label": "Groq",
        "key_env": "GROQ_API_KEY",
        "base_url_env": "GROQ_BASE_URL",
        "default_base_url": "https://api.groq.com/openai/v1",
        "validation_path": "/models",
        "auth": "bearer",
        "models": {"llm": "llama-3.3-70b-versatile", "embedding": "local-all-mpnet-base-v2", "reranking": "local-bge-reranker-large"},
    },
    "ollama": {
        "label": "Ollama/local",
        "key_env": "",
        "base_url_env": "OLLAMA_BASE_URL",
        "default_base_url": "http://localhost:11434",
        "validation_path": "/api/tags",
        "auth": "none",
        "models": {"llm": "llama3.1", "embedding": "nomic-embed-text", "reranking": "local-bge-reranker-large"},
    },
    "openai-compatible": {
        "label": "Bring-your-own OpenAI-compatible endpoint",
        "key_env": "OPENAI_COMPATIBLE_API_KEY",
        "base_url_env": "OPENAI_COMPATIBLE_BASE_URL",
        "default_base_url": "http://localhost:8000/v1",
        "validation_path": "/models",
        "auth": "bearer",
        "models": {"llm": "configured-chat-model", "embedding": "configured-embedding-model", "reranking": "configured-reranking-model"},
    },
}

STANDARD_ROLE_LABELS = {
    "lead": "Lead",
    "worker": "Worker",
    "backend": "Backend Worker",
    "frontend": "Frontend Worker",
    "full-stack": "Full Stack Worker",
    "knowledge": "Knowledge Worker",
    "qa": "QA Worker",
    "generalist": "Generalist",
    "coordinator": "Coordinator",
    "designer": "Designer",
}

STANDARD_ROLE_RESPONSIBILITIES = {
    "lead": "First worker, user interview, planning, and acceptance coordination.",
    "worker": "General project execution worker.",
    "backend": "Backend systems, services, APIs, and data integration.",
    "frontend": "Frontend implementation, user workflows, and UI verification.",
    "full-stack": "Full-stack implementation across backend, frontend, and local tooling.",
    "knowledge": "Documentation, decisions, and project memory.",
    "qa": "Verification, regression tests, and release gates.",
    "generalist": "Cross-cutting implementation, review, and debugging.",
    "coordinator": "Heartbeat, progress tracking, and handoff monitoring.",
    "designer": "Design, visual systems, and creative production.",
}


class WizardError(Exception):
    """Raised for operator-facing wizard validation failures."""


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: str
    mode: int = 0o644
    sensitive: bool = False

    def action(self) -> str:
        if not self.path.exists():
            return "create"
        try:
            current = self.path.read_text(encoding="utf-8")
        except OSError:
            return "update"
        return "unchanged" if current == self.content else "update"


def slugify(value: str, *, fallback: str = "") -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if re.fullmatch(r"[a-z][a-z0-9-]{1,63}", slug):
        return slug
    return fallback


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_cortex_project_config", VALIDATOR_PATH)
    if not spec or not spec.loader:
        raise WizardError(f"Cannot load validator: {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WizardError(f"Config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WizardError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WizardError("Config root must be an object")
    return data


def validate_config(config: dict[str, Any]) -> None:
    validator = load_validator()
    errors = validator.validate(config)
    if errors:
        detail = "\n".join(f"  - {error}" for error in errors)
        raise WizardError(f"Invalid Cortex project config:\n{detail}")


def expand_root(raw_root: str, target_root: Path | None = None) -> Path:
    if target_root is not None:
        return target_root.expanduser().resolve()

    if not raw_root or raw_root in {"${CORTEX_PROJECT_ROOT}", "$CORTEX_PROJECT_ROOT", "__PROJECT_ROOT__"}:
        return Path.cwd().resolve()

    expanded = os.path.expandvars(os.path.expanduser(raw_root))
    if expanded in {"${CORTEX_PROJECT_ROOT}", "$CORTEX_PROJECT_ROOT", "__PROJECT_ROOT__"}:
        return Path.cwd().resolve()
    path = Path(expanded)
    if not path.is_absolute():
        raise WizardError(f"project.root must be absolute or use ${'{'}CORTEX_PROJECT_ROOT{'}'}: {raw_root}")
    return path.resolve()


def q(value: Any) -> str:
    return json.dumps(value)


def yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def normalize_agent(agent: dict[str, Any], index: int) -> dict[str, Any]:
    name = slugify(str(agent.get("name") or f"agent-{index}"))
    role = slugify(str(agent.get("role") or "generalist"))
    display_name = str(agent.get("display_name") or name.title())
    pane = dict(agent.get("pane") or {})
    pane.setdefault("title", display_name)
    pane.setdefault("color", COLOR_CYCLE[index % len(COLOR_CYCLE)])
    pane.setdefault("order", index)
    return {
        **agent,
        "name": name,
        "role": role,
        "display_name": display_name,
        "harness": str(agent.get("harness") or "manual").lower(),
        "pane": pane,
    }


def default_agent(config: dict[str, Any]) -> str:
    agents = [normalize_agent(agent, idx) for idx, agent in enumerate(config.get("agents", []))]
    for agent in agents:
        if agent["role"] != "orchestrator":
            return agent["name"]
    if agents:
        return agents[0]["name"]
    raise WizardError("Config must include at least one agent")


def role_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    configured = {
        str(role.get("slug")): dict(role)
        for role in config.get("roles", [])
        if isinstance(role, dict) and role.get("slug")
    }
    for agent in config.get("agents", []):
        role = slugify(str(agent.get("role") or "generalist"))
        configured.setdefault(
            role,
            {
                "slug": role,
                "label": STANDARD_ROLE_LABELS.get(role, role.replace("-", " ").title()),
                "responsibilities": STANDARD_ROLE_RESPONSIBILITIES.get(role, ""),
                "standard": role in STANDARD_ROLE_LABELS,
            },
        )
    return [configured[key] for key in sorted(configured)]


def runtime_yaml(config: dict[str, Any], root: Path) -> str:
    project = config["project"]
    key = project["key"]
    return (
        "# Generated by cortex-startup-wizard. Edit the project config and rerun the wizard.\n"
        "runtime: docker\n"
        "\n"
        "project:\n"
        f"  name: {key}\n"
        f"  key_prefix: {q(key + ':')}\n"
        f"  stream_name: {q(key + ':cortex:events')}\n"
        '  consumer_group: "cortex-agents"\n'
        "\n"
        "postgres:\n"
        "  port: 5499\n"
        "  container_name: cortex-pg\n"
        "  database: platform_agent_memory\n"
        "  user: postgres\n"
        "  password: postgres\n"
        "\n"
        "workspace:\n"
        f"  root: {q(str(root))}\n"
    )


def workspace_json(config: dict[str, Any], root: Path) -> str:
    project = config["project"]
    key = project["key"]
    agents = [normalize_agent(agent, idx) for idx, agent in enumerate(config["agents"])]
    beat = dict(config.get("beat") or {})
    default = default_agent(config)
    profile_globs = list(project.get("profile_globs") or ["agents/*IDENTITY.md", ".agents/roles/*.md"])
    knowledge_globs = list(project.get("knowledge_globs") or ["AGENTS.md", ".agents/rules/*.md"])

    aliases: dict[str, str] = {agent["name"]: agent["name"] for agent in agents}
    for agent in agents:
        aliases.setdefault(agent["role"], agent["name"])
    orchestrator = beat.get("orchestrator_agent") or default
    aliases.setdefault("orchestrator", orchestrator)

    project_record = {
        "key": key,
        "display_name": project["name"],
        "parent": None,
        "repo_type": "repo",
        "default_agent": default,
        "status": "active",
        "beat": {
            "orchestrator_agent": orchestrator,
            "cadence_minutes": beat.get("cadence_minutes", 25),
            "launchd_label": f"com.cortex.{key}.beat",
            "progress_provider": beat.get("progress_provider", "none"),
        },
        "profile_globs": profile_globs,
        "knowledge_globs": knowledge_globs,
        "roots": [{"path": str(root), "kind": "primary"}],
    }
    if beat.get("progress_file"):
        project_record["beat"]["progress_file"] = beat["progress_file"]

    body = {
        "registry_mode": "configured",
        "program": {"key": key, "name": project["name"], "root": str(root)},
        "agent_aliases": {key: aliases},
        "agent_alias_patterns": {key: []},
        "projects": [project_record],
    }
    return json.dumps(body, indent=2) + "\n"

def beat_env(config: dict[str, Any], root: Path) -> str:
    project = config["project"]
    beat = dict(config.get("beat") or {})
    key = project["key"]
    orchestrator = beat.get("orchestrator_agent") or default_agent(config)
    cadence_minutes = int(beat.get("cadence_minutes", 25))
    return (
        "# Generated by cortex-startup-wizard. No secrets are written here.\n"
        f"CORTEX_PROJECT={key}\n"
        f"CORTEX_WORKSPACE_ROOT={root}\n"
        f"BEAT_CORTEX_AGENT={orchestrator}@{key}\n"
        f"BEAT_CORTEX_AGENT_NAME={orchestrator}\n"
        f"BEAT_LAUNCHD_LABEL=com.cortex.{key}.beat\n"
        f"BEAT_START_INTERVAL={cadence_minutes * 60}\n"
        "CORTEX_API_URL=${CORTEX_API_URL:-http://localhost:8501}\n"
        "CORTEX_KEYS_FILE=${CORTEX_KEYS_FILE:-${CORTEX_WORKSPACE_ROOT}/local-cortex/.env}\n"
    )


def provider_label(name: str) -> str:
    return str(PROVIDER_CATALOG.get(name, {}).get("label") or name)


def normalize_provider_names(raw: list[str] | None) -> list[str]:
    names: list[str] = []
    for value in raw or []:
        for item in value.split(","):
            name = slugify(item.strip(), fallback="")
            if not name:
                continue
            if name not in PROVIDER_CATALOG:
                raise WizardError(f"Unsupported provider {item!r}; choose one of: {', '.join(PROVIDER_CATALOG)}")
            if name not in names:
                names.append(name)
    return names


def env_assign(name: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise WizardError(f"Environment value for {name} cannot contain newlines")
    return f"{name}={shlex.quote(value)}" if value else f"{name}="


def join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def validate_provider_key(provider: str, key_value: str, base_url: str, timeout: float) -> tuple[str, str]:
    meta = PROVIDER_CATALOG[provider]
    if meta.get("key_env") and not key_value:
        return "pending", "missing key"
    headers = {"Accept": "application/json"}
    auth = meta.get("auth")
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {key_value}"
    elif auth == "anthropic":
        headers["x-api-key"] = key_value
        headers["anthropic-version"] = "2023-06-01"
    request = urllib.request.Request(join_url(base_url, str(meta["validation_path"])), headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(2048)
    except urllib.error.HTTPError as exc:
        return "failed", f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return "failed", str(exc.reason)
    except TimeoutError:
        return "failed", "timeout"
    return "validated", "one-call check succeeded"


def provider_model_defaults(provider: str) -> dict[str, str]:
    meta = PROVIDER_CATALOG[provider]
    return {str(key): str(value) for key, value in dict(meta.get("models") or {}).items()}


def build_provider_key_plan(
    config: dict[str, Any],
    *,
    mode: str,
    providers: list[str],
    validate_keys: bool,
    timeout: float,
) -> dict[str, Any]:
    selected = normalize_provider_names(providers)
    if mode == "prompt" and not selected:
        options = ", ".join(f"{name} ({provider_label(name)})" for name in PROVIDER_CATALOG)
        selected = normalize_provider_names([prompt(f"Providers to configure, comma separated. Options: {options}", "openai")])
    if mode == "env" and not selected:
        inferred = [
            name
            for name, meta in PROVIDER_CATALOG.items()
            if meta.get("key_env") and os.environ.get(str(meta["key_env"]))
        ]
        if os.environ.get("OLLAMA_BASE_URL"):
            inferred.append("ollama")
        selected = normalize_provider_names(inferred)

    selections: list[dict[str, Any]] = []
    for name in selected:
        meta = PROVIDER_CATALOG[name]
        key_env = str(meta.get("key_env") or "")
        base_url_env = str(meta.get("base_url_env") or "")
        base_url = os.environ.get(base_url_env) or str(meta.get("default_base_url") or "")
        key_value = ""
        if key_env:
            if mode == "prompt":
                key_value = getpass.getpass(f"{provider_label(name)} key ({key_env}); leave blank to defer: ").strip()
            elif mode == "env":
                key_value = os.environ.get(key_env, "")
        status = "pending" if key_env and not key_value else "not_validated"
        detail = "fill later" if status == "pending" else "validation not requested"
        if validate_keys and status != "pending":
            status, detail = validate_provider_key(name, key_value, base_url, timeout)
        selections.append(
            {
                "provider": name,
                "label": provider_label(name),
                "key_env": key_env,
                "key_value": key_value,
                "base_url_env": base_url_env,
                "base_url": base_url,
                "models": provider_model_defaults(name),
                "validation_status": status,
                "validation_detail": detail,
            }
        )

    return {
        "mode": mode,
        "selected": selections,
        "model_requirements": config.get("model_requirements", []),
    }


def provider_env_content(plan: dict[str, Any]) -> str:
    selected = list(plan.get("selected") or [])
    lines = [
        "# Generated by cortex-startup-wizard. This file is gitignored.",
        "# Do not commit provider keys.",
        "",
    ]
    if selected:
        primary = selected[0]
        lines.append(env_assign("CORTEX_MODEL_PROVIDER", str(primary["provider"])))
        for selection in selected:
            lines.append("")
            lines.append(f"# {selection['label']}")
            key_env = str(selection.get("key_env") or "")
            if key_env:
                lines.append(env_assign(key_env, str(selection.get("key_value") or "")))
            base_url_env = str(selection.get("base_url_env") or "")
            if base_url_env:
                lines.append(env_assign(base_url_env, str(selection.get("base_url") or "")))
            for model_type, model_name in dict(selection.get("models") or {}).items():
                env_name = f"CORTEX_{str(model_type).upper()}_MODEL"
                lines.append(env_assign(env_name, str(model_name)))
    else:
        lines.extend(
            [
                "# No provider selected yet. Choose one provider block from KEYS_PENDING.md,",
                "# fill the relevant values, then rerun validation.",
                "CORTEX_MODEL_PROVIDER=",
                "",
            ]
        )
        for name, meta in PROVIDER_CATALOG.items():
            lines.append(f"# {provider_label(name)}")
            key_env = str(meta.get("key_env") or "")
            base_url_env = str(meta.get("base_url_env") or "")
            if key_env:
                lines.append(f"# {key_env}=")
            if base_url_env:
                lines.append(f"# {base_url_env}={meta.get('default_base_url')}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def local_cortex_gitignore() -> str:
    return (
        "# Local environment and key material\n"
        ".env\n"
        ".env.local\n"
        ".env.*.local\n"
        "*_secrets*\n"
        "*.pem\n"
        "*.key\n"
    )


def keys_pending(config: dict[str, Any], key_plan: dict[str, Any] | None = None) -> str:
    requirements = config.get("model_requirements", [])
    key_plan = key_plan or {"selected": [], "mode": "skip"}
    lines = [
        "# KEYS_PENDING",
        "",
        "Provider keys are stored only in `local-cortex/.env`, which is gitignored.",
        "",
        "If setup was skipped, fill `local-cortex/.env` later and rerun the one-call provider validation.",
        "",
        "## Model Requirements",
        "",
    ]
    for item in requirements:
        required = "required" if item.get("required") else "optional"
        model_type = str(item.get("type"))
        help_text = MODEL_TYPE_HELP.get(model_type, "")
        suffix = f" ({help_text})" if help_text else ""
        lines.append(f"- {model_type}: {required} - {item.get('purpose')}{suffix}")
    lines.append("")
    lines.append("## Provider Options")
    lines.append("")
    for name, meta in PROVIDER_CATALOG.items():
        key_env = meta.get("key_env") or "no API key required"
        base_url_env = meta.get("base_url_env") or "n/a"
        lines.append(f"- {provider_label(name)} (`{name}`): key `{key_env}`, base URL `{base_url_env}`")
    lines.append("")
    lines.append("## Selected Providers")
    lines.append("")
    selected = list(key_plan.get("selected") or [])
    if not selected:
        lines.append("- none selected yet; edit `local-cortex/.env` or rerun the wizard with `--keys-mode env --provider <name>`")
    for selection in selected:
        key_state = "not required"
        if selection.get("key_env"):
            key_state = "supplied" if selection.get("key_value") else "pending"
        lines.append(
            f"- {selection['label']} (`{selection['provider']}`): key {key_state}; "
            f"validation `{selection['validation_status']}` ({selection['validation_detail']})"
        )
        for model_type, model_name in dict(selection.get("models") or {}).items():
            lines.append(f"  - {model_type}: `{model_name}`")
    lines.append("")
    return "\n".join(lines)


def build_planned_files(config: dict[str, Any], root: Path, key_plan: dict[str, Any] | None = None) -> list[PlannedFile]:
    files = [
        PlannedFile(root / ".agents/config/runtime.yaml", runtime_yaml(config, root)),
        PlannedFile(root / ".agents/config/workspace.json", workspace_json(config, root)),
        PlannedFile(root / ".agents/config/beat.env", beat_env(config, root)),
        PlannedFile(root / "local-cortex/.gitignore", local_cortex_gitignore()),
        PlannedFile(root / "local-cortex/.env", provider_env_content(key_plan or {}), mode=0o600, sensitive=True),
        PlannedFile(root / "local-cortex/KEYS_PENDING.md", keys_pending(config, key_plan)),
    ]
    return files


def agent_capabilities(agent: dict[str, Any]) -> dict[str, Any]:
    capabilities = dict(agent.get("capabilities") or {})
    capabilities.update(
        {
            "display_name": agent["display_name"],
            "responsibilities": agent.get("responsibilities", ""),
            "harness": agent["harness"],
            "managed": bool(agent.get("managed")),
            "pane": agent["pane"],
            "visibility": "active",
            "keep_visible": True,
        }
    )
    return capabilities


def project_payload(config: dict[str, Any], root: Path) -> dict[str, Any]:
    project = config["project"]
    key = project["key"]
    agents = [normalize_agent(agent, idx) for idx, agent in enumerate(config["agents"])]
    beat = dict(config.get("beat") or {})
    default = default_agent(config)
    orchestrator = beat.get("orchestrator_agent") or default

    return {
        "project_key": key,
        "display_name": project["name"],
        "repo_root": str(root),
        "repo_type": "repo",
        "status": "active",
        "default_agent": default,
        "roots": [{"path": str(root), "kind": "primary"}],
        "agents": [
            {
                "name": agent["name"],
                "role": agent["role"],
                "model": agent.get("model"),
                "capabilities": agent_capabilities(agent),
            }
            for agent in agents
        ],
        "metadata": {
            "team_name": project.get("team_name"),
            "roots": [{"path": str(root), "kind": "primary"}],
            "default_agent": default,
            "roles": role_records(config),
            "model_requirements": config.get("model_requirements", []),
            "beat": {
                "orchestrator_agent": orchestrator,
                "cadence_minutes": beat.get("cadence_minutes", 25),
                "launchd_label": f"com.cortex.{key}.beat",
                "progress_provider": beat.get("progress_provider", "none"),
                "progress_file": beat.get("progress_file"),
            },
        },
    }


def api_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    project_key: str,
    agent_name: str,
    api_url: str,
    admin_token: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Project": project_key,
            "X-Agent-Name": agent_name,
            "X-Cortex-Admin-Token": admin_token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WizardError(f"Cortex API {method} {path} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise WizardError(f"Cortex API is not reachable at {api_url}: {exc.reason}") from exc
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WizardError(f"Cortex API {method} {path} returned invalid JSON: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def register_with_api(
    config: dict[str, Any],
    root: Path,
    *,
    api_url: str,
    admin_token: str,
    verify_boot: bool = True,
) -> dict[str, Any]:
    key = config["project"]["key"]
    lead = default_agent(config)
    response = api_json(
        "POST",
        "/projects",
        project_payload(config, root),
        project_key=key,
        agent_name=lead,
        api_url=api_url,
        admin_token=admin_token,
    )
    result: dict[str, Any] = {"project": response, "boot": None, "bootstrap": None}
    if verify_boot:
        encoded = urllib.parse.quote(lead, safe="")
        boot = api_json(
            "GET",
            f"/boot/{encoded}?budget=250",
            None,
            project_key=key,
            agent_name=lead,
            api_url=api_url,
            admin_token=admin_token,
        )
        bootstrap = api_json(
            "GET",
            f"/boot/{encoded}?budget=1200",
            None,
            project_key=key,
            agent_name=lead,
            api_url=api_url,
            admin_token=admin_token,
        )
        if not boot.get("boot") or not bootstrap.get("boot"):
            raise WizardError("Cortex API boot verification did not return boot text")
        result["boot"] = "ok"
        result["bootstrap"] = "ok"
    return result


def print_plan(planned: list[PlannedFile], root: Path) -> None:
    print(f"Startup wizard plan for {root}:")
    for item in planned:
        try:
            rel = item.path.relative_to(root)
        except ValueError:
            rel = item.path
        sensitive = " (sensitive)" if item.sensitive else ""
        print(f"  {item.action():<9} {rel}{sensitive}")


def print_diff(planned: list[PlannedFile]) -> None:
    for item in planned:
        if item.sensitive:
            if item.action() != "unchanged":
                print(f"--- {item.path if item.path.exists() else '/dev/null'}")
                print(f"+++ {item.path} (planned)")
                print("@@ sensitive file redacted @@")
            continue
        old = item.path.read_text(encoding="utf-8") if item.path.exists() else ""
        if old == item.content:
            continue
        fromfile = str(item.path) if item.path.exists() else "/dev/null"
        tofile = f"{item.path} (planned)"
        sys.stdout.writelines(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                item.content.splitlines(keepends=True),
                fromfile=fromfile,
                tofile=tofile,
            )
        )


def apply_files(planned: list[PlannedFile]) -> None:
    for item in planned:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_text(item.content, encoding="utf-8")
        item.path.chmod(item.mode)


def prompt(prompt_text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt_text}{suffix}: ").strip()
    return value or default


def first_screen_config(
    *,
    project_name: str,
    project_key: str,
    project_root: str,
    team_name: str,
    scope: str,
    lead_name: str,
    lead_display: str,
    team_template: str,
    cadence_minutes: int,
) -> dict[str, Any]:
    project_name = project_name.strip()
    if not project_name:
        raise WizardError("First project name is required.")
    project_key = slugify(project_key or project_name)
    if not project_key:
        raise WizardError("First project key is required and must start with a letter.")
    lead_name = slugify(lead_name or "lead")
    if not lead_name:
        raise WizardError("Lead worker name must be a valid slug.")
    lead_display = (lead_display or lead_name.title()).strip()
    team_name = (team_name or f"{project_name} Team").strip()
    project_root = project_root or "${CORTEX_PROJECT_ROOT}"
    scope = scope.strip()

    roles: dict[str, dict[str, Any]] = {
        "lead": {
            "slug": "lead",
            "label": STANDARD_ROLE_LABELS["lead"],
            "responsibilities": scope or STANDARD_ROLE_RESPONSIBILITIES["lead"],
            "standard": True,
        }
    }
    agents: list[dict[str, Any]] = [
        {
            "name": lead_name,
            "role": "lead",
            "display_name": lead_display,
            "responsibilities": scope or STANDARD_ROLE_RESPONSIBILITIES["lead"],
            "harness": "kaidera",
            "capabilities": {
                "designation": "interactive",
                "persona_brief": scope,
            },
            "pane": {
                "title": lead_display,
                "color": COLOR_CYCLE[0],
                "order": 0,
            },
        }
    ]

    if team_template == "worker":
        roles["worker"] = {
            "slug": "worker",
            "label": STANDARD_ROLE_LABELS["worker"],
            "responsibilities": STANDARD_ROLE_RESPONSIBILITIES["worker"],
            "standard": True,
        }
        agents.append(
            {
                "name": "worker",
                "role": "worker",
                "display_name": "Worker",
                "responsibilities": STANDARD_ROLE_RESPONSIBILITIES["worker"],
                "harness": "kaidera",
                "pane": {
                    "title": "Worker",
                    "color": COLOR_CYCLE[1],
                    "order": 1,
                },
            }
        )
    elif team_template != "minimal":
        raise WizardError("team template must be 'minimal' or 'worker'.")

    return {
        "schema_version": "1.0",
        "preset": f"first-screen-{team_template}",
        "project": {
            "key": project_key,
            "name": project_name,
            "root": project_root,
            "team_name": team_name,
            "scope": scope,
        },
        "roles": list(roles.values()),
        "agents": agents,
        "model_requirements": [
            {"type": "llm", "required": True, "purpose": "Main agent chat and reasoning model."},
            {"type": "embedding", "required": True, "purpose": "Semantic search over Cortex memory."},
            {"type": "reranking", "required": True, "purpose": "Post-retrieval reranking."},
        ],
        "beat": {"orchestrator_agent": lead_name, "cadence_minutes": cadence_minutes, "progress_provider": "none"},
    }


def interactive_config() -> dict[str, Any]:
    project_name = prompt("First project name")
    project_key = prompt("First project key", slugify(project_name))
    project_root = prompt("Project root", "${CORTEX_PROJECT_ROOT}")
    scope = prompt("Project scope / what the first worker should understand")
    lead_name = prompt("First worker name", "lead")
    lead_display = prompt("First worker display name", lead_name.title() if lead_name else "Lead")
    team_name = prompt("Team name", f"{project_name} Team" if project_name else "")
    team_template = prompt("Team template (minimal|worker)", "minimal").lower()
    cadence = int(prompt("Heartbeat cadence minutes", "25"))
    return first_screen_config(
        project_name=project_name,
        project_key=project_key,
        project_root=project_root,
        team_name=team_name,
        scope=scope,
        lead_name=lead_name,
        lead_display=lead_display,
        team_template=team_template,
        cadence_minutes=cadence,
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure a local Cortex project from a redistributable project config.")
    parser.add_argument("--config", help="Project config JSON. Omit for interactive setup.")
    parser.add_argument("--project-name", help="First-screen project name. Requires no --config.")
    parser.add_argument("--project-key", help="First-screen project key. Defaults to slug(project name).")
    parser.add_argument("--project-scope", default="", help="First-screen project scope/brief for the lead worker.")
    parser.add_argument("--lead-name", default="lead", help="First worker name. Default: lead.")
    parser.add_argument("--lead-display", default="", help="First worker display name. Default: title-cased lead name.")
    parser.add_argument("--team-name", default="", help="Team name. Default: '<project name> Team'.")
    parser.add_argument("--team-template", choices=["minimal", "worker"], default="minimal", help="Initial team shape. Default: minimal lead-only.")
    parser.add_argument("--cadence-minutes", type=int, default=25, help="Heartbeat cadence metadata. Default: 25.")
    parser.add_argument("--root", help="Target project root. Overrides project.root in the config.")
    parser.add_argument("--apply", action="store_true", help="Write planned files and register through Cortex API.")
    parser.add_argument("--dry-run", action="store_true", help="Only print planned changes. This is the default without --apply.")
    parser.add_argument("--diff", action="store_true", help="Print unified diffs for planned file changes.")
    parser.add_argument("--register", dest="register", action="store_true", default=True, help="Register project and agents through Cortex API after writes.")
    parser.add_argument("--no-register", dest="register", action="store_false", help="Skip Cortex API registration.")
    parser.add_argument("--verify-boot", dest="verify_boot", action="store_true", default=True, help="Verify boot/bootstrap for the lead agent after registration.")
    parser.add_argument("--no-verify-boot", dest="verify_boot", action="store_false", help="Skip boot/bootstrap verification.")
    parser.add_argument("--api-url", default=os.environ.get("CORTEX_API_URL", DEFAULT_API_URL), help="Cortex API URL.")
    parser.add_argument("--admin-token", default=os.environ.get("CORTEX_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN), help="Cortex admin token for project registration.")
    parser.add_argument("--keys-mode", choices=["skip", "env", "prompt"], default="skip", help="Provider key setup mode. Default skip writes placeholders and KEYS_PENDING.md.")
    parser.add_argument("--provider", action="append", default=[], help="Provider to configure. Repeat or comma-separate values. Options: openai, anthropic, openrouter, together, groq, ollama, openai-compatible.")
    parser.add_argument("--validate-keys", dest="validate_keys", action="store_true", default=None, help="Run one-call validation for supplied provider keys.")
    parser.add_argument("--no-validate-keys", dest="validate_keys", action="store_false", help="Skip provider key validation.")
    parser.add_argument("--keys-timeout", type=float, default=10.0, help="Provider validation timeout in seconds.")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.config:
        config = load_config(Path(args.config))
    elif args.project_name:
        config = first_screen_config(
            project_name=args.project_name,
            project_key=args.project_key or "",
            project_root=args.root or "${CORTEX_PROJECT_ROOT}",
            team_name=args.team_name,
            scope=args.project_scope,
            lead_name=args.lead_name,
            lead_display=args.lead_display,
            team_template=args.team_template,
            cadence_minutes=args.cadence_minutes,
        )
    else:
        config = interactive_config()
    validate_config(config)
    root_override = Path(args.root) if args.root else None
    root = expand_root(config["project"].get("root", ""), root_override)
    if args.apply and not root.exists():
        raise WizardError(f"Target project root does not exist: {root}")

    validate_keys = bool(args.validate_keys) if args.validate_keys is not None else args.keys_mode in {"env", "prompt"}
    key_plan = build_provider_key_plan(
        config,
        mode=args.keys_mode,
        providers=list(args.provider or []),
        validate_keys=validate_keys,
        timeout=float(args.keys_timeout),
    )
    planned = build_planned_files(config, root, key_plan)
    print_plan(planned, root)
    dry_run = args.dry_run or not args.apply
    if args.diff or dry_run:
        print_diff(planned)

    if dry_run:
        print("Dry run only; no files written and no API registration attempted.")
        return 0

    apply_files(planned)
    print("Wrote startup wizard files.")

    if args.register:
        result = register_with_api(
            config,
            root,
            api_url=args.api_url,
            admin_token=args.admin_token,
            verify_boot=args.verify_boot,
        )
        registered = result.get("project", {}).get("project_key", config["project"]["key"])
        print(f"Registered project through Cortex API: {registered}")
        if result.get("boot") == "ok" and result.get("bootstrap") == "ok":
            print(f"Verified boot/bootstrap for lead agent: {default_agent(config)}")
    else:
        print("Skipped Cortex API registration.")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except WizardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
