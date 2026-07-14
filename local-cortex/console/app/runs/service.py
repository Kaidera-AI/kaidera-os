"""Runs READ feature logic — the read side of run state, behind the port.

The functional core of the `runs` module (Track A, the FIFTH and FINAL feature
carve — analytics → agents → settings → dispatch preceded it). It owns the run-state
READ substance:

  1. The agent-detail LIVE-WORK TRANSCRIPT view-model — the recent-run RAIL for one
     agent (newest-first, filtered to that agent) + the SELECTED run's hydrated body
     (a pinned run, else the agent's running run, else its newest), with the friendly
     status chip + relative-age labels. This is the model the HTML agent-detail pane
     AND the SSE `/runstate/stream` first-paint render.
  2. The run-board view-model — the ACTIVE runs (queued|running) + the RECENT run
     headers + the counts.
  3. Single-run reads WITH body — `get_run` (by run id) and `by_handoff` (the latest
     run for a handoff id).

LAYER RULE (arrows point inward, ratified design §3): this module depends ONLY on
`domain.runstate.RunStatePort` (the run-state SSOT) — it imports NOTHING outward (no
fastapi / httpx / subprocess / psycopg2 / asyncpg) and never reaches back into
`app.main`, the concrete `app.appdb` / `app.adapters`, or the `app.orchestrator`
imperative core. The one presentation concern it needs — a relative-age formatter (a
"how long ago" label over an ISO timestamp) — is INJECTED as a plain callable (the
analytics/dispatch injection pattern), so the service stays free of the concrete
`main._activity_relative` / `datetime` shaping; the default below keeps it
self-contained for tests, and the shell (`api.py`) / `main.py` pass the real
`_activity_relative` when wiring so the labels match the UI exactly.

The shaping is lifted 1:1 from `main._store_run_row` / `_store_transcript_view` /
`_agent_runs_view_store` so the carve is behaviour-preserving — `main.py` now
delegates its run-read substance here, making this the single source of that logic.

SCOPE — READ ONLY. The orchestrator's IMPERATIVE core stays in `main.py` /
`orchestrator.py`: the spawn/run path (`_dispatch_run` / `_pm_beat`), Approve & Run,
the autonomy toggle, and the SSE `/runstate/stream` WRITER side (the port's
`start_run` / `append_output` / `set_status` / `heartbeat` / `subscribe`). This
module reads `recent` / `get_run` / `list_active` / `by_handoff` and shapes them; it
spawns nothing and writes nothing.

Graceful-degrade is the house law: a None store (run-state SSOT failed to construct /
app-DB down), a store whose reads RAISE, or simply no runs ALL degrade to the clean
empty state (empty rail/active/recent, None selected, None single-run). Every store
read is wrapped — a store hiccup never blanks a pane. It never raises.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.domain.runstate import RunStatePort

# Cap on recent runs listed in the agent-detail run rail (newest-first; the store
# read path's `store.recent(limit=…)` window). Lifted from
# `orchestrator.TRANSCRIPT_MAX_RUNS` (its default) — kept as a local constant so the
# service stays free of the concrete orchestrator import (the value is overridable at
# construction so `main.py` can thread the env-tuned cap through).
RECENT_RUNS_MAX = 20

# Status → friendly chip word (shared by the rail row + the selected header). Lifted
# 1:1 from `main._RUN_STATUS_LABEL`. The store adds a 'queued' status the in-memory
# store never had (the orchestrator pre-creates the row before the worker starts).
RUN_STATUS_LABEL = {
    "queued": "queued",
    "running": "running",
    "ok": "completed",
    "error": "errored",
}


def _default_relative(ts: Optional[str]) -> str:
    """A compact 'how long ago' label for an ISO-UTC timestamp (the self-contained
    default formatter — pure stdlib `datetime`, the layer rule allows it).

    'now' (<5s) · 'Ns' · 'Nm' · 'Nh' · else 'Nd'. Best-effort — an unparseable or
    absent timestamp degrades to '' (the row just omits the age). This is lifted 1:1
    from `main._activity_relative`, so the module is fully self-contained and the JSON
    surface renders identical age labels WITHOUT reaching into `app.main` (the
    transitive-import trap the dispatch carve flagged). `main.py`'s HTML delegation
    still INJECTS its own `_activity_relative` (the same logic) so the two surfaces stay
    byte-for-byte — the injected formatter is the seam, not a hard dependency."""
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - when
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 5:
        return "now"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


class RunsService:
    """The run-state READ surface: the agent run rail + transcript view-model, the
    run board (active + recent), and single-run reads (by id / by handoff) — all over
    a `RunStatePort`.

    Construct with the port (the run-state SSOT) + an injected relative-age formatter
    (defaults to a self-contained no-op so the service is dependency-free; the shell
    injects the real `_activity_relative` so the labels match the UI). The store is
    optional so a caller that already holds it can thread it through; every read
    graceful-degrades, so a None / down / erroring store yields the clean empty state,
    never a 500."""

    def __init__(
        self,
        *,
        store: Optional[RunStatePort] = None,
        relative: Callable[[Optional[str]], str] = _default_relative,
        recent_max: int = RECENT_RUNS_MAX,
    ) -> None:
        self._store = store
        self._relative = relative
        self._recent_max = max(1, int(recent_max or RECENT_RUNS_MAX))

    # -- pure shaping (no port read) — the render mappers, lifted 1:1 ----------

    def store_run_row(self, rec: Any) -> dict:
        """Map a RunRecord HEADER → the recent-run rail row the template renders
        (lifted 1:1 from `main._store_run_row`). Header only — no body. Timestamps are
        already ISO strings on the DTO; the relative-age labels go through the injected
        formatter."""
        status = (rec.status or "").lower()
        hid = rec.handoff_id or None
        return {
            "run_id": rec.run_id,
            "project": rec.project,
            "agent": rec.agent,
            "agent_display": rec.agent_display or rec.agent,
            "handoff_id": hid,
            "handoff_short": hid[:8] if hid else None,
            "model": rec.model,
            "harness": rec.harness,
            "status": status,
            "running": status == "running",
            "started_ts": rec.started_at,
            "updated_ts": rec.updated_at,
            "started_ago": self._relative(rec.started_at),
            "updated_ago": self._relative(rec.updated_at),
            "status_label": RUN_STATUS_LABEL.get(status, status or "—"),
            # The per-run JSONB sidecar (Explain capability). NULL for runs with no
            # sidecar (autonomous / chat) — the safe default. An Explain run stamps
            # {"capability":"explain","artifact_id":…,"target_*":…} on terminal success,
            # so a reader (the explain gallery via GET /runs/run/{id}) can jump from the
            # run to its persisted L5 artifact + label the target. `getattr` keeps the
            # mapper resilient to a header DTO that predates the field.
            "metadata": getattr(rec, "metadata", None),
        }

    def store_transcript_view(self, rec: Any) -> dict:
        """Map a RunRecord WITH spans → the selected-run transcript dict the template
        renders (lifted 1:1 from `main._store_transcript_view`). Adds the body + the
        seg-typed segments (RunSpan.kind → the template's seg-{kind}, 1:1) + the
        ended-age label. `truncated` is False here: the store enforces the per-run char
        cap silently in SQL (it drops overflow rather than flagging it)."""
        base = self.store_run_row(rec)
        segments = [
            {"kind": (s.kind or "output"), "text": (s.text or "")}
            for s in (rec.spans or [])
        ]
        base.update(
            {
                "error": rec.error,
                "ended_ts": rec.ended_at,
                "ended_ago": self._relative(rec.ended_at) if rec.ended_at else "",
                "segments": segments,
                "body": "".join(s["text"] for s in segments),
                "truncated": False,
            }
        )
        return base

    # -- the agent run rail + transcript view-model (reads the port) -----------

    async def agent_runs_view(
        self,
        project_key: Optional[str],
        agent_name: str,
        *,
        run_id: Optional[str] = None,
    ) -> dict:
        """Build the agent-detail LIVE-WORK-TRANSCRIPT context for ONE agent FROM THE
        RUNSTATE STORE (lifted 1:1 from `main._agent_runs_view_store`). Returns the
        `agent_run*` keys the template renders.

        Reads:
          * `store.recent(project)` → the recent-run HEADERS, filtered to THIS agent
            (case-insensitive), newest-first → the run rail;
          * the selected run's BODY via `store.get_run(run_id)` when a run is pinned
            (the live SSE pane carries it), else the agent's RUNNING run if any, else
            its newest run (re-read with spans so the body renders).

        GRACEFUL-DEGRADE (never a 500): a None store, a store whose reads RAISE, or
        simply no runs for this agent ALL degrade to the clean empty state. Every store
        read is wrapped. `agent_run_no_orch` is True only when the store itself is
        absent (run-state SSOT down)."""
        key = (project_key or "").strip().lower()
        target = (agent_name or "").strip().lower()

        rows: list[dict] = []
        selected: dict | None = None

        if self._store is not None and key and target:
            # Recent-run headers for the project, filtered to THIS agent. The store
            # keys each run by its plain (lower-cased) agent name (start_run lowers it).
            all_rows: list[Any] = []
            with suppress(Exception):
                all_rows = await self._store.recent(key, limit=self._recent_max)
            rows = [
                self.store_run_row(rec)
                for rec in all_rows
                if (getattr(rec, "agent", "") or "").strip().lower() == target
            ]

            # Resolve the run whose BODY to show: a pinned run_id (must belong to this
            # agent), else the agent's running run, else its newest — re-read WITH spans.
            with suppress(Exception):
                rec: Any = None
                if run_id:
                    cand = await self._store.get_run(run_id)
                    if cand is not None and (
                        getattr(cand, "agent", "") or ""
                    ).strip().lower() == target:
                        rec = cand
                if rec is None and rows:
                    running = [r for r in rows if r.get("running")]
                    pick = (running or rows)[0]  # rows are newest-first
                    rec = await self._store.get_run(pick.get("run_id"))
                if rec is not None:
                    selected = self.store_transcript_view(rec)

        running_n = sum(1 for r in rows if r.get("running"))
        return {
            "agent_runs": rows,
            "agent_run_count": len(rows),
            "agent_run_running": running_n,
            "agent_run_selected": selected,
            "agent_run_selected_id": (selected or {}).get("run_id") if selected else None,
            # Live SSE drives the pane; this flag only gates the running-dot copy.
            "agent_run_active": bool(selected and selected.get("running")) or running_n > 0,
            # The store is the read model now; the 'no_orch' degrade copy shows only when
            # the store itself is absent (run-state SSOT down).
            "agent_run_no_orch": self._store is None,
        }

    # -- the run board + single-run reads (read the port) ----------------------

    async def active(self, project: Optional[str] = None) -> list[dict]:
        """The ACTIVE runs (queued|running) for a project, shaped as header rows
        (newest-first). [] when no store / a down store / the read raises."""
        if self._store is None:
            return []
        recs: list[Any] = []
        with suppress(Exception):
            recs = await self._store.list_active(project) or []
        return [self.store_run_row(r) for r in recs]

    async def recent(
        self, project: Optional[str] = None, limit: Optional[int] = None
    ) -> list[dict]:
        """The RECENT run headers for a project (newest-first), shaped as header rows.
        [] when no store / a down store / the read raises."""
        if self._store is None:
            return []
        lim = self._recent_max if limit is None else max(1, int(limit))
        recs: list[Any] = []
        with suppress(Exception):
            recs = await self._store.recent(project, limit=lim) or []
        return [self.store_run_row(r) for r in recs]

    async def board(
        self, project: Optional[str] = None, *, recent_limit: Optional[int] = None
    ) -> dict[str, Any]:
        """The run-board view-model: the active runs + the recent run headers + the
        counts, one shaped payload. Reads `list_active` + `recent` through the port;
        graceful-degrades to empty lists / zero counts (never a 500)."""
        active = await self.active(project)
        recent = await self.recent(project, limit=recent_limit)
        return {
            "active": active,
            "active_count": len(active),
            "recent": recent,
            "recent_count": len(recent),
        }

    async def get_run(self, run_id: str) -> Optional[dict]:
        """ONE run WITH its hydrated body (the transcript view-model), or None when
        unknown / no store / the read raises."""
        if self._store is None or not run_id:
            return None
        rec: Any = None
        with suppress(Exception):
            rec = await self._store.get_run(run_id)
        return self.store_transcript_view(rec) if rec is not None else None

    async def by_handoff(self, handoff_id: str) -> Optional[dict]:
        """The LATEST run (WITH body) for a handoff id (the transcript view-model), or
        None when unknown / no store / the read raises."""
        if self._store is None or not handoff_id:
            return None
        rec: Any = None
        with suppress(Exception):
            rec = await self._store.by_handoff(handoff_id)
        return self.store_transcript_view(rec) if rec is not None else None


__all__ = [
    "RunsService",
    "RECENT_RUNS_MAX",
    "RUN_STATUS_LABEL",
]
