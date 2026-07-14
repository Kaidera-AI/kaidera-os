"""Console Plan route tests (`app/plan/api.py`) — the read surface over visual-plan MDX.

  * `GET /plan/{project}/list`        — enumerate `docs/plans/**/*.mdx` under repo_root.
  * `GET /plan/{project}/file?path=…` — return one plan's raw MDX, HARD-guarded to stay
                                        inside `docs/plans/` (the security boundary).

Driven in-process via httpx ASGITransport over a minimal app mounting the router, with a
FAKE cortex (`app.state.cortex`) whose repo_root points at a real tmp tree — no live
Cortex, no DB. The traversal guard is the load-bearing assertion here.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.plan.api import router as plan_router


class FakeCortex:
    def __init__(self, repo_root, default_agent=""):
        self.agent = "ren"  # fitness:allow-literal test fixture
        self._repo_root = repo_root
        self.default_agent = default_agent
        self.created_handoffs = []

    async def get_project(self, project_key):
        if self._repo_root is None:
            return None
        return {
            "project_key": project_key,
            "repo_root": self._repo_root,
            "default_agent": self.default_agent,
        }

    async def create_handoff(self, project_key, from_agent, body):
        self.created_handoffs.append((project_key, from_agent, body))
        return {"id": "handoff-plan-1"}


def _make_app(repo_root, default_agent=""):
    app = FastAPI()
    app.include_router(plan_router)
    app.state.cortex = FakeCortex(repo_root, default_agent=default_agent)
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.test")


def _seed(root):
    """Write a plan tree under <root>/docs/plans and a secret OUTSIDE it."""
    plans = root / "docs" / "plans" / "demo"
    plans.mkdir(parents=True)
    (plans / "plan.mdx").write_text("# Demo plan\n\nbody", encoding="utf-8")
    (plans / "canvas.mdx").write_text("<DesignBoard/>", encoding="utf-8")
    (root / "secret.txt").write_text("TOPSECRET", encoding="utf-8")


@pytest.mark.asyncio
async def test_list_enumerates_mdx(tmp_path):
    _seed(tmp_path)
    app = _make_app(str(tmp_path))
    async with _client(app) as c:
        resp = await c.get("/plan/kaidera-os/list")
    assert resp.status_code == 200
    plans = resp.json()["plans"]
    paths = {p["path"] for p in plans}
    assert paths == {"demo/plan.mdx", "demo/canvas.mdx"}
    kinds = {p["path"]: p["kind"] for p in plans}
    assert kinds["demo/plan.mdx"] == "plan"
    assert kinds["demo/canvas.mdx"] == "canvas"


@pytest.mark.asyncio
async def test_list_missing_dir_is_empty_not_error(tmp_path):
    app = _make_app(str(tmp_path))  # no docs/plans
    async with _client(app) as c:
        resp = await c.get("/plan/kaidera-os/list")
    assert resp.status_code == 200
    assert resp.json()["plans"] == []


@pytest.mark.asyncio
async def test_plan_status_reports_bootstrap_need_and_lead(tmp_path):
    app = _make_app(str(tmp_path), default_agent="marlow")
    async with _client(app) as c:
        resp = await c.get("/plan/marketing/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "marketing"
    assert data["has_repo_root"] is True
    assert data["has_plan"] is False
    assert data["plan_count"] == 0
    assert data["lead"] == "marlow"
    assert data["bootstrap_available"] is True
    assert data["recommended_path"] == "docs/plans/marketing-project-plan/plan.mdx"
    assert data["reason"] == "no project plan found"


@pytest.mark.asyncio
async def test_plan_status_reports_ready_when_plan_exists(tmp_path):
    _seed(tmp_path)
    app = _make_app(str(tmp_path), default_agent="marlow")
    async with _client(app) as c:
        resp = await c.get("/plan/marketing/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert data["has_plan"] is True
    assert data["plan_count"] == 2
    assert data["latest_plan"]["kind"] == "plan"


@pytest.mark.asyncio
async def test_file_reads_plan(tmp_path):
    _seed(tmp_path)
    app = _make_app(str(tmp_path))
    async with _client(app) as c:
        resp = await c.get("/plan/kaidera-os/file", params={"path": "demo/plan.mdx"})
    assert resp.status_code == 200
    assert resp.json()["text"].startswith("# Demo plan")


@pytest.mark.asyncio
async def test_file_traversal_is_rejected(tmp_path):
    """The guard: a ../ escape must NOT read a file outside docs/plans, even though it
    ends in a benign extension trick. Both the non-.mdx reject and the containment
    reject keep the secret unreachable."""
    _seed(tmp_path)
    app = _make_app(str(tmp_path))
    async with _client(app) as c:
        # non-.mdx → 400 before any path work
        r1 = await c.get("/plan/kaidera-os/file", params={"path": "../../secret.txt"})
        # an .mdx-suffixed traversal that resolves outside the root → containment 400/404
        r2 = await c.get("/plan/kaidera-os/file", params={"path": "../../secret.mdx"})
    assert r1.status_code == 400
    assert "secret" not in r1.text or "escape" in r1.text.lower() or "required" in r1.text.lower()
    assert r2.status_code in (400, 404)
    assert "TOPSECRET" not in r2.text


@pytest.mark.asyncio
async def test_no_repo_root_is_400(tmp_path):
    app = _make_app(None)
    async with _client(app) as c:
        resp = await c.get("/plan/kaidera-os/list")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_bootstrap_creates_lead_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIDERA_AUTH_ENABLED", "0")
    app = _make_app(str(tmp_path), default_agent="marlow")

    async with _client(app) as c:
        resp = await c.post(
            "/plan/marketing/bootstrap",
            json={
                "title": "Marketing OS operating plan",
                "objective": "Turn the active marketing project into an autonomous plan.",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["lead"] == "marlow"
    assert data["path"] == "docs/plans/marketing-os-operating-plan/plan.mdx"
    assert data["handoff"]["id"] == "handoff-plan-1"

    created = app.state.cortex.created_handoffs
    assert len(created) == 1
    project, from_agent, body = created[0]
    assert (project, from_agent) == ("marketing", "marlow")
    assert body["to_agent"] == "marlow"
    assert body["to_role"] == "lead"
    assert body["acceptance"]["capability"] == "visual-plan"
    assert body["acceptance"]["target_path"] == data["path"]
    assert "autonomous plan" in body["acceptance"]["objective"]


@pytest.mark.asyncio
async def test_bootstrap_requires_lead(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIDERA_AUTH_ENABLED", "0")
    app = _make_app(str(tmp_path))

    async with _client(app) as c:
        resp = await c.post("/plan/marketing/bootstrap", json={"title": "Plan"})

    assert resp.status_code == 400
    assert resp.json() == {
        "ok": False,
        "error": "project has no lead/default_agent configured",
    }
    assert app.state.cortex.created_handoffs == []
