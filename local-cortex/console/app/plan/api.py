"""The `plan` feature module — the console-side surface of the Visual Plan capability.

A "visual plan" is a structured `.mdx` document (frontmatter + markdown + diagram /
file-map / annotated-code / wireframe blocks) that an agent authors with the
`visual-plan` skill BEFORE writing code, for a human to review and approve. Unlike
Explain (which generates self-contained HTML rendered in a sandboxed iframe), a plan
is rendered as real MDX by the SPA's `MdxPlanRenderer` — so it is a READ surface over
files that live in the project's working tree under `docs/plans/`.

This module is intentionally thin:

  * `GET /plan/{project}/list`         — enumerate `docs/plans/**/*.mdx` for the project.
  * `GET /plan/{project}/file?path=…`  — return one plan's raw MDX text.
  * `POST /plan/{project}/bootstrap`   — create a handoff asking the project lead to
                                        author the first plan.

Authoring is still just file writes (an agent with the skill, or a human). The
bootstrap endpoint does NOT write files directly; it creates a normal Cortex handoff
so the project lead authors the plan through the same autonomy/dispatch path as any
other project work. Plans live in the repo, so they version with the code and need no
separate store.

`main.py` mounts the router additively (`app.include_router(plan.router)`). Paths are
resolved against the project's `repo_root` (the same Cortex `/projects` field Explain
uses) and HARD-guarded to stay within `<repo_root>/docs/plans/` — no traversal, no
absolute escape, symlinks resolved before the prefix check.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from app import auth as auth_module

router = APIRouter(prefix="/plan", tags=["plan"])

# Plans live here, relative to the project working tree. A single well-known root keeps
# enumeration cheap and the traversal guard simple.
_PLANS_SUBDIR = "docs/plans"
# Cap the walk so a pathological tree can't hang the request.
_MAX_FILES = 500
_DEFAULT_PLAN_OBJECTIVE = (
    "Create the initial project plan, phased roadmap, acceptance criteria, and next handoffs."
)


def _cortex(request: Request):
    """The shared `CortexClient` (the project lookup seam) from app.state."""
    return getattr(request.app.state, "cortex", None)


async def _project_record(request: Request, project: str) -> dict[str, Any] | None:
    """The Cortex project record, or None if unavailable."""
    cortex = _cortex(request)
    if cortex is None:
        return None
    try:
        proj = await cortex.get_project(project)
    except Exception:
        return None
    return proj if isinstance(proj, dict) else None


def _repo_root_from_project(proj: dict[str, Any] | None) -> str:
    if not isinstance(proj, dict):
        return ""
    root = (proj.get("repo_root") or "").strip()  # fitness:allow-literal Cortex API field name
    return root if root.startswith("/") else ""


async def _repo_root(request: Request, project: str) -> str:
    """The project's absolute working folder, or '' if unresolvable. Mirrors the
    resolution Explain uses (the Cortex `/projects` `repo_root` field)."""
    return _repo_root_from_project(await _project_record(request, project))


def _list_plan_items(repo_root: str) -> list[dict[str, Any]]:
    plans_dir = Path(repo_root) / _PLANS_SUBDIR
    if not plans_dir.is_dir():
        return []

    base = plans_dir.resolve()
    items: list[dict[str, Any]] = []
    for fp in sorted(plans_dir.rglob("*.mdx")):
        try:
            real = fp.resolve()
            real.relative_to(base)
        except (OSError, ValueError):
            continue
        try:
            st = fp.stat()
        except OSError:
            continue
        rel = fp.relative_to(plans_dir).as_posix()
        items.append(
            {
                "path": rel,
                "name": fp.name,
                "slug": rel.split("/", 1)[0] if "/" in rel else fp.stem,
                "kind": _kind_for(rel),
                "size": st.st_size,
                "modified_at": st.st_mtime,
            }
        )
        if len(items) >= _MAX_FILES:
            break
    items.sort(key=lambda it: it["modified_at"], reverse=True)
    return items


def _slug(value: Any) -> str:
    clean = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower()).strip("-._")
    return clean[:80] or "project-plan"


async def _resolve_lead(request: Request, project: str, proj: dict[str, Any] | None) -> str:
    """Resolve the project lead with the same default-agent-first shape Explain uses."""
    if isinstance(proj, dict):
        try:
            lead = str(proj.get("default_agent") or "").strip()
            if lead:
                return lead
        except Exception:
            pass
    cortex = _cortex(request)
    if cortex is None:
        return ""
    try:
        from app.agents.api import build_service, get_operational_store

        store = get_operational_store(request)
        roster = await cortex.get_agents(project) or []
        catalog = await build_service(store).list_agents(project, roster)
        return str(catalog.get("lead") or "").strip()
    except Exception:
        return ""


def _kind_for(name: str) -> str:
    """Classify a plan file by its stem so the SPA can tab/group them."""
    stem = name.rsplit("/", 1)[-1].lower()
    for k in ("canvas", "prototype", "recap"):
        if stem == f"{k}.mdx" or stem.startswith(f"{k}."):
            return k
    return "plan"


@router.get("/{project}/list")
async def plan_list(project: str, request: Request) -> JSONResponse:
    """List every `*.mdx` under `<repo_root>/docs/plans/`. Returns
    `{plans: [{path, name, slug, kind, size, modified_at}]}` sorted newest-first.
    A missing repo_root → 400; a missing plans dir → an empty list (not an error)."""
    repo_root = await _repo_root(request, project)
    if not repo_root:
        return JSONResponse(
            {"error": f"project '{project}' has no absolute repo_root configured"},
            status_code=400,
        )
    plans_dir = Path(repo_root) / _PLANS_SUBDIR
    if not plans_dir.is_dir():
        return JSONResponse({"plans": []})
    return JSONResponse({"plans": _list_plan_items(repo_root)})


@router.get("/{project}/status")
async def plan_status(project: str, request: Request) -> JSONResponse:
    """Read-only project plan status for Dashboard/Plan v2 surfaces."""
    proj = await _project_record(request, project)
    repo_root = _repo_root_from_project(proj)
    if not repo_root:
        return JSONResponse(
            {
                "project": project,
                "ready": False,
                "has_repo_root": False,
                "has_plan": False,
                "plan_count": 0,
                "lead": "",
                "recommended_path": f"{_PLANS_SUBDIR}/{_slug(project)}-project-plan/plan.mdx",
                "bootstrap_available": False,
                "reason": f"project '{project}' has no absolute repo_root configured",
            },
            status_code=200,
        )
    plans = _list_plan_items(repo_root)
    lead = await _resolve_lead(request, project, proj)
    plan_items = [item for item in plans if item.get("kind") == "plan"]
    recommended_title = f"{project} project plan"
    return JSONResponse(
        {
            "project": project,
            "ready": bool(plan_items),
            "has_repo_root": True,
            "repo_root": repo_root,
            "has_plan": bool(plan_items),
            "plan_count": len(plans),
            "lead": lead,
            "latest_plan": plan_items[0] if plan_items else (plans[0] if plans else None),
            "recommended_path": f"{_PLANS_SUBDIR}/{_slug(recommended_title)}/plan.mdx",
            "bootstrap_available": bool(lead),
            "reason": None if plan_items else "no project plan found",
        }
    )


@router.get("/{project}/file")
async def plan_file(project: str, request: Request) -> JSONResponse:
    """Return one plan's raw MDX as `{path, text}`. `path` is relative to
    `docs/plans/` and HARD-guarded to stay inside it (realpath prefix check) — any
    traversal/escape, missing file, or non-`.mdx` target → 400/404."""
    rel = (request.query_params.get("path") or "").strip()
    if not rel or not rel.endswith(".mdx"):
        return JSONResponse({"error": "a relative .mdx 'path' is required"}, status_code=400)

    repo_root = await _repo_root(request, project)
    if not repo_root:
        return JSONResponse(
            {"error": f"project '{project}' has no absolute repo_root configured"},
            status_code=400,
        )

    base = (Path(repo_root) / _PLANS_SUBDIR).resolve()
    target = (base / rel).resolve()
    # Containment guard: the resolved target must live under the plans root.
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"error": "path escapes the plans directory"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"error": "plan not found"}, status_code=404)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return JSONResponse({"error": f"could not read plan: {exc}"}, status_code=400)
    return JSONResponse({"path": rel, "text": text})


@router.post("/{project}/bootstrap")
async def bootstrap_plan(
    project: str,
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> JSONResponse:
    """Ask the project's lead to create the first visual plan.

    This is intentionally a handoff creator, not a filesystem writer. The lead agent owns
    the plan content, the plan lands in the project repo, and autonomy/propose mode still
    decides when the handoff runs.
    """
    if not isinstance(body, dict):
        body = {}
    cortex = _cortex(request)
    if cortex is None:
        return JSONResponse(
            {"ok": False, "error": "cortex client is not configured"},
            status_code=503,
        )

    proj = await _project_record(request, project)
    repo_root = _repo_root_from_project(proj)
    if not repo_root:
        return JSONResponse(
            {"ok": False, "error": f"project '{project}' has no absolute repo_root configured"},
            status_code=400,
        )

    lead = await _resolve_lead(request, project, proj)
    if not lead:
        return JSONResponse(
            {"ok": False, "error": "project has no lead/default_agent configured"},
            status_code=400,
        )

    title = str(body.get("title") or f"{project} project plan").strip()
    objective = str(body.get("objective") or _DEFAULT_PLAN_OBJECTIVE).strip()
    target_path = f"{_PLANS_SUBDIR}/{_slug(body.get('slug') or title)}/plan.mdx"
    handoff_body: dict[str, Any] = {
        "to_agent": lead,
        "to_role": "lead",
        "from_role": "system",
        "priority": "high",
        "summary": f"Create project plan: {title}",
        "context": (
            "The Plan tab has no project plan yet. Use the visual-plan skill to author "
            f"`{target_path}` in the project working tree. Keep the plan project-bound: "
            "include goals, current context, phases, acceptance criteria, risks, open "
            "questions, and the next handoffs required to move the project forward."
        ),
        "acceptance": {
            "capability": "visual-plan",
            "target_path": target_path,
            "objective": objective,
            "must_include": [
                "project goals and operating context",
                "phased roadmap",
                "acceptance criteria",
                "risks and assumptions",
                "next handoffs for the project team",
            ],
        },
    }
    result = await cortex.create_handoff(project, lead, handoff_body)
    ok = isinstance(result, dict) and bool(
        result.get("id") or result.get("handoff_id") or result.get("ok")
    )
    return JSONResponse(
        {
            "ok": ok,
            "lead": lead,
            "path": target_path,
            "handoff": result if ok else None,
            "error": None
            if ok
            else (result.get("error") if isinstance(result, dict) else "handoff failed"),
        },
        status_code=200 if ok else 502,
    )


__all__ = ["router"]
