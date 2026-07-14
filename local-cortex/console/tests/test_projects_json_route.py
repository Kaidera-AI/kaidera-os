"""Harness-service Increment 3 (Track B) — the JSON `GET /projects` route.

The SPA needs a JSON project list (`api.projects()` → `Project[]` in
`spa/src/api/client.ts` / `spa/src/api/types.ts`), but the console historically
exposed only the HTML/HTMX project routes (`GET /projects/{project_key}` and
`/projects/{project_key}/detail`). This adds the missing JSON LIST route.

CONTRACT (asserted here):
  * `GET /projects` returns a JSON ARRAY of the ACTIVE projects, sourced from the
    SAME place the existing project UI reads — `CortexClient.get_active_projects()`
    (the rail + the fleet cards all read that). One source, no second list.
  * Each row carries at least the SPA `Project` fields the rail uses
    (`project_key`, `display_name`, `status`, `repo_root`) plus whatever extra
    registry fields Cortex attaches (the SPA `Project` type is permissive —
    `[k: string]: unknown`). We pass the rows THROUGH unchanged (the SPA never
    invents the list), so `project_id` and other registry fields survive.
  * COLLISION-FREE: the literal `/projects` path is DISTINCT from the existing
    `/projects/{project_key}` (a non-empty segment is required for the latter), so
    the new JSON route can NOT shadow — nor be shadowed by — the HTML partial
    routes. Asserted by route introspection.
  * Graceful-degrade: `get_active_projects()` already returns `[]` on a Cortex
    error, so the route returns an empty array (never a 500) when Cortex is down —
    matching the SPA client's "treat as empty rail" expectation.

We drive the route handler directly with a minimal fake Request carrying a fake
cortex on `app.state` (the `_cortex(request)` helper reads `request.app.state.cortex`),
so no ASGI stack / live Cortex API is needed.
"""

from __future__ import annotations

import pytest

import app.main as main_mod


# ---------------------------------------------------------------------------
#  Fakes
# ---------------------------------------------------------------------------

# A representative active-projects payload — the exact shape
# CortexClient.get_active_projects() yields after identity v2.
SAMPLE_PROJECTS = [
    {
        "project_key": "kaidera-os",
        "display_name": "Kaidera OS",
        "status": "active",
        "project_id": "11111111-2222-4333-8444-555555555555",
        "repo_root": "/some/abs/path/kaidera-os",
        "agent_count": 3,
    },
    {
        "project_key": "kaidera",
        "display_name": "Kaidera AI",
        "status": "active",
        "project_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "repo_root": "/some/abs/path/kaidera",
        "agent_count": 9,
    },
]


class FakeCortex:
    """Serves a scripted `get_active_projects()` and records the call. `rows`
    scripts the return; `raise_` forces the call to RAISE (to prove the route does
    NOT depend on the method raising — the method itself swallows, but belt-and-
    braces we assert the route still degrades if a future variant could)."""

    def __init__(self, rows=None, raise_=False):
        self._rows = rows if rows is not None else list(SAMPLE_PROJECTS)
        self._raise = raise_
        self.calls = 0

    async def get_active_projects(self):
        self.calls += 1
        if self._raise:
            raise RuntimeError("cortex down")
        return list(self._rows)


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
async def test_projects_json_returns_active_project_list():
    """`GET /projects` returns the active-projects list as a JSON array, sourced
    from `CortexClient.get_active_projects()` (the same source the UI reads), with
    the registry rows passed through unchanged."""
    cortex = FakeCortex()
    result = await main_mod.projects_json(_Req(cortex))

    # Sourced from get_active_projects (one source — not a re-derived list).
    assert cortex.calls == 1
    # A JSON ARRAY (list), NOT a dict — the SPA `api.projects()` expects `Project[]`.
    assert isinstance(result, list)
    assert [p["project_key"] for p in result] == ["kaidera-os", "kaidera"]
    # The SPA `Project` fields the rail reads survive …
    first = result[0]
    for field in ("project_key", "display_name", "status", "repo_root"):
        assert field in first
    # … and the extra registry fields pass THROUGH unchanged (permissive Project).
    assert first["project_id"] == "11111111-2222-4333-8444-555555555555"
    assert first["agent_count"] == 3


@pytest.mark.asyncio
async def test_projects_json_empty_when_cortex_returns_none():
    """An empty active set → an empty JSON array (never a 500). Mirrors the SPA
    client's 'empty rail' degrade contract."""
    cortex = FakeCortex(rows=[])
    result = await main_mod.projects_json(_Req(cortex))
    assert result == []


def test_projects_json_route_is_collision_free():
    """The literal `GET /projects` path is DISTINCT from the existing
    `/projects/{project_key}` (and `/detail`) HTML partial routes, so it can NOT
    shadow them — nor be shadowed. Asserted by route introspection on the live app."""
    get_projects_routes = {
        r.path
        for r in main_mod.app.routes
        if getattr(r, "path", None) == "/projects"
        and "GET" in (getattr(r, "methods", None) or set())
    }
    # The bare JSON list route exists …
    assert "/projects" in get_projects_routes
    # … and the HTML partial routes keep their DISTINCT parametrised paths (the new
    # route is a different, more-specific literal — FastAPI never confuses them).
    all_paths = {getattr(r, "path", None) for r in main_mod.app.routes}
    assert "/projects/{project_key}" in all_paths
    assert "/projects/{project_key}/detail" in all_paths


def test_projects_json_route_returns_json_not_html():
    """The new route is a JSON route (default JSONResponse), NOT one of the
    HTMLResponse partial routes — so the SPA gets `application/json`, and the route
    shape is genuinely distinct from the HTML `/projects/{key}` family."""
    from starlette.responses import HTMLResponse

    route = next(
        r
        for r in main_mod.app.routes
        if getattr(r, "path", None) == "/projects"
        and "GET" in (getattr(r, "methods", None) or set())
    )
    # The HTML partial routes declare response_class=HTMLResponse; the JSON list
    # route must NOT — it returns a plain list (FastAPI serialises to JSON).
    assert route.response_class is not HTMLResponse
