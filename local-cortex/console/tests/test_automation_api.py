from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.automation_api import (
    delete_scheduled_job,
    export_automation_feeders,
    import_automation_feeders,
    planning_beat_status,
    upsert_planning_beat,
)
from app import auth as auth_module


@pytest.fixture(autouse=True)
def _auth_off_by_default(monkeypatch):
    monkeypatch.setattr(auth_module, "auth_enabled", lambda: False)


class FakeManageAppDB:
    def __init__(self):
        self.jobs = []
        self.deleted_jobs = []

    async def list_scheduled_jobs(self, project):
        return [j for j in self.jobs if j["project"] == project]

    async def upsert_scheduled_job(self, **kwargs):
        row = {
            "project": kwargs["project"],
            "id": kwargs["job_id"],
            "name": kwargs["name"],
            "enabled": kwargs["enabled"],
            "schedule": kwargs["schedule"],
            "payload": kwargs["payload"],
            "next_run_at": kwargs["next_run_at"].isoformat() if kwargs["next_run_at"] else None,
        }
        self.jobs = [j for j in self.jobs if not (j["project"] == row["project"] and j["id"] == row["id"])]
        self.jobs.append(row)
        return row

    async def delete_scheduled_job(self, **kwargs):
        before = len(self.jobs)
        self.jobs = [
            j for j in self.jobs
            if not (j["project"] == kwargs["project"] and j["id"] == kwargs["job_id"])
        ]
        self.deleted_jobs.append(kwargs)
        return len(self.jobs) != before


class FakeCortex:
    def __init__(self):
        self.created = []

    async def create_handoff(self, project, from_agent, body):
        self.created.append((project, from_agent, body))
        return {"id": "handoff-123"}

    async def get_project(self, project):
        return {"project_key": project, "default_agent": "marlow"}

    async def get_agents(self, project):
        return [
            {"name": "marlow", "role": "lead"},
            {"name": "cole", "role": "pm"},
        ]


def _manage_request(appdb=None):
    appdb = appdb or FakeManageAppDB()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(appdb=appdb, cortex=FakeCortex())))
    return request, appdb


@pytest.mark.asyncio
async def test_import_export_automation_feeders_round_trips_definitions():
    request, appdb = _manage_request()

    imported = await import_automation_feeders(
        "Marketing",
        request,
        {
            "scheduled_jobs": [
                {
                    "id": "heartbeat",
                    "name": "Heartbeat",
                    "enabled": True,
                    "schedule": {"kind": "interval", "every_seconds": 3600},
                    "payload": {
                        "from_agent": "marlow",
                        "to_role": "lead",
                        "summary": "Review the plan.",
                    },
                }
            ],
        },
        _admin=None,
    )

    exported = await export_automation_feeders("marketing", request)

    assert imported["ok"] is True
    assert imported["imported"] == {"scheduled_jobs": 1}
    assert exported["connected"] is True
    assert set(exported) == {"project", "version", "scheduled_jobs", "connected"}
    assert exported["scheduled_jobs"][0]["id"] == "heartbeat"
    assert appdb.jobs[0]["project"] == "marketing"


@pytest.mark.asyncio
async def test_delete_automation_feeders_removes_rows():
    request, appdb = _manage_request()
    appdb.jobs.append({"project": "marketing", "id": "heartbeat"})

    job = await delete_scheduled_job("marketing", "heartbeat", request, _admin=None)

    assert job == {"ok": True, "deleted": True, "id": "heartbeat", "error": None}
    assert appdb.jobs == []


@pytest.mark.asyncio
async def test_planning_beat_preset_uses_pm_worker_and_saves_schedule():
    request, appdb = _manage_request()

    result = await upsert_planning_beat(
        "marketing",
        request,
        {"every_minutes": 120},
        _admin=None,
    )
    status = await planning_beat_status("marketing", request)

    assert result["ok"] is True
    assert result["job"]["id"] == "pm-planning-beat"
    assert result["job"]["schedule"] == {"kind": "interval", "every_seconds": 7200}
    assert result["job"]["payload"]["from_agent"] == "cole"
    assert result["job"]["payload"]["to_agent"] == "cole"
    assert result["job"]["payload"]["to_role"] == "pm"
    assert result["job"]["payload"]["acceptance"]["capability"] == "pm-planning-beat"
    assert result["job"]["payload"]["acceptance"]["mode"] == "epic-decompose"
    assert status["configured"] is True
    # Status surfaces the hybrid decomposition mode + skill for the health card.
    assert status["recommended"]["mode"] == "epic-decompose"
    assert status["recommended"]["skill"] == "project-plan-create"
    assert status["recommended"]["to_agent"] == "cole"
    assert appdb.jobs[0]["project"] == "marketing"


@pytest.mark.asyncio
async def test_import_automation_feeders_reports_row_errors():
    request, _appdb = _manage_request()

    result = await import_automation_feeders(
        "marketing",
        request,
        {"scheduled_jobs": [{"id": "broken", "schedule": {"kind": "interval"}}]},
        _admin=None,
    )

    assert result["ok"] is False
    assert result["imported"] == {"scheduled_jobs": 0}
    assert result["errors"][0]["kind"] == "scheduled_job"
