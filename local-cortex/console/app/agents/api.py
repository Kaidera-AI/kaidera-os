"""Agents API — the imperative shell for the `agents` module.

A FastAPI `APIRouter` exposing the agents CATALOG as typed JSON. This is the ONLY
part of the module that imports fastapi (the layer rule: the service is pure; the
shell does I/O + wiring). Two endpoints:

  * `GET /agents/{project}`          — the roster catalog (Interactive/Autonomous
                                        groups + orchestrator label + default lead),
  * `GET /agents/{project}/{agent}`  — one agent resolved to its effective
                                        config/designation/role (+ the config-view).

Each endpoint:
  * resolves the `OperationalStorePort` from `app.state` (the adapter the app wired
    at startup) via `Depends` — so the route depends on the PORT, not the concrete
    store,
  * resolves the ROSTER source (the Cortex client) from `app.state` via `Depends`
    and fetches the project roster (the catalog is roster-shaping over the override
    store; the roster's origin is the Cortex registry),
  * constructs the `AgentsService` over the port (injecting the real `harness`-backed
    config resolver + config-view shaper so the JSON labels match the HTML view
    exactly), and returns the shaped JSON.

`main.py` mounts this additively (`app.include_router(agents.router)`); the existing
HTML agents column + agent-detail pane delegate their catalog substance to the SAME
`AgentsService`, so the JSON API and the HTML surfaces share one source of logic.

PATH NOTE (additive, non-colliding): the existing HTML agent-detail pane is served
by `main`'s `GET /agents/{project}/{agent}` (HTMLResponse). To stay strictly
additive — the router is registered BEFORE those HTML routes, and FastAPI matches
the first-registered route for a given path shape — the JSON detail is exposed at
the distinct THREE-segment `GET /agents/{project}/{agent}/detail` so it can NEVER
shadow the two-segment HTML pane. The one-segment `GET /agents/{project}` (the
catalog) has no existing one-segment counterpart, so it is collision-free as-is.

Graceful-degrade rides through from the service/store (a down store falls back to
the registry-heuristic classification, never a 500); an unknown agent on the detail
route is a clean 404 (never a 500)."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from app import harness as harness_cfg
from app.agents import epics as epics_shape
from app.agents.service import AgentsService, build_config_catalog
from app.domain.ports import OperationalStorePort

router = APIRouter(prefix="/agents", tags=["agents"])


async def _safe(coro, default):
    """Await a Cortex read, returning `default` if it raises. The CortexClient already
    graceful-degrades to []/{} internally; this is the belt-and-braces guard so a surprising
    raise (a fake/older client) can't 500 the route, and so one read's failure never aborts the
    gather for the others (mirrors the history shell's `_safe`)."""
    try:
        return await coro
    except Exception:
        return default


# Flat {model_value: pretty_label} map across every harness's model list, built once
# from harness.HARNESS_MODELS — the SAME source main uses for its card model labels,
# so the JSON model labels match the HTML exactly. An unknown id falls through.
_MODEL_LABELS: dict[str, str] = {
    m["value"]: m["label"]
    for models in harness_cfg.HARNESS_MODELS.values()
    for m in models
}

# Default routing for an unconfigured agent. In self-contained mode (the
# distributable, no host CLIs) a fresh agent defaults to kaidera (the
# in-process API lane). In dev mode it stays claude-code (the
# proven subscription path). Both remain selectable in the dropdown; this
# only affects the initial default for a fresh unconfigured agent.
def _resolve_default_harness() -> str:
    """The default harness for a NEW unconfigured agent.

    Honors the operator's System setting `harness_default` FIRST (a real, wired control
    — read LIVE here, validated to a known harness), and falls back to **kaidera** — the
    default harness for EVERY AI worker and deploy (CTO directive: kaidera is what we
    promote + the only harness whose model/thinking/auth we drive from our own Settings;
    others are opt-in overrides). Read at CALL time (never cached at import) so a Settings
    change takes effect without a console restart, and so the app-DB is never touched at import."""
    mode_default = "kaidera"  # fitness:allow-literal canonical harness id (CTO default-for-all-workers, not a per-project literal)
    try:
        from app import settings as settings_store

        configured = harness_cfg.canonical_harness(
            str(settings_store.load().get("harness_default") or "").strip()
        )
        return configured or mode_default
    except Exception:  # the app-DB may be down / not yet up — fall back to the mode default
        return mode_default


# The default model id (per harness.HARNESS_MODELS). Only applied when the
# effective harness is a fixed-lane harness (claude-code) and no model is set.
_DEFAULT_MODEL = "claude-opus-4-8[1m]"


def _model_label(model: Optional[str]) -> Optional[str]:
    """Human label for a model id, else the raw value."""
    if not model:
        return None
    return _MODEL_LABELS.get(model, model)


def _harness_resolve_config(agent: dict, override: dict) -> dict:
    """The real per-agent config resolver (override-first → registry → default),
    wrapping `harness._registry_config` + `canonical_harness` + `harness_label` +
    the model-label map — so the card's harness/model match the runner + the detail
    panel exactly. Mirrors `main._agent_view`'s resolution block 1:1."""
    caps = agent.get("capabilities") or {}
    reg = harness_cfg._registry_config(agent)
    harness = harness_cfg.canonical_harness(
        override.get("harness") or reg["harness"]
    ) or _resolve_default_harness()
    model = (override.get("model") or reg["model"] or "").strip() or None
    # VALIDITY (feature #99): coerce an impossible stored model to the harness default
    # so the JSON card shows the SAME runnable model the runner uses — never an
    # impossible pair (same coercion as main._agents_resolve_config / _chat_routing_for).
    if model is not None:
        model = harness_cfg.coerce_model(harness, model)
    # Fill the default model when none is set: claude-code → its fixed default; kaidera
    # → the out-of-the-box Fireworks kimi default (so the seeded onboarding Lead is runnable
    # with zero config). Other catalog/pi lanes keep no fixed default (picker-driven).
    if model is None:
        if harness == "claude-code":
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


def _harness_config_view(
    agent: dict, override: dict, catalog_groups: list, registry_designation: str,
    pi_catalog_groups: list | None = None,
) -> dict:
    """The real per-agent config-view shaper, wrapping `harness.agent_config_view`
    (the full inline-edit row model). Keeps the JSON detail's config-view identical
    to the Configure card / the inline header dropdowns."""
    return harness_cfg.agent_config_view(
        agent, override, catalog_groups, registry_designation,
        pi_catalog_groups=pi_catalog_groups,
    )


def get_operational_store(request: Request) -> OperationalStorePort:
    """Resolve the `OperationalStorePort` for the request.

    Prefers a pre-wired `app.state.opstore` (an `AppDbOperationalStore`); falls back
    to wrapping the live `app.state.appdb` so the route works even before the app
    explicitly stashes the adapter. Constructed at this composition seam so callers
    receive the PORT, never the concrete store (mirrors the analytics resolver)."""
    state = request.app.state
    store = getattr(state, "opstore", None)
    if store is not None:
        return store
    from app.adapters.opstore import AppDbOperationalStore

    return AppDbOperationalStore(appdb=state.appdb)


def get_roster_source(request: Request) -> Any:
    """Resolve the ROSTER source — the Cortex client stashed on `app.state` (it
    exposes `get_agents(project)`). The catalog service consumes the roster the
    caller fetches; this is the composition seam that supplies it."""
    return request.app.state.cortex


def get_runstate_store(request: Request) -> Any:
    """Resolve the RunStatePort (`app.state.runstate`) for the request — the
    durable run_state/run_span store chat history reads from. None when the app-DB
    is down (the history route then degrades to an empty conversation)."""
    return getattr(request.app.state, "runstate", None)


async def _live_catalog_groups() -> list:
    """Fetch the live Providers & Models catalog `groups` (the kaidera
    catalog-lane model source) — the SAME cached source `main` feeds the HTML
    Configure card (`providers.view_catalog(get_catalog())['groups']`). Imported
    lazily so the agents shell stays free of a module-load coupling to providers.
    `get_catalog()` is cached + never raises; an unexpected failure still degrades
    to [] (fixed lanes only) at the endpoint."""
    from app import providers as providers_catalog

    catalog = await providers_catalog.get_catalog()
    return providers_catalog.view_catalog(catalog).get("groups", [])


def _harness_base_url() -> str:
    host = os.environ.get("HARNESS_SERVICE_HOST", "host.docker.internal")
    port = os.environ.get("HARNESS_SERVICE_PORT", "8766")
    return f"http://{host}:{port}"


def _harness_headers() -> dict[str, str]:
    token = (os.environ.get("HARNESS_SERVICE_TOKEN", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _live_pi_catalog_groups() -> list:
    """Fetch PI's host-side dynamic catalog groups.

    In CONTAINERIZED / remote mode the `pi` CLI is absent from this process, so
    we fetch from the host harness-service `/models/pi` bridge. In NATIVE mode
    (the console runs on the host with the CLI present) we shell `pi` directly
    via `pi_catalog.list_pi_model_groups()` — no extra hop, no manual
    harness-service to keep alive. Failure degrades to [] so the service layer
    can fall back to the safe fixed PI list.
    """
    from app import pi_catalog

    # NATIVE / dev: the CLI lives in this process's PATH — shell it directly.
    if not pi_catalog._remote_mode():
        return await pi_catalog.list_pi_model_groups()
    # CONTAINER: ask the host bridge.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            resp = await client.get(
                               f"{_harness_base_url()}/models/pi",
                headers=_harness_headers(),
            )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    groups = data.get("groups") if isinstance(data, dict) else None
    return groups if isinstance(groups, list) else []


async def _live_claude_model_options() -> list:
    """Discover the installed Claude Code aliases and current effort levels."""
    from app import claude_catalog

    return await claude_catalog.list_claude_model_options()


async def _live_codex_model_options() -> list:
    """Discover Codex models and exact per-model effort ladders via app-server."""
    from app import codex_catalog

    return await codex_catalog.list_codex_model_options()


def get_catalog_source(request: Request) -> Callable[[], Awaitable[list]]:
    """Resolve the providers CATALOG source — an async callable returning the
    catalog `groups`. Defaults to the live cached providers layer; constructed at
    this seam so the route depends on a callable (the tests inject a fake), never
    the concrete providers module."""
    return _live_catalog_groups


def get_pi_catalog_source(request: Request) -> Callable[[], Awaitable[list]]:
    """Resolve the host PI catalog source. Tests inject a fake callable."""
    return _live_pi_catalog_groups


def get_claude_catalog_source(request: Request) -> Callable[[], Awaitable[list]]:
    return _live_claude_model_options


def get_codex_catalog_source(request: Request) -> Callable[[], Awaitable[list]]:
    return _live_codex_model_options


def build_service(store: OperationalStorePort) -> AgentsService:
    """Construct the agents service over the port, injecting the concrete harness-
    backed config resolver + config-view shaper (so JSON labels match the HTML)."""
    return AgentsService(
        store=store,
        resolve_config=_harness_resolve_config,
        config_view=_harness_config_view,
    )


@router.get("/{project}")
async def list_endpoint(
    project: str,
    store: OperationalStorePort = Depends(get_operational_store),
    roster: Any = Depends(get_roster_source),
) -> dict[str, Any]:
    """`GET /agents/{project}` — the project's roster catalog as JSON: the
    Interactive/Autonomous groups (override-first classification), the orchestrator
    label, and the default lead. Includes `project` in the payload.

    The roster is fetched from Cortex (`roster.get_agents(project)`); a down Cortex
    yields an empty roster (empty groups) and a down store falls back to the
    registry heuristic — never a 500."""
    agents = await roster.get_agents(project)
    svc = build_service(store)
    catalog = await svc.list_agents(project, agents)
    return {"project": project, **catalog}


async def _safe_read(source: Callable[[], Awaitable[list]]) -> list:
    """Safely read a catalog source, degrading to [] on any failure."""
    try:
        result = await source()
    except Exception:  # pragma: no cover - provider/host degrade; belt-and-braces
        return []
    return result if isinstance(result, list) else []


@router.get("/{project}/config-catalog")
async def config_catalog_endpoint(
    project: str,
    catalog_source: Callable[[], Awaitable[list]] = Depends(get_catalog_source),
    pi_catalog_source: Callable[[], Awaitable[list]] = Depends(get_pi_catalog_source),
    claude_catalog_source: Callable[[], Awaitable[list]] = Depends(get_claude_catalog_source),
    codex_catalog_source: Callable[[], Awaitable[list]] = Depends(get_codex_catalog_source),
) -> dict[str, Any]:
    """`GET /agents/{project}/config-catalog` — the FULL harness→model+reasoning
    option catalog as JSON, for the SPA Configure experience.

    The per-agent `GET /agents/{p}/{a}/detail` config-view only carries the CURRENT
    agent's resolved option set (the effective harness); the SPA needs EVERY
    harness's option sets up-front to repopulate the model/reasoning dropdowns
    CLIENT-SIDE when the harness <select> changes (no per-keystroke round-trip).
    Sourced from `harness.HARNESS_MODELS` / `HARNESS_REASONING` (the fixed
    subscription lanes) + the live Providers catalog (the own-harness lane) + the host PI
    catalog (`pi --list-models` via harness-service). Includes `project` in the
    payload.

    Graceful-degrade (house law): a providers-catalog fetch failure degrades to the
    FIXED lanes only (empty kaidera catalog, fixed PI fallback) — never a 500.
    `project` is accepted for URL-shape symmetry with the rest of the agents surface;
    the catalog itself is project-independent today (a future per-project provider
    scoping can use it).

    PATH NOTE (collision-free, strictly additive): the TWO-segment
    `/agents/{project}/config-catalog` leaf is registered on this router (mounted
    BEFORE `main`'s HTML routes), so it is matched ahead of the two-segment HTML
    agent-detail pane (`main` `GET /agents/{p}/{a}`); a literal `config-catalog`
    leaf also can't be confused with the three-segment `/detail` JSON route."""
    async def _read(source: Callable[[], Awaitable[list]]) -> list:
        return await _safe_read(source)

    catalog_groups, pi_catalog_groups, claude_models, codex_models = await asyncio.gather(
        _read(catalog_source),
        _read(pi_catalog_source),
        _read(claude_catalog_source),
        _read(codex_catalog_source),
    )
    catalog = build_config_catalog(
        harness_cfg,
        catalog_groups,
        pi_catalog_groups,
        claude_models,
        codex_models,
    )
    return {"project": project, **catalog}


@router.get("/{project}/{agent}/chat/history")
async def chat_history_endpoint(
    project: str,
    agent: str,
    session_id: str,
    store: Any = Depends(get_runstate_store),
) -> dict[str, Any]:
    """`GET /agents/{project}/{agent}/chat/history?session_id=…` — the prior turns
    of ONE chat conversation as JSON, oldest-first, so the SPA can restore a
    conversation after a page reload (the chat session_id is persisted in the
    browser; without this a reload minted a fresh session and the history
    "disappeared" even though it was always in run_state/run_span).

    Reuses the SAME `chat_history.load_session_history` the live chat path threads
    context from, so the restored turns are byte-identical to what was sent. Each
    turn is `{user, reply}`. Graceful-degrade (house law): a None / down store, a
    blank session_id, or any read failure yields `{turns: []}` — never a 500 — so
    the SPA renders an empty composer and a fresh chat still works.

    PATH NOTE (collision-free, strictly additive): the FOUR-segment
    `/agents/{project}/{agent}/chat/history` GET leaf is distinct from main's
    `POST /agents/{p}/{a}/chat` + `POST /agents/{p}/{a}/chat/upload` (different
    method + an extra `history` segment), and from this router's three-segment
    `GET /agents/{p}/{a}/detail`. The `session_id` query param is required (a blank
    value degrades to an empty turns list)."""
    from app import chat_history as chat_history_module

    sess = (session_id or "").strip()
    if store is None or not sess:
        return {"project": project, "agent": agent, "session_id": sess, "turns": []}
    try:
        turns = await chat_history_module.load_session_history(
            store, project, agent, sess
        )
    except Exception:  # pragma: no cover — store degrade; belt-and-braces
        turns = []
    return {
        "project": project,
        "agent": agent,
        "session_id": sess,
        "turns": [{"user": u, "reply": r} for (u, r) in turns],
    }


@router.get("/{project}/epics")
async def epics_endpoint(
    project: str,
    roster: Any = Depends(get_roster_source),
) -> dict[str, Any]:
    """`GET /agents/{project}/epics` — the col-2 Active-Epic widget + metrics block as JSON,
    for the SPA `AgentsColumn`.

    Shapes the live Cortex `/epics` (the per-epic progress + increments) + `/state` (the active-
    tasks / pending-handoffs / events-24h counters) + `/board` (the derived pending-tasks count)
    into `{project, epic, metrics}` via the PURE `app.agents.epics` helpers — the SAME shaping
    the legacy HTML col-2 uses (`main._epic_view` / `_metrics_view`), so JSON + HTML share one
    source. `epic.mode == 'epics'` carries the active-major epic stack; `'continuous'` is the
    'continuous · no epics' line.

    The three Cortex reads run CONCURRENTLY, each guarded (`_safe`) so one failure degrades
    alone; a None client (failed to construct) yields the clean continuous/empty payload. A
    degraded /state leaves the counters null (the SPA renders '—') — NEVER fabricated progress.
    Never a 500.

    PATH NOTE (collision-free, strictly additive): the TWO-segment `/agents/{project}/epics`
    leaf is registered on this router (mounted BEFORE `main`'s HTML routes) so it is matched
    ahead of the two-segment HTML agent-detail pane (`main` `GET /agents/{p}/{a}`); a literal
    `epics` leaf can't be confused with the three-segment `/detail` JSON route either."""
    cortex = roster if (roster is not None and hasattr(roster, "get_epics")) else None
    if cortex is None:
        # No usable client (None / a stand-in without the epic seams) → clean continuous payload.
        payload = epics_shape.build_epics_payload(None, None, None)
        return {"project": project, **payload}

    epics_payload, state, board = await asyncio.gather(
        _safe(cortex.get_epics(project), {"epics": []}),
        _safe(cortex.get_state(project), {}),
        _safe(cortex.get_board(project), []),
    )
    payload = epics_shape.build_epics_payload(
        epics_payload if isinstance(epics_payload, dict) else {"epics": []},
        state if isinstance(state, dict) else {},
        board if isinstance(board, list) else [],
    )
    return {"project": project, **payload}


@router.get("/{project}/{agent}/detail")
async def detail_endpoint(
    project: str,
    agent: str,
    store: OperationalStorePort = Depends(get_operational_store),
    roster: Any = Depends(get_roster_source),
    pi_catalog_source: Callable[[], Awaitable[list]] = Depends(get_pi_catalog_source),
    claude_catalog_source: Callable[[], Awaitable[list]] = Depends(get_claude_catalog_source),
    codex_catalog_source: Callable[[], Awaitable[list]] = Depends(get_codex_catalog_source),
) -> dict[str, Any]:
    """`GET /agents/{project}/{agent}/detail` — one agent resolved to its effective
    config/designation/role (+ the inline config-view) as JSON. Includes `project`
    in the payload. A name not in the roster is a clean 404 (never a 500).

    The THREE-segment `/detail` leaf keeps this from shadowing the existing
    two-segment HTML agent-detail pane (`main` `GET /agents/{p}/{a}`) — strictly
    additive (see the module docstring's PATH NOTE)."""
    agents, pi_catalog_groups, _claude_models, _codex_models = await asyncio.gather(
        roster.get_agents(project),
        _safe_read(pi_catalog_source),
        _safe_read(claude_catalog_source),
        _safe_read(codex_catalog_source),
    )
    svc = build_service(store)
    detail = await svc.get_agent(
        project, agent, agents, pi_catalog_groups=pi_catalog_groups,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="agent not found in project roster")
    return {"project": project, **detail}


__all__ = [
    "router",
    "list_endpoint",
    "detail_endpoint",
    "epics_endpoint",
    "config_catalog_endpoint",
    "get_operational_store",
    "get_roster_source",
    "get_catalog_source",
    "get_claude_catalog_source",
    "get_codex_catalog_source",
    "build_service",
]
