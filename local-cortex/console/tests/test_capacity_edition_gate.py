"""Layer-2 capacity gates (app/registration_api.py): the PUBLIC free tier caps
projects (1) and AI workers per team (4); a license raises the caps; DEV is unlimited.
Only NEW entities consume quota — upserts/updates are always allowed."""

from __future__ import annotations

import pytest

from app import license as lic
from app import registration_api as reg


class FakeCortex:
    def __init__(self, *, roster=None, projects=None):
        self._roster = roster or []
        self._projects = projects or []
        self.calls: list[str] = []

    async def get_roster(self, project_key):
        return self._roster

    async def get_projects(self):
        return self._projects

    async def create_agent(self, project_key, *, name, role, capabilities=None,
                           writer_scope=None, role_description=None):
        self.calls.append("create_agent")
        return {"registered": True, "agent": name, "role": role}

    async def create_project(self, *, project_key, display_name=None, repo_root=None,
                             repo_type=None, default_agent=None, agents=None):
        self.calls.append("create_project")
        return {"registered": True, "project_key": project_key}


def _roster(n):
    return [{"name": f"w{i}", "role": "qa"} for i in range(n)]


# --- worker cap ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_public_blocks_fifth_worker(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    cortex = FakeCortex(roster=_roster(4))
    res = await reg.register_agent_route("proj", {"name": "w5", "role": "qa"}, cortex=cortex)
    assert res["ok"] is False and "Worker limit" in res["error"]
    assert "create_agent" not in cortex.calls  # never written


@pytest.mark.asyncio
async def test_public_allows_worker_under_cap(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    cortex = FakeCortex(roster=_roster(3))
    res = await reg.register_agent_route("proj", {"name": "w4", "role": "qa"}, cortex=cortex)
    assert res["ok"] is True and "create_agent" in cortex.calls


@pytest.mark.asyncio
async def test_public_allows_upsert_of_existing_worker_at_cap(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    cortex = FakeCortex(roster=_roster(4))  # at cap, but w0 already exists
    res = await reg.register_agent_route("proj", {"name": "w0", "role": "qa"}, cortex=cortex)
    assert res["ok"] is True and "create_agent" in cortex.calls


@pytest.mark.asyncio
async def test_license_raises_worker_cap(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY",
                       lic.generate_license("DXB", days=365, features=["workers:10"]))
    cortex = FakeCortex(roster=_roster(4))
    res = await reg.register_agent_route("proj", {"name": "w5", "role": "qa"}, cortex=cortex)
    assert res["ok"] is True and "create_agent" in cortex.calls


@pytest.mark.asyncio
async def test_dev_edition_is_unlimited(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    cortex = FakeCortex(roster=_roster(50))
    res = await reg.register_agent_route("proj", {"name": "w51", "role": "qa"}, cortex=cortex)
    assert res["ok"] is True and "create_agent" in cortex.calls


# --- project cap ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_public_blocks_second_project(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    cortex = FakeCortex(projects=[{"project_key": "first", "status": "active"}])
    res = await reg.register_project_route(
        {"project_key": "second", "repo_root": "/tmp/x"}, cortex=cortex)
    assert res["ok"] is False and "Project limit" in res["error"]
    assert "create_project" not in cortex.calls


@pytest.mark.asyncio
async def test_public_allows_update_of_existing_project(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    cortex = FakeCortex(projects=[{"project_key": "first", "status": "active"}])
    res = await reg.register_project_route(
        {"project_key": "first", "repo_root": "/tmp/x"}, cortex=cortex)
    assert res["ok"] is True and "create_project" in cortex.calls
