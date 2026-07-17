"""The `dispatch` feature module — the dispatch board (Track A, the 4th carve).

The FOURTH feature module carved out of `app/main.py`'s blob behind the SDK ports
(analytics was first, agents second, settings third; runs follows). It owns the
READ/BOARD side of the Dispatch surface: LIST the open/pending handoffs (the
dispatch queue) each with a rule-based proposed agent, the board counts, and the
project autonomy/propose-mode flag reads. (The orchestrator's imperative core — the
spawn/run path, `_pm_beat`, the Approve & Run + autonomy-toggle + approve-gate POST
routes, and the live status/feed/wave assembly — stays in `main.py` /
`orchestrator.py` for a LATER carve; this is the board read carve, and the running
orchestrator is untouched.)

A clean VERTICAL slice, layered like the rest of the SDK (arrows point inward):

  * `service.py` — the board LOGIC (`DispatchService`). Depends ONLY on
    `domain.ports.CortexMemoryPort` (the dispatch queue — `get_handoffs()`) +
    `domain.ports.OperationalStorePort` (the autonomy + propose-mode flag reads),
    plus an injectable per-agent config resolver (the proposal's harness/model), a
    store-backed per-agent override reader, and an awaiting-approval lister (the
    parked-for-review id set — which is NOT on the port, it lives in
    `settings.list_awaiting_approval`); it imports NOTHING outward (no fastapi /
    httpx / subprocess / psycopg2 / asyncpg) and never reaches back into `app.main`,
    the concrete `app.appdb` / `app.adapters`, the concrete `app.harness` /
    `app.harness`, or the `app.orchestrator` imperative core. The roster is passed
    IN by the caller (the agents `agents=` pattern). The shaping + proposal logic
    lifted 1:1 from `main._dispatch_is_open` / `_agent_index` / `_normalize_target` /
    `_proposed_agent` / `_dispatch_row` / `_dispatch_rows` (+ the board counts/flag
    reads from `_dispatch_context`).
  * `api.py` — the imperative SHELL: a FastAPI `APIRouter` whose
    `GET /dispatch/{project}/board` resolves the `CortexMemoryPort` (bound to the
    path project) + the `OperationalStorePort` + the Cortex roster source from
    `app.state` (via `Depends`), constructs the service over them (injecting the real
    `harness`-backed resolver + the `settings.list_awaiting_approval` lister), and
    returns the shaped JSON. The only part that imports fastapi.

`main.py` mounts the router additively and the existing HTML Dispatch center
delegates its board substance to `DispatchService` — one source of the board logic,
multiple surfaces.

The module-isolation arrow is enforced by `.importlinter`'s `modules-are-independent`
(independence) contract: `app.dispatch` may import the ports/domain but not the other
feature modules — the 5th fitness gate fails if it regresses.
"""

from app.dispatch.api import router
from app.dispatch.service import DispatchService

__all__ = ["DispatchService", "router"]
