"""Autonomy go-live: the engine starts/stops LIVE.

The console used to build the orchestrator ONCE at boot, gated on
``harness_autostart``. If that was OFF at boot, ``app.state.orchestrator`` stayed
None forever — flipping a project's autonomy ON did nothing until a full console
restart. These tests pin the two module-level pieces that fix that:

  * ``_engine_wanted(app)`` — the "should the engine run?" decision. True when the
    operator pre-warmed via ``harness_autostart``, OR when any project has autonomy
    ON (so a single project's toggle is enough), and False when both are off/empty.
    NEVER raises: a settings read that blows up degrades to "engine off".
  * ``_engine_supervisor(app, stop)`` — the live reconciler. When the engine is
    wanted and not running, ONE tick builds + starts the orchestrator and stashes it
    on ``app.state.orchestrator``; when it is no longer wanted, one tick stops + clears
    it.

Both are module-level (taking ``app``) precisely so they are testable without
standing up the FastAPI lifespan. No live Cortex / app-DB is needed: the settings
load and the autonomous-project reader are monkeypatched, and the orchestrator is a
stub.
"""
import asyncio
import types

import pytest

import app.main as main
import app.orchestrator as orch


def _fake_app():
    """A minimal stand-in for the FastAPI app with just the .state the engine
    helpers touch (orchestrator slot only)."""
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace(orchestrator=None)
    return app


# ---------------------------------------------------------------------------
#  _engine_wanted
# ---------------------------------------------------------------------------

async def test_engine_wanted_true_when_harness_autostart_on(monkeypatch):
    # harness_autostart ON short-circuits to True even with no autonomous project.
    monkeypatch.setattr("app.settings.load", lambda: {"harness_autostart": True})

    async def _no_projects():
        return []

    monkeypatch.setattr(orch, "_autonomous_projects_async", _no_projects)
    assert await main._engine_wanted(_fake_app()) is True


async def test_engine_wanted_true_when_a_project_is_autonomous(monkeypatch):
    # harness_autostart OFF, but a project has autonomy ON → engine is wanted, so a
    # single project's toggle is enough (the actual go-live fix).
    monkeypatch.setattr("app.settings.load", lambda: {"harness_autostart": False})

    async def _one_project():
        return ["sample-worker"]

    monkeypatch.setattr(orch, "_autonomous_projects_async", _one_project)
    assert await main._engine_wanted(_fake_app()) is True


async def test_engine_wanted_false_when_both_off(monkeypatch):
    monkeypatch.setattr("app.settings.load", lambda: {"harness_autostart": False})

    async def _no_projects():
        return []

    monkeypatch.setattr(orch, "_autonomous_projects_async", _no_projects)
    assert await main._engine_wanted(_fake_app()) is False


async def test_engine_wanted_false_when_settings_read_raises(monkeypatch):
    # A down app-DB / unreadable settings file must degrade to "engine off", never
    # raise (an outage can't surprise-start autonomy).
    def _boom():
        raise RuntimeError("settings store down")

    monkeypatch.setattr("app.settings.load", _boom)

    async def _no_projects():
        return []

    monkeypatch.setattr(orch, "_autonomous_projects_async", _no_projects)
    assert await main._engine_wanted(_fake_app()) is False


async def test_engine_wanted_false_when_project_reader_raises(monkeypatch):
    # harness_autostart OFF and the autonomous-project reader blows up → still False.
    monkeypatch.setattr("app.settings.load", lambda: {})

    async def _boom():
        raise RuntimeError("app-db down")

    monkeypatch.setattr(orch, "_autonomous_projects_async", _boom)
    assert await main._engine_wanted(_fake_app()) is False


# ---------------------------------------------------------------------------
#  _engine_supervisor — one tick reconciliation
# ---------------------------------------------------------------------------

class _StubOrchestrator:
    """Stand-in for Orchestrator with the start()/stop() the supervisor calls."""

    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def _one_tick_then_stop(stop, wanted_value):
    """Build a ``_engine_wanted`` replacement that returns ``wanted_value`` and
    SETS the stop event as a side effect — so the supervisor runs exactly ONE
    reconciliation tick (the loop guard is unset on entry; the body reconciles;
    then the ``wait_for(stop.wait())`` returns immediately and the guard exits).
    This avoids both the never-runs case (stop pre-set) and the 8s real sleep."""

    async def _wanted(_app):
        stop.set()
        return wanted_value

    return _wanted


async def test_supervisor_starts_orchestrator_when_wanted(monkeypatch):
    app = _fake_app()
    assert app.state.orchestrator is None

    built = _StubOrchestrator()
    stop = asyncio.Event()
    monkeypatch.setattr(main, "_engine_wanted", _one_tick_then_stop(stop, True))
    monkeypatch.setattr(main, "_build_orchestrator", lambda _app: built)

    await main._engine_supervisor(app, stop)

    assert app.state.orchestrator is built
    assert built.started is True


async def test_supervisor_stops_orchestrator_when_not_wanted(monkeypatch):
    app = _fake_app()
    running = _StubOrchestrator()
    app.state.orchestrator = running

    stop = asyncio.Event()
    monkeypatch.setattr(main, "_engine_wanted", _one_tick_then_stop(stop, False))

    await main._engine_supervisor(app, stop)

    assert app.state.orchestrator is None
    assert running.stopped is True


async def test_supervisor_noop_when_wanted_and_already_running(monkeypatch):
    # Already running + still wanted → the supervisor must NOT rebuild/restart.
    app = _fake_app()
    running = _StubOrchestrator()
    app.state.orchestrator = running

    stop = asyncio.Event()
    monkeypatch.setattr(main, "_engine_wanted", _one_tick_then_stop(stop, True))

    def _should_not_build(_app):
        raise AssertionError("supervisor rebuilt an already-running orchestrator")

    monkeypatch.setattr(main, "_build_orchestrator", _should_not_build)

    await main._engine_supervisor(app, stop)

    assert app.state.orchestrator is running
    assert running.stopped is False


async def test_supervisor_tick_failure_does_not_propagate(monkeypatch):
    # A tick that raises must be swallowed (the supervisor never crashes the loop /
    # the console). The stop event is set first so the post-tick wait exits at once.
    app = _fake_app()
    stop = asyncio.Event()

    async def _boom(_app):
        stop.set()
        raise RuntimeError("transient reconcile failure")

    monkeypatch.setattr(main, "_engine_wanted", _boom)

    # Must not raise.
    await main._engine_supervisor(app, stop)
    assert app.state.orchestrator is None
