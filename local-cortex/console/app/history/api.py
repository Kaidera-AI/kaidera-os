"""History API — the imperative shell (FastAPI router) for the activity-timeline surface.

The ONLY part of the history module that imports fastapi. ONE READ-ONLY endpoint that feeds
the SPA `HistoryView` a clean `{events, decisions, agent_count}` JSON:

  * `GET /history/{project}?limit=N&include_decisions=1` — the cross-agent activity
    timeline + optional recent-decisions feed + the roster agent count. `events` are the reverse-chronological
    `/history` rows each run through the PORTED summariser (a readable line, never the raw
    noisy tool-call JSON); `decisions` are the recent-decisions feed from `/search`;
    `agent_count` is the distinct agents on the roster. `limit` (optional) is the raw
    `/history` window requested (the rendered timeline is then capped at HISTORY_EVENT_CAP).

The hot-path Cortex reads (`get_history` + `get_roster`) run CONCURRENTLY and each
graceful-degrades to [] inside the `CortexClient`; the heavier `/search` decisions feed is
opt-in (`include_decisions=1`) and timeboxed so dashboard polling/project switching does
not block on a shared memory search. The route returns
`{events:[], decisions:[], agent_count:0}` — it NEVER 500s. The shared
`CortexClient` on `app.state.cortex` supplies all three seams; a None client (failed to
construct) also yields the clean empty payload. No host forward, no DB, no mutation — a pure
projection of the live Cortex memory.

PATH NOTE (additive, non-colliding): everything lives under the distinct `/history/...`
prefix, so it can never shadow `/runs/...`, `/runstate/...`, `/dispatch/...`, `/agents/...`,
`/explain/...`, `/analytics/...`, `/graph/...`, or `/settings/...`.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.history.shape import (
    HISTORY_DECISIONS_CAP,
    roster_agent_count,
    shape_decisions,
    shape_events,
)

router = APIRouter(prefix="/history", tags=["history"])

# The (broad) seed query the decisions feed runs against /search. The API needs a term; this
# catch-all surfaces recent decisions/lessons for the project. Config-as-data (env-overridable,
# no per-project literal) — mirrors the legacy `main._HISTORY_DECISIONS_SEED`.
_DECISIONS_SEED = os.environ.get("HISTORY_DECISIONS_SEED", "cortex")
_DECISIONS_TIMEOUT_S = max(
    0.05,
    int(os.environ.get("HISTORY_DECISIONS_TIMEOUT_MS", "800")) / 1000,
)
_READ_TIMEOUT_S = max(
    0.05,
    int(os.environ.get("HISTORY_READ_TIMEOUT_MS", "1200")) / 1000,
)

# How big a raw /history window to request by default. The live API caps the window
# server-side; the rendered timeline is bounded by HISTORY_EVENT_CAP regardless. Env-overridable.
_DEFAULT_HISTORY_LIMIT = int(os.environ.get("HISTORY_WINDOW_LIMIT", "200"))


def _cortex(request: Request):
    """The shared `CortexClient` (the history/search/roster seams) from app.state, or None
    when the client failed to construct (the route then degrades to an empty payload)."""
    return getattr(request.app.state, "cortex", None)


def _empty_payload() -> dict[str, Any]:
    """The clean empty payload for a down/None Cortex (never a 500)."""
    return {"events": [], "decisions": [], "agent_count": 0}


async def _safe(coro, default, *, timeout: float | None = None):
    """Await a Cortex read, returning `default` if it raises. The CortexClient already
    graceful-degrades to []/{} internally; this is the belt-and-braces guard so a surprising
    raise (a fake/older client) can't 500 the route, and so one section's failure never
    aborts the gather for the others."""
    try:
        if timeout is not None:
            return await asyncio.wait_for(coro, timeout=timeout)
        return await coro
    except Exception:
        return default


async def _build_history(request: Request, project: str, limit: int) -> dict[str, Any]:
    """Fetch + shape the `{events, decisions, agent_count}` payload for one project.

    Pulls Cortex `/history` (the raw timeline window) + `/roster` (the agent count)
    CONCURRENTLY, each guarded so one failure degrades alone, then shapes them via the
    pure `app.history.shape` helpers. The heavier `/search` decisions seed is opt-in
    because dashboard polling needs events, not a broad memory search. A None client or a
    raising read both yield the clean empty section — this coroutine NEVER raises."""
    return await _build_history_payload(request, project, limit, include_decisions=False)


async def _build_history_payload(
    request: Request,
    project: str,
    limit: int,
    *,
    include_decisions: bool,
) -> dict[str, Any]:
    """Build the history payload, keeping the hot polling path free of broad search."""
    cortex = _cortex(request)
    if cortex is None:
        return _empty_payload()

    history, roster = await asyncio.gather(
        _safe(cortex.get_history(project, limit=limit), [], timeout=_READ_TIMEOUT_S),
        _safe(cortex.get_roster(project), [], timeout=_READ_TIMEOUT_S),
    )
    search: list[dict[str, Any]] = []
    if include_decisions:
        search = await _safe(
            asyncio.wait_for(
                # rerank=False: this is a broad recent-decisions SEED feed, not a precise
                # query — the ~3s reranker buys nothing here and makes history load slow.
                cortex.search(project, _DECISIONS_SEED, limit=HISTORY_DECISIONS_CAP, rerank=False),
                timeout=_DECISIONS_TIMEOUT_S,
            ),
            [],
        )

    return {
        "events": shape_events(history if isinstance(history, list) else []),
        "decisions": shape_decisions(search if isinstance(search, list) else []),
        "agent_count": roster_agent_count(roster if isinstance(roster, list) else []),
    }


@router.get("/{project}")
async def history(project: str, request: Request) -> JSONResponse:
    """The activity timeline + recent-decisions feed + roster count for `project`.

    `?limit=<n>` sizes the raw /history window (bounded server-side; the rendered timeline is
    capped at HISTORY_EVENT_CAP regardless). Returns `{events, decisions, agent_count}`; empty
    sections (200) on a down/empty/None Cortex — never a 500."""
    limit = _DEFAULT_HISTORY_LIMIT
    raw_limit = request.query_params.get("limit")
    if raw_limit:
        try:
            parsed = int(raw_limit)
            if parsed > 0:
                # Bound the requested window so a silly ?limit=99999 can't hammer Cortex;
                # the rendered timeline is capped at HISTORY_EVENT_CAP regardless.
                limit = min(parsed, 1000)
        except ValueError:
            pass
    include_decisions = request.query_params.get("include_decisions", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    payload = await _build_history_payload(
        request,
        project,
        limit,
        include_decisions=include_decisions,
    )
    return JSONResponse(payload)


__all__ = ["router"]
