"""AV-2 (a) — tests for the periodic run-state prune scheduler.

`prune_runstate_forever` is the SCHEDULER that finally drives the adapter's
`prune_old` (previously dead code → run_state / run_span grew UNBOUNDED). It mirrors
`providers.refresh_catalog_forever`: prune-then-sleep on an interval, best-effort,
never raises, no-ops on a None store. These are pure unit tests — fakes for the
store + sleep, no DB needed.
"""
from __future__ import annotations

import pytest

from app.adapters import runstate_pg


@pytest.mark.asyncio
async def test_prune_runstate_forever_prunes_each_tick():
    """The loop calls store.prune_old() once per tick and stops cleanly at _max_iters."""

    class FakeStore:
        def __init__(self) -> None:
            self.calls = 0

        async def prune_old(self):
            self.calls += 1
            return 0

    store = FakeStore()
    sleeps: list[int] = []

    async def fake_sleep(s):  # no real wait
        sleeps.append(s)

    await runstate_pg.prune_runstate_forever(
        store, interval_s=123, sleep=fake_sleep, _max_iters=3
    )

    assert store.calls == 3  # pruned once per tick
    assert sleeps == [123, 123, 123]  # slept the configured interval each tick


@pytest.mark.asyncio
async def test_prune_runstate_forever_survives_a_prune_error():
    """A failing prune is swallowed — the loop keeps ticking, never crashes the console."""

    ticks = {"n": 0}

    class BoomStore:
        async def prune_old(self):
            ticks["n"] += 1
            raise RuntimeError("app-DB down")

    async def fake_sleep(_s):
        return None

    # Must return normally (not raise) despite every prune_old raising.
    await runstate_pg.prune_runstate_forever(
        BoomStore(), sleep=fake_sleep, _max_iters=2
    )

    assert ticks["n"] == 2  # both ticks ran past the exception


@pytest.mark.asyncio
async def test_prune_runstate_forever_noop_when_store_none():
    """A None store (app-DB down / store failed to construct) is a clean no-op — the
    loop still ticks (so it picks up a later store) but never touches anything."""

    sleeps: list[int] = []

    async def fake_sleep(s):
        sleeps.append(s)

    # No store, no crash; the loop simply sleeps through its iterations.
    await runstate_pg.prune_runstate_forever(
        None, interval_s=60, sleep=fake_sleep, _max_iters=2
    )

    assert sleeps == [60, 60]


@pytest.mark.asyncio
async def test_prune_runstate_forever_logs_deleted_count():
    """When prune reclaims rows the loop logs the count (best-effort visibility)."""

    class FakeStore:
        async def prune_old(self):
            return 5

    class FakeLog:
        def __init__(self) -> None:
            self.infos: list[tuple] = []

        def info(self, *args):
            self.infos.append(args)

        def warning(self, *args):  # pragma: no cover - not exercised here
            pass

    log = FakeLog()

    async def fake_sleep(_s):
        return None

    await runstate_pg.prune_runstate_forever(
        FakeStore(), sleep=fake_sleep, log=log, _max_iters=1
    )

    assert log.infos and log.infos[0][1] == 5  # logged the trimmed run count
