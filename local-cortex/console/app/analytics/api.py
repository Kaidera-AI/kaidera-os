"""Analytics API — the imperative shell for the `analytics` module.

A FastAPI `APIRouter` exposing the analytics module's data as typed JSON. This is
the ONLY part of the module that imports fastapi (the layer rule: the service is
pure; the shell does I/O + wiring). The endpoint:

  * resolves the `OperationalStorePort` from `app.state` (the adapter the app wired
    at startup) via `Depends` — so the route depends on the PORT, not the concrete
    store,
  * constructs the `AnalyticsService` over it (injecting the real
    `providers.provider_label` / `providers.fmt_cost` formatters so the JSON labels
    match the HTML view exactly), and
  * returns the shaped usage/cost dict.

`main.py` mounts this additively (`app.include_router(analytics.router)`); the
existing HTML Analytics view delegates its usage/cost substance to the SAME
`AnalyticsService`, so the JSON API and the HTML surface share one source of logic.

Graceful-degrade rides through from the service/store (a down store yields the
'store not connected' empty state, never a 500)."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request

from app import providers as providers_catalog
from app.analytics.service import AnalyticsService, format_tokens
from app.domain.ports import OperationalStorePort

router = APIRouter(prefix="/analytics", tags=["analytics"])

# The trailing window (days) the headline 'Decisions · Nd' KPI counts over — mirrors the legacy
# HTML `main._ANALYTICS_WINDOW_DAYS`. Config-as-data (env-overridable, no per-project literal).
_KPI_WINDOW_DAYS = int(os.environ.get("ANALYTICS_KPI_WINDOW_DAYS", "7"))


def _iso_days_ago(days: int) -> str:
    """ISO-8601 (UTC, 'Z') timestamp `days` ago — for the /decisions/recent-count window. Lifted
    from `main._iso_days_ago`."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _safe(coro, default):
    """Await a Cortex/store read, returning `default` if it raises — so one failing read never
    aborts the gather for the others, and a surprising raise can't 500 the route (mirrors the
    history shell's `_safe`)."""
    try:
        return await coro
    except Exception:
        return default


def get_kpi_cortex(request: Request) -> Any:
    """Resolve the Cortex client (the /state + /decisions seams) from `app.state` for the KPI
    strip, or None when the client failed to construct (the route then degrades to null
    counters). The usage endpoint needs no Cortex (App-DB only); the KPI strip does."""
    return getattr(request.app.state, "cortex", None)


def get_operational_store(request: Request) -> OperationalStorePort:
    """Resolve the `OperationalStorePort` for the request.

    Prefers a pre-wired `app.state.opstore` (an `AppDbOperationalStore`); falls
    back to wrapping the live `app.state.appdb` so the route works even before the
    app explicitly stashes the adapter. Constructed at this composition seam so
    callers receive the PORT, never the concrete store."""
    state = request.app.state
    store = getattr(state, "opstore", None)
    if store is not None:
        return store
    # Fallback: wrap the app's AppDB in the adapter on demand.
    from app.adapters.opstore import AppDbOperationalStore

    return AppDbOperationalStore(appdb=state.appdb)


def build_service(store: OperationalStorePort) -> AnalyticsService:
    """Construct the analytics service over the port, injecting the concrete
    provider/cost formatters (so JSON labels match the HTML view)."""
    return AnalyticsService(
        store=store,
        provider_label=providers_catalog.provider_label,
        fmt_cost=providers_catalog.fmt_cost,
    )


@router.get("/{project}/usage")
async def usage_endpoint(
    project: str,
    store: OperationalStorePort = Depends(get_operational_store),
) -> dict[str, Any]:
    """`GET /analytics/{project}/usage` — the project's usage + est.-cost
    breakdowns (by model / model×provider / per agent / cost-by-agent / cost-by-
    project) as JSON. Includes `project` in the payload for the caller.

    The roster (display-name map) is read from the operational store's sibling —
    the JSON surface omits it (the bare agent name is returned, mapped to itself);
    the HTML view passes the Cortex roster for display names."""
    svc = build_service(store)
    usage = await svc.usage_cost(project)
    return {"project": project, **usage}


@router.get("/{project}/kpis")
async def kpis_endpoint(
    project: str,
    cortex: Any = Depends(get_kpi_cortex),
    store: OperationalStorePort = Depends(get_operational_store),
) -> dict[str, Any]:
    """`GET /analytics/{project}/kpis` — the Analytics view's slim headline KPI strip as JSON,
    for the SPA `AnalyticsView` (Events/24h · Active tasks · Decisions · recent Tokens).

    The existing `/usage` route covers the tokens/cost BREAKDOWN; the KPI COUNTERS were
    HTML-only (`main._analytics_view` headline block). This is that missing JSON surface, built
    from the SAME sources: Cortex `/state` (events_24h · active_tasks · pending_handoffs) + the
    trailing-window decisions count, plus the App-DB project token rollup (the 'Tokens · recent'
    KPI — the same total the usage view shows).

    The reads run CONCURRENTLY, each guarded (`_safe`) so one failure degrades alone; a None
    Cortex (failed to construct) leaves the counters null (the SPA renders 'n/a' — NEVER
    fabricated zeros), and a down store leaves tokens at 0/null. Never a 500.

    PATH NOTE (collision-free, strictly additive): the `/analytics/{project}/kpis` leaf is a
    distinct literal from the existing `/analytics/{project}/usage` route, so it can neither
    shadow nor be shadowed by it."""
    since = _iso_days_ago(_KPI_WINDOW_DAYS)

    if cortex is None:
        state: dict = {}
        decisions_recent: int | None = None
        by_project = await _safe(store.usage_by_project(project), {})
    else:
        state, decisions_recent, by_project = await asyncio.gather(
            _safe(cortex.get_state(project), {}),
            _safe(cortex.get_decisions_recent_count(project, since), None),
            _safe(store.usage_by_project(project), {}),
        )

    summary = state.get("summary", {}) if isinstance(state, dict) else {}
    by_project = by_project if isinstance(by_project, dict) else {}
    tokens_recent = by_project.get("tokens") or 0

    return {
        "project": project,
        "events_24h": summary.get("events_24h"),
        "active_tasks": summary.get("active_tasks"),
        "pending_handoffs": summary.get("pending_handoffs"),
        "decisions_recent": decisions_recent,
        "window_days": _KPI_WINDOW_DAYS,
        "tokens_recent": tokens_recent,
        "tokens_recent_h": format_tokens(tokens_recent) if tokens_recent else None,
    }


__all__ = [
    "router",
    "usage_endpoint",
    "kpis_endpoint",
    "get_operational_store",
    "get_kpi_cortex",
    "build_service",
]
