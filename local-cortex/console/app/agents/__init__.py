"""The `agents` feature module — the roster catalog (Track A, the 2nd carve).

The SECOND feature module carved out of `app/main.py`'s blob behind the SDK ports
(analytics was first; settings / dispatch / runs follow). It owns the READ/catalog
side of the agents surface: LIST a project's roster grouped Interactive vs
Autonomous (classified override-first) with the orchestrator label + the default
lead, and GET one agent resolved to its effective config/designation/role. (The
chat-routing + runtime-config WRITE paths stay in `main.py` for now — a later
carve; this is the read/catalog carve.)

A clean VERTICAL slice, layered like the rest of the SDK (arrows point inward):

  * `service.py` — the catalog LOGIC (`AgentsService`). Depends ONLY on
    `domain.ports.OperationalStorePort` (the per-agent override store) + two
    injectable presentation callables (a config resolver + a config-view shaper);
    it imports NOTHING outward (no fastapi / httpx / subprocess / psycopg2 /
    asyncpg) and never reaches back into `app.main`, the concrete `app.appdb` /
    `app.adapters` or the concrete `app.harness`. The roster is
    passed IN by the caller (the analytics `agents=` pattern). The classification +
    shaping logic lifted 1:1 from `main._agent_view` / `_group_agents` /
    `_classify_interactive` / `_has_cpo_tag` / `_registry_interactive` /
    `_orchestrator_label` / `_lead_agent_name` (+ `_agent_detail_view`'s resolution).
  * `api.py` — the imperative SHELL: a FastAPI `APIRouter` whose `GET /agents/{p}`
    + `GET /agents/{p}/{a}` resolve the `OperationalStorePort` + the Cortex roster
    source from `app.state` (via `Depends`), construct the service over them
    (injecting the real `harness`-backed resolvers), and return the shaped JSON. The
    only part that imports fastapi.

`main.py` mounts the router additively and the existing HTML agents column +
agent-detail pane delegate their catalog substance to `AgentsService` — one source
of the logic, multiple surfaces.

The module-isolation arrow is enforced by `.importlinter`'s
`modules-are-independent` (independence) contract: `app.agents` may import the
ports/domain but not the other feature modules — the 5th fitness gate fails if it
regresses.
"""

from app.agents.api import router
from app.agents.service import AgentsService

__all__ = ["AgentsService", "router"]
