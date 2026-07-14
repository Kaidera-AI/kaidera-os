"""Console Explain route tests (`app/explain/api.py`).

The console-side surface of the Explain capability:
  * `POST /explain/{project}`              — validate, mint run_id, open a run_state row
                                             (lease_owner='explain'), forward the spawn to
                                             the HOST harness-service `/explain` (httpx),
                                             return `{run_id, accepted}`.
  * `GET /explain/{project}/result/{run_id}` — the persisted artifact for a run (via
                                             CortexClient.get_artifact_by_source_file).
  * `GET /explain/{project}/list`          — the gallery (recent html artifacts).

Driven via an in-process httpx ASGITransport over a minimal app that mounts the router,
with a FAKE cortex (`app.state.cortex`), a fake run-state store (`app.state.runstate`), and
an INJECTED httpx MockTransport for the host-service forward (`app.state.explain_http`) —
NO live harness-service, no live Cortex, nothing spawned.
"""

from __future__ import annotations

import io
import json
import tarfile

import httpx
import pytest
from fastapi import FastAPI

from app.explain.api import router as explain_router
from app.harness import harness_default_model


class FakeCortexForExplain:
    """A minimal CortexClient stand-in for the route: get_project (repo_root) +
    get_artifact_by_source_file (the result lookup). NOTE: the gallery NO LONGER reads
    Cortex search — it enumerates run_state (lease_owner='explain') — so `search` is
    retained only for the (unchanged) result route's neighbours, not the list."""

    def __init__(self, *, repo_root="/abs/project", artifact=None,
                 default_agent=None, agents=None, agents_raises=False):
        self.agent = "ren"  # fitness:allow-literal test fixture (console reader)
        self._repo_root = repo_root
        self._artifact = artifact
        self._default_agent = default_agent
        self._agents = agents
        self._agents_raises = agents_raises

    async def get_project(self, project_key):
        if self._repo_root is None:
            return None
        proj = {"project_key": project_key, "repo_root": self._repo_root}
        if self._default_agent is not None:
            proj["default_agent"] = self._default_agent
        return proj

    async def get_agents(self, project_key):
        if self._agents_raises:
            raise RuntimeError("roster down")
        return self._agents or []

    async def get_artifact_by_source_file(self, project_key, source_file):
        return self._artifact


class _Rec:
    """A tiny RunRecord-shaped header (only the fields explain_list reads)."""

    def __init__(self, *, run_id, project="kaidera-os", status="ok", lease_owner="explain",
                 metadata=None, started_at=None, spans=None, agent="kai", harness=None,
                 model=None, updated_at=None, ended_at=None):
        self.run_id = run_id
        self.project = project
        self.status = status
        self.lease_owner = lease_owner
        self.metadata = metadata
        self.started_at = started_at
        self.spans = spans or []
        self.agent = agent
        self.harness = harness
        self.model = model
        self.updated_at = updated_at
        self.ended_at = ended_at


class FakeRunStateForExplain:
    """Records start_run + set_status, and serves scripted recent(lease_owner='explain')
    rows for the gallery (structural RunStatePort, no DB)."""

    def __init__(self, *, recent_rows=None, recent_raises=False, runs=None,
                 get_run_raises=False):
        self.started = []
        self.statuses = []
        self._recent_rows = recent_rows or []
        self._recent_raises = recent_raises
        self._runs = runs or {}
        self._get_run_raises = get_run_raises
        self.recent_calls = []

    async def start_run(self, *, run_id, project, agent, agent_display=None,
                        handoff_id=None, harness=None, model=None, pid=None,
                        lease_owner=None, session_id=None):
        self.started.append({"run_id": run_id, "lease_owner": lease_owner,
                             "project": project, "agent": agent})
        return type("Rec", (), {"run_id": run_id})()

    async def set_status(self, run_id, status, *, error=None, metadata=None):
        self.statuses.append({"run_id": run_id, "status": status, "error": error,
                              "metadata": metadata})

    async def recent(self, project=None, limit=20, *, session_id=None, lease_owner=None):
        # The gallery calls recent(project, lease_owner='explain'); serve the scripted
        # rows (already newest-first, as the real store returns). A non-explain lease is
        # not expected from the gallery, but filter defensively.
        self.recent_calls.append({"project": project, "limit": limit,
                                  "lease_owner": lease_owner})
        if self._recent_raises:
            raise RuntimeError("store down")
        rows = self._recent_rows
        if lease_owner is not None:
            rows = [r for r in rows if (r.lease_owner or "") == lease_owner]
        return rows[:limit]

    async def get_run(self, run_id):
        if self._get_run_raises:
            raise RuntimeError("store down")
        return self._runs.get(run_id)


def _make_app(*, cortex=None, runstate=None, harness_handler=None):
    """A minimal app mounting the explain router, with the fakes on app.state."""
    app = FastAPI()
    app.include_router(explain_router)
    app.state.cortex = cortex if cortex is not None else FakeCortexForExplain()
    app.state.runstate = runstate if runstate is not None else FakeRunStateForExplain()
    if harness_handler is not None:
        app.state.explain_http = httpx.AsyncClient(
            transport=httpx.MockTransport(harness_handler)
        )
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.test")


# ---------------------------------------------------------------------------
#  POST /explain/{project}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_explain_forwards_to_host_and_returns_202(monkeypatch):
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")  # exercise the bridge-forward path
    seen: dict = {}

    def harness_handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"run_id": seen.get("rid"), "accepted": True})

    rs = FakeRunStateForExplain()
    app = _make_app(runstate=rs, harness_handler=harness_handler)
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "file", "path": "mod.py"})

    assert resp.status_code == 202
    data = resp.json()
    assert data["accepted"] is True
    run_id = data["run_id"]
    assert run_id  # minted

    # Forwarded to the host /explain with the resolved repo_root (NOT client-supplied).
    assert seen["url"].endswith("/explain")
    body = seen["body"]
    assert body["kind"] == "file"
    assert body["path"] == "mod.py"
    assert body["repo"] == "/abs/project"  # the project's repo_root
    assert body["run_id"] == run_id

    # The run_state row opened with the explain lease BEFORE the forward.
    assert rs.started and rs.started[0]["lease_owner"] == "explain"
    assert rs.started[0]["run_id"] == run_id


@pytest.mark.asyncio
async def test_start_explain_accepts_project_target_without_path(monkeypatch):
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    seen: dict = {}

    def harness_handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": True})

    app = _make_app(harness_handler=harness_handler)
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "project"})

    assert resp.status_code == 202
    assert resp.json()["accepted"] is True
    assert seen["body"]["kind"] == "project"
    assert seen["body"]["repo"] == "/abs/project"
    assert "path" not in seen["body"]


# ---------------------------------------------------------------------------
#  PROJECT-BOUND writer resolution: the explain WRITER is the project's resolved
#  LEAD (default_agent → designation-driven lead), with the lead's currently-
#  selected harness/model (via _chat_routing_for); body harness/model override
#  wins; graceful fallback to the console reader when no lead resolves.
# ---------------------------------------------------------------------------


class _StubOpStore:
    """A minimal OperationalStorePort stand-in: no overrides, so the agents service
    falls back to the registry heuristic for grouping/lead resolution."""

    def load_agent_overrides(self):
        return {}

    def get_agent_override(self, project, agent):
        return {}


@pytest.mark.asyncio
async def test_start_explain_project_resolves_lead_agent_and_model(monkeypatch):
    """A {kind:'project'} run resolves the project's LEAD as the writer agent and
    derives its harness/model from _chat_routing_for — NOT the console reader."""
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    # _chat_routing_for is monkeypatched for a deterministic, registry-free assertion.
    monkeypatch.setattr(
        "app.main._chat_routing_for",
        lambda record, project: ("kaidera", "kimi-k2", "max"),
    )
    seen: dict = {}

    def harness_handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": True})

    # A roster whose lead resolves via the cpo/lead role hint (designation-driven).
    cortex = FakeCortexForExplain(
        agents=[
            {"name": "ren", "role": "lead"},  # fitness:allow-literal test fixture roster
            {"name": "bob", "role": "developer"},  # fitness:allow-literal test fixture roster
        ]
    )
    app = _make_app(cortex=cortex, harness_handler=harness_handler)
    app.state.opstore = _StubOpStore()
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "project"})

    assert resp.status_code == 202
    body = seen["body"]
    # The writer is the resolved LEAD (not the console reader "ren"... here the lead
    # IS "ren", but it was resolved via the roster, not the console fallback — and the
    # harness/model come from _chat_routing_for, proving the lead path ran).
    assert body["agent"] == "ren"
    assert body["harness"] == "kaidera"
    assert body["model"] == "kimi-k2"


@pytest.mark.asyncio
async def test_start_explain_project_resolves_default_agent_field(monkeypatch):
    """default_agent off the project row resolves the writer even with an EMPTY roster
    (the (a) source short-circuits the agents-service path)."""
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    monkeypatch.setattr(
        "app.main._chat_routing_for",
        lambda record, project: ("codex", "gpt-x", None),
    )
    seen: dict = {}

    def harness_handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": True})

    # No roster, but default_agent names the lead AND is present in the roster record
    # lookup (so _chat_routing_for gets a record).
    cortex = FakeCortexForExplain(
        default_agent="lia",  # fitness:allow-literal test fixture default_agent
        agents=[{"name": "lia", "role": "developer"}],  # fitness:allow-literal test fixture roster
    )
    app = _make_app(cortex=cortex, harness_handler=harness_handler)
    app.state.opstore = _StubOpStore()
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "project"})

    assert resp.status_code == 202
    body = seen["body"]
    assert body["agent"] == "lia"
    assert body["harness"] == "codex"
    assert body["model"] == "gpt-x"


@pytest.mark.asyncio
async def test_start_explain_project_degrades_to_console_agent_when_no_lead(monkeypatch):
    """No default_agent, a roster read that RAISES, no opstore → the writer degrades to
    today's console reader and the run still accepts (202). No 400/500 from the new path."""
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    monkeypatch.setenv("CORTEX_CONSOLE_AGENT", "")  # force the cortex.agent fallback
    seen: dict = {}

    def harness_handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": True})

    cortex = FakeCortexForExplain(agents_raises=True)  # roster down; no default_agent
    app = _make_app(cortex=cortex, harness_handler=harness_handler)
    # NO opstore on app.state, and no appdb → get_operational_store raises → the
    # agents-service path degrades to None lead.
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "project"})

    assert resp.status_code == 202  # graceful-degrade, never a 500/400
    body = seen["body"]
    # The console reader (cortex.agent) is the writer — today's behavior preserved.
    assert body["agent"] == cortex.agent  # "ren" (the console reader fixture)
    # No lead → no resolved routing; harness/model omitted (no body values either).
    assert "harness" not in body
    assert "model" not in body


@pytest.mark.asyncio
async def test_start_explain_body_harness_model_override_wins(monkeypatch):
    """With a resolvable lead, an explicit body harness/model OVERRIDES the lead's
    resolved routing (override-first)."""
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    monkeypatch.setattr(
        "app.main._chat_routing_for",
        lambda record, project: ("kaidera", "kimi-k2", "max"),  # the lead's routing
    )
    seen: dict = {}

    def harness_handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": True})

    cortex = FakeCortexForExplain(
        agents=[{"name": "ren", "role": "lead"}],  # fitness:allow-literal test fixture roster
    )
    app = _make_app(cortex=cortex, harness_handler=harness_handler)
    app.state.opstore = _StubOpStore()
    async with _client(app) as c:
        resp = await c.post(
            "/explain/kaidera-os",
            json={"kind": "file", "path": "app/x.py",
                  "harness": "codex", "model": "gpt-x"},
        )

    assert resp.status_code == 202
    body = seen["body"]
    # Explicit body override beats the lead's resolved routing.
    assert body["harness"] == "codex"
    assert body["model"] == "gpt-x"
    # The writer is still the resolved lead.
    assert body["agent"] == "ren"


@pytest.mark.asyncio
async def test_start_explain_body_harness_only_uses_that_harness_default(monkeypatch):
    """If Advanced overrides harness but not model, do not carry the lead's prior
    model into a different harness; use the override harness default instead."""
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    monkeypatch.setattr(
        "app.main._chat_routing_for",
        lambda record, project: ("kaidera", "kimi-k2", "max"),
    )
    seen: dict = {}

    def harness_handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": True})

    cortex = FakeCortexForExplain(
        agents=[{"name": "ren", "role": "lead"}],  # fitness:allow-literal test fixture roster
    )
    app = _make_app(cortex=cortex, harness_handler=harness_handler)
    app.state.opstore = _StubOpStore()
    async with _client(app) as c:
        resp = await c.post(
            "/explain/kaidera-os",
            json={"kind": "file", "path": "app/x.py", "harness": "codex"},
        )

    assert resp.status_code == 202
    body = seen["body"]
    assert body["harness"] == "codex"
    assert body["model"] == harness_default_model("codex")
    assert body["agent"] == "ren"


@pytest.mark.asyncio
async def test_start_explain_advanced_kind_no_body_routing_uses_lead_model(monkeypatch):
    """An advanced kind with NO body harness/model defaults to the lead's resolved
    routing (the lead model drives when the operator didn't override)."""
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    monkeypatch.setattr(
        "app.main._chat_routing_for",
        lambda record, project: ("kaidera", "kimi-k2", "max"),
    )
    seen: dict = {}

    def harness_handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": True})

    cortex = FakeCortexForExplain(
        agents=[{"name": "ren", "role": "lead"}],  # fitness:allow-literal test fixture roster
    )
    app = _make_app(cortex=cortex, harness_handler=harness_handler)
    app.state.opstore = _StubOpStore()
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "file", "path": "app/x.py"})

    assert resp.status_code == 202
    body = seen["body"]
    assert body["harness"] == "kaidera"
    assert body["model"] == "kimi-k2"
    assert body["agent"] == "ren"


@pytest.mark.asyncio
async def test_start_explain_rejected_host_marks_run_error_and_200(monkeypatch):
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    def harness_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"accepted": False, "error": "boom"})

    rs = FakeRunStateForExplain()
    app = _make_app(runstate=rs, harness_handler=harness_handler)
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "file", "path": "mod.py"})

    assert resp.status_code == 200  # a rejected spawn is a clean 200 accepted=false
    assert resp.json()["accepted"] is False
    # The pre-opened run was marked errored (never silently dies).
    assert any(s["status"] == "error" for s in rs.statuses)


@pytest.mark.asyncio
async def test_start_explain_host_down_degrades_to_accepted_false(monkeypatch):
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")

    def harness_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("host down")

    app = _make_app(harness_handler=harness_handler)
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "file", "path": "mod.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["error"]  # the connect error is surfaced, never raised


@pytest.mark.asyncio
async def test_start_explain_native_spawns_run_explain_locally(monkeypatch):
    """Native console (HARNESS_SPAWN_MODE unset): the route spawns run-explain directly
    as a detached process — NO bridge forward — so a selfcontained VM (no
    host.docker.internal) doesn't fail with a DNS error. Asserts the argv carries the
    resolved repo_root + run_id and the run is accepted."""
    monkeypatch.delenv("HARNESS_SPAWN_MODE", raising=False)  # native path

    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()  # a detached proc handle; the route doesn't await it

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("app.harness_service._default_run_explain_script", lambda: "/x/run-explain")
    monkeypatch.setattr("app.harness_service._open_run_stderr", lambda rid: subprocess.DEVNULL)
    monkeypatch.setattr("app.harness_service._worker_env", lambda: {"HARNESS_APPDB_DSN": "x"})

    rs = FakeRunStateForExplain()
    app = _make_app(runstate=rs)  # NO harness_handler — the bridge must not be used
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "file", "path": "mod.py"})

    assert resp.status_code == 202
    assert resp.json()["accepted"] is True
    argv = captured["argv"]
    assert argv[0] == "/x/run-explain"
    assert "--repo" in argv and "/abs/project" in argv  # resolved repo_root, not client-supplied
    assert "--kind" in argv and "file" in argv
    assert "--path" in argv and "mod.py" in argv
    assert captured["kwargs"].get("start_new_session") is True  # detached
    assert rs.started and rs.started[0]["lease_owner"] == "explain"


@pytest.mark.asyncio
async def test_start_explain_bad_kind_is_400():
    app = _make_app()
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "nope"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_start_explain_file_without_path_is_400():
    app = _make_app()
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "file"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_start_explain_blast_without_fn_is_400():
    app = _make_app()
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "blast"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_start_explain_no_repo_root_is_400():
    """A project with no absolute repo_root can't be generated host-side → 400."""
    cortex = FakeCortexForExplain(repo_root=None)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.post("/explain/kaidera-os", json={"kind": "file", "path": "m.py"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
#  GET /explain/{project}/result/{run_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_result_hit():
    artifact = {"id": "art-7", "text": "<title>Add</title> preview…",
                "meta": "explain/run-9.html", "category": "html", "source": "artifacts"}
    cortex = FakeCortexForExplain(artifact=artifact)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/result/run-9")
    assert resp.status_code == 200
    data = resp.json()
    assert data["artifact_id"] == "art-7"
    assert data["source_file"] == "explain/run-9.html"
    assert data["modality"] == "html"
    assert "preview" in data["html"]


@pytest.mark.asyncio
async def test_explain_result_miss_is_404():
    cortex = FakeCortexForExplain(artifact=None)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/result/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
#  GET /explain/{project}/export/{run_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_export_returns_html_and_manifest_tarball():
    html = "<!DOCTYPE html><html><head><title>Export me</title></head><body>ok</body></html>"
    run = _Rec(
        run_id="run-9",
        status="ok",
        spans=[{"kind": "input", "text": "ignored"}, {"kind": "output", "text": html}],
        harness="codex",
        model="gpt-x",
        metadata={"artifact_id": "art-9", "caption": "Export me", "target_kind": "file"},
    )
    app = _make_app(runstate=FakeRunStateForExplain(runs={"run-9": run}))
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/export/run-9")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/gzip")
    assert 'filename="kaidera-explainer-run-9.tar.gz"' in resp.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as archive:
        names = archive.getnames()
        root = "kaidera-explainer-run-9"
        assert names == [f"{root}/explainer.html", f"{root}/manifest.json"]
        assert archive.extractfile(f"{root}/explainer.html").read().decode() == html
        manifest = json.load(archive.extractfile(f"{root}/manifest.json"))
    assert manifest["project"] == "kaidera-os"
    assert manifest["artifact_id"] == "art-9"
    assert manifest["files"] == ["explainer.html"]


@pytest.mark.asyncio
async def test_explain_export_salvages_complete_html_from_errored_run():
    html = "<!DOCTYPE html><html><body>still valid</body></html>"
    run = _Rec(
        run_id="old-error",
        status="error",
        spans=[{"kind": "output", "text": f"Harness status.\n{html}"}],
    )
    app = _make_app(runstate=FakeRunStateForExplain(runs={"old-error": run}))
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/export/old-error")
    assert resp.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as archive:
        exported = archive.extractfile(
            "kaidera-explainer-old-error/explainer.html"
        ).read().decode()
    assert exported == html


@pytest.mark.asyncio
async def test_explain_export_is_project_scoped_and_explain_only():
    html_span = [{"kind": "output", "text": "<!DOCTYPE html><html></html>"}]
    wrong_project = _Rec(run_id="wrong-project", project="marketing", spans=html_span)
    wrong_lane = _Rec(run_id="wrong-lane", lease_owner="chat", spans=html_span)
    rs = FakeRunStateForExplain(
        runs={"wrong-project": wrong_project, "wrong-lane": wrong_lane}
    )
    app = _make_app(runstate=rs)
    async with _client(app) as c:
        project_resp = await c.get("/explain/kaidera-os/export/wrong-project")
        lane_resp = await c.get("/explain/kaidera-os/export/wrong-lane")
    assert project_resp.status_code == 404
    assert lane_resp.status_code == 404


@pytest.mark.asyncio
async def test_explain_export_rejects_incomplete_output():
    run = _Rec(run_id="partial", status="running", spans=[{"kind": "output", "text": "working"}])
    app = _make_app(runstate=FakeRunStateForExplain(runs={"partial": run}))
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/export/partial")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
#  GET /explain/{project}/list — the gallery, now enumerated from run_state
#  (lease_owner='explain'), NOT Cortex content search (which can't prefix-
#  enumerate artifacts — the live-testing bug this fixes).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_list_enumerates_explain_runs_from_runstate():
    """The gallery lists recent `lease_owner='explain'` runs (most-recent-first),
    shaping each into a gallery item: run_id (first-class), artifact_id + target from
    the run's metadata sidecar, the derived source_file, and the status. It reads
    run_state — NOT Cortex search."""
    # newest-first (as the store returns): r2 then r1. Each carries the metadata the
    # explain run stamps on terminal success.
    rows = [
        _Rec(run_id="r2", status="ok", started_at="2026-06-07T11:00:00",
             metadata={"capability": "explain", "artifact_id": "a2",
                       "target_kind": "blast", "target_path": "explain_one",
                       "caption": "Explains explain_one"}),
        _Rec(run_id="r1", status="ok", started_at="2026-06-07T10:00:00",
             metadata={"capability": "explain", "artifact_id": "a1",
                       "target_kind": "file", "target_path": "app/main.py",
                       "caption": "Explains main.py"}),
    ]
    rs = FakeRunStateForExplain(recent_rows=rows)
    app = _make_app(runstate=rs)
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/list")
    assert resp.status_code == 200
    items = resp.json()["artifacts"]

    # most-recent-first, run_id first-class, artifact_id from the run metadata.
    assert [it["run_id"] for it in items] == ["r2", "r1"]
    assert [it["artifact_id"] for it in items] == ["a2", "a1"]
    # target + caption recovered from the metadata sidecar (no input-span parsing).
    assert items[0]["target_kind"] == "blast"
    assert items[0]["target_path"] == "explain_one"
    assert items[0]["caption"] == "Explains explain_one"
    assert items[1]["target_path"] == "app/main.py"
    # the source_file is the deterministic explain/<run_id>.html (SPA back-compat).
    assert all(it["source_file"] == f"explain/{it['run_id']}.html" for it in items)
    assert all(it["modality"] == "html" for it in items)
    # status surfaced so the gallery can show generating/errored.
    assert items[0]["status"] == "ok"

    # It actually enumerated via the explain lease (not Cortex search).
    assert rs.recent_calls and rs.recent_calls[0]["lease_owner"] == "explain"


@pytest.mark.asyncio
async def test_explain_list_falls_back_to_run_id_when_metadata_sparse():
    """A run whose metadata lacks an artifact_id / target still appears: run_id stays
    first-class, artifact_id is None (still generating / L5 write degraded), and the
    caption falls back to the run_id short. The gallery never drops a real explain run
    just because its sidecar is thin."""
    rows = [
        _Rec(run_id="abc12345-run", status="running", started_at="2026-06-07T12:00:00",
             metadata={"capability": "explain"}),  # no artifact_id, no target yet
        _Rec(run_id="def67890-run", status="ok", started_at="2026-06-07T11:00:00",
             metadata=None),  # no sidecar at all
    ]
    rs = FakeRunStateForExplain(recent_rows=rows)
    app = _make_app(runstate=rs)
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/list")
    assert resp.status_code == 200
    items = resp.json()["artifacts"]
    assert [it["run_id"] for it in items] == ["abc12345-run", "def67890-run"]
    assert items[0]["artifact_id"] is None
    assert items[0]["status"] == "running"
    # a caption is always present (falls back to the run short) so the row renders.
    assert items[0]["caption"]
    assert items[1]["artifact_id"] is None


@pytest.mark.asyncio
async def test_explain_list_labels_valid_html_from_errored_run_as_recovered():
    header = _Rec(run_id="recovered-run", status="error")
    hydrated = _Rec(
        run_id="recovered-run",
        status="error",
        spans=[
            {"kind": "output", "text": "Harness progress.\n"},
            {"kind": "output", "text": "<!DOCTYPE html><html><body>done</body></html>"},
        ],
    )
    rs = FakeRunStateForExplain(
        recent_rows=[header],
        runs={"recovered-run": hydrated},
    )
    app = _make_app(runstate=rs)

    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/list")

    assert resp.status_code == 200
    assert resp.json()["artifacts"][0]["status"] == "recovered"


@pytest.mark.asyncio
async def test_explain_list_empty_on_down_runstate():
    """A None run-state store (app-DB down) degrades to {artifacts: []} — never a 500."""
    app = FastAPI()
    app.include_router(explain_router)
    app.state.cortex = FakeCortexForExplain()
    app.state.runstate = None  # run-state SSOT unavailable
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/list")
    assert resp.status_code == 200
    assert resp.json() == {"artifacts": []}


@pytest.mark.asyncio
async def test_explain_list_empty_when_runstate_raises():
    """A run-state read that RAISES degrades to {artifacts: []} (graceful-degrade —
    a store hiccup never 500s the gallery)."""
    rs = FakeRunStateForExplain(recent_raises=True)
    app = _make_app(runstate=rs)
    async with _client(app) as c:
        resp = await c.get("/explain/kaidera-os/list")
    assert resp.status_code == 200
    assert resp.json() == {"artifacts": []}
