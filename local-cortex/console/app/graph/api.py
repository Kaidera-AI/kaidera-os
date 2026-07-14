"""Graph API — the imperative shell (FastAPI router) for the knowledge/code-graph surface.

The ONLY part of the graph module that imports fastapi. Two READ-ONLY endpoints that feed
the SPA `GraphView` cytoscape canvas a cytoscape-AGNOSTIC `{nodes, edges, stats}` JSON:

  * `GET /graph/{project}`                   — the SEED/default view. Runs a project-
    flavoured catch-all term against Cortex `/cortex-graph-search` (the API needs a term to
    return entities) so the canvas isn't empty on first paint.

  * `GET /graph/{project}/search?q=&limit=`  — re-centre on a search term. `q` drives the
    Cortex graph search; the matching entities + their 1-hop neighbours become the bounded
    graph. `limit` (optional) is how many entity hits to request (the neighbourhood then
    fans out from there); the rendered graph is capped at ~140 nodes regardless.

Both shape the dual-level Cortex payload (`high_level`/`low_level` entities → nodes coloured
by kind; `relationships` → edges) via `app.graph.shape.shape_graph`, capped at
`GRAPH_NODE_CAP`, and fold `/graph/stats` (own/total/repo counts) + the rendered/total
counts into `stats` via `repo_stats`. The shared `CortexClient` on `app.state.cortex`
supplies both seams (`graph_search` + `get_graph_stats`) — both already graceful-degrade to
{} on a down/empty Cortex, so the route returns `{nodes:[], edges:[], stats:{...0}}` and
NEVER 500s. No host forward, no DB, no mutation — a pure projection of the live graph.

PATH NOTE (additive, non-colliding): everything lives under the distinct `/graph/...`
prefix, so it can never shadow `/runs/...`, `/runstate/...`, `/dispatch/...`, `/agents/...`,
`/explain/...`, `/analytics/...`, or `/settings/...`.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.graph.shape import GRAPH_NODE_CAP, repo_stats, shape_graph, shape_memory_graph

router = APIRouter(prefix="/graph", tags=["graph"])

# Re-export the cap so tests + callers can reference the single source of truth.
_GRAPH_NODE_CAP = GRAPH_NODE_CAP

# The seed terms the default view runs against /cortex-graph-search when no explicit `q` is
# given (the API needs a term to return entities). The API entity search is a
# substring/similarity filter on entity name+description, so a single magic word (e.g.
# "architecture") yields an EMPTY canvas on any project whose entities don't happen to
# contain that token. We instead try a list of BROAD, structural seed terms (no per-project
# literal) and use the first that returns nodes — so the default view renders the existing
# graph for any project. Env-overridable: GRAPH_SEED_QUERY may set a comma-separated list
# that is tried FIRST (a per-deployment override), then the built-in fallbacks.
_SEED_TERMS_DEFAULT = [
    "api",
    "service",
    "endpoint",
    "project",
    "deployment",
    "cortex",
    "agent",
    "architecture",
    "data",
    "system",
]
_SEED_QUERY_ENV = os.environ.get("GRAPH_SEED_QUERY", "").strip()
_SEED_TERMS = (
    [t.strip() for t in _SEED_QUERY_ENV.split(",") if t.strip()] + _SEED_TERMS_DEFAULT
    if _SEED_QUERY_ENV
    else _SEED_TERMS_DEFAULT
)

# How many entity hits to REQUEST from /cortex-graph-search by default. We ask for more than
# the rendered node count so the one-hop expansion brings in a richer neighbourhood; the
# rendered graph is then bounded by GRAPH_NODE_CAP. Env-overridable (a tunable bound).
_DEFAULT_QUERY_LIMIT = int(os.environ.get("GRAPH_QUERY_LIMIT", "28"))


def _cortex(request: Request):
    """The shared `CortexClient` (the graph-search + stats seams) from app.state, or None
    when the client failed to construct (the route then degrades to an empty graph)."""
    return getattr(request.app.state, "cortex", None)


def _empty_stats() -> dict[str, Any]:
    """The all-zero/None stats block for a down/empty Cortex (never a 500)."""
    return {
        "own_nodes": None,
        "own_edges": None,
        "total_nodes": None,
        "total_edges": None,
        "repo_count": 0,
        "repos": [],
        "shown_nodes": 0,
        "shown_edges": 0,
        "total_shown_nodes": None,
        "kind_counts": {"code": 0, "mem": 0, "work": 0},
        "entity_count": 0,
        "relationship_count": 0,
        "source_counts": {},
        "backlog": {},
        "layers": _layer_stats({}, {}, {}, {"code": 0, "mem": 0, "work": 0}, ""),
        "node_cap": GRAPH_NODE_CAP,
        "capped": False,
    }


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _sum_values(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    return sum(value for value in data.values() if isinstance(value, int))


def _layer_stats(
    repo: dict[str, Any],
    l4: dict[str, Any],
    health: dict[str, Any],
    kind_counts: dict[str, int],
    project: str,
) -> list[dict[str, Any]]:
    """Return the six Cortex layer read-out for the graph surface.

    Counts are deliberately conservative: only show a number when this route has
    a reliable source for it. Unknowns stay unknown instead of being fabricated.
    """
    own_code_nodes = _int_or_none(repo.get("own_nodes"))
    own_code_edges = _int_or_none(repo.get("own_edges"))
    entity_count = _int_or_zero(l4.get("entity_count"))
    relationship_count = _int_or_zero(l4.get("relationship_count"))
    source_counts = l4.get("source_counts") if isinstance(l4.get("source_counts"), dict) else {}
    backlog = l4.get("backlog") if isinstance(l4.get("backlog"), dict) else {}
    backlog_total = _sum_values(backlog)
    work_products = _int_or_zero(source_counts.get("work_products") if isinstance(source_counts, dict) else 0)
    memory_sources = (
        _int_or_zero(source_counts.get("decisions") if isinstance(source_counts, dict) else 0)
        + _int_or_zero(source_counts.get("lessons") if isinstance(source_counts, dict) else 0)
        + _int_or_zero(source_counts.get("knowledge") if isinstance(source_counts, dict) else 0)
    )
    embed_model = health.get("embed_model") if isinstance(health, dict) else None
    embed_provider = health.get("embed_provider") if isinstance(health, dict) else None
    cortex_ok = (health.get("status") if isinstance(health, dict) else "") == "healthy"

    return [
        {
            "id": "L1",
            "name": "Operational memory",
            "status": "observed" if memory_sources or kind_counts.get("work", 0) else "empty",
            "count": memory_sources or None,
            "detail": "decisions, lessons, knowledge, agents, handoffs",
        },
        {
            "id": "L2",
            "name": "Vector retrieval",
            "status": "configured" if cortex_ok and embed_model else "unknown",
            "count": None,
            "detail": f"{embed_provider or 'provider'} · {embed_model or 'model not reported'}",
        },
        {
            "id": "L3",
            "name": "Code graph",
            "status": "ready" if own_code_nodes else "missing",
            "count": own_code_nodes,
            "edges": own_code_edges,
            "detail": "code graph worker repo match" if own_code_nodes else "no code graph for this project key",
        },
        {
            "id": "L4",
            "name": "Entity graph",
            "status": "ready" if entity_count else ("backlog" if backlog_total else "empty"),
            "count": entity_count,
            "edges": relationship_count,
            "backlog": backlog_total,
            "detail": "entities and relationships extracted from Cortex memory",
        },
        {
            "id": "L5",
            "name": "Work products",
            "status": "observed" if work_products else "not observed",
            "count": work_products or None,
            "detail": "Explain/artifacts/work-product memory",
        },
        {
            "id": "L6",
            "name": "Runtime boot",
            "status": "configured" if project else "unknown",
            "count": None,
            "detail": f"project registry and boot context for {project or 'selected project'}",
        },
    ]


async def _read_stats(cortex: Any, project: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        stats = await cortex.get_graph_stats(project)
    except Exception:
        stats = {}
    try:
        get_l4_stats = getattr(cortex, "get_cortex_graph_stats", None)
        l4_stats = await get_l4_stats(project) if callable(get_l4_stats) else {}
    except Exception:
        l4_stats = {}
    try:
        get_health = getattr(cortex, "get_health", None)
        health = await get_health() if callable(get_health) else {}
    except Exception:
        health = {}
    return (
        stats if isinstance(stats, dict) else {},
        l4_stats if isinstance(l4_stats, dict) else {},
        health if isinstance(health, dict) else {},
    )


def _stats_block(
    *,
    repo_raw: dict[str, Any],
    l4_stats: dict[str, Any],
    health: dict[str, Any],
    shaped: dict[str, Any],
    project: str,
    mode: str,
) -> dict[str, Any]:
    repo = repo_stats(repo_raw, project)
    total_shown = repo["own_nodes"]
    if mode == "memory":
        entity_count = l4_stats.get("entity_count") if isinstance(l4_stats.get("entity_count"), int) else None
        source_counts = l4_stats.get("source_counts") if isinstance(l4_stats.get("source_counts"), dict) else {}
        source_total = _sum_values(source_counts)
        total_shown = (entity_count + source_total) if isinstance(entity_count, int) else None
    if total_shown is None and isinstance(l4_stats, dict):
        entity_count = l4_stats.get("entity_count")
        if isinstance(entity_count, int) and entity_count > 0:
            total_shown = entity_count
    if total_shown is None:
        total_shown = repo["total_nodes"]
    return {
        **repo,
        "shown_nodes": shaped["shown_nodes"],
        "shown_edges": shaped["shown_edges"],
        "total_shown_nodes": total_shown,
        "kind_counts": shaped["kind_counts"],
        "entity_count": l4_stats.get("entity_count", 0),
        "relationship_count": l4_stats.get("relationship_count", 0),
        "source_counts": l4_stats.get("source_counts", {}),
        "backlog": l4_stats.get("backlog", {}),
        "layers": _layer_stats(repo, l4_stats, health, shaped["kind_counts"], project),
        "node_cap": GRAPH_NODE_CAP,
        "capped": shaped["shown_nodes"] >= GRAPH_NODE_CAP,
        "mode": mode,
    }


async def _build_graph(request: Request, project: str, term: str, limit: int) -> dict[str, Any]:
    """Fetch + shape the `{nodes, edges, stats}` payload for one project + term.

    Pulls Cortex `/cortex-graph-search` (expanded, for one-hop neighbours) + `/graph/stats`,
    shapes the dual-level entities/relationships into bounded nodes/edges, and folds the
    repo + shown counts into `stats`. Graceful-degrade rides through the CortexClient
    (both seams return {} on error); a None client or a raising shaping step both yield the
    clean empty graph — this coroutine NEVER raises."""
    cortex = _cortex(request)
    if cortex is None:
        return {"nodes": [], "edges": [], "stats": _empty_stats()}

    # Both reads graceful-degrade to {} inside the client; we still guard the calls so a
    # surprising raise (a fake/older client) can't 500 the route.
    explicit = (term or "").strip()
    # SEED PATH (no explicit term): try the broad structural seed terms in order and keep
    # the FIRST result that actually yields entities, so the default canvas renders the
    # existing graph regardless of which words a project's entities contain. SEARCH PATH
    # (explicit term): unchanged — run exactly that one query.
    candidates = [explicit] if explicit else _SEED_TERMS
    search: dict[str, Any] = {}
    for cand in candidates:
        try:
            result = await cortex.graph_search(project, cand, limit=limit, expand=True)
        except Exception:
            result = {}
        if isinstance(result, dict) and (
            result.get("high_level") or result.get("low_level")
        ):
            search = result
            break
        # Remember the last (possibly empty) result so an all-empty project still shapes
        # cleanly to an empty graph rather than dropping to {} unexpectedly.
        if isinstance(result, dict):
            search = result
    stats, l4_stats, health = await _read_stats(cortex, project)

    try:
        shaped = shape_graph(search if isinstance(search, dict) else {})
    except Exception:
        shaped = {
            "nodes": [], "edges": [], "shown_nodes": 0, "shown_edges": 0,
            "kind_counts": {"code": 0, "mem": 0, "work": 0},
        }

    stats_block = _stats_block(
        repo_raw=stats,
        l4_stats=l4_stats,
        health=health,
        shaped=shaped,
        project=project,
        mode="search",
    )
    return {"nodes": shaped["nodes"], "edges": shaped["edges"], "stats": stats_block}


async def _build_memory_graph(request: Request, project: str, limit: int) -> dict[str, Any]:
    cortex = _cortex(request)
    if cortex is None:
        return {"nodes": [], "edges": [], "stats": _empty_stats()}
    try:
        get_memory = getattr(cortex, "get_cortex_memory_graph", None)
        raw = await get_memory(project, limit=limit) if callable(get_memory) else {}
    except Exception:
        raw = {}
    stats, l4_stats, health = await _read_stats(cortex, project)
    try:
        shaped = shape_memory_graph(raw if isinstance(raw, dict) else {})
    except Exception:
        shaped = {
            "nodes": [], "edges": [], "shown_nodes": 0, "shown_edges": 0,
            "kind_counts": {"code": 0, "mem": 0, "work": 0},
        }
    stats_block = _stats_block(
        repo_raw=stats,
        l4_stats=l4_stats,
        health=health,
        shaped=shaped,
        project=project,
        mode="memory",
    )
    return {"nodes": shaped["nodes"], "edges": shaped["edges"], "stats": stats_block}


@router.get("/{project}")
async def graph_seed(project: str, request: Request) -> JSONResponse:
    """The SEED/default graph for `project` — a project-flavoured catch-all term's
    bounded neighbourhood. `{nodes, edges, stats}`; empty (200) on a down/empty Cortex."""
    payload = await _build_graph(request, project, "", _DEFAULT_QUERY_LIMIT)
    return JSONResponse(payload)


@router.get("/{project}/search")
async def graph_search(project: str, request: Request) -> JSONResponse:
    """Re-centre the graph on a search term — `?q=<term>&limit=<n>`. The matching entities
    + their 1-hop neighbours become the bounded graph. `{nodes, edges, stats}`; a blank `q`
    falls back to the seed term; empty (200) on a down/empty Cortex (never a 500)."""
    q = (request.query_params.get("q") or "").strip()
    limit = _DEFAULT_QUERY_LIMIT
    raw_limit = request.query_params.get("limit")
    if raw_limit:
        try:
            parsed = int(raw_limit)
            if parsed > 0:
                # Bound the requested hit count so a silly ?limit=99999 can't hammer Cortex;
                # the rendered graph is capped at GRAPH_NODE_CAP regardless.
                limit = min(parsed, 200)
        except ValueError:
            pass
    payload = await _build_graph(request, project, q, limit)
    return JSONResponse(payload)


@router.get("/{project}/memory")
async def graph_memory(project: str, request: Request) -> JSONResponse:
    """The project-scoped Cortex memory graph: all current L4 entities/relationships,
    bounded for browser safety. `{nodes, edges, stats}`; empty (200) on down Cortex."""
    raw_limit = request.query_params.get("limit")
    limit = 500
    if raw_limit:
        try:
            parsed = int(raw_limit)
            if parsed > 0:
                limit = min(parsed, 2000)
        except ValueError:
            pass
    payload = await _build_memory_graph(request, project, limit)
    return JSONResponse(payload)


__all__ = ["router", "_GRAPH_NODE_CAP"]
