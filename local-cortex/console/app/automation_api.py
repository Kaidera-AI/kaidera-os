"""Automation API — durable scheduled jobs.

The API stores trigger definitions in the app-DB and emits Cortex handoffs.
It does not run agents directly. Existing project autonomy/propose-mode/agent
auto-dispatch settings decide what happens after a handoff is created.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from . import auth as auth_module
from . import automation_feed

router = APIRouter(prefix="/automation", tags=["automation"])


def _appdb(request: Request) -> Any:
    return getattr(request.app.state, "appdb", None)


def _cortex(request: Request) -> Any:
    return getattr(request.app.state, "cortex", None)


def _clean_project(project: str) -> str:
    return (project or "").strip().lower()


def _clean_agent(value: Any) -> str:
    return str(value or "").strip().lower()


def _dt(value: Any) -> datetime | None:
    return automation_feed.parse_dt(value)


async def _resolve_planning_worker(
    cortex: Any,
    *,
    project_key: str,
    preferred_agent: str = "",
) -> tuple[str, str]:
    """Return `(from_agent, to_role)` for a PM planning beat.

    Prefer a configured PM AI worker. Fall back to the project default/lead so a
    project without a PM can still create a planning beat for its lead.
    """
    return await automation_feed.resolve_planning_worker(
        cortex,
        project_key=project_key,
        preferred_agent=_clean_agent(preferred_agent),
    )


async def _save_scheduled_job_definition(
    appdb: Any,
    *,
    project_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    name = str(payload.get("name") or payload.get("id") or "scheduled job").strip()
    job_id = automation_feed.clean_job_id(str(payload.get("id") or name))
    schedule = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}
    handoff = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    if not project_key or not job_id:
        return {"ok": False, "error": "project and id are required"}
    if not isinstance(schedule, dict) or not schedule:
        return {"ok": False, "error": "schedule is required"}
    from_agent, body = automation_feed.handoff_payload_from_job({"payload": handoff})
    if not from_agent or not body:
        return {
            "ok": False,
            "error": "payload requires from_agent, to_role, and summary",
        }
    next_run_at = _dt(payload.get("next_run_at")) or automation_feed.initial_next_run(
        schedule, datetime.now(timezone.utc)
    )
    row = await appdb.upsert_scheduled_job(
        project=project_key,
        job_id=job_id,
        name=name,
        enabled=bool(payload.get("enabled", True)),
        schedule=schedule,
        payload=handoff,
        next_run_at=next_run_at,
    )
    if row is None:
        return {"ok": False, "error": "scheduled job could not be saved"}
    return {"ok": True, "job": row}


@router.get("/{project}/scheduled-jobs")
async def list_scheduled_jobs(project: str, request: Request) -> dict[str, Any]:
    appdb = _appdb(request)
    if appdb is None:
        return {"jobs": [], "connected": False}
    jobs = await appdb.list_scheduled_jobs(_clean_project(project))
    return {"jobs": jobs, "connected": True}


@router.get("/{project}/planning-beat")
async def planning_beat_status(project: str, request: Request) -> dict[str, Any]:
    appdb = _appdb(request)
    cortex = _cortex(request)
    project_key = _clean_project(project)
    from_agent, to_role = await _resolve_planning_worker(cortex, project_key=project_key)
    recommended = {
        "from_agent": from_agent,
        "to_agent": from_agent,
        "to_role": to_role,
        "every_minutes": 240,
        # Hybrid PM planner: the beat decomposes the active epic via this skill,
        # it is no longer a generic "review the plan" prompt.
        "mode": automation_feed.PLANNING_MODE,
        "skill": automation_feed.PLANNING_SKILL,
    }
    if appdb is None:
        return {
            "connected": False,
            "configured": False,
            "job": None,
            "recommended": recommended,
        }
    jobs = await appdb.list_scheduled_jobs(project_key)
    job = next((j for j in jobs if j.get("id") == "pm-planning-beat"), None)
    return {
        "connected": True,
        "configured": job is not None,
        "job": job,
        "recommended": recommended,
    }


@router.post("/{project}/planning-beat")
async def upsert_planning_beat(
    project: str,
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    appdb = _appdb(request)
    cortex = _cortex(request)
    if appdb is None:
        return {"ok": False, "error": "app-DB unavailable"}
    project_key = _clean_project(project)
    from_agent, default_to_role = await _resolve_planning_worker(
        cortex,
        project_key=project_key,
        preferred_agent=str(payload.get("from_agent") or payload.get("planner_agent") or ""),
    )
    if not from_agent:
        return {"ok": False, "error": "no PM or lead worker is configured for this project"}
    try:
        every_minutes = int(payload.get("every_minutes") or 240)
    except Exception:
        every_minutes = 240
    definition = automation_feed.pm_planning_schedule_payload(
        project=project_key,
        from_agent=from_agent,
        to_role=str(payload.get("to_role") or default_to_role or "pm"),
        to_agent=str(payload.get("to_agent") or payload.get("planner_agent") or from_agent),
        every_minutes=every_minutes,
        summary=str(payload.get("summary") or "").strip() or None,
    )
    definition["enabled"] = bool(payload.get("enabled", True))
    return await _save_scheduled_job_definition(appdb, project_key=project_key, payload=definition)


@router.post("/{project}/scheduled-jobs")
async def upsert_scheduled_job(
    project: str,
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    appdb = _appdb(request)
    if appdb is None:
        return {"ok": False, "error": "app-DB unavailable"}
    project_key = _clean_project(project)
    return await _save_scheduled_job_definition(appdb, project_key=project_key, payload=payload)


@router.delete("/{project}/scheduled-jobs/{job_id}")
async def delete_scheduled_job(
    project: str,
    job_id: str,
    request: Request,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    appdb = _appdb(request)
    if appdb is None:
        return {"ok": False, "deleted": False, "error": "app-DB unavailable"}
    clean_id = automation_feed.clean_job_id(job_id)
    delete = getattr(appdb, "delete_scheduled_job", None)
    if not callable(delete):
        return {"ok": False, "deleted": False, "error": "delete is not supported by this app-DB"}
    deleted = bool(await delete(project=_clean_project(project), job_id=clean_id))
    return {"ok": deleted, "deleted": deleted, "id": clean_id, "error": None if deleted else "scheduled job not found"}


@router.post("/{project}/scheduled-jobs/{job_id}/run-now")
async def run_scheduled_job_now(
    project: str,
    job_id: str,
    request: Request,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    appdb = _appdb(request)
    if appdb is None:
        return {"ok": False, "error": "app-DB unavailable"}
    jobs = await appdb.list_scheduled_jobs(_clean_project(project))
    row = next((j for j in jobs if j.get("id") == automation_feed.clean_job_id(job_id)), None)
    if row is None:
        return {"ok": False, "error": "scheduled job not found"}
    saved = await appdb.upsert_scheduled_job(
        project=_clean_project(project),
        job_id=str(row["id"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        schedule=row.get("schedule") or {},
        payload=row.get("payload") or {},
        next_run_at=datetime.now(timezone.utc),
    )
    return {"ok": saved is not None, "job": saved}


@router.get("/{project}/feeders/export")
async def export_automation_feeders(project: str, request: Request) -> dict[str, Any]:
    appdb = _appdb(request)
    project_key = _clean_project(project)
    if appdb is None:
        return {
            "project": project_key,
            "version": 1,
            "scheduled_jobs": [],
            "connected": False,
        }
    return {
        "project": project_key,
        "version": 1,
        "scheduled_jobs": await appdb.list_scheduled_jobs(project_key),
        "connected": True,
    }


@router.post("/{project}/feeders/import")
async def import_automation_feeders(
    project: str,
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    appdb = _appdb(request)
    project_key = _clean_project(project)
    if appdb is None:
        return {"ok": False, "error": "app-DB unavailable", "imported": {"scheduled_jobs": 0}, "errors": []}
    jobs = payload.get("scheduled_jobs") or payload.get("jobs") or []
    if not isinstance(jobs, list):
        return {"ok": False, "error": "scheduled_jobs must be an array", "imported": {"scheduled_jobs": 0}, "errors": []}

    imported = {"scheduled_jobs": 0}
    errors: list[dict[str, Any]] = []
    for idx, item in enumerate(jobs):
        if not isinstance(item, dict):
            errors.append({"kind": "scheduled_job", "index": idx, "error": "row must be an object"})
            continue
        result = await _save_scheduled_job_definition(appdb, project_key=project_key, payload=item)
        if result.get("ok"):
            imported["scheduled_jobs"] += 1
        else:
            errors.append({"kind": "scheduled_job", "id": item.get("id"), "index": idx, "error": result.get("error")})
    return {"ok": not errors, "imported": imported, "errors": errors}
