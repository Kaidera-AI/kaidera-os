"""App-local run task registry for in-process detached console runs.

Only local chat / Approve & Run tasks live here. Remote harness-service runs are
cancelled through the HarnessPort instead.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any


LOCAL_RUN_CANCELLED_ERROR = "run cancelled by operator"


def local_run_registry(state: Any) -> dict[str, asyncio.Task[Any]]:
    """Return the app-local run registry, creating it if a narrow test fake omitted it."""
    registry = getattr(state, "local_run_tasks", None)
    if registry is None:
        registry = {}
        setattr(state, "local_run_tasks", registry)
    return registry


def register_local_run_task(
    state: Any, run_id: str, task: asyncio.Task[Any]
) -> asyncio.Task[Any]:
    """Track one detached local run task and remove it when the task finishes."""
    registry = local_run_registry(state)
    previous = registry.get(run_id)
    if previous is not None and previous is not task and not previous.done():
        previous.cancel()
    registry[run_id] = task

    def _discard(done: asyncio.Task[Any]) -> None:
        if registry.get(run_id) is done:
            registry.pop(run_id, None)
        with suppress(BaseException):
            done.exception()

    task.add_done_callback(_discard)
    return task


def cancel_registered_local_run(state: Any, run_id: str) -> bool:
    """Best-effort cancel of a registered local task. Unknown/done is a clean no-op."""
    task = local_run_registry(state).get(run_id)
    if task is None:
        return False
    if task.done():
        local_run_registry(state).pop(run_id, None)
        return False
    task.cancel()
    return True


async def shutdown_local_run_tasks(state: Any, *, timeout_s: float = 5.0) -> None:
    """Cancel all app-local detached run tasks and wait briefly before shutdown."""
    registry = local_run_registry(state)
    tasks = [task for task in registry.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        with suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout_s,
            )
    registry.clear()


__all__ = [
    "LOCAL_RUN_CANCELLED_ERROR",
    "cancel_registered_local_run",
    "local_run_registry",
    "register_local_run_task",
    "shutdown_local_run_tasks",
]
