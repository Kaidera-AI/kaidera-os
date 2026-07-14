"""Runs API — the imperative shell for the `runs` module.

A FastAPI `APIRouter` exposing the run-state READ surface as typed JSON. This is the
ONLY part of the module that imports fastapi (the layer rule: the service is pure;
the shell does I/O + wiring). Three endpoints:

  * `GET /runs/{project}`                              — the run BOARD (active runs +
                                                          recent headers + counts).
  * `GET /runs/{project}/by-handoff/{handoff_id}`      — the latest run (WITH body) for
                                                          a handoff id.
  * `GET /runs/run/{run_id}`                           — one run (WITH body) by run id.
  * `POST /runs/run/{run_id}/cancel`                   — explicit operator cancel.

Each endpoint:
  * resolves the `RunStatePort` from `app.state.runstate` (the `RunStatePgStore` the
    app wired at startup) via `Depends` — so the route depends on the PORT, not the
    concrete store,
  * constructs the `RunsService` over it (injecting the real `_activity_relative`
    formatter so the JSON age-labels match the HTML view), and
  * returns the shaped JSON.

`main.py` mounts this additively (`app.include_router(runs.router)`); the existing
agent-detail run rail + SSE first-paint delegate their run-read substance to the SAME
`RunsService`, so the JSON API and the HTML surface share one source of run-read logic.

PATH NOTE (additive, non-colliding): the existing live run route is the SSE writer-
side `GET /runstate/stream` (a `/runstate` prefix). The runs JSON lives under the
distinct `/runs/...` prefix, all GET reads, so it can NEVER shadow that SSE route, the
`/dispatch/...` routes, or the `/agents/...` routes. Strictly additive (verified by
`test_router_runs_path_does_not_collide`).

Graceful-degrade rides through from the service/port (a None / down store yields an
empty board + None single-run); a `GET /runs/run/{id}` for an unknown/absent run
returns a clean 404, never a 500."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app import local_run_tasks
from app.domain.runstate import RunStatePort
from app.runs.service import RunsService

router = APIRouter(prefix="/runs", tags=["runs"])
_ACTIVE_STATUSES = {"queued", "running"}


def get_runstate_store(request: Request) -> Optional[RunStatePort]:
    """Resolve the `RunStatePort` for the request — the `app.state.runstate` SSOT
    store (the `RunStatePgStore` over the shared app-DB pool), or None if it failed to
    construct / the app-DB is down. Returned as the PORT; the service graceful-degrades
    a None store to the empty state."""
    return getattr(request.app.state, "runstate", None)


def build_service(store: Optional[RunStatePort]) -> RunsService:
    """Construct the runs service over the port.

    The relative-age formatter is the service's OWN self-contained default
    (`runs.service._default_relative`, lifted 1:1 from `main._activity_relative`) — so
    the JSON age-labels match the HTML view WITHOUT the shell reaching into `app.main`
    (which would create a transitive `app.runs -> app.main -> app.<other module>` edge
    that fails the independence gate — the trap the dispatch carve flagged). The HTML
    delegation in `main.py` injects its own `_activity_relative` (the same logic); the
    formatter is a seam, not a hard dependency."""
    return RunsService(store=store)


@router.get("/{project}")
async def runs_board_endpoint(
    project: str,
    store: Optional[RunStatePort] = Depends(get_runstate_store),
) -> dict[str, Any]:
    """`GET /runs/{project}` — the project's run BOARD as JSON: the ACTIVE runs
    (queued|running headers) + the RECENT run headers + the counts. Includes `project`
    in the payload.

    Reads `list_active` + `recent` through the `RunStatePort`. A None / down store
    yields an empty board (zero counts) — never a 500."""
    svc = build_service(store)
    board = await svc.board(project)
    return {"project": project, **board}


@router.get("/{project}/by-handoff/{handoff_id}")
async def run_by_handoff_endpoint(
    project: str,
    handoff_id: str,
    store: Optional[RunStatePort] = Depends(get_runstate_store),
) -> dict[str, Any]:
    """`GET /runs/{project}/by-handoff/{handoff_id}` — the LATEST run (WITH its
    hydrated body) for a handoff id, as JSON. 404 when no run exists for the handoff
    (or the store is down) — never a 500. (`project` scopes the URL; the handoff id is
    globally unique, so the run is looked up by handoff id directly.)"""
    svc = build_service(store)
    run = await svc.by_handoff(handoff_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no run for handoff")
    return run


@router.get("/run/{run_id}")
async def run_detail_endpoint(
    run_id: str,
    store: Optional[RunStatePort] = Depends(get_runstate_store),
) -> dict[str, Any]:
    """`GET /runs/run/{run_id}` — ONE run (WITH its hydrated body) by run id, as JSON.
    404 when the run is unknown (or the store is down) — never a 500.

    The `/run/{run_id}` leaf is distinct from `/{project}` (a two-segment shape under
    a literal `run` first segment), so it can't be ambiguous with the board route."""
    svc = build_service(store)
    run = await svc.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("/run/{run_id}/cancel")
async def cancel_run_endpoint(
    request: Request,
    run_id: str,
    store: Optional[RunStatePort] = Depends(get_runstate_store),
) -> dict[str, Any]:
    """Best-effort explicit operator cancel for local and harness-service runs.

    The run-state vocabulary stays unchanged: a cancelled active run is terminal
    `error` with an operator-cancel message. Unknown or already-terminal runs are a
    200 no-op so clients can retry safely.
    """
    local_cancelled = local_run_tasks.cancel_registered_local_run(
        request.app.state, run_id
    )

    harness_cancelled = False
    harness_port = getattr(request.app.state, "harness_port", None)
    cancel_run = getattr(harness_port, "cancel_run", None)
    if callable(cancel_run):
        try:
            harness_cancelled = bool(await cancel_run(run_id))
        except Exception:
            harness_cancelled = False

    status: str | None = None
    marked = False
    if store is not None:
        try:
            current = await store.get_run(run_id)
        except Exception:
            current = None
        status = getattr(current, "status", None) if current is not None else None
        if status in _ACTIVE_STATUSES:
            try:
                await store.set_status(
                    run_id,
                    "error",
                    error=local_run_tasks.LOCAL_RUN_CANCELLED_ERROR,
                )
                marked = True
                status = "error"
            except Exception:
                marked = False

    return {
        "run_id": run_id,
        "cancelled": bool(local_cancelled or harness_cancelled or marked),
        "local_task_cancelled": local_cancelled,
        "harness_cancelled": harness_cancelled,
        "marked": marked,
        "status": status,
    }


__all__ = [
    "router",
    "runs_board_endpoint",
    "run_by_handoff_endpoint",
    "run_detail_endpoint",
    "cancel_run_endpoint",
    "get_runstate_store",
    "build_service",
]
