"""Dispatch worker command — the shared detached-worker execution seam.

This module owns the worker-spawn lifecycle common to autonomous dispatch and the
remote/manual Approve & Run path:

  1. pre-create the durable run_state row when a store is available,
  2. call a HarnessPort with one SpawnRequest,
  3. persist the terminal result returned by a synchronous adapter,
  4. terminalize the pre-created row when the worker never starts, and
  5. return a small outcome DTO for the caller to expose/log.

It does not know about FastAPI, ActivityFeed, or the scheduler loop. Callers keep
their presentation policy; this module keeps the execution contract in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
import uuid
from typing import Any

from app.domain.harness import SpawnHandle, SpawnRequest


@dataclass
class DispatchWorkerSpec:
    project: str
    agent: str
    handoff_id: str
    agent_display: str | None = None
    harness: str | None = None
    model: str | None = None
    repo_root: str | None = None
    lease_owner: str = "dispatch"
    run_timeout_s: float = 900.0
    run_id: str | None = None
    require_run_id: bool = True


@dataclass
class DispatchWorkerOutcome:
    run_id: str
    accepted: bool
    exit_code: int | None
    stderr_tail: str | None
    error: str | None
    elapsed_s: float

    @property
    def status(self) -> str:
        if not self.accepted:
            return "rejected"
        if self.exit_code is None:
            return "dispatched"
        if self.exit_code in (0, 2):
            return "ok"
        return "error"


async def dispatch_worker(
    spec: DispatchWorkerSpec,
    *,
    runstate: Any | None,
    harness_port: Any,
) -> DispatchWorkerOutcome:
    """Pre-create run-state, spawn one worker through HarnessPort, return outcome.

    The port contract says `spawn_run` never raises, but this function still catches a
    misbehaving adapter so a bad harness seam cannot crash the route/scheduler.
    """
    run_id = spec.run_id or (str(uuid.uuid4()) if spec.require_run_id else "")
    if runstate is not None:
        requested_run_id = spec.run_id or str(uuid.uuid4())
        run_id = requested_run_id
        try:
            rec = await runstate.start_run(
                run_id=requested_run_id,
                project=spec.project,
                agent=spec.agent,
                agent_display=spec.agent_display or spec.agent,
                handoff_id=spec.handoff_id or None,
                harness=spec.harness,
                model=spec.model,
                lease_owner=spec.lease_owner,
            )
            run_id = getattr(rec, "run_id", None) or run_id
        except Exception:
            # Store writes are best-effort. The worker still runs; if it cannot write
            # run-state either, it degrades internally.
            run_id = spec.run_id or (str(uuid.uuid4()) if spec.require_run_id else "")

    started = time.monotonic()
    try:
        handle = await harness_port.spawn_run(
            SpawnRequest(
                run_id=run_id,
                project=spec.project,
                agent=spec.agent,
                handoff_id=spec.handoff_id,
                harness=spec.harness,
                model=spec.model,
                repo_root=spec.repo_root,
                run_timeout_s=spec.run_timeout_s,
            )
        )
    except Exception as exc:  # pragma: no cover - defensive against bad adapters
        handle = SpawnHandle(run_id=run_id, accepted=False, error=str(exc))
    elapsed = time.monotonic() - started

    if not handle.accepted:
        await terminalize_unstarted_run(
            runstate, run_id, handle.error or "worker spawn rejected"
        )
    elif handle.exit_code is not None and runstate is not None and run_id:
        # A synchronous adapter has observed the definitive process exit. Reinforce
        # the worker's own terminal write here; on timeout the worker is killed and
        # cannot run its finally/terminal path itself. Async adapters return
        # exit_code=None and remain worker-owned.
        try:
            if handle.exit_code == 0:
                await runstate.set_status(run_id, "ok")
            elif handle.exit_code != 2:
                failure = (
                    handle.error
                    or handle.stderr_tail
                    or f"run-agent exited {handle.exit_code}"
                )
                await runstate.set_status(run_id, "error", error=failure[-300:])
        except Exception:
            pass

    return DispatchWorkerOutcome(
        run_id=run_id,
        accepted=bool(handle.accepted),
        exit_code=handle.exit_code,
        stderr_tail=handle.stderr_tail,
        error=handle.error,
        elapsed_s=elapsed,
    )


async def terminalize_unstarted_run(
    runstate: Any | None, run_id: str | None, error: str | None
) -> None:
    """Best-effort terminal status for a pre-created row when no worker started."""
    if not runstate or not run_id:
        return
    try:
        await runstate.set_status(run_id, "error", error=(error or "spawn failed")[-300:])
    except Exception:
        pass


__all__ = [
    "DispatchWorkerSpec",
    "DispatchWorkerOutcome",
    "dispatch_worker",
    "terminalize_unstarted_run",
]
