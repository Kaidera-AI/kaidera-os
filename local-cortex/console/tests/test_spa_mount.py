"""Track C final step — the containerized console SERVES the SPA at ``/app``.

The SPA (``spa/dist``, a Vite production bundle) is mounted by the FastAPI app via
``StaticFiles(html=True)`` at ``/app`` so the operator can use the refined console
at ``http://127.0.0.1:8765/app`` — same-origin with the module APIs it already
calls (``/agents`` · ``/runs`` · ``/dispatch`` · ``/analytics`` · ``/settings`` ·
``/projects`` · ``/runstate/stream``), so no CORS/proxy is needed when served from
:8765.

CONTRACT (asserted here, against a FIXTURE ``dist`` dir — no real ``npm build`` in
the unit test):
  * The mount is ADDITIVE — it serves the SPA at ``/app`` while the legacy HTML
    routes (incl. ``/``) are untouched. We assert ``/`` still answers.
  * ``GET /app/``           → 200 + the SPA ``index.html`` (StaticFiles ``html=True``
    serves ``index.html`` for the directory).
  * ``GET /app/assets/<f>`` → 200 + the hashed asset bytes (so ``base: '/app/'``
    asset URLs resolve under the mount).
  * Deep links under ``/app`` (client-side routes with no matching file) fall back
    to ``index.html`` (200), so a refresh on a deep link is not a 404.
  * A MISSING ``dist`` (the SPA was never built) must NOT crash the app at
    import/startup: ``mount_spa`` logs + SKIPS the mount and returns ``False``; the
    app still boots and the legacy ``/`` route still answers 200 (``/app/`` is then
    simply absent → 404, the honest "not built" state, never a 500).

We exercise the real ``app.main.mount_spa`` helper on a fresh minimal ``FastAPI``
app (with a stand-in legacy ``/`` route) via the FastAPI ``TestClient`` — so the
mount logic is tested in isolation WITHOUT booting the full app lifespan
(orchestrator/watchdog/Cortex client). This is the SDK-style testable seam: the
real app calls the same helper at module load with the real ``spa/dist`` path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

import app.main as main_mod

# The exact marker bytes the fixture SPA bundle carries, so we can prove the SPA's
# own index/asset is served (not some other handler).
INDEX_HTML = (
    "<!doctype html><html><head><title>Kaidera OS Console SPA</title>"
    '<script type="module" src="/app/assets/index-DEADBEEF.js"></script>'
    "</head><body><div id=\"root\"></div></body></html>"
)
ASSET_JS = "/* spa fixture bundle */ console.log('spa');\n"
LEGACY_BODY = "<html><body>LEGACY CONSOLE HTML</body></html>"


def _write_fixture_dist(root: Path) -> Path:
    """Build a minimal ``dist`` dir that looks like a Vite production bundle:
    an ``index.html`` + a hashed asset under ``assets/``."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index-DEADBEEF.js").write_text(ASSET_JS, encoding="utf-8")
    return dist


def _app_with_legacy_root() -> FastAPI:
    """A fresh minimal app carrying a stand-in legacy ``/`` route — proves the SPA
    mount is ADDITIVE (it never displaces ``/``)."""
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def _legacy_root() -> HTMLResponse:  # pragma: no cover - trivial
        return HTMLResponse(LEGACY_BODY)

    return app


def test_mount_spa_serves_index_at_app(tmp_path: Path):
    """``GET /app/`` → 200 + the SPA index HTML; the mount is additive (``/`` still
    serves the legacy HTML)."""
    dist = _write_fixture_dist(tmp_path)
    app = _app_with_legacy_root()

    mounted = main_mod.mount_spa(app, dist)
    assert mounted is True  # dist exists → the mount was added

    client = TestClient(app)

    # /app/ serves the SPA index (StaticFiles html=True).
    res = client.get("/app/")
    assert res.status_code == 200
    assert "Kaidera OS Console SPA" in res.text
    assert 'src="/app/assets/index-DEADBEEF.js"' in res.text

    # ADDITIVE: the legacy / route is untouched.
    legacy = client.get("/")
    assert legacy.status_code == 200
    assert "LEGACY CONSOLE HTML" in legacy.text


def test_mount_spa_serves_hashed_asset(tmp_path: Path):
    """A hashed asset under ``/app/assets/`` → 200 + its bytes (so ``base:'/app/'``
    asset URLs resolve under the mount)."""
    dist = _write_fixture_dist(tmp_path)
    app = _app_with_legacy_root()
    main_mod.mount_spa(app, dist)
    client = TestClient(app)

    res = client.get("/app/assets/index-DEADBEEF.js")
    assert res.status_code == 200
    assert "spa fixture bundle" in res.text


def test_mount_spa_deep_link_falls_back_to_index(tmp_path: Path):
    """A deep link under ``/app`` that maps to no file (a client-side route) falls
    back to ``index.html`` (200), so a refresh on a deep link is not a 404."""
    dist = _write_fixture_dist(tmp_path)
    app = _app_with_legacy_root()
    main_mod.mount_spa(app, dist)
    client = TestClient(app)

    res = client.get("/app/some/client/route")
    assert res.status_code == 200
    assert "Kaidera OS Console SPA" in res.text


def test_mount_spa_missing_dist_skips_mount_app_still_boots(tmp_path: Path):
    """A MISSING ``dist`` (SPA never built) → the mount is SKIPPED (returns False),
    the app still boots, and the legacy ``/`` route still answers 200. ``/app/`` is
    then simply absent (404) — the honest 'not built' state, never a 500/crash."""
    missing = tmp_path / "nope" / "dist"  # does not exist
    assert not missing.exists()
    app = _app_with_legacy_root()

    mounted = main_mod.mount_spa(app, missing)
    assert mounted is False  # no dist → no mount, but no exception either

    client = TestClient(app)

    # The legacy app still boots + serves /.
    legacy = client.get("/")
    assert legacy.status_code == 200
    assert "LEGACY CONSOLE HTML" in legacy.text

    # /app/ is absent (not built) → 404, NOT a crash/500.
    res = client.get("/app/")
    assert res.status_code == 404


def test_mount_spa_missing_index_skips_mount(tmp_path: Path):
    """A ``dist`` dir that exists but has NO ``index.html`` (a broken/partial build)
    is treated the same as missing — skip the mount (False), app still boots. Guards
    against StaticFiles raising on a dir with no index when ``html=True``."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)  # dir exists, but no index.html
    app = _app_with_legacy_root()

    mounted = main_mod.mount_spa(app, dist)
    assert mounted is False

    client = TestClient(app)
    assert client.get("/").status_code == 200


def test_real_app_exposes_app_mount_when_dist_present():
    """On the REAL app: when ``spa/dist`` has been built, a ``/app`` mount is present
    in the route table (a Mount whose path is ``/app``). When it has NOT been built,
    NO ``/app`` mount exists (the guard skipped it) — and either way importing
    ``app.main`` did not raise. This pins that the real app wires the SAME helper at
    module load against the real ``spa/dist`` path, additively."""
    spa_dist = main_mod.SPA_DIST_DIR
    has_index = (spa_dist / "index.html").is_file()

    app_mounts = [
        r
        for r in main_mod.app.routes
        if getattr(r, "path", None) == "/app"
        and r.__class__.__name__ == "Mount"
    ]
    if has_index:
        assert len(app_mounts) == 1, "built spa/dist should be mounted at /app"
    else:
        assert app_mounts == [], "no /app mount when spa/dist is not built"

    # ADDITIVE invariant either way: the legacy `/` route is still registered.
    root_routes = {
        getattr(r, "path", None)
        for r in main_mod.app.routes
        if "GET" in (getattr(r, "methods", None) or set())
    }
    assert "/" in root_routes
