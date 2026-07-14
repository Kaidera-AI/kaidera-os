"""The `runs` feature module — the run-state READ side (Track A, the FINAL carve).

The FIFTH and LAST feature module carved out of `app/main.py`'s blob behind the SDK
ports (following the analytics → agents → settings → dispatch pattern). With this
carve, Track A's five feature modules are all behind ports. It owns the run-state READ
substance: the agent-detail LIVE-WORK TRANSCRIPT view-model (the recent-run rail + the
selected-run hydrated body), the run board (active + recent), and single-run reads (by
id / by handoff) — read over the `RunStatePort`.

A clean VERTICAL slice, layered like the rest of the SDK (arrows point inward):

  * `service.py` — the run-read LOGIC (`RunsService`). Depends ONLY on
    `domain.runstate.RunStatePort` (the run-state SSOT) + an injectable relative-age
    formatter; it imports NOTHING outward (no fastapi / httpx / subprocess / psycopg2 /
    asyncpg) and never reaches back into `app.main`, the concrete `app.appdb` /
    `app.adapters`, or the `app.orchestrator` imperative core. The rail/transcript
    shaping lifted 1:1 from `main._store_run_row` / `_store_transcript_view` /
    `_agent_runs_view_store`.
  * `api.py` — the imperative SHELL: a FastAPI `APIRouter` whose endpoints resolve the
    `RunStatePort` from `app.state.runstate` (via `Depends`), construct the service over
    it, and return the shaped JSON. The only part that imports fastapi.

`main.py` mounts the router additively (`app.include_router(runs.router)`) and the
existing agent-detail run rail + SSE first-paint delegate their run-read substance to
`RunsService` — one source of the logic, two surfaces (JSON + HTML).

SCOPE — READ ONLY. The orchestrator's IMPERATIVE core stays in `main.py` /
`orchestrator.py`: the spawn/run path (`_dispatch_run` / `_pm_beat`), Approve & Run,
the autonomy toggle, and the SSE `/runstate/stream` WRITER side. This module reads +
shapes; it spawns nothing and writes nothing.

The module-isolation arrow is enforced by `.importlinter`'s `modules-are-independent`
(independence) contract: `app.runs` may import the ports/domain but not the other
feature modules — the 5th fitness gate fails if it regresses.
"""

from app.runs.api import router
from app.runs.service import RunsService

__all__ = ["RunsService", "router"]
