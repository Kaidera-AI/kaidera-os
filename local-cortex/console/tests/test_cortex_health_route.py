"""Track 2 — the console JSON `GET /cortex/health` route (the Cortex-tab health fix).

ROOT CAUSE (confirmed in the CTO backlog): the SPA Cortex tab read same-origin
`/health` for a JSON health read-out, but the console exposes NO JSON `/health`
(only the HTMX `/health-pill` HTML partial), so `GET /health` 404s and the tab
always showed "unreachable" — even though the container reaches Cortex fine
(`CortexClient.get_health()` returns a healthy dict).

THE FIX: add a console JSON health endpoint at a clean, NON-colliding path,
`GET /cortex/health`, that returns `CortexClient.get_health()` MERGED with the
connection info the Cortex tab wants: `base_url`, `project`, plus the surface
fields the health dict carries (`status`, `surface_version`, `event_backend`,
`rls_enforced`).

CONTRACT (asserted here):
  * `GET /cortex/health` returns the health dict from `CortexClient.get_health()`
    (the SAME source the HTML pill reads — one source, no second probe) WITH the
    connection fields folded in (`base_url` from the client, `project` echoed).
  * A down Cortex → `get_health()` already returns the synthetic
    `{"status": "unreachable", ...}` shape, so the route returns that (still HTTP
    200, the connection fields still present) — NEVER a 500. The tab then shows a
    real "unreachable" derived from a genuine reachable endpoint, not a 404.
  * COLLISION-FREE: `/cortex/health` is a brand-new path family (`/cortex/...`)
    that no existing route owns, and it is NOT under the SPA static mount (`/app`),
    so it can neither shadow nor be shadowed. Asserted by route introspection.
  * It is a JSON route (default JSONResponse), NOT one of the HTMLResponse
    partials — the SPA gets `application/json`.

We drive the route handler directly with a minimal fake Request carrying a fake
cortex on `app.state` (the `_cortex(request)` helper reads
`request.app.state.cortex`), so no ASGI stack / live Cortex API is needed — the
same idiom as `test_projects_json_route.py`.
"""

from __future__ import annotations

import pytest

import app.main as main_mod


# ---------------------------------------------------------------------------
#  Fakes
# ---------------------------------------------------------------------------

# A representative healthy payload — the exact shape CortexClient.get_health()
# yields from the live `/health` (status + the surface fields the tab renders).
HEALTHY = {
    "status": "healthy",
    "surface_version": "v2.5",
    "event_backend": "postgres",
    "rls_enforced": True,
}

# The synthetic shape get_health() returns when the API is unreachable.
UNREACHABLE = {
    "status": "unreachable",
    "surface_version": None,
    "error": "connect timed out",
}


class FakeCortex:
    """Serves a scripted `get_health()` and records the call. `base_url` is the
    client's configured base (the route folds it into the response as connection
    info). `health` scripts the return dict."""

    def __init__(self, health=None, base_url="http://localhost:8501"):
        self._health = dict(health if health is not None else HEALTHY)
        self.base_url = base_url
        self.calls = 0
        self.backlog_calls = []
        self.backfill_calls = []
        self.job_calls = []

    async def get_health(self):
        self.calls += 1
        return dict(self._health)

    async def get_embedding_backlog(self, project):
        self.backlog_calls.append(project)
        return {
            "backlog": {"decisions": 1, "knowledge": 2, "total": 3},
            "coverage": {
                "decisions": {"total": 5, "embedded": 4, "backlog": 1, "skipped": 0, "pct": 80.0},
                "knowledge": {"total": 4, "embedded": 2, "backlog": 2, "skipped": 0, "pct": 50.0},
            },
        }

    async def backfill_embeddings(self, project, body):
        self.backfill_calls.append((project, dict(body)))
        return {"project": project, "table": body.get("table"), "dry_run": body.get("dry_run"), "processed": 3}

    async def get_embedding_backfill_job(self, project, job_id):
        self.job_calls.append((project, job_id))
        return {"id": job_id, "project": project, "status": "completed"}


class _Req:
    """Minimal fake Request carrying app.state.cortex for `_cortex(request)`."""

    def __init__(self, cortex):
        self.app = type("App", (), {})()
        self.app.state = type("State", (), {})()
        self.app.state.cortex = cortex


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cortex_health_returns_health_plus_connection():
    """`GET /cortex/health` returns the health dict from `get_health()` (the same
    source the HTML pill reads) with the connection fields folded in — the surface
    fields the tab renders survive, plus `base_url` + `project`."""
    cortex = FakeCortex()
    result = await main_mod.cortex_health_json(_Req(cortex), project="kaidera-os")

    # Sourced from get_health (one source — not a second probe).
    assert cortex.calls == 1
    # The surface fields the tab renders survive verbatim.
    assert result["status"] == "healthy"
    assert result["surface_version"] == "v2.5"
    assert result["event_backend"] == "postgres"
    assert result["rls_enforced"] is True
    # Connection info folded in: the client's base URL + the echoed project.
    assert result["base_url"] == "http://localhost:8501"
    assert result["project"] == "kaidera-os"


@pytest.mark.asyncio
async def test_cortex_health_degrades_to_unreachable_not_500():
    """A down Cortex → `get_health()` returns the synthetic 'unreachable' shape, so
    the route returns that (status 'unreachable', the connection fields still
    present) — NEVER a 500. This is the real "unreachable" the tab should show, off
    a genuinely reachable console endpoint (not a 404)."""
    cortex = FakeCortex(health=UNREACHABLE)
    result = await main_mod.cortex_health_json(_Req(cortex), project="kaidera-os")

    assert result["status"] == "unreachable"
    # the connection fields are still present (so the tab still shows base_url/project)
    assert result["base_url"] == "http://localhost:8501"
    assert result["project"] == "kaidera-os"
    # the underlying error is surfaced (the tab shows "couldn't reach Cortex: …")
    assert result["error"] == "connect timed out"


@pytest.mark.asyncio
async def test_cortex_health_project_defaults_when_omitted():
    """With no `project` query the route echoes the console's CONFIGURED default
    project (`_default_project()` — Settings/env, generic/empty by default) — never
    a crash on a missing param. The harness hardcodes no project name (§2.7), so
    this tracks the resolver, not a fixed 'kaidera-os'."""
    cortex = FakeCortex()
    result = await main_mod.cortex_health_json(_Req(cortex), project=None)
    assert result["project"] == main_mod._default_project()


def test_cortex_health_route_is_collision_free():
    """The literal `GET /cortex/health` path is a brand-new `/cortex/...` family no
    existing route owns, and it is NOT under the `/app` SPA mount — so it can
    neither shadow nor be shadowed. Asserted by route introspection on the live app."""
    cortex_health_get = {
        r.path
        for r in main_mod.app.routes
        if getattr(r, "path", None) == "/cortex/health"
        and "GET" in (getattr(r, "methods", None) or set())
    }
    assert "/cortex/health" in cortex_health_get
    # The /cortex/* family is owned entirely by the console (new + unshared): the health
    # read-out + the admin-token status probe. No EXTERNAL route shadows them.
    cortex_family = {
        getattr(r, "path", "")
        for r in main_mod.app.routes
        if str(getattr(r, "path", "")).startswith("/cortex/")
    }
    assert cortex_family == {
        "/cortex/health",
        "/cortex/admin-status",
        "/cortex/config",
        "/cortex/embeddings/backlog",
        "/cortex/embeddings/backfill",
        "/cortex/embeddings/jobs/{job_id}",
    }
    # … and it does NOT live under the SPA static mount.
    assert not "/cortex/health".startswith("/app")


def test_cortex_health_route_returns_json_not_html():
    """The new route is a JSON route (default JSONResponse), NOT one of the
    HTMLResponse partial routes — so the SPA gets `application/json`."""
    from starlette.responses import HTMLResponse

    route = next(
        r
        for r in main_mod.app.routes
        if getattr(r, "path", None) == "/cortex/health"
        and "GET" in (getattr(r, "methods", None) or set())
    )
    assert route.response_class is not HTMLResponse


@pytest.mark.asyncio
async def test_cortex_embedding_backlog_proxy_returns_project_coverage():
    cortex = FakeCortex()
    result = await main_mod.cortex_embeddings_backlog_json(_Req(cortex), project="kaidera-os")

    assert cortex.backlog_calls == ["kaidera-os"]
    assert result["ok"] is True
    assert result["project"] == "kaidera-os"
    assert result["backlog"]["total"] == 3
    assert result["coverage"]["knowledge"]["backlog"] == 2
    assert result["error"] is None


@pytest.mark.asyncio
async def test_cortex_embedding_backfill_proxy_forwards_request_body():
    cortex = FakeCortex()
    result = await main_mod.cortex_embeddings_backfill_json(
        _Req(cortex),
        payload={"table": "knowledge", "limit": 25, "dry_run": True},
        project="kaidera-os",
    )

    assert cortex.backfill_calls == [
        ("kaidera-os", {"table": "knowledge", "limit": 25, "dry_run": True})
    ]
    assert result["ok"] is True
    assert result["result"]["processed"] == 3


@pytest.mark.asyncio
async def test_cortex_embedding_job_proxy_forwards_project_and_job():
    cortex = FakeCortex()
    result = await main_mod.cortex_embedding_backfill_job_json(
        _Req(cortex),
        job_id="job-1",
        project="kaidera-os",
    )

    assert cortex.job_calls == [("kaidera-os", "job-1")]
    assert result["ok"] is True
    assert result["job"]["status"] == "completed"
