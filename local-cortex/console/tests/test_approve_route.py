"""Task 3 (approve route) TDD — tests for the propose-mode approve flow.

Tests for POST /projects/{key}/handoffs/{id}/approve and the supporting
settings_store accessors:
  * The new status-based approve route sets status='approved' (not delete).
  * The gate detects status='approved' and falls through to the normal spawn path.
  * Idempotent: re-approving a handoff that is already 'approved' is a no-op.
  * The safety-critical approve→spawn SEQUENCE: propose_mode ON → ready handoff
    is gated (status='awaiting') → set status='approved' (the approve action) →
    run _maybe_dispatch AGAIN → asserts it SPAWNS (the blocker this test catches).

These tests use the settings_store + a stub _db (no live DB needed).
"""

import app.settings as settings_store
from app import appdb as appdb_mod


def _make_approval_db(*, initially_gated: set | None = None) -> object:
    """A stub _db that tracks awaiting-approval state (as status strings) in memory.

    initially_gated is a set of (project, handoff_id) pairs that are pre-loaded
    with status='awaiting'."""
    UNAVAILABLE = appdb_mod.UNAVAILABLE
    # Maps (project, handoff_id) -> status string ('awaiting' | 'approved')
    awaiting: dict[tuple, str] = {
        (k[0], k[1]): "awaiting" for k in (initially_gated or set())
    }

    class _StubDB:
        # ---- project_propose_mode ----
        def get_project_propose_mode(self, project):
            return False

        def set_project_propose_mode(self, project, enabled, updated_by=None):
            return True

        # ---- pending_approval (status-based, new gate design) ----
        def get_approval_status(self, project, handoff_id):
            return awaiting.get((project, handoff_id), None)

        def set_approval_status(self, project, handoff_id, status):
            awaiting[(project, handoff_id)] = str(status)
            return True

        def list_awaiting_approval(self, project):
            # Only 'awaiting' rows appear in the approval queue.
            return [hid for (proj, hid), s in awaiting.items()
                    if proj == project and s == "awaiting"]

        # ---- legacy pending_approval methods (still present; used by old callers) ----
        def set_awaiting_approval(self, project, handoff_id):
            awaiting[(project, handoff_id)] = "awaiting"
            return True

        def clear_awaiting_approval(self, project, handoff_id):
            awaiting.pop((project, handoff_id), None)
            return True

        def is_awaiting_approval(self, project, handoff_id):
            return awaiting.get((project, handoff_id)) == "awaiting"

        # ---- project_autonomy (needed for broader settings) ----
        def get_project_autonomy(self, project):
            return False

        def list_autonomous_projects(self):
            return []

        # ---- extras ----
        def load_agent_overrides(self):
            return {}

        def load_app_settings(self):
            return {}

    stub = _StubDB()
    stub._awaiting = awaiting  # maps (proj, hid) → status string
    return stub


def test_clear_awaiting_approval_removes_record(monkeypatch):
    """settings_store.clear_awaiting_approval removes the pending_approval row."""
    stub = _make_approval_db(initially_gated={("kaidera-os", "h-approve1")})
    monkeypatch.setattr(settings_store, "_db", stub)

    assert settings_store.is_awaiting_approval("kaidera-os", "h-approve1") is True
    ok = settings_store.clear_awaiting_approval("kaidera-os", "h-approve1")
    assert ok is True
    assert settings_store.is_awaiting_approval("kaidera-os", "h-approve1") is False


def test_clear_awaiting_approval_idempotent_on_missing(monkeypatch):
    """Clearing a record that does not exist is a no-op (returns True, no error)."""
    stub = _make_approval_db()  # no gated handoffs
    monkeypatch.setattr(settings_store, "_db", stub)

    ok = settings_store.clear_awaiting_approval("kaidera-os", "h-not-there")
    assert ok is True  # idempotent: no error


def test_list_awaiting_approval_returns_gated_ids(monkeypatch):
    """list_awaiting_approval returns the handoff IDs currently gated (status='awaiting').
    Approved handoffs do NOT appear in the queue."""
    gated = {("kaidera-os", "h-aa"), ("kaidera-os", "h-bb")}
    stub = _make_approval_db(initially_gated=gated)
    monkeypatch.setattr(settings_store, "_db", stub)

    result = settings_store.list_awaiting_approval("kaidera-os")
    assert set(result) == {"h-aa", "h-bb"}

    # Approving one removes it from the queue.
    settings_store.set_approval_status("kaidera-os", "h-aa", "approved")
    result2 = settings_store.list_awaiting_approval("kaidera-os")
    assert set(result2) == {"h-bb"}, "approved handoff should not appear in awaiting queue"


def test_list_awaiting_approval_empty_for_other_project(monkeypatch):
    """Awaiting-approval records are project-scoped."""
    gated = {("project-x", "h-other")}
    stub = _make_approval_db(initially_gated=gated)
    monkeypatch.setattr(settings_store, "_db", stub)

    result = settings_store.list_awaiting_approval("kaidera-os")
    assert result == []


def test_is_awaiting_approval_false_for_unknown(monkeypatch):
    """is_awaiting_approval returns False for a handoff not in the table."""
    stub = _make_approval_db()
    monkeypatch.setattr(settings_store, "_db", stub)
    assert settings_store.is_awaiting_approval("kaidera-os", "h-unknown") is False


def test_is_awaiting_approval_unavailable_db_returns_false(monkeypatch):
    """When the DB is unreachable, is_awaiting_approval fails-safe to False."""
    UNAVAILABLE = appdb_mod.UNAVAILABLE

    class _UnavailableDB:
        def is_awaiting_approval(self, project, handoff_id):
            return UNAVAILABLE

    monkeypatch.setattr(settings_store, "_db", _UnavailableDB())
    assert settings_store.is_awaiting_approval("kaidera-os", "h-any") is False


# ---------------------------------------------------------------------------
#  New status-based accessors — get_approval_status / set_approval_status
# ---------------------------------------------------------------------------

def test_get_approval_status_none_for_unknown(monkeypatch):
    """get_approval_status returns None for a handoff not yet parked."""
    stub = _make_approval_db()
    monkeypatch.setattr(settings_store, "_db", stub)
    assert settings_store.get_approval_status("kaidera-os", "h-new") is None


def test_get_approval_status_awaiting_after_park(monkeypatch):
    """get_approval_status returns 'awaiting' after the gate parks the handoff."""
    stub = _make_approval_db(initially_gated={("kaidera-os", "h-parked")})
    monkeypatch.setattr(settings_store, "_db", stub)
    assert settings_store.get_approval_status("kaidera-os", "h-parked") == "awaiting"


def test_set_approval_status_approved_returns_approved(monkeypatch):
    """set_approval_status to 'approved' → get_approval_status returns 'approved'."""
    stub = _make_approval_db(initially_gated={("kaidera-os", "h-to-approve")})
    monkeypatch.setattr(settings_store, "_db", stub)

    ok = settings_store.set_approval_status("kaidera-os", "h-to-approve", "approved")
    assert ok is True
    assert settings_store.get_approval_status("kaidera-os", "h-to-approve") == "approved"


def test_set_approval_status_idempotent(monkeypatch):
    """set_approval_status is idempotent — calling twice with 'approved' is a no-op."""
    stub = _make_approval_db()
    monkeypatch.setattr(settings_store, "_db", stub)

    settings_store.set_approval_status("kaidera-os", "h-idem", "approved")
    settings_store.set_approval_status("kaidera-os", "h-idem", "approved")
    assert settings_store.get_approval_status("kaidera-os", "h-idem") == "approved"


def test_get_approval_status_unavailable_db_returns_none(monkeypatch):
    """When the DB is unreachable, get_approval_status fails-safe to None."""
    UNAVAILABLE = appdb_mod.UNAVAILABLE

    class _UnavailableDB:
        def get_approval_status(self, project, handoff_id):
            return UNAVAILABLE

    monkeypatch.setattr(settings_store, "_db", _UnavailableDB())
    assert settings_store.get_approval_status("kaidera-os", "h-any") is None


# ---------------------------------------------------------------------------
#  SAFETY-CRITICAL SEQUENCE TEST: propose_mode ON → gate → approve → spawn
#
#  This is the test that would have caught BLOCKER 1.
#  The prior gate (based on _dispatched) meant the approve route un-dispatched
#  the handoff, but the NEXT sweep re-gated it (is_propose_mode still True),
#  so it never spawned. The new status-based gate falls through to the spawn
#  path when status='approved'.
# ---------------------------------------------------------------------------

import pytest
import app.orchestrator as orch
from app.orchestrator import Orchestrator


class _FakeCortex:
    async def get_agents(self, project_key):
        return [{"name": "kai", "display_name": "Kai"}]

    async def get_handoffs(self, project_key):
        return []

    async def orchestration_plan(self, project_key):
        return {}


class _FakeAppDB:
    async def orchestration_plan(self, project_key):
        return {}


class _FakeProc:
    last_argv: list | None = None
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


def _make_orch() -> Orchestrator:
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


def _make_full_db(*, propose_on: bool = True) -> object:
    """A stub _db with both propose_mode=True and full status-based pending_approval."""
    UNAVAILABLE = appdb_mod.UNAVAILABLE
    awaiting: dict[tuple, str] = {}

    class _StubDB:
        # ---- propose_mode ----
        def get_project_propose_mode(self, project):
            return propose_on

        def set_project_propose_mode(self, project, enabled, updated_by=None):
            return True

        # ---- pending_approval (status-based) ----
        def get_approval_status(self, project, handoff_id):
            return awaiting.get((project, handoff_id), None)

        def set_approval_status(self, project, handoff_id, status):
            awaiting[(project, handoff_id)] = str(status)
            return True

        def list_awaiting_approval(self, project):
            return [hid for (proj, hid), s in awaiting.items()
                    if proj == project and s == "awaiting"]

        # ---- legacy compat ----
        def set_awaiting_approval(self, project, handoff_id):
            awaiting[(project, handoff_id)] = "awaiting"
            return True

        def clear_awaiting_approval(self, project, handoff_id):
            awaiting.pop((project, handoff_id), None)
            return True

        def is_awaiting_approval(self, project, handoff_id):
            return awaiting.get((project, handoff_id)) == "awaiting"

        # ---- project_autonomy ----
        def get_project_autonomy(self, project):
            return True

        def list_autonomous_projects(self):
            return ["kaidera-os"]

        # ---- agent_settings / app_settings ----
        def get_agent_designation(self, project, agent):
            return UNAVAILABLE

        def get_agent_override(self, project, agent):
            return {}

        def load_agent_overrides(self):
            return {}

        def load_app_settings(self):
            return {}

    stub = _StubDB()
    stub._awaiting = awaiting
    return stub


@pytest.mark.asyncio
async def test_approve_then_dispatch_spawns(monkeypatch):
    """SAFETY-CRITICAL SEQUENCE: propose_mode ON → gate → approve → next sweep SPAWNS.

    This is the exact sequence that was broken before the blocker fix:
      1. propose_mode=True, handoff is ready (status=None → no row yet).
      2. _maybe_dispatch sweep 1: gate fires, writes status='awaiting', gates (returns).
         No spawn. Handoff is NOT in _dispatched.
      3. Approve route: sets status='approved' (simulates operator clicking Approve).
      4. _maybe_dispatch sweep 2: gate checks status='approved' → falls through →
         SPAWNS the handoff (_FakeProc.last_argv is set, _inflight incremented).

    The old gate (based on _dispatched) would have re-gated in step 4 because
    is_propose_mode was still True. This test prevents regression."""
    monkeypatch.setattr(orch.subprocess, "Popen", _FakeProc)
    _FakeProc.last_argv = None
    _FakeProc.next_rc = 0

    stub_db = _make_full_db(propose_on=True)
    monkeypatch.setattr(settings_store, "_db", stub_db)

    o = _make_orch()
    handoff = {"id": "h-seq-approve", "status": "pending",
               "summary": "do work after approve", "to_agent": "kai"}

    # --- Sweep 1: gate fires, parks handoff as 'awaiting' ---
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")
    assert _FakeProc.last_argv is None, "sweep 1 must NOT spawn (propose_mode=ON, no approval yet)"
    assert stub_db._awaiting.get(("kaidera-os", "h-seq-approve")) == "awaiting", (
        "sweep 1 must write status='awaiting'"
    )
    # Gated handoffs must NOT be in _dispatched (critical: if added, sweep 2 would short-circuit).
    assert ("kaidera-os", "h-seq-approve") not in o._dispatched, (
        "gated handoff must NOT be in _dispatched"
    )

    # --- Approve action: operator clicks Approve → set status='approved' ---
    settings_store.set_approval_status("kaidera-os", "h-seq-approve", "approved")
    assert stub_db._awaiting.get(("kaidera-os", "h-seq-approve")) == "approved", (
        "approve must write status='approved'"
    )

    # --- Sweep 2: gate sees status='approved' → falls through → SPAWNS ---
    # _maybe_dispatch creates the spawn task and synchronously increments _inflight
    # + adds to _dispatched before the task actually runs — check those, not
    # _FakeProc.last_argv (which requires the task to execute).
    await o._maybe_dispatch("kaidera-os", handoff, source="poll")
    assert ("kaidera-os", "h-seq-approve") in o._dispatched, (
        "sweep 2 MUST add handoff to _dispatched (spawn path reached) — "
        "this was the blocker: the old gate re-gated because is_propose_mode was still True"
    )
    assert o._inflight.get("kaidera-os", 0) >= 1, (
        "inflight counter must be incremented when spawn path is reached"
    )


# ---------------------------------------------------------------------------
#  Approve-route integration: the NEW approve logic sets status='approved'.
#  Gated handoffs are NOT in _dispatched, so there is no _dispatched.discard.
# ---------------------------------------------------------------------------

def test_approve_sets_status_approved(monkeypatch):
    """The approve action sets status='approved' (not deletes the record).
    This is the new approve contract: the gate checks status, not row presence."""
    stub = _make_approval_db(initially_gated={("kaidera-os", "h-approve-new")})
    monkeypatch.setattr(settings_store, "_db", stub)

    # Status starts as 'awaiting'.
    assert settings_store.get_approval_status("kaidera-os", "h-approve-new") == "awaiting"

    # Approve: set status='approved' (what the route now calls).
    ok = settings_store.set_approval_status("kaidera-os", "h-approve-new", "approved")
    assert ok is True
    assert settings_store.get_approval_status("kaidera-os", "h-approve-new") == "approved"

    # Should no longer appear in the awaiting queue.
    queue = settings_store.list_awaiting_approval("kaidera-os")
    assert "h-approve-new" not in queue


def test_approve_already_approved_is_noop(monkeypatch):
    """Re-approving a handoff that is already 'approved' is a clean no-op."""
    stub = _make_approval_db(initially_gated={("kaidera-os", "h-re-approve")})
    monkeypatch.setattr(settings_store, "_db", stub)

    settings_store.set_approval_status("kaidera-os", "h-re-approve", "approved")
    # Second approve — idempotent.
    ok = settings_store.set_approval_status("kaidera-os", "h-re-approve", "approved")
    assert ok is True
    assert settings_store.get_approval_status("kaidera-os", "h-re-approve") == "approved"
