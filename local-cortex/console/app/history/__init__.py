"""The `history` feature module — the console-side activity-timeline JSON surface.

The Cortex cross-agent activity timeline (who · what · when) + a recent-decisions feed is
rendered in the SPA as a reverse-chronological timeline (`HistoryView`). THIS module is the
clean backend that feeds it: a small read-only FastAPI surface that shapes Cortex's noisy
`/history` stream into a readable `events` timeline (each row summarised, NOT raw tool-call
JSON), folds a recent-`decisions` feed from `/search`, and a roster `agent_count`.

  * `shape.py` — pure, I/O-free shaping (stdlib only): the PORTED summariser
      (`summarize_row`, lifted 1:1 from `main._summarize_history_row`) + a relative-age
      formatter + the timeline/decisions/roster shaping, bounded so a long window never
      ships whole.
  * `api.py`   — a FastAPI `APIRouter` (the only part that imports fastapi):
      - `GET /history/{project}?limit=N` — `{events, decisions, agent_count}`.

`main.py` mounts the router additively (`app.include_router(history.router)`). It reads the
shared `CortexClient` on `app.state.cortex` (the `get_history` + `search` + `get_roster`
seams, all already graceful-degrading) — no host forward, no DB, no mutation.

SCOPE — read + shape only. No generation, no write; this is a pure projection of the live
Cortex memory, summarised + bounded for the SPA timeline. Mirrors the `app/graph/` Track-A
module shape (a pure `shape.py` + a thin `api.py`).
"""

from app.history.api import router
from app.history.shape import (
    HISTORY_DECISIONS_CAP,
    HISTORY_EVENT_CAP,
    shape_decisions,
    shape_events,
)

__all__ = [
    "router",
    "shape_events",
    "shape_decisions",
    "HISTORY_EVENT_CAP",
    "HISTORY_DECISIONS_CAP",
]
