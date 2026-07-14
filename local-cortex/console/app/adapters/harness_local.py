"""HarnessPort adapter — the LOCAL host-side subprocess spawn (the existing behaviour).

The imperative-shell adapter (`app/adapters/`) that IMPLEMENTS the pure `HarnessPort`
Protocol (`app/domain/harness.py`) over the EXISTING `subprocess.Popen` host spawn —
the SAME spawn the orchestrator's `_dispatch_run` does inline today. Arrows point
inward (ratified design §3): the domain port stays pure; this adapter is the boundary
that talks to the OS process.

BEHAVIOUR-PRESERVING EXTRACTION (not a rewrite): the spawn mechanics are lifted 1:1
from `orchestrator._dispatch_run` —
  argv `[run_agent_script, agent, handoff_id, project, run_id]`,
  `Popen(argv, stdout=DEVNULL, stderr=PIPE, text=True, start_new_session=True)`,
  then `await asyncio.to_thread(proc.communicate, timeout=run_timeout_s)`,
  with a `TimeoutExpired` killing the child + reporting `exit_code=-1`,
  and the last ~300 chars of stderr carried back as `stderr_tail`.
This is what lets a later increment swap in a `RemoteHarnessAdapter` (POST to the
host harness-service) with no change to the orchestrator — both satisfy `HarnessPort`.

FIRE-AND-FORGET + graceful-degrade (the port contract): `spawn_run` NEVER raises. An
`OSError` on `Popen` (script missing / not executable) is reported as
`SpawnHandle(accepted=False, error=…)`, not an exception, so a broken spawn path can
never crash the dispatch loop. `cancel_run` is a best-effort no-op (`return False`)
for I1 — a remote adapter will POST /cancel.

`run_agent_script` + `run_timeout_s` default to the orchestrator's `RUN_AGENT_SCRIPT`
+ `RUN_TIMEOUT_S` (resolved lazily so importing `app.adapters` never eagerly pulls in
the orchestrator); `popen` defaults to the real `subprocess.Popen`. All three are
injectable so tests can drive the adapter with a fake popen + a fixed script/timeout
and assert the spawn contract without firing a real worker.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from typing import Any, Callable, Optional

# How many trailing chars of the worker's stderr to carry back (the orchestrator's
# error feed line uses the tail — that is where the failure message lands).
_STDERR_TAIL_CHARS = 300


class LocalHarnessAdapter:
    """`HarnessPort` over the host-side `subprocess.Popen` spawn (the existing path).

    Constructed with no args in production (binds the orchestrator's RUN_AGENT_SCRIPT
    + RUN_TIMEOUT_S and the real `subprocess.Popen`); tests inject `run_agent_script`
    / `run_timeout_s` / `popen`. Satisfies the `HarnessPort` Protocol structurally."""

    def __init__(
        self,
        run_agent_script: Optional[str] = None,
        run_timeout_s: Optional[float] = None,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        # Resolve the orchestrator defaults LAZILY (only when not injected) so that
        # `import app.adapters` does not eagerly import the orchestrator module.
        if run_agent_script is None or run_timeout_s is None:
            from app import orchestrator as _orch

            if run_agent_script is None:
                run_agent_script = _orch.RUN_AGENT_SCRIPT
            if run_timeout_s is None:
                run_timeout_s = _orch.RUN_TIMEOUT_S
        self._run_agent_script = run_agent_script
        self._run_timeout_s = float(run_timeout_s)
        self._popen = popen

    async def spawn_run(self, request):
        """Spawn the worker as its OWN OS process and await its exit (off the event
        loop). Returns a `SpawnHandle`; NEVER raises (an OSError on spawn → an
        accepted=False handle). Byte-for-byte the orchestrator's inline host spawn."""
        from app.domain.harness import SpawnHandle

        # argv: the legacy host-spawn order. run_id is appended when the caller has
        # pre-created a run_state row; blank preserves the old 4-arg degrade path.
        argv = [
            self._run_agent_script,
            request.agent,
            request.handoff_id,
            request.project,
        ]
        if getattr(request, "run_id", None):
            argv.append(request.run_id)
        timeout = request.run_timeout_s or self._run_timeout_s
        repo_root = (getattr(request, "repo_root", None) or "").strip() or None

        from app.harness_runner import _apply_project_workspace

        popen_kwargs = {
            "cwd": repo_root or None,
            "env": _apply_project_workspace(dict(os.environ), request.project, repo_root),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "text": True,
            "start_new_session": True,
        }

        # SPAWN — argv LIST (no shell → no injection surface), detached into its own
        # session, stdout discarded, stderr captured. An OSError (e.g. the script is
        # missing / not executable) is reported, never raised (fire-and-forget).
        try:
            proc = self._popen(argv, **popen_kwargs)
        except OSError as exc:
            return SpawnHandle(run_id=request.run_id, accepted=False, error=str(exc))

        # AWAIT the worker's exit OFF the event loop so the wait never blocks it and
        # concurrent dispatches can't starve the child's stderr drain.
        try:
            _out, err = await asyncio.to_thread(proc.communicate, timeout=timeout)
        except asyncio.CancelledError:
            with _suppress():
                if proc.poll() is None:
                    _kill_worker_session(proc)
                    await asyncio.to_thread(proc.wait)
            raise
        except subprocess.TimeoutExpired:
            # The worker is a session leader. Kill its whole process group so a
            # harness/tool child cannot keep mutating after the scheduler retries.
            _kill_worker_session(proc)
            with _suppress():
                await asyncio.to_thread(proc.wait)
            return SpawnHandle(
                run_id=request.run_id,
                accepted=True,
                exit_code=-1,
                error=f"run-agent timed out after {timeout:.0f}s",
            )

        rc = proc.returncode
        stderr_tail = ((err or "").strip())[-_STDERR_TAIL_CHARS:] or None
        return SpawnHandle(
            run_id=request.run_id,
            accepted=True,
            exit_code=rc,
            stderr_tail=stderr_tail,
        )

    async def cancel_run(self, run_id: str) -> bool:
        """Best-effort cancel — a no-op for the local adapter (I1). NEVER raises. A
        remote harness-service adapter (I2) will POST /cancel and return its result."""
        return False

    async def spawn_chat(self, request) -> "object":
        """No-op for the local adapter (harness-service I4). NEVER raises.

        In local/legacy mode the chat route runs `stream_chat` IN-PROCESS — the console
        host already has the harness CLIs + their OAuth login, so there is NO host seam
        to cross for chat (unlike a container, where the REMOTE adapter POSTs /chat).
        Returning `accepted=False` keeps the chat route on its existing in-process path:
        the route only takes the remote branch when an adapter ACCEPTS the chat spawn."""
        from app.domain.harness import SpawnHandle

        return SpawnHandle(run_id=request.run_id, accepted=False)


def _kill_worker_session(proc: Any) -> None:
    """Kill a detached worker and every child that inherited its process group."""
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except (OSError, ValueError):
            pass
    with _suppress():
        proc.kill()


class _suppress:
    """Tiny inline `contextlib.suppress(Exception)` — the kill/reap on the timeout
    path must never raise (the child may already be gone). Kept local so the adapter
    needs no extra import."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True  # swallow any exception raised in the with-body


__all__ = ["LocalHarnessAdapter"]
