"""AV-3 — the requeue → re-dispatch → re-stuck → retry-cap loop, end to end.

This is the gap AV-3 closes. The watchdog RELEASES a stuck mid-run handoff back to
pending (incrementing ``retry_count`` server-side via POST /handoffs/{id}/release) so
the dispatcher re-picks it. But the orchestrator's in-memory ``_dispatched`` set was
APPEND-ONLY: a mid-run handoff added at dispatch time was never discarded, so when it
returned to pending the idempotency gate short-circuited (``if key in self._dispatched:
return``) and the requeued run STRANDED pending — never re-dispatched (blocked here),
never re-escalated (the watchdog only scans ``claimed``), so ``retry_count`` never
reached the cap. A regression vs the old behaviour where stuck runs always escalated.

The prior watchdog suites (``TestRequeueAndRetryCap``) inject FIXED ``retry_count``
values into ``FakeOps`` and never touch the orchestrator dispatch funnel, so they
masked this. These tests wire BOTH halves — the REAL ``Orchestrator._scan_pending`` /
``_maybe_dispatch`` and the REAL ``Watchdog.scan_once`` — to ONE mutable handoff and
assert the loop actually closes (the run is RE-dispatched after each requeue) and
STOPS at the cap (escalates once, no requeue-loop forever).

Stubs follow the test_dispatch_gate.py conventions (settings_store._db stub,
``_dispatch_run`` overridden so no real subprocess spawns).
"""
from __future__ import annotations

import pytest

import app.orchestrator as orch
import app.settings as settings_store
from app import appdb as appdb_mod
from app.orchestrator import Orchestrator
from app.watchdog import Watchdog


# ---------------------------------------------------------------------------
#  One handoff's shared lifecycle — mutated by BOTH the orchestrator (dispatch
#  marks it claimed) and the watchdog (release → pending + retry_count++).
# ---------------------------------------------------------------------------

class _HandoffWorld:
    HID = "abcdef1234567890"

    def __init__(self) -> None:
        self.status = "pending"            # pending | claimed
        self.claimed_by: str | None = None
        self.retry_count = 0
        self.dispatched: list[int] = []    # retry_count seen at each (re)dispatch
        self.released: list[str] = []      # reason of each watchdog release (requeue)
        self.escalated: list[str] = []     # reason of each watchdog escalation
        self.escalation_rows: list[dict] = []  # live [WATCHDOG-SIGNAL] queue (dedup)

    def _row(self) -> dict:
        return {
            "id": self.HID,
            "status": self.status,
            "summary": "stuck work",
            "to_agent": "worker",
            "retry_count": self.retry_count,
            "claimed_by": self.claimed_by,
        }

    def pending_rows(self) -> list[dict]:
        return [self._row()] if self.status == "pending" else []

    def claimed_rows(self) -> list[dict]:
        return [self._row()] if self.status == "claimed" else []

    def mark_claimed(self) -> None:
        self.status = "claimed"
        self.claimed_by = "worker"

    def do_release(self) -> None:  # mirrors POST /handoffs/{id}/release
        self.status = "pending"
        self.claimed_by = None
        self.retry_count += 1


# ---------------------------------------------------------------------------
#  Orchestrator side fakes
# ---------------------------------------------------------------------------

class _OrchCortex:
    def __init__(self, world: _HandoffWorld) -> None:
        self._world = world

    async def get_handoffs(self, project_key, status=None):
        if status == "claimed":
            return self._world.claimed_rows()
        return self._world.pending_rows()

    async def get_agents(self, project_key):
        return [{"name": "worker", "display_name": "Worker"}]

    async def orchestration_plan(self, project_key):
        return {}


class _OrchAppDB:
    async def orchestration_plan(self, project_key):
        return {}


def _make_settings_db() -> object:
    """Minimal settings_store._db stub: project autonomous ON, propose mode OFF,
    no per-agent overrides (mirrors test_dispatch_gate._make_propose_db)."""
    UNAVAILABLE = appdb_mod.UNAVAILABLE

    class _StubDB:
        def get_project_propose_mode(self, project):
            return False

        def get_project_autonomy(self, project):
            return True

        def list_autonomous_projects(self):
            return ["kaidera-os"]

        def get_agent_designation(self, project, agent):
            return UNAVAILABLE

        def get_agent_override(self, project, agent):
            return {}

        def load_agent_overrides(self):
            return {}

        def load_app_settings(self):
            return {}

        def get_approval_status(self, project, handoff_id):
            return None

        def set_approval_status(self, project, handoff_id, status):
            return True

    return _StubDB()


def _make_orch(world: _HandoffWorld) -> Orchestrator:
    o = Orchestrator(
        cortex=_OrchCortex(world),
        appdb=_OrchAppDB(),
        harness_runner=object(),
        chat_routing_for=lambda agent, project: ("pi", "m", "high"),
        record_usage=None,
        find_agent=lambda agents, name: None,
        resolve_target=lambda handoff, agents: {"name": "worker", "display_name": "Worker"},
        classify_interactive=lambda agent, desig: False,
        project_identity=lambda cortex, project: None,
        agent_view=lambda a: a,
        runstate=None,  # reclaim pass inert (no store → it skips)
    )

    async def _fake_dispatch_run(project_key, handoff, target):
        # Simulate the spawned worker: claim the handoff then GO STUCK (never
        # completes). Release the concurrency slot exactly like the real finally.
        world.dispatched.append(world.retry_count)
        world.mark_claimed()
        o._inflight[project_key] = max(0, o._inflight.get(project_key, 1) - 1)

    o._dispatch_run = _fake_dispatch_run  # type: ignore[assignment]
    return o


# ---------------------------------------------------------------------------
#  Watchdog side ops — reads/writes the SAME world (real release/escalate path).
# ---------------------------------------------------------------------------

class _WatchdogOps:
    def __init__(self, world: _HandoffWorld) -> None:
        self._world = world

    async def get_handoffs(self, project):
        return self._world.claimed_rows()

    async def get_open_escalations(self, project):
        return list(self._world.escalation_rows)

    async def has_success_marker(self, project, hid):
        return False  # the run is stuck — it never logs success

    async def claimed_age_seconds(self, project, hid):
        return 10_000.0  # well past the stale threshold → stuck

    async def complete(self, project, hid):
        pass

    async def release(self, project, hid, reason=""):
        self._world.released.append(reason)
        self._world.do_release()

    async def escalate(self, project, handoff, reason):
        hid = handoff.get("id", "?")
        self._world.escalated.append(reason)
        self._world.escalation_rows.append(
            {"id": f"esc-{len(self._world.escalated)}",
             "summary": f"[WATCHDOG-SIGNAL] stuck run {hid[:8]}: {reason}"}
        )


async def _drain(o: Orchestrator) -> None:
    for task in list(o._runs):
        await task


# ---------------------------------------------------------------------------
#  THE END-TO-END LOOP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_requeue_redispatch_loop_closes_and_stops_at_cap(monkeypatch):
    """Drive the REAL dispatch funnel + REAL watchdog over ONE stuck handoff:

      dispatch → (stuck) → watchdog release (requeue, retry++) → RE-dispatch → ...
      → at the cap the watchdog ESCALATES instead of requeuing, and the handoff is
      no longer re-dispatched.

    Without the AV-3 fix the handoff is dispatched exactly ONCE and then strands
    pending forever (``_dispatched`` blocks re-dispatch), never reaching the cap.
    """
    monkeypatch.setattr(settings_store, "_db", _make_settings_db())

    world = _HandoffWorld()
    o = _make_orch(world)
    wd = Watchdog(_WatchdogOps(world))  # default cap = WATCHDOG_MAX_RETRIES (3)
    cap = wd._max_retries
    project = "kaidera-os"

    for _ in range(cap + 3):
        await o._scan_pending(project)
        await _drain(o)
        await wd.scan_once(project)

    # The loop CLOSED: the run was re-dispatched after every requeue (cap+1 total),
    # not stranded at 1. This is the core AV-3 assertion.
    assert len(world.dispatched) == cap + 1, (
        f"expected the requeued run to be RE-dispatched {cap + 1} times "
        f"(initial + {cap} requeues); got {world.dispatched}. A value of 1 means the "
        f"append-only _dispatched set stranded the requeue (the AV-3 regression)."
    )
    # The dispatch saw a strictly-rising retry_count each cycle (proof each dispatch
    # is a fresh pass through the funnel, reconciled against the live count).
    assert world.dispatched == list(range(cap + 1)), world.dispatched

    # It requeued exactly ``cap`` times then STOPPED — escalated once, never again.
    assert len(world.released) == cap, f"expected {cap} requeues, got {world.released}"
    assert len(world.escalated) == 1, (
        f"expected exactly ONE escalation at the cap, got {world.escalated}"
    )
    assert world.retry_count == cap
    # At the cap the run is LEFT claimed (escalated to the lead), not requeue-looped.
    assert world.status == "claimed"


@pytest.mark.asyncio
async def test_idempotency_gate_redispatches_only_on_retry_increase(monkeypatch):
    """Focused unit of the fix at the gate: a handoff already in ``_dispatched`` is
    re-dispatched IFF its live ``retry_count`` rose above the recorded baseline.
    A repeat with the SAME count stays idempotent (no double-dispatch)."""
    monkeypatch.setattr(settings_store, "_db", _make_settings_db())

    world = _HandoffWorld()
    o = _make_orch(world)
    project = "kaidera-os"
    key = (project, world.HID)

    # First dispatch (retry_count 0) → recorded baseline 0, run goes claimed.
    await o._scan_pending(project)
    await _drain(o)
    assert key in o._dispatched
    assert o._dispatched_retry[key] == 0
    assert len(world.dispatched) == 1

    # The worker is "stuck" (claimed). A poll sweep now sees NO pending row, so the
    # funnel does nothing — and crucially does NOT re-dispatch the claimed run.
    await o._scan_pending(project)
    await _drain(o)
    assert len(world.dispatched) == 1, "a still-claimed run must not be re-dispatched"

    # Simulate a watchdog requeue WITHOUT bumping retry_count (shouldn't happen, but
    # guards over-eager reconciliation): release back to pending, SAME retry_count.
    world.status = "pending"
    world.claimed_by = None
    await o._scan_pending(project)
    await _drain(o)
    assert len(world.dispatched) == 1, (
        "no retry_count increase → the idempotency gate must hold (no re-dispatch)"
    )

    # Now a REAL requeue: retry_count rises above the baseline → must re-dispatch.
    world.retry_count = 1
    await o._scan_pending(project)
    await _drain(o)
    assert len(world.dispatched) == 2, (
        "retry_count rose above the recorded baseline → the run must be re-dispatched"
    )
    assert o._dispatched_retry[key] == 1, "the new baseline must be the requeued count"
