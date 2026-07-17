"""In-console REGISTRATION routes — feature-gap #81 (the SPA's registration backend).

Three additive console routes that wrap the `CortexClient` registration writes so the
SPA's registration UX has a JSON backend: add an agent, deregister an agent, add a
project. They are the WRITE counterparts to the read-only roster/projects surfaces —
the console's in-app way to grow a project's roster (and the project list itself)
without dropping to the `cortex-add-agent` / project-onboard CLIs.

  * POST /agents/{project}/register          → CortexClient.create_agent
        body: {name, role, harness?, model?, reasoning?, designation?, auto_dispatch?,
               writer_scope?, role_description?}  — the config fields fold into `capabilities`.
  * POST /agents/{project}/{agent}/deregister → CortexClient.remove_agent  (admin)
  * GET  /project-packs?repo_root=/abs/path  → discover installed packs
  * POST /projects/register                  → CortexClient.create_project (admin)
        body: {project_key, display_name?, repo_root, repo_type?, default_agent?,
               project_pack_key?}

HOUSE LAW — every route GRACEFUL-DEGRADES + is TOKEN-SAFE:
  * input is validated client-side first (blank name/role/key, non-absolute repo_root)
    → a friendly `ok=false` + a clear `error` WITHOUT touching Cortex;
  * the write itself is best-effort — a degraded write (None/False from the client,
    which already swallows transport/4xx/5xx) becomes a soft `ok=false` + a friendly,
    NON-LEAKY error (the admin token is NEVER in the response, mirroring the workspace
    editor's friendly-error pattern);
  * never a 500.

The concrete `CortexClient` is resolved at a `Depends` seam (`app.state.cortex`) so
the handlers can be driven directly with a fake in tests (the settings_module idiom).
This module is the ONLY registration fastapi importer; `main.py` mounts it additively
(`app.include_router(registration_api.router)`). The distinct `/agents/{project}/
register` + `/agents/{project}/{agent}/deregister` + `/projects/register` shapes carry
a LITERAL trailing segment, so they can't shadow the live `GET /agents/{project}` /
`POST /agents/{p}/{a}/config` / `GET /projects` routes (different trailing literal or
method).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from . import auth as auth_module
from . import seed_personas
from .portal_contract import contract_for_stream

router = APIRouter(tags=["registration"])

# The standard onboarding Lead (the FIRST AI worker the operator chats with on a
# brand-new project). The default name is role-based and neutral; deployments can
# still pass lead_name when they want a branded first worker.
_LEAD_ROW = next(
    (r for r in seed_personas.DEFAULT_ROSTER if r.get("agent") == "lead"),
    {"role": "lead", "designation": "interactive"},
)
_ONBOARDING_LEAD = {
    "agent": "lead",
    "role": str(_LEAD_ROW.get("role") or "lead"),
    "designation": str(_LEAD_ROW.get("designation") or "interactive"),
}

# The config fields a register form can set; they fold into the registry
# `capabilities` blob (capability FIELD names — not project keys / agent names).
_AGENT_CONFIG_FIELDS = ("harness", "model", "reasoning", "designation", "auto_dispatch")
_PACK_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_PROJECT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_REGISTRY_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_EPHEMERAL_AGENT_RE = re.compile(r"^(?:claude-subagent-[a-f0-9]{6,20}|codex-agent)$")
_BLOCKED_AGENT_NAMES = frozenset({
    "actually", "adding", "an", "and", "are", "auditing", "building", "but",
    "changing", "create", "deploying", "doing", "editing", "fixing", "for",
    "here", "immediately", "implementing", "in", "instead", "invested",
    "is", "it", "looking", "missing", "not", "now", "of", "on", "or",
    "project", "rebuilding", "researching", "reviewing", "running", "sending",
    "still", "system", "team", "the", "there", "this", "to", "tracing",
    "trying", "using", "was", "with", "working", "writing", "you",
})
_PROJECT_PACKS_DIR = Path(".kaidera-os") / "project-packs"
_EXTENSION_PATHS_ENV = "KAIDERA_OS_EXTENSION_PATHS"
_ORCHESTRATOR_ROLE = "orchestrator"


def _project_pack_dirs(repo_root: str) -> tuple[Path, ...]:
    root = Path(repo_root)
    return (root / _PROJECT_PACKS_DIR,)


async def _capacity_block(kind: str, project: str, cortex: Any, *, subject: str | None = None) -> str | None:
    """Community source has no capacity gate."""
    _ = (kind, project, cortex, subject)
    return None


def get_cortex(request: Request):
    """Resolve the Cortex client for the registration writes — `app.state.cortex`,
    or None if not wired (degraded). Resolved at this seam so the handlers can be
    driven directly with a fake (the module test idiom)."""
    return getattr(request.app.state, "cortex", None)


def _clean(value: Any) -> str:
    """A stripped string, or '' for None/blank. Total + pure."""
    return str(value).strip() if value is not None else ""


def _normalise_project_key(value: Any) -> tuple[str, str | None]:
    key = _clean(value).lower()
    if not key:
        return "", "A project key is required."
    if not _PROJECT_KEY_RE.fullmatch(key):
        return key, "Project key must use lowercase letters, digits, and hyphens only."
    return key, None


def _normalise_agent_name_for_project(value: Any, project: str) -> tuple[str, str | None]:
    name = _clean(value).lower()
    project_key = _clean(project).lower()
    if not name:
        return "", "An agent name is required."
    if "@" in name:
        base, suffix = name.split("@", 1)
        suffix_key = re.sub(r"[\s_]+", "-", suffix.strip())
        if suffix_key != project_key:
            return name, (
                f"Agent '{name}' belongs to project '{suffix}', but this registration "
                f"is scoped to '{project_key}'."
            )
        name = base
    if name in _BLOCKED_AGENT_NAMES:
        return name, f"'{name}' is not a valid AI worker name."
    if _EPHEMERAL_AGENT_RE.fullmatch(name):
        return name, "Transient harness worker IDs cannot be added to a project roster."
    if not _REGISTRY_AGENT_RE.fullmatch(name):
        return name, "Agent names must match [a-z][a-z0-9_-]{1,31}."
    return name, None


def _capabilities_from(payload: dict[str, Any]) -> dict[str, Any]:
    """Fold the register form's config fields (harness/model/reasoning/designation/auto_dispatch)
    into a `capabilities` dict — only the NON-blank ones (so we never push an empty
    value that would be meaningless on the registry)."""
    caps: dict[str, Any] = {}
    for field in _AGENT_CONFIG_FIELDS:
        val = _clean(payload.get(field))
        if val:
            caps[field] = val
    return caps


def _is_orchestrator_role(role: Any) -> bool:
    return str(role or "").strip().lower() == _ORCHESTRATOR_ROLE


def _override_agent_from_key(project: str, key: str) -> str | None:
    prefix = f"{(project or '').strip().lower()}:"
    raw = str(key or "").strip().lower()
    if not raw.startswith(prefix):
        return None
    return raw[len(prefix):].strip() or None


def _agent_role_override(project: str, agent: str) -> str | None:
    try:
        from . import settings as settings_store

        entry = settings_store.get_agent_override(project, agent)
    except Exception:
        return None
    if not isinstance(entry, dict) or "role" not in entry:
        return None
    return str(entry.get("role") or "")


def _project_role_overrides(project: str) -> dict[str, str]:
    try:
        from . import settings as settings_store

        overrides = settings_store.load_agent_overrides()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for key, entry in (overrides or {}).items():
        agent = _override_agent_from_key(project, str(key))
        if agent and isinstance(entry, dict) and "role" in entry:
            out[agent] = str(entry.get("role") or "")
    return out


async def _orchestrator_registration_conflict(
    cortex: Any,
    project: str,
    target_agent: str,
    target_role: str,
) -> str | None:
    """Return the existing orchestrator name if this registration would create two."""
    if not _is_orchestrator_role(target_role):
        return None
    target = _clean(target_agent).lower()

    overrides = _project_role_overrides(project)
    for name, role in overrides.items():
        if name != target and _is_orchestrator_role(role):
            return name

    getter = getattr(cortex, "get_agents", None)
    if not callable(getter):
        getter = getattr(cortex, "get_roster", None)
    if not callable(getter):
        return None
    try:
        roster = await getter(project)
    except Exception:
        return None

    for row in roster or []:
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("name")).lower()
        if not name or name == target:
            continue
        role = overrides.get(name)
        if role is None:
            role = _agent_role_override(project, name)
        effective_role = role if role is not None else row.get("role")
        if _is_orchestrator_role(effective_role):
            return name
    return None


def _is_safe_relative_path(value: Any) -> bool:
    """True for non-blank relative paths/globs that cannot escape the pack root."""
    if not isinstance(value, str) or not value.strip():
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _is_under(root: Path, path: Path) -> bool:
    """Return whether `path` resolves under `root` (symlink-safe best effort)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_pack_manifest(manifest_path: Path) -> dict[str, Any] | None:
    """Load one installed project-pack manifest.

    The redistributable validator remains the strict contract. Runtime discovery
    is intentionally defensive so a half-installed pack cannot break Add Project.
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    pack = data.get("pack")
    if not isinstance(pack, dict):
        return None
    key = _clean(pack.get("key"))
    name = _clean(pack.get("name"))
    version = _clean(pack.get("version"))
    if not key or not _PACK_KEY_RE.match(key) or not name or not version:
        return None
    return data


def _pack_seed_files(pack_root: Path, manifest: dict[str, Any]) -> list[str]:
    """Return installed Cortex seed files as pack-relative paths."""
    rels: set[str] = set()
    install = manifest.get("install") if isinstance(manifest.get("install"), dict) else {}
    seed_glob = install.get("cortex_seed_glob") if isinstance(install, dict) else None
    if _is_safe_relative_path(seed_glob):
        try:
            for candidate in pack_root.glob(str(seed_glob)):
                if candidate.is_file() and _is_under(pack_root, candidate):
                    rels.add(candidate.relative_to(pack_root).as_posix())
        except (OSError, ValueError):
            pass

    assets = manifest.get("assets") if isinstance(manifest.get("assets"), list) else []
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("type") != "cortex_seed":
            continue
        rel = asset.get("path")
        if not _is_safe_relative_path(rel):
            continue
        candidate = pack_root / str(rel)
        if candidate.is_file() and _is_under(pack_root, candidate):
            rels.add(Path(str(rel)).as_posix())
    return sorted(rels)


def _extension_env_name(manifest: dict[str, Any]) -> str:
    install = manifest.get("install") if isinstance(manifest.get("install"), dict) else {}
    name = _clean(install.get("enable_extensions_env")) if isinstance(install, dict) else ""
    return name or "KAIDERA_OS_EXTENSION_MODULES"


def _split_module_list(raw: str | None) -> list[str]:
    """Comma-separated module list, stable unique order."""
    seen: set[str] = set()
    modules: list[str] = []
    for part in str(raw or "").split(","):
        module = part.strip()
        if not module or module in seen:
            continue
        seen.add(module)
        modules.append(module)
    return modules


def _read_extension_env_modules(pack_root: Path, manifest: dict[str, Any]) -> list[str]:
    """Read enabled modules from the pack-local extensions.env helper."""
    path = pack_root / "extensions.env"
    env_name = _extension_env_name(manifest)
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() != env_name:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            return _split_module_list(value)
    except OSError:
        return []
    return []


def _write_extension_env_modules(
    pack_root: Path,
    manifest: dict[str, Any],
    modules: list[str],
) -> Path:
    """Write the pack-local extensions.env helper file."""
    env_name = _extension_env_name(manifest)
    cleaned = _split_module_list(",".join(modules))
    dest = pack_root / "extensions.env"
    dest.write_text(
        "# Generated by Kaidera OS project-pack extension control.\n"
        "# Restart the console after changing this file, then source/copy this value\n"
        "# into the deployment environment if your service manager does not do so.\n"
        f"{env_name}={','.join(cleaned)}\n"
        f"{_EXTENSION_PATHS_ENV}={shlex.quote(str(pack_root.resolve()))}\n",
        encoding="utf-8",
    )
    return dest


def _loaded_extension_modules(manifest: dict[str, Any]) -> set[str]:
    """Modules named in the live console process env for this pack."""
    return set(_split_module_list(os.environ.get(_extension_env_name(manifest))))


def _pack_portals(pack_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return safe portal health metadata from the installed pack manifest."""
    portals = manifest.get("portals") if isinstance(manifest.get("portals"), list) else []
    rows: list[dict[str, Any]] = []
    for portal in portals:
        if not isinstance(portal, dict):
            continue
        key = _clean(portal.get("key"))
        route_prefix = _clean(portal.get("route_prefix"))
        if not key or not route_prefix:
            continue
        frontend_path = _clean(portal.get("frontend_path")) or None
        frontend_exists = False
        if frontend_path and _is_safe_relative_path(frontend_path):
            candidate = pack_root / frontend_path
            frontend_exists = candidate.is_file() and _is_under(pack_root, candidate)
        required = bool(portal.get("required", False))
        if frontend_path and frontend_exists:
            status = "ready"
        elif frontend_path and required:
            status = "missing_frontend"
        elif frontend_path:
            status = "frontend_not_installed"
        else:
            status = "metadata_only"
        agent = _clean(portal.get("agent")) or None
        stream_contract = _clean(portal.get("stream_contract")) or None
        rows.append({
            "key": key,
            "type": _clean(portal.get("type")) or None,
            "agent": agent,
            "route_prefix": route_prefix,
            "auth": _clean(portal.get("auth")) or None,
            "stream_contract": stream_contract,
            "runtime_contract": contract_for_stream(stream_contract, agent=agent),
            "frontend_path": frontend_path,
            "frontend_exists": frontend_exists,
            "required": required,
            "description": _clean(portal.get("description")) or None,
            "status": status,
        })
    return rows


def _pack_option(pack_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    pack = manifest["pack"]
    project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
    extensions = manifest.get("extensions") if isinstance(manifest.get("extensions"), list) else []
    enabled_modules = set(_read_extension_env_modules(pack_root, manifest))
    loaded_modules = _loaded_extension_modules(manifest)
    module_rows: list[dict[str, Any]] = []
    for ext in extensions:
        if not isinstance(ext, dict):
            continue
        module = str(ext.get("module") or "").strip()
        if not module:
            continue
        enabled = module in enabled_modules
        loaded = module in loaded_modules
        if enabled and loaded:
            status = "loaded"
        elif enabled and not loaded:
            status = "enabled_restart_required"
        elif loaded and not enabled:
            status = "loaded_disable_restart_required"
        else:
            status = "disabled"
        module_rows.append({
            "module": module,
            "required": bool(ext.get("required", False)),
            "description": _clean(ext.get("description")) or None,
            "enabled": enabled,
            "loaded": loaded,
            "status": status,
            "restart_required": enabled != loaded,
        })
    default_key = _clean(project.get("default_key")) if isinstance(project, dict) else ""
    seed_files = _pack_seed_files(pack_root, manifest)
    portal_rows = _pack_portals(pack_root, manifest)
    restart_required = any(bool(row["restart_required"]) for row in module_rows)
    return {
        "key": str(pack["key"]),
        "name": str(pack["name"]),
        "version": str(pack["version"]),
        "description": _clean(pack.get("description")) or None,
        "default_project_key": default_key or None,
        "seed_files": seed_files,
        "seed_count": len(seed_files),
        "extension_modules": [row["module"] for row in module_rows],
        "extensions": module_rows,
        "extensions_enabled": sorted(enabled_modules),
        "extension_env": _extension_env_name(manifest),
        "extension_paths_env": _EXTENSION_PATHS_ENV,
        "extension_path": str(pack_root.resolve()),
        "portals": portal_rows,
        "restart_required": restart_required,
    }


def _installed_project_pack_options(repo_root: str) -> list[dict[str, Any]]:
    options: dict[str, dict[str, Any]] = {}
    for packs_dir in _project_pack_dirs(repo_root):
        if not packs_dir.is_dir():
            continue
        try:
            pack_dirs = sorted(path for path in packs_dir.iterdir() if path.is_dir())
        except OSError:
            continue
        for pack_root in pack_dirs:
            if not _is_under(packs_dir, pack_root):
                continue
            manifest = _read_pack_manifest(pack_root / "project-pack.json")
            if manifest is None:
                continue
            option = _pack_option(pack_root, manifest)
            options.setdefault(str(option.get("key") or ""), option)
    return sorted(options.values(), key=lambda p: str(p.get("key") or ""))


def _find_installed_project_pack(repo_root: str, pack_key: str) -> dict[str, Any] | None:
    key = _clean(pack_key)
    if not key or not _PACK_KEY_RE.match(key):
        return None
    for packs_dir in _project_pack_dirs(repo_root):
        pack_root = packs_dir / key
        if not pack_root.is_dir() or not _is_under(packs_dir, pack_root):
            continue
        manifest = _read_pack_manifest(pack_root / "project-pack.json")
        if manifest is None:
            continue
        option = _pack_option(pack_root, manifest)
        if option.get("key") == key:
            return {"root": pack_root, "manifest": manifest, "option": option}
    return None


async def _ingest_project_pack_seed(
    cortex: Any,
    project_key: str,
    installed_pack: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort Cortex seed import for one installed project pack."""
    option = dict(installed_pack["option"])
    pack_root = installed_pack["root"]
    seed_files = list(option.get("seed_files") or [])
    result: dict[str, Any] = {
        "key": option.get("key"),
        "name": option.get("name"),
        "seed_files": seed_files,
        "seed_count": len(seed_files),
        "ingested": 0,
        "errors": [],
    }
    if not hasattr(cortex, "ingest_knowledge"):
        if seed_files:
            result["errors"].append("Cortex client does not expose knowledge ingest.")
        return result

    for rel in seed_files:
        candidate = pack_root / rel
        if not candidate.is_file() or not _is_under(pack_root, candidate):
            result["errors"].append(f"Seed file unavailable: {rel}")
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError:
            result["errors"].append(f"Could not read seed file: {rel}")
            continue
        source_file = f"{_PROJECT_PACKS_DIR.as_posix()}/{option['key']}/{rel}"
        try:
            write = await cortex.ingest_knowledge(
                project_key,
                content=content,
                source_file=source_file,
                category="project-pack",
                section=f"{option['name']} seed",
                on_conflict="update",
            )
        except Exception:
            write = None
        if write:
            result["ingested"] += 1
        else:
            result["errors"].append(f"Cortex rejected seed file: {rel}")
    return result


# ---------------------------------------------------------------------------
#  POST /agents/{project}/register → create_agent  (caller/writer-gated)
# ---------------------------------------------------------------------------


@router.post("/agents/{project}/register")
async def register_agent_route(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    cortex: Any = Depends(get_cortex),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    """`POST /agents/{project}/register` — register/UPSERT one agent on the project's
    roster via `CortexClient.create_agent` (`POST /agents`). The console's caller
    the configured console service identity gates the write; the new agent is the
    SUBJECT. The config fields fold into `capabilities`. Echoes `{ok, agent, role,
    error}` — a degraded write (the caller isn't a registered writer, or Cortex is
    unreachable) is a friendly `ok=false`. Never a 500; never leaks a token."""
    project_key = _clean(project).lower()
    name, name_err = _normalise_agent_name_for_project(payload.get("name"), project_key)
    role = _clean(payload.get("role"))
    if name_err:
        return {"ok": False, "agent": name or None, "role": None, "error": name_err}
    if not role:
        return {"ok": False, "agent": name, "role": None, "error": "A role is required."}
    if cortex is None:
        return {"ok": False, "agent": name, "role": role,
                "error": "Cortex is unavailable — couldn't reach the registry to add the agent."}

    conflict = await _orchestrator_registration_conflict(cortex, project_key, name, role)
    if conflict:
        return {
            "ok": False,
            "agent": name,
            "role": role,
            "error": (
                f"Only one deterministic orchestrator is allowed per project. "
                f"Unset '{conflict}' as orchestrator before assigning '{name}'."
            ),
        }

    cap_err = await _capacity_block("workers", project, cortex, subject=name)
    if cap_err:
        return {"ok": False, "agent": name, "role": role, "error": cap_err}

    capabilities = _capabilities_from(payload)
    writer_scope = _clean(payload.get("writer_scope")) or None
    role_description = _clean(payload.get("role_description")) or None
    try:
        result = await cortex.create_agent(
            project_key,
            name=name,
            role=role,
            capabilities=capabilities,
            writer_scope=writer_scope,
            role_description=role_description,
        )
    except Exception:  # the client graceful-degrades to None; belt-and-braces here
        result = None

    if not result:
        return {
            "ok": False,
            "agent": name,
            "role": role,
            "error": (
                "Cortex didn't register the agent. The console's writer may not be "
                "authorised to add agents on this project, or Cortex is unreachable."
            ),
        }
    return {
        "ok": True,
        "agent": result.get("agent") or name if isinstance(result, dict) else name,
        "role": result.get("role") or role if isinstance(result, dict) else role,
        "error": None,
    }


# ---------------------------------------------------------------------------
#  POST /agents/{project}/{agent}/deregister → remove_agent  (admin-gated)
# ---------------------------------------------------------------------------


@router.post("/agents/{project}/{agent}/deregister")
async def deregister_agent_route(
    project: str,
    agent: str,
    cortex: Any = Depends(get_cortex),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    """`POST /agents/{project}/{agent}/deregister` — remove (deactivate) an agent from
    the project's roster via `CortexClient.remove_agent` (`POST /admin/agents/remove`,
    ADMIN-token gated). History is preserved (roster-only deactivation). Echoes
    `{ok, removed, agent, error}`; a failure (admin token missing / Cortex rejected /
    unreachable) is a friendly `ok=false` nudging the admin-token requirement (never
    the token value itself). Never a 500."""
    name = _clean(agent)
    if not name:
        return {"ok": False, "removed": False, "agent": name,
                "error": "An agent name is required to deregister."}
    if cortex is None:
        return {"ok": False, "removed": False, "agent": name,
                "error": "Cortex is unavailable — couldn't reach the registry."}

    try:
        ok = await cortex.remove_agent(_clean(project), name)
    except Exception:  # the client graceful-degrades to False; belt-and-braces here
        ok = False

    if not ok:
        return {
            "ok": False,
            "removed": False,
            "agent": name,
            "error": (
                "Couldn't deregister the agent. Removing an agent needs the Cortex "
                "admin token configured (CORTEX_ADMIN_TOKEN in the environment or "
                "local-cortex/.env), and a reachable Cortex."
            ),
        }
    return {"ok": True, "removed": True, "agent": name, "error": None}


# ---------------------------------------------------------------------------
#  POST /projects/register → create_project  (admin-gated)
# ---------------------------------------------------------------------------


def _lead_persona_brief(project: str, lead_name: str, scope: str) -> str | None:
    """Build the lead worker's STARTING persona brief from the project SCOPE (the operator's
    one-line description). The lead's role/skills/team are then shaped from this brief plus the
    first conversation — so "the role comes from the project scope" is real on day one. Returns
    None when no scope was given (the lead falls back to the generic lead role)."""
    scope = _clean(scope)
    if not scope:
        return None
    name = _clean(lead_name) or str(_ONBOARDING_LEAD["agent"])
    return (
        f"You are {name}, the lead worker for the '{_clean(project)}' project. The operator's "
        f"initial scope for this project: {scope} Use this scope and your first conversation with "
        f"the operator to shape your own role, persona, and the skills + team this project needs "
        f"to deliver it."
    )


def _onboarding_lead_agent(project: str, lead_name: str | None = None, scope: str | None = None) -> dict[str, Any]:
    """Build the first-worker spec for POST /projects.

    Fresh project setup is admin-gated. The first lead worker is therefore
    included in the project-registration payload instead of created with a later
    writer-gated POST /agents call.
    """
    name = _clean(lead_name) or str(_ONBOARDING_LEAD["agent"])
    capabilities: dict[str, Any] = {
        "harness": "kaidera",  # fitness:allow-literal canonical harness id (own-harness runtime), not a per-project literal
        "designation": str(_ONBOARDING_LEAD["designation"]),
    }
    persona_brief = _lead_persona_brief(project, name, scope or "")
    if persona_brief:
        capabilities["persona_brief"] = persona_brief
    return {
        "name": name,
        "role": str(_ONBOARDING_LEAD["role"]),
        "capabilities": capabilities,
    }


@router.get("/project-packs")
async def list_project_packs_route(
    repo_root: str = Query(default=""),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    """Discover project packs already installed under one project root.

    The installer writes packs into `.kaidera-os/project-packs/<pack>/`. This route
    only reads that project-owned directory and returns safe metadata for the Add
    Project modal; it never imports pack code.
    """
    root = _clean(repo_root)
    if not root:
        return {"ok": True, "packs": [], "error": None}
    if not root.startswith("/"):
        return {
            "ok": False,
            "packs": [],
            "error": "The project folder (repo_root) must be an absolute path.",
        }
    return {"ok": True, "packs": _installed_project_pack_options(root), "error": None}


@router.post("/project-packs/extensions")
async def set_project_pack_extension_route(
    payload: dict[str, Any] = Body(default_factory=dict),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    """Enable/disable one manifest-declared extension module for an installed pack.

    This updates the pack-local `extensions.env` helper only. Loading/unloading
    Python modules still requires the deployment to restart the console with the
    corresponding `KAIDERA_OS_EXTENSION_MODULES` value, which the response reports as
    `restart_required`.
    """
    root = _clean(payload.get("repo_root"))
    pack_key = _clean(payload.get("pack_key"))
    module = _clean(payload.get("module"))
    enabled = bool(payload.get("enabled"))
    if not root or not root.startswith("/"):
        return {"ok": False, "pack": None, "error": "The project folder (repo_root) must be an absolute path."}
    if not pack_key:
        return {"ok": False, "pack": None, "error": "A project pack key is required."}
    if not module:
        return {"ok": False, "pack": None, "error": "An extension module is required."}
    installed_pack = _find_installed_project_pack(root, pack_key)
    if installed_pack is None:
        return {"ok": False, "pack": None, "error": f"Project pack '{pack_key}' is not installed in this project folder."}

    manifest = installed_pack["manifest"]
    pack_root = installed_pack["root"]
    declared = set(installed_pack["option"].get("extension_modules") or [])
    if module not in declared:
        return {"ok": False, "pack": None, "error": f"Extension module '{module}' is not declared by pack '{pack_key}'."}

    modules = _read_extension_env_modules(pack_root, manifest)
    if enabled and module not in modules:
        modules.append(module)
    elif not enabled:
        modules = [m for m in modules if m != module]
    try:
        _write_extension_env_modules(pack_root, manifest, modules)
    except OSError:
        return {"ok": False, "pack": None, "error": "Could not update the pack extension helper file."}

    refreshed = _find_installed_project_pack(root, pack_key)
    pack = refreshed["option"] if refreshed is not None else None
    return {"ok": True, "pack": pack, "error": None}


@router.post("/projects/register")
async def register_project_route(
    payload: dict[str, Any] = Body(default_factory=dict),
    cortex: Any = Depends(get_cortex),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    """`POST /projects/register` — register/update a Cortex project via
    `CortexClient.create_project` (`POST /projects`, ADMIN-token gated). The
    `project_key` is required and the `repo_root` (working folder) must be ABSOLUTE
    (same rule as the workspace editor). Echoes `{ok, project_key, error}`; a degraded
    write (admin token missing / Cortex unreachable / rejected) is a friendly
    `ok=false` nudging the admin-token requirement (never the token). Never a 500."""
    key, key_err = _normalise_project_key(payload.get("project_key"))
    new_root = _clean(payload.get("repo_root"))  # fitness:allow-literal Cortex field name (the `root` agent-name pattern is a false positive)
    if key_err:
        return {"ok": False, "project_key": key or None, "error": key_err}
    if new_root and not new_root.startswith("/"):
        return {"ok": False, "project_key": key,
                "error": "The project folder (repo_root) must be an absolute path."}
    if cortex is None:
        return {"ok": False, "project_key": key,
                "error": "Cortex is unavailable — couldn't reach the registry to add the project."}

    pack_key = _clean(payload.get("project_pack_key"))
    installed_pack = None
    if pack_key:
        if not new_root:
            return {
                "ok": False,
                "project_key": key,
                "error": "A project folder is required before selecting a project pack.",
            }
        installed_pack = _find_installed_project_pack(new_root, pack_key)
        if installed_pack is None:
            return {
                "ok": False,
                "project_key": key,
                "error": f"Project pack '{pack_key}' is not installed in this project folder.",
            }

    display_name = _clean(payload.get("display_name")) or None
    description = _clean(payload.get("description")) or None  # project scope → lead persona
    lead_name_raw = _clean(payload.get("lead_name")) or None
    lead_name = None
    if lead_name_raw:
        lead_name, lead_err = _normalise_agent_name_for_project(lead_name_raw, key)
        if lead_err:
            return {"ok": False, "project_key": key, "error": lead_err}
    repo_type = _clean(payload.get("repo_type")) or None
    lead_agent = _onboarding_lead_agent(key, lead_name=lead_name, scope=description)
    default_agent = _clean(payload.get("default_agent")) or str(lead_agent["name"])

    cap_err = await _capacity_block("projects", key, cortex, subject=key)
    if cap_err:
        return {"ok": False, "project_key": key, "error": cap_err}

    try:
        result = await cortex.create_project(
            project_key=key,
            display_name=display_name,
            repo_root=new_root or None,
            repo_type=repo_type,
            default_agent=default_agent,
            agents=[lead_agent],
        )
    except Exception:  # the client graceful-degrades to None; belt-and-braces here
        result = None

    if not result:
        return {
            "ok": False,
            "project_key": key,
            "error": (
                "Cortex didn't register the project. Adding a project needs the Cortex "
                "admin token configured (CORTEX_ADMIN_TOKEN in the environment or "
                "local-cortex/.env), and a reachable Cortex."
            ),
        }
    registered_key = result.get("project_key") or key if isinstance(result, dict) else key
    pack_result = None
    if installed_pack is not None:
        pack_result = await _ingest_project_pack_seed(cortex, str(registered_key), installed_pack)
    return {
        "ok": True,
        "project_key": registered_key,
        "lead_seeded": True,
        "lead_name": str(lead_agent["name"]),
        "project_pack": pack_result,
        "error": None,
    }


__all__ = [
    "router",
    "register_agent_route",
    "deregister_agent_route",
    "list_project_packs_route",
    "set_project_pack_extension_route",
    "register_project_route",
    "get_cortex",
]
