"""Automation feeders: scheduled jobs → Cortex handoffs.

This module is intentionally narrow. It does not run agents and it does not
manage project-specific loops or domain ingress. It turns durable app-DB trigger
definitions into ordinary Cortex handoffs, then the existing autonomous dispatch
path decides whether and how those handoffs run.
"""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Capability tag that marks a scheduled handoff as the hybrid PM planning beat.
# It is the single key the emit path keys off to specialize the dedup fingerprint
# (see ``resolve_planning_cycle_token``) and downstream surfaces use to recognize
# the beat. Never a project key / agent name — purely a role-agnostic capability.
PLANNING_CAPABILITY = "pm-planning-beat"

# Hybrid PM planner mode. The scheduled job no longer files a generic "review the
# plan" prompt: it spawns the resolved PM/lead to decompose the active epic into
# correctly-sequenced waves of worker handoffs.
PLANNING_MODE = "epic-decompose"

# The core skill the planning beat names so the spawned worker's skill selector
# reliably picks the decomposition method. Bound to the PM/lead role at runtime
# via ``cortex-skill`` (never a source DB write).
PLANNING_SKILL = "project-plan-create"

# Bounds for the per-cycle staleness-escape token. ``DEFAULT`` keeps a parked
# planning handoff deduped for the window, then lets a genuinely-stuck cycle
# rotate so re-planning is never silently dropped forever.
DEFAULT_PM_PLANNING_STALE_MINUTES = 360
MIN_PM_PLANNING_STALE_MINUTES = 15
MAX_PM_PLANNING_STALE_MINUTES = 7 * 24 * 60

# Static fallback token used when the open-handoff staleness read fails. It keeps
# re-emissions byte-identical (today's safe within-cycle dedup behavior) instead
# of rotating blindly on a transient Cortex read error.
STATIC_PLANNING_CYCLE = "static"

# Env-tunable "act at most N times per beat" budget woven into the mission copy.
DEFAULT_PM_PLANNING_HANDOFF_BUDGET = 1


def clean_job_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip().lower()).strip("-")


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_time(value: Any) -> time | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        hour, minute, *rest = text.split(":")
        second = rest[0] if rest else "0"
        return time(int(hour), int(minute), int(second))
    except Exception:
        return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def initial_next_run(schedule: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """First next_run_at for a new/updated job."""
    now = now or datetime.now(timezone.utc)
    kind = str((schedule or {}).get("kind") or "once").strip().lower()
    if kind == "once":
        run_at = parse_dt(schedule.get("run_at"))
        return run_at if run_at and run_at > now else now
    if kind == "interval":
        start_at = parse_dt(schedule.get("start_at"))
        if start_at:
            return start_at if start_at > now else now
        seconds = max(60, _safe_int(schedule.get("every_seconds"), 3600))
        return now + timedelta(seconds=seconds)
    if kind == "daily":
        return _next_daily(schedule, now)
    return None


def next_run_after(schedule: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """Next run after a job has fired. None disables a one-shot job."""
    now = now or datetime.now(timezone.utc)
    kind = str((schedule or {}).get("kind") or "once").strip().lower()
    if kind == "once":
        return None
    if kind == "interval":
        seconds = max(60, _safe_int(schedule.get("every_seconds"), 3600))
        return now + timedelta(seconds=seconds)
    if kind == "daily":
        return _next_daily(schedule, now + timedelta(seconds=1))
    return None


def retry_after(now: datetime | None = None) -> datetime:
    """Short backoff after a failed handoff create; avoids retry-spam."""
    return (now or datetime.now(timezone.utc)) + timedelta(minutes=5)


def _next_daily(schedule: dict[str, Any], now: datetime) -> datetime | None:
    at = _parse_time(schedule.get("time"))
    if at is None:
        return None
    tz_name = str(schedule.get("timezone") or "UTC").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    days_raw = schedule.get("days")
    allowed_days = None
    if isinstance(days_raw, list) and days_raw:
        allowed_days = {_safe_int(day, -1) for day in days_raw if str(day).strip()}
        allowed_days = {day for day in allowed_days if 0 <= day <= 6}
    local_now = now.astimezone(tz)
    for offset in range(0, 8):
        day = local_now.date() + timedelta(days=offset)
        if allowed_days is not None and day.weekday() not in allowed_days:
            continue
        candidate = datetime.combine(day, at, tzinfo=tz)
        if candidate > local_now:
            return candidate.astimezone(timezone.utc)
    return None


def handoff_payload_from_job(job: dict[str, Any]) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    payload = job.get("payload") if isinstance(job, dict) else None
    if not isinstance(payload, dict):
        return None, None
    from_agent = str(payload.get("from_agent") or "").strip().lower()
    summary = str(payload.get("summary") or "").strip()
    to_role = str(payload.get("to_role") or "").strip().lower()
    if not from_agent or not summary or not to_role:
        return None, None
    keys = {
        "from_role", "to_role", "to_agent", "priority", "summary", "branch",
        "files_changed", "verification", "next_steps", "context",
        "parent_goal_id", "acceptance", "evidence", "retry", "escalation",
    }
    body = {k: v for k, v in payload.items() if k in keys}
    body.setdefault("priority", "medium")
    body["to_role"] = to_role
    body["summary"] = summary
    return from_agent, body


def _agent_role(agent: dict[str, Any]) -> str:
    return str(agent.get("role") or agent.get("role_profile") or "").strip().lower()


def _agent_name(agent: dict[str, Any]) -> str:
    return str(agent.get("name") or agent.get("agent") or "").strip().lower()


async def resolve_planning_worker(
    cortex: Any,
    *,
    project_key: str,
    preferred_agent: str = "",
) -> tuple[str, str]:
    """Resolve the project's PM, falling back to its registered lead."""
    preferred = (preferred_agent or "").strip().lower()
    if preferred:
        return preferred, "pm"
    project_record: dict[str, Any] = {}
    agents: list[dict[str, Any]] = []
    if cortex is not None:
        try:
            raw_project = await cortex.get_project(project_key)
            project_record = raw_project if isinstance(raw_project, dict) else {}
        except Exception:
            pass
        try:
            raw_agents = await cortex.get_agents(project_key)
            agents = raw_agents if isinstance(raw_agents, list) else []
        except Exception:
            pass
    for agent in agents:
        name = _agent_name(agent)
        role = _agent_role(agent)
        if name and role in {"pm", "project-manager", "product-manager", "cpo"}:
            return name, "pm"
        caps = agent.get("capabilities") if isinstance(agent.get("capabilities"), dict) else {}
        designation = str(caps.get("designation") or caps.get("role_preset") or "").strip().lower()
        if name and designation in {"pm", "pm-ai-agent", "project-manager"}:
            return name, "pm"
    default_agent = str(project_record.get("default_agent") or "").strip().lower()
    if default_agent:
        return default_agent, "lead"
    for agent in agents:
        name = _agent_name(agent)
        if name and _agent_role(agent) in {"lead", "cpo", "cmo"}:
            return name, "lead"
    return "", "pm"


def pm_planning_stale_minutes(env: dict[str, str] | None = None) -> int:
    """Bounded staleness window for the planning cycle-token (env-tunable)."""
    raw = (env if env is not None else os.environ).get("PM_PLANNING_STALE_MINUTES")
    minutes = _safe_int(raw, DEFAULT_PM_PLANNING_STALE_MINUTES)
    return max(MIN_PM_PLANNING_STALE_MINUTES, min(minutes, MAX_PM_PLANNING_STALE_MINUTES))


def pm_planning_handoff_budget(env: dict[str, str] | None = None) -> int:
    """Bounded "act at most N times per beat" budget (env-tunable)."""
    raw = (env if env is not None else os.environ).get("PM_PLANNING_HANDOFF_BUDGET")
    return max(1, min(_safe_int(raw, DEFAULT_PM_PLANNING_HANDOFF_BUDGET), 5))


def carries_planning_capability(record: Any) -> bool:
    """True iff a job payload body / handoff row is the PM planning beat."""
    acc = record.get("acceptance") if isinstance(record, dict) else None
    if isinstance(acc, dict):
        return str(acc.get("capability") or "").strip().lower() == PLANNING_CAPABILITY
    return False


def planning_cycle_token(now: datetime | None = None, *, stale_minutes: int | None = None) -> str:
    """Time-window token; rotates once per staleness window.

    Used as the rotating fingerprint specializer so a genuinely-new or recovery
    planning cycle gets a distinct Cortex dedup fingerprint.
    """
    now = now or datetime.now(timezone.utc)
    window_minutes = stale_minutes if stale_minutes is not None else pm_planning_stale_minutes()
    window = max(MIN_PM_PLANNING_STALE_MINUTES, int(window_minutes)) * 60
    return str(int(now.timestamp()) // window)


def resolve_planning_cycle_token(
    open_handoffs: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    stale_minutes: int | None = None,
) -> str:
    """Stable token while a FRESH open planning handoff exists; rotate once stale/absent.

    - Parked/held (propose_mode on, or interactive lead without auto_dispatch):
      a still-fresh open planning handoff keeps the SAME token, so the next
      emission is byte-identical and Cortex dedup collapses it (no pile-up).
    - Stuck/stale: an open planning handoff older than the window rotates to the
      time-window token, yielding a distinct fingerprint so re-planning is never
      silently dropped forever.
    - Healthy loop (prior planning handoff completed → not in the open set):
      no open match, so the rotating time-window token is used; there is nothing
      to dedup against, and the next interval re-plans cleanly.
    """
    now = now or datetime.now(timezone.utc)
    minutes = stale_minutes if stale_minutes is not None else pm_planning_stale_minutes()
    cutoff = now - timedelta(minutes=max(MIN_PM_PLANNING_STALE_MINUTES, int(minutes)))
    for row in open_handoffs or []:
        if not carries_planning_capability(row):
            continue
        acc = row.get("acceptance") if isinstance(row, dict) else None
        token = str((acc or {}).get("planning_cycle") or "").strip()
        created = parse_dt(row.get("created_at")) if isinstance(row, dict) else None
        if token and created and created > cutoff:
            return token
    return planning_cycle_token(now, stale_minutes=minutes)


def pm_planning_schedule_payload(
    *,
    project: str,
    from_agent: str,
    to_role: str = "pm",
    to_agent: str | None = None,
    every_minutes: int = 240,
    summary: str | None = None,
    handoff_budget: int | None = None,
) -> dict[str, Any]:
    """Canonical scheduled-handoff payload for the hybrid PM planning beat.

    This is the AV-5 upgrade: instead of a generic "review the plan" prompt, the
    payload is an epic-decomposition mission that points the spawned PM/lead at
    the ``project-plan-create`` skill. It stays project-agnostic — the project,
    identity, and roster are resolved at runtime from ``cortex-boot`` — and never
    bakes a project key or agent name into source (the project arg is the live
    project the scheduled job belongs to). The per-cycle ``planning_cycle`` token
    is stamped later, at emit time, by ``run_due_scheduled_jobs``.
    """
    minutes = max(15, min(int(every_minutes or 240), 7 * 24 * 60))
    clean_project = (project or "").strip().lower()
    clean_from = (from_agent or "").strip().lower()
    target_role = (to_role or "pm").strip().lower()
    target_agent = (to_agent or "").strip().lower()
    budget = pm_planning_handoff_budget() if handoff_budget is None else max(1, int(handoff_budget))
    default_summary = (
        f"PM planning beat: decompose the active epic into waves and worker handoffs "
        f"for {clean_project}."
    )
    actor_label = clean_from or "the executing PM agent"
    command_actor = clean_from or "<you>"
    context = (
        f"Run ONE proactive PM planning beat for THIS project as `{actor_label}` "
        "(use the project + identity from cortex-boot — never hardcode another project or agent).\n"
        f"1. Boot + read state: cortex-boot {command_actor}; cortex-handoff --mine {command_actor}; identify the "
        "active epic and current wave from cortex-search.\n"
        "2. Follow your project-plan-create skill to decompose the active epic into the "
        "smallest coherent worker handoffs, sequenced by wave/dependency, each with "
        "acceptance criteria + Verify steps.\n"
        f"3. Act AT MOST {budget} time(s) this beat: EMIT the next safe handoff(s) in ONE "
        "===FILE-HANDOFFS=== JSON block at the END of your reply (your parent process files "
        "them via the Cortex API — do NOT run cortex-handoff --create, and never assume filing "
        "is sandboxed) OR handle a [WATCHDOG-SIGNAL] OR escalate OR log epic-done, then STOP. "
        "The scheduler re-triggers next interval.\n"
        "Loop guard: before creating any handoff, list open handoffs and SKIP equivalents. "
        "NEVER create another pm-planning-beat handoff — the scheduler owns the cadence."
    )
    return {
        "id": "pm-planning-beat",
        "name": "PM planning beat",
        "enabled": True,
        "schedule": {"kind": "interval", "every_seconds": minutes * 60},
        "payload": {
            "from_agent": clean_from,
            "to_role": target_role,
            **({"to_agent": target_agent} if target_agent else {}),
            "priority": "medium",
            "summary": summary or default_summary,
            "context": context,
            "acceptance": {
                "capability": PLANNING_CAPABILITY,
                "mode": PLANNING_MODE,
                "skill": PLANNING_SKILL,
                "project": clean_project,
                "handoff_budget": budget,
                "must_review": [
                    "active epic",
                    "current wave",
                    "open handoffs",
                    "active runs",
                    "blocked work",
                    "recent memory",
                ],
            },
        },
    }


async def ensure_pm_planning_schedule(
    *,
    appdb: Any,
    cortex: Any,
    project: str,
    every_minutes: int = 240,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ensure an autonomous project has one enabled, durable PM planning beat.

    Existing enabled jobs are preserved exactly. A missing, disabled, or stranded
    canonical job is repaired and made due immediately so a reboot or autonomy
    toggle does not wait one full interval before the first heartbeat.
    """
    project_key = (project or "").strip().lower()
    if not project_key or appdb is None:
        return {"ok": False, "created": False, "error": "project and app-DB are required"}
    try:
        jobs = await appdb.list_scheduled_jobs(project_key)
    except Exception as exc:
        return {"ok": False, "created": False, "error": str(exc)}
    existing = next((job for job in jobs or [] if job.get("id") == "pm-planning-beat"), None)
    if existing and existing.get("enabled") and existing.get("next_run_at"):
        return {"ok": True, "created": False, "job": existing, "error": None}

    from_agent, to_role = await resolve_planning_worker(cortex, project_key=project_key)
    if not from_agent:
        return {"ok": False, "created": False, "error": "no PM or lead worker is configured"}
    definition = pm_planning_schedule_payload(
        project=project_key,
        from_agent=from_agent,
        to_role=to_role,
        to_agent=from_agent,
        every_minutes=every_minutes,
    )
    try:
        row = await appdb.upsert_scheduled_job(
            project=project_key,
            job_id=definition["id"],
            name=definition["name"],
            enabled=True,
            schedule=definition["schedule"],
            payload=definition["payload"],
            next_run_at=now or datetime.now(timezone.utc),
        )
    except Exception as exc:
        return {"ok": False, "created": False, "error": str(exc)}
    return {
        "ok": row is not None,
        "created": existing is None,
        "repaired": existing is not None,
        "job": row,
        "error": None if row is not None else "scheduled job could not be saved",
    }


async def _open_planning_handoffs(cortex: Any, project: str) -> list[dict[str, Any]]:
    """Open (pending + claimed/parked) handoffs that are PM planning beats.

    Raises on a hard read failure so the caller can fall back to the static
    token. A missing ``claimed`` view is non-fatal (pending covers parked
    propose-mode handoffs).
    """
    rows: list[dict[str, Any]] = []
    pending = await cortex.get_handoffs(project)
    if isinstance(pending, list):
        rows.extend(pending)
    try:
        claimed = await cortex.get_handoffs(project, status="claimed")
        if isinstance(claimed, list):
            rows.extend(claimed)
    except Exception:
        pass
    return [r for r in rows if carries_planning_capability(r)]


async def _stamp_planning_cycle(
    body: dict[str, Any],
    *,
    cortex: Any,
    project: str,
    now: datetime,
) -> None:
    """Stamp ``acceptance.planning_cycle`` on a planning-beat body before create.

    Graceful-degrade: any read failure falls back to the static token (today's
    byte-identical within-cycle dedup behavior) rather than crashing or rotating
    blindly. Copies acceptance so a shared job payload dict is never mutated.
    """
    try:
        open_planning = await _open_planning_handoffs(cortex, project)
        token = resolve_planning_cycle_token(open_planning, now=now)
    except Exception:
        token = STATIC_PLANNING_CYCLE
    acc = dict(body.get("acceptance") or {})
    acc["planning_cycle"] = token
    body["acceptance"] = acc


async def run_due_scheduled_jobs(
    *,
    appdb: Any,
    cortex: Any,
    project: str,
    feed: Any = None,
    limit: int = 20,
) -> int:
    """Emit handoffs for due scheduled jobs in one project.

    Returns the number of handoffs successfully created or deduped. Every error
    is captured in the job row; no exception should escape the orchestrator
    reconcile loop.
    """
    now = datetime.now(timezone.utc)
    jobs = await appdb.due_scheduled_jobs(project, now=now, limit=limit)
    created = 0
    for job in jobs:
        job_id = str(job.get("id") or "")
        from_agent, body = handoff_payload_from_job(job)
        if not from_agent or not body:
            await appdb.mark_scheduled_job_run(
                project=project,
                job_id=job_id,
                status="error",
                next_run_at=retry_after(now),
                error="scheduled job payload requires from_agent, to_role, and summary",
            )
            continue
        # Hybrid PM planning beat: stamp the per-cycle staleness-escape token so a
        # parked planning ask dedups within the window but a stuck one can rotate.
        # Every other scheduled job is emitted unchanged.
        if carries_planning_capability(body):
            await _stamp_planning_cycle(body, cortex=cortex, project=project, now=now)
        result = await cortex.create_handoff(project, from_agent, body)
        if result and result.get("id"):
            next_at = next_run_after(job.get("schedule") or {}, now=now)
            await appdb.mark_scheduled_job_run(
                project=project,
                job_id=job_id,
                status="deduped" if result.get("deduped") else "created",
                next_run_at=next_at,
                enabled=next_at is not None,
            )
            created += 1
            if feed is not None:
                with _suppress_feed_errors():
                    feed.add(
                        project,
                        "info",
                        f"Scheduled job {job.get('name') or job_id} created handoff {str(result['id'])[:8]}",
                        agent=from_agent,
                        level="info",
                    )
            continue
        await appdb.mark_scheduled_job_run(
            project=project,
            job_id=job_id,
            status="error",
            next_run_at=retry_after(now),
            error=str((result or {}).get("error") or "Cortex rejected handoff"),
        )
    return created


class _suppress_feed_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> bool:
        return True
