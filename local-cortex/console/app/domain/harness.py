"""HarnessPort — the worker-SPAWN seam the orchestrator dispatches through (PURE).

This is the functional core for the harness-service work (Track B, Increment 1 —
the port seam). It defines the data shapes (`SpawnRequest`, `SpawnHandle`) and the
`HarnessPort` Protocol that the orchestrator depends on to launch an agent worker.
It is deliberately PURE: it imports ONLY the standard library — NO subprocess /
httpx / fastapi / psycopg2 / asyncpg. Arrows point inward (ratified design §3): the
adapters in `app/adapters/` IMPLEMENT this Protocol; the orchestrator depends on the
Protocol, never on a concrete spawn mechanism. A guard test
(`tests/test_harness_port_purity.py`) asserts the import purity, exactly like the
RunStatePort + SDK-ports guards.

Why a Protocol (structural typing): the orchestrator's existing host-side spawn (the
inline `subprocess.Popen` in `_dispatch_run`) and a future REMOTE spawn (a POST to a
host-resident harness-service, once the harness CLIs no longer live in-process) both
satisfy this one surface. The local adapter (`adapters/harness_local.py`,
`LocalHarnessAdapter`) is built in I1 and is byte-for-byte the existing host spawn;
the remote adapter (`RemoteHarnessAdapter`) lands in I2 — and because both implement
THIS port, the orchestrator changes once (the additive fork) and never again.

THE CONTRACT — spawn is FIRE-AND-FORGET and NEVER raises:
  * `spawn_run(request)` launches the worker and returns a `SpawnHandle`. It NEVER
    raises into the caller — a failure to even start the worker is reported as
    `accepted=False` (+ an `error` string), not an exception. This mirrors the
    house graceful-degrade law: a broken spawn path must never crash the dispatch
    loop. The local adapter additionally AWAITS the worker's exit (off the event
    loop) and carries the exit code back in `exit_code` so the orchestrator can map
    it to the same activity-feed outcome as today (0→completed / 2→skipped /
    else→error); a remote/async adapter may instead return `accepted=True` with
    `exit_code=None` (the worker reports its terminal state later via run-state),
    which the orchestrator records as a "dispatched" outcome.
  * `cancel_run(run_id)` is BEST-EFFORT and NEVER raises — True iff a live run was
    found and a cancel was issued, False otherwise (the I1 local adapter is a no-op
    that returns False; a remote adapter would POST /cancel).

The DTOs are plain dataclasses so they round-trip through `dataclasses.asdict` for
JSON serialization without pulling in pydantic here — which is what lets the I2 wire
API (`POST /spawn` / `POST /cancel`) serialize a `SpawnRequest` / `SpawnHandle`
straight across the host boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable


@dataclass
class SpawnRequest:
    """One agent-worker spawn request — the run's scope + its optional routing.

    Mirrors the orchestrator's existing spawn inputs: the pre-minted `run_id` (a
    uuid4 the orchestrator creates so the detached worker writes the SAME run_state
    row), the `project`, the resolved `agent` name, and the `handoff_id` the worker
    claims. `harness` / `model` are the resolved per-agent routing (optional —
    today the worker re-resolves them itself; carried here so a remote harness-
    service can route without a second lookup). `repo_root` is the optional project
    workspace root used by host-side adapters to run the worker in the correct folder
    and scope; None is the legacy cwd fallback. `run_timeout_s` is the hard ceiling on
    the run (the orchestrator's RUN_TIMEOUT_S default)."""

    run_id: str
    project: str
    agent: str
    handoff_id: str
    harness: Optional[str] = None
    model: Optional[str] = None
    repo_root: Optional[str] = None
    run_timeout_s: float = 900.0


@dataclass
class ChatSpawnRequest:
    """One INTERACTIVE-chat turn spawn request (harness-service Increment 4).

    The chat twin of `SpawnRequest`: GIVEN a pre-minted `run_id` (the chat route
    pre-creates the `run_state` row so the UI follows the reply via
    `/runstate/stream`), a `project`, the resolved `agent`, and the operator's
    `message`, the host chat runner runs ONE `stream_chat` turn and writes spans +
    terminal status to that SAME run_state row. There is deliberately NO `handoff_id`
    — a chat is a free-standing run (`lease_owner='chat'`, nothing to claim/complete).
    `harness` / `model` / `reasoning` are the resolved per-agent routing (optional —
    carried so a host runner can route without a second lookup); `repo_root` is the
    optional project workspace root for host-side cwd/scope; `run_timeout_s` is the
    hard ceiling. Round-trips via `dataclasses.asdict` for the I2 wire (`POST /chat`)."""

    run_id: str
    project: str
    agent: str
    message: str
    harness: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    repo_root: Optional[str] = None
    # The per-conversation grouping key (multi-turn chat, feature-gap step 6 Inc B) —
    # carried so the HOST chat runner threads the conversation's prior turns into the
    # prompt (and writes the user message as an `input` span) exactly as the in-process
    # route does. Optional + additive; None for a single-shot turn. Round-trips via
    # `dataclasses.asdict` for the `POST /chat` wire.
    session_id: Optional[str] = None
    # The HOST attachment paths for this turn (chat file-attachments, feature-gap step 6
    # Inc A) — carried so the HOST chat runner inlines the uploaded files into the prompt
    # (the only channel the non-interactive harnesses expose for a file). Optional +
    # additive; empty for a turn with no attachments (→ no behaviour change). The REMOTE
    # adapter forwards the container's uploaded bytes to the host first (via
    # `upload_attachment`) and puts the resulting host paths here. Round-trips via
    # `dataclasses.asdict` for the `POST /chat` wire (`--attachment-paths a,b` argv).
    attachment_paths: List[str] = field(default_factory=list)
    run_timeout_s: float = 900.0


@dataclass
class SpawnHandle:
    """The result of a spawn attempt — accepted/rejected + (when known) the outcome.

    `accepted` is True iff the worker was actually launched (False = the spawn never
    happened, e.g. the script is missing or the harness-service is down — see
    `error`). `exit_code` is the worker's process exit code WHEN the adapter awaited
    it (the local adapter does; `-1` marks a timeout-killed overrun) — or None when
    the adapter returns before the worker terminates (the async "dispatched" shape,
    terminal state arriving later via run-state). `stderr_tail` is the last chunk of
    the worker's stderr (for the error feed line); `error` carries a spawn/await
    failure reason. Round-trips via `dataclasses.asdict` for the I2 wire API."""

    run_id: str
    accepted: bool
    exit_code: Optional[int] = None
    stderr_tail: Optional[str] = None
    error: Optional[str] = None


@runtime_checkable
class HarnessPort(Protocol):
    """The seam the orchestrator dispatches a worker through (the port it depends on).

    The local adapter spawns a host subprocess (the existing behaviour); a platform
    adapter could POST to a remote harness-service — the orchestrator never changes.
    `runtime_checkable` so a stub/adapter can be structurally verified in tests.
    """

    async def spawn_run(self, request: SpawnRequest) -> SpawnHandle:
        """Launch the agent worker for `request`. FIRE-AND-FORGET: this NEVER raises
        — a failure to start the worker is reported as `SpawnHandle(accepted=False,
        error=…)`, never an exception (a broken spawn path must not crash the
        dispatch loop). When the adapter awaits the worker's exit it returns
        `accepted=True` with the `exit_code` (and `stderr_tail`); an async adapter
        may return `accepted=True, exit_code=None` (the worker reports terminal
        state later via run-state)."""
        ...

    async def cancel_run(self, run_id: str) -> bool:
        """Best-effort cancel of a live run. NEVER raises. True iff a run was found
        and a cancel was issued; False otherwise (the local adapter is a no-op that
        returns False; a remote adapter POSTs /cancel)."""
        ...

    async def spawn_chat(self, request: ChatSpawnRequest) -> SpawnHandle:
        """Launch ONE interactive-chat turn for `request` (harness-service I4 — the
        chat host seam). FIRE-AND-FORGET, same contract as `spawn_run`: NEVER raises —
        a failure to start is `SpawnHandle(accepted=False, error=…)`. A remote adapter
        POSTs `/chat` to the host service (which has the CLIs) and returns the async
        'dispatched' shape (`accepted=True, exit_code=None`); the chat runner writes the
        reply to the run-state row the route pre-created (the UI reads
        `/runstate/stream`). The LOCAL adapter is a no-op returning `accepted=False`:
        in local/legacy mode the chat route runs `stream_chat` IN-PROCESS (the console
        host already has the CLIs), so there is no seam to cross."""
        ...


__all__ = ["SpawnRequest", "ChatSpawnRequest", "SpawnHandle", "HarnessPort"]
