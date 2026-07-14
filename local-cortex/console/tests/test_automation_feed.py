from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import automation_feed


def test_interval_schedule_has_first_and_next_run():
    now = datetime(2026, 6, 27, 10, 0, tzinfo=timezone.utc)
    schedule = {"kind": "interval", "every_seconds": 600}

    first = automation_feed.initial_next_run(schedule, now)
    nxt = automation_feed.next_run_after(schedule, now)

    assert first and first.isoformat() == "2026-06-27T10:10:00+00:00"
    assert nxt and nxt.isoformat() == "2026-06-27T10:10:00+00:00"


def test_once_schedule_disables_after_run():
    now = datetime(2026, 6, 27, 10, 0, tzinfo=timezone.utc)

    assert automation_feed.next_run_after({"kind": "once"}, now) is None


def test_handoff_payload_requires_writer_target_and_summary():
    agent, body = automation_feed.handoff_payload_from_job({
        "payload": {
            "from_agent": "marlow",
            "to_role": "pm",
            "summary": "Review the publishing plan",
            "unknown": "ignored",
        }
    })

    assert agent == "marlow"
    assert body == {
        "to_role": "pm",
        "summary": "Review the publishing plan",
        "priority": "medium",
    }


def test_pm_planning_schedule_payload_is_standard_handoff_job():
    payload = automation_feed.pm_planning_schedule_payload(
        project="Marketing",
        from_agent="Cole",
        to_role="pm",
        to_agent="Cole",
        every_minutes=90,
    )

    assert payload["id"] == "pm-planning-beat"
    assert payload["schedule"] == {"kind": "interval", "every_seconds": 5400}
    assert payload["payload"]["from_agent"] == "cole"
    assert payload["payload"]["to_agent"] == "cole"
    assert payload["payload"]["to_role"] == "pm"
    assert payload["payload"]["acceptance"]["capability"] == "pm-planning-beat"


def test_pm_planning_payload_is_epic_decomposition_mission():
    payload = automation_feed.pm_planning_schedule_payload(
        project="Marketing",
        from_agent="Cole",
        to_role="pm",
        to_agent="Cole",
        every_minutes=240,
        handoff_budget=1,
    )
    body = payload["payload"]
    acc = body["acceptance"]

    # AV-5: the beat decomposes the active epic and names the decomposition skill
    # (so the spawned worker's skill selector reliably picks it) — it is no longer
    # a generic "review the plan" prompt.
    assert acc["mode"] == "epic-decompose"
    assert acc["skill"] == "project-plan-create"
    assert acc["capability"] == "pm-planning-beat"
    assert acc["handoff_budget"] == 1
    assert "decompose" in body["summary"].lower()
    assert "project-plan-create" in body["context"]
    # Loop guard the spawned worker must honor lives in the mission text.
    assert "pm-planning-beat" in body["context"]
    # The planner identity is explicit in the generated mission so `<you>` cannot
    # drift to the wrong worker during a spawned run.
    assert "cortex-boot cole" in body["context"]
    assert "cortex-handoff --mine cole" in body["context"]
    # The per-cycle dedup token is stamped at emit time, never baked into the
    # schedule definition.
    assert "planning_cycle" not in acc


def test_planning_cycle_token_rotates_per_window():
    base = datetime(2026, 6, 29, 0, 0, tzinfo=timezone.utc)

    t0 = automation_feed.planning_cycle_token(base, stale_minutes=60)
    t_same = automation_feed.planning_cycle_token(base + timedelta(minutes=59), stale_minutes=60)
    t_next = automation_feed.planning_cycle_token(base + timedelta(minutes=61), stale_minutes=60)

    assert t0 == t_same
    assert t0 != t_next


def test_resolve_planning_cycle_token_stable_while_fresh_then_rotates():
    now = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    rotating = automation_feed.planning_cycle_token(now, stale_minutes=360)
    fresh = {
        "acceptance": {"capability": "pm-planning-beat", "planning_cycle": "cycle-A"},
        "created_at": (now - timedelta(minutes=10)).isoformat(),
    }
    stale = {
        "acceptance": {"capability": "pm-planning-beat", "planning_cycle": "cycle-A"},
        "created_at": (now - timedelta(minutes=999)).isoformat(),
    }
    other = {
        "acceptance": {"capability": "something-else", "planning_cycle": "x"},
        "created_at": now.isoformat(),
    }

    # Fresh open planning handoff → keep its token → next emission byte-identical
    # → Cortex dedup collapses it (within-cycle loop guard, no pile-up).
    assert automation_feed.resolve_planning_cycle_token([fresh], now=now, stale_minutes=360) == "cycle-A"
    # Stale open planning handoff → rotate → distinct fingerprint → re-plan
    # (recovery; never silently dropped forever).
    assert automation_feed.resolve_planning_cycle_token([stale], now=now, stale_minutes=360) == rotating
    # No open planning handoff (healthy loop: prior completed) → rotating token.
    assert automation_feed.resolve_planning_cycle_token([], now=now, stale_minutes=360) == rotating
    # Non-planning open handoffs never anchor the token.
    assert automation_feed.resolve_planning_cycle_token([other], now=now, stale_minutes=360) == rotating


class FakeAppDB:
    def __init__(self, jobs):
        self.jobs = jobs
        self.marked = []

    async def due_scheduled_jobs(self, project, now=None, limit=20):
        return list(self.jobs)

    async def mark_scheduled_job_run(self, **kwargs):
        self.marked.append(kwargs)
        return True


class FakeCortex:
    def __init__(self, response):
        self.response = response
        self.created = []

    async def create_handoff(self, project, from_agent, body):
        self.created.append((project, from_agent, body))
        return self.response


class PlanningScheduleAppDB:
    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])
        self.upserts = []

    async def list_scheduled_jobs(self, project):
        return list(self.jobs)

    async def upsert_scheduled_job(self, **kwargs):
        self.upserts.append(kwargs)
        return {"id": kwargs["job_id"], **kwargs}


class PlanningRosterCortex:
    async def get_project(self, project):
        return {"project_key": project, "default_agent": "lead"}

    async def get_agents(self, project):
        return [
            {"name": "lead", "role": "cmo"},
            {"name": "planner", "role": "pm"},
        ]


@pytest.mark.asyncio
async def test_autonomy_ensure_creates_immediately_due_pm_heartbeat():
    appdb = PlanningScheduleAppDB()
    now = datetime.now(timezone.utc)

    result = await automation_feed.ensure_pm_planning_schedule(
        appdb=appdb,
        cortex=PlanningRosterCortex(),
        project="example",
        now=now,
    )

    assert result["ok"] is True
    assert result["created"] is True
    saved = appdb.upserts[0]
    assert saved["job_id"] == "pm-planning-beat"
    assert saved["enabled"] is True
    assert saved["next_run_at"] == now
    assert saved["payload"]["to_agent"] == "planner"
    assert saved["payload"]["acceptance"]["capability"] == "pm-planning-beat"


@pytest.mark.asyncio
async def test_autonomy_ensure_preserves_existing_enabled_pm_heartbeat():
    existing = {
        "id": "pm-planning-beat",
        "enabled": True,
        "next_run_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"summary": "custom"},
    }
    appdb = PlanningScheduleAppDB([existing])

    result = await automation_feed.ensure_pm_planning_schedule(
        appdb=appdb,
        cortex=PlanningRosterCortex(),
        project="example",
    )

    assert result["ok"] is True
    assert result["created"] is False
    assert result["job"] == existing
    assert appdb.upserts == []


@pytest.mark.asyncio
async def test_due_scheduled_job_emits_handoff_and_advances_interval():
    job = {
        "id": "morning",
        "name": "Morning planning",
        "schedule": {"kind": "interval", "every_seconds": 3600},
        "payload": {
            "from_agent": "marlow",
            "to_role": "pm",
            "summary": "Run the morning planning beat",
        },
    }
    appdb = FakeAppDB([job])
    cortex = FakeCortex({"id": "abc123", "status": "pending"})

    count = await automation_feed.run_due_scheduled_jobs(
        appdb=appdb, cortex=cortex, project="marketing"
    )

    assert count == 1
    assert cortex.created[0][0:2] == ("marketing", "marlow")
    assert cortex.created[0][2]["summary"] == "Run the morning planning beat"
    assert appdb.marked[0]["status"] == "created"
    assert appdb.marked[0]["next_run_at"] is not None


@pytest.mark.asyncio
async def test_due_scheduled_job_records_payload_error_without_cortex_call():
    appdb = FakeAppDB([{"id": "bad", "name": "Bad", "schedule": {}, "payload": {}}])
    cortex = FakeCortex({"id": "unused"})

    count = await automation_feed.run_due_scheduled_jobs(
        appdb=appdb, cortex=cortex, project="marketing"
    )

    assert count == 0
    assert cortex.created == []
    assert appdb.marked[0]["status"] == "error"


class PlanningFakeCortex:
    """Cortex fake that exposes the open-handoff read the planning emit path uses.

    It deliberately also exposes claim/complete so a test can prove the feeder
    NEVER dispatches itself (it only creates handoffs; propose_mode/auto_dispatch
    gating stays downstream).
    """

    def __init__(self, open_rows=None, fail_read=False):
        self.open_rows = open_rows or []
        self.fail_read = fail_read
        self.created = []
        self.dispatched = []

    async def get_handoffs(self, project, status=None):
        if self.fail_read:
            raise RuntimeError("cortex down")
        if status == "claimed":
            return []
        return list(self.open_rows)

    async def create_handoff(self, project, from_agent, body):
        self.created.append((project, from_agent, body))
        return {"id": "plan-1", "status": "pending"}

    async def claim_handoff(self, *args, **kwargs):  # pragma: no cover - guard
        self.dispatched.append("claim")
        return True

    async def complete_handoff(self, *args, **kwargs):  # pragma: no cover - guard
        self.dispatched.append("complete")
        return True


def _planning_job():
    payload = automation_feed.pm_planning_schedule_payload(
        project="marketing", from_agent="cole", to_role="pm", to_agent="cole", every_minutes=240,
    )
    return {
        "id": payload["id"],
        "name": payload["name"],
        "schedule": payload["schedule"],
        "payload": payload["payload"],
    }


@pytest.mark.asyncio
async def test_due_planning_job_reuses_token_while_fresh_open_exists():
    fresh = {
        "acceptance": {"capability": "pm-planning-beat", "planning_cycle": "cycle-A"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    appdb = FakeAppDB([_planning_job()])
    cortex = PlanningFakeCortex(open_rows=[fresh])

    count = await automation_feed.run_due_scheduled_jobs(
        appdb=appdb, cortex=cortex, project="marketing"
    )

    assert count == 1
    body = cortex.created[0][2]
    # Reuses the open beat's token → byte-identical payload → Cortex dedup (no pile-up).
    assert body["acceptance"]["planning_cycle"] == "cycle-A"
    assert body["acceptance"]["mode"] == "epic-decompose"


@pytest.mark.asyncio
async def test_due_planning_job_rotates_token_when_no_open_planning():
    appdb = FakeAppDB([_planning_job()])
    cortex = PlanningFakeCortex(open_rows=[])

    await automation_feed.run_due_scheduled_jobs(
        appdb=appdb, cortex=cortex, project="marketing"
    )

    body = cortex.created[0][2]
    assert body["acceptance"]["planning_cycle"] == automation_feed.planning_cycle_token()


@pytest.mark.asyncio
async def test_due_planning_job_falls_back_to_static_token_on_read_failure():
    appdb = FakeAppDB([_planning_job()])
    cortex = PlanningFakeCortex(fail_read=True)

    await automation_feed.run_due_scheduled_jobs(
        appdb=appdb, cortex=cortex, project="marketing"
    )

    body = cortex.created[0][2]
    assert body["acceptance"]["planning_cycle"] == automation_feed.STATIC_PLANNING_CYCLE


@pytest.mark.asyncio
async def test_planning_feeder_only_emits_and_never_dispatches_itself():
    """propose_mode/auto_dispatch respect: the feeder emits an ordinary PENDING
    handoff and performs NO claim/dispatch, so the existing gates remain the sole
    authority over whether the planning beat actually spawns."""
    appdb = FakeAppDB([_planning_job()])
    cortex = PlanningFakeCortex(open_rows=[])

    await automation_feed.run_due_scheduled_jobs(
        appdb=appdb, cortex=cortex, project="marketing"
    )

    assert len(cortex.created) == 1
    assert cortex.dispatched == []
    assert appdb.marked[0]["status"] == "created"
