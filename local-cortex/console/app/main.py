"""Kaidera OS Harness Console — FastAPI app.

The real multi-column console shell, wired to LIVE Cortex data. Matches the
`design/console-v2.html` prototype's visual language (dark #001220 top bar,
light #F8FAFB body, multi-column layout, Space Grotesk) but rebuilt as clean
Jinja templates over the existing app.css brand tokens.

Shell-agnostic: NO pywebview imports here. The same ASGI `app` runs under
plain `uvicorn app.main:app` (dev) and inside the packaged pywebview window
later (see ../bootstrap.py).

Layout (4 columns): rail (project switcher) · agents · center · workspace.
  - Top bar: dark, Kaidera AI logo, nav tabs (Dashboard · History · Graph ·
    Analytics · Settings), health pill. Nav switches the CENTER view; only
    Dashboard is live (the rest render a placeholder).
  - Column 1 (rail): project switcher over real active projects, each row
    showing a cross-project "needs attention" summary (pending handoffs +
    active tasks from /state). Default-selects the active internal project key.
  - Column 2 (agents): the selected project's real agents (from /runtime,
    falling back to /roster), grouped Interactive (Lead) vs Autonomous.
    Clicking an agent swaps the CENTER to that agent's detail view (R2).
  - Column 3 (center): the live Dashboard view by default; an AGENT-DETAIL
    view (header + token usage + recent-activity feed + chat composer) when
    an agent is selected (R2). Clicking the project / "back to Dashboard"
    returns the center to the Dashboard.
  - Column 4 (workspace): a labeled "Workspace · R5" placeholder.

Routes:
  GET /                       full console shell (default Dashboard view)
  GET /projects/{key}         HTMX partial — agents column + center for a project
  GET /agents/{key}/{agent}   HTMX partial — swap the center to an agent's detail
  GET /views/{view}           HTMX partial — switch the center view (nav tabs)
  GET /health-pill            HTMX partial — just the health pill (live refresh)

All data is pulled live from the local Cortex API via CortexClient. Read-only:
no route mutates Cortex. The R2 agent-detail chat composer is UI-only — its
SEND is NOT wired to any harness yet (see TODO(harness) in _agent_detail.html).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

import httpx
from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from . import appdb as appdb_store
from . import attachments as attachments_module
from . import chat_ltm as chat_ltm_module
from . import claude_catalog
from . import codex_catalog
from . import harness as harness_cfg
from . import harness_runner
from . import orchestrator as orchestrator_mod
from . import pi_catalog
from . import provider_check
from . import providers as providers_catalog
from . import settings as settings_store
from . import project_profile
from . import watchdog as watchdog_mod
from . import workspace as ws
from . import analytics as analytics_module
from . import agents as agents_module
from . import automation_api as automation_module
from . import auth as auth_module
from . import registration_api as registration_module
from . import skills_api as skills_module
from . import settings_module
from .settings_module import api as settings_api
from . import dispatch as dispatch_module
from .dispatch.command import DispatchWorkerSpec, dispatch_worker
from . import explain as explain_module
from . import plan as plan_module
from . import graph as graph_module
from . import history as history_module
from . import local_run_tasks
from . import runs as runs_module
from .domain import roles as role_alias
from .adapters import runstate_pg
from .adapters.opstore import AppDbOperationalStore
from .cortex_client import AdminTokenMissing, CortexClient
from .cortex_client import masked_admin_token
from .cortex_client import resolve_admin_token as cortex_admin_token
from .workspace import WorkspaceError
from .version import __version__


def _load_console_extensions() -> list[Any]:
    """Load explicit project/customer extensions.

    Core Kaidera OS is project-agnostic. A deployment that needs a bespoke worker
    adds the installed project-pack root to KAIDERA_OS_EXTENSION_PATHS and names its
    module in KAIDERA_OS_EXTENSION_MODULES. Nothing project-specific is imported by
    default.
    """
    _install_console_extension_paths()
    raw = os.environ.get("KAIDERA_OS_EXTENSION_MODULES", "")
    modules: list[Any] = []
    for name in [part.strip() for part in raw.split(",") if part.strip()]:
        try:
            modules.append(importlib.import_module(name))
        except Exception as exc:
            raise RuntimeError(f"Failed to load Kaidera OS extension module {name!r}") from exc
    return modules


def _split_extension_path_entries(raw: str | None) -> list[str]:
    """Split the extension path env. Accept pathsep and comma for operator UX."""
    seen: set[str] = set()
    entries: list[str] = []
    for chunk in str(raw or "").replace(",", os.pathsep).split(os.pathsep):
        entry = chunk.strip()
        if not entry or entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    return entries


def _install_console_extension_paths() -> list[str]:
    """Prepend explicit project-pack roots to sys.path before importing modules."""
    added: list[str] = []
    for raw_path in _split_extension_path_entries(os.environ.get("KAIDERA_OS_EXTENSION_PATHS")):
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise RuntimeError(f"Kaidera OS extension path must be absolute: {raw_path!r}")
        if not path.is_dir():
            raise RuntimeError(f"Kaidera OS extension path is not a directory: {raw_path!r}")
        resolved = str(path.resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
            added.append(resolved)
    return added


CONSOLE_EXTENSION_MODULES = _load_console_extensions()


def _extension_routing_override(
    agent_name: str,
    project_key: str,
    model: str | None,
    reasoning: str | None,
) -> tuple[str, str | None, str | None] | None:
    for module in CONSOLE_EXTENSION_MODULES:
        fn = getattr(module, "registered_agent_routing_override", None)
        if not callable(fn):
            continue
        override = fn(agent_name, project_key, model, reasoning)
        if override is not None:
            return override
    return None


def _register_console_extension_hooks(app: FastAPI) -> None:
    """Register routers and public auth paths declared by installed extensions."""
    for extension_module in CONSOLE_EXTENSION_MODULES:
        for matcher in getattr(extension_module, "public_path_matchers", []) or []:
            auth_module.register_public_path_matcher(matcher)
        for path in getattr(extension_module, "public_paths", []) or []:
            auth_module.register_public_path_matcher(str(path))

        register_public_paths = getattr(extension_module, "register_public_paths", None)
        if callable(register_public_paths):
            register_public_paths(auth_module.register_public_path_matcher)

        register_routers = getattr(extension_module, "register_routers", None)
        if callable(register_routers):
            register_routers(app)

        extension_router = getattr(extension_module, "router", None)
        if extension_router is not None:
            app.include_router(extension_router)


def _configure_console_logging() -> None:
    """Wire the console's application loggers to a stdout/journal handler.

    WHY (redist dogfood GAP #2): the console runs under ``uvicorn app.main:app``,
    and uvicorn configures ONLY its own ``uvicorn*`` loggers — it never touches the
    app's ``console`` / ``console.auth`` / ``console.project_profile`` loggers. With no
    handler on those, every ``log.info(...)`` they emit (notably ``auth._send_login_email``
    logging the first-admin one-time sign-in code in ``delivery=log`` mode) is silently
    dropped, so the documented "read the bootstrap code from the console journal" path is
    broken on a fresh install. This attaches a StreamHandler to the ``console`` logger
    namespace at INFO so those records reach stdout (→ the systemd journal).

    Scoped + idempotent + non-clobbering: we configure ONLY the ``console`` parent logger
    (every app logger is ``console.*`` → propagates to it), set ``propagate=False`` so we
    don't double-log through the root, and bail if it already has a handler (so a re-import
    under reload can't stack handlers). Uvicorn's own loggers are left untouched. The level
    is ``KAIDERA_LOG_LEVEL`` (default INFO)."""
    import logging as _logging

    level_name = (os.environ.get("KAIDERA_LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(_logging, level_name, _logging.INFO)
    logger = _logging.getLogger("console")
    logger.setLevel(level)
    # Don't double-emit through the root handler (uvicorn/gunicorn may add one); the
    # console namespace owns its own stream.
    logger.propagate = False
    if not logger.handlers:
        handler = _logging.StreamHandler()  # stdout/stderr → journal under systemd
        handler.setLevel(level)
        handler.setFormatter(
            _logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)


# Configure app logging at import time so it is wired no matter how the ASGI app is
# launched (``uvicorn app.main:app`` in the redist/systemd, or bootstrap.py's in-process
# uvicorn.Server) — both import this module. Best-effort: a logging-setup failure must
# never block the app from importing.
with suppress(Exception):
    _configure_console_logging()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
LOCAL_CORTEX_DIR = BASE_DIR.parent.parent
REPO_ROOT = LOCAL_CORTEX_DIR.parent
UPDATE_LOG_DIR = LOCAL_CORTEX_DIR / "logs"
UPDATE_JOB_STATUS_PATH = UPDATE_LOG_DIR / "update-job.json"
DEFAULT_RELEASE_REPO = "Kaidera-AI/homebrew-kaidera"
UPDATE_STATUS_CACHE_TTL_SECONDS = float(os.environ.get("KAIDERA_OS_UPDATE_STATUS_TTL", "900"))
UPDATE_IMPACT = [
    "Downloads and verifies the latest signed Kaidera OS release.",
    "Synchronizes release-managed app files and prunes stale shipped files when rsync is available.",
    "Runs install.sh, which may rebuild/recreate Cortex services, run migrations, and restart the console.",
    "Preserves deployment-local runtime state such as local-cortex/.env, .console-host, logs, and database volumes.",
]
UPDATE_BACKUP_GUIDANCE = [
    "For routine patch updates, update.sh preserves Kaidera OS runtime secrets and local state.",
    "Before major upgrades, take a VM snapshot or back up local-cortex/.env plus Docker volumes.",
    "The update job log path is recorded in /console/update-job for audit and troubleshooting.",
]
UPDATE_ROLLBACK_GUIDANCE = [
    "To roll back to a known release, run KAIDERA_RELEASE=vX.Y.Z ./update.sh from the install root.",
    "If the console process does not restart cleanly, restart the console service and re-check /console/version.",
    "If Cortex health fails after update, keep the install in place and inspect the recorded update log before re-running.",
]
UPDATE_POST_UPDATE_CHECKS = [
    "console /console/version responds",
    "console /healthz responds",
    "Cortex admin status is reachable",
]
_UPDATE_STATUS_CACHE_LOCK = threading.Lock()
_UPDATE_STATUS_CACHE: dict[str, Any] | None = None
_UPDATE_STATUS_CACHE_AT = 0.0
_UPDATE_STATUS_REFRESHING = False
# The refined SPA (Track C) production bundle: `spa/dist`, a sibling of `app/`
# under the console dir. Served at `/app` by `mount_spa` below (see that helper).
# BASE_DIR is `<console>/app`, so the bundle is `<console>/spa/dist`.
SPA_DIST_DIR = BASE_DIR.parent / "spa" / "dist"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Expose the build version to EVERY template (shown bottom-right in the UI) so the
# operator can tell builds apart. Source of truth: app/version.py + CHANGELOG.md.
templates.env.globals["app_version"] = __version__

# Center views reachable from the top-bar nav. Dashboard (R1), Settings (R4),
# and History · Graph · Analytics (R7) are all LIVE — each is wired to live
# Cortex data below. The value is the human label shown in the nav.
NAV_VIEWS: dict[str, str] = {
    "dashboard": "Dashboard",
    "dispatch": "Dispatch",
    "history": "History",
    "graph": "Graph",
    "analytics": "Analytics",
    "settings": "Settings",
}
# Which increment each not-yet-live view is slated for (placeholder caption).
# Empty now: every nav view is wired (R1 dashboard · R4 settings · R7 the rest).
# Kept as the single fallback table for an unknown view id (→ generic stub).
VIEW_INCREMENT: dict[str, str] = {}

# ---------------------------------------------------------------------------
#  Settings view (R4a/R4b/R4c) — 4 sub-tabs, all now functional:
#    Configure (R4c) · Providers & Models (R4b) · Cortex (R4c) · System (R4a).
# ---------------------------------------------------------------------------
# Ordered sub-tabs: id → (label, soon=increment-or-None-if-live). All live now.
SETTINGS_TABS: list[dict[str, str | None]] = [
    {"id": "configure", "label": "Configure", "soon": None},
    {"id": "providers", "label": "Models", "soon": None},
    {"id": "projects", "label": "Workspace", "soon": None},
    {"id": "cortex", "label": "Cortex", "soon": None},
    {"id": "license", "label": "License & Account", "soon": None},
    {"id": "system", "label": "System", "soon": None},
]
SETTINGS_TAB_IDS = {t["id"] for t in SETTINGS_TABS}
DEFAULT_SETTINGS_TAB = "system"

# Human label + the increment each not-yet-live settings sub-tab lands in.
# All four sub-tabs are now live (R4a–R4c), so nothing is placeholdered here.
# Kept as the single fallback table for an unknown sub-tab id (→ generic stub).
_SETTINGS_PLACEHOLDER: dict[str, tuple[str, str]] = {}

# Inline sub-nav icons (stroke="currentColor"), matched to the prototype look.
SETTINGS_TAB_ICONS: dict[str, str] = {
    "configure": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/>'
        '<path d="M1 14h6M9 8h6M17 16h6"/></svg>'
    ),
    "license": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>'
        '</svg>'
    ),
    "providers": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<rect x="3" y="4" width="18" height="6" rx="1.5"/>'
        '<rect x="3" y="14" width="18" height="6" rx="1.5"/>'
        '<path d="M7 7h.01M7 17h.01"/></svg>'
    ),
    "projects": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>'
    ),
    "cortex": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="9" r="2.5"/>'
        '<circle cx="9" cy="18" r="2.5"/><path d="M8 7.3l8 1.2M8.2 16.2l1-7M15.6 11l-5.4 5"/></svg>'
    ),
    "system": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>'
    ),
}

def _default_project() -> str:
    """The console's default project key — resolved from CONFIG, never hardcoded.

    Resolution order (the harness ships project-agnostic; §2.7) — precedence
    env/setting OVERRIDE > project profile > empty:
      1. app-DB `cortex_default_project` setting (Settings → System; empty by default)
      2. env `KAIDERA_DEFAULT_PROJECT` (per-deployment override)
      3. the dropped-in project's PROFILE `default_project` hint — discovered from the
         configured profiles dir (a profile DECLARES itself the default), so a FRESH
         install opens on the right project with NO `KAIDERA_DEFAULT_PROJECT` set.
      4. "" (empty — the shell's project picker / `_pick_selected` then falls back
         to the FIRST registered Cortex project, which is the live-list tier).

    Returns "" when nothing is configured; the SPA already handles a no-default
    shell with a picker, and `_pick_selected` resolves the first project from the
    live /projects list (so an empty default never leaves the shell projectless)."""
    try:
        cfg = settings_store.load()
        val = (cfg.get("cortex_default_project") or "").strip()
        if val:
            return val
    except Exception:
        pass  # settings store unavailable → fall through to env / profile / empty
    env = (os.environ.get("KAIDERA_DEFAULT_PROJECT") or "").strip()
    if env:
        return env
    # Profile-sourced DEFAULT (the fresh-install path): a dropped-in project profile
    # self-declares as the default. Tolerant — "" when no profile does; never blocks.
    try:
        return project_profile.discover_default_project()
    except Exception:
        return ""


# Task statuses that count as "live / in-flight" work. Everything NOT in this
# set is a *pending* task for the col-2 Metrics block (/state has no pending-task
# counter, so we derive it from /board).
_ACTIVE_TASK_STATUSES = ("in_progress", "active")


def _continuous_projects() -> tuple[str, ...]:
    """Projects that run a continuous backlog with NO epic structure — CONFIG-driven.

    Drives a UI hint only: such a project's /epics surface returns an empty list,
    so the col-2 Active-Epic section + the fleet card epic strip can render
    "continuous · no epics" rather than an empty epic block. A project NOT in this
    set that also returns no epics is treated identically (no epics is no epics) —
    so the set is purely an explicit-labelling nicety, not behaviour.

    Sourced from env `KAIDERA_CONTINUOUS_PROJECTS` (comma-separated project keys) UNIONed
    over the project profiles that DECLARE `continuous: true` (the profile-sourced default —
    a dropped-in project self-labels, so a FRESH install gets the hint with no env). The
    harness hardcodes no project name; with neither env nor a continuous profile the set is
    empty. A deployment that wants the explicit hint can still set e.g.
    `KAIDERA_CONTINUOUS_PROJECTS=marketing,bpa`."""
    raw = os.environ.get("KAIDERA_CONTINUOUS_PROJECTS", "")
    keys = [p.strip() for p in raw.split(",") if p.strip()]
    try:
        for k in project_profile.continuous_projects():  # profile-sourced default
            if k not in keys:
                keys.append(k)
    except Exception:
        pass  # profile dir unavailable → env-only (never blocks)
    return tuple(keys)

# Cap on epics shown in the col-2 Active-Epic stack (newest/most-active lead).
# Keep the narrow column tidy if a project exposes a long epic list.
_COL2_EPIC_MAX = 4
# Cap on per-increment dots shown on a compact fleet-card epic strip.
_FLEET_INC_MAX = 12

# Epic statuses that mark the "live / in-flight" epic — used to pick which epic
# leads the col-2 stack + to sort the fleet strip's headline epic. Anything else
# (done/blocked/paused) sorts after these.
_ACTIVE_EPIC_STATUSES = ("build", "active", "in_progress")

# A project's pending-handoff count at/above this is "needs attention" (the
# fleet Dashboard highlights the card + sorts it to the front). kaidera runs
# ~263 pending, Kaidera OS ~27 on the live box — so this threshold reliably
# separates a busy queue from a calm one.
_FLEET_ATTENTION_PENDING = 10
# How many cosmetic glyph swatches the fleet cards cycle through for projects
# without a fixed colour. Mirrors the rail's pico tints so a project looks the
# same in the rail and on a card.
_FLEET_PICO_SWATCHES = ("mk", "en", "ev", "mk2")


def _build_orchestrator(app: FastAPI) -> Any:
    """Construct the autonomy Orchestrator from the composition-root state.

    Module-level (taking ``app``) so the boot path AND the live engine-supervisor
    build it IDENTICALLY — and so it is unit-testable without standing up the
    lifespan. The kwargs mirror the original inline boot construction exactly.
    """
    return orchestrator_mod.Orchestrator(
        cortex=app.state.cortex,
        appdb=app.state.appdb,
        harness_runner=harness_runner,
        chat_routing_for=_chat_routing_for,
        record_usage=lambda project_key, agent, model, ev: record_run_usage_appdb(
            app.state.appdb, project_key, agent, model, ev
        ),
        find_agent=_find_agent,
        resolve_target=_resolve_target_agent,
        classify_interactive=_classify_interactive,
        project_identity=_project_identity,
        agent_view=_agent_view,
        runstate=app.state.runstate,
        harness_port=app.state.harness_port,
    )


async def _engine_wanted(app: FastAPI) -> bool:
    """Should the autonomy engine be running right now?

    Run the engine if the operator pre-warmed it (``harness_autostart`` System
    setting) OR any project has autonomy ON — so flipping a single project's
    autonomous-dispatch is, by itself, enough to bring the engine up live (no
    console restart, no separate autostart flag). Module-level (taking ``app``)
    so it is unit-testable. NEVER raises: a down app-DB / unreadable settings
    file degrades to "engine off" (fail-safe — an outage can't surprise-start
    autonomy). ``app`` is accepted for symmetry/testability though the current
    checks read process-global settings + the orchestrator's serialized reader.
    """
    try:
        from app import settings as _ss
        if bool(_ss.load().get("harness_autostart")):
            return True
    except Exception:
        pass
    try:
        return bool(await orchestrator_mod._autonomous_projects_async())
    except Exception:
        return False


async def _engine_supervisor(app: FastAPI, stop_ev: "asyncio.Event") -> None:
    """Live reconciler for the autonomy engine.

    Every ~8s (or immediately on shutdown signal) it compares "is the engine
    wanted?" with "is it running?" and starts / stops the orchestrator to match
    — so a project flipping autonomy ON brings the engine up WITHOUT a console
    restart, and flipping the last one OFF (with ``harness_autostart`` off) tears
    it back down. Every tick is guarded so a transient failure never kills the
    supervisor or the console.
    """
    _log = __import__("logging").getLogger("console")
    while not stop_ev.is_set():
        try:
            want = await _engine_wanted(app)
            running = app.state.orchestrator is not None
            if want and not running:
                orch = _build_orchestrator(app)
                orch.start()
                app.state.orchestrator = orch
                _log.info(
                    "autonomy engine STARTED live (a project enabled autonomy, "
                    "or harness_autostart is on)"
                )
            elif not want and running:
                with suppress(Exception):
                    await app.state.orchestrator.stop()
                app.state.orchestrator = None
                _log.info(
                    "autonomy engine STOPPED live (no autonomous project + "
                    "harness_autostart off)"
                )
        except Exception as exc:
            _log.warning("engine supervisor tick failed: %s", exc)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_ev.wait(), timeout=8.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create one shared Cortex client for the app's lifetime, and apply the
    PROJECT-SUPPLIED one-time agent-designation seed. The harness names no worker — it is
    a pure runtime; the seed is project DATA. At launch the seed's DEFAULT SOURCE is the
    dropped-in project's PROFILE (`<project>.profile.json` in the configured profiles dir;
    loaded by `project_profile`), with the `KAIDERA_DESIGNATION_SEED` env knob as the
    OVERRIDE — so a deployment that drops in a `<project>.profile.json` with a designations
    block auto-configures with NO manual env, while the project-agnostic shipped package (no
    concrete profile, only the example.profile.json template) + no env stamps nothing. It is
    the project-agnostic default. It is idempotent + non-destructive (guarded by a marker; never
    clobbers an operator edit) — see settings.seed_agent_overrides + settings._load_designation_seed.
    Graceful-degrade: a missing/invalid profile logs + falls back to empty, never crashes boot.
    """
    # License posture (Kaidera OS) — SOFT gate: an unlicensed hosted deploy logs a prominent
    # warning (contact the configured platform administrator) but NEVER bricks the service; dev/test
    # deployments are exempt (license_required → False). Guarded so a license hiccup can't block boot.
    with suppress(Exception):
        import logging as _logging

        from app import license as license_mod

        license_mod.enforce_at_startup(_logging.getLogger("console"))
    try:
        settings_store.seed_agent_overrides()
    except OSError:
        # A read-only/locked config dir must not block app start; the Configure
        # page still falls back to the registry heuristic without the seed.
        pass
    # CHAT FILE-ATTACHMENTS (feature-gap step 6, Inc A): a best-effort startup sweep of
    # stale attachment sandbox dirs (older than 24h) — the backstop for any chat turn
    # whose per-run cleanup didn't fire (a crash / a hard stop). Never blocks boot (a
    # missing dir / any error is a clean no-op).
    with suppress(Exception):
        attachments_module.sweep_stale_attachments()
    app.state.local_run_tasks = {}
    app.state.cortex = CortexClient()
    # App-DB (E007 / DATA_SEPARATION) — the operational store for usage telemetry
    # + analytics, SEPARATE from Cortex. Created here but connected lazily; if the
    # harness-appdb container isn't up the console still runs (every appdb call
    # degrades cleanly). A best-effort warm-up ping primes `available()` for the
    # first Analytics paint without blocking startup if the DB is down.
    app.state.appdb = appdb_store.AppDB()
    # The harness carries NO in-console chat-share surface: old bespoke chat-share
    # routers and standalone portal shells are external project extensions, not core.
    try:
        await app.state.appdb.ping()
    except Exception:
        pass
    # OperationalStorePort adapter over the App-DB (Track A) — the port the carved
    # feature modules (analytics first) depend on, so they never touch the concrete
    # AppDB/SettingsDB. Stashed once at the composition root; the analytics router's
    # Depends resolves it from here (falling back to wrapping app.state.appdb if
    # absent). Construction is trivial (no I/O) but guarded so it never blocks boot.
    try:
        app.state.opstore = AppDbOperationalStore(appdb=app.state.appdb)
    except Exception:
        app.state.opstore = None

    # Autonomous orchestrator (E007 Phase 1 — the dispatch loop). Started here, but
    # SHIPS DARK: it reconciles the ON set from the app-DB project_autonomy table,
    # which seeds NO rows, so every project is OFF and the loop is a clean idle
    # no-op until an operator flips a project ON in the Dispatch view. It auto-runs
    # agents on the user's subscription, so OFF-by-default + the per-project
    # kill-switch + concurrency cap + idempotency live inside the loop. The loop
    # NEVER crashes the console (every sweep is guarded). A start failure must not
    # block app boot — the console runs fine without autonomy.
    app.state.orchestrator = None
    app.state.watchdog = None
    app.state.watchdog_stop = None
    app.state.watchdog_task = None
    app.state.catalog_refresh_task = None
    app.state.update_status_refresh_task = None
    app.state.license_refresh_task = None
    app.state.license_refresh_stop = None
    app.state.runstate_prune_task = None
    # Live engine supervisor (autonomy go-live): reconciles the orchestrator's
    # running state against "is the engine wanted?" every few seconds, so flipping
    # a project's autonomy ON brings the engine up WITHOUT a console restart (and
    # the last project OFF, with harness_autostart off, tears it back down).
    app.state.engine_supervisor_task = None
    app.state.engine_supervisor_stop = None
    # RunState SSOT store (Milestone 1) — the run-state Pg adapter over the SHARED
    # AppDB pool (no second pool). Built best-effort; a construction failure (or a
    # down app-DB) leaves it None / degraded so the orchestrator falls back to the
    # legacy spawn argv + in-memory transcript alone — autonomy is never blocked.
    app.state.runstate = None
    try:
        app.state.runstate = runstate_pg.RunStatePgStore(app.state.appdb)
        with suppress(Exception):
            abandoned = await app.state.runstate.abandon_stale_request_lived_runs(
                current_pid=os.getpid()
            )
            if abandoned:
                import logging as _logging
                _logging.getLogger("console").warning(
                    "run-state startup reconciled %d abandoned request-lived run(s)",
                    abandoned,
                )
    except Exception as exc:  # never block startup on the run-state store
        import logging as _logging
        _logging.getLogger("console").warning(
            "run-state store failed to construct (run-state SSOT degraded): %s", exc
        )
    # HarnessPort (Track B / harness-service I1) — pick the worker-spawn mechanism
    # from HARNESS_SPAWN_MODE at the composition root. Default (unset/'legacy') is
    # None → the orchestrator's existing inline subprocess.Popen path (zero behaviour
    # change); 'local' → the LocalHarnessAdapter (the same host spawn, behind the
    # port); 'remote' → the I2 remote adapter (fails closed to None until built). The
    # factory NEVER raises, so a harness-mode misconfig can't block boot.
    app.state.harness_port = orchestrator_mod._make_harness_port()
    # Autonomy engine boot. The engine comes up on boot when it is WANTED — i.e.
    # the operator pre-warmed it (`harness_autostart`, System settings) OR a project
    # already has autonomy ON. Per-project Flags still gate each project; this only
    # decides whether the engine itself runs. A degraded app-DB / unreadable settings
    # reads "not wanted" (fail-safe — an outage can never surprise-start autonomy).
    # When not wanted the engine ships DARK (app.state.orchestrator stays None — the
    # same handled state as a start failure). The live supervisor (below) then keeps
    # this in sync at runtime, so toggling autonomy no longer needs a console restart.
    try:
        _engine_should_run = await _engine_wanted(app)
    except Exception:
        _engine_should_run = False
    if not _engine_should_run:
        import logging as _logging
        _logging.getLogger("console").info(
            "autonomy engine DARK at boot (no autonomous project + harness_autostart "
            "off) — enabling a project's autonomy or the Settings → System switch "
            "starts it live, no restart needed."
        )
    else:
        try:
            orch = _build_orchestrator(app)
            orch.start()
            app.state.orchestrator = orch
        except Exception as exc:  # never block startup on the orchestrator
            import logging as _logging
            _logging.getLogger("console").warning(
                "orchestrator failed to start (console runs without autonomy): %s", exc
            )

    # Runtime watchdog — the failure-supervisor that pairs
    # with the orchestrator. Deterministic: each heartbeat it scans every autonomous
    # project's CLAIMED handoffs, re-completes silent complete-failures, and ESCALATES
    # stale / orphaned / timed-out runs to the project lead (a CONSULT handoff). It reads the
    # autonomous set via the orchestrator's OFF-loop, serialized reader so it never
    # races the orchestrator on the sync settings DB. SHIPS DARK (no autonomous
    # project → nothing to supervise) and NEVER blocks boot. project="" is a
    # placeholder — the real project is passed per-scan from the reader.
    try:
        # OBSERVATION reads the RunState SSOT store (Milestone 1 T11): real liveness
        # (the worker's heartbeat + the durable terminal status) instead of grepping
        # Cortex memory text. Project-scoped Cortex API ops are the FALLBACK when the
        # store is None/down and also own escalation/completion; this remains valid for
        # every registered project without crossing a workspace CLI isolation guard.
        wd_cortex_ops = watchdog_mod.CortexWatchdogOps(
            project="", client=app.state.cortex
        )
        wd_ops = watchdog_mod.StoreWatchdogOps(
            app.state.runstate, wd_cortex_ops, project=""
        )
        wd = watchdog_mod.Watchdog(wd_ops)
        wd_stop = asyncio.Event()
        app.state.watchdog = wd
        app.state.watchdog_stop = wd_stop
        app.state.watchdog_task = asyncio.create_task(
            wd.run_forever(orchestrator_mod._autonomous_projects_async, stop=wd_stop),
            name="pm-watchdog",
        )
    except Exception as exc:  # never block startup on the watchdog
        import logging as _logging
        _logging.getLogger("console").warning(
            "watchdog failed to start (console runs without supervision): %s", exc
        )

    # Live engine supervisor — the autonomy go-live piece. Reconciles the
    # orchestrator's running state against `_engine_wanted` every ~8s so flipping
    # a project's autonomous-dispatch ON brings the engine up immediately (no
    # console restart), and flipping the last one OFF (with harness_autostart off)
    # tears it back down. Backgrounded + fully guarded — never blocks/crashes boot.
    try:
        app.state.engine_supervisor_stop = asyncio.Event()
        app.state.engine_supervisor_task = asyncio.create_task(
            _engine_supervisor(app, app.state.engine_supervisor_stop),
            name="engine-supervisor",
        )
    except Exception as exc:  # never block startup on the engine supervisor
        __import__("logging").getLogger("console").warning(
            "engine supervisor failed to start: %s", exc
        )

    # Periodic catalog refresh: force-rebuild the model/price catalog so new
    # models + price changes land without waiting for someone to open the picker (the
    # 15-min cache TTL only refreshes on access). Backgrounded — never blocks startup.
    try:
        import logging as _logging
        app.state.catalog_refresh_task = asyncio.create_task(
            providers_catalog.refresh_catalog_forever(
                log=_logging.getLogger("console")
            ),
            name="catalog-periodic-refresh",
        )
    except Exception as exc:  # never block startup on the catalog refresher
        import logging as _logging
        _logging.getLogger("console").warning(
            "catalog daily refresh failed to start: %s", exc
        )

    # Periodic run-state prune: trim run_state / run_span back to RUN_MAX_RUNS newest
    # rows per project on an interval so a long-running console never accumulates
    # unbounded run history (prune_old was previously dead code — the tables grew
    # forever). Backgrounded + best-effort — never blocks startup, no-ops when the
    # run-state store is None (app-DB down / store failed to construct).
    try:
        import logging as _logging
        app.state.runstate_prune_task = asyncio.create_task(
            runstate_pg.prune_runstate_forever(
                getattr(app.state, "runstate", None),
                log=_logging.getLogger("console"),
            ),
            name="runstate-prune",
        )
    except Exception as exc:  # never block startup on the prune sweeper
        import logging as _logging
        _logging.getLogger("console").warning(
            "run-state prune sweeper failed to start: %s", exc
        )

    # Prime the PI model catalog in the background so the FIRST AI-Worker config popup
    # shows the live PI list, not the fixed fallback. `pi --list-models` is a ~6-8s node
    # cold-start, so a read NEVER blocks on it (pi_catalog is stale-while-revalidate);
    # this just triggers the one background fetch at boot to warm the cache.
    try:
        from app import pi_catalog as _pi_catalog
        app.state.pi_catalog_prime_task = asyncio.create_task(
            _pi_catalog.list_pi_model_groups(), name="pi-catalog-prime"
        )
    except Exception as exc:  # never block startup on the pi catalog prime
        import logging as _logging
        _logging.getLogger("console").warning(
            "pi catalog prime failed to start: %s", exc
        )

    # Claude and Codex model/effort catalogs are host-CLI capabilities. Prime their
    # short-lived caches so both the SPA and legacy Configure surfaces render the
    # installed harnesses rather than release-time fallback lists on first open.
    try:
        app.state.claude_catalog_prime_task = asyncio.create_task(
            claude_catalog.list_claude_model_options(), name="claude-catalog-prime"
        )
        app.state.codex_catalog_prime_task = asyncio.create_task(
            codex_catalog.list_codex_model_options(), name="codex-catalog-prime"
        )
    except Exception as exc:  # never block startup on subscription catalog discovery
        import logging as _logging
        _logging.getLogger("console").warning(
            "subscription catalog prime failed to start: %s", exc
        )

    # Signed-release update status is an advisory UI badge. Warm it in the
    # background so app startup and first paint never wait on GitHub/gh.
    try:
        app.state.update_status_refresh_task = asyncio.create_task(
            _refresh_update_status_cache(),
            name="update-status-refresh",
        )
    except Exception as exc:  # never block startup on the update badge
        import logging as _logging
        _logging.getLogger("console").warning("update status warm-up failed: %s", exc)

    # Online license heartbeat. Advisory only: keeps online grants fresh and picks up
    # revocation/latest-release hints, but never blocks startup or gates the current
    # local grant/free tier if the Kaidera AI platform is unavailable.
    try:
        import logging as _logging
        from app import license_refresh

        license_svc = settings_module.SettingsService(store=app.state.opstore)
        app.state.license_refresh_stop = asyncio.Event()
        app.state.license_refresh_task = asyncio.create_task(
            license_refresh.heartbeat_forever(
                load_settings=license_svc.load_app_settings,
                save_settings=license_svc.upsert_app_settings,
                stop=app.state.license_refresh_stop,
                log=_logging.getLogger("console"),
            ),
            name="license-heartbeat",
        )
    except Exception as exc:  # never block startup on license refresh
        import logging as _logging
        _logging.getLogger("console").warning(
            "license heartbeat failed to start: %s", exc
        )

    try:
        yield
    finally:
        # Stop the live engine supervisor first so it can't re-start the
        # orchestrator we are about to tear down (signal + cancel).
        sup_stop = getattr(app.state, "engine_supervisor_stop", None)
        if sup_stop is not None:
            with suppress(Exception):
                sup_stop.set()
        sup_task = getattr(app.state, "engine_supervisor_task", None)
        if sup_task is not None:
            sup_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await sup_task
        # Stop the orchestrator first so no run is spawned during teardown.
        orch = getattr(app.state, "orchestrator", None)
        if orch is not None:
            try:
                await orch.stop()
            except Exception:
                pass
        # Stop the PM watchdog (signal + cancel) before closing the Cortex client
        # it uses for scans/escalations.
        wd_stop = getattr(app.state, "watchdog_stop", None)
        if wd_stop is not None:
            wd_stop.set()
        wd_task = getattr(app.state, "watchdog_task", None)
        if wd_task is not None:
            wd_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await wd_task
        # Stop the daily catalog refresher.
        cat_task = getattr(app.state, "catalog_refresh_task", None)
        if cat_task is not None:
            cat_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await cat_task
        # Stop the periodic run-state prune sweeper.
        prune_task = getattr(app.state, "runstate_prune_task", None)
        if prune_task is not None:
            prune_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await prune_task
        # Stop the advisory update-status warm-up if shutdown wins the race.
        update_task = getattr(app.state, "update_status_refresh_task", None)
        if update_task is not None:
            update_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await update_task
        # Stop the advisory license heartbeat before closing the app-DB settings port.
        lic_stop = getattr(app.state, "license_refresh_stop", None)
        if lic_stop is not None:
            with suppress(Exception):
                lic_stop.set()
        lic_task = getattr(app.state, "license_refresh_task", None)
        if lic_task is not None:
            lic_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await lic_task
        # Stop detached in-process chat / Approve & Run tasks before closing the
        # shared stores they use to mark terminal cancellation.
        with suppress(Exception):
            await local_run_tasks.shutdown_local_run_tasks(app.state)
        await app.state.cortex.aclose()
        await app.state.appdb.aclose()
        # Close the sync settings-store connection too (app-DB settings backend).
        try:
            appdb_store.settings_db.close()
        except Exception:
            pass


class _SpaStaticFiles(StaticFiles):
    """``StaticFiles`` with an SPA deep-link fallback: a sub-path under the mount
    that maps to no real file serves ``index.html`` (200) instead of 404, so a
    refresh / direct-load of a client-side route under ``/app`` is not a 404.

    The bundled SPA today uses HASH routing (``/app/#/<project>/<agent>`` — the hash
    never reaches the server, so ``/app/`` alone already restores any view), so this
    fallback is belt-and-braces; it keeps ``/app`` forgiving and future-proofs a
    later switch to path-based routing. It only ever affects URLs UNDER ``/app`` (the
    mount path) — the module JSON APIs live at the root (``/agents`` · ``/runs`` · …)
    and are never shadowed. A genuinely missing asset (a hashed bundle file that
    isn't there) still returns index.html with a 200; that is the standard SPA trade
    (the bundle is content-hashed, so a missing asset means a stale client, not a
    server bug)."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        from starlette.exceptions import HTTPException as _StarletteHTTPException

        served_index = path in ("", "index.html")
        try:
            resp = await super().get_response(path, scope)
        except _StarletteHTTPException as exc:
            if exc.status_code == 404:
                # Fall back to the SPA entry so client-side routes resolve.
                resp = await super().get_response("index.html", scope)
                served_index = True
            else:
                raise
        # index.html points at the CURRENT content-hashed bundle, so it MUST revalidate
        # every load — without this, browsers heuristically cache a stale index.html and
        # never pick up a new deploy's bundle (the "I see the old UI after an update" bug).
        # Catch every HTML response (the `/app/` directory entry AND `/app/index.html`);
        # the hashed /assets/* files are immutable and keep StaticFiles' default caching.
        is_html = resp.headers.get("content-type", "").startswith("text/html")
        if served_index or is_html or path.endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


def mount_spa(app: FastAPI, dist_dir: Path) -> bool:
    """ADDITIVELY serve the refined SPA (Track C) at ``/app`` from ``dist_dir``.

    Mounts the SPA bundle at ``/app`` so ``/app`` → ``dist/index.html``,
    ``/app/assets/*`` → the hashed bundle assets, and any deep link under ``/app``
    that maps to no file falls back to ``index.html`` (the ``_SpaStaticFiles``
    fallback) — so the SPA's client-side routes survive a refresh. Served
    same-origin with the module JSON APIs (``/agents`` · ``/runs`` · ``/dispatch`` ·
    ``/analytics`` · ``/settings`` · ``/projects`` · ``/runstate/stream``), so no
    CORS/proxy is needed when reached from the console's own port. The legacy HTML
    routes (incl. ``/``) are untouched — this is purely additive; flipping the
    default is a later step.

    MISSING-DIST GUARD: the SPA is built into the image at deploy time (the
    Dockerfile's node stage), but the app must still BOOT without it — a plain
    ``uvicorn app.main:app`` from a checkout where ``spa/dist`` was never built must
    not crash at import. So if ``dist_dir`` (or its ``index.html``) is absent we LOG
    and SKIP the mount, returning ``False`` (``/app`` is then simply 404 — the honest
    "not built" state — never a 500). Returns ``True`` when the mount was added.
    """
    index_html = dist_dir / "index.html"
    if not index_html.is_file():
        import logging as _logging

        _logging.getLogger("console").warning(
            "SPA bundle not found at %s — skipping the /app mount (the console boots "
            "without the SPA; build it via the Dockerfile node stage or "
            "`npm --prefix spa run build`). The legacy HTML console at / is unaffected.",
            dist_dir,
        )
        return False
    # html=True → a directory request serves index.html; the subclass adds the
    # unmatched-sub-path → index.html fallback. The index guard above already proved
    # the dir is a real, built bundle.
    app.mount(
        "/app", _SpaStaticFiles(directory=str(dist_dir), html=True), name="spa"
    )
    return True


app = FastAPI(
    title="Kaidera OS",
    description="Authenticated Kaidera OS control plane over Cortex.",
    version=__version__,
    lifespan=lifespan,
)


@app.middleware("http")
async def first_party_auth_gate(request: Request, call_next):
    """Optional first-party console auth gate.

    Dev mode stays open by default. Hosted/redistributable deployments turn this on
    with KAIDERA_AUTH_ENABLED=1 (or any non-dev KAIDERA_DEPLOY_MODE).
    """
    if not auth_module.auth_enabled() or auth_module.is_public_path(request.url.path):
        return await call_next(request)
    if await auth_module.current_user_from_request(request):
        return await call_next(request)
    if auth_module.wants_html(request):
        target = auth_module.safe_next(
            request.url.path + (f"?{request.url.query}" if request.url.query else "")
        )
        return RedirectResponse(
            f"/auth/login?next={quote(target, safe='/')}",
            status_code=303,
        )
    return JSONResponse({"detail": "authentication_required"}, status_code=401)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Serve the refined SPA (Track C) at /app from the built `spa/dist` bundle — so the
# operator uses the SPA at http://127.0.0.1:8765/app, same-origin with the module
# APIs. ADDITIVE (the legacy HTML console at / is untouched) and GUARDED (a missing
# `spa/dist` logs + skips the mount, so the app still boots un-built). See mount_spa.
mount_spa(app, SPA_DIST_DIR)

# Feature-module routers (Track A carve) — mounted ADDITIVELY so the existing
# routes keep working. The analytics module owns the usage/cost logic behind the
# OperationalStorePort; its `GET /analytics/{project}/usage` is the clean JSON
# surface, and the existing HTML Analytics view delegates to the SAME service
# (`_analytics_usage` below) — one source of the logic, two surfaces.
app.include_router(analytics_module.router)
app.include_router(automation_module.router)
# The agents module owns the roster CATALOG (list + detail) behind the
# OperationalStorePort; its `GET /agents/{project}` + `GET /agents/{project}/{agent}
# /detail` are the clean JSON surfaces, and the existing HTML agents column +
# agent-detail pane delegate their catalog substance to the SAME service (the
# module-level `_agents_service` below; `_agent_view` / `_group_agents` /
# `_orchestrator_label` / `_lead_agent_name` now route through it) — one source of
# the catalog logic. STRICTLY ADDITIVE: the JSON detail uses the distinct
# three-segment `/detail` leaf, so it can NOT shadow the existing two-segment HTML
# `GET /agents/{p}/{a}` pane (registered later but same path shape); the
# one-segment list path has no existing counterpart. The HTML panes keep working.
app.include_router(agents_module.router)
app.include_router(auth_module.router)
_register_console_extension_hooks(app)


@app.get("/whoami")
async def whoami(request: Request) -> dict:
    """The signed-in user for the SPA profile menu.

    Prefer the first-party Kaidera AI session. Generic upstream identity headers are
    accepted for deployments that put another trusted proxy in front of the console.
    Local development mode with auth disabled falls back to the historical generic admin so
    existing dev sessions do not break.
    """
    user = await auth_module.current_user_from_request(request)
    if user:
        return auth_module.user_payload(user)
    h = request.headers
    if auth_module.trusted_proxy_headers(request):
        name = (
            h.get("X-Kaidera-Name")
            or h.get("X-Forwarded-Preferred-Username")
            or h.get("X-Forwarded-User")
            or h.get("X-Forwarded-Email")
            or ""
        ).strip()
        email = (h.get("X-Kaidera-Email") or h.get("X-Forwarded-Email") or "").strip()
        groups = h.get("X-Kaidera-Groups") or h.get("X-Forwarded-Groups") or ""
        if name or email or groups.strip():
            is_admin = "admin" in {g.strip().lower() for g in groups.split(",")}
            return {
                "authenticated": True,
                "name": name or email or "User",
                "email": email,
                "is_admin": is_admin,
                "role": "admin" if is_admin else "user",
            }
    return {
        "authenticated": not auth_module.auth_enabled(),
        "name": "User",
        "email": "",
        "is_admin": not auth_module.auth_enabled(),
        "role": "admin" if not auth_module.auth_enabled() else "user",
    }


# The console exposes NO in-console single-agent chat-share router: the bespoke `/wren` stack
# was deleted in v0.1.113 and the standalone `local-cortex/portal/` chat shell in v0.1.119.
# The settings module owns the OPERATIONAL settings logic (app/system settings,
# per-agent config get/resolve/save, designation normalise + seed, project autonomy/
# propose-mode flags) behind the OperationalStorePort; its `GET /settings/{project}/
# app` + `/settings/{project}/agents/{agent}/config` + `/settings/{project}/flags`
# are the clean JSON surfaces, and the existing HTML System page + Configure card +
# the inline agent-config save delegate their config substance to the SAME service
# (the module-level `_settings_service` below) — one source of the config logic.
# STRICTLY ADDITIVE: the JSON routes use a distinct two-plus-segment
# `/settings/{project}/...` shape (all GET), so they can NOT shadow the existing
# one-segment HTML `GET /settings/{page}` tab route NOR the live `POST /agents/{p}/
# {a}/config` + `/chat` routes (different root + method); and the router mounts
# BEFORE those HTML routes. The HTML surfaces keep working unchanged.
app.include_router(settings_module.router)
# The dispatch module owns the BOARD/READ side of the Dispatch center (the open-
# handoff queue + rule-based proposals + the board counts + the autonomy/propose-mode
# flag reads) behind the CortexMemoryPort (the queue) + the OperationalStorePort (the
# flags). Its `GET /dispatch/{project}/board` is the clean JSON surface, and the
# existing HTML Dispatch center delegates its board substance to the SAME service (the
# module-level `_dispatch_service` below; `_dispatch_rows` now routes through it) —
# one source of the board logic. STRICTLY ADDITIVE: the JSON board uses a distinct
# `GET /dispatch/{project}/board` shape — a `/board` leaf AND a GET — so it can NOT
# shadow the live `POST /dispatch/{p}/autonomous` + `POST /dispatch/{p}/run` routes
# (registered later, both POST) nor the `GET /stream` SSE proxy (different prefix). The
# orchestrator's spawn/run imperative core (the toggle/Approve&Run/approve POSTs +
# the live status/feed/wave assembly) stays in main.py — this is the read carve only.
app.include_router(dispatch_module.router)
# The runs module owns the run-state READ side (the agent-detail LIVE-WORK TRANSCRIPT
# view-model — the recent-run rail + the selected-run hydrated body — plus the run
# board: active + recent runs, and single-run reads by id / by handoff) behind the
# RunStatePort (the run-state SSOT the worker writes). Its `GET /runs/{project}` (+
# `/runs/{project}/by-handoff/{handoff_id}` + `/runs/run/{run_id}`) are the clean JSON
# surfaces, and the existing HTML agent-detail run rail + the SSE first-paint delegate
# their run-read substance to the SAME service (the module-level `_runs_service` below;
# `_store_run_row` / `_store_transcript_view` / `_agent_runs_view_store` now route
# through it) — one source of the run-read logic. STRICTLY ADDITIVE: the JSON routes
# use the distinct `/runs/...` root (all GET reads), so they can NOT shadow the live
# SSE writer route `GET /runstate/stream` (a `/runstate` prefix) nor the dispatch/agents
# roots. The SSE WRITER side + the orchestrator's spawn/run imperative core stay in
# main.py — this is the run-READ carve only (Track A's LAST feature module).
app.include_router(runs_module.router)

# The explain module fronts the Explain capability (an LLM-generated visual code
# explainer persisted as a Cortex L5 artifact). `POST /explain/{project}` mints a run_id,
# opens a run_state row (lease_owner='explain'), and forwards the spawn to the HOST
# harness-service `/explain` (the container can't read the repo / run cortex-graph — the
# host can), EXACTLY like the chat remote path; `GET /explain/{project}/result/{run_id}`
# + `GET /explain/{project}/list` read the persisted artifact(s) via the CortexClient.
# STRICTLY ADDITIVE: the distinct `/explain/...` root (all reads + one start POST) can't
# shadow the `/runs/...`, `/runstate/...`, `/dispatch/...`, or `/agents/...` roots. The
# generation runs HOST-side (app/explain_run.py via scripts/run-explain); the persisted
# HTML is rendered SANDBOXED by the SPA (an isolated iframe), never inline.
app.include_router(explain_module.router)
# Visual Plan: read surface over `docs/plans/**/*.mdx` (authored with the visual-plan
# skill). The SPA renders the MDX natively (MdxPlanRenderer), repo-root scoped + guarded.
app.include_router(plan_module.router)

# The graph module fronts the knowledge/code-graph view: a clean read-only JSON surface
# that shapes Cortex's dual-level `/cortex-graph-search` (L4 entities → nodes coloured by
# kind; relationships → edges) + `/graph/stats` (own/total/repo counts) into a cytoscape-
# AGNOSTIC `{nodes, edges, stats}` for the SPA `GraphView` canvas. `GET /graph/{project}` is
# the seed view; `GET /graph/{project}/search?q=&limit=` re-centres on a term. BOUNDED at
# ~140 nodes (the search hits + their 1-hop neighbours — never the whole ~5,868-node graph)
# and graceful-degrades to an empty graph on a down/empty Cortex (never a 500). STRICTLY
# ADDITIVE + READ-ONLY: the distinct `/graph/...` root can't shadow `/runs/...`,
# `/runstate/...`, `/dispatch/...`, `/agents/...`, or `/explain/...`.
app.include_router(graph_module.router)

# The history module fronts the cross-agent activity-timeline view: a clean read-only JSON
# surface that shapes Cortex's noisy `/history` stream into a readable `events` timeline (each
# row run through the PORTED summariser — `main._summarize_history_row` — so it's a clean line,
# never raw tool-call JSON), folds a recent-`decisions` feed (from `/search`) + a roster
# `agent_count`. `GET /history/{project}?limit=N` → `{events, decisions, agent_count}`. The
# three Cortex reads run concurrently + each graceful-degrades, so a down section blanks alone
# and the route NEVER 500s. STRICTLY ADDITIVE + READ-ONLY: the distinct `/history/...` root
# can't shadow `/runs/...`, `/runstate/...`, `/dispatch/...`, `/agents/...`, `/explain/...`,
# `/analytics/...`, `/graph/...`, or `/settings/...`.
app.include_router(history_module.router)

# The registration module fronts the in-console registration UX backend (feature-gap #81):
# three additive WRITE routes wrapping the CortexClient registration writes — add an agent
# (`POST /agents/{project}/register` → create_agent, writer-gated), deregister an agent
# (`POST /agents/{project}/{agent}/deregister` → remove_agent, admin-gated), and add a
# project (`POST /projects/register` → create_project, admin-gated). Each graceful-degrades
# to a friendly, NON-LEAKY `{ok, error}` (never a 500; the admin token is never echoed),
# mirroring the workspace editor's friendly-error pattern. The distinct trailing-literal
# shapes (`/register`, `/deregister`) can't shadow the live `GET /agents/{project}` /
# `POST /agents/{p}/{a}/config` / `GET /projects` routes (different trailing literal/method).
app.include_router(registration_module.router)

# The skills module fronts the SPA Skills tab: browse the Cortex skills catalogue, install a
# skill from a GitHub URL, and bind a skill to an agent/role. `GET /skills/{project}` proxies
# CortexClient.get_skills (the global + project catalogue); `POST /skills/{project}/install`
# shells out to the `cortex-skill install` CLI (clone + SKILL.md parse + register live in that
# one tool — the console reuses it, like the watchdog reuses the cortex-* CLIs) then returns the
# refreshed list; `POST /skills/{project}/{slug}/bind` proxies CortexClient.bind_skill. Each route
# graceful-degrades to a friendly `{ok, error, ...}` (never a 500; the read degrades to an empty
# catalogue). STRICTLY ADDITIVE: no existing surface owns the `/skills/...` root.
app.include_router(skills_module.router)


def _cortex(request: Request) -> CortexClient:
    return request.app.state.cortex


def _appdb(request: Request) -> appdb_store.AppDB:
    return request.app.state.appdb


def _orchestrator(request: Request):
    """The app's autonomous orchestrator (or None if it failed to start). The
    Dispatch view + toggle route read it for live status + the activity feed."""
    return getattr(request.app.state, "orchestrator", None)


def _runstate(request: Request):
    """The RunState SSOT store (the `RunStatePgStore` over the shared app-DB pool)
    — or None if it failed to construct / the app-DB is down. The crew/agent
    read paths read live run state from THIS (the durable single source of truth
    the worker writes), replacing the in-memory transcript store as the read model
    (Milestone 1 T7). Every store read graceful-degrades, so a None / down store
    never blanks a pane — the view falls back to the empty state."""
    return getattr(request.app.state, "runstate", None)


# ---------------------------------------------------------------------------
#  Cross-project attention summaries (rail column 1)
# ---------------------------------------------------------------------------

def _attention_from_states(states: dict[str, dict | None]) -> dict[str, dict]:
    """Derive the rail's per-project attention map from a pre-fetched states map.

    `states` maps project_key → /state['summary'] (or None when unreachable).
    Returns {project_key: {"pending": int|None, "tasks": int|None}}. Pure (no
    I/O) so the full page can fetch every project's /state ONCE (via
    _fleet_states) and feed both the rail attention line AND the fleet cards from
    the same data instead of fetching /state twice."""
    out: dict[str, dict] = {}
    for key, summary in states.items():
        summary = summary or {}
        out[key] = {
            "pending": summary.get("pending_handoffs"),
            "tasks": summary.get("active_tasks"),
        }
    return out


async def _attention_summaries(
    cortex: CortexClient, projects: list[dict]
) -> dict[str, dict]:
    """For every active project, pull its /state summary so the rail can show a
    'needs attention' line (pending handoffs + active tasks). Fetched
    concurrently. Returns {project_key: {"pending": int|None, "tasks": int|None}}.

    Degrades gracefully: a project whose /state errors out yields None counts
    (rendered as '—'), never an exception. Thin wrapper over _fleet_states +
    _attention_from_states (the single /state-fetch path)."""
    states = await _fleet_states(cortex, projects)
    return _attention_from_states(states)


def _pick_selected(projects: list[dict], requested: str | None) -> str | None:
    """Resolve which project is selected. Prefer the requested key if it is an
    active project; otherwise the CONFIGURED default project (Settings/env, via
    `_default_project()`) when that is a real active project; otherwise the FIRST
    active project (the live-list fallback — so the shell is useful on first paint
    even with no configured default). No project name is hardcoded (§2.7)."""
    if not projects:
        return None
    keys = {p.get("project_key") for p in projects}
    if requested and requested in keys:
        return requested
    default = _default_project()
    if default and default in keys:
        return default
    return projects[0].get("project_key")


# ---------------------------------------------------------------------------
#  Agent grouping (agents column 2) — DELEGATES to app.agents (Track A, 2nd carve)
# ---------------------------------------------------------------------------
# The classification primitives (the registry interactive heuristic, the lead-tag
# rule, the synthetic-name nudge, and the override-first classifier) were lifted
# 1:1 into `app.agents.service` behind the OperationalStorePort. The shims below
# preserve main.py's existing signatures (every call site is unchanged) but route
# through the ONE module, so the HTML column/pane and the JSON `GET /agents/...`
# endpoints share a single source of the catalog logic. (The hint/mark CONSTANTS +
# the DESIGNATION_* comparison now live in app.agents.service.)


def _has_cpo_tag(role: str) -> bool:
    """[delegates to app.agents] True if a role reads as the primary lead; co-lead
    variants are excluded."""
    return agents_module.service.has_cpo_tag(role)


def _registry_interactive(agent: dict) -> bool:
    """[delegates to app.agents] The registry-derived interactive heuristic (the
    fallback when no console designation override is set); a synthetic/polluted
    name is never pulled Interactive by the heuristic."""
    return agents_module.service.registry_interactive(agent)


def _classify_interactive(agent: dict, designation: str = "") -> bool:
    """[delegates to app.agents] Classify Interactive vs Autonomous — OVERRIDE-FIRST
    (a console designation override wins; else the registry heuristic)."""
    return agents_module.service.classify_interactive(agent, designation)


def _is_test_name(name: str | None) -> bool:
    """[delegates to app.agents] True for a polluted/synthetic agent name."""
    return agents_module.service.is_test_name(name)


# Flat {model_value: pretty_label} map across every harness's model list, built
# once from harness.HARNESS_MODELS. Lets the agent card show the SAME human model
# label the detail-panel dropdown shows (e.g. "opus" → "Opus 4.8") instead of the
# raw slug. An unknown/custom model id falls through to the raw value.
_MODEL_LABELS: dict[str, str] = {
    m["value"]: m["label"]
    for models in harness_cfg.HARNESS_MODELS.values()
    for m in models
}


def _model_label(model: str | None) -> str | None:
    """Human label for a model id (override-resolved), else the raw value."""
    if not model:
        return None
    return _MODEL_LABELS.get(model, model)


# ---------------------------------------------------------------------------
#  Agents CATALOG — delegates to the carved `app.agents` module (Track A, 2nd).
# ---------------------------------------------------------------------------
# The roster catalog/classification logic (the col-2 grouping + the agent-detail
# header resolution) was lifted 1:1 into `app.agents.service.AgentsService` behind
# the OperationalStorePort. The shims below preserve main.py's existing function
# signatures (every call site is unchanged) but route through the ONE service, so
# the HTML column/pane and the JSON `GET /agents/...` endpoints share one source of
# the catalog logic. The service is constructed with main's harness-backed config
# resolver + config-view shaper (so the card labels match the runner + the detail
# panel exactly — the same values main computed inline before the carve).


def _agents_resolve_config(agent: dict, override: dict) -> dict:
    """The per-agent config resolver injected into the AgentsService: RESOLVED
    harness/model (override-first → registry → default) + their human labels, so
    the card matches the runner + the detail panel — never the bare registry value.
    Lifts main's prior inline `_agent_view` resolution block 1:1."""
    caps = agent.get("capabilities") or {}
    reg = harness_cfg._registry_config(agent)
    harness = harness_cfg.canonical_harness(
        override.get("harness") or reg["harness"]
    ) or _DEFAULT_HARNESS
    model = (override.get("model") or reg["model"] or "").strip() or None
    # LICENSE backstop: show kaidera (not a locked harness) when the override names one
    # the license doesn't grant — keeps the card consistent with what the runner spawns.
    try:
        from app import license as _license_mod
        if not _license_mod.entitlements().has_harness(harness):
            harness, model = "kaidera", None
    except Exception:
        pass
    # VALIDITY (feature #99): coerce an impossible stored model to the harness
    # default so the CARD displays the SAME runnable model the runner uses — never an
    # impossible pair (same coercion as _chat_routing_for).
    if model is not None:
        model = harness_cfg.coerce_model(harness, model)
    # Fill the default model when none is set: the default claude-code lane → its fixed
    # default; kaidera → the out-of-the-box Fireworks kimi default (so the seeded
    # onboarding Lead is runnable with zero config). Same logic as _chat_routing_for.
    if model is None:
        if harness == _DEFAULT_HARNESS:
            model = _DEFAULT_MODEL
        elif harness == "kaidera":  # fitness:allow-literal canonical harness id (own-harness runtime), not a per-project literal
            model = harness_cfg.harness_default_model("kaidera")  # fitness:allow-literal canonical harness id arg
    return {
        "harness": harness,
        "harness_label": harness_cfg.harness_label(harness),
        "model": model,
        "model_label": _model_label(model),
        "thinking": caps.get("thinking"),
    }


# The ONE catalog service — wired with main's harness-backed resolvers. No store is
# bound here: the shims read the override map via `settings_store` (the existing
# app-DB-backed settings facade the HTML surface has always used) and pass it in
# to the pure shaping helpers, so behaviour is byte-for-byte preserved. The JSON
# router builds its OWN store-backed AgentsService (over the OperationalStorePort)
# per request.
_agents_service = agents_module.AgentsService(
    resolve_config=_agents_resolve_config,
    config_view=harness_cfg.agent_config_view,
)

# The ONE operational-settings service (Track A, the settings carve) — the single
# source of the config LOGIC (per-agent override resolve/save, designation
# normalise + seed, project autonomy/propose-mode flags). No store is bound here:
# the HTML config shims read/resolve via the existing app-DB-backed
# `settings_store` path (with JSON seed/fallback only when the DB is unavailable),
# and the JSON router builds its OWN store-backed SettingsService (over the
# OperationalStorePort) per request. The HTML save path still calls
# `settings_store.save_agent_override` so the bytes land in the same canonical
# app-DB store.
_settings_service = settings_module.SettingsService()

# The ONE dispatch BOARD service (Track A, the dispatch carve) — the single source of
# the board LOGIC (open-handoff listing + rule-based proposals + the board counts).
# No ports are bound here: the HTML board shim (`_dispatch_rows`) calls the pure
# `dispatch_rows(...)` shaping directly with handoffs/agents main already fetched
# concurrently in `_dispatch_context`, so behaviour is byte-for-byte preserved (the
# flags + awaiting-approval + orchestrator/wave/activity assembly stay in
# `_dispatch_context`). The proposal's harness/model resolve via main's injected
# `_agents_resolve_config` (the same override-first → registry → default values main
# computed inline before the carve) over the existing app-DB-backed
# `settings_store` override read. The JSON router builds its OWN port-backed
# DispatchService (over the CortexMemoryPort + OperationalStorePort) per request.
_dispatch_service = dispatch_module.DispatchService(
    resolve_config=_agents_resolve_config,
    get_override=settings_store.get_agent_override,
    # ROLE-ALIAS signals — so the Dispatch view's proposal routes a `cpo`/lead
    # to_role to the project's INTERACTIVE lead (designation-driven), exactly like
    # the orchestrator's `_resolve_target_agent`. The proposed agent + the dispatched
    # agent share ONE source (app.domain.roles), so they can never diverge.
    designation_of=settings_store.get_agent_designation,
    classify_interactive=agents_module.service.classify_interactive,
    # Role ALIAS reader — so the Dispatch view also resolves secondary roles like
    # `creative-multimedia` to the agent whose override or capability lists them,
    # matching the orchestrator's resolver.
    role_aliases_of=lambda project, agent: settings_store.get_agent_override(project, agent).get("role_aliases", ""),
)


def _agent_view(
    agent: dict, designation: str = "", role_override: str = "", override: dict | None = None
) -> dict:
    """Flatten a runtime/roster agent into the fields the agents column needs.

    DELEGATES to `app.agents.AgentsService.agent_view` (Track A): the shaping +
    classification now live in the carved module behind the port, and this wrapper
    preserves main.py's signature so every call site is unchanged. The card's
    harness/model are resolved via main's injected `_agents_resolve_config` (the
    same override-first → registry → default values main computed inline before the
    carve), so the row reads the RESOLVED config, never the bare registry value."""
    return _agents_service.agent_view(
        agent, designation=designation, role_override=role_override, override=override
    )


def _group_agents(
    agents: list[dict], project_key: str | None = None
) -> dict[str, list[dict]]:
    """Split agents into Interactive (Lead) and Autonomous groups, each as a list
    of flattened agent-view dicts. Sorted by display name within each group.

    DELEGATES to `app.agents.AgentsService.group_agents` (Track A) — the single
    source of the grouping. Classification is OVERRIDE-FIRST: each agent's console
    `designation` override (keyed "{project}:{agent}") wins; absent that, the
    registry heuristic decides. The override map is loaded ONCE here (the existing
    app-DB-backed `settings_store` path) and threaded into the service."""
    overrides = settings_store.load_agent_overrides()
    return _agents_service.group_agents(agents, project_key, overrides)


def _orchestrator_label(agents: list[dict], project_key: str | None = None) -> str | None:
    """The display name of THIS project's orchestrator-role agent, resolved from the
    roster (the deterministic dispatcher's configured display name), or None when the
    project has no orchestrator. Project-agnostic: NO worker name is baked here — the
    label comes from project roles/config, never a source literal (cortex.md).

    DELEGATES to `app.agents.AgentsService.orchestrator_label` (Track A). The
    Autonomous-group header reads "triggered by <orchestrator>"; resolved
    dynamically from the roster (an agent whose EFFECTIVE role, override-first, is
    `orchestrator`). None → the template drops the attribution gracefully."""
    overrides = settings_store.load_agent_overrides()
    return _agents_service.orchestrator_label(agents, project_key, overrides)


# ---------------------------------------------------------------------------
#  Col-2 Metrics + Active Epic derivations
# ---------------------------------------------------------------------------

def _pending_tasks(tasks: list[dict]) -> int:
    """Count /board tasks that are NOT live (status not in in_progress/active).
    /state exposes active_tasks but no pending counter, so the col-2 Metrics
    block derives 'Pending tasks' here from the board rows."""
    return sum(
        1 for t in tasks if (t.get("status") or "") not in _ACTIVE_TASK_STATUSES
    )


def _metrics_view(state: dict, tasks: list[dict]) -> dict:
    """Build the compact col-2 Metrics block for the selected project.

    Active tasks · Pending handoffs · Events/24h come straight off
    state.summary; Pending tasks is derived from the board (see _pending_tasks).
    A None counter renders as '—' in the template (graceful degrade)."""
    summary = state.get("summary", {}) if isinstance(state, dict) else {}
    return {
        "active_tasks": summary.get("active_tasks"),
        "pending_tasks": _pending_tasks(tasks),
        "pending_handoffs": summary.get("pending_handoffs"),
        "events_24h": summary.get("events_24h"),
    }


# Map an increment status onto the 3 visual families the templates style
# (.done / .prog / .todo): done = filled green, prog = teal in-flight, todo =
# empty track. Any unknown status falls back to 'todo' (empty) so a new status
# never fabricates a filled bar.
def _inc_status_kind(status: str) -> str:
    """Bucket an increment status into done / prog / todo for the dot/bar style."""
    s = (status or "").lower()
    if s == "done":
        return "done"
    if s in ("in_progress", "active", "build"):
        return "prog"
    return "todo"


def _clamp_pct(pct: object) -> int:
    """Coerce a raw pct value to a clamped 0–100 int (None/garbage → 0)."""
    try:
        n = int(pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def _shape_epic(epic: dict) -> dict:
    """Flatten one /epics row into the fields the templates render directly.

    Pulls epic_id/title/status/overall_pct + a shaped increments list (each with
    a num label, clamped pct, raw status, and the done/prog/todo style kind).
    `is_active` flags a build/active/in_progress epic (drives the col-2 lead +
    fleet sort). Pure presentation shaping — the templates do no logic."""
    incs_raw = epic.get("increments")
    incs_raw = incs_raw if isinstance(incs_raw, list) else []
    increments: list[dict] = []
    for inc in incs_raw:
        if not isinstance(inc, dict):
            continue
        num = inc.get("num")
        pct = _clamp_pct(inc.get("pct"))
        status = inc.get("status") or ""
        increments.append(
            {
                "num": num,
                "label": f"Inc{num}" if num is not None else "Inc",
                "title": inc.get("title") or "",
                "pct": pct,
                "status": status,
                "kind": _inc_status_kind(status),
            }
        )
    status = epic.get("status") or ""
    return {
        "epic_id": epic.get("epic_id") or "—",
        "title": epic.get("title") or "",
        "status": status,
        "overall_pct": _clamp_pct(epic.get("overall_pct")),
        "increments": increments,
        "increment_count": len(increments),
        "is_active": status.lower() in _ACTIVE_EPIC_STATUSES,
        "updated_at": epic.get("updated_at"),
    }


def _shape_epics(payload: dict) -> list[dict]:
    """Shape a /epics payload's `epics` list into sorted, render-ready epic views.

    Sort is active-major: build/active/in_progress epics lead (the one you're
    working), then by overall % desc, then by epic_id — so the live epic is the
    prominent one in the col-2 stack and the fleet strip's headline. Returns []
    for a project with no epics (continuous-backlog or simply none)."""
    rows = payload.get("epics") if isinstance(payload, dict) else None
    rows = rows if isinstance(rows, list) else []
    shaped = [_shape_epic(e) for e in rows if isinstance(e, dict)]
    shaped.sort(
        key=lambda e: (
            0 if e["is_active"] else 1,
            -e["overall_pct"],
            e["epic_id"],
        )
    )
    return shaped


def _epic_view(project_key: str | None, epics_payload: dict | None = None) -> dict:
    """Build the col-2 Active-Epic section from the live /epics surface.

    `epics_payload` is the project's GET /epics response (or None when not
    fetched). When the project has epics we return mode='epics' with the shaped,
    sorted epic stack (the active/build epic leads) — the template renders each
    epic's title · overall % bar · per-increment mini-bars. When the project has
    NO epics (continuous-backlog like Marketing/BPA, or simply none) we return
    mode='continuous' with the 'continuous · no epics' line. Degrades to the same
    continuous line if /epics was unreachable (epics_payload None / empty) — we
    never fabricate epic progress."""
    epics = _shape_epics(epics_payload or {})
    if epics:
        return {
            "mode": "epics",
            "epics": epics[:_COL2_EPIC_MAX],
            "epic_count": len(epics),
        }
    # No epics → continuous-backlog line (also the graceful-degrade state).
    return {
        "mode": "continuous",
        "label": "continuous · no epics",
        "epics": [],
        "epic_count": 0,
    }


# ---------------------------------------------------------------------------
#  Fleet overview (Dashboard nav view) — ALL active projects at a glance
#
#  The Dashboard tab is the cross-project landing view (the prototype's
#  all-projects dashboard): a KPI strip + a card per active project showing real
#  vitals (agents · active tasks · pending handoffs · events/24h from /state,
#  fetched concurrently) + health + a best-effort epic-progress strip. Clicking a
#  card reuses the rail's project-select flow (GET /projects/{key} → #scope-region)
#  to scope col-2/col-4 to that project. Per-project center views (agent detail,
#  History/Analytics/Graph) are unaffected.
# ---------------------------------------------------------------------------

def _fleet_pico(project_key: str) -> str:
    """Cosmetic glyph swatch for a project card (mirrors the rail's pico tints).

    Every project key hashes uniformly into one of the brand swatches so a
    project's chip colour is stable + matches the rail. No project name is pinned
    (§2.7) — the colour is a pure function of the key length."""
    return _FLEET_PICO_SWATCHES[len(project_key or "") % len(_FLEET_PICO_SWATCHES)]


def _fleet_health(vitals_ok: bool, pending: int | None) -> dict:
    """Per-card health pill: derived purely from what /state returned.

    'down' when /state was unreachable for this project (counts are None);
    'busy' when the pending-handoff queue is at/above the attention threshold;
    'ok' otherwise. Drives the card's flag colour + the attention sort/highlight.
    NOTE: this is liveness-of-the-read + queue pressure, NOT a deep health probe
    — there's no per-project health endpoint yet, so we infer from the vitals we
    already fetch (no extra call)."""
    if not vitals_ok:
        return {"state": "down", "label": "no data"}
    if pending is not None and pending >= _FLEET_ATTENTION_PENDING:
        return {"state": "busy", "label": "needs attention"}
    return {"state": "ok", "label": "healthy"}


def _epic_strip_view(project_key: str | None, epics_payload: dict | None = None) -> dict:
    """Compact epic-progress strip for a fleet card, from the live /epics surface.

    `epics_payload` is the project's GET /epics response (fetched per project,
    concurrently with /state — see _fleet_states). When the project has epics we
    surface the HEADLINE epic (the active/build one, else the most-complete): its
    epic_id · title · overall % + a compact per-increment bar (each increment a
    done/prog/todo mini-segment). `extra` counts any further epics beyond the
    headline (the card shows "+N more"). When the project has NO epics
    (continuous-backlog, or simply none) we render the 'continuous · no epics'
    strip with an idle track. Degrades to that same continuous strip if /epics was
    unreachable — never fabricates progress.

    Returns {mode, label, epic_id, overall_pct: int|None, increments: list,
    extra: int}. mode='epics' when a real epic leads; 'continuous' otherwise."""
    epics = _shape_epics(epics_payload or {})
    if epics:
        head = epics[0]
        return {
            "mode": "epics",
            "label": head["title"] or head["epic_id"],
            "epic_id": head["epic_id"],
            "overall_pct": head["overall_pct"],
            "increments": head["increments"][:_FLEET_INC_MAX],
            "extra": len(epics) - 1,
        }
    return {
        "mode": "continuous",
        "label": "continuous · no epics",
        "epic_id": None,
        "overall_pct": None,
        "increments": [],
        "extra": 0,
    }


def _fleet_card_view(
    project: dict, vitals: dict | None, epics_payload: dict | None = None
) -> dict:
    """Shape one active project + its /state vitals into a fleet-card view dict.

    `project` is a /projects row (project_key, display_name,
    agent_count, repo_root). `vitals` is that project's /state['summary']
    (or None when /state was unreachable). `epics_payload` is that project's
    GET /epics response (or None). Pulls the real counters (active_tasks ·
    pending_handoffs · events/24h) straight off the summary, derives the health
    pill + the real epic strip, and emits the avatar/pico cosmetics. A None
    counter renders as '—' in the template (graceful degrade)."""
    key = project.get("project_key") or ""
    name = project.get("display_name") or key
    vitals_ok = vitals is not None
    v = vitals or {}
    pending = v.get("pending_handoffs")
    health = _fleet_health(vitals_ok, pending)
    return {
        "key": key,
        "name": name,
        "repo_root": project.get("repo_root"),  # fitness:allow-literal "repo_root" is a real wire/dict key, not a project literal (false match on 'root')
        "pico": _fleet_pico(key),
        "initial": (name[:1] or key[:1] or "·").upper(),
        "agent_count": project.get("agent_count"),
        "active_tasks": v.get("active_tasks"),
        "pending_handoffs": pending,
        "events_24h": v.get("events_24h"),
        "health": health,
        "needs_attention": health["state"] in ("busy", "down"),
        "epic": _epic_strip_view(key, epics_payload),
    }


def _fleet_kpis(cards: list[dict]) -> dict:
    """Roll the per-card vitals into the top KPI strip (fleet-wide totals).

    Sums the real counters across every project that reported them (None counts
    skipped, never coerced to 0 so a degraded read doesn't understate the fleet).
    `attention` is the count of cards flagged needs-attention. `tokens_today` is
    intentionally absent — there is no fleet token total on /state, so the
    template renders that KPI as 'n/a' rather than inventing a number."""
    projects_n = len(cards)
    agents = sum(c["agent_count"] for c in cards if isinstance(c["agent_count"], int))
    active = sum(c["active_tasks"] for c in cards if isinstance(c["active_tasks"], int))
    pending = sum(
        c["pending_handoffs"] for c in cards if isinstance(c["pending_handoffs"], int)
    )
    events = sum(c["events_24h"] for c in cards if isinstance(c["events_24h"], int))
    attention = sum(1 for c in cards if c["needs_attention"])
    return {
        "projects": projects_n,
        "agents": agents,
        "active_tasks": active,
        "pending_handoffs": pending,
        "events_24h": events,
        "attention": attention,
    }


def _fleet_cards(
    projects: list[dict],
    states: dict[str, dict],
    epics: dict[str, dict] | None = None,
) -> list[dict]:
    """Build the sorted fleet-card list from active projects + per-project vitals.

    `states` maps project_key → /state['summary'] dict (or None when that
    project's /state was unreachable). `epics` maps project_key → GET /epics
    payload (or None / absent). Sort is attention-major: needs-attention cards
    first (the 'who needs attention across the fleet' scan), then by
    pending-handoff count desc, then by name — so the busiest queues lead."""
    epics = epics or {}
    cards = [
        _fleet_card_view(
            p,
            states.get(p.get("project_key") or ""),
            epics.get(p.get("project_key") or ""),
        )
        for p in projects
        if p.get("project_key")
    ]
    cards.sort(
        key=lambda c: (
            0 if c["needs_attention"] else 1,
            -(c["pending_handoffs"] or 0),
            c["name"].lower(),
        )
    )
    return cards


async def _fleet_states(
    cortex: CortexClient, projects: list[dict]
) -> dict[str, dict | None]:
    """Fetch every active project's /state summary concurrently for the fleet view.

    Returns {project_key: summary_dict_or_None}. A project whose /state errors
    out (or returns no summary) yields None (rendered as '—'/'no data'), never an
    exception — same graceful-degrade contract as the rail's _attention_summaries.
    """
    keys = [p.get("project_key") for p in projects if p.get("project_key")]
    states = await asyncio.gather(
        *(cortex.get_state(k) for k in keys), return_exceptions=True
    )
    out: dict[str, dict | None] = {}
    for key, state in zip(keys, states):
        if isinstance(state, dict) and isinstance(state.get("summary"), dict):
            out[key] = state["summary"]
        else:
            out[key] = None
    return out


async def _fleet_epics(
    cortex: CortexClient, projects: list[dict]
) -> dict[str, dict | None]:
    """Fetch every active project's GET /epics payload concurrently for the fleet.

    Returns {project_key: epics_payload_or_None}. A project whose /epics errors
    out yields None (rendered as the 'continuous · no epics' strip) — same
    graceful-degrade contract as _fleet_states. CortexClient.get_epics already
    returns {"epics": []} on error, so this only sees None on an unexpected
    gather exception."""
    keys = [p.get("project_key") for p in projects if p.get("project_key")]
    payloads = await asyncio.gather(
        *(cortex.get_epics(k) for k in keys), return_exceptions=True
    )
    out: dict[str, dict | None] = {}
    for key, payload in zip(keys, payloads):
        out[key] = payload if isinstance(payload, dict) else None
    return out


async def _fleet_context(
    cortex: CortexClient,
    selected_key: str | None,
    projects: list[dict] | None = None,
) -> dict:
    """Fetch + shape everything the all-projects Dashboard (fleet) view needs.

    Pulls the active-projects list (reused if the caller already loaded it) and
    every project's /state summary concurrently, then builds the sorted fleet
    cards + the top KPI strip. `selected_key` is carried through so the center's
    self-poll + the rail/workspace stay scoped to the operator's current project
    selection (the fleet grid itself is cross-project; selection only affects
    cols 2/4). Always returns render-ready context (empty fleet degrades to an
    empty-state in the template)."""
    if projects is None:
        projects = await cortex.get_active_projects()
    # /state (vitals) and /epics (progress) fetched concurrently across the fleet.
    states, epics = await asyncio.gather(
        _fleet_states(cortex, projects),
        _fleet_epics(cortex, projects),
    )
    cards = _fleet_cards(projects, states, epics)
    return {
        "active_view": "dashboard",
        "selected_key": selected_key,
        "fleet_cards": cards,
        "fleet_count": len(cards),
        "fleet_kpis": _fleet_kpis(cards),
    }


# ---------------------------------------------------------------------------
#  Agent detail (center view) — header · token usage · recent-activity feed
# ---------------------------------------------------------------------------

# Cap on activity-feed rows shown in the agent-detail center (newest kept).
_ACTIVITY_MAX = 14

# Pull "type":"..." out of a (possibly truncated) JSON content blob.
_TYPE_RE = re.compile(r'"type":\s*"([^"]+)"')
# Pull "name":"..." (the tool/function name) out of a function_call blob.
_NAME_RE = re.compile(r'"name":\s*"([^"]+)"')
# Pull the first cmd value out of an exec_command arguments blob. The cmd lives
# inside the escaped JSON `arguments` string, so it appears as `\"cmd\":\"…` in
# the raw row; we accept either the escaped or the plain form.
_CMD_RE = re.compile(r'\\?"cmd\\?":\s*\\?"((?:[^"\\]|\\.)*)')


def _short(text: str, n: int = 90) -> str:
    """Collapse whitespace and clip to n chars with an ellipsis."""
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _summarize_history_row(content: str) -> dict | None:
    """Turn one raw /history `content` blob into a clean, readable feed row.

    The history stream is noisy: most rows are tool-call JSON (often truncated
    mid-string by the API), with occasional token_count / reasoning frames. We
    classify the row by its "type" and render a short human line — never the
    raw JSON. Returns {kind, label, detail} or None for rows we deliberately
    drop (token_count frames surface as the header readout, not feed rows).

    kind drives the feed bubble styling: 'say' (a plain agent message),
    'tool' (an action it took), 'think' (a reasoning step)."""
    raw = (content or "").strip()
    if not raw:
        return None

    # Plain (non-JSON) text → treat as something the agent said.
    if not raw.startswith("{"):
        return {"kind": "say", "label": _short(raw, 220), "detail": None}

    # Try a full parse first (most rows are truncated, so this usually fails;
    # we fall back to regex field extraction below).
    obj: dict | None = None
    if raw.count("{") == raw.count("}"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                obj = parsed
        except ValueError:
            obj = None

    type_m = _TYPE_RE.search(raw)
    ctype = (obj or {}).get("type") if obj else (type_m.group(1) if type_m else None)

    if ctype == "token_count":
        # Surfaced in the header token readout, not as a feed row.
        return None

    if ctype == "reasoning":
        return {"kind": "think", "label": "reasoned about the next step", "detail": None}

    if ctype == "function_call":
        name_m = _NAME_RE.search(raw)
        fn = (obj or {}).get("name") if obj else (name_m.group(1) if name_m else "tool")
        fn = fn or "tool"
        # For exec_command, surface the shell cmd as the detail. Prefer a clean
        # parse of the (escaped JSON) `arguments` string when the row parsed
        # fully; otherwise regex the cmd out of the raw (handles truncation).
        detail = None
        cmd = None
        if obj and isinstance(obj.get("arguments"), str):
            try:
                args = json.loads(obj["arguments"])
                if isinstance(args, dict) and isinstance(args.get("cmd"), str):
                    cmd = args["cmd"]
            except ValueError:
                cmd = None
        if cmd is None:
            cmd_m = _CMD_RE.search(raw)
            if cmd_m:
                cmd = cmd_m.group(1).encode().decode("unicode_escape", "ignore")
        if cmd:
            detail = _short(cmd, 80)
        return {"kind": "tool", "label": f"ran {fn}", "detail": detail}

    if ctype == "function_call_output":
        return {"kind": "tool", "label": "tool output", "detail": None}

    if ctype == "message":
        # An assistant/user message frame: pull readable text if present.
        text = ""
        if obj:
            c = obj.get("content")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                text = " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict)
                )
        return {"kind": "say", "label": _short(text, 220) or "(message)", "detail": None}

    # Unknown structured frame — label it by its type, never dump the JSON.
    return {"kind": "tool", "label": (ctype or "activity").replace("_", " "), "detail": None}


def _format_tokens(n: int) -> str:
    """Human token count: 1_240_000 -> '1.24M', 94_592 -> '94.6k'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _parse_token_frame(content: str) -> dict | None:
    """Parse ONE `token_count` frame's `total_token_usage` into integer fields.

    The frame carries
    `{"info": {"total_token_usage": {"input_tokens", "cached_input_tokens",
    "output_tokens"}}}` (often truncated by the API). Defensive: a full JSON
    parse when the braces balance, else regex the three integer fields out of the
    raw. Returns {total, input, output, cached} or None for a non-frame / empty
    frame (no input AND no output)."""
    raw = (content or "").strip()
    if '"token_count"' not in raw:
        return None

    usage: dict | None = None
    if raw.count("{") == raw.count("}"):
        try:
            obj = json.loads(raw)
            usage = (obj.get("info") or {}).get("total_token_usage")
        except (ValueError, AttributeError):
            usage = None

    def _grab(field: str) -> int | None:
        if isinstance(usage, dict) and isinstance(usage.get(field), int):
            return usage[field]
        m = re.search(rf'"{field}":\s*(\d+)', raw)
        return int(m.group(1)) if m else None

    inp = _grab("input_tokens")
    out = _grab("output_tokens")
    cached = _grab("cached_input_tokens")
    if inp is None and out is None:
        return None
    return {
        "total": (inp or 0) + (out or 0),
        "input": inp,
        "output": out,
        "cached": cached,
    }


def _token_usage(rows: list[dict]) -> dict | None:
    """Best-effort token usage for the agent, parsed from the most recent
    `token_count` frame in its history rows.

    Returns {total, total_h, input, output, cached} or None when no usable frame
    exists (caller renders a 'usage · n/a' placeholder). This single-frame
    (latest-run) readout drives the agent-detail header only — a Cortex /history
    agent-activity signal, distinct from the Analytics usage/cost view (which now
    reads the App-DB usage_events, not /history token frames)."""
    for content in reversed(rows):  # most-recent frame wins
        frame = _parse_token_frame(content)
        if frame is None:
            continue
        return {
            "total": frame["total"],
            "total_h": _format_tokens(frame["total"]),
            "input": frame["input"],
            "output": frame["output"],
            "cached": frame["cached"],
        }
    return None


async def _fetch_pi_catalog_groups() -> list[dict[str, Any]]:
    """Fetch the host PI model catalog groups (pi --list-models).

    Reusable async helper — callers pass the result to harness config views.
    Degrades gracefully to [] when pi is unavailable (the harness layer falls
    back to its fixed HARNESS_MODELS list)."""
    try:
        return await pi_catalog.list_pi_model_groups()
    except Exception:
        return []


def _find_agent(agents: list[dict], agent_name: str) -> dict | None:
    """[delegates to app.agents] Locate one agent record (case-insensitive name
    match) in the runtime/roster list. Returns the raw record or None if absent."""
    return agents_module.AgentsService.find_agent(agents, agent_name)


def _agent_detail_view(
    agent: dict,
    project: dict | None,
    project_key: str,
    history: list[dict],
    catalog_groups: list[dict] | None = None,
    pi_catalog_groups: list[dict] | None = None,
    orch: Any | None = None,
    run_id: str | None = None,
    runs_ctx: dict | None = None,
) -> dict:
    """Assemble the agent-detail center context for a clicked agent.

    Pulls the header identity from the (already-flattened) agent view, the
    compound id from the project key, harness/model/reasoning from runtime
    capabilities (with '—' for absent fields), the token-usage readout, the LIVE
    WORK TRANSCRIPT (this agent's runs from the orchestrator's transcript store —
    its streamed output/thinking/tool segments, the prominent main content), and
    the durable activity timeline from the agent's own /history rows (decisions +
    tool-use, filtered client-side — the API ignores its agent_name filter; see
    CortexClient). All presentation; the template does no logic. Classification +
    effective role honor the same console designation/role override as the col-2
    grouping.

    ``orch`` is accepted for signature compatibility (the activity strip + dispatch
    still pass it) but the live run transcript is NO LONGER read from it — the
    RunState SSOT store is the single live-state path (Milestone 1 T7/T12). The async
    caller (``_agent_center_context``) reads the store and passes the ready
    ``runs_ctx`` in; when none is supplied (e.g. the inline-config POST path, which
    only re-renders the header sub-line) the runs block is the empty, no-poll state.
    ``run_id`` pins a specific run (the live SSE pane carries it so the view stays on it).

    The header's harness · model · reasoning render as INLINE editable <select>
    dropdowns (the CTO's original spec) pre-set to the agent's EFFECTIVE config
    (registry value overlaid with any console-local override). `catalog_groups`
    (app.providers.view_catalog()['groups']) feeds the kaidera/pi model lists;
    these dropdowns POST to the SAME POST /settings/configure as Settings →
    Configure, so the two stay in one store. `ad_cfg` is the per-agent config view
    model (reused from harness.agent_config_view); `ad_harness_*` carry the
    options + the JS repopulation map (same as the Configure card)."""
    catalog_groups = catalog_groups or []
    ov = settings_store.get_agent_override(project_key, agent.get("name") or "")
    # DELEGATE the designation NORMALISE to the carved settings module (the single
    # source of the config logic — `SettingsService.normalize_designation`); the
    # override itself is read via the existing app-DB-backed `settings_store` path.
    designation = settings_module.service.normalize_designation(ov.get("designation"))
    view = _agent_view(agent, designation, ov.get("role", ""), override=ov)
    compound = f"{view['name']}@{project_key}"

    # Per-agent harness/model/reasoning CONFIG view model — the SAME shaping the
    # Configure card uses (effective = registry overlaid with the console-local
    # override; the override wins for the selected option). Reused so the inline
    # header dropdowns and Settings → Configure edit ONE store, never forking it.
    reg_designation = (
        settings_store.DESIGNATION_INTERACTIVE
        if _registry_interactive(agent)
        else settings_store.DESIGNATION_AUTONOMOUS
    )
    ad_cfg = harness_cfg.agent_config_view(
        agent, ov, catalog_groups, reg_designation, pi_catalog_groups
    )

    # Reasoning label (runtime 'thinking' capability). Fall back to '—'.
    reasoning = view["thinking"] or "—"

    # Filter this agent's history rows (API does NOT filter), newest LAST so the
    # feed reads top→bottom like a conversation. /history arrives newest-first.
    own_rows = [
        m.get("content", "")
        for m in history
        if (m.get("agent_name") or "").lower() == view["name"].lower()
    ]
    own_meta = [
        m for m in history
        if (m.get("agent_name") or "").lower() == view["name"].lower()
    ]

    usage = _token_usage(own_rows)

    # Build the activity feed (oldest→newest). Summarize each row; drop the ones
    # that summarize to None (e.g. token_count frames).
    feed: list[dict] = []
    for m in own_meta:
        summary = _summarize_history_row(m.get("content", ""))
        if summary is None:
            continue
        summary["when"] = m.get("when")
        feed.append(summary)
    feed.reverse()  # history is newest-first → reverse to oldest-first (newest at bottom)
    feed = feed[-_ACTIVITY_MAX:]  # keep the most recent N

    # LIVE WORK TRANSCRIPT (the prominent main content) — this agent's runs.
    # READ MODEL = the RunState SSOT store (Milestone 1 T7/T12): the async caller
    # (``_agent_center_context``) reads the store and passes the ready ``runs_ctx``
    # in. When no store context was supplied (e.g. the inline-config POST path, which
    # re-renders just the header sub-line) the runs block is the empty, no-poll state
    # (the in-memory ``_agent_runs_view`` fallback was removed at T12).
    runs = runs_ctx if runs_ctx is not None else _empty_agent_runs()

    return {
        "agent": view,
        "agent_compound": compound,
        "agent_reasoning": reasoning,
        "agent_usage": usage,
        "agent_feed": feed,
        "agent_feed_count": len(feed),
        # LIVE WORK TRANSCRIPT context (agent_runs / agent_run_selected / …) — the
        # agent's streamed output (+ thinking/tool once the runner emits them),
        # newest-first, with the running/newest run's body shown prominently.
        **runs,
        # Inline-editable header config (harness · model · reasoning) — same view
        # model + options + JS repopulation map as the Configure card. These edit
        # the SAME console agent_overrides via POST /settings/configure.
        "ad_cfg": ad_cfg,
        "ad_harness_options": harness_cfg.harness_options(),
        "ad_harness_map": json.dumps(harness_cfg.harness_js_map(catalog_groups, pi_catalog_groups)),
    }


# ---------------------------------------------------------------------------
#  Live harness chat (R2b) — resolve the agent's claude model + system context
# ---------------------------------------------------------------------------

# The DEFAULT routing for a new / unconfigured agent (one with no console override
# AND no registry harness/model): claude-code · Opus 4.8 (1M context) · max effort.
# `max` is the highest reasoning level in harness.HARNESS_REASONING["claude-code"].
_DEFAULT_HARNESS = "claude-code"
_DEFAULT_MODEL = "claude-opus-4-8[1m]"   # Opus 4.8 (1M context), per harness.HARNESS_MODELS
_DEFAULT_REASONING = "max"


def _chat_routing_for(agent: dict, project_key: str) -> tuple[str, str | None, str | None]:
    """Resolve the (harness, model, reasoning) to run for an agent's chat/dispatch
    turn — the agent's CONFIGURED routing, with the default for an unconfigured one.

    Resolution (override-first, same precedence the Configure card shows):
      * harness   : console override → registry harness → _DEFAULT_HARNESS.
      * model     : console override → registry model → (only when the effective
                    harness is the default claude-code AND nothing else set) the
                    _DEFAULT_MODEL. A model is NEVER forced for a non-default
                    harness (e.g. don't hand a claude alias to codex).
      * reasoning : console override → registry reasoning → (claude-code default
                    only) _DEFAULT_REASONING.

    So an agent with NO override and NO registry harness resolves to
    claude-code / Opus 4.8 (1M) / max; an agent configured for codex/pi/etc. runs
    that harness with its own model. The runner does the actual per-harness spawn."""
    ov = settings_store.get_agent_override(project_key, agent.get("name") or "")
    reg = harness_cfg._registry_config(agent)

    eff_harness = harness_cfg.canonical_harness(ov.get("harness") or reg["harness"]) or _DEFAULT_HARNESS
    eff_model = (ov.get("model") or reg["model"] or "").strip() or None
    eff_reasoning = (ov.get("reasoning") or reg["reasoning"] or "").strip() or None

    # LICENSE backstop (the teeth): a stored/hand-edited override naming a harness the
    # license doesn't grant must NEVER spawn it. Coerce to kaidera (always free) and drop
    # the model so the kaidera default fills below. No-op in DEV (entitlements grant all).
    try:
        from app import license as _license_mod
        if not _license_mod.entitlements().has_harness(eff_harness):
            eff_harness, eff_model = "kaidera", None
    except Exception:
        pass

    # VALIDITY (feature #99): a STORED model can be impossible for the effective
    # harness (a stale override after a harness change, or a registry capability with
    # a cross-harness model — the CTO saw claude-code + a Gemini model). Coerce it to
    # the harness default so the WORKER never spawns an impossible pair. A blank /
    # catalog-lane / unknown model passes through untouched.
    if eff_model is not None:
        eff_model = harness_cfg.coerce_model(eff_harness, eff_model)

    # Fill the DEFAULTS only for the default claude-code lane (so we never pass a
    # claude model/effort to a different harness that wouldn't understand it).
    if eff_harness == _DEFAULT_HARNESS:
        if eff_model is None:
            eff_model = _DEFAULT_MODEL
        if eff_reasoning is None:
            eff_reasoning = _DEFAULT_REASONING
    # kaidera gets the out-of-the-box Fireworks kimi default so a fresh kaidera
    # agent (the seeded onboarding Lead) runs with zero config; the operator changes it later.
    elif eff_harness == "kaidera" and eff_model is None:  # fitness:allow-literal canonical harness id (own-harness runtime), not a per-project literal
        eff_model = harness_cfg.harness_default_model("kaidera")  # fitness:allow-literal canonical harness id arg

    override = _extension_routing_override(str(agent.get("name") or ""), project_key, eff_model, eff_reasoning)
    if override is not None:
        return override

    return eff_harness, eff_model, eff_reasoning


def _chat_system_context(agent_view: dict, compound: str, project_key: str, workspace: str | None = None) -> str:
    """The agent's RICH system framing for a chat/dispatch turn — its identity persona +
    current Cortex context (the SAME `run_agent.build_agent_persona` the autonomous worker
    uses), so a chatted agent acts as ITS persona on ITS project, not a generic line.

    NO hardcoded project name: the persona is agent+project scoped, and the fallback one-
    liner takes the project from the `project_key` VARIABLE — never a literal. Skills are
    appended separately (`_system_with_delivered_skills`)."""
    name = agent_view.get("name") or "agent"
    role = agent_view.get("role") or "agent"
    display = agent_view.get("display_name") or name
    try:
        from . import run_agent as _run_agent

        rich = _run_agent.build_agent_persona(name, project_key, workspace=workspace)
        if rich:
            return rich
    except Exception:
        pass
    # Project-aware fallback (project from the variable, NOT a hardcoded name).
    return (
        f"You are {display} ({compound}), a {role} on the {project_key} project. "
        "Reply concisely and directly to the operator's message."
    )


def _system_with_delivered_skills(
    system: str, agent_name: str, project_key: str, task_text: str,
    workspace: str | None = None,
) -> str:
    """Append the agent's task-relevant delivered SKILLs to its chat/dispatch system
    prompt, the SAME way the autonomous worker does (run_agent._select_skills +
    _skills_section over the boot persona manifest).

    The interactive chat + dispatch paths historically framed the agent with only a
    light one-line system prompt, so a globally-delivered skill
    never reached the chatbot — the model could not know it had the skill or its
    scripts. This bridges that gap using the existing skill mechanism (no new path):
      1) read persona.skills from `cortex-boot <name>` (CORTEX_PROJECT scoped to the
         agent's project, since the console default project may differ / be absent),
      2) select the <=N task-relevant skills for THIS message,
      3) append the rendered SKILL.md section.

    PURE BEST-EFFORT: any failure (boot error, no skills, import hiccup) returns the
    system prompt UNCHANGED — chat must never break because of skill injection."""
    try:
        import os as _os
        import subprocess as _subprocess

        from . import run_agent as _run_agent

        env = dict(_os.environ)
        # Scope the boot to the agent's project; the console process often has no
        # CORTEX_PROJECT (or a different one), which would 404 the boot.
        if project_key:
            env["CORTEX_PROJECT"] = project_key
        boot_cwd = None
        if workspace:
            # Run cortex-boot FROM the project's repo_root with the project's
            # `.agents/scripts` first on PATH, so the workspace-path isolation guard
            # resolves the agent under the SELECTED project (not the console workspace).
            boot_cwd = workspace
            env["PATH"] = _os.path.join(workspace, ".agents", "scripts") + _os.pathsep + env.get("PATH", "")
        proc = _subprocess.run(
            ["cortex-boot", agent_name],
            capture_output=True, text=True, timeout=30, env=env, cwd=boot_cwd,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return system
        _, skills = _run_agent._parse_cortex_boot(raw)
        if not skills:
            return system
        try:
            max_n = _run_agent._max_skills()
        except Exception:
            max_n = 3
        try:
            selected = _run_agent._select_skills(task_text or "", skills, max_n)
        except Exception:
            selected = skills[:max_n]
        section = _run_agent._skills_section(selected)
        return (system + section) if section else system
    except Exception:
        # Never let skill injection break the chat — degrade to the bare system prompt.
        return system


def _build_chat_system(
    agent_view: dict, compound: str, project_key: str, skills_agent: str, task_text: str,
    workspace: str | None = None,
) -> str:
    """Build the FULL chat/dispatch system prompt — the agent's persona
    (`_chat_system_context`) PLUS its task-relevant delivered SKILLs
    (`_system_with_delivered_skills`) — in ONE call.

    Both halves shell out to `cortex-boot` (a BLOCKING subprocess), so this MUST run
    OFF the event loop via `asyncio.to_thread` at the (async) chat + dispatch call
    sites — building it inline would freeze the loop, and with it every SSE heartbeat
    and run-state NOTIFY, for the boot's duration on every single turn."""
    system = _chat_system_context(agent_view, compound, project_key, workspace=workspace)
    return _system_with_delivered_skills(system, skills_agent, project_key, task_text, workspace=workspace)



# ---------------------------------------------------------------------------
#  Usage telemetry (E007 / DATA_SEPARATION) — capture each harness/chat run's
#  token usage + estimated cost into the App-DB (NOT Cortex). Fire-and-forget
#  from the chat/dispatch result frame: a write failure NEVER breaks the chat.
# ---------------------------------------------------------------------------

def _effective_harness_for(agent: dict, project_key: str) -> str | None:
    """The agent's EFFECTIVE harness (console override wins, else registry).
    Same resolution the Configure card / chat-model path use."""
    ov = settings_store.get_agent_override(project_key, agent.get("name") or "")
    reg = harness_cfg._registry_config(agent)
    return harness_cfg.canonical_harness(ov.get("harness") or reg["harness"])


def _est_cost_for_run(
    tokens_in: int | None,
    tokens_out: int | None,
    resolved: dict,
    reported_cost: float | None,
) -> float | None:
    """Estimated USD cost for ONE run.

    Prefers the harness's own `total_cost_usd` (`reported_cost`) when present —
    that's the load-bearing billing number for the subscription lane. Otherwise
    derives it from the providers pricing (per-Mtok in/out) × this run's tokens.
    Returns None when neither a reported cost nor a priced model is available
    (the row then stores NULL cost — never a fabricated number)."""
    if reported_cost is not None:
        try:
            return float(reported_cost)
        except (TypeError, ValueError):
            pass
    p_in = resolved.get("price_in_per_mtok")
    p_out = resolved.get("price_out_per_mtok")
    if p_in is None and p_out is None:
        return None
    ti = tokens_in or 0
    to = tokens_out or 0
    cost = 0.0
    if p_in is not None:
        cost += (ti / 1_000_000.0) * p_in
    if p_out is not None:
        cost += (to / 1_000_000.0) * p_out
    return cost


async def record_run_usage_appdb(
    appdb: appdb_store.AppDB,
    project_key: str,
    agent: dict,
    model: str | None,
    result_ev: dict,
) -> None:
    """Request-free core of `_record_run_usage` — persist ONE run's usage to a
    given App-DB handle. Factored out so the autonomous orchestrator loop (which
    has no Request) can record server-side runs through the SAME path the routes
    use. Best-effort + non-blocking: any failure is swallowed."""
    try:
        tokens_in = result_ev.get("tokens_in")
        tokens_out = result_ev.get("tokens_out")
        reported_cost = result_ev.get("cost_usd")

        # Nothing meaningful to record (no tokens AND no cost) → skip the row.
        if not tokens_in and not tokens_out and reported_cost is None:
            return

        # Resolve provider + per-Mtok price off the (cached, never-raising)
        # catalog so the stored cost estimate matches the Analytics pricing.
        try:
            catalog = await providers_catalog.get_catalog()
            pricing_index = providers_catalog.pricing_index(catalog)
            resolved = providers_catalog.resolve_model(model, pricing_index)
        except Exception:
            resolved = providers_catalog.resolve_model(model, {})

        provider = resolved.get("provider")
        harness = _effective_harness_for(agent, project_key)
        cost = _est_cost_for_run(tokens_in, tokens_out, resolved, reported_cost)

        await appdb.record_usage(
            project=project_key,
            agent=agent.get("name") or None,
            harness=harness,
            model=model,
            provider=provider,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_est=cost,
        )
    except Exception:
        # Telemetry must never break the chat — swallow everything.
        return


async def _record_run_usage(
    request: Request,
    project_key: str,
    agent: dict,
    model: str | None,
    result_ev: dict,
) -> None:
    """Persist ONE run's usage to the App-DB from a runner `result` event (route
    path — resolves the App-DB off the request). Delegates to the request-free
    `record_run_usage_appdb`. Fire-and-forget; never breaks the chat/dispatch."""
    await record_run_usage_appdb(_appdb(request), project_key, agent, model, result_ev)


# ---------------------------------------------------------------------------
#  History center view (R7) — reverse-chrono activity timeline
# ---------------------------------------------------------------------------

# Cap on timeline rows shown in the History center (newest kept).
_HISTORY_MAX = 60
# Cap on recent-decisions rows shown in the History sidebar.
_HISTORY_DECISIONS_MAX = 14
# The (broad) seed query the History decisions feed runs against /search. The
# API needs a term; this catch-all surfaces recent decisions/lessons for the
# project. TODO(decisions-feed): swap for a dedicated /decisions/recent endpoint
# when one lands (the current /search seed is a pragmatic stand-in).
_HISTORY_DECISIONS_SEED = "cortex"


def _activity_kind_label(kind: str) -> str:
    """Human noun for a summarized row's kind (drives the timeline group tint)."""
    return {"say": "message", "tool": "action", "think": "reasoning"}.get(kind, "activity")


def _history_view(history: list[dict], decisions: list[dict]) -> dict:
    """Assemble the History center context: a reverse-chronological timeline of
    the project's recent activity (who · what · when) + a recent-decisions rail.

    The /history stream is the same noisy tool-call JSON the agent-detail feed
    parses, so we REUSE `_summarize_history_row` to turn each row into a clean
    one-liner (dropping the token_count frames it returns None for). /history
    arrives newest-first; we keep that order (reverse-chronological) and cap the
    list. Each row keeps its agent_name so the timeline reads who·what·when.

    `decisions` are /search results (source=decisions/lessons/graph mix); we
    shape them into a compact recent-decisions list. Both degrade to empty
    sections gracefully when the API returns nothing."""
    timeline: list[dict] = []
    for m in history:
        summary = _summarize_history_row(m.get("content", ""))
        if summary is None:
            continue
        summary["when"] = m.get("when")
        summary["agent"] = m.get("agent_name") or "—"
        summary["role"] = m.get("role") or ""
        summary["kind_label"] = _activity_kind_label(summary["kind"])
        timeline.append(summary)
    timeline = timeline[:_HISTORY_MAX]  # cap to the newest-first window…
    timeline.reverse()                  # …then render oldest→newest so the NEWEST sits at the BOTTOM
                                        # (chat-style; the history pane bottom-pins like the live transcript)

    # Distinct agents seen in the window (for the header readout).
    agents_seen = sorted({r["agent"] for r in timeline if r["agent"] and r["agent"] != "—"})

    decision_rows: list[dict] = []
    for d in decisions:
        text = _short(d.get("text") or "", 160)
        if not text:
            continue
        decision_rows.append(
            {
                "text": text,
                "source": d.get("source") or "memory",
                "category": d.get("category") or "",
            }
        )
        if len(decision_rows) >= _HISTORY_DECISIONS_MAX:
            break

    return {
        "hist_timeline": timeline,
        "hist_count": len(timeline),
        "hist_agents": agents_seen,
        "hist_agent_count": len(agents_seen),
        "hist_decisions": decision_rows,
        "hist_decision_count": len(decision_rows),
    }


async def _history_context(cortex: CortexClient, project_key: str) -> dict:
    """Fetch + shape everything the History center view needs for one project.

    Concurrent: the project record (header hex/name), the raw /history window,
    and a /search seed for the recent-decisions rail. Always returns render-ready
    context (empty sections degrade gracefully)."""
    # NOTE: /search latency is intermittent on this surface and a larger limit
    # makes it more likely to exceed the client timeout (→ empty rail, which
    # still degrades gracefully). We request just over the display cap to keep
    # the call responsive; the rail caps at _HISTORY_DECISIONS_MAX regardless.
    project, history, decisions = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_history(project_key),
        # rerank=False: a broad recent-decisions SEED feed, not a precise query — skip the
        # ~3s reranker so the History rail loads fast (HNSW retrieval alone is ~190ms).
        cortex.search(project_key, _HISTORY_DECISIONS_SEED, limit=_HISTORY_DECISIONS_MAX, rerank=False),
    )
    return {
        "active_view": "history",
        "selected": project,
        "selected_key": project_key,
        **_history_view(history, decisions),
    }


# ---------------------------------------------------------------------------
#  Analytics center view (R7) — usage analytics (Langfuse/Phoenix-style)
# ---------------------------------------------------------------------------

# How many days back the "decisions logged recently" stat looks (and the per-day
# bar breakdown spans). The API caps the window server-side (max_window_days).
_ANALYTICS_WINDOW_DAYS = 7
# Cap on per-agent rows shown in the Analytics breakdowns.
_ANALYTICS_AGENT_MAX = 8


def _iso_days_ago(days: int) -> str:
    """ISO-8601 (UTC, 'Z') timestamp `days` ago — for /decisions/recent-count."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bar_rows(pairs: list[tuple[str, int]], cap: int) -> list[dict]:
    """Shape (label, value) pairs into proportional bar rows for the templates.

    Sorted desc by value, capped, each row carrying a 0–100 `pct` relative to
    the largest value (so the top bar fills the track). Empty input → []."""
    rows = [(lbl, val) for lbl, val in pairs if isinstance(val, int) and val > 0]
    rows.sort(key=lambda p: p[1], reverse=True)
    rows = rows[:cap]
    if not rows:
        return []
    top = rows[0][1] or 1
    return [
        {"label": lbl, "value": val, "value_h": _format_tokens(val), "pct": round(val / top * 100)}
        for lbl, val in rows
    ]


# NOTE: the agent display-name map + the usage/cost shaping moved into the carved
# `app.analytics` module (Track A) — `_analytics_usage_cost` below now delegates to
# `analytics.AnalyticsService.shape_usage_cost`. The `_bar_rows` helper stays here
# because `_analytics_view` still uses it for the Cortex decisions/volume bars.


def _analytics_usage_cost(
    agents: list[dict],
    by_model: list[dict],
    by_model_provider: list[dict],
    by_agent: list[dict],
    by_project: dict,
    appdb_connected: bool,
) -> dict:
    """The substance of the Analytics view: usage + est. cost — read from the
    App-DB `usage_events` (E007 / DATA_SEPARATION), NOT the old Cortex /history
    token-frame derivation.

    DELEGATES to the carved `app.analytics` module (Track A): the metric logic now
    lives in `analytics.AnalyticsService.shape_usage_cost` (behind the
    OperationalStorePort), and this thin wrapper preserves main.py's existing
    signature so `_analytics_view` / `_analytics_context` are unchanged. The
    service is the SINGLE source of the usage/cost logic — the HTML view here and
    the JSON `GET /analytics/{project}/usage` both call it. The concrete provider/
    cost formatters are injected so the labels match the rest of the UI."""
    # No store needed here — main.py already fetched the rows concurrently (with
    # the Cortex KPIs); the service just shapes them. The JSON route uses the
    # store-backed `usage_cost(...)` path instead.
    svc = analytics_module.AnalyticsService(
        provider_label=providers_catalog.provider_label,
        fmt_cost=providers_catalog.fmt_cost,
    )
    return svc.shape_usage_cost(
        agents, by_model, by_model_provider, by_agent, by_project, appdb_connected,
    )


def _analytics_view(
    state: dict,
    decision_stats: dict,
    msg_rows: list[dict],
    counts: dict[str, int | None],
    recent_decisions: int | None,
    agents: list[dict],
    usage: dict,
) -> dict:
    """Assemble the Analytics center context for one project.

    The SUBSTANCE is the usage + est.-cost breakdowns, now read from the App-DB
    `usage_events` (E007 / DATA_SEPARATION) — `usage` is the already-shaped
    _analytics_usage_cost result (usage by model, by model×provider, per agent,
    est. cost by agent + project). The Cortex `/history` token-frame derivation
    that previously powered this has been removed.

    A slim headline KPI row is still kept from CORTEX (these ARE agent-memory
    signals, correctly sourced from Cortex): /state (events_24h, active_tasks,
    pending_handoffs), /counts, /decisions/recent-count, plus the decisions-by-
    agent / activity-volume bars. The 'Tokens · recent' KPI now reflects the App-
    DB project token total. Any missing counter renders 'n/a'/'—' (graceful)."""
    summary = state.get("summary", {}) if isinstance(state, dict) else {}

    # Decisions-by-agent bar breakdown (strip the empty-hex synthetic buckets).
    by_agent = decision_stats.get("by_agent") if isinstance(decision_stats, dict) else None
    dec_pairs: list[tuple[str, int]] = []
    if isinstance(by_agent, dict):
        for name, val in by_agent.items():
            if isinstance(val, int) and not str(name).endswith(":????"):
                dec_pairs.append((str(name), val))

    # Activity volume by agent: sum message counts across roles per agent.
    vol: dict[str, int] = {}
    for r in msg_rows:
        name = (r.get("agent_name") or "").strip()
        cnt = r.get("count")
        if name and isinstance(cnt, int):
            vol[name] = vol.get(name, 0) + cnt

    # 'Tokens · recent' KPI now comes from the App-DB project total (not /history).
    total_tokens = usage.get("total_tokens") or 0

    return {
        "an_events_24h": summary.get("events_24h"),
        "an_active_tasks": summary.get("active_tasks"),
        "an_pending_handoffs": summary.get("pending_handoffs"),
        "an_decisions_total": counts.get("decisions"),
        "an_handoffs_total": counts.get("handoffs"),
        "an_lessons_total": counts.get("lessons"),
        "an_recent_decisions": recent_decisions,
        "an_window_days": _ANALYTICS_WINDOW_DAYS,
        "an_decisions_active": (
            decision_stats.get("active") if isinstance(decision_stats, dict) else None
        ),
        "an_decisions_by_agent": _bar_rows(dec_pairs, _ANALYTICS_AGENT_MAX),
        "an_volume_by_agent": _bar_rows(list(vol.items()), _ANALYTICS_AGENT_MAX),
        # 'Tokens · recent' headline KPI — App-DB project token total.
        "an_tokens": {
            "total": total_tokens,
            "total_h": usage.get("total_tokens_h"),
        },
        # the substance: usage + est.-cost breakdowns (model / model×provider /
        # per-agent / cost-by-agent / cost-by-project), sourced from the App-DB.
        "an_usage": usage,
    }


async def _analytics_context(
    cortex: CortexClient, appdb: appdb_store.AppDB, project_key: str
) -> dict:
    """Fetch + shape everything the Analytics center view needs, concurrently.

    Usage + cost now come from the APP-DB `usage_events` (E007 / DATA_SEPARATION)
    — usage by model / by model×provider / by agent / by project, all fetched
    concurrently from the app-DB. The Cortex `/history` token-frame derivation is
    no longer used for analytics.

    The slim headline KPIs (events/24h, active tasks, decisions, the decisions-
    by-agent + activity-volume bars) still come from CORTEX (/state,
    /decisions/stats, /messages/counts/by-agent-role, /counts, /decisions/recent-
    count) — those ARE agent-memory signals. The providers catalog is fetched only
    for the 'pricing available' footnote note (cost itself is precomputed +
    stored, so it no longer depends on a live catalog).

    Graceful: if the app-DB is DOWN every usage list is empty and the view shows
    the 'usage store not connected' state; each Cortex call already degrades to a
    safe empty value on error. Never raises a 500."""
    since = _iso_days_ago(_ANALYTICS_WINDOW_DAYS)
    (
        project,
        state,
        decision_stats,
        msg_rows,
        c_decisions,
        c_handoffs,
        c_lessons,
        recent_decisions,
        agents,
        catalog,
        usage_by_model,
        usage_by_model_provider,
        usage_by_agent,
        usage_by_project,
    ) = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_state(project_key),
        cortex.get_decision_stats(project_key),
        cortex.get_message_counts_by_agent_role(project_key),
        cortex.get_count(project_key, "decisions"),
        cortex.get_count(project_key, "handoffs"),
        cortex.get_count(project_key, "lessons"),
        cortex.get_decisions_recent_count(project_key, since),
        cortex.get_agents(project_key),
        providers_catalog.get_catalog(),
        appdb.usage_by_model(project_key),
        appdb.usage_by_model_provider(project_key),
        appdb.usage_by_agent(project_key),
        appdb.usage_by_project(project_key),
    )
    counts = {"decisions": c_decisions, "handoffs": c_handoffs, "lessons": c_lessons}
    usage = _analytics_usage_cost(
        agents,
        usage_by_model,
        usage_by_model_provider,
        usage_by_agent,
        usage_by_project,
        appdb.available(),
    )
    return {
        "active_view": "analytics",
        "selected": project,
        "selected_key": project_key,
        "an_catalog_priced": catalog.get("openrouter_count", 0) > 0,
        **_analytics_view(
            state, decision_stats, msg_rows, counts, recent_decisions, agents, usage,
        ),
    }


# ---------------------------------------------------------------------------
#  Graph center view (R7) — knowledge / code graph (stats + entity browse)
# ---------------------------------------------------------------------------

# The seed query the Graph entity list runs against /cortex-graph-search when no
# explicit search term is given (the API needs a term to return entities). A
# project-flavoured catch-all so the default view isn't empty.
_GRAPH_SEED_QUERY = "cortex"
# Cap on entity rows + relationship rows shown in the side lists.
_GRAPH_ENTITY_MAX = 18
_GRAPH_REL_MAX = 14

# How many entity hits to REQUEST from /cortex-graph-search to seed the visual
# node-edge graph. We ask for more than the list cap so the one-hop expansion
# (relationships) brings in a richer neighbourhood; the rendered graph is then
# bounded by _GRAPH_NODE_CAP below regardless of how many rows come back.
_GRAPH_VIZ_QUERY_LIMIT = 28
# HARD CAP on nodes rendered in the visual graph. The Kaidera OS graph is
# ~5,868 nodes / 44k edges — rendering all of it would hang the browser, so we
# render only the search hits + their 1-hop neighbours, capped here. The visual
# graph's "showing N of M" note is driven off this vs the /graph/stats total.
_GRAPH_NODE_CAP = 140
# Companion edge cap (keeps the canvas legible even if a few hub nodes are very
# highly connected). Edges whose endpoints both survive the node cap are kept,
# up to this many.
_GRAPH_EDGE_CAP = 320


def _graph_entity_kind(entity_type: str) -> str:
    """Map a graph entity_type to one of the prototype's 3 node-kind families:
    'code' (file/function/module), 'work' (handoff/task/agent), else 'mem'
    (concept/decision/lesson/tool/...). Drives the node-kind swatch colour."""
    et = (entity_type or "").lower()
    if et in ("file", "function", "module", "class", "method", "callsite", "code"):
        return "code"
    if et in ("handoff", "task", "agent"):
        return "work"
    return "mem"


def _graph_elements(
    hi: list[dict],
    lo: list[dict],
    rels: list[dict],
) -> tuple[list[dict], int, int, dict[str, int]]:
    """Build the BOUNDED cytoscape node+edge element list for the visual graph.

    Entities (`hi` high-level + `lo` low-level from /cortex-graph-search) become
    NODES keyed by entity name; relationships become EDGES between those names.
    Crucially, the one-hop expansion surfaces relationship endpoints that are NOT
    in the direct entity hits (verified against the live surface) — those are the
    NEIGHBOUR nodes, so we synthesise a node for any relationship endpoint we
    haven't already seen (kind inferred from the relationship's source/target
    type). Direct entity hits are flagged `hit=1` (drawn larger / un-dimmed) so
    the search hits stand out from their expanded neighbourhood.

    HARD-BOUNDED: at most _GRAPH_NODE_CAP nodes (search hits first, then the
    most-connected neighbours) and _GRAPH_EDGE_CAP edges (only those whose BOTH
    endpoints survived the node cap). This is what keeps a ~5,868-node / 44k-edge
    project from ever shipping its whole graph to the browser.

    Returns (elements, node_count, edge_count, kind_counts) where `elements` is
    the cytoscape elements array ({data:{...}} dicts), the counts are the
    RENDERED totals, and kind_counts tallies rendered nodes per code/mem/work
    family (for the legend)."""
    # ---- 1. collect candidate nodes from the direct entity hits -------------
    # name -> node-data dict. Direct hits win their kind/desc over a later
    # neighbour-only sighting of the same name.
    nodes: dict[str, dict] = {}

    def _add_entity(e: dict, *, hit: bool) -> None:
        if not isinstance(e, dict):
            return
        name = e.get("name") or e.get("entity_name") or e.get("id")
        if not name:
            return
        name = str(name)
        etype = e.get("entity_type") or e.get("type") or "entity"
        existing = nodes.get(name)
        if existing is None:
            nodes[name] = {
                "id": name,
                "label": _short(name, 42),
                "full": name,
                "etype": etype,
                "kind": _graph_entity_kind(etype),
                "desc": _short(e.get("description") or "", 220),
                "hit": 1 if hit else 0,
                "deg": 0,
            }
        else:
            # Promote to a hit if this sighting is one; keep the richer desc.
            if hit:
                existing["hit"] = 1
                existing["etype"] = etype
                existing["kind"] = _graph_entity_kind(etype)
            if not existing["desc"] and e.get("description"):
                existing["desc"] = _short(e.get("description") or "", 220)

    # low-level first (concrete file/tool/entity hits), then high-level concepts.
    for e in lo:
        _add_entity(e, hit=True)
    for e in hi:
        _add_entity(e, hit=True)

    # ---- 2. fold relationship endpoints in as NEIGHBOUR nodes ---------------
    # A relationship endpoint not already a node is a 1-hop neighbour — give it a
    # node so the edge has both ends. Kind comes from the relationship's typed
    # endpoint. Track raw edges (dedup undirected pairs+type) for step 4.
    raw_edges: list[dict] = []
    seen_edge_keys: set[str] = set()
    for r in rels:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        tgt = r.get("target")
        if not src or not tgt:
            continue
        src = str(src)
        tgt = str(tgt)
        if src == tgt:
            continue
        rtype = r.get("relationship_type") or "related"
        # synthesise neighbour nodes for endpoints we haven't seen as entities
        if src not in nodes:
            _add_entity(
                {"name": src, "entity_type": r.get("source_type") or "entity"},
                hit=False,
            )
        if tgt not in nodes:
            _add_entity(
                {"name": tgt, "entity_type": r.get("target_type") or "entity"},
                hit=False,
            )
        key = "::".join(sorted((src, tgt)) + [str(rtype)])
        if key in seen_edge_keys:
            continue
        seen_edge_keys.add(key)
        raw_edges.append({"source": src, "target": tgt, "rel": str(rtype)})
        # bump degree on both endpoints (drives the neighbour-ranking cut)
        nodes[src]["deg"] += 1
        nodes[tgt]["deg"] += 1

    # ---- 3. bound the node set: all hits + the most-connected neighbours -----
    hit_nodes = [n for n in nodes.values() if n["hit"]]
    nbr_nodes = [n for n in nodes.values() if not n["hit"]]
    # neighbours ranked by degree (most-connected first) so the cut keeps the
    # nodes that actually hold the neighbourhood together.
    nbr_nodes.sort(key=lambda n: n["deg"], reverse=True)
    budget = max(0, _GRAPH_NODE_CAP - len(hit_nodes))
    kept_nodes = hit_nodes[:_GRAPH_NODE_CAP] + nbr_nodes[:budget]
    kept_ids = {n["id"] for n in kept_nodes}

    # ---- 4. keep only edges whose BOTH endpoints survived the node cap ------
    kept_edges: list[dict] = []
    for e in raw_edges:
        if e["source"] in kept_ids and e["target"] in kept_ids:
            kept_edges.append(e)
        if len(kept_edges) >= _GRAPH_EDGE_CAP:
            break

    # ---- 5. emit cytoscape elements + rendered tallies ----------------------
    kind_counts = {"code": 0, "mem": 0, "work": 0}
    elements: list[dict] = []
    for n in kept_nodes:
        kind_counts[n["kind"]] = kind_counts.get(n["kind"], 0) + 1
        elements.append(
            {
                "data": {
                    "id": n["id"],
                    "label": n["label"],
                    "full": n["full"],
                    "kind": n["kind"],
                    "etype": n["etype"],
                    "desc": n["desc"],
                    "hit": n["hit"],
                }
            }
        )
    for i, e in enumerate(kept_edges):
        elements.append(
            {
                "data": {
                    "id": f"e{i}",
                    "source": e["source"],
                    "target": e["target"],
                    "rel": e["rel"],
                }
            }
        )
    return elements, len(kept_nodes), len(kept_edges), kind_counts


def _graph_view(
    stats: dict,
    project_key: str,
    search: dict,
    term: str,
    entities: list[dict],
    entities_reachable: bool,
) -> dict:
    """Assemble the Graph center context for one project.

    `stats` (/graph/stats) lists every code-graph repo on the box; we pull out
    THIS project's repo (node/edge counts) for the headline stats and keep the
    cross-repo totals + a small repo table for context. `search`
    (/cortex-graph-search) gives the dual-level entity list + relationships,
    which we shape into a searchable entity/relationship browse. `entities`
    (/admin/cortex/entities, L4) is used ONLY when reachable
    (`entities_reachable`) — otherwise we fall back to the graph-search entities
    and note the admin surface is token-gated.

    The graph data may be thin/stale (the graph_rebuild cron has been failing),
    so the template always shows a 'graph data may be stale' note. A stats +
    entity-list view is enough for R7; a rich force-directed viz is a TODO."""
    repos = stats.get("repos") if isinstance(stats, dict) else None
    repos = repos if isinstance(repos, list) else []
    # This project's own code-graph repo (match on name == project_key).
    own = next((r for r in repos if r.get("name") == project_key), None)

    # Cross-repo context table (top repos by node count, capped).
    repo_rows = sorted(
        (
            {
                "name": r.get("name"),
                "nodes": r.get("nodes") or 0,
                "edges": r.get("edges") or 0,
                "is_own": r.get("name") == project_key,
            }
            for r in repos
            if isinstance(r, dict) and r.get("name")
        ),
        key=lambda r: r["nodes"],
        reverse=True,
    )[:8]

    # Dual-level entities from graph-search → a flat, shaped entity list.
    hi = search.get("high_level") if isinstance(search, dict) else None
    lo = search.get("low_level") if isinstance(search, dict) else None
    rels = search.get("relationships") if isinstance(search, dict) else None
    hi = hi if isinstance(hi, list) else []
    lo = lo if isinstance(lo, list) else []
    rels = rels if isinstance(rels, list) else []

    # Prefer the (token-gated) admin L4 list when it actually answered; else the
    # graph-search entities (high + low level merged, low-level first as those
    # are the concrete file/tool/entity hits).
    entity_src = entities if (entities_reachable and entities) else (lo + hi)
    entity_rows: list[dict] = []
    seen_ids: set[str] = set()
    for e in entity_src:
        if not isinstance(e, dict):
            continue
        name = e.get("name") or e.get("entity_name") or e.get("id")
        if not name:
            continue
        eid = str(e.get("id") or name)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        etype = e.get("entity_type") or e.get("type") or "entity"
        entity_rows.append(
            {
                "name": _short(str(name), 80),
                "type": etype,
                "kind": _graph_entity_kind(etype),
                "desc": _short(e.get("description") or "", 130),
                "score": e.get("score"),
            }
        )
        if len(entity_rows) >= _GRAPH_ENTITY_MAX:
            break

    rel_rows: list[dict] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        tgt = r.get("target")
        if not src or not tgt:
            continue
        rel_rows.append(
            {
                "source": _short(str(src), 60),
                "target": _short(str(tgt), 60),
                "rel": r.get("relationship_type") or "related",
                "src_kind": _graph_entity_kind(r.get("source_type") or ""),
                "tgt_kind": _graph_entity_kind(r.get("target_type") or ""),
            }
        )
        if len(rel_rows) >= _GRAPH_REL_MAX:
            break

    # ---- VISUAL node-edge graph (the primary element of the view) -----------
    # Built from the FULL graph-search payload (all hi+lo entities + every
    # relationship), bounded to _GRAPH_NODE_CAP nodes / _GRAPH_EDGE_CAP edges.
    # NOTE: we deliberately feed the visual graph the raw graph-search entities
    # (hi+lo) — NOT the admin-L4 list — because only graph-search carries the
    # relationships that become the edges (the L4 admin list is endpoint-less).
    elements, gnode_count, gedge_count, kind_counts = _graph_elements(hi, lo, rels)

    # "Showing N of M" — M is this project's own code-graph node/edge totals
    # (the bounded neighbourhood is a slice of that whole graph).
    own_nodes = own.get("nodes") if own else None
    own_edges = own.get("edges") if own else None

    return {
        "gr_term": term,
        "gr_own_repo": own.get("name") if own else None,
        "gr_own_nodes": own_nodes,
        "gr_own_edges": own_edges,
        "gr_total_nodes": stats.get("total_nodes") if isinstance(stats, dict) else None,
        "gr_total_edges": stats.get("total_edges") if isinstance(stats, dict) else None,
        "gr_repo_count": len(repos),
        "gr_repo_rows": repo_rows,
        "gr_entities": entity_rows,
        "gr_entity_count": len(entity_rows),
        "gr_relationships": rel_rows,
        "gr_rel_count": len(rel_rows),
        "gr_kind_counts": kind_counts,
        "gr_admin_reachable": entities_reachable,
        "gr_entity_source": "admin-l4" if (entities_reachable and entities) else "graph-search",
        # ---- visual graph payload (consumed by the cytoscape canvas) ----
        # JSON-serialised here so the template can drop it straight into a
        # <script type="application/json"> island the canvas JS reads.
        "gr_elements_json": json.dumps(elements, ensure_ascii=False),
        "gr_graph_nodes": gnode_count,
        "gr_graph_edges": gedge_count,
        # M for the "showing N of M nodes" note (own-repo node total; falls back
        # to the cross-repo total, then None → the note shows "—").
        "gr_graph_total_nodes": (
            own_nodes
            if own_nodes is not None
            else (stats.get("total_nodes") if isinstance(stats, dict) else None)
        ),
        "gr_node_cap": _GRAPH_NODE_CAP,
        # True when the cap actually clipped the neighbourhood (drives the
        # "search to explore" nudge vs a calmer "full neighbourhood" note).
        "gr_graph_capped": gnode_count >= _GRAPH_NODE_CAP,
    }


async def _graph_context(
    cortex: CortexClient, project_key: str, term: str | None = None
) -> dict:
    """Fetch + shape everything the Graph center view needs, concurrently.

    `term` is the optional entity search (from the in-view search box); when
    blank we seed a project-flavoured catch-all so the default view isn't empty.
    Pulls /graph/stats (cross-repo node/edge counts), /cortex-graph-search (the
    dual-level entity + relationship browse, expanded for one-hop context), and
    best-effort /admin/cortex/entities (L4 — used only if the token-gated admin
    surface answers; otherwise the graph-search entities stand in)."""
    query = (term or "").strip() or _GRAPH_SEED_QUERY
    # Request a larger entity set for the VISUAL graph (the one-hop expansion
    # then fans out further); the side lists still cap at _GRAPH_ENTITY_MAX.
    stats, search, (entities, reachable) = await asyncio.gather(
        cortex.get_graph_stats(project_key),
        cortex.graph_search(project_key, query, limit=_GRAPH_VIZ_QUERY_LIMIT, expand=True),
        cortex.get_cortex_entities(project_key, search=(term or "").strip(), limit=_GRAPH_ENTITY_MAX),
    )
    return {
        "active_view": "graph",
        "selected": project_key and await cortex.get_project(project_key),
        "selected_key": project_key,
        **_graph_view(stats, project_key, search, (term or "").strip(), entities, reachable),
    }


# ---------------------------------------------------------------------------
#  Dispatch center view (R3) — event-driven handoff → proposed-agent surface
#
#  The Dispatch tab is the operator's "what work is waiting + who should take it"
#  view, PROPOSE-MODE: it lists the selected project's open/pending handoffs and,
#  per handoff, a RULE-BASED proposed agent (match handoff.to_agent if set, else
#  handoff.to_role, against the roster) with that agent's harness + model. NOTHING
#  fires autonomously — each row has an "Approve & Run" button that the human must
#  click to spawn the agent's harness on the handoff (see POST /dispatch/.../run).
#
#  The view is EVENT-DRIVEN: it subscribes (browser EventSource) to the console
#  /stream proxy over Cortex GET /events and refreshes the handoff/dispatch list
#  the instant a new event lands — no 10s poll for this view (the other views keep
#  their polls). See _dispatch.html.
# ---------------------------------------------------------------------------

# The dispatch BOARD constants + the open-status predicate + the per-handoff
# proposal/row shaping moved 1:1 into `app.dispatch.service` (Track A, the dispatch
# carve): `OPEN_STATUSES` / `DISPATCH_MAX` / `PRIORITY_ORDER` / `is_open` /
# `propose_agent` / `dispatch_row` / `dispatch_rows` now live there, and the board
# listing flows through the module-level `_dispatch_service` (the HTML `_dispatch_rows`
# shim above). KEPT here: `_agent_index` + `_normalize_target` — the roster-match
# primitives the LIVE orchestrator target resolver (`_resolve_target_agent`, passed
# into the Orchestrator) still uses; those stay until the orchestrator's imperative
# core is carved.


def _agent_index(agents: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """Index the roster for dispatch matching: (by_name, by_role).

    `by_name` maps a lower-cased agent name → the raw agent record. `by_role`
    maps a lower-cased role → the FIRST agent with that role (roster order;
    deterministic since the roster list is stable). Both are best-effort lookups
    for the rule-based proposer — a richer skill match is a follow-up."""
    by_name: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    for a in agents:
        name = (a.get("name") or "").strip().lower()
        if name and name not in by_name:
            by_name[name] = a
        role = (a.get("role") or "").strip().lower()
        if role and role not in by_role:
            by_role[role] = a
    return by_name, by_role


def _normalize_target(value: str | None) -> str:
    """Normalize a handoff routing target to the registry token form.

    Identity v2 uses plain actor slugs and project UUID scope. Colon compound
    identities are intentionally not stripped here; if legacy/corrupt input
    appears, it should remain unmatched and visible to audit.
    """
    return (value or "").strip().lower()


# NOTE: the per-handoff proposal (`_proposed_agent`) + row shaping (`_dispatch_row`)
# moved 1:1 into `app.dispatch.service` (`propose_agent` / `dispatch_row`) — the
# board listing now flows through the module-level `_dispatch_service` (the
# `_dispatch_rows` shim below). The orchestrator's COMPLETE-record target resolver
# (`_resolve_target_agent`) is a separate concern and stays here with `_agent_index`
# / `_normalize_target` until the orchestrator's imperative core is carved.


def _dispatch_rows(
    handoffs: list[dict],
    agents: list[dict],
    project_key: str,
    project_id: str | None,
) -> list[dict]:
    """Build the sorted Dispatch list: open handoffs, each with a proposed agent.

    Filters to open/pending handoffs, proposes an agent per row (rule-based), and
    sorts urgent-major (priority weight, then newest first) so the most pressing
    waiting work leads. Capped at the board max.

    DELEGATES to `app.dispatch.DispatchService.dispatch_rows` (Track A, the dispatch
    BOARD carve): the listing + proposal + sort logic now lives in the carved module
    behind the ports, and this wrapper preserves main.py's signature so every call
    site (the `_dispatch_context` board assembly) is unchanged. The proposal's
    harness/model resolve via main's injected `_agents_resolve_config` over the
    existing app-DB-backed `settings_store` override read — the same values main
    computed inline before the carve. The orchestrator's target resolver
    (`_resolve_target_agent`) keeps using main's own `_agent_index`/`_normalize_target`
    primitives (the live imperative path is untouched)."""
    return _dispatch_service.dispatch_rows(
        handoffs, agents, project_key, project_id
    )


def _resolve_target_agent(
    handoff: dict, agents: list[dict], project_key: str = ""
) -> dict | None:
    """Resolve a handoff's target to a FULL roster agent record (or None).

    The autonomous orchestrator's target resolver. Delegates to the SHARED
    `app.domain.roles.resolve_target` (the SAME helper the Dispatch view's
    `_proposed_agent` uses, so the proposed agent and the dispatched agent never
    diverge), returning the COMPLETE agent dict so the loop can feed it to
    `_chat_routing_for` / `_agent_view`.

    Match precedence: handoff.to_agent → exact roster name (then a name-in-role
    fallback); else handoff.to_role → the HUMAN set (cto/human/operator → None, left
    for a person, never auto-dispatched even if an agent carries that role) → the
    literal by-role match (real roles like full-stack-developer → an AI worker) → a
    name-in-role fallback → a LEAD alias (cpo/co-lead/lead → the project's
    INTERACTIVE lead). Returns None when nothing matches (the loop logs 'unassigned'
    and does NOT run anything).

    The lead alias is DESIGNATION-driven, not a name hardcode: the interactive lead
    is found via this project's per-agent designation override (override-first, keyed
    by `project_key`) + the registry heuristic — so any project's `cpo` handoff routes
    to THAT project's interactive lead, never a baked-in agent. ROLE ALIASES are also
    project-local: an agent's console override or registry `role_aliases` capability
    lists secondary dispatchable roles (e.g. `creative-multimedia` → gem). The
    orchestrator threads its `project_key` in; the store read + the classifier + the
    alias reader are injected here at the composition seam so the pure domain helper
    stays free of the app-DB / agents module. With `project_key` blank, the override
    alias reads simply resolve empty and the classifier falls back to the registry
    heuristic (still no hardcode)."""
    return role_alias.resolve_target(
        handoff,
        agents,
        designation_of=lambda name: settings_store.get_agent_designation(project_key, name),
        classify_interactive=agents_module.service.classify_interactive,
        aliases_of=lambda name: settings_store.get_agent_override(project_key, name).get("role_aliases", ""),
    )


async def _project_identity(cortex: CortexClient, project_key: str) -> str | None:
    """The live project UUID (or None). Best-effort; degrades to None."""
    project = await cortex.get_project(project_key)
    return (project or {}).get("project_id") if project else None


async def _dispatch_context(
    cortex: CortexClient, project_key: str, orch: Any = None
) -> dict:
    """Fetch + shape everything the Dispatch center view needs for one project.

    Concurrent: the project record (header name), the project's /handoffs
    (the dispatch queue), and the roster (/runtime→/roster, for the proposed-agent
    match + its harness/model). Always returns render-ready context (an empty
    queue degrades to the 'no open handoffs' empty state).

    `orch` (the autonomous orchestrator, or None) adds the AUTONOMOUS state to the
    context: whether this project's master toggle is ON (read fresh from the app-DB
    — OFF by default), the loop's live status, the per-project in-flight count, and
    the orchestrator's recent-activity feed. When `orch` is None (loop failed to start) the view
    still renders, in the safe OFF/propose-mode state."""
    project, handoffs, agents = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_handoffs(project_key),
        cortex.get_agents(project_key),
    )
    project_id = (project or {}).get("project_id") if project else None
    rows = _dispatch_rows(handoffs, agents, project_key, project_id)
    proposed_n = sum(1 for r in rows if r["proposed"])

    # Autonomous state (fail-safe OFF when the app-DB / orchestrator is unavailable).
    autonomous_on = settings_store.is_project_autonomous(project_key)
    if orch is not None:
        st = orch.status(project_key)
        activity = orch.feed.recent(project_key, limit=orchestrator_mod.ACTIVITY_MAX)
        loop_running = st.get("loop_running", False)
        inflight = st.get("inflight", 0)
        # Wave plan summary (E007 Phase 1.5): per-epic active wave + running/waiting.
        # Empty epics list → no wave plan → flat Phase-1 dispatch (no wave strip).
        waves = st.get("waves") or {"epics": [], "any": False}
    else:
        activity = []
        loop_running = False
        inflight = 0
        waves = {"epics": [], "any": False}

    # Propose-mode state (PM Relentless Beat Inc 1). Read fail-safe: a degraded
    # DB returns False (auto-spawn / existing behaviour). awaiting_approval_ids is
    # the set of handoff IDs currently parked for human review; the template uses
    # this to surface an Approve button per gated handoff.
    propose_mode_on = settings_store.is_propose_mode(project_key)
    awaiting_approval_ids: set[str] = set(
        settings_store.list_awaiting_approval(project_key)
    )

    return {
        "active_view": "dispatch",
        "selected": project,
        "selected_key": project_key,
        "dispatch_rows": rows,
        "dispatch_count": len(rows),
        "dispatch_proposed_count": proposed_n,
        "dispatch_unassigned_count": len(rows) - proposed_n,
        # --- autonomous orchestrator surface ---
        "autonomous_on": autonomous_on,
        "autonomous_loop_running": loop_running,
        "autonomous_inflight": inflight,
        "autonomous_cap": orchestrator_mod.MAX_CONCURRENT,
        "autonomous_activity": activity,
        # --- wave plan (E007 Phase 1.5) ---
        "autonomous_waves": waves.get("epics", []),
        "autonomous_waves_any": bool(waves.get("any")),
        # --- propose-mode (PM Relentless Beat Inc 1) ---
        "propose_mode_on": propose_mode_on,
        "awaiting_approval_ids": awaiting_approval_ids,
    }


# ---------------------------------------------------------------------------
#  Crew activity strip (always-present shell indicator) — surfaces the
#  orchestrator's in-memory activity ring buffer prominently + persistently
#  from the shell header (not buried in the Dispatch tab), so "watch the crew
#  work" actually works across every center view. Polled by a header slot
#  (HTMX every ~4s) and shaped here. The ring buffer is live telemetry only —
#  empty (idle / fresh process) renders a clean empty state, never an error.
# ---------------------------------------------------------------------------

# How many recent activity rows the header strip's expandable feed shows (the
# strip's collapsed line always shows just the newest one). Smaller than the
# Dispatch view's full ACTIVITY_MAX — the strip is an at-a-glance recap.
_ACTIVITY_STRIP_MAX = 12


def _activity_relative(ts: str | None) -> str:
    """A compact 'how long ago' label for an ISO-UTC activity timestamp.

    'now' (<5s) · 'Ns' · 'Nm' · 'Nh' · else 'Nd'. Best-effort — an unparseable
    or absent timestamp degrades to '' (the row just omits the age)."""
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - when
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 5:
        return "now"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _activity_strip_context(orch: Any, project_key: str | None) -> dict:
    """Shape the always-present crew-activity strip for one scoped project.

    Reads the orchestrator's in-memory ring buffer (newest-first) via
    ``orch.feed.recent`` + the live loop/autonomy state via ``orch.status``, and
    returns render-ready context: a status (live · idle · off), the newest action
    for the collapsed line, and the recent feed for the expandable panel. When
    ``orch`` is None (loop failed to start) or no project is scoped, it degrades to
    the clean idle/empty state — never an error. Shell-agnostic (no pywebview)."""
    key = (project_key or "").strip().lower()
    items: list[dict] = []
    autonomous_on = False
    loop_running = False
    inflight = 0
    cap = orchestrator_mod.MAX_CONCURRENT

    if orch is not None and key:
        with suppress(Exception):
            autonomous_on = settings_store.is_project_autonomous(key)
        with suppress(Exception):
            st = orch.status(key)
            loop_running = bool(st.get("loop_running"))
            inflight = st.get("inflight", 0) or 0
            cap = st.get("max_concurrent", cap) or cap
        with suppress(Exception):
            raw = orch.feed.recent(key, limit=_ACTIVITY_STRIP_MAX)
            for a in raw:
                items.append(
                    {
                        "kind": a.get("kind") or "info",
                        "level": a.get("level") or "info",
                        "text": a.get("text") or "",
                        "agent": a.get("agent"),
                        "handoff_short": a.get("handoff_short"),
                        "ago": _activity_relative(a.get("ts")),
                    }
                )

    # Status: ON+running → live; ON but idle → watching; OFF → off.
    if autonomous_on and loop_running:
        status = "live"
    elif autonomous_on:
        status = "watching"
    else:
        status = "off"

    return {
        "act_items": items,
        "act_count": len(items),
        "act_latest": items[0] if items else None,
        "act_status": status,
        "act_autonomous_on": autonomous_on,
        "act_inflight": inflight,
        "act_cap": cap,
        "act_project": project_key or "",
        # True only when the orchestrator loop itself didn't start (degrade copy).
        "act_no_orch": orch is None,
    }


# ---------------------------------------------------------------------------
#  Recent-runs rail cap + the empty-runs context.
#
#  The agent-detail LIVE-WORK TRANSCRIPT reads the durable RunState SSOT store
#  (see ``_agent_runs_view_store`` below) — the in-memory transcript decorators
#  (``_crew_run_row`` / ``_crew_transcript_view``) and the orchestrator-backed
#  ``_agent_runs_view`` were removed at T12. ``_empty_agent_runs`` is the no-run,
#  no-poll context used wherever a runs block is needed but no store read ran
#  (e.g. the inline-config POST path).
# ---------------------------------------------------------------------------

# Cap on recent runs listed in the agent-detail run rail (newest-first; the store
# read path's ``store.recent(limit=…)`` window — see orchestrator.TRANSCRIPT_MAX_RUNS).
_CREW_RUNS_MAX = orchestrator_mod.TRANSCRIPT_MAX_RUNS

# The ONE runs READ service (Track A, the FINAL feature carve) — the single source of
# the run-read LOGIC (the agent-detail run rail + transcript view-model, the run board,
# single-run reads). The render mappers (`store_run_row` / `store_transcript_view`) are
# pure (need no store), so this store-LESS instance shapes directly; the live
# `app.state.runstate` store is passed PER-REQUEST to ``_agent_runs_view_store`` (the
# SSOT is request-scoped on app.state). The relative-age formatter is main's
# ``_activity_relative`` (so the labels match the UI exactly) and the recent-runs cap
# is the env-tuned ``_CREW_RUNS_MAX`` (the same window the read path used inline before
# the carve). The JSON router builds its OWN port-backed RunsService per request.
_runs_service = runs_module.RunsService(
    relative=_activity_relative,
    recent_max=_CREW_RUNS_MAX,
)


def _empty_agent_runs() -> dict:
    """The empty LIVE-WORK-TRANSCRIPT context (no runs, no poll). Used when a runs
    block is needed but no store read produced one — the clean idle state the
    template renders. ``agent_run_no_orch`` is False: the store is the read model
    now, so 'no runs' is the normal empty state, not a degraded 'no orchestrator' one."""
    return {
        "agent_runs": [],
        "agent_run_count": 0,
        "agent_run_running": 0,
        "agent_run_selected": None,
        "agent_run_selected_id": None,
        "agent_run_active": False,
        "agent_run_no_orch": False,
    }

# ---------------------------------------------------------------------------
#  Store-backed read model (Milestone 1 T7) — the agent-detail LIVE-WORK
#  TRANSCRIPT now reads from the RunState SSOT store (app.state.runstate) instead
#  of the in-memory transcript store. The worker writes a durable run_state row +
#  run_span spans + heartbeat + terminal status (T5/T6), so the operator SEES the
#  real, restart-survivable work. The mapping below turns RunRecord/RunSpan DTOs
#  into the SAME render-ready dict the in-memory ``_crew_run_row`` /
#  ``_crew_transcript_view`` produce — the template is untouched (the span kinds
#  thinking/tool/output match the existing segment kinds 1:1, per the T6 notes).
#  This replaces ``_enrich_run_from_cortex`` (the ~2s Cortex re-grep, now deleted):
#  its reason to exist ended when the worker started writing spans to the store.
# ---------------------------------------------------------------------------

# Status → friendly chip word: now lives in the carved `runs` module as
# ``runs.service.RUN_STATUS_LABEL`` (the render mappers moved there — the module is the
# single source of the run-read logic). Removed from main.py to avoid a stale twin.


def _store_run_row(rec: Any) -> dict:
    """Map a RunRecord HEADER → the recent-run rail row the template renders.

    DELEGATES to ``_runs_service.store_run_row`` (Track A, the runs carve): the render
    mapping now lives in the carved module behind the ``RunStatePort``, and this
    wrapper preserves main's signature so every call site is unchanged. Header only —
    no body; the relative-age labels resolve via main's injected ``_activity_relative``."""
    return _runs_service.store_run_row(rec)


def _store_transcript_view(rec: Any) -> dict:
    """Map a RunRecord WITH spans → the selected-run transcript dict the template
    renders.

    DELEGATES to ``_runs_service.store_transcript_view`` (Track A, the runs carve):
    the header + body + seg-typed segments + ended-age mapping now lives in the carved
    module behind the ``RunStatePort``, this wrapper keeps main's signature so the call
    sites are unchanged. ``truncated`` is False (the store enforces the per-run char cap
    silently in SQL)."""
    return _runs_service.store_transcript_view(rec)


async def _agent_runs_view_store(
    store: Any,
    project_key: str | None,
    agent_name: str,
    *,
    run_id: str | None = None,
) -> dict:
    """Build the agent-detail LIVE-WORK-TRANSCRIPT context for ONE agent FROM THE
    RUNSTATE STORE (the durable SSOT the worker writes — Milestone 1 T7). This is the
    ONE live-state read path now (the in-memory fallback was removed at T12); returns
    the ``agent_run*`` keys the template renders.

    DELEGATES to ``_runs_service.agent_runs_view`` (Track A, the runs carve) — the rail
    + selected-body assembly now lives in the carved module behind the ``RunStatePort``,
    so the module is the single source of the run-read logic. The live store is bound
    PER-CALL (the SSOT is request-scoped on app.state) by passing it to a request-scoped
    service instance built over the same injected formatter/cap as the module-level one.
    The signature is preserved so every call site (`_agent_detail_overview`, the chat
    SSE, the `/runstate/stream` re-read) is unchanged.

    GRACEFUL-DEGRADE (never a 500): a None store, a store whose reads RAISE, or simply
    no runs for this agent ALL degrade to the clean empty state — handled in the
    service."""
    svc = runs_module.RunsService(
        store=store, relative=_activity_relative, recent_max=_CREW_RUNS_MAX
    )
    return await svc.agent_runs_view(project_key, agent_name, run_id=run_id)


# ---------------------------------------------------------------------------
#  Per-project drill-in (center detail view) — open handoffs + tasks LISTS
# ---------------------------------------------------------------------------

# Handoff statuses that count as OPEN (still waiting / in-flight) for the
# drill-in's handoff list. A completed/closed/cancelled handoff is filtered out
# (the detail view is about live work, like the Dispatch queue). A claimed/
# in-progress handoff is still open. Unknown/blank status → treated as open.
_DETAIL_OPEN_HANDOFF_CLOSED = ("completed", "complete", "done", "closed", "cancelled", "canceled")

# Priority sort weight for the handoff list (urgent first; unknown sorts last).
_DETAIL_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "medium": 2, "low": 3}


def _detail_handoff_is_open(handoff: dict) -> bool:
    """True if a handoff is still open (not a closed/completed/cancelled row)."""
    status = (handoff.get("status") or "").strip().lower()
    return status not in _DETAIL_OPEN_HANDOFF_CLOSED


def _detail_handoff_row(handoff: dict) -> dict:
    """Shape one open handoff into a render-ready drill-in row: summary, from→to,
    priority, status, created. Carries the handoff UUID for reference."""
    hid = handoff.get("id") or ""
    compound = hid
    to_target = (
        handoff.get("to_agent")
        or (f"role · {handoff.get('to_role')}" if handoff.get("to_role") else None)
        or "—"
    )
    return {
        "compound": compound,
        "summary": _short(handoff.get("summary") or "", 220),
        "summary_full": handoff.get("summary") or "",
        "from_agent": handoff.get("from_agent") or "—",
        "to_target": to_target,
        "priority": (handoff.get("priority") or "").strip().lower() or "normal",
        "status": (handoff.get("status") or "pending").strip().lower(),
        "claimed_by": handoff.get("claimed_by"),
        "created_at": handoff.get("created_at"),
    }


def _detail_handoff_rows(handoffs: list[dict]) -> list[dict]:
    """Open handoffs, shaped + sorted urgent-first then newest-first."""
    rows = [
        _detail_handoff_row(h)
        for h in handoffs
        if _detail_handoff_is_open(h)
    ]
    rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    rows.sort(key=lambda r: _DETAIL_PRIORITY_ORDER.get(r["priority"], 9))
    return rows


def _detail_task_row(task: dict) -> dict:
    """Shape one board task into a render-ready drill-in row: title, assigned,
    status, priority."""
    return {
        "title": task.get("title") or "—",
        "assigned": task.get("assigned_agent") or "—",
        "status": (task.get("status") or "").strip().lower() or "—",
        "priority": task.get("priority"),
        "is_active": (task.get("status") or "") in _ACTIVE_TASK_STATUSES,
    }


def _detail_task_rows(tasks: list[dict]) -> list[dict]:
    """All board tasks, shaped + sorted active-first then by priority (high→low;
    the API uses a numeric priority where larger = more important)."""
    rows = [_detail_task_row(t) for t in tasks]
    # priority can be int or None → sort None last, larger first.
    rows.sort(key=lambda r: (r["priority"] is None, -(r["priority"] or 0)))
    rows.sort(key=lambda r: not r["is_active"])
    return rows


async def _project_detail_context(cortex: CortexClient, project_key: str) -> dict:
    """Fetch + shape the per-project drill-in (center detail view) for one project.

    The all-projects Dashboard shows only COUNTS; this is the click-IN that opens
    the FULL per-project lists. Concurrent: the project record (header name/folder),
    /handoffs (open queue), /board (tasks), /state (header vitals).
    Always returns render-ready context (empty lists degrade to empty states)."""
    project, handoffs, tasks, state = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_handoffs(project_key),
        cortex.get_board(project_key),
        cortex.get_state(project_key),
    )
    handoff_rows = _detail_handoff_rows(handoffs)
    task_rows = _detail_task_rows(tasks)
    summary = (state or {}).get("summary") or {}
    return {
        "active_view": "project_detail",
        "selected": project,
        "selected_key": project_key,
        "pd_project_name": (project or {}).get("display_name") or project_key,
        "pd_repo_root": (project or {}).get("repo_root"),  # fitness:allow-literal "repo_root" is a real wire/dict key, not a project literal (false match on 'root')
        "pd_handoffs": handoff_rows,
        "pd_handoff_count": len(handoff_rows),
        "pd_tasks": task_rows,
        "pd_task_count": len(task_rows),
        "pd_active_tasks": summary.get("active_tasks"),
        "pd_pending_handoffs": summary.get("pending_handoffs"),
        "pd_events_24h": summary.get("events_24h"),
        "pd_unknown": project is None,
    }


# ---------------------------------------------------------------------------
#  Per-project context loaders (shared by full page + partials)
# ---------------------------------------------------------------------------

async def _project_context(cortex: CortexClient, project_key: str) -> dict:
    """Fetch everything the agents column + center dashboard need for one
    project, concurrently. Used by both the full page and the HTMX swap.

    Also derives the col-2 Metrics block (state + board) and the live Active-Epic
    section (from GET /epics), so the agents column (col 2) renders Agents →
    Metrics → Active Epic without the templates doing logic."""
    project, agents, handoffs, tasks, state, epics = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_agents(project_key),
        cortex.get_handoffs(project_key),
        cortex.get_board(project_key),
        cortex.get_state(project_key),
        cortex.get_epics(project_key),
    )
    groups = _group_agents(agents, project_key)
    return {
        "selected": project,
        "selected_key": project_key,
        "agents": agents,
        "agent_groups": groups,
        "agent_count": len(agents),
        # The project's orchestrator-role agent name for the Autonomous-group
        # header ("triggered by <orchestrator>"); None → no attribution shown.
        "orchestrator_label": _orchestrator_label(agents, project_key),
        "handoffs": handoffs,
        "tasks": tasks,
        "state": state,
        "metrics": _metrics_view(state, tasks),
        "epic": _epic_view(project_key, epics),
    }


# ---------------------------------------------------------------------------
#  Workspace (column 4) — project-scoped file tree + read-only viewer (R5a)
# ---------------------------------------------------------------------------

async def _repo_root_for(cortex: CortexClient, project_key: str) -> str:
    """Resolve a project key to its on-disk repo_root via Cortex /projects.

    Raises WorkspaceError(404) if the project is unknown/inactive or carries no
    repo_root — so the workspace routes return a clean 'no worktree' partial
    instead of a 500."""
    project = await cortex.get_project(project_key)
    if not project:
        raise WorkspaceError(f"unknown project: {project_key}", status=404)
    repo_root = project.get("repo_root")  # fitness:allow-literal "repo_root" is a real wire/dict key, not a project literal (false match on 'root')
    if not repo_root:
        raise WorkspaceError(
            f"project '{project_key}' has no repo_root on disk", status=404
        )
    return repo_root


def _ws_root_ctx(cortex_project: dict | None, project_key: str) -> dict:
    """Shared context for the workspace dock header (root label + project name)."""
    name = (cortex_project or {}).get("display_name") or project_key
    return {
        "ws_project_key": project_key,
        "ws_project_name": name,
        "ws_repo_root": (cortex_project or {}).get("repo_root"),  # fitness:allow-literal "repo_root" is a real wire/dict key, not a project literal (false match on 'root')
    }


@app.get("/workspace/{project_key}", response_class=HTMLResponse)
async def workspace_panel(request: Request, project_key: str) -> HTMLResponse:
    """HTMX partial: the whole col-4 workspace dock for a project (header +
    toolbar + the root directory's tree). Returned on a project switch so the
    workspace re-roots to the newly selected project's worktree."""
    cortex = _cortex(request)
    project = await cortex.get_project(project_key)
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        entries = ws.list_dir(repo_root, "")
        error = None
    except WorkspaceError as exc:
        entries = []
        error = exc.message
    return templates.TemplateResponse(
        request,
        "_workspace.html",
        {
            "entries": entries,
            "ws_error": error,
            **_ws_root_ctx(project, project_key),
        },
    )


@app.get("/workspace/{project_key}/tree", response_class=HTMLResponse)
async def workspace_tree(
    request: Request, project_key: str, path: str = ""
) -> HTMLResponse:
    """HTMX partial: one directory's entries (lazy-loaded on folder expand).

    `path` is the rel-path under the project's repo_root to list ("" = the repo root itself).
    Returns the `_tree.html` fragment — a flat list of rows for that one
    directory; each folder row lazy-loads ITS children on first expand. The
    security gate lives in workspace.list_dir (rejects `..`/symlink escapes)."""
    cortex = _cortex(request)
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        entries = ws.list_dir(repo_root, path)
        error = None
        status = 200
    except WorkspaceError as exc:
        entries = []
        error = exc.message
        status = exc.status
    resp = templates.TemplateResponse(
        request,
        "_tree.html",
        {
            "entries": entries,
            "ws_error": error,
            "ws_project_key": project_key,
            "ws_parent": path,
        },
    )
    resp.status_code = status
    return resp


@app.get("/workspace/{project_key}/filetree")
async def workspace_filetree(
    request: Request, project_key: str, path: str = ""
) -> JSONResponse:
    """JSON sibling of `workspace_tree` for the SPA Workspace column. Lazy: the SPA
    fetches one directory per folder expand (`path` = rel-path under repo_root, "" = the repo root itself).
    The secure walk (rejects `..`/symlink escapes) is `ws.list_dir`. Returns
    `{path, entries:[{name, path, is_dir, size}]}`; a WorkspaceError → `{error}` + its status."""
    cortex = _cortex(request)
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        entries = ws.list_dir(repo_root, path)
    except WorkspaceError as exc:
        return JSONResponse(
            {"error": exc.message, "path": path, "entries": []}, status_code=exc.status
        )
    return JSONResponse(
        {
            "path": path,
            "entries": [
                {"name": e.name, "path": e.rel_path, "is_dir": e.is_dir, "size": e.size}
                for e in entries
            ],
        }
    )


@app.get("/workspace/{project_key}/filecontent")
async def workspace_filecontent(
    request: Request, project_key: str, path: str = ""
) -> JSONResponse:
    """JSON file READ for the SPA Workspace viewer. Returns ws.read_file's shape
    `{path, size, binary, truncated, content, lines}`. Secure (rejects ../symlink
    escapes via ws.read_file). A missing path → 400; a WorkspaceError → its status."""
    if not path:
        return JSONResponse({"error": "a file path is required"}, status_code=400)
    cortex = _cortex(request)
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        data = ws.read_file(repo_root, path)
    except WorkspaceError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    return JSONResponse(data)


@app.post("/workspace/{project_key}/filecontent")
async def workspace_filecontent_save(
    request: Request, project_key: str
) -> JSONResponse:
    """JSON file WRITE for the SPA Workspace editor. Body `{path, content}` → `{ok, path}`.
    Same secure path guard (ws.write_file). A missing field → 400; a WorkspaceError → its status."""
    cortex = _cortex(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = (str(body.get("path") or "")).strip()
    content = body.get("content")
    if not path or content is None:
        return JSONResponse({"error": "path and content are required"}, status_code=400)
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        ws.write_file(repo_root, path, str(content))
    except WorkspaceError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    return JSONResponse({"ok": True, "path": path})


@app.get("/workspace/{project_key}/file", response_class=HTMLResponse)
async def workspace_file(
    request: Request, project_key: str, path: str = ""
) -> HTMLResponse:
    """HTMX partial: a file's content in the read-only viewer.

    `path` is the rel-path under repo_root to read. Renders `_file.html`: the
    rel-path header + a monospace body (or a 'binary file — N bytes' / 'large
    file' notice). Path-escape attempts (`../../etc/hosts`, absolute paths,
    out-of-root symlinks) are rejected by workspace.read_file (403) and render
    a clean error panel, never the file."""
    cortex = _cortex(request)
    file_data: dict | None = None
    error = None
    status = 200
    if not path:
        error = "no file selected"
        status = 400
    else:
        try:
            repo_root = await _repo_root_for(cortex, project_key)
            file_data = ws.read_file(repo_root, path)
        except WorkspaceError as exc:
            error = exc.message
            status = exc.status
    resp = templates.TemplateResponse(
        request,
        "_file.html",
        {
            "file": file_data,
            "ws_error": error,
            "ws_status": status,
            "ws_project_key": project_key,
            "ws_path": path,
        },
    )
    resp.status_code = status
    return resp


# ---------------------------------------------------------------------------
#  Workspace EDITOR pop-out (R5b) — overlay body load + secure SAVE write
# ---------------------------------------------------------------------------

def _is_excalidraw(rel_path: str) -> bool:
    """True if this path is an Excalidraw drawing (.excalidraw / .excalidraw.md).

    An excalidraw file gets the rendered-SVG drawing in the center pane (decoded
    from the file's real scene) with a source-edit toggle; everything else gets
    the plain text preview + editable textarea."""
    low = (rel_path or "").lower()
    return low.endswith(".excalidraw") or low.endswith(".excalidraw.md")


# Excalidraw stroke palette → hex (the named colours the Obsidian/Excalidraw
# pickers emit). Anything already a hex/rgb string passes through untouched.
_EXCAL_COLORS = {
    "transparent": "none",
    "black": "#1e1e1e",
    "white": "#ffffff",
    "red": "#e03131",
    "green": "#2f9e44",
    "blue": "#1971c2",
    "yellow": "#f08c00",
    "orange": "#e8590c",
}


def _excal_color(val: Any, fallback: str) -> str:
    """Resolve an excalidraw colour token to something SVG can paint."""
    if not isinstance(val, str) or not val.strip():
        return fallback
    v = val.strip()
    if v in _EXCAL_COLORS:
        return _EXCAL_COLORS[v]
    return v  # already #hex / rgb() / named CSS colour


def _excal_render_svg(elements: list[dict]) -> str | None:
    """Render a decoded Excalidraw element list to a self-contained SVG string.

    Draws the real geometry — rectangles, ellipses, diamonds, lines/arrows
    (incl. multipoint), and text — positioned by each element's x/y/width/height,
    with the file's own stroke/background colours. The viewBox is the scene's
    bounding box (padded), so the drawing fills the pane at any size. Returns the
    SVG markup, or None if there's nothing renderable.

    This is a faithful static render (no live editing handles); it is NOT the
    Excalidraw React canvas, but it draws the actual saved scene rather than a
    mock. XML-escapes all text/colour values that land in the output."""
    els = [e for e in (elements or []) if isinstance(e, dict) and not e.get("isDeleted")]
    if not els:
        return None

    from xml.sax.saxutils import escape as _xe

    # Bounding box across every element (points are relative to x/y).
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for e in els:
        x = float(e.get("x", 0) or 0)
        y = float(e.get("y", 0) or 0)
        w = float(e.get("width", 0) or 0)
        h = float(e.get("height", 0) or 0)
        pts = e.get("points") or []
        if pts:
            for p in pts:
                try:
                    px, py = float(p[0]), float(p[1])
                except (TypeError, ValueError, IndexError):
                    continue
                minx = min(minx, x + px); maxx = max(maxx, x + px)
                miny = min(miny, y + py); maxy = max(maxy, y + py)
        minx = min(minx, x); miny = min(miny, y)
        maxx = max(maxx, x + w); maxy = max(maxy, y + h)
    if minx == float("inf"):
        return None
    pad = 24.0
    vb_w = max(1.0, (maxx - minx) + pad * 2)
    vb_h = max(1.0, (maxy - miny) + pad * 2)
    vb_x = minx - pad
    vb_y = miny - pad

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{vb_x:.2f} {vb_y:.2f} {vb_w:.2f} {vb_h:.2f}" '
        f'width="{vb_w:.0f}" height="{vb_h:.0f}" class="xc-svg">',
        '<defs><marker id="xc-arrow" markerWidth="10" markerHeight="10" refX="7" '
        'refY="3" orient="auto" markerUnits="userSpaceOnUse">'
        '<path d="M0,0 L7,3 L0,6 Z" fill="context-stroke"/></marker></defs>',
    ]

    def _stroke_attrs(e: dict, default_w: float = 1.6) -> str:
        sc = _excal_color(e.get("strokeColor"), "#1e1e1e")
        sw = e.get("strokeWidth")
        try:
            sw = float(sw)
        except (TypeError, ValueError):
            sw = default_w
        dash = ""
        style = e.get("strokeStyle")
        if style == "dashed":
            dash = f' stroke-dasharray="{sw*3:.1f} {sw*2:.1f}"'
        elif style == "dotted":
            dash = f' stroke-dasharray="{sw:.1f} {sw*2:.1f}"'
        op = e.get("opacity", 100)
        try:
            op = max(0.0, min(1.0, float(op) / 100.0))
        except (TypeError, ValueError):
            op = 1.0
        return (
            f'stroke="{_xe(sc)}" stroke-width="{sw:.2f}" '
            f'stroke-linecap="round" stroke-linejoin="round" '
            f'opacity="{op:.2f}"{dash}'
        )

    def _fill(e: dict) -> str:
        bg = _excal_color(e.get("backgroundColor"), "none")
        return "none" if bg in (None, "", "transparent") else bg

    for e in els:
        kind = e.get("type")
        x = float(e.get("x", 0) or 0)
        y = float(e.get("y", 0) or 0)
        w = float(e.get("width", 0) or 0)
        h = float(e.get("height", 0) or 0)
        if kind == "rectangle":
            r = 8 if e.get("roundness") else 0
            parts.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
                f'rx="{r}" fill="{_xe(_fill(e))}" {_stroke_attrs(e)}/>'
            )
        elif kind == "ellipse":
            parts.append(
                f'<ellipse cx="{x+w/2:.2f}" cy="{y+h/2:.2f}" rx="{w/2:.2f}" '
                f'ry="{h/2:.2f}" fill="{_xe(_fill(e))}" {_stroke_attrs(e)}/>'
            )
        elif kind == "diamond":
            pts = f"{x+w/2:.2f},{y:.2f} {x+w:.2f},{y+h/2:.2f} {x+w/2:.2f},{y+h:.2f} {x:.2f},{y+h/2:.2f}"
            parts.append(
                f'<polygon points="{pts}" fill="{_xe(_fill(e))}" {_stroke_attrs(e)}/>'
            )
        elif kind in ("arrow", "line", "draw", "freedraw"):
            raw = e.get("points") or [[0, 0], [w, h]]
            coords: list[str] = []
            for p in raw:
                try:
                    coords.append(f"{x+float(p[0]):.2f},{y+float(p[1]):.2f}")
                except (TypeError, ValueError, IndexError):
                    continue
            if len(coords) >= 2:
                marker = ' marker-end="url(#xc-arrow)"' if kind == "arrow" else ""
                parts.append(
                    f'<polyline points="{" ".join(coords)}" fill="none" '
                    f'{_stroke_attrs(e)}{marker}/>'
                )
        elif kind == "text":
            txt = e.get("text", "") or e.get("originalText", "")
            if not txt:
                continue
            fs = float(e.get("fontSize", 16) or 16)
            color = _excal_color(e.get("strokeColor"), "#1e1e1e")
            anchor = {"center": "middle", "right": "end"}.get(e.get("textAlign"), "start")
            tx = x + (w / 2 if anchor == "middle" else (w if anchor == "end" else 0))
            fam = "Virgil, var(--display), sans-serif"
            if e.get("fontFamily") == 3:
                fam = "var(--mono), monospace"
            lines = txt.split("\n")
            lh = fs * 1.25
            # SVG text baseline ≈ top + ~0.8em for the first line.
            tspans = "".join(
                f'<tspan x="{tx:.2f}" dy="{(lh if i else fs*0.92):.2f}">{_xe(ln) or " "}</tspan>'
                for i, ln in enumerate(lines)
            )
            parts.append(
                f'<text x="{tx:.2f}" y="{y:.2f}" font-size="{fs:.1f}" '
                f'font-family="{fam}" fill="{_xe(color)}" text-anchor="{anchor}" '
                f'style="white-space:pre">{tspans}</text>'
            )
        # other element types (image/frame/embeddable) are skipped in this render.

    parts.append("</svg>")
    return "".join(parts)


async def _load_editable(
    cortex: CortexClient, project_key: str, path: str
) -> tuple[dict | None, str | None, int]:
    """Shared loader for the center file pane: read a file for preview/edit.

    Returns (file_data, error, status). file_data is the workspace.read_file
    dict augmented with center-pane fields:
      * is_excalidraw : bool
      * excalidraw    : {count, svg} rendered drawing (or None / {error})
      * kind          : short type label for the header icon
    Binary files are surfaced as an error (the text/SVG panes are text-only).
    """
    if not path:
        return None, "no file selected", 400
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        file_data = ws.read_file(repo_root, path)
    except WorkspaceError as exc:
        return None, exc.message, exc.status

    if file_data.get("binary"):
        return None, "binary file — cannot preview or edit as text", 415

    is_excal = _is_excalidraw(path)
    file_data["is_excalidraw"] = is_excal
    file_data["kind"] = _file_kind(file_data.get("name", path))
    if is_excal:
        scene = ws.parse_excalidraw(file_data.get("content"))
        if scene and scene.get("elements"):
            svg = _excal_render_svg(scene["elements"])
            file_data["excalidraw"] = {"count": scene["count"], "svg": svg} if svg else None
        else:
            file_data["excalidraw"] = None
    else:
        file_data["excalidraw"] = None
    return file_data, None, 200


def _file_kind(name: str) -> str:
    """Map a filename to the short kind label the header/tree icons use."""
    low = (name or "").lower()
    if low.endswith(".excalidraw.md") or low.endswith(".excalidraw"):
        return "excal"
    ext = low.rsplit(".", 1)[1] if "." in low else ""
    return {
        "md": "md", "markdown": "md", "py": "py", "html": "html", "htm": "html",
        "sql": "sql", "json": "json", "yaml": "yaml", "yml": "yaml",
        "sh": "sh", "bash": "sh", "zsh": "sh", "txt": "txt",
    }.get(ext, "file")


@app.get("/workspace/{project_key}/edit", response_class=HTMLResponse)
async def workspace_edit(
    request: Request, project_key: str, path: str = "", mode: str = "preview"
) -> HTMLResponse:
    """HTMX partial: the CENTER file pane body for a file (preview or edit).

    Swapped into the center overlay pane (`#ws-editor-body`) when a tree file is
    CLICKED. Renders `_editor.html` in one of two modes:
      * mode=preview (default) — read-only view. Text files show a monospace
        body with a line gutter; a `.excalidraw(.md)` shows its DRAWING rendered
        as SVG from the file's real scene. An "Edit" toggle flips to edit mode.
      * mode=edit — an editable monospace <textarea> seeded with the file source,
        plus Save / Preview / Close. (For an excalidraw file this edits the raw
        `.excalidraw(.md)` source; saving re-renders the drawing on the next
        preview.) The toggle re-fetches this route with the other mode.

    The SAME security gate as the reads applies (workspace.read_file) — an
    escaping path is rejected (403) and renders an error, never the file. The
    overlay open/close is client-side (see openWsFile / closeWsEditor)."""
    cortex = _cortex(request)
    file_data, error, status = await _load_editable(cortex, project_key, path)
    edit_mode = (mode or "preview").lower() == "edit"
    # A truncated (large) file is preview-only — saving would lose the tail.
    if file_data and file_data.get("truncated"):
        edit_mode = False
    resp = templates.TemplateResponse(
        request,
        "_editor.html",
        {
            "file": file_data,
            "ws_error": error,
            "ws_status": status,
            "ws_project_key": project_key,
            "ws_path": path,
            "edit_mode": edit_mode,
        },
    )
    resp.status_code = status
    return resp


# ---------------------------------------------------------------------------
#  Workspace file-tree MUTATIONS — create / rename / move / delete. Each runs
#  the requested rel-path(s) through workspace.py's `_safe_target` repo_root
#  gate; an escaping path is rejected 403 and nothing is changed. All return the
#  `_ws_action.html` fragment (small toast payload) + fire HX-Trigger:ws-tree-
#  changed so the tree refreshes out-of-band.
# ---------------------------------------------------------------------------

def _ws_action_response(
    request: Request,
    *,
    project_key: str,
    ok: str | None,
    detail: str | None,
    error: str | None,
    status: int,
    changed: bool,
) -> HTMLResponse:
    """Render the shared action-result fragment + flag a tree refresh on success."""
    resp = templates.TemplateResponse(
        request,
        "_ws_action.html",
        {
            "ws_ok": ok,
            "ws_detail": detail,
            "ws_error": error,
            "ws_status": status,
            "ws_project_key": project_key,
        },
    )
    resp.status_code = status
    if changed:
        resp.headers["HX-Trigger"] = "ws-tree-changed"
    return resp


@app.post("/workspace/{project_key}/new", response_class=HTMLResponse)
async def workspace_new(
    request: Request, project_key: str
) -> HTMLResponse:
    """Create a new file or folder. Body fields: `parent` (dir rel-path, '' =
    the repo base), `name` (single component), `kind` ('file' | 'folder'). The new path
    is gated by workspace.create_file / create_dir (repo_root sandbox)."""
    cortex = _cortex(request)
    form = await _read_posted_form(request)
    parent = (form.get("parent") or "").strip().strip("/")
    name = (form.get("name") or "").strip()
    kind = (form.get("kind") or "file").strip().lower()
    rel = f"{parent}/{name}".strip("/") if parent else name
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        if kind == "folder":
            res = ws.create_dir(repo_root, rel)
            label = "Created folder"
        else:
            res = ws.create_file(repo_root, rel)
            label = "Created file"
        return _ws_action_response(
            request, project_key=project_key, ok=label, detail=res["rel_path"],
            error=None, status=200, changed=True,
        )
    except WorkspaceError as exc:
        return _ws_action_response(
            request, project_key=project_key, ok=None, detail=None,
            error=exc.message, status=exc.status, changed=False,
        )


@app.post("/workspace/{project_key}/rename", response_class=HTMLResponse)
async def workspace_rename(
    request: Request, project_key: str
) -> HTMLResponse:
    """Rename an entry. Body fields: `path` (existing rel-path), `name` (new
    single component). Both ends gated by workspace.rename_entry (sandbox)."""
    cortex = _cortex(request)
    form = await _read_posted_form(request)
    path = (form.get("path") or "").strip()
    new_name = (form.get("name") or "").strip()
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        res = ws.rename_entry(repo_root, path, new_name)
        return _ws_action_response(
            request, project_key=project_key, ok="Renamed", detail=res["rel_path"],
            error=None, status=200, changed=True,
        )
    except WorkspaceError as exc:
        return _ws_action_response(
            request, project_key=project_key, ok=None, detail=None,
            error=exc.message, status=exc.status, changed=False,
        )


@app.post("/workspace/{project_key}/move", response_class=HTMLResponse)
async def workspace_move(
    request: Request, project_key: str
) -> HTMLResponse:
    """Move an entry into a directory. Body fields: `path` (source rel-path),
    `dest` (destination DIR rel-path, '' = the repo base). Both gated by
    workspace.move_entry (sandbox); neither end may escape the repo_root."""
    cortex = _cortex(request)
    form = await _read_posted_form(request)
    path = (form.get("path") or "").strip()
    dest = (form.get("dest") or "").strip()
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        res = ws.move_entry(repo_root, path, dest)
        return _ws_action_response(
            request, project_key=project_key, ok="Moved", detail=res["rel_path"],
            error=None, status=200, changed=True,
        )
    except WorkspaceError as exc:
        return _ws_action_response(
            request, project_key=project_key, ok=None, detail=None,
            error=exc.message, status=exc.status, changed=False,
        )


@app.post("/workspace/{project_key}/delete", response_class=HTMLResponse)
async def workspace_delete(
    request: Request, project_key: str
) -> HTMLResponse:
    """Delete an entry (file, or folder recursively). Body field: `path` (rel-
    path). Gated by workspace.delete_entry (sandbox; refuses the repo base)."""
    cortex = _cortex(request)
    form = await _read_posted_form(request)
    path = (form.get("path") or "").strip()
    try:
        repo_root = await _repo_root_for(cortex, project_key)
        res = ws.delete_entry(repo_root, path)
        return _ws_action_response(
            request, project_key=project_key, ok="Deleted", detail=res["rel_path"],
            error=None, status=200, changed=True,
        )
    except WorkspaceError as exc:
        return _ws_action_response(
            request, project_key=project_key, ok=None, detail=None,
            error=exc.message, status=exc.status, changed=False,
        )


async def _read_posted_content(request: Request) -> str:
    """Pull the `content` field from the POST body WITHOUT python-multipart.

    The editor SAVE form posts `application/x-www-form-urlencoded` (HTMX's
    default); we also accept `application/json` ({"content": "..."}). Both are
    parsed from the raw body so the console keeps its zero-extra-deps footprint
    (Starlette's request.form() would require the python-multipart package).

    A non-string / missing field yields "" (an empty save) rather than an error.
    """
    raw = await request.body()
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    text = raw.decode("utf-8", "replace")

    if ctype == "application/json":
        try:
            obj = json.loads(text or "{}")
        except ValueError:
            return ""
        val = obj.get("content") if isinstance(obj, dict) else None
        return val if isinstance(val, str) else ""

    # default: urlencoded form body. keep_blank_values so an empty textarea
    # (clearing a file) still yields "" for `content` rather than dropping it.
    fields = parse_qs(text, keep_blank_values=True)
    vals = fields.get("content")
    return vals[0] if vals else ""


async def _read_posted_form(request: Request) -> dict[str, str]:
    """Parse an ENTIRE urlencoded (or JSON) POST body into a flat {key: value}
    dict, WITHOUT python-multipart (same zero-extra-deps approach as
    `_read_posted_content`). Used by the Settings System save, which posts many
    named fields at once.

    For urlencoded bodies, the LAST value wins on duplicate keys — which is what
    the System form needs: a bool flag renders a presentational checkbox PLUS a
    hidden mirror input sharing the field name; the hidden mirror is emitted
    after the checkbox, so its true/false reliably wins. `keep_blank_values` so a
    cleared text field submits "" (and a blank secret reads as "unchanged")."""
    raw = await request.body()
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    text = raw.decode("utf-8", "replace")

    if ctype == "application/json":
        try:
            obj = json.loads(text or "{}")
        except ValueError:
            return {}
        if not isinstance(obj, dict):
            return {}
        return {str(k): ("" if v is None else str(v)) for k, v in obj.items()}

    fields = parse_qs(text, keep_blank_values=True)
    return {k: (v[-1] if v else "") for k, v in fields.items()}


@app.post("/workspace/{project_key}/file", response_class=HTMLResponse)
async def workspace_save(
    request: Request, project_key: str, path: str = ""
) -> HTMLResponse:
    """Write a file from the editor pop-out — the ONE mutating workspace route.

    `path` is the rel-path under repo_root; the new content is the POST body's
    `content` form field. The write goes through workspace.write_file, which
    runs the identical `_safe_target` repo_root gate as the reads: an escaping
    path (`../../tmp/evil`, absolute, out-of-root symlink) is rejected with 403
    and NOTHING is written.

    Returns the `_save_result.html` fragment (a small confirmation / error
    banner swapped into the editor footer) and sets the HTTP status to match
    (200 saved · 4xx rejected). On success it also flags an out-of-band tree
    refresh via the HX-Trigger header so a newly-created file appears in col 4."""
    cortex = _cortex(request)
    content = await _read_posted_content(request)

    saved: dict | None = None
    error = None
    status = 200
    if not path:
        error = "no file path"
        status = 400
    else:
        try:
            repo_root = await _repo_root_for(cortex, project_key)
            saved = ws.write_file(repo_root, path, content)
        except WorkspaceError as exc:
            error = exc.message
            status = exc.status

    resp = templates.TemplateResponse(
        request,
        "_save_result.html",
        {
            "saved": saved,
            "ws_error": error,
            "ws_status": status,
            "ws_project_key": project_key,
            "ws_path": path,
        },
    )
    resp.status_code = status
    # Tell the col-4 dock to refresh its tree so a brand-new file shows up.
    if saved is not None:
        resp.headers["HX-Trigger"] = "ws-saved"
    return resp


# ---------------------------------------------------------------------------
#  Settings · Configure (R4c) — per-agent harness/model/reasoning configurator
# ---------------------------------------------------------------------------

# The local-cortex 6-layer Cortex, names + KEEP/NEW/OPTIMIZE status, top→bottom
# (L6 first). Sourced from local-cortex/ARCHITECTURE.md "Layer status" + the
# cortex.md 6-layer table. Display-only — drives the Cortex tab read-out.
_CORTEX_LAYERS: list[dict[str, str]] = [
    {
        "id": "L6",
        "name": "Boot Context",
        "status": "KEEP",
        "what": "cortex-boot pulls identity, facts, and recent history for session start.",
    },
    {
        "id": "L5",
        "name": "Multimodal Artifacts",
        "status": "NEW",
        "what": "artifacts + artifact_edges with typed modality, captions, neighborhood text, and provenance.",
    },
    {
        "id": "L4",
        "name": "Knowledge Graph",
        "status": "NEW",
        "what": "cortex_entities + cortex_relationships with LightRAG-style dual-level retrieval.",
    },
    {
        "id": "L3",
        "name": "Code Graph",
        "status": "OPTIMIZE",
        "what": "better-code-review-graph via DuckDB + SQLite, exposed through cortex-graph-*.",
    },
    {
        "id": "L2",
        "name": "Vector Embeddings",
        "status": "KEEP",
        "what": "pgvector 768-d embeddings over durable text rows.",
    },
    {
        "id": "L1",
        "name": "Verbatim Storage",
        "status": "KEEP",
        "what": "decisions, lessons, knowledge, messages, handoffs, tasks, agents, and artifacts.",
    },
]


async def _configure_context(cortex: CortexClient, project: str | None) -> dict:
    """Build the Configure (R4c) sub-tab context for the selected project.

    Lists the project's agents (from /projects/{key}/runtime, falling back to
    /roster via CortexClient.get_agents) and, per agent, shapes the
    harness/model/reasoning configurator row: the CURRENT EFFECTIVE config (the
    registry value overlaid with any console-local override) plus the dropdown
    option sets for the effective harness. Also emits the harness→{models,
    reasoning} JS map (incl. the provider catalog for the kaidera/pi lanes)
    so the model + reasoning dropdowns re-populate client-side when the harness
    changes.

    The provider catalog (kaidera/pi model source) comes from the cached
    providers layer — get_catalog() never raises, so this always renders."""
    project_key = project or _default_project()
    agents = await cortex.get_agents(project_key)

    # kaidera/pi pull their model lists from the live Providers & Models
    # catalog (cached ~15 min; never raises) and the host PI catalog.
    catalog = await providers_catalog.get_catalog()
    catalog_groups = providers_catalog.view_catalog(catalog).get("groups", [])
    pi_catalog_groups = await _fetch_pi_catalog_groups()

    overrides = settings_store.load_agent_overrides()
    rows: list[dict] = []
    for agent in agents:
        name = agent.get("name") or ""
        key = settings_store._override_store_key(project_key, name)
        # registry-derived designation (the heuristic default) for the
        # "registry: …" hint + the effective value when no override is set.
        reg_designation = (
            settings_store.DESIGNATION_INTERACTIVE
            if _registry_interactive(agent)
            else settings_store.DESIGNATION_AUTONOMOUS
        )
        rows.append(
            harness_cfg.agent_config_view(
                agent, overrides.get(key, {}), catalog_groups, reg_designation,
                pi_catalog_groups,
            )
        )
    rows.sort(key=lambda r: r["display_name"].lower())

    return {
        "settings_page": "configure",
        "settings_body_template": "_settings_configure.html",
        "selected_key": project_key,
        "cfg_agents": rows,
        "cfg_agent_count": len(rows),
        "cfg_harness_options": harness_cfg.harness_options(),
        "cfg_harness_map": json.dumps(harness_cfg.harness_js_map(catalog_groups, pi_catalog_groups)),
        "cfg_catalog_total": catalog.get("total", 0),
    }


async def _cortex_settings_context(cortex: CortexClient, project: str | None) -> dict:
    """Build the Cortex (R4c) sub-tab context — an informational read-out.

    Shows the Cortex connection (base URL from the console-local settings store,
    defaulting to the schema default) with a pointer to System to change it, the
    live /health (status, surface_version, event_backend, rls_enforced), and a
    compact read of the local-cortex 6-layer architecture (L1…L6 names +
    KEEP/NEW/OPTIMIZE status from ARCHITECTURE.md). Mostly display."""
    settings_store.ensure_store()
    cfg = settings_store.load()
    health = await cortex.get_health()
    return {
        "settings_page": "cortex",
        "settings_body_template": "_settings_cortex.html",
        "selected_key": project or _default_project(),
        "cortex_base_url": cfg.get("cortex_base_url"),
        "cortex_client_base_url": cortex.base_url,
        "cortex_default_project": cfg.get("cortex_default_project"),
        "cortex_health": health,
        "cortex_layers": _CORTEX_LAYERS,
    }


async def _projects_folder_context(
    cortex: CortexClient,
    project: str | None,
    *,
    saved_key: str | None = None,
    saved_prev: str | None = None,
    saved_new: str | None = None,
    save_error: str | None = None,
) -> dict:
    """Build the Workspace (project-folder config) settings sub-tab context.

    Lists every ACTIVE project with its current canonical working folder
    (`repo_root`) so the operator can change it IN-APP (the in-app version of the
    repo_root fix we previously did by CLI). Each row's edit field POSTs to
    /settings/projects/{key}/folder, which PATCHes Cortex with the admin token
    (backend-only; see CortexClient.set_project_repo_root).

    `token_configured` drives a banner when the admin token can't be sourced (env
    nor .env) — in that state the save is disabled with a clear message rather
    than failing on submit. The save-result fields (`saved_*`) are populated only
    on the POST response so the saved row shows previous → new inline.

    NOTE: we read `resolve_admin_token()` only to learn IF a token exists (a bool)
    — the token VALUE never enters the context and is never rendered."""
    projects = await cortex.get_active_projects()
    rows = [
        {
            "key": p.get("project_key"),
            "name": p.get("display_name") or p.get("project_key"),
            "repo_root": p.get("repo_root"),  # fitness:allow-literal "repo_root" is a real wire/dict key, not a project literal (false match on 'root')
        }
        for p in projects
        if p.get("project_key")
    ]
    token_configured = bool(cortex_admin_token())
    return {
        "settings_page": "projects",
        "settings_body_template": "_settings_projects.html",
        "selected_key": project or _default_project(),
        "pf_rows": rows,
        "pf_count": len(rows),
        "pf_token_configured": token_configured,
        # save-result (POST only)
        "pf_saved_key": saved_key,
        "pf_saved_prev": saved_prev,
        "pf_saved_new": saved_new,
        "pf_save_error": save_error,
    }


# ---------------------------------------------------------------------------
#  Settings view (R4a/R4b/R4c) — context builders for the 4-tab Settings layout
# ---------------------------------------------------------------------------

async def _settings_body_context(
    cortex: CortexClient, page: str, project: str | None
) -> dict:
    """Build the context for ONE settings sub-tab body.

    For "system" (R4a) this loads the console-local settings store and shapes it
    into render-ready groups (app.settings.view_groups). For "providers" (R4b) it
    fetches the dynamic provider/model catalog (OpenRouter public list always +
    any key-configured providers, ~15-min cached) and shapes it for the read-only
    Providers & Models table. For "configure" (R4c) it builds the per-agent
    harness/model/reasoning configurator for the selected project; for "cortex"
    (R4c) the informational Cortex connection + /health + 6-layer read-out. An
    unknown sub-tab id degrades to a generic stub. Used by both the full Settings
    view and the sub-tab HTMX swap.

    Async because several tabs do I/O (live catalog, runtime agents, /health) —
    the System branch does no network I/O but the function is uniformly awaited.
    """
    page = page if page in SETTINGS_TAB_IDS else DEFAULT_SETTINGS_TAB
    if page == "system":
        settings_store.ensure_store()  # materialise the file on first run
        return {
            "settings_page": "system",
            "settings_body_template": "_settings_system.html",
            "groups": settings_store.view_groups(),
            "custom_providers": settings_store.view_custom_providers(),
            "selected_key": project,
        }
    if page == "providers":
        # Read-only dynamic catalog. get_catalog() never raises (network failure
        # → cached/empty + a note), so the tab always renders.
        catalog = await providers_catalog.get_catalog()
        return {
            "settings_page": "providers",
            "settings_body_template": "_settings_providers.html",
            "catalog": providers_catalog.view_catalog(catalog),
            "selected_key": project,
        }
    if page == "configure":
        return await _configure_context(cortex, project)
    if page == "projects":
        return await _projects_folder_context(cortex, project)
    if page == "cortex":
        return await _cortex_settings_context(cortex, project)
    if page == "license":
        from app import license as lic
        try:
            from app import settings as _settings
            raw = _settings._read_raw() or {}
        except Exception:
            raw = {}
        # Make a dict out of the dataclass to pass to the template
        ent = lic.entitlements()
        # if there is a pending login, its info might be somewhere, but we'll manage it via HTMX
        return {
            "settings_page": "license",
            "settings_body_template": "_settings_license.html",
            "selected_key": project,
            "entitlements": {
                "valid": ent.valid,
                "reason": ent.reason,
                "customer": ent.customer,
                "org_id": ent.org_id,
                "in_grace": ent.in_grace,
                "valid_until": ent.valid_until,
                "grace_until": ent.grace_until,
                "wallet": ent.wallet,
                "addons": ent.addons,
                "harnesses": list(ent.harnesses),
                "limits": ent.limits,
            },
            "install_id": raw.get("license_install_id", "Not generated yet"),
            "machine_fp": __import__("app.license_client").license_client.machine_fingerprint(raw, lambda x: True) if raw.get("license_machine_salt") else "Not generated yet",
        }
    # Unknown sub-tab id — generic stub (no sub-tab should normally reach this).
    label, increment = _SETTINGS_PLACEHOLDER.get(page, (page.title(), "a later increment"))
    return {
        "settings_page": page,
        "settings_body_template": "_settings_placeholder.html",
        "placeholder_label": label,
        "placeholder_increment": increment,
        "selected_key": project,
    }


async def _settings_context(cortex: CortexClient, page: str, project: str | None) -> dict:
    """Full context for the Settings center view (header + sub-nav + active body).

    Wraps `_settings_body_context` with the sub-nav metadata (tabs + icons) the
    `_settings.html` shell needs. The active body partial is included inline on
    first render; later tab clicks swap just `#settings-body`."""
    return {
        "active_view": "settings",
        "settings_tabs": SETTINGS_TABS,
        "settings_tab_icons": SETTINGS_TAB_ICONS,
        **(await _settings_body_context(cortex, page, project)),
    }


# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def console(request: Request, project: str | None = None) -> HTMLResponse:
    """Full console shell. Renders the top bar (nav + health pill), the project
    rail (with cross-project attention counts), the agents column for the
    selected project, the center ALL-PROJECTS Dashboard (fleet overview), and the
    workspace dock.

    The Dashboard is cross-project: a card per active project with real vitals
    (agents · active tasks · pending handoffs · events/24h) + health + a
    best-effort epic strip. `?project=<key>` selects a project — that only scopes
    cols 2/4 (agents + workspace); the fleet grid stays cross-project."""
    # The React SPA at /app is the MODERN UI — provider key-add, project creation, the agent
    # chat pane, the relocated version. Land users there by default so a fresh deploy doesn't
    # open on the legacy HTMX shell (the "I don't see the add-API option" trap). The legacy
    # shell below stays the fallback when the SPA bundle isn't built.
    if (SPA_DIST_DIR / "index.html").exists():
        return RedirectResponse(url="/app/", status_code=307)
    cortex = _cortex(request)
    health, projects = await asyncio.gather(
        cortex.get_health(),
        cortex.get_active_projects(),
    )
    # Fetch every project's /state ONCE (feeds BOTH the rail attention line and
    # the fleet cards — no double /state fetch) + every project's /epics, both
    # concurrently across the fleet.
    states, epics = await asyncio.gather(
        _fleet_states(cortex, projects),
        _fleet_epics(cortex, projects),
    )
    attention = _attention_from_states(states)
    selected_key = _pick_selected(projects, project)

    # Center: the all-projects fleet overview (built from the shared states + epics).
    fleet_cards = _fleet_cards(projects, states, epics)
    fleet_ctx = {
        "fleet_cards": fleet_cards,
        "fleet_count": len(fleet_cards),
        "fleet_kpis": _fleet_kpis(fleet_cards),
    }

    # Col-2 (agents) + col-4 (workspace) are scoped to the selected project.
    ctx: dict = {
        "selected": None,
        "selected_key": selected_key,
        "agents": [],
        "agent_groups": {"interactive": [], "autonomous": []},
        "agent_count": 0,
        "handoffs": [],
        "tasks": [],
        "state": {},
        "metrics": _metrics_view({}, []),
        "epic": _epic_view(selected_key),
    }
    if selected_key:
        ctx = await _project_context(cortex, selected_key)

    # Workspace dock (col 4): root the file tree at the selected project's
    # repo_root on first paint. Degrades to a 'no worktree' panel if the project
    # has no repo_root or it isn't accessible.
    ws_entries: list = []
    ws_error: str | None = None
    if selected_key:
        try:
            repo_root = await _repo_root_for(cortex, selected_key)
            ws_entries = ws.list_dir(repo_root, "")
        except WorkspaceError as exc:
            ws_error = exc.message
    ws_ctx = _ws_root_ctx(ctx.get("selected"), selected_key or "")

    # Crew-activity strip (always-present shell header indicator) — first paint;
    # the header slot then HTMX-polls /activity to keep it live across views.
    act_ctx = _activity_strip_context(_orchestrator(request), selected_key)

    return templates.TemplateResponse(
        request,
        "console.html",
        {
            "health": health,
            "projects": projects,
            "attention": attention,
            "nav_views": NAV_VIEWS,
            "active_view": "dashboard",
            "entries": ws_entries,
            "ws_error": ws_error,
            **ws_ctx,
            **ctx,
            **fleet_ctx,
            **act_ctx,
        },
    )


@app.get("/projects")
async def projects_json(request: Request) -> list[dict]:
    """JSON: the active-projects list (the SPA's `api.projects()` → `Project[]`).

    The SPA rail needs a JSON project list, but the console historically exposed
    only the HTML/HTMX project routes (`GET /projects/{project_key}` +
    `/projects/{project_key}/detail`). This is the missing JSON LIST surface.

    SOURCE — the SAME place the existing project UI reads: the rail + the fleet
    cards both build off `CortexClient.get_active_projects()` (see the `/` route +
    `_fleet_states`). We read that ONE source and pass the registry rows THROUGH
    unchanged (the SPA never invents the list), so the SPA `Project` fields the
    rail uses (`project_key` · `display_name` · `status` · `repo_root`) plus the
    extra registry fields (`project_id`, `agent_count`, …) all survive — the SPA
    `Project` type is permissive (`[k: string]: unknown`).

    COLLISION-FREE: the literal `/projects` path is distinct from the parametrised
    `/projects/{project_key}` HTML partial (a non-empty segment is required for the
    latter), so this can neither shadow nor be shadowed by the HTML routes. It
    returns a plain list → FastAPI serialises JSON (default JSONResponse), so it is
    a genuinely different response shape from the HTMLResponse partial family.

    Graceful-degrade: `get_active_projects()` already returns `[]` on a Cortex
    error, so a down Cortex yields an empty array here (never a 500) — matching the
    SPA client's 'treat as empty rail' expectation."""
    cortex = _cortex(request)
    return await cortex.get_active_projects()


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz() -> dict[str, str]:
    """Unauthenticated liveness probe for external uptime monitors.

    PURPOSE — a monitor (or a HEAD ping) needs to confirm the console process is up WITHOUT a
    session. It is on the auth allowlist (`auth.is_public_path`), so it returns 200 even when
    first-party auth is enabled (selfcontained mode), where every other path 401s unauthenticated.
    This is a PURE liveness check — it touches no backend (no Cortex/DB call), so it stays fast and
    can't flap on a transient dependency blip. For the live Cortex/6-layer readiness read-out use
    `GET /cortex/health` instead.

    COLLISION-FREE — `/healthz` is a fresh top-level literal route, outside the `/app` SPA mount and
    distinct from every project/agent path; HEAD returns the same 200 with an empty body.
    """
    return {"status": "ok", "version": __version__}


@app.get("/console/version")
async def console_version_json() -> dict[str, str]:
    """JSON: the console build version for the SPA shell badge.

    SOURCE — ``app/version.py`` is already the single release version used by the
    FastAPI metadata and the legacy Jinja templates. The SPA needs the same value
    over same-origin JSON because its bundle is static and cannot read Jinja globals.

    COLLISION-FREE: ``/console/version`` is a literal route under the console prefix,
    outside the ``/app`` SPA static mount and distinct from every project/agent path.
    It returns a plain dict, so FastAPI serialises JSON.
    """
    return {"version": __version__}


def _release_version_tuple(value: str | None) -> tuple[int, int, int] | None:
    """Parse ``vX.Y.Z`` / ``X.Y.Z`` into a comparable tuple."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", value or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _release_version_text(value: str | None) -> str | None:
    m = re.search(r"(\d+\.\d+\.\d+)", value or "")
    return m.group(1) if m else None


def _bounded_release_notes(value: Any, limit: int = 1600) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _base_update_status() -> dict[str, Any]:
    repo = (os.environ.get("KAIDERA_REPO") or "").strip() or DEFAULT_RELEASE_REPO
    current = __version__
    return {
        "current_version": current,
        "latest_version": None,
        "latest_tag": None,
        "update_available": None,
        "check_ok": False,
        "source": "github-release",
        "repo": repo,
        "update_command": "./update.sh",
        "apply_endpoint": "/console/update/apply",
        "job_endpoint": "/console/update-job",
        "can_apply": False,
        "admin_required": True,
        "release_name": None,
        "release_notes": None,
        "impact": UPDATE_IMPACT,
        "backup_guidance": UPDATE_BACKUP_GUIDANCE,
        "rollback_guidance": UPDATE_ROLLBACK_GUIDANCE,
        "post_update_checks": UPDATE_POST_UPDATE_CHECKS,
        "error": None,
    }


def _console_update_status(run_cmd=subprocess.run) -> dict[str, Any]:
    """Return the latest signed-release status, degrading softly.

    The updater itself uses GitHub Releases through ``gh`` + minisign. Reuse the
    same channel for status so the app reports the canonical publisher view. If
    ``gh`` is missing, unauthenticated, offline, or the repo cannot be reached, this
    returns ``check_ok=false`` instead of raising; update status is advisory and must
    never break the console.
    """
    base = _base_update_status()
    repo = base["repo"]
    current = base["current_version"]
    gh = shutil.which("gh")
    if not gh:
        return {**base, "error": "GitHub CLI not installed; run ./update.sh on the server."}

    try:
        result = run_cmd(
            [
                gh,
                "release",
                "view",
                "--repo",
                repo,
                "--json",
                "tagName,publishedAt,url,name,body",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        return {**base, "error": f"release check failed: {exc}"}
    if getattr(result, "returncode", 1) != 0:
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        return {**base, "error": detail[:240] or "release check failed"}

    try:
        payload = json.loads(getattr(result, "stdout", "") or "{}")
    except Exception as exc:
        return {**base, "error": f"release check returned invalid JSON: {exc}"}

    latest_tag = str(payload.get("tagName") or "").strip() or None
    latest_version = _release_version_text(latest_tag)
    current_tuple = _release_version_tuple(current)
    latest_tuple = _release_version_tuple(latest_version)
    available = (
        latest_tuple > current_tuple
        if latest_tuple is not None and current_tuple is not None
        else None
    )
    return {
        **base,
        "latest_version": latest_version,
        "latest_tag": latest_tag,
        "update_available": available,
        "can_apply": available is True,
        "check_ok": latest_version is not None,
        "release_name": payload.get("name"),
        "release_notes": _bounded_release_notes(payload.get("body")),
        "published_at": payload.get("publishedAt"),
        "release_url": payload.get("url"),
        "error": None if latest_version else "latest release tag did not contain a semantic version",
    }


def _clear_update_status_cache_for_tests() -> None:
    global _UPDATE_STATUS_CACHE, _UPDATE_STATUS_CACHE_AT, _UPDATE_STATUS_REFRESHING
    with _UPDATE_STATUS_CACHE_LOCK:
        _UPDATE_STATUS_CACHE = None
        _UPDATE_STATUS_CACHE_AT = 0.0
        _UPDATE_STATUS_REFRESHING = False


def _update_status_cache_snapshot(refresh: bool = False) -> tuple[dict[str, Any], bool]:
    """Return a fast update-status payload and whether a background refresh is needed."""
    now = time.monotonic()
    with _UPDATE_STATUS_CACHE_LOCK:
        cached = dict(_UPDATE_STATUS_CACHE) if _UPDATE_STATUS_CACHE else None
        cache_age = now - _UPDATE_STATUS_CACHE_AT if cached else None
        fresh = (
            cached is not None
            and cache_age is not None
            and cache_age < UPDATE_STATUS_CACHE_TTL_SECONDS
        )
        refreshing = _UPDATE_STATUS_REFRESHING

    if cached and fresh and not refresh:
        return {**cached, "cached": True, "refreshing": refreshing}, False

    should_refresh = not refreshing
    if cached:
        return {
            **cached,
            "cached": True,
            "stale": True,
            "refreshing": refreshing or should_refresh,
        }, should_refresh

    return {
        **_base_update_status(),
        "source": "github-release-cache",
        "cached": False,
        "refreshing": refreshing or should_refresh,
        "error": "update check running in background",
    }, should_refresh


async def _refresh_update_status_cache(run_cmd=subprocess.run) -> None:
    global _UPDATE_STATUS_CACHE, _UPDATE_STATUS_CACHE_AT, _UPDATE_STATUS_REFRESHING
    with _UPDATE_STATUS_CACHE_LOCK:
        if _UPDATE_STATUS_REFRESHING:
            return
        _UPDATE_STATUS_REFRESHING = True
    try:
        status = await asyncio.to_thread(_console_update_status, run_cmd)
        status = {
            **status,
            "checked_at": _utc_now_iso(),
            "cached": False,
            "stale": False,
            "refreshing": False,
        }
        with _UPDATE_STATUS_CACHE_LOCK:
            _UPDATE_STATUS_CACHE = status
            _UPDATE_STATUS_CACHE_AT = time.monotonic()
    finally:
        with _UPDATE_STATUS_CACHE_LOCK:
            _UPDATE_STATUS_REFRESHING = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_update_job_status(path: Path = UPDATE_JOB_STATUS_PATH) -> dict[str, Any]:
    if not path.exists():
        return {
            "status": "idle",
            "job_id": None,
            "pid": None,
            "started_at": None,
            "finished_at": None,
            "return_code": None,
            "log_path": None,
            "health_checks": [],
            "error": None,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "status": "unknown",
            "job_id": None,
            "pid": None,
            "started_at": None,
            "finished_at": None,
            "return_code": None,
            "log_path": str(path),
            "health_checks": [],
            "error": f"could not read update job status: {exc}",
        }
    return data if isinstance(data, dict) else {"status": "unknown", "error": "invalid job status"}


def _write_update_job_status(data: dict[str, Any], path: Path = UPDATE_JOB_STATUS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _pid_alive(pid: Any) -> bool:
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return False
    if p <= 0:
        return False
    try:
        os.kill(p, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _current_update_job_status() -> dict[str, Any]:
    status = _read_update_job_status()
    if status.get("status") == "running" and not _pid_alive(status.get("pid")):
        status = {
            **status,
            "status": "unknown",
            "finished_at": status.get("finished_at") or _utc_now_iso(),
            "error": status.get("error") or "update process is no longer running and did not write a final status",
        }
        _write_update_job_status(status)
    return status


def _runner_source(status_path: Path, log_path: Path, repo_root: Path, job_id: str) -> str:
    """Build the detached updater runner script.

    The script lives under ``local-cortex/logs`` and outlives the current console
    process. It runs ``./update.sh`` from the install root, appends all output to a
    job log, then writes final status for the restarted console to read.
    """
    return f"""\
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATUS_PATH = Path({str(status_path)!r})
LOG_PATH = Path({str(log_path)!r})
REPO_ROOT = Path({str(repo_root)!r})
JOB_ID = {job_id!r}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(**patch) -> None:
    try:
        current = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.exists() else {{}}
    except Exception:
        current = {{}}
    current.update(patch)
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(STATUS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def fetch(path: str) -> tuple[int | None, str, str | None, str]:
    port = os.environ.get("KAIDERA_CONSOLE_PORT", "8765")
    url = f"http://localhost:{{port}}{{path}}"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            raw = resp.read(4000).decode("utf-8", "replace")
            return int(resp.status), raw, None, url
    except Exception as exc:
        return None, "", str(exc), url


def check_http(name: str, path: str, needle: str | None = None, attempts: int = 3) -> dict:
    last: dict | None = None
    for attempt in range(1, attempts + 1):
        code, raw, err, url = fetch(path)
        ok = code == 200 and (needle is None or needle in raw)
        detail = f"HTTP {{code}}" if code else f"unreachable: {{err}}"
        if ok:
            return {{"name": name, "status": "ok", "detail": detail, "url": url, "checked_at": now()}}
        last = {{"name": name, "status": "failed", "detail": detail, "url": url, "checked_at": now()}}
        if attempt < attempts:
            time.sleep(2)
    return last or {{"name": name, "status": "unknown", "detail": "not checked", "checked_at": now()}}


def post_update_health_checks() -> list[dict]:
    checks = [
        check_http("Console version", "/console/version", '"version"', attempts=5),
        check_http("Console health", "/healthz", '"status"', attempts=3),
    ]
    code, raw, err, url = fetch("/cortex/admin-status")
    if code == 200 and '"status":"ok"' in raw:
        status, detail = "ok", "admin token ok"
    elif code == 200:
        status, detail = "failed", raw[:240] or "unexpected admin status"
    else:
        status, detail = "failed", f"unreachable: {{err}}"
    checks.append({{"name": "Cortex admin status", "status": status, "detail": detail, "url": url, "checked_at": now()}})
    return checks


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_status(status="running", job_id=JOB_ID, pid=os.getpid(), started_at=now(), log_path=str(LOG_PATH), error=None)
    try:
        with LOG_PATH.open("ab") as log:
            log.write((f"== Kaidera OS update {{JOB_ID}} started at {{now()}} ==\\n").encode())
            log.flush()
            proc = subprocess.run(
                ["bash", "./update.sh"],
                cwd=str(REPO_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                check=False,
            )
            rc = int(proc.returncode)
            log.write((f"== Kaidera OS update {{JOB_ID}} finished rc={{rc}} at {{now()}} ==\\n").encode())
        health_checks = post_update_health_checks()
        write_status(
            status="succeeded" if rc == 0 else "failed",
            return_code=rc,
            finished_at=now(),
            health_checks=health_checks,
            error=None if rc == 0 else f"update.sh exited {{rc}}",
        )
        return rc
    except Exception as exc:
        write_status(status="failed", return_code=1, finished_at=now(), error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _start_update_apply_job(popen=subprocess.Popen) -> dict[str, Any]:
    current = _current_update_job_status()
    if current.get("status") == "running" and _pid_alive(current.get("pid")):
        return {"accepted": False, "already_running": True, "job": current}

    update_script = REPO_ROOT / "update.sh"
    if not update_script.exists():
        status = {
            "status": "failed",
            "job_id": None,
            "pid": None,
            "started_at": None,
            "finished_at": _utc_now_iso(),
            "return_code": 127,
            "log_path": None,
            "health_checks": [],
            "error": f"update script not found at {update_script}",
        }
        _write_update_job_status(status)
        return {"accepted": False, "already_running": False, "job": status}

    UPDATE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    log_path = UPDATE_LOG_DIR / f"update-{job_id}.log"
    runner_path = UPDATE_LOG_DIR / f"update-runner-{job_id}.py"
    status = {
        "status": "starting",
        "job_id": job_id,
        "pid": None,
        "started_at": _utc_now_iso(),
        "finished_at": None,
        "return_code": None,
        "log_path": str(log_path),
        "health_checks": [],
        "error": None,
        "command": "./update.sh",
    }
    _write_update_job_status(status)
    runner_path.write_text(
        _runner_source(UPDATE_JOB_STATUS_PATH, log_path, REPO_ROOT, job_id),
        encoding="utf-8",
    )

    try:
        proc = popen(
            [sys.executable, str(runner_path)],
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    except OSError as exc:
        status = {
            **status,
            "status": "failed",
            "finished_at": _utc_now_iso(),
            "return_code": 1,
            "error": str(exc),
        }
        _write_update_job_status(status)
        return {"accepted": False, "already_running": False, "job": status}

    status = {**status, "status": "running", "pid": getattr(proc, "pid", None)}
    _write_update_job_status(status)
    return {"accepted": True, "already_running": False, "job": status}


@app.get("/console/update-status")
async def console_update_status_json(refresh: bool = False) -> dict[str, Any]:
    """JSON: advisory signed-release update status for the SPA badge.

    This is intentionally a read-only notification surface. Applying an update
    restarts the console, so the one-click apply path needs a separate async job
    contract; until then the endpoint tells operators when ``./update.sh`` should
    be run.
    """
    status, should_refresh = _update_status_cache_snapshot(refresh=refresh)
    if should_refresh:
        asyncio.create_task(_refresh_update_status_cache(), name="update-status-refresh")
    return status


@app.get("/console/update-job")
async def console_update_job_json(
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    """JSON: latest update-apply job state.

    Admin-gated when auth is enabled because it exposes local log paths and update
    process metadata. Auth-off local mode remains open, matching other operator
    mutation/status surfaces.
    """
    return await asyncio.to_thread(_current_update_job_status)


@app.post("/console/update/apply")
async def console_update_apply_json(
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> JSONResponse:
    """Start a detached signed-release update job.

    The job runs ``./update.sh`` from the install root, which may update the app,
    rebuild/recreate Cortex services, run migrations, and restart the console. This
    route returns ``202`` as soon as the external runner is launched; callers should
    poll ``GET /console/update-job`` after the console comes back.
    """
    result = await asyncio.to_thread(_start_update_apply_job)
    status_code = 202 if result.get("accepted") or result.get("already_running") else 500
    return JSONResponse(result, status_code=status_code)


@app.get("/cortex/health")
async def cortex_health_json(
    request: Request, project: str | None = None
) -> dict:
    """JSON: the live Cortex health + connection info (the SPA Cortex tab's read-out).

    THE GAP THIS CLOSES: the SPA Cortex tab needs a same-origin JSON health read,
    but the console historically exposed only the HTMX `GET /health-pill` HTML
    partial — there was NO JSON health route, so the tab's `fetch('/health')` 404'd
    and it always rendered "unreachable" even though the container reaches Cortex
    fine. This is that missing JSON surface, at a clean non-colliding path.

    SOURCE — the SAME place the HTML pill reads: `CortexClient.get_health()` (the
    live `GET /health` on the loopback Cortex). We fold in the connection info the
    tab wants — `base_url` (the client's configured base) + the echoed `project` —
    and pass the health dict's surface fields (`status`, `surface_version`,
    `event_backend`, `rls_enforced`) through unchanged.

    COLLISION-FREE: `/cortex/health` is a brand-new `/cortex/...` path family no
    existing route owns, and it is NOT under the `/app` SPA static mount, so it can
    neither shadow nor be shadowed (different prefix). It returns a plain dict →
    FastAPI serialises JSON (default JSONResponse), distinct from the HTMLResponse
    `/health-pill` partial.

    GRACEFUL-DEGRADE: `get_health()` already returns a synthetic
    `{"status": "unreachable", ...}` dict on a transport error (it never raises), so
    a down Cortex yields THAT here (the connection fields still present) — HTTP 200,
    never a 500. The tab then shows a REAL "unreachable" off a genuinely reachable
    console endpoint, not a 404."""
    cortex = _cortex(request)
    health = await cortex.get_health()
    # Fold the connection info onto the health dict the tab renders. The health
    # surface fields (status / surface_version / event_backend / rls_enforced) ride
    # through unchanged; the connection fields are added (overriding any same-named
    # key from the health payload is fine — base_url/project are console-side facts).
    out = dict(health) if isinstance(health, dict) else {"status": "unreachable"}
    out["base_url"] = cortex.base_url
    out["project"] = (project or "").strip() or _default_project()
    return out


@app.get("/cortex/admin-status")
async def cortex_admin_status(request: Request) -> dict:
    """JSON: does the console's Cortex ADMIN TOKEN work? — the SAME require-admin gate that
    project registration hits. The Settings → Cortex tab renders this so a token MISMATCH is
    visible UP FRONT (not as a cryptic failure the first time you create a project). Read-only
    probe via `CortexClient.verify_admin` (GET /beat/roles); HTTP 200 always; the FULL token
    never leaves the server — only a masked hint + the status.

    `status`: ok | mismatch | no_token | unreachable. `masked`: e.g. "••••••••a1b2". `env_path`:
    where the operator sets/rotates it (the single source both the console + cortex-api read)."""
    cortex = _cortex(request)
    status = await cortex.verify_admin()
    return {
        "status": status,
        "configured": status != "no_token",
        "works": status == "ok",
        "masked": masked_admin_token(),
        "env_path": "local-cortex/.env",
    }


@app.get("/cortex/config")
async def cortex_config_json(
    request: Request,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict:
    """JSON: effective Cortex platform config.

    This proxies the Cortex API admin config route backend-side so the browser can
    edit ingestion model settings without ever seeing the admin token. A missing
    token or unreachable Cortex is a soft config error in the payload, not a page
    crash.
    """
    try:
        config = await _cortex(request).get_platform_config()
        return {"ok": True, "config": config, "error": None}
    except AdminTokenMissing:
        return {
            "ok": False,
            "config": {},
            "error": "admin token not configured — set CORTEX_ADMIN_TOKEN in local-cortex/.env",
        }
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:400] if exc.response is not None else ""
        return {
            "ok": False,
            "config": {},
            "error": f"Cortex rejected the config read ({exc.response.status_code}){': ' + detail if detail else ''}",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "config": {}, "error": f"couldn't reach Cortex: {exc}"}


@app.post("/cortex/config")
async def cortex_config_update_json(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict:
    """JSON: patch Cortex platform config through the admin API.

    Accepts a small `{config: {...}}` or direct patch body. The Cortex API validates
    patchable columns; this route only keeps the admin token server-side and maps
    failures to soft JSON.
    """
    patch = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    try:
        config = await _cortex(request).update_platform_config(dict(patch or {}))
        return {"ok": True, "config": config, "error": None}
    except AdminTokenMissing:
        return {
            "ok": False,
            "config": {},
            "error": "admin token not configured — set CORTEX_ADMIN_TOKEN in local-cortex/.env",
        }
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:400] if exc.response is not None else ""
        return {
            "ok": False,
            "config": {},
            "error": f"Cortex rejected the config update ({exc.response.status_code}){': ' + detail if detail else ''}",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "config": {}, "error": f"couldn't reach Cortex: {exc}"}


@app.get("/cortex/embeddings/backlog")
async def cortex_embeddings_backlog_json(
    request: Request,
    project: str | None = None,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict:
    """JSON: project embedding coverage/backlog through cortex-api.

    This is the operator visibility half of Cortex vector-space hardening. The
    browser never sees the admin token; the console proxies the existing
    `/beat/embeddings/backlog` API and maps transport/auth failures to a soft
    payload so Settings can show the problem without crashing.
    """
    project_key = (project or "").strip() or _default_project()
    try:
        data = await _cortex(request).get_embedding_backlog(project_key)
        return {
            "ok": True,
            "project": project_key,
            "backlog": data.get("backlog") or {},
            "coverage": data.get("coverage") or {},
            "error": None,
        }
    except AdminTokenMissing:
        return {
            "ok": False,
            "project": project_key,
            "backlog": {},
            "coverage": {},
            "error": "admin token not configured — set CORTEX_ADMIN_TOKEN in local-cortex/.env",
        }
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:400] if exc.response is not None else ""
        return {
            "ok": False,
            "project": project_key,
            "backlog": {},
            "coverage": {},
            "error": f"Cortex rejected the embedding backlog read ({exc.response.status_code}){': ' + detail if detail else ''}",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "project": project_key,
            "backlog": {},
            "coverage": {},
            "error": f"couldn't reach Cortex: {exc}",
        }


@app.post("/cortex/embeddings/backfill")
async def cortex_embeddings_backfill_json(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    project: str | None = None,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict:
    """JSON: start or dry-run an embedding backfill via cortex-api."""
    body = dict(payload.get("request") if isinstance(payload.get("request"), dict) else payload)
    project_key = str(body.pop("project", "") or project or "").strip() or _default_project()
    try:
        data = await _cortex(request).backfill_embeddings(project_key, body)
        return {"ok": True, "project": project_key, "result": data, "error": None}
    except AdminTokenMissing:
        return {
            "ok": False,
            "project": project_key,
            "result": {},
            "error": "admin token not configured — set CORTEX_ADMIN_TOKEN in local-cortex/.env",
        }
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:400] if exc.response is not None else ""
        return {
            "ok": False,
            "project": project_key,
            "result": {},
            "error": f"Cortex rejected the embedding backfill ({exc.response.status_code}){': ' + detail if detail else ''}",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "project": project_key,
            "result": {},
            "error": f"couldn't reach Cortex: {exc}",
        }


@app.get("/cortex/embeddings/jobs/{job_id}")
async def cortex_embedding_backfill_job_json(
    request: Request,
    job_id: str,
    project: str | None = None,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict:
    """JSON: poll one embedding backfill job through cortex-api."""
    project_key = (project or "").strip() or _default_project()
    try:
        data = await _cortex(request).get_embedding_backfill_job(project_key, job_id)
        return {"ok": True, "project": project_key, "job": data, "error": None}
    except AdminTokenMissing:
        return {
            "ok": False,
            "project": project_key,
            "job": {},
            "error": "admin token not configured — set CORTEX_ADMIN_TOKEN in local-cortex/.env",
        }
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:400] if exc.response is not None else ""
        return {
            "ok": False,
            "project": project_key,
            "job": {},
            "error": f"Cortex rejected the embedding job read ({exc.response.status_code}){': ' + detail if detail else ''}",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "project": project_key,
            "job": {},
            "error": f"couldn't reach Cortex: {exc}",
        }


@app.get("/projects/{project_key}", response_class=HTMLResponse)
async def project_swap(request: Request, project_key: str) -> HTMLResponse:
    """HTMX partial: the agents column + center for one project, returned on a
    project switch (rail row OR fleet-card click) and on the scope poll.

    Scoping a project re-renders col-2 (that project's agents · metrics · epic) and
    defaults the CENTER to the project's lead agent pane (the Dashboard is the
    cross-project overview, shown only when explicitly selected; we fall back to it if the
    project has no agent to land on). One canonical builder (`_agent_center_context`) renders
    that center for both this switch and the agent route — no second path to drift.
    Carries data-selected-key on its root so the rail re-syncs its highlight."""
    cortex = _cortex(request)
    project_ctx = await _project_context(cortex, project_key)
    # Default the CENTER to the project's lead agent pane — NOT the Dashboard. The
    # Dashboard is the cross-project overview, shown only when explicitly selected. Falls
    # back to the fleet/Dashboard if the project has no agent to land on.
    lead = _lead_agent_name(project_ctx.get("agent_groups") or {})
    center_ctx = await _agent_center_context(request, cortex, project_key, lead) if lead else None
    if center_ctx is None:
        center_ctx = await _fleet_context(cortex, project_key)
    return templates.TemplateResponse(
        request,
        "_scope.html",
        {**project_ctx, **center_ctx, "selected_key": project_key},
    )


@app.get("/projects/{project_key}/detail", response_class=HTMLResponse)
async def project_detail(request: Request, project_key: str) -> HTMLResponse:
    """HTMX partial: the per-project DRILL-IN — swap the CENTER region to one
    project's full open-handoffs + tasks LISTS.

    The all-projects Dashboard shows only COUNTS; this is the click-IN (from a
    dashboard card's 'details' affordance or the col-2 Metrics 'details ›' link)
    that opens the detailed per-project view the Dashboard lacks: every OPEN
    handoff (summary · from→to · priority · status · created) and every task
    (title · assigned · status · priority), with a header vitals strip.

    Returns the `_center.html` shell on the `project_detail` branch. The
    Dashboard itself is left as-is — this is purely the drill-IN. An unknown
    project still renders (empty lists + an inline 'unknown project' note) rather
    than erroring."""
    cortex = _cortex(request)
    ctx = await _project_detail_context(cortex, project_key)
    return templates.TemplateResponse(request, "_center.html", ctx)


# NOTE (2026-06-03): #3 durable post-restart run-history reconstruction was REMOVED. It searched
# Cortex (~2s) on every agent-pane load when the in-memory store was empty, which made every
# project/agent switch slow — and a per-load cache didn't help when exploring different agents.
# Deferred to a future *streaming* history approach. The agent pane shows the live in-memory
# transcript store (and enriches an in-store run from Cortex); it does NOT reconstruct history
# after a restart.


def _lead_agent_name(agent_groups: dict) -> str | None:
    """[delegates to app.agents] The project's lead agent name — the default
    landing pane on a project switch. Prefers the interactive agent carrying the lead
    tag, else the first interactive agent, else None. Reuses the already-classified
    col-2 groups, so it never re-derives designations."""
    return _agents_service.lead_agent_name(agent_groups)


async def _agent_center_context(
    request: Request, cortex: CortexClient, project_key: str, agent_name: str, run: str | None = None
) -> dict | None:
    """Build the agent-detail CENTER context for one agent (header + live transcript +
    activity timeline + composer), enriched from Cortex. THE one canonical builder — used by
    both the agent route and the project-switch default-pane, so there is no second path to
    drift. Returns None when the agent isn't in the project (caller falls back to the Dashboard)."""
    project, agents, history, catalog, pi_catalog_groups = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_agents(project_key),
        cortex.get_history(project_key),
        providers_catalog.get_catalog(),
        _fetch_pi_catalog_groups(),
    )
    agent = _find_agent(agents, agent_name)
    if agent is None:
        return None
    catalog_groups = providers_catalog.view_catalog(catalog).get("groups", [])
    # LIVE WORK TRANSCRIPT read model = the RunState SSOT store (Milestone 1 T7/T12),
    # the ONE live-state path: the durable run_state row + run_span spans + heartbeat
    # the worker writes. Read it here (async) and hand the ready context to the sync
    # ``_agent_detail_view``. Graceful-degrade to the empty state if the store is down
    # (the in-memory fallback was removed at T12 — a pre-store run just won't show).
    runs_ctx = await _agent_runs_view_store(
        _runstate(request), project_key, agent_name, run_id=run,
    )
    detail = _agent_detail_view(
        agent, project, project_key, history, catalog_groups,
        pi_catalog_groups=pi_catalog_groups,
        orch=_orchestrator(request), run_id=run, runs_ctx=runs_ctx,
    )
    # _enrich_run_from_cortex (the ~2s Cortex re-grep) is DELETED: the worker writes
    # spans directly to the store, so the durable transcript is read above. Live
    # appends arrive via the T8 RunState SSOT push (EventSource on
    # GET /runstate/stream) — the per-agent feed file + /feed-stream were removed at T12.
    return {"active_view": "agent", "selected": project, "selected_key": project_key, **detail}


@app.get("/agents/{project_key}/{agent_name}", response_class=HTMLResponse)
async def agent_detail(
    request: Request, project_key: str, agent_name: str, run: str | None = None
) -> HTMLResponse:
    """HTMX partial: swap the CENTER region to a selected agent's detail view.

    Returns the agent-detail center: the header (compound id + harness · model ·
    reasoning + token-usage readout), the agent's LIVE WORK TRANSCRIPT (its
    streamed run output — the prominent main content, filtered to THIS agent out
    of the orchestrator's transcript store), its durable ACTIVITY TIMELINE (Cortex
    /history decisions + tool-use), and the live chat composer. Works identically
    for EVERY agent — interactive lead or autonomous (the AI workers) — not just
    the one you talk to. The agents column (col 2) is NOT re-rendered — it stays
    put and re-highlights the clicked row client-side via the data-selected-agent
    marker on the swapped center root.

    ``run`` pins a specific transcript run id (the pane's ~2s self-poll passes it
    so a RUNNING run keeps filling in live without jumping to the newest each tick).

    If the agent isn't found in the project's runtime/roster, the center falls
    back to the Dashboard (defensive — an unknown name shouldn't blank the UI)."""
    cortex = _cortex(request)
    ctx = await _agent_center_context(request, cortex, project_key, agent_name, run=run)
    if ctx is None:
        # Unknown agent → degrade to the Dashboard rather than an empty pane.
        return templates.TemplateResponse(
            request, "_center.html", await _fleet_context(cortex, project_key)
        )
    return templates.TemplateResponse(request, "_center.html", ctx)


@app.post("/agents/{project_key}/{agent_name}/config", response_class=HTMLResponse)
async def agent_config_save(
    request: Request, project_key: str, agent_name: str
) -> HTMLResponse:
    """Persist ONE agent's harness/model/reasoning override from the INLINE header
    dropdowns (the CTO's original spec), then swap the header's editable sub-line
    back with the new effective state + a subtle "saved" pill.

    ONE SOURCE OF TRUTH: this calls the SAME app.settings.save_agent_override the
    Settings → Configure card uses (POST /settings/configure), writing the SAME
    app-DB-backed `agent_overrides["{project}:{agent}"]` overlay — so a change here
    shows in Configure and vice-versa (the store is never forked). A blank field
    CLEARS that field's override (falls back to the registry value).

    Reads the urlencoded form body WITHOUT python-multipart (same zero-extra-deps
    approach as the System/workspace/Configure saves). Re-resolves the agent so the
    swapped sub-line shows the post-save EFFECTIVE config + repopulated model/
    reasoning option sets (kaidera/pi pull the providers catalog).

    CONSOLE-LOCAL BY DESIGN (feature-gap #81, the CTO's reversed decision): this save
    writes ONLY the console-local override — it does NOT push to the Cortex registry
    (the registry stays authoritative). Committing the config to the registry is an
    EXPLICIT, on-demand gesture via the SPA's "Promote to registry" action
    (`POST /settings/{project}/agents/{agent}/promote`), never automatic on save."""
    cortex = _cortex(request)
    form = await _read_posted_form(request)

    # Persist the override FIRST (only harness/model/reasoning from this surface;
    # designation/role stay owned by the Configure card). A field absent from the
    # form is NOT touched; a present-but-blank field clears that override.
    save_error: str | None = None
    cfg_fields = ("harness", "model", "reasoning")
    override = {k: form.get(k, "") for k in cfg_fields if k in form}
    try:
        settings_store.save_agent_override(project_key, agent_name, override)
        saved = True
    except OSError as exc:
        saved = False
        save_error = f"write failed: {exc}"

    # Re-resolve everything the inline sub-line needs to re-render at the new
    # effective state (override now applied), reusing the detail view shaping.
    project, agents, catalog, pi_catalog_groups = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_agents(project_key),
        providers_catalog.get_catalog(),
        _fetch_pi_catalog_groups(),
    )
    agent = _find_agent(agents, agent_name)

    # NOTE (feature-gap #81): this save is console-LOCAL only — it does NOT push to the
    # Cortex registry. Promotion to the registry is the explicit "Promote to registry"
    # action (POST /settings/{project}/agents/{agent}/promote), never automatic on save.
    if agent is None:
        # Unknown agent — return a minimal error sub-line (defensive; the header
        # only POSTs for a known agent it just rendered).
        return templates.TemplateResponse(
            request,
            "_agent_detail_config.html",
            {
                "selected_key": project_key,
                "agent": {"name": agent_name},
                "agent_compound": agent_name,
                "ad_cfg": harness_cfg.agent_config_view({"name": agent_name}, {}, []),
                "ad_harness_options": harness_cfg.harness_options(),
                "ad_saved": False,
                "ad_save_error": f"unknown agent '{agent_name}'",
            },
        )

    catalog_groups = providers_catalog.view_catalog(catalog).get("groups", [])
    detail = _agent_detail_view(
        agent, project, project_key, [], catalog_groups,
        pi_catalog_groups=pi_catalog_groups,
    )
    return templates.TemplateResponse(
        request,
        "_agent_detail_config.html",
        {
            "selected_key": project_key,
            "agent": detail["agent"],
            "agent_compound": detail["agent_compound"],
            "ad_cfg": detail["ad_cfg"],
            "ad_harness_options": detail["ad_harness_options"],
            "ad_saved": saved,
            "ad_save_error": save_error,
        },
    )


def _valid_client_run_id(value: str | None, *, existing: set[str] | None = None) -> str | None:
    """Validate a client-supplied run id (chat file-attachments, step 6).

    The SPA pre-mints ONE uuid4 it uses for BOTH the attachment upload(s) and the chat
    send, so the bytes land under the SAME run the turn writes to. We accept it ONLY when
    it is a genuine uuid4 (version-4) AND not already an existing run id (so a client
    can't hijack / overwrite another run's row) — otherwise the route mints its own. A
    blank / malformed / path-y value returns None (→ mint). Pure + total: any parse error
    is None, never a raise."""
    rid = (value or "").strip()
    if not rid:
        return None
    try:
        parsed = uuid.UUID(rid)
    except (ValueError, AttributeError, TypeError):
        return None
    # Require version 4 AND an exact canonical round-trip (rejects odd-but-parseable
    # inputs like a braced/urn form, so the id we store is exactly what we validated).
    if parsed.version != 4 or str(parsed) != rid.lower():
        return None
    if existing and rid in existing:
        return None
    return rid


@app.post("/agents/{project_key}/{agent_name}/chat/upload")
async def agent_chat_upload(
    request: Request, project_key: str, agent_name: str
) -> JSONResponse:
    """Receive ONE chat attachment (feature-gap step 6, Inc A — the upload route).

    The SPA base64-encodes a picked file and POSTs JSON `{run_id, filename, content_type,
    data}` here (NO python-multipart — base64-in-JSON preserves the no-multipart
    discipline `_read_posted_form` already follows). The bytes are confined + written via
    `attachments.receive_upload`, which runs the sandbox gate (an escaping filename / an
    oversized body is rejected and NOTHING is written). We return `{attachment_id,
    filename, size_bytes}` — the minted id the SPA echoes on the chat send — and NEVER the
    host path (the absolute on-disk location must not cross to the client).

    `run_id` is the SPA's pre-minted `client_run_id` (the SAME id it sends on the chat
    POST), so the uploaded files group under the run the turn writes to. This route does
    NOT touch Cortex / the run-state store (it only writes the sandbox file); the chat
    send wires the attachment into the prompt + persists the span."""
    body = await _read_posted_form(request)
    run_id = (body.get("run_id") or "").strip()
    filename = (body.get("filename") or "").strip()
    content_type = (body.get("content_type") or "application/octet-stream").strip()
    data_b64 = body.get("data") or ""

    if not run_id:
        return JSONResponse({"error": "run_id is required"}, status_code=400)
    if not filename:
        return JSONResponse({"error": "filename is required"}, status_code=400)
    if not data_b64:
        return JSONResponse({"error": "data is required"}, status_code=400)

    try:
        meta = attachments_module.receive_upload(run_id, filename, data_b64, content_type)
    except attachments_module.AttachmentError as exc:
        # An escape (403) / a bad-or-oversized body (400) — the gate's own status.
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    except Exception as exc:  # pragma: no cover - defensive: never 500 the client
        return JSONResponse({"error": f"upload failed: {exc}"}, status_code=400)

    # ONLY the client-safe fields — never the host_path (server-side only).
    return JSONResponse(
        {
            "attachment_id": meta.attachment_id,
            "filename": meta.filename,
            "size_bytes": meta.size_bytes,
        }
    )


@app.post("/agents/{project_key}/{agent_name}/chat")
async def agent_chat(
    request: Request, project_key: str, agent_name: str
) -> EventSourceResponse:
    """LIVE harness chat (R2b) — spawn claude-code on the user's SUBSCRIPTION and
    stream the agent's reply back to the composer as Server-Sent Events.

    The composer POSTs `{message}` (urlencoded or JSON). We resolve the agent's
    effective CLAUDE model (override-first; non-claude harnesses fall back to the
    runner default — R2b routes everything through claude-code, TODO(harness-
    routing) for codex/kaidera/pi), build a light one-line system framing
    from the agent's role, and hand off to `harness_runner.stream_chat`.

    The runner's event dicts (session / delta / result / error / done) are mapped
    onto SSE frames the browser's `fetch`+ReadableStream reader appends to the
    feed (see _agent_detail.html). Events:
      * `event: delta`    data: {"text": "..."}      — append streamed text
      * `event: tasks`    data: [{"content","status"}] — the agent's task list
                                                       (TodoWrite) for the N/total indicator
      * `event: subagent` data: {"label"}            — a sub-agent spawn (Task)
      * `event: error`    data: {"message","category"} — show an error bubble
      * `event: done`     data: {}                    — close the stream
    (session/result frames are folded into the stream but not separately
    rendered; result carries the assembled text only if no deltas streamed.
    tool/thinking/tasks/subagent frames are visibility-only.)

    This route does NOT touch Cortex (read-only console invariant holds) — it only
    spawns the local claude-code CLI. Auth is the logged-in subscription; we never
    pass an API key (the runner strips ANTHROPIC_API_KEY from the child env)."""
    cortex = _cortex(request)
    form = await _read_posted_form(request)
    message = (form.get("message") or "").strip()
    # MULTI-TURN CONTEXT (feature-gap step 6, Inc B): the SPA mints a stable per-
    # conversation session_id and sends it on every chat POST. Absent (legacy
    # `api.chat(project, agent, message)` / the HTML composer) → None → single-shot,
    # identical to today. Blank-normalised so an empty field is treated as no session.
    session_id = (form.get("session_id") or "").strip() or None
    # CHAT FILE-ATTACHMENTS (feature-gap step 6, Inc A): the SPA pre-mints ONE uuid4
    # `client_run_id` it uses for BOTH the attachment upload(s) and this send, and sends
    # the uploaded attachment ids as a CSV. Both are OPTIONAL + ADDITIVE — absent (legacy
    # composer / no attachments) → no client run id, no attachments, byte-for-byte the
    # existing path. The ids are parsed defensively (blank / odd entries dropped).
    client_run_id = form.get("client_run_id")
    attachment_ids = [
        a.strip() for a in (form.get("attachment_ids") or "").split(",") if a.strip()
    ]

    # Resolve the agent so the model + system framing match the detail view.
    project, agents = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_agents(project_key),
    )
    agent = _find_agent(agents, agent_name)
    # The SELECTED project's repo_root — the workspace the harness + cortex-boot run
    # IN, so a chat with an agent runs in that project's folder + project scope
    # (not the console process workspace). See _apply_project_workspace.
    chat_workspace = (project or {}).get("repo_root") or None  # fitness:allow-literal "repo_root" is a real wire/dict key, not a project literal (false match on 'root')

    if agent is None:
        async def _unknown() -> AsyncGenerator[dict[str, str], None]:
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": f"Unknown agent '{agent_name}' in project '{project_key}'.",
                     "category": "unknown_agent"}
                ),
            }
            yield {"event": "done", "data": "{}"}
        return EventSourceResponse(_unknown())

    ov = settings_store.get_agent_override(project_key, agent.get("name") or "")
    designation = settings_store.normalize_designation(ov.get("designation"))
    agent_view = _agent_view(agent, designation, ov.get("role", ""), override=ov)
    compound = f"{agent_view['name']}@{project_key}"

    # DESIGNATION GUARD — chat is INTERACTIVE-ONLY (mirrors app.domain.designation:
    # is_chat_enabled). A non-interactive AI worker (autonomous) and a deterministic
    # agent have no chat surface — their runs stream in the feed instead, and a
    # deterministic agent has no model at all. The SPA already hides the composer for
    # these; this refuses a DIRECT API call with a clear error rather than spawning an
    # LLM turn. Interactivity resolves override-first → registry (same as the grouping).
    if not _classify_interactive(agent, designation):
        _is_det = designation == "deterministic"
        _label = agent_view.get("display_name") or agent_name

        async def _no_chat() -> AsyncGenerator[dict[str, str], None]:
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "message": (
                            f"'{_label}' is a deterministic agent (no model attached) — it can't "
                            "be chatted; it runs on a schedule or trigger."
                            if _is_det
                            else f"'{_label}' is a non-interactive AI worker — it runs "
                            "autonomously and has no chat. Set its designation to Interactive to "
                            "chat with it."
                        ),
                        "category": "not_interactive",
                    }
                ),
            }
            yield {"event": "done", "data": "{}"}

        return EventSourceResponse(_no_chat())

    # Resolve the agent's CONFIGURED harness + model (override-first; an
    # unconfigured agent → claude-code / Opus 4.8 (1M) / max). The runner spawns
    # exactly this harness — claude-code/codex/pi/kaidera are real lanes.
    harness, model, _reasoning = _chat_routing_for(agent, project_key)
    # Build the FULL system prompt (persona + delivered skills) OFF the event loop:
    # both halves shell out to `cortex-boot`, so doing it inline would freeze the loop
    # (and every SSE heartbeat / run-state NOTIFY) for the boot's duration each turn.
    system = await asyncio.to_thread(
        _build_chat_system,
        agent_view, compound, project_key, agent_view.get("name") or agent_name, message,
        chat_workspace,
    )

    # RunState SSOT store (Milestone 1 T10): an interactive chat now writes to the
    # SAME durable store an autonomous run does, so it shows up on the T8
    # `/runstate/stream` pane alongside autonomous runs. A chat has NO handoff, so —
    # UNLIKE the worker / Approve & Run — there is nothing to claim or complete; we
    # only open a `run_state` row (lease_owner='chat', handoff_id=None) and walk it
    # running → ok | error. The lease_owner='chat' marker is load-bearing: the
    # watchdog (T11) treats 'approve_run'/'chat'-style in-process runs as
    # REQUEST-LIVED (terminal status is the completion signal, NOT heartbeat age) —
    # an interactive chat has no separate worker PID and never heartbeats.
    # Graceful-degrade (house law): a None / down store leaves run-state unwritten but
    # the chat still streams (only durable run-state is lost). Pre-create the run_id up
    # front so the SSE 'run' frame, the store row, and every span/status share ONE id.
    store = _runstate(request)
    chat_agent = agent_view.get("name") or agent_name
    # Use the SPA's pre-minted `client_run_id` (a uuid4) when valid + not an existing run,
    # so the attachment upload(s) and this turn share ONE id (the files land under the
    # run we write to); otherwise mint our own (the legacy path). The validation rejects a
    # malformed / path-y / non-v4 id, so the id we open the row under is always a clean
    # uuid4 — no client-controlled string ever reaches the store key.
    run_id = _valid_client_run_id(client_run_id) or str(uuid.uuid4())

    # CHAT-DISPATCH SEAM (harness-service Increment 4 — ADDITIVE + FLAGGED). When the
    # console is CONTAINERIZED it carries no harness CLIs, so the in-process
    # `stream_chat` below cannot work. With `HARNESS_SPAWN_MODE=remote` AND a
    # HarnessPort wired (the SAME factory the orchestrator's worker-spawn uses), we POST
    # the chat turn to the HOST harness-service (`spawn_chat` → `POST /chat`), which
    # shells the host chat runner; that runner writes the reply to the SAME run_state
    # row we pre-create here, and the UI reads it via the existing `/runstate/stream`
    # SSE — so the chat surface keeps working in a container with NO new client path.
    # The flag (not the mere presence of a port) gates the seam: unset/local/legacy →
    # the EXISTING in-process path runs byte-for-byte (zero behaviour change). A wired
    # port that REJECTS the spawn (accepted=False — e.g. the local adapter's no-op, or
    # the host service down) also falls back to in-process, so the chat never silently
    # dies. This mirrors `_dispatch_run`'s `harness_port` fork exactly.
    _spawn_mode = os.environ.get("HARNESS_SPAWN_MODE", "").strip().lower()
    _harness_port = getattr(request.app.state, "harness_port", None)
    _chat_remote = _spawn_mode == "remote" and _harness_port is not None

    # CHAT FILE-ATTACHMENTS (feature-gap step 6, Inc A): resolve this turn's uploaded
    # attachments — the files the upload route wrote into the run's sandbox under the
    # SHARED `client_run_id`. In LOCAL/in-process mode the console IS on the host, so the
    # sandbox host paths are inlined straight into the prompt. In REMOTE mode the bytes
    # live in the CONTAINER's sandbox, so each is forwarded to the HOST via
    # `upload_attachment` (a down upload degrades to "" and that attachment is dropped —
    # the turn still sends). `_attachment_local_paths` are the in-process host paths;
    # they're empty when no attachment was uploaded (→ no behaviour change). The
    # filenames feed the attachment SPAN (so the transcript shows a chip). Best-effort:
    # any resolution hiccup leaves the lists empty and the turn proceeds attachment-free.
    _attachment_local_paths: list[str] = []
    _attachment_names: list[str] = []
    if attachment_ids:
        with suppress(Exception):
            _paths = attachments_module.list_run_attachments(run_id)
            _attachment_local_paths = [str(p) for p in _paths]
            _attachment_names = [p.name for p in _paths]

    async def _events() -> AsyncGenerator[dict[str, str], None]:
        """Bridge runner events → SSE frames. Each SSE frame's `data` is JSON so
        the client parses uniformly; `event` names the frame type.

        ADDITIVE store cycle (T10): open a chat run row, mark it running, append each
        streamed span, and set the terminal status (ok/error). Every store call is
        best-effort (the `_rs_*` wrappers swallow failures) — a down store must never
        crash the chat; the reply still streams regardless."""
        # Open the run_state row (lease_owner='chat', NO handoff) and tell the pane
        # which run to follow. Best-effort: a down store leaves the row unwritten but
        # the chat proceeds. The 'run' frame carries the run_id either way so the T8
        # pane can subscribe to ?run=<id>.
        if store is not None:
            try:
                await store.start_run(
                    run_id=run_id,
                    project=project_key,
                    agent=chat_agent,
                    agent_display=agent_view.get("display_name") or chat_agent,
                    handoff_id=None,
                    harness=harness,
                    model=model,
                    pid=None if _chat_remote else os.getpid(),
                    lease_owner="chat",
                    session_id=session_id,
                )
            except Exception:
                pass
        yield {"event": "run", "data": json.dumps({"run_id": run_id})}

        # Store helpers (best-effort; mirror dispatch_run / run_agent.run_one's _rs_*).
        async def _rs_status(status: str, **kw: Any) -> None:
            if store is None:
                return
            with suppress(Exception):
                await store.set_status(run_id, status, **kw)

        async def _rs_totals(*, tokens_in: Any, tokens_out: Any, cost_est_usd: Any) -> None:
            """Stamp the run's FINAL token/cost totals on the run header via ``heartbeat``
            (the RunStatePort home for tokens/cost — ``set_status`` takes only status/
            error/metadata). Passing tokens to ``set_status`` raised a swallowed TypeError
            that pinned the row at 'running'. Best-effort, like every store write."""
            if store is None:
                return
            with suppress(Exception):
                await store.heartbeat(
                    run_id, tokens_in=tokens_in, tokens_out=tokens_out,
                    cost_est_usd=cost_est_usd, pid=os.getpid(),
                )

        seq = 0

        async def _rs_span(kind: str, text: str) -> None:
            nonlocal seq
            if store is None or not text:
                return
            seq += 1
            with suppress(Exception):
                await store.append_output(run_id, seq=seq, kind=kind, text=text)

        # REMOTE CHAT SEAM (I4): in remote mode, hand the turn to the host harness-
        # service instead of running stream_chat in-process. The host chat runner
        # writes the reply to THIS run_id's row; the UI reads it via /runstate/stream,
        # so we only emit a `done` to close the SSE (the row already carries the run).
        # FIRE-AND-FORGET: spawn_chat NEVER raises (a down service → accepted=False).
        # On ACCEPT → close the SSE and STOP (the in-process path is skipped). On
        # REJECT → mark the run errored + emit a clean error frame, then STOP — a
        # containerized console has NO local CLIs to retry with, so a remote reject is
        # an honest terminal error (graceful-degrade = no crash, not a silent retry).
        if _chat_remote:
            from app.domain.harness import ChatSpawnRequest

            # CHAT FILE-ATTACHMENTS (step 6, Inc A — the remote transport, touched LAST):
            # the container's upload route landed the bytes in ITS sandbox, but the HOST
            # chat runner needs them on the HOST disk. So for each uploaded file we POST
            # its base64 bytes to the host (`upload_attachment` → `POST /upload`) and
            # collect the host paths into `ChatSpawnRequest.attachment_paths` (the host
            # runner inlines them). FIRE-AND-FORGET: `upload_attachment` NEVER raises (a
            # down host-upload → "" → that attachment is dropped, the turn still sends).
            # No attachments → an empty list (the host runner takes the no-attachment
            # path, unchanged). We mint a fresh attachment_id per file for the host's
            # per-attachment subdir keying (the SPA's ids were for the container sandbox).
            _host_paths: list[str] = []
            if _attachment_local_paths and hasattr(_harness_port, "upload_attachment"):
                for _lp in _attachment_local_paths:
                    with suppress(Exception):
                        _data = await asyncio.to_thread(
                            lambda p=_lp: __import__("base64").b64encode(
                                Path(p).read_bytes()
                            ).decode("ascii")
                        )
                        _hp = await _harness_port.upload_attachment(
                            uuid.uuid4().hex, Path(_lp).name, _data
                        )
                        if _hp:
                            _host_paths.append(_hp)

            chat_req = ChatSpawnRequest(
                run_id=run_id,
                project=project_key,
                agent=chat_agent,
                message=message,
                harness=harness,
                model=model,
                reasoning=_reasoning,
                repo_root=chat_workspace,
                session_id=session_id,
                attachment_paths=_host_paths,
            )
            handle = await _harness_port.spawn_chat(chat_req)
            if getattr(handle, "accepted", False):
                # Dispatched host-side; the reply arrives via the pre-created run_state
                # row (the /runstate/stream pane is already following ?run=run_id).
                yield {"event": "done", "data": "{}"}
                return
            # Rejected (service down / no host seam): terminal error on the pre-created
            # row + a clean error frame; never raises, never silently dies.
            _rej = (getattr(handle, "error", None) or "chat dispatch rejected")
            await _rs_status("error", error=str(_rej))
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": f"Remote chat unavailable: {_rej}.",
                     "category": "chat_dispatch_rejected"}
                ),
            }
            yield {"event": "done", "data": "{}"}
            return

        # LOCAL CHAT: use the same durable runner core the host-service path uses, and
        # let /runstate/stream be the live transcript. This POST only announces the run
        # id and terminal/error state; output, thinking, tools, tasks, and sub-agents are
        # all persisted as run-state spans by chat_one.
        from .chat_run import chat_one

        async def _usage(ev: dict[str, Any]) -> None:
            await _record_run_usage(request, project_key, agent, model, ev)

        if store is not None:
            async def _run_detached_chat() -> None:
                try:
                    await chat_one(
                        chat_agent,
                        message,
                        project_key,
                        run_id=run_id,
                        runner=harness_runner,
                        runstate=store,
                        harness=harness,
                        model=model,
                        reasoning=_reasoning,
                        system=system,
                        session_id=session_id,
                        attachment_paths=_attachment_local_paths,
                        start_run=False,
                        pid=os.getpid(),
                        workspace=chat_workspace,
                        project_key=project_key,
                        ltm_log_fn=partial(
                            chat_ltm_module.cli_log,
                            workspace=chat_workspace,
                        ),
                        ltm_agent=compound,
                        on_result=_usage,
                    )
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await store.set_status(
                            run_id,
                            "error",
                            error=local_run_tasks.LOCAL_RUN_CANCELLED_ERROR,
                        )
                    raise
                except Exception as exc:
                    with suppress(Exception):
                        await store.set_status(
                            run_id,
                            "error",
                            error=f"chat crashed: {exc}",
                        )
                finally:
                    if _attachment_names:
                        with suppress(Exception):
                            attachments_module.cleanup_run_attachments(run_id)

            task = asyncio.create_task(
                _run_detached_chat(), name=f"local-chat-{run_id}"
            )
            local_run_tasks.register_local_run_task(request.app.state, run_id, task)
            await asyncio.sleep(0)
            yield {"event": "done", "data": "{}"}
            return

        try:
            result = await chat_one(
                chat_agent,
                message,
                project_key,
                run_id=run_id,
                runner=harness_runner,
                runstate=store,
                harness=harness,
                model=model,
                reasoning=_reasoning,
                system=system,
                session_id=session_id,
                attachment_paths=_attachment_local_paths,
                start_run=False,
                pid=os.getpid(),
                workspace=chat_workspace,
                project_key=project_key,
                ltm_log_fn=partial(
                    chat_ltm_module.cli_log,
                    workspace=chat_workspace,
                ),
                ltm_agent=compound,
                on_result=_usage,
            )
        except (asyncio.CancelledError, GeneratorExit):
            await _mark_run_cancelled(_rs_status)
            raise
        if result.status == "error":
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": result.error or "harness error", "category": "error"}
                ),
            }
        elif result.text:
            # Terminal fallback/receipt only. Live output is no longer streamed through
            # this POST; the transcript follows /runstate/stream. The final result keeps
            # degraded/no-run-state cases usable and preserves the composer completion.
            yield {"event": "result", "data": json.dumps({"text": result.text})}
        yield {"event": "done", "data": "{}"}

        # CHAT FILE-ATTACHMENTS (step 6, Inc A): the local turn reached a terminal
        # state, so fire-and-forget cleanup of this run's sandbox directory. The shared
        # runner cleans exact host paths; this removes the run-keyed local sandbox.
        if _attachment_names:
            with suppress(Exception):
                attachments_module.cleanup_run_attachments(run_id)

    # ping=15s keeps the SSE connection from idling out on a slow first token.
    return EventSourceResponse(_events(), ping=15)


# ---------------------------------------------------------------------------
#  RunState SSOT live push (Milestone 1 T8) — the real-time replacement for the
#  agent-detail pane's poll. A NOTIFY off the store's run_state_events bus (T4
#  subscribe) is a WAKE signal carrying a run_id; on each wake we RE-READ the SAME
#  T7 read model the HTTP first-paint uses (_agent_runs_view_store →
#  store.recent/get_run) and push it, so the SSE push and the first paint render
#  from the IDENTICAL model and cannot disagree (ratified design decision #5). This
#  is the SOLE live channel for the agent pane now — the in-memory transcript store
#  and the ~/.cortex-feed feed channel (+ /feed-stream) were removed at T12.
# ---------------------------------------------------------------------------

async def _mark_run_cancelled(rs_status) -> None:
    """Best-effort terminal run status when an SSE generator is cancelled — the client
    disconnected (tab close / refresh / navigate / proxy idle). Called from the
    ``except (CancelledError, GeneratorExit)`` arm of the chat + dispatch SSE generators,
    which then re-raise. ``shield()``ed so the DB write completes even though the
    surrounding task is being torn down; never raises (``_rs_status`` itself already
    suppresses store errors). Without this, a disconnect orphans the run at 'running'
    forever and the in-flight turn is lost."""
    with suppress(BaseException):
        await asyncio.shield(
            rs_status("error", error="stream cancelled or client disconnected before completion")
        )


def _render_transcript_partial(runs_ctx: dict, agent_name: str) -> str:
    """Render the SAME ``_agent_transcript.html`` partial the agent-detail pane
    swaps in for first paint, from a store read-model context — so the SSE push
    delivers byte-identical markup to the first render (one render path, no drift).

    The partial only reads ``agent_run_selected`` + ``agent`` (its display name), so
    we hand it exactly that. Best-effort: a render hiccup returns "" (the frame still
    carries the structured selected-run fields the client can fall back to)."""
    try:
        tmpl = templates.get_template("_agent_transcript.html")
        return tmpl.render(
            agent_run_selected=runs_ctx.get("agent_run_selected"),
            agent={
                "name": agent_name,
                "display_name": (runs_ctx.get("agent_run_selected") or {}).get(
                    "agent_display"
                )
                or agent_name,
            },
        )
    except Exception as exc:  # pragma: no cover - defensive; structured fields remain
        log = __import__("logging").getLogger("console")
        log.warning("runstate transcript render failed (degraded): %s", exc)
        return ""


_REQUEST_LIVED_RUN_LEASES = {"chat", "approve_run"}


def _runstate_restart_row(run: Any, current_pid: int | None) -> dict[str, Any]:
    """Classify one active run for restart-survival visibility."""
    lease_owner = getattr(run, "lease_owner", None)
    pid = getattr(run, "pid", None)
    is_request_lived = lease_owner in _REQUEST_LIVED_RUN_LEASES
    same_pid = pid is not None and current_pid is not None and int(pid) == int(current_pid)
    if is_request_lived and same_pid:
        lifecycle = "live_request"
        restart_survivable = False
        needs_reconcile = False
    elif is_request_lived and pid is not None:
        lifecycle = "needs_reconcile"
        restart_survivable = False
        needs_reconcile = True
    elif is_request_lived:
        lifecycle = "legacy_request_lived"
        restart_survivable = False
        needs_reconcile = False
    elif lease_owner:
        lifecycle = "restart_survivable"
        restart_survivable = True
        needs_reconcile = False
    else:
        lifecycle = "unknown_lifecycle"
        restart_survivable = False
        needs_reconcile = False
    return {
        "run_id": getattr(run, "run_id", None),
        "project": getattr(run, "project", None),
        "agent": getattr(run, "agent", None),
        "handoff_id": getattr(run, "handoff_id", None),
        "status": getattr(run, "status", None),
        "lease_owner": lease_owner,
        "pid": pid,
        "heartbeat_at": getattr(run, "heartbeat_at", None),
        "updated_at": getattr(run, "updated_at", None),
        "lifecycle": lifecycle,
        "restart_survivable": restart_survivable,
        "needs_reconcile": needs_reconcile,
    }


async def _runstate_restart_status(store: Any, project: str, current_pid: int | None) -> dict[str, Any]:
    """Read-only restart-survival snapshot over active run_state rows."""
    project_key = (project or "").strip()
    if store is None:
        return {
            "ok": False,
            "project": project_key,
            "store": "degraded",
            "current_pid": current_pid,
            "active": [],
            "counts": {"active": 0, "restart_survivable": 0, "request_lived": 0, "needs_reconcile": 0},
            "error": "Run-state store is unavailable.",
        }
    try:
        active_runs = await store.list_active(project_key or None)
    except Exception as exc:
        return {
            "ok": False,
            "project": project_key,
            "store": "error",
            "current_pid": current_pid,
            "active": [],
            "counts": {"active": 0, "restart_survivable": 0, "request_lived": 0, "needs_reconcile": 0},
            "error": str(exc),
        }
    rows = [_runstate_restart_row(run, current_pid) for run in active_runs]
    return {
        "ok": True,
        "project": project_key,
        "store": "ok",
        "current_pid": current_pid,
        "active": rows,
        "counts": {
            "active": len(rows),
            "restart_survivable": sum(1 for row in rows if row["restart_survivable"]),
            "request_lived": sum(1 for row in rows if row["lease_owner"] in _REQUEST_LIVED_RUN_LEASES),
            "needs_reconcile": sum(1 for row in rows if row["needs_reconcile"]),
        },
        "error": None,
    }


async def _runstate_stream_gen(
    store: Any,
    project: str,
    *,
    agent: str | None = None,
    run_id: str | None = None,
):
    """Bare async generator backing ``GET /runstate/stream``. Factored out so tests
    can drive it without a full ASGI stack.

    Parks on ``store.subscribe(project)`` (the T4 LISTEN run_state_events wake bus).
    On EACH wake (a changed run's run_id) it RE-READS the SAME read model the HTTP
    first-paint uses — ``_agent_runs_view_store(store, project, agent, run_id=…)`` —
    and yields one ``event: runstate`` frame whose ``data`` JSON carries that fresh
    read-model: the structured selected-run fields (run_id / status / running / body
    / segments) AND the rendered ``_agent_transcript.html`` partial the pane swaps
    into ``#ad-tx-panel``. Because the push re-reads the SAME model the first paint
    rendered, they cannot disagree (ratified design decision #5).

    The wake's run_id is used only as a SIGNAL that *something* changed; the frame
    follows the pane's own selection (the pinned ``run_id`` if the pane passed one,
    else the agent's running/newest run — the view resolves it). Scoped to ``agent``
    so an open pane only re-renders its own transcript.

    GRACEFUL-DEGRADE (house law): a None store, a down app-DB, or a dropped
    subscription ENDS the generator cleanly (no frame, no raise) — a dead store can
    never 500 the SSE layer. A transient per-wake read/render error is swallowed (we
    skip that one frame and keep streaming) rather than tearing the stream down."""
    if store is None:
        # DEGRADE: no run-state SSOT → nothing to stream; end cleanly (no raise).
        return

    async def _frame(woke_run_id: str | None, *, initial: bool = False) -> dict | None:
        """Read the selected run view and shape one SSE frame.

        A run-filtered stream may be opened by run id alone (no agent pane context).
        In that case the selected run is hydrated directly through ``get_run`` and
        shaped with the same transcript mapper as ``GET /runs/run/{id}``. Agent-scoped
        streams keep the existing agent rail read model unchanged."""
        try:
            if run_id and not (agent or "").strip():
                rec = await store.get_run(run_id)
                if rec is None:
                    return None
                selected = _store_transcript_view(rec)
                payload_project = selected.get("project") or project
                payload_agent = selected.get("agent") or agent
                runs_ctx = {
                    "agent_runs": [selected],
                    "agent_run_count": 1,
                    "agent_run_running": 1 if selected.get("running") else 0,
                    "agent_run_selected": selected,
                    "agent_run_selected_id": selected.get("run_id"),
                    "agent_run_active": bool(selected.get("running")),
                    "agent_run_no_orch": False,
                }
            else:
                payload_project = project
                payload_agent = agent
                runs_ctx = await _agent_runs_view_store(
                    store, project, agent or "", run_id=run_id
                )
                selected = runs_ctx.get("agent_run_selected")
                # A run-filtered stream only replays when the selected run exists
                # (and, for an agent-scoped stream, belongs to that agent).
                if run_id and selected is None:
                    return None
        except Exception as exc:  # pragma: no cover - defensive (view self-guards)
            log = __import__("logging").getLogger("console")
            log.warning("runstate stream re-read failed (skipping frame): %s", exc)
            return None

        payload = {
            "project": payload_project,
            "agent": payload_agent,
            "wake_run_id": woke_run_id,
            "running": runs_ctx.get("agent_run_running") or 0,
            "count": runs_ctx.get("agent_run_count") or 0,
            "selected_id": runs_ctx.get("agent_run_selected_id"),
            "selected": selected,
            "html": _render_transcript_partial(runs_ctx, payload_agent or ""),
        }
        if initial:
            payload["initial"] = True
        return {"event": "runstate", "data": json.dumps(payload)}

    if run_id:
        # Reconnect/reload visibility: selected-run streams need an immediate frame
        # for an existing run, not only after the next NOTIFY. Start the subscription
        # first so the LISTEN setup races ahead of the snapshot read as much as the
        # current subscribe() primitive allows. Residual race: subscribe() has no
        # explicit "ready" handshake, so a change can still land while the listener is
        # being established or while this snapshot read is in flight. The snapshot is
        # nevertheless a fresh store read, and any later NOTIFY is followed normally.
        subscription = store.subscribe(project)
        aiter = subscription.__aiter__()
        wake_fut = asyncio.ensure_future(aiter.__anext__())
        try:
            await asyncio.sleep(0)
            initial_frame = await _frame(run_id, initial=True)
            if initial_frame is not None:
                yield initial_frame

            while True:
                try:
                    woke_run_id = await wake_fut
                except StopAsyncIteration:
                    return
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as exc:  # pragma: no cover - defensive
                    log = __import__("logging").getLogger("console")
                    log.warning("runstate stream subscribe failed (degraded): %s", exc)
                    return

                wake_fut = asyncio.ensure_future(aiter.__anext__())
                frame = await _frame(woke_run_id)
                if frame is not None:
                    yield frame
        finally:
            if not wake_fut.done():
                wake_fut.cancel()
            with suppress(BaseException):
                await wake_fut
            aclose = getattr(aiter, "aclose", None)
            if callable(aclose):
                with suppress(Exception):
                    await aclose()
        return

    # subscribe() graceful-degrades itself: a down/dropped DB ends it cleanly, so a
    # dead store simply yields no wakes and this generator ends without raising.
    async for woke_run_id in store.subscribe(project):
        # woke_run_id is only a wake signal; re-read the SAME model the HTTP route
        # uses. Per-wake errors are isolated so one bad read can't kill the stream.
        frame = await _frame(woke_run_id)
        if frame is None:
            continue
        yield frame


@app.get("/runstate/stream")
async def runstate_stream(
    request: Request,
    project: str | None = None,
    agent: str | None = None,
    run: str | None = None,
) -> EventSourceResponse:
    """Live-push the RunState SSOT to the agent-detail pane as Server-Sent Events
    (Milestone 1 T8) — the real-time replacement for the pane's old ~2s poll.

    Each SSE frame: ``event: runstate`` / ``data`` = the fresh read model (the
    rendered ``#ad-tx-panel`` transcript partial + the structured selected-run
    fields). The browser opens ``EventSource('/runstate/stream?project=<key>&agent=
    <name>')`` and swaps the transcript region from each frame — so a running run
    fills in live with NO poll.

    Scope: ``?project=`` (defaults to the default project) bounds the wake bus to one
    project; ``?agent=`` scopes the re-render to the open pane's agent; ``?run=``
    pins a specific run (the pane carries it when the operator clicked a past run),
    else the agent's running/newest run is followed.

    Re-reads the SAME read model the HTTP first-paint uses on every wake (ratified
    design decision #5: NOTIFY is only a wake; both surfaces re-read
    ``store.recent``/``get_run``), so the push and the first paint cannot disagree.

    GRACEFUL-DEGRADE: a down/absent run-state store or a dropped subscription ends
    the stream cleanly (the EventSource just sees no frames) rather than erroring —
    a dead app-DB never 500s this route. READ-ONLY: it only reads the store."""
    store = _runstate(request)
    project_key = (project or _default_project()).strip()
    return EventSourceResponse(
        _runstate_stream_gen(store, project_key, agent=agent, run_id=run),
        ping=15,
    )


@app.get("/runstate/restart-status")
async def runstate_restart_status(
    request: Request,
    project: str | None = None,
) -> dict[str, Any]:
    """Read-only active-run restart-survival status.

    Startup reconciliation mutates abandoned request-lived rows; this endpoint is
    the visible proof surface that active durable rows are still present and which
    lifecycle class they belong to.
    """
    project_key = (project or _default_project()).strip()
    return await _runstate_restart_status(_runstate(request), project_key, os.getpid())


# ---------------------------------------------------------------------------
#  Live event feed (R3) — console-side SSE proxy over Cortex GET /events
# ---------------------------------------------------------------------------

@app.get("/stream")
async def stream_proxy(request: Request, project: str | None = None):
    """Console-side SSE PROXY over the Cortex `GET /events` bridge (R3).

    A browser `EventSource` cannot send the `X-Project` + `X-Agent-Name` headers
    the Cortex RLS event surface requires, so the console bridges them: this route
    opens a server-side stream to Cortex `/events` (with the scoped headers) and
    re-streams the raw `text/event-stream` bytes straight back to the browser
    (heartbeats + frames passthrough untouched). The Dispatch view subscribes via
    `EventSource('/stream?project=<key>')` and refreshes its handoff/dispatch list
    the instant a new event lands — event-driven push, no poll.

    Cortex `/events` is event-driven (parks on the Postgres `cortex_events` NOTIFY
    condition, wakes on a row insert), so this proxy is idle between events by
    design — it costs nothing while waiting. READ-ONLY: a GET against the event
    bridge; it never mutates Cortex.

    Returns `text/event-stream`. If the project is missing we still answer 200 with
    a single comment frame then close (the client just sees no events) rather than
    erroring the EventSource. If the upstream is unreachable the stream closes
    cleanly (CortexClient.stream_events stops yielding)."""
    cortex = _cortex(request)
    project_key = (project or _default_project()).strip()

    async def _proxy() -> AsyncGenerator[bytes, None]:
        # An opening comment frame both confirms the stream is live to the browser
        # and flushes headers immediately (some buffers wait for first bytes).
        yield b": connected\n\n"
        async for chunk in cortex.stream_events(project_key):
            yield chunk

    # Raw passthrough of Cortex's text/event-stream. Disable proxy buffering so
    # frames reach the browser the instant Cortex emits them (event-driven).
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _proxy(), media_type="text/event-stream", headers=headers
    )


# ---------------------------------------------------------------------------
#  Dispatch — ACTIVITY feed + wave-plan as JSON (SPA-surfacing step 4)
#
#  The legacy HTML Dispatch center reads the orchestrator's autonomous-activity ring buffer +
#  the E007 wave-plan strip straight off the orchestrator inside _dispatch_context
#  (orch.feed.recent + orch.status['waves']). That path renders Jinja — there was no
#  JSON endpoint, so the React SPA could not reach the feed/waves. This SMALL
#  additive endpoint exposes EXACTLY that orchestrator state as JSON, sourced the
#  same way the HTML context does. BOUNDARY: the feed + waves are the orchestrator's
#  IN-MEMORY state (read via _orchestrator → orch.feed / orch.status), so this stays
#  in main.py — the pure app/dispatch module must import nothing of the orchestrator.
# ---------------------------------------------------------------------------

def _dispatch_activity_context(orch: Any, project_key: str) -> dict:
    """Shape the orchestrator's activity ring + wave plan for one project as JSON.

    Reads `orch.feed.recent` (newest-first activity) + `orch.status['waves']` (the
    E007 per-epic wave summary) + the live loop/inflight telemetry. EVERY read
    graceful-degrades (house law): orch None / a raising status / a raising feed all
    fall back to the clean idle/empty payload — the endpoint never raises. Pure (no
    request, no I/O beyond the in-memory orchestrator) so it is unit-tested directly."""
    key = (project_key or "").strip().lower()
    activity: list[dict] = []
    waves: list[dict] = []
    waves_any = False
    loop_running = False
    inflight = 0
    cap = orchestrator_mod.MAX_CONCURRENT

    if orch is not None and key:
        # Live loop/autonomy telemetry + the wave-plan summary (independent of the
        # feed read — a raising status must not lose the feed, and vice-versa).
        with suppress(Exception):
            st = orch.status(key)
            loop_running = bool(st.get("loop_running"))
            inflight = st.get("inflight", 0) or 0
            cap = st.get("max_concurrent", cap) or cap
            wv = st.get("waves") or {"epics": [], "any": False}
            waves = list(wv.get("epics", []) or [])
            waves_any = bool(wv.get("any"))
        # The orchestrator's activity ring buffer (newest-first), with a compact relative age.
        with suppress(Exception):
            raw = orch.feed.recent(key, limit=orchestrator_mod.ACTIVITY_MAX)
            for a in raw:
                activity.append(
                    {
                        "kind": a.get("kind") or "info",
                        "level": a.get("level") or "info",
                        "text": a.get("text") or "",
                        "agent": a.get("agent"),
                        "handoff_short": a.get("handoff_short"),
                        "ago": _activity_relative(a.get("ts")),
                    }
                )

    return {
        "project": project_key,
        "activity": activity,
        "activity_count": len(activity),
        "waves": waves,
        "waves_any": waves_any,
        "loop_running": loop_running,
        "inflight": inflight,
        "cap": cap,
        # True only when the orchestrator loop itself didn't start (degrade copy).
        "no_orch": orch is None,
    }


@app.get("/dispatch/{project_key}/activity")
async def dispatch_activity(request: Request, project_key: str) -> dict:
    """`GET /dispatch/{project}/activity` — the orchestrator's autonomous-activity feed + the
    E007 wave-plan strip as JSON (the SPA Dispatch view's activity surface).

    Strictly additive: a NEW `/activity` leaf under `/dispatch/{project}`, distinct
    from `/board` (GET), `/run` + `/autonomous` (POST) — shadows nothing. The feed +
    waves are the orchestrator's in-memory state; a None / degraded orchestrator
    yields the clean idle/empty payload (never a 500)."""
    return _dispatch_activity_context(_orchestrator(request), project_key)


# ---------------------------------------------------------------------------
#  Dispatch — AUTONOMOUS toggle (E007 Phase 1 master kill-switch)
# ---------------------------------------------------------------------------

@app.post("/dispatch/{project_key}/autonomous", response_class=HTMLResponse)
async def dispatch_autonomous_toggle(
    request: Request, project_key: str
) -> HTMLResponse:
    """Flip the per-project AUTONOMOUS master switch (the kill-switch for the
    orchestrator loop). CONSEQUENTIAL: turning this ON lets the background orchestrator auto-run
    agents on new handoffs, on the operator's subscription. OFF is the default for
    every project and OFF means the loop dispatches NOTHING for it.

    The desired state is the POSTed `enabled` field (a presentational checkbox + a
    hidden mirror sharing the name; the mirror wins — same pattern as the System
    save). We persist it to the app-DB `project_autonomy` table, then poke the loop
    to reconcile NOW (open/close its watcher) rather than waiting for the next poll.
    Re-renders the whole Dispatch center so the switch + activity feed reflect the
    authoritative post-write state. A failed write degrades to the unchanged state
    (never a 500)."""
    cortex = _cortex(request)
    form = await _read_posted_form(request)
    raw = (form.get("enabled") or "").strip().lower()
    enabled = raw in ("1", "true", "on", "yes")

    settings_store.set_project_autonomous(project_key, enabled, updated_by="console")
    # Reconcile immediately so the watcher opens/closes without a poll delay.
    orch = _orchestrator(request)
    if orch is not None:
        try:
            orch.notify_toggle_changed()
        except Exception:
            pass

    return templates.TemplateResponse(
        request, "_center.html",
        await _dispatch_context(cortex, project_key, orch),
    )


# ---------------------------------------------------------------------------
#  Dispatch — PROPOSE-MODE approve gate (PM Relentless Beat Inc 1)
# ---------------------------------------------------------------------------

@app.post("/projects/{project_key}/handoffs/{handoff_id}/approve",
          response_class=HTMLResponse)
async def approve_handoff(
    request: Request, project_key: str, handoff_id: str
) -> HTMLResponse:
    """Approve a handoff that the Dispatch gate parked awaiting human review.

    PROPOSE-MODE GATE: when propose_mode is ON for a project, _maybe_dispatch
    parks each ready handoff by writing status='awaiting' to `pending_approval`
    (instead of auto-spawning it) and emits an ActivityFeed 'awaiting_approval'
    line. Clicking Approve in the Dispatch view calls this route, which:
      1. Sets the handoff's approval status to 'approved' (idempotent UPSERT).
      2. Pokes the orchestrator's wake event so it reconciles immediately
         (no poll-interval delay after approval).

    The next _maybe_dispatch sweep reads status='approved' and falls through to
    the normal spawn path. Gated handoffs are NOT in _dispatched (the new gate
    design), so there is nothing to discard here.

    Re-renders the Dispatch center so the approved handoff's 'awaiting approval'
    badge disappears and the queue reflects the post-approve state.

    IDEMPOTENT: re-approving a handoff that is already 'approved' (or was never
    gated) is a clean no-op — the UPSERT is safe to call multiple times, and we
    still re-render the Dispatch view so the operator gets a live refresh. Never
    a 500."""
    cortex = _cortex(request)
    orch = _orchestrator(request)

    # 1. Set status='approved' (idempotent UPSERT — no error if already approved
    #    or if no row exists yet).
    settings_store.set_approval_status(project_key, handoff_id, "approved")

    # 2. Wake the loop for an immediate reconcile (no poll delay after approval).
    #    Gated handoffs are NOT in _dispatched (new gate design), so no discard.
    if orch is not None:
        try:
            orch.notify_toggle_changed()
        except Exception:
            pass  # orchestrator not running — next poll will pick it up

    return templates.TemplateResponse(
        request, "_center.html",
        await _dispatch_context(cortex, project_key, orch),
    )


# ---------------------------------------------------------------------------
#  Dispatch — "Approve & Run" (R3, PROPOSE-MODE)
# ---------------------------------------------------------------------------

async def _run_local_approve_detached(
    *,
    request: Request,
    cortex: Any,
    project_key: str,
    agent: dict,
    claim_agent: str,
    handoff_id: str,
    prompt: str,
    model: str | None,
    system: str,
    harness: str | None,
    reasoning: str | None,
    chat_workspace: str | None,
    run_id: str,
    store: Any,
) -> None:
    """Run claimed local Approve & Run work after the control SSE has detached."""

    async def _rs_status(status: str, **kw: Any) -> None:
        if store is None:
            return
        with suppress(Exception):
            await store.set_status(run_id, status, **kw)

    async def _rs_totals(*, tokens_in: Any, tokens_out: Any, cost_est_usd: Any) -> None:
        if store is None:
            return
        with suppress(Exception):
            await store.heartbeat(
                run_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_est_usd=cost_est_usd,
                pid=os.getpid(),
            )

    seq = 0

    async def _rs_span(kind: str, text: str) -> None:
        nonlocal seq
        if store is None or not text:
            return
        seq += 1
        with suppress(Exception):
            await store.append_output(run_id, seq=seq, kind=kind, text=text)

    await _rs_status("running")
    run_failed = False
    run_error: str | None = None
    result_ev: dict | None = None
    streamed_parts: list[str] = []
    try:
        async for ev in harness_runner.stream_chat(
            prompt,
            model=model,
            system=system,
            harness=harness,
            reasoning=reasoning,
            workspace=chat_workspace,
            project_key=project_key,
            run_context="approve_run",
        ):
            kind = ev.get("type")
            if kind == "delta":
                text = ev.get("text", "")
                streamed_parts.append(text)
                await _rs_span("output", text)
            elif kind == "thinking":
                await _rs_span("thinking", ev.get("text", ""))
            elif kind == "tool":
                await _rs_span("tool", ev.get("text", ""))
            elif kind == "tasks":
                await _rs_span("tasks", json.dumps(ev.get("items") or []))
            elif kind == "subagent":
                await _rs_span("subagent", str(ev.get("label") or "sub-agent"))
            elif kind == "result":
                await _record_run_usage(request, project_key, agent, model, ev)
                result_ev = ev
                text = ev.get("text") or ""
                if text and text.strip() != "".join(streamed_parts).strip():
                    await _rs_span("output", text)
            elif kind == "error":
                run_failed = True
                run_error = ev.get("message", "harness error")
    except asyncio.CancelledError:
        await _rs_status(
            "error", error=local_run_tasks.LOCAL_RUN_CANCELLED_ERROR
        )
        raise
    except Exception as exc:
        run_failed = True
        run_error = f"run crashed: {exc}"

    if run_failed:
        await _rs_status("error", error=run_error)
        return

    await _rs_totals(
        tokens_in=(result_ev or {}).get("tokens_in"),
        tokens_out=(result_ev or {}).get("tokens_out"),
        cost_est_usd=(result_ev or {}).get("cost_usd"),
    )
    await _rs_status("ok")
    if handoff_id:
        try:
            await cortex.complete_handoff(project_key, handoff_id, claim_agent)
        except Exception as exc:
            log = __import__("logging").getLogger("console")
            log.warning(
                "dispatch_run complete_handoff raised (watchdog will reconcile): %s",
                exc,
            )


@app.post("/dispatch/{project_key}/run")
async def dispatch_run(
    request: Request, project_key: str, agent_name: str
) -> EventSourceResponse:
    """Run an APPROVED dispatch (R3) — the human-in-the-loop trigger.

    PROPOSE-MODE INVARIANT: this route fires ONLY when the operator clicks
    "Approve & Run" on a proposed dispatch. Nothing in the Dispatch view (and
    nothing on page load / on a live event) calls this — the dispatch list only
    PROPOSES; the actual harness spawn happens here, on an explicit human POST.

    `agent_name` (query) is the proposed agent; the POST body carries the handoff
    `summary` (the work) + the handoff `id`/`compound` (for the system framing).
    We resolve the agent's effective CLAUDE model (override-first; non-claude
    harnesses fall back to the runner default — R3 routes the approved run through
    claude-code, same as the chat; TODO(harness-routing) for codex/kaidera/pi)
    and stream the harness reply back as SSE exactly like the chat composer.

    REAL DISPATCH CYCLE (Milestone 1 T9 — this was a DEAD surface: it streamed a
    reply but claimed nothing, wrote nowhere, and never completed). The run now runs
    the same lifecycle the detached worker does (run_agent.run_one):
      * CLAIM the handoff first (`cortex.claim_handoff`) — and if the claim FAILS,
        emit a clear error frame and STOP (an unclaimable handoff must not run).
      * open a `run_state` row in the SSOT store (lease_owner='approve_run', a
        uuid4 run_id) and surface `?run=<run_id>` (a `run` SSE frame) so the T8
        `/runstate/stream` pane follows THIS run live; mark it 'running'.
      * stream INTO the store — `append_output(run_id, kind, text)` per event —
        WHILE keeping the user-facing SSE stream working (the human still sees the
        reply live, byte-for-byte as before).
      * on success → `set_status(run_id,'ok', tokens…)` + `cortex.complete_handoff`;
        on a run error → `set_status(run_id,'error', error=…)` and leave the handoff
        CLAIMED (the watchdog reconciles a failed run; we never falsely complete).

    GRACEFUL-DEGRADE: every store + Cortex call is best-effort — a down store / API
    must NEVER crash this route; the reply still streams (the run + the Cortex audit
    proceed regardless, mirroring the worker's house law). Completion still bills
    nothing: the runner strips ANTHROPIC_API_KEY so it runs on the user's
    subscription.

    TODO(auto-mode): an autonomous orchestrator would, once trusted (the design's
    'training-wheels → auto' graduation), call this dispatch path itself on a new
    cortex_events handoff insert instead of waiting for the human click — gated by
    the Inc08 governance (rate/concurrency caps, approval gates, kill switch). For
    R3 that autonomous flip is deliberately NOT wired: a dispatch runs ONLY on the
    explicit Approve & Run POST below.
    """
    cortex = _cortex(request)
    form = await _read_posted_form(request)
    summary = (form.get("summary") or "").strip()
    handoff_id = (form.get("handoff_id") or "").strip()
    handoff_compound = (form.get("handoff_compound") or "").strip() or handoff_id

    if handoff_id and settings_store.is_propose_mode(project_key):
        approval_status = settings_store.get_approval_status(project_key, handoff_id)
        if approval_status != "approved":
            async def _approval_required() -> AsyncGenerator[dict[str, str], None]:
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {"message": (
                            f"Handoff {handoff_compound or handoff_id} is not approved "
                            "for dispatch. Approve it before running."),
                         "category": "approval_required"}
                    ),
                }
                yield {"event": "done", "data": "{}"}
            return EventSourceResponse(_approval_required())

    # Resolve the agent so the model + system framing match the proposal.
    project, agents = await asyncio.gather(
        cortex.get_project(project_key),
        cortex.get_agents(project_key),
    )
    chat_workspace = (project or {}).get("repo_root") or None  # fitness:allow-literal "repo_root" is a real wire/dict key, not a project literal (false match on 'root')
    agent = _find_agent(agents, agent_name)

    if agent is None:
        async def _unknown() -> AsyncGenerator[dict[str, str], None]:
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": f"Unknown agent '{agent_name}' in project '{project_key}'.",
                     "category": "unknown_agent"}
                ),
            }
            yield {"event": "done", "data": "{}"}
        return EventSourceResponse(_unknown())

    if not summary:
        async def _empty() -> AsyncGenerator[dict[str, str], None]:
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": "This handoff has no summary to dispatch.",
                     "category": "empty_handoff"}
                ),
            }
            yield {"event": "done", "data": "{}"}
        return EventSourceResponse(_empty())

    ov = settings_store.get_agent_override(project_key, agent.get("name") or "")
    designation = settings_store.normalize_designation(ov.get("designation"))
    agent_view = _agent_view(agent, designation, ov.get("role", ""), override=ov)
    compound = f"{agent_view['name']}@{project_key}"

    # Resolve the proposed agent's CONFIGURED harness + model (override-first;
    # unconfigured → claude-code / Opus 4.8 (1M) / max) and run THAT harness.
    harness, model, _reasoning = _chat_routing_for(agent, project_key)
    # System framing: the agent's identity + the handoff it is being dispatched on.
    # Built OFF the event loop (persona + skills both shell out to `cortex-boot`).
    base = await asyncio.to_thread(
        _build_chat_system,
        agent_view, compound, project_key, agent_view.get("name") or (agent.get("name") or ""), summary,
        chat_workspace,
    )
    ref = f" You are being dispatched on handoff {handoff_compound}." if handoff_compound else ""
    system = base + ref
    # The "prompt" the agent acts on IS the handoff's work summary.
    prompt = summary

    # The RunState SSOT store (T9): a None / down store graceful-degrades — the run
    # still claims, streams, and completes (only durable run-state is lost). We claim
    # AS the resolved target agent (the same agent we're about to run) — the Cortex
    # claim endpoint only flips the row when the claimer matches the handoff's target.
    store = _runstate(request)
    claim_agent = agent_view.get("name") or agent_name
    # Pre-create the run_id up front so the SSE 'run' frame, the store row, and every
    # span/status share ONE id (the uuid4 the T8 pane follows via ?run=<id>).
    run_id = str(uuid.uuid4())

    # DISPATCH-SPAWN SEAM (harness-service bridge — ADDITIVE + FLAGGED). When the
    # console is CONTAINERIZED it carries no harness CLIs, so the in-process
    # `stream_chat` lifecycle below cannot execute — the run would become a ghost
    # (run-state `running` but no host process) and the handoff would strand `claimed`
    # forever. With `HARNESS_SPAWN_MODE=remote` AND a HarnessPort wired (the SAME
    # factory the orchestrator's auto-dispatch uses), we instead SPAWN THE WORKER on
    # the HOST harness-service (`spawn_run` → the detached run-agent unit), exactly
    # like `orchestrator._dispatch_run`. That worker CLAIMS the handoff itself (the
    # sole claimer — so the route does NOT pre-claim in this mode: claim-exactly-once),
    # runs the resolved harness, writes spans + heartbeat + terminal status to the SAME
    # run_state row we pre-create here, and COMPLETES the handoff. The UI reads the
    # reply via the existing `/runstate/stream` SSE — so Approve & Run works in a
    # container with NO new client path. The flag (not the mere presence of a port)
    # gates the seam: unset/local/legacy → the EXISTING in-process path runs unchanged
    # (so non-container dev still works). A wired port that REJECTS the spawn
    # (accepted=False — service down / the local adapter's no-op) is an honest terminal
    # error here: a containerized console has NO local CLI to fall back to, so we fail
    # LOUDLY (error frame + run-state error) rather than ghost-strand. Because the
    # route never claimed in this mode, a rejected spawn leaves the handoff cleanly
    # unclaimed (NOT stranded). Mirrors `agent_chat`'s remote fork.
    _spawn_mode = os.environ.get("HARNESS_SPAWN_MODE", "").strip().lower()
    _harness_port = getattr(request.app.state, "harness_port", None)
    _dispatch_remote = _spawn_mode == "remote" and _harness_port is not None

    async def _events() -> AsyncGenerator[dict[str, str], None]:
        """Run the real claim → stream-into-store → complete cycle, bridging runner
        events → SSE frames (same user-facing mapping as the chat route).

        Every store/cortex call is best-effort (graceful-degrade): a down store or a
        Cortex blip must never crash the stream — the human still sees the reply.

        REMOTE BRIDGE (harness-service): in remote mode this hands the WORKER spawn to
        the host harness-service (the worker claims+runs+completes) and returns,
        instead of running the in-process lifecycle (the container has no CLI)."""
        # 0. REMOTE DISPATCH SEAM (the bridge): hand the worker spawn to the host
        #    harness-service. We do NOT pre-claim here — the spawned worker is the SOLE
        #    claimer (claim-exactly-once, matching the orchestrator's auto-dispatch), so
        #    a rejected spawn leaves the handoff cleanly unclaimed (never stranded). The
        #    worker writes the reply to THIS run_id's row; the UI reads /runstate/stream,
        #    so on accept we only pre-create the row, emit the `run` frame, and close the
        #    SSE with `done`. spawn_run is FIRE-AND-FORGET, but we still guard it (a
        #    misbehaving adapter must not crash the route): on accept=False OR a raise →
        #    terminal `error` on the pre-created row + a clear error frame, then STOP.
        if _dispatch_remote:
            outcome = await dispatch_worker(
                DispatchWorkerSpec(
                    run_id=run_id,
                    project=project_key,
                    agent=claim_agent,
                    agent_display=agent_view.get("display_name") or claim_agent,
                    handoff_id=handoff_id,
                    harness=harness,
                    model=model,
                    repo_root=chat_workspace,
                    lease_owner="approve_run",
                ),
                runstate=store,
                harness_port=_harness_port,
            )
            yield {"event": "run", "data": json.dumps({"run_id": outcome.run_id})}

            if outcome.accepted:
                # Dispatched host-side; the worker claims+runs+completes and writes the
                # reply to the pre-created run_state row (the pane follows ?run=run_id).
                yield {"event": "done", "data": "{}"}
                return
            # Rejected (service down / no host seam / a raising adapter): fail LOUDLY —
            # terminal error on the pre-created row + a clean error frame. Never claimed
            # in this mode, so the handoff is NOT stranded (a person can re-dispatch).
            _rej = outcome.error or "harness-service unavailable"
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": (
                        f"Could not dispatch {handoff_compound or handoff_id or 'run'} to "
                        f"the harness-service: {_rej}. The handoff was NOT claimed — "
                        f"re-run when the harness-service is reachable."),
                     "category": "dispatch_spawn_rejected"}
                ),
            }
            yield {"event": "done", "data": "{}"}
            return
        # 1. CLAIM first. A claim is keyed on the handoff id; with no handoff id this
        #    is a free-standing run (nothing to claim/complete — we still stream + log
        #    run-state). A claim that FAILS (someone else has it / not targeted here /
        #    Cortex down) must NOT run an unclaimable handoff: error frame + stop.
        if handoff_id:
            try:
                claimed = await cortex.claim_handoff(project_key, handoff_id, claim_agent)
            except Exception as exc:  # graceful-degrade: a raising claim → not claimed
                log = __import__("logging").getLogger("console")
                log.warning("dispatch_run claim raised (treating as unclaimable): %s", exc)
                claimed = False
            if not claimed:
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {"message": (
                            f"Could not claim handoff {handoff_compound or handoff_id} as "
                            f"{claim_agent} — it may already be claimed, not pending, or not "
                            f"targeted at this agent. Not running."),
                         "category": "claim_failed"}
                    ),
                }
                yield {"event": "done", "data": "{}"}
                return

        # 2. Open the run_state row (lease_owner='approve_run') and tell the pane which
        #    run to follow. Best-effort: a down store leaves run-state unwritten but the
        #    run proceeds. The 'run' frame carries the run_id either way so the T8 pane
        #    can subscribe to ?run=<id>.
        if store is not None:
            try:
                await store.start_run(
                    run_id=run_id,
                    project=project_key,
                    agent=claim_agent,
                    agent_display=agent_view.get("display_name") or claim_agent,
                    handoff_id=handoff_id or None,
                    harness=harness,
                    model=model,
                    pid=os.getpid(),
                    lease_owner="approve_run",
                )
            except Exception:
                pass
        yield {"event": "run", "data": json.dumps({"run_id": run_id})}

        if store is not None:
            task = asyncio.create_task(
                _run_local_approve_detached(
                    request=request,
                    cortex=cortex,
                    project_key=project_key,
                    agent=agent,
                    claim_agent=claim_agent,
                    handoff_id=handoff_id,
                    prompt=prompt,
                    model=model,
                    system=system,
                    harness=harness,
                    reasoning=_reasoning,
                    chat_workspace=chat_workspace,
                    run_id=run_id,
                    store=store,
                ),
                name=f"local-approve-run-{run_id}",
            )
            local_run_tasks.register_local_run_task(request.app.state, run_id, task)
            await asyncio.sleep(0)
            yield {"event": "done", "data": "{}"}
            return

        # Store helpers (best-effort; mirror run_agent.run_one's _rs_* wrappers).
        async def _rs_status(status: str, **kw: Any) -> None:
            if store is None:
                return
            with suppress(Exception):
                await store.set_status(run_id, status, **kw)

        async def _rs_totals(*, tokens_in: Any, tokens_out: Any, cost_est_usd: Any) -> None:
            """Stamp the run's FINAL token/cost totals on the run header via ``heartbeat``
            (the RunStatePort home for tokens/cost — ``set_status`` takes only status/
            error/metadata). Passing tokens to ``set_status`` raised a swallowed TypeError
            that pinned the row at 'running'. Best-effort, like every store write."""
            if store is None:
                return
            with suppress(Exception):
                await store.heartbeat(
                    run_id, tokens_in=tokens_in, tokens_out=tokens_out,
                    cost_est_usd=cost_est_usd, pid=os.getpid(),
                )

        seq = 0

        async def _rs_span(kind: str, text: str) -> None:
            nonlocal seq
            if store is None or not text:
                return
            seq += 1
            with suppress(Exception):
                await store.append_output(run_id, seq=seq, kind=kind, text=text)

        # 3. Mark RUNNING, then stream — appending each event to the store AND yielding
        #    the user-facing frame. Track the terminal outcome so we complete (or not).
        await _rs_status("running")
        run_failed = False
        run_error: str | None = None
        result_ev: dict | None = None
        _streamed_parts: list[str] = []  # what deltas streamed — guards the result echo
        try:
            async for ev in harness_runner.stream_chat(
                prompt, model=model, system=system, harness=harness, reasoning=_reasoning,
                workspace=chat_workspace, project_key=project_key, run_context="approve_run",
            ):
                kind = ev.get("type")
                if kind == "delta":
                    text = ev.get("text", "")
                    _streamed_parts.append(text)
                    await _rs_span("output", text)
                    yield {"event": "delta", "data": json.dumps({"text": text})}
                elif kind == "thinking":
                    think_text = ev.get("text", "")
                    await _rs_span("thinking", think_text)
                    yield {"event": "thinking", "data": json.dumps({"text": think_text})}
                elif kind == "tool":
                    tool_text = ev.get("text", "")
                    await _rs_span("tool", tool_text)
                    yield {"event": "tool", "data": json.dumps({"text": tool_text, "name": ev.get("name", "")})}
                elif kind == "tasks":
                    # TASKS INDICATOR (claude-code TodoWrite) — see agent_chat for the
                    # rationale. Structured items alongside the raw `tool` event above.
                    yield {"event": "tasks", "data": json.dumps(ev.get("items") or [])}
                elif kind == "subagent":
                    # SUB-AGENT INDICATOR (claude-code Task) — see agent_chat.
                    yield {"event": "subagent", "data": json.dumps({"label": ev.get("label")})}
                elif kind == "result":
                    # Capture the dispatched run's usage into the App-DB (E007
                    # telemetry) — fire-and-forget, never breaks the dispatch.
                    await _record_run_usage(request, project_key, agent, model, ev)
                    result_ev = ev
                    txt = ev.get("text") or ""
                    # De-dup the result ECHO: a streaming harness echoes the full reply
                    # in `result`, so appending an exact echo after the deltas DOUBLES
                    # it. Skip only an exact echo; additional/result-only text is kept.
                    if txt and txt.strip() != "".join(_streamed_parts).strip():
                        await _rs_span("output", txt)
                        yield {"event": "result", "data": json.dumps({"text": txt})}
                elif kind == "error":
                    run_failed = True
                    run_error = ev.get("message", "harness error")
                    yield {
                        "event": "error",
                        "data": json.dumps(
                            {"message": run_error,
                             "category": ev.get("category", "error")}
                        ),
                    }
                elif kind == "done":
                    yield {"event": "done", "data": "{}"}
                # session frames are intentionally not surfaced to the feed.
        except (asyncio.CancelledError, GeneratorExit):
            # SSE disconnect cancels this generator. CancelledError/GeneratorExit are
            # BaseExceptions that bypass the `except Exception` below — so WITHOUT this the
            # terminal status never runs and the autonomous run is orphaned at 'running'
            # (now reachable: autonomy is ON). Mark it terminal best-effort, shield()ed so
            # the write survives the cancellation, then re-raise.
            await _mark_run_cancelled(_rs_status)
            raise
        except Exception as exc:
            # A crash mid-stream is a failed run (don't complete the handoff).
            run_failed = True
            run_error = f"run crashed: {exc}"
            yield {
                "event": "error",
                "data": json.dumps({"message": run_error, "category": "error"}),
            }

        # 4. Terminal: on success set 'ok' (with telemetry) + COMPLETE the handoff; on
        #    failure set 'error' and LEAVE the handoff claimed (the watchdog
        #    reconciles — we never falsely complete a failed run).
        if run_failed:
            await _rs_status("error", error=run_error)
        else:
            await _rs_totals(
                tokens_in=(result_ev or {}).get("tokens_in"),
                tokens_out=(result_ev or {}).get("tokens_out"),
                cost_est_usd=(result_ev or {}).get("cost_usd"),
            )
            await _rs_status("ok")
            if handoff_id:
                try:
                    await cortex.complete_handoff(project_key, handoff_id, claim_agent)
                except Exception as exc:  # graceful-degrade: watchdog re-completes
                    log = __import__("logging").getLogger("console")
                    log.warning("dispatch_run complete_handoff raised (watchdog will "
                                "reconcile): %s", exc)

    return EventSourceResponse(_events(), ping=15)


@app.get("/views/{view}", response_class=HTMLResponse)
async def center_view(
    request: Request,
    view: str,
    project: str | None = None,
    q: str | None = None,
    run: str | None = None,
) -> HTMLResponse:
    """HTMX partial: swap the CENTER region to a nav-selected view.

    Live views: `dashboard` (R1), `dispatch` (R3), `settings` (R4), and
    `history` / `analytics` / `graph` (R7) — each wired to live, project-scoped
    data. An unknown view falls back to the generic placeholder panel. `q` is the
    Graph view's optional in-view entity-search term; `run` is accepted for URL
    compatibility but unused by the remaining views."""
    cortex = _cortex(request)
    selected_key = project or _default_project()

    if view == "dashboard":
        # The Dashboard tab is the ALL-PROJECTS overview (cross-project fleet),
        # NOT a per-project view. selected_key is carried only so the center's
        # self-poll + the rail/workspace stay on the operator's current project.
        ctx = await _fleet_context(cortex, selected_key)
        return templates.TemplateResponse(
            request,
            "_center.html",
            ctx,
        )

    # Settings (R4a–R4c): a full 4-tab Settings layout (Configure · Providers &
    # Models · Cortex · System), defaulting to the functional System tab.
    if view == "settings":
        return templates.TemplateResponse(
            request,
            "_center.html",
            await _settings_context(cortex, DEFAULT_SETTINGS_TAB, selected_key),
        )

    # R3 — Dispatch center view (event-driven handoff → proposed-agent surface,
    # PROPOSE-MODE for manual runs; the per-project Autonomous toggle gates the orchestrator).
    if view == "dispatch":
        return templates.TemplateResponse(
            request, "_center.html",
            await _dispatch_context(cortex, selected_key, _orchestrator(request))
        )

    # R7 — History · Analytics · Graph center views, all live.
    if view == "history":
        return templates.TemplateResponse(
            request, "_center.html", await _history_context(cortex, selected_key)
        )
    if view == "analytics":
        return templates.TemplateResponse(
            request,
            "_center.html",
            await _analytics_context(cortex, _appdb(request), selected_key),
        )
    if view == "graph":
        return templates.TemplateResponse(
            request, "_center.html", await _graph_context(cortex, selected_key, q)
        )

    label = NAV_VIEWS.get(view, view.title())
    increment = VIEW_INCREMENT.get(view, "a later increment")
    return templates.TemplateResponse(
        request,
        "_center.html",
        {
            "active_view": view,
            "placeholder_label": label,
            "placeholder_increment": increment,
            # placeholder still needs an empty scope so the template is uniform
            "selected": None,
            "selected_key": project,
            "tasks": [],
            "handoffs": [],
            "state": {},
            "agent_count": 0,
        },
    )


# ---------------------------------------------------------------------------
#  Settings routes (R4a–R4c)
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request, project: str | None = None) -> HTMLResponse:
    """Full Settings center view (default System tab), as a standalone partial.

    Mirrors GET /views/settings — handy as a direct route. Returns the
    `_settings.html` shell (header + 4-tab sub-nav + active body)."""
    cortex = _cortex(request)
    selected_key = project or _default_project()
    return templates.TemplateResponse(
        request,
        "_settings.html",
        await _settings_context(cortex, DEFAULT_SETTINGS_TAB, selected_key),
    )


@app.get("/settings/{page}", response_class=HTMLResponse)
async def settings_page(
    request: Request, page: str, project: str | None = None
) -> HTMLResponse:
    """HTMX partial: ONE settings sub-tab's body (swapped into #settings-body).

    `page` is one of configure / providers / projects / cortex / system. System
    renders the functional store editor; Providers & Models the read-only dynamic
    catalog (~15-min cached); Configure the per-agent harness/model/reasoning
    editor for the selected project; Workspace (projects) the per-project
    canonical-folder (repo_root) editor; Cortex the connection + /health + 6-layer
    read-out. An unknown page falls back to System (the default)."""
    cortex = _cortex(request)
    selected_key = project or _default_project()
    ctx = await _settings_body_context(cortex, page, selected_key)
    return templates.TemplateResponse(request, ctx["settings_body_template"], ctx)


@app.post("/settings/projects/{project_key}/folder", response_class=HTMLResponse)
async def settings_project_folder_save(
    request: Request, project_key: str, project: str | None = None
) -> HTMLResponse:
    """Change ONE project's canonical working folder (repo_root) in-app — the
    in-app version of the repo_root fix previously done by CLI.

    Reads the urlencoded form body WITHOUT python-multipart (same zero-extra-deps
    approach as the other Settings saves). The new folder is the `repo_root`
    field. We validate it is a NON-BLANK ABSOLUTE path, then call the ONE
    admin-authed Cortex method (CortexClient.set_project_repo_root → PATCH
    /projects/{key} with the X-Cortex-Admin-Token header). The token is sourced
    backend-only (env first, then local-cortex/.env) and NEVER reaches the
    browser.

    Failure modes degrade gracefully to an inline banner (no crash):
      * blank / relative path        → ValueError → 'must be an absolute path'
      * admin token not configured   → AdminTokenMissing → 'admin token not
                                       configured' (nothing is sent to Cortex)
      * API/transport error          → httpx error → the surfaced detail
    On success the row shows previous → new (from the endpoint's
    `previous_repo_root` / `repo_root`). Returns the WHOLE Workspace sub-tab body
    (re-read fresh so the listed folder reflects the change) swapped into
    #settings-body."""
    cortex = _cortex(request)
    selected_key = project or _default_project()
    form = await _read_posted_form(request)
    new_root = (form.get("repo_root") or "").strip()  # fitness:allow-literal "repo_root" is a real form/wire key, not a project literal (false match on 'root')

    saved_prev: str | None = None
    saved_new: str | None = None
    save_error: str | None = None
    try:
        result = await cortex.set_project_repo_root(project_key, new_root)
        saved_prev = result.get("previous_repo_root")  # fitness:allow-literal "previous_repo_root" is a real wire key, not a project literal (false match on 'root')
        saved_new = result.get("repo_root") or new_root  # fitness:allow-literal "repo_root" is a real wire key, not a project literal (false match on 'root')
    except ValueError as exc:
        save_error = str(exc)
    except AdminTokenMissing:
        save_error = (
            "admin token not configured — set CORTEX_ADMIN_TOKEN in the "
            "environment or in local-cortex/.env to edit project folders."
        )
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = (exc.response.json() or {}).get("detail") or ""
        except (ValueError, AttributeError):
            detail = exc.response.text[:200] if exc.response is not None else ""
        save_error = f"Cortex rejected the change ({exc.response.status_code}){': ' + detail if detail else ''}"
    except httpx.HTTPError as exc:
        save_error = f"couldn't reach Cortex: {exc}"

    ctx = await _projects_folder_context(
        cortex,
        selected_key,
        saved_key=project_key,
        saved_prev=saved_prev,
        saved_new=saved_new,
        save_error=save_error,
    )
    return templates.TemplateResponse(request, ctx["settings_body_template"], ctx)


def _license_settings_service(request: Request) -> settings_api.SettingsService:
    return settings_api.build_service(AppDbOperationalStore(appdb=_appdb(request)))


async def _license_settings(request: Request) -> tuple[settings_api.SettingsService, dict[str, Any]]:
    svc = _license_settings_service(request)
    values = await settings_api._settings_io(lambda: svc.load_app_settings())
    return svc, values if isinstance(values, dict) else {}


async def _license_body_response(request: Request, **overrides: Any) -> HTMLResponse:
    ctx = await _settings_body_context(_cortex(request), "license", _default_project())
    ctx.update(overrides)
    return templates.TemplateResponse(request, ctx["settings_body_template"], ctx)


def _license_login_message(request: Request, message: str, *, kind: str = "error") -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_settings_license_login_message.html",
        {"message": message, "kind": kind},
    )


def _license_retarget_settings_body(response: HTMLResponse) -> HTMLResponse:
    response.headers["HX-Retarget"] = "#settings-body"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def _extract_license_grant_text(raw: str) -> str:
    text = (raw or "").strip()
    if not text.startswith("{"):
        return text
    try:
        parsed = json.loads(text)
    except Exception:
        return text
    if not isinstance(parsed, dict):
        return text
    for key in ("grant", "license_key", "token"):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return text


async def _read_uploaded_license_grant(upload: Any) -> tuple[str | None, str | None]:
    reader = getattr(upload, "read", None)
    if not callable(reader):
        return None, "Choose a license grant file to import."
    try:
        raw = reader()
        if hasattr(raw, "__await__"):
            raw = await raw
    except Exception as exc:
        return None, f"Could not read license grant file: {exc}"
    if isinstance(raw, bytes):
        if len(raw) > 256_000:
            return None, "License grant file is too large."
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "License grant file must be UTF-8 text."
    else:
        text = str(raw or "")
    grant = _extract_license_grant_text(text)
    if not grant:
        return None, "License grant file is empty."
    return grant, None


@app.post("/settings/license/login/start", response_class=HTMLResponse)
async def settings_license_login_start(
    request: Request,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> HTMLResponse:
    from app.license_client import start_device_flow
    res = await start_device_flow()
    if not res.get("ok"):
        return _license_login_message(request, f"Failed to start login: {res.get('error') or 'unknown error'}")

    return templates.TemplateResponse(request, "_settings_license_polling.html", {
        "device_code": res["device_code"],
        "user_code": res["user_code"],
        "verification_uri": res["verification_uri"],
        "code_verifier": res["code_verifier"],
        "interval": res["interval"]
    })

@app.post("/settings/license/login/poll", response_class=HTMLResponse)
async def settings_license_login_poll(
    request: Request,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> HTMLResponse:
    form = await request.form()
    device_code = str(form.get("device_code") or "")
    code_verifier = str(form.get("code_verifier") or "")
    if not device_code or not code_verifier:
        return _license_login_message(request, "Login session is missing its device code. Start login again.")

    from app.license_client import poll_device_flow, activate
    res = await poll_device_flow(device_code, code_verifier)

    if res.get("status") == "pending":
        try:
            interval = int(str(form.get("interval") or "5"))
        except ValueError:
            interval = 5
        if res.get("slow_down"):
            interval += 5
        # Keep polling
        return templates.TemplateResponse(request, "_settings_license_polling.html", {
            "device_code": device_code,
            "user_code": str(form.get("user_code") or ""),
            "verification_uri": str(form.get("verification_uri") or ""),
            "code_verifier": code_verifier,
            "interval": interval,
        })
    elif res.get("status") == "done":
        # Token acquired. Now activate.
        org_login_token = res["org_login_token"]
        svc, settings = await _license_settings(request)
        act_res = await activate(org_login_token, settings=settings, save_settings=svc.upsert_app_settings)
        if not act_res.ok:
            return _license_login_message(request, f"Activation failed: {act_res.error or 'unknown error'}")

        # Success! Reload the settings license body
        return _license_retarget_settings_body(await _license_body_response(request, refresh_success=True))
    else:
        return _license_login_message(request, f"Login failed: {res.get('message') or 'unknown error'}")

@app.post("/settings/license/refresh", response_class=HTMLResponse)
async def settings_license_refresh(
    request: Request,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> HTMLResponse:
    from app.license_client import heartbeat

    svc, settings = await _license_settings(request)
    res = await heartbeat(settings=settings, save_settings=svc.upsert_app_settings)

    if not res.ok:
        return await _license_body_response(request, refresh_error=res.error)
    else:
        return await _license_body_response(request, refresh_success=True)

@app.post("/settings/license/import", response_class=HTMLResponse)
async def settings_license_import(
    request: Request,
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> HTMLResponse:
    form = await request.form()
    grant, error = await _read_uploaded_license_grant(form.get("grant_file"))
    if error or not grant:
        return await _license_body_response(request, import_error=error or "License grant is empty.")

    from app import license as lic
    if not lic.verify_license(grant):
        return await _license_body_response(request, import_error="Imported grant is invalid or expired.")

    svc, _settings = await _license_settings(request)
    saved = await settings_api._settings_io(lambda: svc.upsert_app_settings({"license_key": grant}))
    if not saved:
        return await _license_body_response(request, import_error="Could not store imported license grant.")
    return await _license_body_response(request, import_success=True)


@app.post("/settings/system", response_class=HTMLResponse)
async def settings_system_save(
    request: Request, project: str | None = None
) -> HTMLResponse:
    """Persist the System settings form to the console-local store (R4a).

    Reads the urlencoded form body WITHOUT python-multipart (same approach as the
    workspace SAVE — keeps the zero-extra-deps footprint), applies only known
    schema keys via app.settings.save (which leaves a blank/masked secret
    unchanged and types every value), then returns the save-result banner
    (`_settings_saved.html`) swapped into #sys-save-result.

    Read-only Cortex is untouched: this writes ONLY the console settings store,
    never the real Cortex/system .env or any real secret file."""
    updates = await _read_posted_form(request)
    selected_key = project or _default_project()
    saved = False
    saved_count = 0
    saved_error: str | None = None
    try:
        # Count only known, present schema keys (what actually gets applied).
        known = {k: v for k, v in updates.items() if k in settings_store._field_index()}
        settings_store.save(known)
        saved = True
        saved_count = len(known)
    except OSError as exc:
        saved_error = f"write failed: {exc}"

    return templates.TemplateResponse(
        request,
        "_settings_saved.html",
        {
            "saved": saved,
            "saved_count": saved_count,
            "saved_error": saved_error,
            "selected_key": selected_key,
        },
    )


@app.post("/settings/system/test-key", response_class=HTMLResponse)
async def settings_test_key(
    request: Request, project: str | None = None
) -> HTMLResponse:
    """Probe ONE provider credential and render the inline ✓/✗ result (R4a follow-up).

    `field` (form) is a built-in secret key (anthropic_api_key / openai_api_key /
    openrouter_api_key / fireworks_api_key / …) or `custom:<id>` for an operator-added
    provider. For built-ins the form ALSO carries that field's current input value: a
    freshly-typed key is tested as-is (pre-save feedback); a blank/masked field falls
    back to the stored key, then to the REAL process-env / local-cortex/.env key the
    harness runs with (so e.g. OpenRouter tests green off its .env key even when the
    console store is empty). The probe is a cheap read-only call (model list / key
    info — never a completion, zero tokens) and NEVER echoes the key — only ok + a
    message. Swapped into the per-row #keytest-* slot."""
    form = await _read_posted_form(request)
    selected_key = project or _default_project()
    field = (form.get("field") or "").strip()
    # The operator-typed value of THAT secret input (built-ins only); custom
    # providers resolve their key server-side from the stored entry, so typed=None.
    typed = form.get(field) if field and not field.startswith("custom:") else None
    result = await provider_check.test_provider(field, typed)
    return templates.TemplateResponse(
        request,
        "_settings_keytest.html",
        {"r": result, "field": field, "selected_key": selected_key},
    )


def _custom_providers_ctx(
    project: str | None,
    *,
    added: str | None = None,
    removed: bool = False,
    error: str | None = None,
) -> dict:
    """Context for the custom-providers partial (the list + add-form region that
    swaps into #sys-custom-providers). `added`/`removed`/`error` drive a small
    inline status line; the list is always re-read fresh (masked) from the store."""
    return {
        "custom_providers": settings_store.view_custom_providers(),
        "cp_added": added,
        "cp_removed": removed,
        "cp_error": error,
        "selected_key": project or _default_project(),
    }


@app.post("/settings/system/custom-provider", response_class=HTMLResponse)
async def settings_custom_provider_add(
    request: Request, project: str | None = None
) -> HTMLResponse:
    """Add ONE operator-defined custom provider (name + base URL + API key) to the
    console-local store (Task 4). Reads the urlencoded body WITHOUT python-
    multipart (same zero-extra-deps approach as the other settings saves), appends
    via app.settings.add_custom_provider (atomic write; built-in/agent settings
    preserved), and returns the refreshed custom-providers partial swapped into
    #sys-custom-providers — the new row shows its key masked.

    Writes ONLY the console settings store; the real Cortex/.env is untouched."""
    form = await _read_posted_form(request)
    selected_key = project or _default_project()
    name = (form.get("cp_name") or "").strip()
    base_url = (form.get("cp_base_url") or "").strip()
    api_key = (form.get("cp_api_key") or "").strip()

    added: str | None = None
    error: str | None = None
    if not name:
        error = "A provider name is required."
    else:
        try:
            entry = settings_store.add_custom_provider(name, base_url, api_key)
            added = entry["name"]
        except ValueError as exc:
            error = str(exc)
        except OSError as exc:
            error = f"write failed: {exc}"

    return templates.TemplateResponse(
        request,
        "_settings_custom_providers.html",
        _custom_providers_ctx(selected_key, added=added, error=error),
    )


@app.post("/settings/system/custom-provider/delete", response_class=HTMLResponse)
async def settings_custom_provider_delete(
    request: Request, project: str | None = None
) -> HTMLResponse:
    """Remove ONE custom provider by id (Task 4). Reads the urlencoded body for
    `cp_id`, removes via app.settings.remove_custom_provider (atomic; other
    settings preserved), and returns the refreshed custom-providers partial."""
    form = await _read_posted_form(request)
    selected_key = project or _default_project()
    pid = (form.get("cp_id") or "").strip()

    removed = False
    error: str | None = None
    try:
        removed = settings_store.remove_custom_provider(pid)
    except OSError as exc:
        error = f"write failed: {exc}"

    return templates.TemplateResponse(
        request,
        "_settings_custom_providers.html",
        _custom_providers_ctx(selected_key, removed=removed, error=error),
    )


# --- codex (ChatGPT) subscription login. JSON routes (the SPA Providers tab calls
# them; a CLI can too). Current supported flow is `codex login --device-auth`:
# the old app-owned direct OAuth usercode endpoint is retained in app/codex_oauth.py
# for future verification, but live auth now rejects it with 403.
@app.post("/settings/providers/codex-login/start")
async def codex_login_start(request: Request) -> dict[str, Any]:
    """Begin Codex device-code login; returns the URL + one-time code to show
    the operator and a flow id to poll with."""
    from app import codex_oauth
    try:
        flow = await codex_oauth.start_device_flow()
    except Exception as exc:  # noqa: BLE001 — never 500 the settings page
        return {"ok": False, "error": f"codex device-flow start failed: {exc}"}
    return {"ok": True, **flow}


@app.post("/settings/providers/codex-login/poll")
async def codex_login_poll(request: Request) -> dict[str, Any]:
    """Poll once for completion. Body: device_auth_id + user_code."""
    from app import codex_oauth
    form = await _read_posted_form(request)
    did = (form.get("device_auth_id") or "").strip()
    uc = (form.get("user_code") or "").strip()
    if not did or not uc:
        return {"status": "error", "message": "device_auth_id + user_code required"}
    return await codex_oauth.poll_device_flow(did, uc)


@app.post("/settings/providers/codex-login/logout")
async def codex_login_logout(request: Request) -> dict[str, Any]:
    """Clear stored Codex subscription credentials."""
    from app import codex_oauth
    app_db_ok = codex_oauth.clear_codex_oauth_blob()
    cli = codex_oauth.logout_codex_cli()
    return {"ok": bool(app_db_ok and cli.get("ok", False)), "cli": cli}


@app.get("/settings/providers/codex-login/state")
async def codex_login_state(request: Request) -> dict[str, Any]:
    """Whether Codex subscription login is available, plus the active method."""
    from app import codex_oauth
    app_db_logged_in = codex_oauth.is_logged_in()
    cli = codex_oauth.codex_cli_status()
    method = "app_oauth" if app_db_logged_in else ("codex_cli" if cli.get("logged_in") else "")
    return {
        "logged_in": bool(app_db_logged_in or cli.get("logged_in")),
        "account_id": codex_oauth.account_id() if app_db_logged_in else "",
        "method": method,
        "auth_method": cli.get("auth_method") or "",
        "cli_available": bool(cli.get("available")),
        "cli_message": cli.get("message") or "",
    }


@app.post("/settings/configure", response_class=HTMLResponse)
async def settings_configure_save(
    request: Request, project: str | None = None
) -> HTMLResponse:
    """Persist ONE agent's harness/model/reasoning override to the console store
    (R4c). Reads the urlencoded form body WITHOUT python-multipart (same zero-
    extra-deps approach as the System/workspace saves).

    Expects `agent` + any of `harness` / `model` / `reasoning`. The override is
    layered over the registry value for display; a blank field CLEARS that
    override (falls back to the registry value). Returns the per-agent save
    banner (`_settings_configure_saved.html`) swapped into that row's result slot.

    CONSOLE-LOCAL BY DESIGN (feature-gap #81, the CTO's reversed decision): this legacy
    Configure card writes ONLY the console-local override — it does NOT push to the
    Cortex registry. Committing the config to the registry is the explicit "Promote to
    registry" action (`POST /settings/{project}/agents/{agent}/promote`)."""
    form = await _read_posted_form(request)
    selected_key = project or _default_project()
    agent = (form.get("agent") or "").strip()

    saved = False
    saved_error: str | None = None
    effective: dict[str, str] = {}
    if not agent:
        saved_error = "no agent specified"
    else:
        override = {
            k: form.get(k, "") for k in settings_store.AGENT_OVERRIDE_FIELDS
        }
        try:
            effective = settings_store.save_agent_override(
                selected_key, agent, override
            )
            saved = True
        except OSError as exc:
            saved_error = f"write failed: {exc}"

    return templates.TemplateResponse(
        request,
        "_settings_configure_saved.html",
        {
            "saved": saved,
            "saved_error": saved_error,
            "cfg_agent": agent,
            "cfg_effective": effective,
            "cfg_has_override": bool(effective),
            "selected_key": selected_key,
        },
    )


@app.get("/health-pill", response_class=HTMLResponse)
async def health_pill(request: Request) -> HTMLResponse:
    """HTMX partial: just the top-bar health pill, for independent live refresh."""
    cortex = _cortex(request)
    health = await cortex.get_health()
    return templates.TemplateResponse(
        request,
        "_health_pill.html",
        {"health": health},
    )


@app.get("/activity", response_class=HTMLResponse)
async def activity_strip(request: Request, project: str | None = None) -> HTMLResponse:
    """HTMX partial: the always-present crew-activity strip in the shell header.

    Polled (~every 4s) by the header slot so the operator can 'watch the crew
    work' from ANY center view, not just the Dispatch tab — the orchestrator picking up
    handoffs, agents running, completing/erroring, with relative timestamps. Reads
    the orchestrator's in-memory ring buffer (live telemetry); a fresh process /
    idle project renders the clean empty state. Scoped to ?project=<key> (the
    currently-selected project); no project or no orchestrator → idle state."""
    orch = _orchestrator(request)
    return templates.TemplateResponse(
        request,
        "_activity_strip.html",
        _activity_strip_context(orch, project),
    )
