"""Task 2 (Dispatch gate) TDD — RED tests.

Tests for the propose_mode gate inside Orchestrator._maybe_dispatch:

  * When propose_mode is ON for a project, _maybe_dispatch must NOT spawn
    and must record the handoff as awaiting-approval + emit an ActivityFeed line.
  * When propose_mode is OFF (the default), _maybe_dispatch still spawns
    exactly as before (no change for existing projects).
  * Idempotency: the same handoff is only gated once (second call is a no-op
    because it is already in _dispatched or already awaiting-approval).

These tests stub out all Cortex/app-DB calls so no live service is needed.
"""
import subprocess

import pytest

import app.orchestrator as orch
import app.settings as settings_store
from app import appdb as appdb_mod
from app.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
#  Re-use the _FakeProc + helpers from test_orchestrator_spawn
# ---------------------------------------------------------------------------

class _FakeProc:
    last_argv: list[str] | None = None
    next_rc: int = 0

    def __init__(self, argv, **kwargs):
        _FakeProc.last_argv = list(argv)

    def communicate(self, timeout=None):
        self.returncode = _FakeProc.next_rc
        return (None, "")

    def wait(self):
        return _FakeProc.next_rc

    def poll(self):
        return _FakeProc.next_rc

    def kill(self):
        pass


def _reset_fake(*, rc=0):
    _FakeProc.last_argv = None
    _FakeProc.next_rc = rc


# ---------------------------------------------------------------------------
#  Stub helpers for settings + cortex
# ---------------------------------------------------------------------------

class _FakeCortex:
    """Minimal async cortex stub — get_agents returns one autonomous agent."""
    async def get_agents(self, project_key):
        return [{"name": "kai", "display_name": "Kai"}]

    async def get_handoffs(self, project_key):
        return []

    async def orchestration_plan(self, project_key):
        return {}


class _FakeAppDB:
    """Minimal async appdb stub — returns empty plan."""
    async def orchestration_plan(self, project_key):
        return {}


def _make_orch():
    return Orchestrator(
        cortex=_FakeCortex(),
        appdb=_FakeAppDB(),
        harness_runner=object(),
        chat_routing_for=lambda agent, project: ("pi", "gpt-5.3-codex-spark", "high"),
        record_usage=None,
        find_agent=lambda agents, name: None,
        resolve_target=lambda handoff, agents: {"name": "kai", "display_name": "Kai"},
        classify_interactive=lambda agent, desig: False,
        project_identity=lambda cortex, project: None,
        agent_view=lambda a: a,
    )


def _make_propose_db(*, propose_on: bool = False) -> object:
    """Stub _db that controls propose_mode and records awaiting-approval calls."""
    UNAVAILABLE = appdb_mod.UNAVAILABLE
    propose_state: dict[str, bool] = {}
    awaiting: dict[tuple, bool] = {}

    class _StubDB:
        # ---- propose_mode ----
        def get_project_propose_mode(self, project):
            if propose_on:
                return True
            return propose_state.get((project or "").strip().lower(), False)

        def set_project_propose_mode(self, project, enabled, updated_by=None):
            propose_state[(project or "").strip().lower()] = bool(enabled)
            return True

        # ---- pending_approval (status-based, new gate design) ----
        def get_approval_status(self, project, handoff_id):
            # Returns 'awaiting', 'approved', or None (no row).
            return awaiting.get((project, handoff_id), None)

        def set_approval_status(self, project, handoff_id, status):
            awaiting[(project, handoff_id)] = status
            return True

        def list_awaiting_approval(self, project):
            # Only 'awaiting' rows appear in the approval queue.
            return [hid for (proj, hid), s in awaiting.items()
                    if proj == project and s == "awaiting"]

        # ---- legacy pending_approval methods (kept for settings_store compat) ----
        def set_awaiting_approval(self, project, handoff_id):
            awaiting[(project, handoff_id)] = "awaiting"
            return True

        def clear_awaiting_approval(self, project, handoff_id):
            awaiting.pop((project, handoff_id), None)
            return True

        def is_awaiting_approval(self, project, handoff_id):
            return awaiting.get((project, handoff_id)) == "awaiting"

        # ---- project_autonomy (needed for _autonomous_projects_async) ----
        def get_project_autonomy(self, project):
            return True  # always ON so the OFF-gate passes

        def list_autonomous_projects(self):
            return ["kaidera-os"]

        # ---- agent_settings / app_settings (not needed but keep stub safe) ----
        def get_agent_designation(self, project, agent):
            return UNAVAILABLE

        def get_agent_override(self, project, agent):
            return {}

        def load_agent_overrides(self):
            return {}

        def load_app_settings(self):
            return {}

    stub = _StubDB()
    stub._awaiting = awaiting  # expose for assertions (maps (proj, hid) → status str)
    return stub


def _feed_kinds(o, project="kaidera-os"):
    return [e.get("kind") for e in o.feed.recent(project)]


def _feed_texts(o, project="kaidera-os"):
    return [e.get("text", "") for e in o.feed.recent(project)]


def _make_orch_interactive():
    """Variant where the resolved target is classified INTERACTIVE (lead/CPO)."""
    async def _noop_pm_beat(project_key: str, *, reason: str) -> None:
        return None

    o = Orchestrator(
        cortex=_FakeCortex(),
        appdb=_FakeAppDB(),
        harness_runner=object(),
        chat_routing_for=lambda agent, project: ("pi", "gpt-5.3-codex-spark", "high"),
        record_usage=None,
        find_agent=lambda agents, name: None,
        resolve_target=lambda handoff, agents: {"name": "sample-worker", "display_name": "Sample Worker", "role": "lead"},
        classify_interactive=lambda agent, desig: True,
        project_identity=lambda cortex, project: None,
        agent_view=lambda a: a,
    )
    o._pm_beat = _noop_pm_beat  # type: ignore[method-assign]
    return o


@pytest.mark.asyncio
async def test_interactive_agent_left_for_human_by_default(monkeypatch):
    """An interactive-designated target is skipped by default — never dispatched."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=0)
    stub_db = _make_propose_db(propose_on=False)
    monkeypatch.setattr(settings_store, "_db", stub_db)
    # ensure the global opt-in is OFF
    monkeypatch.setattr(orch, "DISPATCH_INTERACTIVE", False)

    o = _make_orch_interactive()
    handoff = {"id": "h-int0001", "status": "pending", "summary": "cmo work",
               "to_role": "cmo"}
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    # No spawn, handoff marked seen, feed says interactive.
    assert _FakeProc.last_argv is None
    assert ("kaidera-os", "h-int0001") in o._dispatched
    assert any("interactive" in t for t in _feed_texts(o))


@pytest.mark.asyncio
async def test_interactive_agent_dispatched_when_opt_in_enabled(monkeypatch):
    """ORCH_DISPATCH_INTERACTIVE=1 bypasses the interactive guard and dispatches
    the interactive-designated target."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=0)
    stub_db = _make_propose_db(propose_on=False)
    monkeypatch.setattr(settings_store, "_db", stub_db)
    monkeypatch.setattr(orch, "DISPATCH_INTERACTIVE", True)

    o = _make_orch_interactive()
    handoff = {"id": "h-int0002", "status": "pending", "summary": "cmo work",
               "to_role": "cmo"}
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    # Let the spawned dispatch task run to completion (it calls subprocess.Popen).
    for task in list(o._runs):
        await task

    # It DID spawn because the opt-in is enabled, and the slot is released after.
    assert _FakeProc.last_argv is not None
    assert ("kaidera-os", "h-int0002") in o._dispatched
    assert o._inflight.get("kaidera-os", 0) == 0  # slot released after task completion
    assert any("ORCH_DISPATCH_INTERACTIVE" in t for t in _feed_texts(o))


@pytest.mark.asyncio
async def test_interactive_agent_dispatched_when_auto_dispatch_enabled(monkeypatch):
    """A chat-capable lead can still execute queued work when the per-agent
    auto_dispatch setting is explicitly enabled."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=0)
    stub_db = _make_propose_db(propose_on=False)
    monkeypatch.setattr(settings_store, "_db", stub_db)
    monkeypatch.setattr(orch, "DISPATCH_INTERACTIVE", False)

    async def _auto_dispatch(project_key, agent_name):
        assert project_key == "kaidera-os"
        assert agent_name
        return "true"

    monkeypatch.setattr(orch, "_agent_auto_dispatch_async", _auto_dispatch)

    o = _make_orch_interactive()
    handoff = {"id": "h-int0003", "status": "pending", "summary": "cmo work",
               "to_role": "cmo"}
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    for task in list(o._runs):
        await task

    assert _FakeProc.last_argv is not None
    assert ("kaidera-os", "h-int0003") in o._dispatched
    assert o._inflight.get("kaidera-os", 0) == 0
    assert any("auto_dispatch=true" in t for t in _feed_texts(o))


@pytest.mark.asyncio
async def test_cap_deferral_logs_once_then_logs_routing_when_dispatched(monkeypatch):
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=0)
    monkeypatch.setattr(settings_store, "_db", _make_propose_db(propose_on=False))
    monkeypatch.setattr(orch, "DISPATCH_INTERACTIVE", False)

    async def _auto_dispatch(project_key, agent_name):
        return "true"

    monkeypatch.setattr(orch, "_agent_auto_dispatch_async", _auto_dispatch)
    instance = _make_orch_interactive()
    instance._inflight["kaidera-os"] = orch.MAX_CONCURRENT
    handoff = {
        "id": "h-cap0001",
        "status": "pending",
        "summary": "queued lead work",
        "to_role": "cmo",
    }

    await instance._maybe_dispatch("kaidera-os", handoff, source="poll")
    await instance._maybe_dispatch("kaidera-os", handoff, source="poll")

    texts = _feed_texts(instance)
    assert sum("at cap" in text for text in texts) == 1
    assert not any("auto_dispatch=true" in text for text in texts)

    instance._inflight["kaidera-os"] = 0
    await instance._maybe_dispatch("kaidera-os", handoff, source="poll")
    for task in list(instance._runs):
        await task

    texts = _feed_texts(instance)
    assert sum("auto_dispatch=true" in text for text in texts) == 1
    assert ("kaidera-os", "h-cap0001") not in instance._cap_deferred_logged


# ---------------------------------------------------------------------------
#  Task 2 tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_mode_on_does_not_spawn(monkeypatch):
    """A ready handoff in a propose-mode project is NOT spawned."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake()
    stub_db = _make_propose_db(propose_on=True)
    monkeypatch.setattr(settings_store, "_db", stub_db)

    o = _make_orch()
    handoff = {"id": "h-gate0001", "status": "pending", "summary": "do the thing",
               "to_agent": "kai"}
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    # Worker must NOT have been launched.
    assert _FakeProc.last_argv is None, "spawn was called but should not have been"


@pytest.mark.asyncio
async def test_propose_mode_on_records_awaiting_approval(monkeypatch):
    """A gated handoff is recorded as awaiting-approval in the app-DB."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake()
    stub_db = _make_propose_db(propose_on=True)
    monkeypatch.setattr(settings_store, "_db", stub_db)

    o = _make_orch()
    handoff = {"id": "h-gate0002", "status": "pending", "summary": "work",
               "to_agent": "kai"}
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    # The handoff must appear in the awaiting-approval store.
    assert stub_db._awaiting.get(("kaidera-os", "h-gate0002")), (
        "handoff not recorded as awaiting-approval"
    )


@pytest.mark.asyncio
async def test_propose_mode_on_emits_feed_line(monkeypatch):
    """Gating a handoff emits an ActivityFeed 'awaiting_approval' entry."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake()
    stub_db = _make_propose_db(propose_on=True)
    monkeypatch.setattr(settings_store, "_db", stub_db)

    o = _make_orch()
    handoff = {"id": "h-gate0003", "status": "pending", "summary": "work",
               "to_agent": "kai"}
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    kinds = _feed_kinds(o)
    assert "awaiting_approval" in kinds, f"expected 'awaiting_approval' in feed, got {kinds}"


@pytest.mark.asyncio
async def test_propose_mode_off_still_dispatches(monkeypatch):
    """When propose_mode is OFF (default), the handoff is dispatched (inflight incremented,
    task created, NOT recorded as awaiting-approval)."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake(rc=0)
    stub_db = _make_propose_db(propose_on=False)
    monkeypatch.setattr(settings_store, "_db", stub_db)

    o = _make_orch()
    handoff = {"id": "h-nogate01", "status": "pending", "summary": "auto work",
               "to_agent": "kai"}
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    # The handoff was dispatched: it's in _dispatched and inflight was incremented.
    assert ("kaidera-os", "h-nogate01") in o._dispatched, (
        "handoff not in _dispatched after propose_mode=OFF dispatch"
    )
    assert o._inflight.get("kaidera-os", 0) >= 1, (
        "inflight not incremented (dispatch task not started)"
    )
    # Must NOT be in the awaiting-approval store.
    assert not stub_db._awaiting.get(("kaidera-os", "h-nogate01")), (
        "handoff was incorrectly recorded as awaiting-approval when propose_mode is OFF"
    )
    for task in list(o._runs):
        await task


@pytest.mark.asyncio
async def test_propose_mode_gate_idempotent_second_call_noop(monkeypatch):
    """The same handoff is only gated once — second call is a silent no-op.

    With the new status-based gate:
      * First call: status is None → writes 'awaiting', emits feed line, gates.
      * Second call: status is 'awaiting' → gates silently (no re-log, no re-write).
    The feed should have exactly ONE 'awaiting_approval' entry (not two).
    The handoff is never added to _dispatched (so both calls re-evaluate from DB)."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _reset_fake()
    stub_db = _make_propose_db(propose_on=True)
    monkeypatch.setattr(settings_store, "_db", stub_db)

    o = _make_orch()
    handoff = {"id": "h-gate0004", "status": "pending", "summary": "work",
               "to_agent": "kai"}
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    # Snapshot: status written, feed has one awaiting_approval entry.
    assert stub_db._awaiting.get(("kaidera-os", "h-gate0004")) == "awaiting"
    feed_after_first = [e for e in o.feed.recent("kaidera-os")
                        if e.get("kind") == "awaiting_approval"]
    assert len(feed_after_first) == 1, f"expected 1 awaiting_approval feed entry, got {feed_after_first}"

    # Handoff must NOT be in _dispatched (new gate design: no _dispatched add for gated handoffs).
    assert ("kaidera-os", "h-gate0004") not in o._dispatched, (
        "gated handoff must NOT be in _dispatched (new gate design)"
    )

    # Second call — status is 'awaiting' → gates silently; no re-log, no re-write.
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")

    feed_after_second = [e for e in o.feed.recent("kaidera-os")
                         if e.get("kind") == "awaiting_approval"]
    assert len(feed_after_second) == 1, (
        f"feed line emitted twice; expected 1 awaiting_approval, got {feed_after_second}"
    )
    # Status unchanged (still 'awaiting', not re-written to overwrite).
    assert stub_db._awaiting.get(("kaidera-os", "h-gate0004")) == "awaiting"
