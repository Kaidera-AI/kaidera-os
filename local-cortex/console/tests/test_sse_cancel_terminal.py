"""The SSE-disconnect orphan fix (2026-06-22).

When a browser drops the chat/dispatch SSE connection, Starlette cancels the generator;
`asyncio.CancelledError`/`GeneratorExit` are BaseExceptions that bypass `except Exception`,
so the terminal `_rs_status` never ran and the run was orphaned at 'running' forever.

Both generators now catch `(CancelledError, GeneratorExit)`, call `_mark_run_cancelled`
(best-effort, shielded terminal write), and re-raise. These pin that helper's contract.
"""
from __future__ import annotations

import pytest

from app import main


@pytest.mark.asyncio
async def test_mark_run_cancelled_writes_error_terminal():
    calls: list[tuple[str, dict]] = []

    async def fake_rs(status, **kw):
        calls.append((status, kw))

    await main._mark_run_cancelled(fake_rs)

    assert len(calls) == 1
    status, kw = calls[0]
    assert status == "error"  # the run is marked terminal, NOT left at 'running'
    assert "cancel" in kw["error"].lower() or "disconnect" in kw["error"].lower()


@pytest.mark.asyncio
async def test_mark_run_cancelled_never_raises_when_store_fails():
    async def boom(status, **kw):
        raise RuntimeError("app-DB down")

    # It runs while the generator is being torn down (in the except arm before `raise`),
    # so it MUST be best-effort and never raise — otherwise it would mask the cancellation.
    await main._mark_run_cancelled(boom)
