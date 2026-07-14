"""Epic + metrics shaping for the col-2 Active-Epic widget — PURE, no I/O.

The functional core behind the `GET /agents/{project}/epics` JSON surface (the agents
module's `api.py` shell fetches the Cortex `/epics` + `/state` + `/board` reads and calls
these to shape the payload). Lifted 1:1 from `main._shape_epic` / `_shape_epics` /
`_epic_view` / `_inc_status_kind` / `_clamp_pct` / `_metrics_view` / `_pending_tasks` so the
JSON surface and the legacy HTML col-2 share ONE source of the shaping.

LAYER RULE (arrows point inward): this module imports NOTHING outward (no fastapi / httpx /
subprocess / db) and never reaches back into `app.main` or the other feature modules — it is
pure presentation shaping over the raw Cortex dicts the caller passes in. The independence
contract (`.importlinter` modules-are-independent) keeps it that way.

Graceful-degrade is the house law: an empty / None `/epics` payload shapes to the
'continuous · no epics' line (NEVER fabricated progress); a None `/state` counter survives as
None (the SPA renders '—').
"""

from __future__ import annotations

from typing import Any

# Epic statuses that mark the ACTIVE/build epic (drives the active-major sort + the lead flag).
# Lifted from `main._ACTIVE_EPIC_STATUSES`.
ACTIVE_EPIC_STATUSES = ("build", "active", "in_progress")

# Task statuses that count as LIVE (everything else on the board is 'pending'). Lifted from
# `main._ACTIVE_TASK_STATUSES`.
ACTIVE_TASK_STATUSES = ("in_progress", "active")

# Max epics surfaced in the col-2 stack (the active one leads). Lifted from `main._COL2_EPIC_MAX`.
COL2_EPIC_MAX = 4

# The 'continuous-backlog / no epics' line (also the graceful-degrade state).
CONTINUOUS_LABEL = "continuous · no epics"


def _inc_status_kind(status: str) -> str:
    """Bucket an increment status into done / prog / todo for the dot/bar style.

    done = filled green, prog = teal in-flight, todo = empty track. An unknown status falls
    back to 'todo' so a new status never fabricates a filled bar. Lifted from
    `main._inc_status_kind`."""
    s = (status or "").lower()
    if s == "done":
        return "done"
    if s in ("in_progress", "active", "build"):
        return "prog"
    return "todo"


def _clamp_pct(pct: object) -> int:
    """Coerce a raw pct value to a clamped 0–100 int (None/garbage → 0). Lifted from
    `main._clamp_pct`."""
    try:
        n = int(pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def shape_epic(epic: dict) -> dict:
    """Flatten one /epics row into the fields the SPA renders directly.

    Pulls epic_id/title/status/overall_pct + a shaped increments list (each with a num label,
    clamped pct, raw status, and the done/prog/todo style kind). `is_active` flags a
    build/active/in_progress epic (drives the active-major sort + the lead flag). Lifted 1:1
    from `main._shape_epic`."""
    incs_raw = epic.get("increments")
    incs_raw = incs_raw if isinstance(incs_raw, list) else []
    increments: list[dict] = []
    for inc in incs_raw:
        if not isinstance(inc, dict):
            continue
        num = inc.get("num")
        pct = _clamp_pct(inc.get("pct"))
        status = inc.get("status") or ""
        increments.append(
            {
                "num": num,
                "label": f"Inc{num}" if num is not None else "Inc",
                "title": inc.get("title") or "",
                "pct": pct,
                "status": status,
                "kind": _inc_status_kind(status),
            }
        )
    status = epic.get("status") or ""
    return {
        "epic_id": epic.get("epic_id") or "—",
        "title": epic.get("title") or "",
        "status": status,
        "overall_pct": _clamp_pct(epic.get("overall_pct")),
        "increments": increments,
        "increment_count": len(increments),
        "is_active": status.lower() in ACTIVE_EPIC_STATUSES,
        "updated_at": epic.get("updated_at"),
    }


def shape_epics(payload: dict) -> list[dict]:
    """Shape a /epics payload's `epics` list into a sorted, render-ready epic stack.

    Sort is ACTIVE-major: build/active/in_progress epics lead (the one you're working), then
    by overall % desc, then by epic_id — so the live epic is prominent. Returns [] for a
    project with no epics (continuous-backlog or simply none). Lifted 1:1 from
    `main._shape_epics`."""
    rows = payload.get("epics") if isinstance(payload, dict) else None
    rows = rows if isinstance(rows, list) else []
    shaped = [shape_epic(e) for e in rows if isinstance(e, dict)]
    shaped.sort(
        key=lambda e: (
            0 if e["is_active"] else 1,
            -e["overall_pct"],
            e["epic_id"],
        )
    )
    return shaped


def epic_view(epics_payload: dict | None = None) -> dict:
    """Build the Active-Epic section from a Cortex /epics payload.

    `epics_payload` is the project's GET /epics response (or None when not fetched / degraded).
    When the project has epics → mode='epics' with the shaped, sorted stack (the active epic
    leads, capped at COL2_EPIC_MAX). When it has NO epics (continuous-backlog, or simply none,
    or a degraded read) → mode='continuous' with the 'continuous · no epics' line — NEVER
    fabricated progress. Lifted 1:1 from `main._epic_view` (sans the unused project_key arg)."""
    epics = shape_epics(epics_payload or {})
    if epics:
        return {
            "mode": "epics",
            "epics": epics[:COL2_EPIC_MAX],
            "epic_count": len(epics),
        }
    return {
        "mode": "continuous",
        "label": CONTINUOUS_LABEL,
        "epics": [],
        "epic_count": 0,
    }


def pending_tasks(tasks: list[dict]) -> int:
    """Count /board tasks that are NOT live (status not in in_progress/active). /state exposes
    active_tasks but no pending counter, so the metrics block derives 'Pending tasks' here from
    the board rows. Lifted 1:1 from `main._pending_tasks`."""
    return sum(1 for t in tasks if (t.get("status") or "") not in ACTIVE_TASK_STATUSES)


def metrics_view(state: dict, tasks: list[dict]) -> dict:
    """Build the compact metrics block for the project.

    Active tasks · Pending handoffs · Events/24h come straight off state.summary; Pending
    tasks is derived from the board (see `pending_tasks`). A None counter survives as None
    (the SPA renders '—'). Lifted 1:1 from `main._metrics_view`."""
    summary = state.get("summary", {}) if isinstance(state, dict) else {}
    return {
        "active_tasks": summary.get("active_tasks"),
        "pending_tasks": pending_tasks(tasks if isinstance(tasks, list) else []),
        "pending_handoffs": summary.get("pending_handoffs"),
        "events_24h": summary.get("events_24h"),
    }


def build_epics_payload(
    epics_payload: dict | None, state: dict | None, tasks: list[dict] | None
) -> dict[str, Any]:
    """Assemble the full `GET /agents/{project}/epics` JSON body from the three Cortex reads.

    `{epic: epic_view(...), metrics: metrics_view(...)}` — pure shaping over the raw dicts the
    caller fetched (each already graceful-degraded to a safe empty value). Never raises."""
    return {
        "epic": epic_view(epics_payload),
        "metrics": metrics_view(state or {}, tasks or []),
    }


__all__ = [
    "ACTIVE_EPIC_STATUSES",
    "ACTIVE_TASK_STATUSES",
    "COL2_EPIC_MAX",
    "CONTINUOUS_LABEL",
    "shape_epic",
    "shape_epics",
    "epic_view",
    "pending_tasks",
    "metrics_view",
    "build_epics_payload",
]
