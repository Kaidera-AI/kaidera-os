"""The `analytics` feature module — usage + est.-cost analytics (Track A carve).

The FIRST feature module carved out of `app/main.py`'s blob behind the SDK ports
(it establishes the pattern the remaining modules — agents / settings / dispatch /
runs — follow). It owns the Analytics view's substance: usage + estimated-cost
breakdowns (by model / by model×provider / per agent / cost-by-agent / cost-by-
project) computed over the App-DB `usage_events`.

A clean VERTICAL slice, layered like the rest of the SDK (arrows point inward):

  * `service.py` — the feature LOGIC (`AnalyticsService.usage_cost`). Depends ONLY
    on `domain.ports.OperationalStorePort` (the operational data source) + two
    injectable pure formatters; it imports NOTHING outward (no fastapi / httpx /
    subprocess / psycopg2 / asyncpg) and never reaches back into `app.main`, the
    concrete `app.appdb`, or `app.adapters`. The metric logic lifted 1:1 from
    `main._analytics_usage_cost`.
  * `api.py` — the imperative SHELL: a FastAPI `APIRouter` whose endpoint resolves
    the `OperationalStorePort` from `app.state` (via `Depends`), constructs the
    service over it, and returns the shaped JSON. The only part that imports
    fastapi.

`main.py` mounts the router additively (`app.include_router(analytics.router)`)
and the existing HTML Analytics view delegates its usage/cost substance to
`AnalyticsService` — one source of the logic, two surfaces (JSON + HTML).

The module-isolation arrow is enforced by `.importlinter`'s
`modules-are-independent` (independence) contract: `app.analytics` may import the
ports/domain but not the other feature modules — the 5th fitness gate fails if it
regresses.
"""

from app.analytics.api import router
from app.analytics.service import AnalyticsService

__all__ = ["AnalyticsService", "router"]
