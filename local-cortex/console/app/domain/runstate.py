"""RunStatePort — the SINGLE-SOURCE-OF-TRUTH interface for live run state (PURE).

This is the functional core for run state (core re-architecture, Milestone 1).
It defines the data shapes (`RunSpan`, `RunRecord`) and the `RunStatePort`
Protocol that the rest of the app depends on. It is deliberately PURE: it imports
only the standard library — NO httpx / fastapi / subprocess / psycopg2 / asyncpg.
Arrows point inward (ratified design §3): the Pg adapter (`app/adapters/
runstate_pg.py`, T3) IMPLEMENTS this Protocol; the orchestrator, the detached
worker, interactive chat, "Approve & Run", and the watchdog all depend on the
Protocol — never on a concrete store. A guard test asserts the import purity.

Why a Protocol (structural typing): the in-memory `TranscriptStore`
(`orchestrator.py`) and the Pg adapter both satisfy this surface, so the backing
store can be swapped behind FastAPI `Depends` with near-zero call-site churn. The
method set is a SUPERSET of the existing `TranscriptStore` API
(`start_run / append_output(≈append) / get_run / recent / by_handoff`) plus the
NEW signals that fix the visibility pain: `heartbeat`, `set_status`,
`list_active`, and `subscribe` (the LISTEN/NOTIFY live push).

The DTOs mirror the app-DB schema in `.agents/data/appdb/2026-06-05-runstate.sql`
(run_state + run_span). They are plain dataclasses so they round-trip through
`dataclasses.asdict` for JSON serialization without pulling in pydantic here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Optional, Protocol, runtime_checkable


@dataclass
class RunSpan:
    """One append-only transcript segment of a run (a `run_span` row).

    `seq` is the per-run monotonic order the writer assigns; (run_id, seq) is the
    idempotency key in the DB (a re-delivered span is a no-op). `kind` is the
    segment type ('think' | 'tool' | 'output' | ...). `ts` is an ISO-8601 string
    (kept as a string so the DTO stays serialization-friendly and dependency-free
    — adapters convert to/from timestamptz at the boundary)."""

    seq: int
    kind: str
    text: str = ""
    ts: Optional[str] = None


@dataclass
class RunRecord:
    """One run's header + live state + telemetry (a `run_state` row).

    Mirrors the run_state table. `spans` is the (optionally hydrated) transcript
    body — empty by default for the recent-runs/list views (header only), filled
    by `get_run`/`by_handoff`. All timestamps are ISO-8601 strings at this layer
    (adapter converts). `status` walks queued → running → ok | error."""

    run_id: str
    project: Optional[str] = None
    agent: Optional[str] = None
    agent_display: Optional[str] = None
    handoff_id: Optional[str] = None
    harness: Optional[str] = None
    model: Optional[str] = None
    # The per-conversation grouping key for interactive chat (feature-gap step 6,
    # Inc B). NULL for every non-session turn (autonomous runs + legacy single-shot
    # chat) — the safe default that preserves today's behaviour; a chat turn that
    # belongs to a conversation carries it so its turns can be threaded into the
    # prompt. Mirrors the run_state.session_id column
    # (.agents/data/appdb/2026-06-07-chat-session.sql).
    session_id: Optional[str] = None
    status: str = "queued"
    error: Optional[str] = None
    pid: Optional[int] = None
    lease_owner: Optional[str] = None
    # A small JSONB sidecar of per-run facts (Explain capability). NULL for every run
    # with no sidecar (autonomous runs, chat turns) — the safe default. An Explain run
    # stamps {"artifact_id": "<uuid>"} on terminal success so a reader (the SPA) can jump
    # from the run to its persisted L5 artifact. Mirrors the run_state.metadata column
    # (.agents/data/appdb/2026-06-07-runstate-metadata.sql).
    metadata: Optional[dict] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cost_est_usd: Optional[float] = None
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    ended_at: Optional[str] = None
    spans: list[RunSpan] = field(default_factory=list)


@runtime_checkable
class RunStatePort(Protocol):
    """The single source of truth for live run state (the port the app depends on).

    Every writer (orchestrator, worker, chat, Approve&Run, watchdog) and every
    reader (crew/agent views, SSE, watchdog) goes through THIS. Implementations
    are async (over the asyncpg pool) and MUST graceful-degrade — a down store
    never breaks a run (the contract lives in the adapter, T3). `runtime_checkable`
    so a stub/adapter can be structurally verified in tests.
    """

    # -- writers ---------------------------------------------------------------

    async def start_run(
        self,
        *,
        run_id: str,
        project: str,
        agent: str,
        agent_display: Optional[str] = None,
        handoff_id: Optional[str] = None,
        harness: Optional[str] = None,
        model: Optional[str] = None,
        pid: Optional[int] = None,
        lease_owner: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> RunRecord:
        """Open (UPSERT) a run row with status='queued' and return it. The caller
        pre-creates `run_id` (a uuid4) so the detached worker can write the same
        row. Idempotent on run_id (re-opening an existing run is safe).

        `session_id` (OPTIONAL, multi-turn chat) is the per-conversation grouping
        key — passed by the chat path so the turn joins a conversation; omitted
        (None) by the worker / single-shot chat (the additive default)."""
        ...

    async def append_output(
        self,
        run_id: str,
        *,
        seq: int,
        kind: str,
        text: str,
    ) -> None:
        """Append one transcript span (a `run_span` row). Idempotent on
        (run_id, seq) — a re-delivered span is a no-op, never a duplicate."""
        ...

    async def set_status(
        self,
        run_id: str,
        status: str,
        *,
        error: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Transition a run's status (queued → running → ok | error) and stamp
        `ended_at` on a terminal status. `error` carries failure detail.

        `metadata` (OPTIONAL, additive) stamps a small JSONB sidecar of per-run facts
        — e.g. the Explain run stamps `{"artifact_id": …}` on terminal success so a
        reader can jump from the run to its persisted L5 artifact. None (the default)
        leaves the column untouched (existing writers are byte-for-byte unchanged)."""
        ...

    async def heartbeat(
        self,
        run_id: str,
        *,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        cost_est_usd: Optional[float] = None,
        pid: Optional[int] = None,
    ) -> None:
        """Bump `heartbeat_at = now()` (the liveness signal the watchdog reads)
        and optionally update the running token/cost totals + pid. Called by the
        worker on a cadence; a stale heartbeat_at means a dead run."""
        ...

    # -- readers (one read model feeds both first-paint HTTP and the SSE push) --

    async def get_run(self, run_id: str) -> Optional[RunRecord]:
        """One run WITH its hydrated spans (body), or None if unknown."""
        ...

    async def list_active(
        self, project: Optional[str] = None
    ) -> list[RunRecord]:
        """Runs currently queued|running (headers, newest-first), optionally
        project-scoped. What the live dashboard / dispatch board shows now."""
        ...

    async def recent(
        self,
        project: Optional[str] = None,
        limit: int = 20,
        *,
        session_id: Optional[str] = None,
        lease_owner: Optional[str] = None,
    ) -> list[RunRecord]:
        """Recent run HEADERS (no body), newest-first, optionally project-scoped.

        `session_id` (OPTIONAL, multi-turn chat) scopes the read to ONE conversation's
        recent turns; None (the default) keeps the project-wide recent-runs behaviour.

        `lease_owner` (OPTIONAL, additive) scopes the read to runs holding that lease —
        e.g. the Explain gallery enumerates `lease_owner='explain'` runs through it (the
        run_state-as-source-of-truth path, NOT Cortex content search). None (the default)
        leaves the read lease-agnostic, so every existing caller is byte-for-byte
        unchanged. The two filters compose (both AND-applied when both are given)."""
        ...

    async def by_handoff(self, handoff_id: str) -> Optional[RunRecord]:
        """The latest run (WITH body) for a handoff id, or None. The crew view
        lands on a handoff and shows its live transcript; the watchdog looks a run
        up by handoff to read its heartbeat_at."""
        ...

    # -- live push -------------------------------------------------------------

    async def subscribe(
        self, project: Optional[str] = None
    ) -> AsyncIterator[str]:
        """LISTEN on the `run_state_events` bus and yield each changed run's
        run_id (optionally filtered to a project). A pure WAKE stream — the caller
        re-reads `get_run`/`list_active` so the push and first-paint never
        disagree. The app-DB twin of `cortex_client.stream_events`.

        Declared as an async generator (the `yield` below makes it one, and makes
        the Protocol member `isasyncgenfunction`); it yields `str` run_ids. The
        bare `yield` here is unreachable scaffolding — implementations override
        the whole method — but it pins the structural shape of the port."""
        if False:  # pragma: no cover - shape-only; implementations override this
            yield ""


__all__ = ["RunSpan", "RunRecord", "RunStatePort"]
