"""Dispatch API — the imperative shell for the `dispatch` module.

A FastAPI `APIRouter` exposing the dispatch BOARD as typed JSON. This is the ONLY
part of the module that imports fastapi (the layer rule: the service is pure; the
shell does I/O + wiring). One endpoint:

  * `GET /dispatch/{project}/board` — the dispatch board (open-handoff rows each with
                                      a proposed agent + the board counts + the
                                      project autonomy/propose-mode flags).

The endpoint:
  * resolves the `CortexMemoryPort` for the project (the dispatch queue source) — a
    `CortexMemoryAdapter` bound to the path project over the app's `app.state.cortex`
    client — via `Depends`, so the route depends on the PORT, not the concrete
    client,
  * resolves the `OperationalStorePort` from `app.state` (the adapter the app wired
    at startup) via `Depends` — for the autonomy + propose-mode flag reads,
  * resolves the ROSTER source (the Cortex client) from `app.state` via `Depends` and
    fetches the project roster (the board is queue-shaping over the roster; the
    roster's origin is the Cortex registry),
  * constructs the `DispatchService` over the ports (injecting the real `harness`-
    backed config resolver, the store-backed per-agent override reader, and the
    `settings.list_awaiting_approval` lister so the JSON board matches the HTML view
    exactly), and returns the shaped JSON.

`main.py` mounts this additively (`app.include_router(dispatch.router)`); the existing
HTML Dispatch center delegates its board substance to the SAME `DispatchService`, so
the JSON API and the HTML surface share one source of board logic.

PATH NOTE (additive, non-colliding): the existing live dispatch routes are
`POST /dispatch/{project_key}/autonomous` (the autonomy toggle) +
`POST /dispatch/{project_key}/run` (Approve & Run) — both POST. The board JSON lives
at `GET /dispatch/{project}/board`: a distinct `/board` leaf AND a GET, so it can
NEVER shadow those POST routes, and the different root keeps it clear of the
`GET /stream` SSE proxy. Strictly additive (verified by
`test_router_board_path_does_not_collide`).

Graceful-degrade rides through from the service/ports (a down Cortex → an empty
board, a down store → fail-safe-OFF flags); the board never raises a 500."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app import harness as harness_cfg
from app import settings as settings_store
from app.dispatch.service import DispatchService
from app.domain.ports import CortexMemoryPort, OperationalStorePort

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


# Default routing for an unconfigured agent on the default claude-code lane — only
# applied to fill the default model when the effective harness is the default and
# nothing else is set (the same guard main._proposed_agent uses via _agents_resolve_config).
_DEFAULT_HARNESS = "claude-code"
_DEFAULT_MODEL = "claude-opus-4-8[1m]"  # the default model id, per harness.HARNESS_MODELS


def _harness_resolve_config(agent: dict, override: dict) -> dict:
    """The real per-agent config resolver (override-first → registry → default),
    wrapping `harness._registry_config` + `canonical_harness` + `harness_label` — so
    the proposal's harness/model match the runner + the Configure card exactly.
    Mirrors `main._proposed_agent`'s resolution block 1:1 (the same values the
    `_agents_resolve_config` injection computes)."""
    reg = harness_cfg._registry_config(agent)
    harness = harness_cfg.canonical_harness(
        override.get("harness") or reg["harness"]
    ) or _DEFAULT_HARNESS
    model = (override.get("model") or reg["model"] or "").strip() or None
    # fill the default model only on the default claude-code lane (don't hand a
    # claude model to a different harness) — same guard as main.
    if harness == _DEFAULT_HARNESS and model is None:
        model = _DEFAULT_MODEL
    return {
        "harness": harness,
        "harness_label": harness_cfg.harness_label(harness),
        "model": model,
    }


def get_cortex_memory(request: Request) -> CortexMemoryPort:
    """Resolve the `CortexMemoryPort` for the request, bound to the path project.

    Wraps the app's shared `app.state.cortex` client in a `CortexMemoryAdapter` bound
    to the request's project (constructed at this composition seam so the route
    receives the PORT, never the concrete client). The board reads the pending
    dispatch queue through it."""
    from app.adapters.cortex_memory import CortexMemoryAdapter

    project = (request.path_params.get("project") or "").strip()
    return CortexMemoryAdapter(request.app.state.cortex, project_key=project)


def get_operational_store(request: Request) -> OperationalStorePort:
    """Resolve the `OperationalStorePort` for the request.

    Prefers a pre-wired `app.state.opstore` (an `AppDbOperationalStore`); falls back
    to wrapping the live `app.state.appdb` so the route works even before the app
    explicitly stashes the adapter. Constructed at this composition seam so callers
    receive the PORT, never the concrete store (mirrors the analytics/agents
    resolver)."""
    state = request.app.state
    store = getattr(state, "opstore", None)
    if store is not None:
        return store
    from app.adapters.opstore import AppDbOperationalStore

    return AppDbOperationalStore(appdb=state.appdb)


def get_roster_source(request: Request) -> Any:
    """Resolve the ROSTER source — the Cortex client stashed on `app.state` (it
    exposes `get_agents(project)`). The board service consumes the roster the caller
    fetches; this is the composition seam that supplies it."""
    return request.app.state.cortex


def build_service(
    cortex: CortexMemoryPort, store: OperationalStorePort
) -> DispatchService:
    """Construct the dispatch service over the ports, injecting the concrete harness-
    backed config resolver, the store-backed per-agent override reader, and the
    `settings.list_awaiting_approval` lister (so the JSON board matches the HTML)."""
    # Bind the per-agent override reader defensively: the OperationalStorePort
    # declares `get_agent_override`, but binding via getattr keeps wiring robust to a
    # port variant that omits it (→ "no override", the proposal falls back to the
    # registry config) — the house-law graceful degrade at the composition seam.
    get_override = getattr(store, "get_agent_override", lambda project, agent: {})
    return DispatchService(
        cortex=cortex,
        store=store,
        resolve_config=_harness_resolve_config,
        get_override=get_override,
        awaiting_approval=settings_store.list_awaiting_approval,
        # ROLE-ALIAS designation reader — so the JSON board's proposal routes a
        # `cpo`/lead to_role to the project's INTERACTIVE lead exactly like the HTML
        # board + the orchestrator. The classifier is left at the domain's registry-
        # heuristic default: this module is module-INDEPENDENT (the import-linter
        # contract forbids it importing `app.agents`), and the domain default mirrors
        # `agents.service.classify_interactive`, so the override-first designation read
        # here + that heuristic give the same lead the HTML path resolves.
        designation_of=settings_store.get_agent_designation,
        # Role ALIAS reader — mirror the HTML board's alias resolution by reading the
        # console override `role_aliases` field, so the JSON proposal never diverges.
        role_aliases_of=lambda project, agent: get_override(project, agent).get("role_aliases", ""),
    )


@router.get("/{project}/board")
async def board_endpoint(
    project: str,
    cortex: CortexMemoryPort = Depends(get_cortex_memory),
    store: OperationalStorePort = Depends(get_operational_store),
    roster: Any = Depends(get_roster_source),
) -> dict[str, Any]:
    """`GET /dispatch/{project}/board` — the project's dispatch board as JSON: the
    sorted open-handoff rows (each with a rule-based proposed agent), the board
    counts (total / proposed / unassigned), and the project autonomy/propose-mode
    flags + the awaiting-approval id set. Includes `project` in the payload.

    The dispatch queue is read through the `CortexMemoryPort` (pending handoffs); the
    roster is fetched from Cortex (`roster.get_agents(project)`) for the proposal
    match; the flags come from the `OperationalStorePort`. A down Cortex yields an
    empty board, a down store yields fail-safe-OFF flags — never a 500.

    The `awaiting_approval_ids` set is JSON-serialized as a sorted list (a Python set
    isn't JSON-native)."""
    agents = await roster.get_agents(project)
    svc = build_service(cortex, store)
    board = await svc.board(project, agents)
    # a set isn't JSON-serializable — surface the parked ids as a sorted list.
    board["awaiting_approval_ids"] = sorted(board["awaiting_approval_ids"])
    return {"project": project, **board}


__all__ = [
    "router",
    "board_endpoint",
    "get_cortex_memory",
    "get_operational_store",
    "get_roster_source",
    "build_service",
]
