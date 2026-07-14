"""Console Graph route tests (`app/graph/api.py`).

The console-side knowledge/code-graph JSON surface — the clean backend for the SPA
`GraphView` (cytoscape canvas). Two READ-ONLY endpoints:

  * `GET /graph/{project}`              — the SEED/default view (a project-flavoured
                                          catch-all term against /cortex-graph-search).
  * `GET /graph/{project}/search?q=&limit=` — re-centre on a search term.

Both return `{nodes, edges, stats}`:
  - `nodes` shaped from the dual-level entities (high_level + low_level) → one node per
    entity name, coloured/typed by `kind` (code | mem | work), search hits flagged `hit`.
  - `edges` from the `relationships` (source→target, the relationship_type as the label);
    relationship endpoints NOT in the direct hits are synthesised as 1-hop NEIGHBOUR nodes.
  - HARD-CAPPED at ~140 nodes (the legacy `_GRAPH_NODE_CAP`) so a big graph never ships
    whole; `stats` carries own/total/shown counts + the repo table.

Driven via an in-process httpx ASGITransport over a minimal app that mounts the router,
with a FAKE cortex on `app.state.cortex` (a scripted `graph_search` + `get_graph_stats`) —
NO live Cortex, nothing spawned. The shaping is cytoscape-AGNOSTIC (id/label/kind/source/
target), so these assertions check the data shape, not any canvas.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.graph.api import _GRAPH_NODE_CAP, router as graph_router


class FakeCortexForGraph:
    """A minimal CortexClient stand-in: scriptable graph_search + get_graph_stats.

    `search_payload` is the dual-level dict the live /cortex-graph-search returns
    ({high_level, low_level, relationships}); `stats_payload` is the /graph/stats dict
    ({repos:[{name,nodes,edges,path}], total_nodes, total_edges}). Either may be set to
    raise to exercise the graceful-degrade path. Records the queries it was asked."""

    def __init__(self, *, search_payload=None, stats_payload=None, l4_stats_payload=None,
                 health_payload=None, memory_payload=None, search_raises=False, stats_raises=False):
        self._search = search_payload if search_payload is not None else {}
        self._stats = stats_payload if stats_payload is not None else {}
        self._l4_stats = l4_stats_payload if l4_stats_payload is not None else {}
        self._health = health_payload if health_payload is not None else {}
        self._memory = memory_payload if memory_payload is not None else {}
        self._search_raises = search_raises
        self._stats_raises = stats_raises
        self.search_calls = []
        self.stats_calls = []
        self.l4_stats_calls = []
        self.memory_calls = []

    async def graph_search(self, project_key, query, limit=8, expand=False):
        self.search_calls.append({"project": project_key, "query": query,
                                  "limit": limit, "expand": expand})
        if self._search_raises:
            raise RuntimeError("cortex graph down")
        return self._search

    async def get_graph_stats(self, project_key):
        self.stats_calls.append({"project": project_key})
        if self._stats_raises:
            raise RuntimeError("cortex stats down")
        return self._stats

    async def get_cortex_graph_stats(self, project_key):
        self.l4_stats_calls.append({"project": project_key})
        return self._l4_stats

    async def get_health(self):
        return self._health

    async def get_cortex_memory_graph(self, project_key, limit=500):
        self.memory_calls.append({"project": project_key, "limit": limit})
        return self._memory


# A representative dual-level payload: 2 low-level (concrete) hits, 1 high-level concept,
# and 3 relationships — one of which points at an endpoint ("orchestrator.py") NOT in the
# direct hits, so it must be synthesised as a 1-hop neighbour node.
_SEARCH = {
    "high_level": [
        {"id": "h1", "entity_type": "concept", "name": "dispatch flow",
         "description": "how work is dispatched", "score": 0.7},
    ],
    "low_level": [
        {"id": "l1", "entity_type": "file", "name": "app/main.py",
         "description": "the console app", "score": 0.9},
        {"id": "l2", "entity_type": "handoff", "name": "abcd:5872",
         "description": "a pending handoff", "score": 0.6},
    ],
    "relationships": [
        {"source": "app/main.py", "source_type": "file",
         "relationship_type": "defines", "target": "dispatch flow",
         "target_type": "concept", "description": "main defines the flow"},
        {"source": "app/main.py", "source_type": "file",
         "relationship_type": "imports", "target": "orchestrator.py",
         "target_type": "file", "description": "main imports the orchestrator"},
        {"source": "dispatch flow", "source_type": "concept",
         "relationship_type": "tracked_by", "target": "abcd:5872",
         "target_type": "handoff", "description": "the flow is tracked by the handoff"},
    ],
}

_STATS = {
    "repos": [
        {"name": "kaidera-os", "nodes": 5868, "edges": 44000, "path": "/abs/kaidera-os"},
        {"name": "kaidera", "nodes": 1200, "edges": 9000, "path": "/abs/kaidera"},
    ],
    "total_nodes": 7068,
    "total_edges": 53000,
}

_STATS_CASE_VARIANT = {
    "repos": [
        {"name": "Marketing", "nodes": 222, "edges": 333, "path": "/abs/Marketing"},
    ],
    "total_nodes": 222,
    "total_edges": 333,
}


def _make_app(*, cortex=None):
    app = FastAPI()
    app.include_router(graph_router)
    app.state.cortex = cortex if cortex is not None else FakeCortexForGraph()
    return app


def _client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t.test")


def _node_by_id(nodes, nid):
    for n in nodes:
        if n["id"] == nid:
            return n
    return None


# ---------------------------------------------------------------------------
#  GET /graph/{project}/search — entities → nodes, relationships → edges
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_search_shapes_nodes_and_edges():
    cortex = FakeCortexForGraph(search_payload=_SEARCH, stats_payload=_STATS)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/kaidera-os/search", params={"q": "dispatch"})
    assert resp.status_code == 200
    data = resp.json()

    nodes = data["nodes"]
    edges = data["edges"]
    ids = {n["id"] for n in nodes}

    # Every entity hit became a node, keyed by name.
    assert "app/main.py" in ids
    assert "dispatch flow" in ids
    assert "abcd:5872" in ids
    # The relationship endpoint NOT in the direct hits was synthesised as a neighbour node.
    assert "orchestrator.py" in ids

    # Node kinds mapped from entity_type → the 3 families.
    assert _node_by_id(nodes, "app/main.py")["kind"] == "code"     # file
    assert _node_by_id(nodes, "abcd:5872")["kind"] == "work"       # handoff
    assert _node_by_id(nodes, "dispatch flow")["kind"] == "mem"    # concept
    assert _node_by_id(nodes, "orchestrator.py")["kind"] == "code"  # file (synthesised)

    # A node carries a cytoscape-agnostic shape (id + label + kind), and direct hits flag.
    main = _node_by_id(nodes, "app/main.py")
    assert main["label"]
    assert main["hit"] == 1
    # The synthesised neighbour is NOT a hit.
    assert _node_by_id(nodes, "orchestrator.py")["hit"] == 0

    # Edges carry source/target + the relationship label.
    pairs = {(e["source"], e["target"]) for e in edges}
    assert ("app/main.py", "dispatch flow") in pairs
    assert ("app/main.py", "orchestrator.py") in pairs
    assert ("dispatch flow", "abcd:5872") in pairs
    # Every edge has a stable id + a label from the relationship_type.
    one = next(e for e in edges if e["source"] == "app/main.py" and e["target"] == "orchestrator.py")
    assert one["id"]
    assert one["label"] == "imports"

    # The query was forwarded with one-hop expansion (so neighbours come back).
    assert cortex.search_calls and cortex.search_calls[0]["query"] == "dispatch"
    assert cortex.search_calls[0]["expand"] is True


@pytest.mark.asyncio
async def test_graph_search_computes_stats():
    cortex = FakeCortexForGraph(search_payload=_SEARCH, stats_payload=_STATS)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/kaidera-os/search", params={"q": "dispatch"})
    stats = resp.json()["stats"]

    # Own repo (name == project) node/edge counts.
    assert stats["own_nodes"] == 5868
    assert stats["own_edges"] == 44000
    # Cross-repo totals.
    assert stats["total_nodes"] == 7068
    assert stats["total_edges"] == 53000
    # Rendered counts == what's in the payload (4 nodes, 3 edges here, well under the cap).
    assert stats["shown_nodes"] == 4
    assert stats["shown_edges"] == 3
    # The repo table is carried for context (own flagged).
    assert isinstance(stats["repos"], list) and stats["repos"]
    own = next(r for r in stats["repos"] if r["name"] == "kaidera-os")
    assert own["is_own"] is True
    # Not capped at this size.
    assert stats["capped"] is False
    assert stats["node_cap"] == _GRAPH_NODE_CAP


@pytest.mark.asyncio
async def test_graph_stats_match_own_repo_case_insensitively():
    cortex = FakeCortexForGraph(search_payload=_SEARCH, stats_payload=_STATS_CASE_VARIANT)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/marketing/search", params={"q": "dispatch"})
    stats = resp.json()["stats"]

    assert stats["own_nodes"] == 222
    assert stats["own_edges"] == 333
    assert stats["repos"][0]["name"] == "Marketing"
    assert stats["repos"][0]["is_own"] is True


@pytest.mark.asyncio
async def test_graph_stats_include_l4_and_six_layer_status():
    l4 = {
        "entity_count": 262,
        "relationship_count": 556,
        "source_counts": {"decisions": 10, "lessons": 2, "knowledge": 3, "work_products": 1},
        "backlog": {"decisions": 5, "lessons": 0, "knowledge": 1, "work_products": 0},
    }
    health = {
        "status": "healthy",
        "embed_provider": "openrouter",
        "embed_model": "nvidia/llama-nemotron-embed-vl-1b-v2:free",
    }
    cortex = FakeCortexForGraph(
        search_payload=_SEARCH,
        stats_payload={"repos": [], "total_nodes": 0, "total_edges": 0},
        l4_stats_payload=l4,
        health_payload=health,
    )
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/marketing/search", params={"q": "marlow"})
    stats = resp.json()["stats"]

    assert stats["entity_count"] == 262
    assert stats["relationship_count"] == 556
    assert stats["source_counts"]["work_products"] == 1
    assert stats["backlog"]["decisions"] == 5
    assert stats["total_shown_nodes"] == 262
    layers = {layer["id"]: layer for layer in stats["layers"]}
    assert layers["L2"]["status"] == "configured"
    assert "nvidia/llama" in layers["L2"]["detail"]
    assert layers["L3"]["status"] == "missing"
    assert layers["L4"]["status"] == "ready"
    assert layers["L4"]["count"] == 262
    assert layers["L4"]["backlog"] == 6


@pytest.mark.asyncio
async def test_graph_search_caps_at_node_limit():
    """A payload bigger than the cap renders at most _GRAPH_NODE_CAP nodes (search hits
    first), keeps only edges whose BOTH endpoints survived, and flags capped=True."""
    # Build 200 distinct low-level entity hits — over the 140 cap.
    big_low = [
        {"id": f"l{i}", "entity_type": "file", "name": f"file_{i}.py",
         "description": f"file {i}"}
        for i in range(200)
    ]
    # A chain of relationships among the first handful (both endpoints are hits).
    rels = [
        {"source": f"file_{i}.py", "source_type": "file",
         "relationship_type": "imports", "target": f"file_{i+1}.py",
         "target_type": "file"}
        for i in range(10)
    ]
    payload = {"high_level": [], "low_level": big_low, "relationships": rels}
    cortex = FakeCortexForGraph(search_payload=payload, stats_payload=_STATS)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/kaidera-os/search", params={"q": "files"})
    data = resp.json()
    assert len(data["nodes"]) == _GRAPH_NODE_CAP
    assert data["stats"]["shown_nodes"] == _GRAPH_NODE_CAP
    assert data["stats"]["capped"] is True
    # Edges only between surviving nodes.
    kept_ids = {n["id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["source"] in kept_ids and e["target"] in kept_ids


@pytest.mark.asyncio
async def test_graph_search_respects_limit_param():
    cortex = FakeCortexForGraph(search_payload=_SEARCH, stats_payload=_STATS)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        await c.get("/graph/kaidera-os/search", params={"q": "dispatch", "limit": 25})
    # The limit is forwarded to the Cortex graph_search.
    assert cortex.search_calls[0]["limit"] == 25


# ---------------------------------------------------------------------------
#  GET /graph/{project}/memory — project-scoped memory graph
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_memory_uses_project_memory_endpoint():
    cortex = FakeCortexForGraph(
        stats_payload=_STATS,
        l4_stats_payload={"entity_count": 2, "relationship_count": 1, "source_counts": {"decisions": 1}, "backlog": {}},
        memory_payload={
            "nodes": [
                {"name": "Marlow", "entity_type": "agent", "description": "lead"},
                {"name": "Publishing cadence", "entity_type": "concept", "description": "daily plan"},
            ],
            "edges": [
                {"id": "r1", "source": "Marlow", "target": "Publishing cadence",
                 "relationship_type": "owns"},
            ],
            "sources": [
                {
                    "id": "source:decisions:dec-1",
                    "source_id": "dec-1",
                    "source_table": "decisions",
                    "source_type": "decision",
                    "label": "Keep daily publishing cadence",
                    "description": "decision source row",
                }
            ],
            "source_edges": [
                {
                    "id": "se1",
                    "source": "source:decisions:dec-1",
                    "target": "Publishing cadence",
                    "relationship_type": "extracted_entity",
                }
            ],
        },
    )
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/marketing/memory")
    assert resp.status_code == 200
    data = resp.json()
    full = [n["full"] for n in data["nodes"]]
    assert full == ["Marlow", "Publishing cadence", "Keep daily publishing cadence"]
    assert {e["label"] for e in data["edges"]} == {"owns", "extracted_entity"}
    source = next(node for node in data["nodes"] if node["id"] == "source:decisions:dec-1")
    assert source["etype"] == "decision"
    assert source["kind"] == "mem"
    assert data["stats"]["mode"] == "memory"
    assert data["stats"]["total_shown_nodes"] == 3
    assert cortex.memory_calls == [{"project": "marketing", "limit": 500}]


# ---------------------------------------------------------------------------
#  GET /graph/{project} — the seed/default view
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_seed_uses_a_default_term():
    """The seed view (no ?q) still returns a populated graph by running a project-
    flavoured catch-all term against /cortex-graph-search (the API needs a term)."""
    cortex = FakeCortexForGraph(search_payload=_SEARCH, stats_payload=_STATS)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/kaidera-os")
    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"]  # seeded, not empty
    # A non-blank seed query was used.
    assert cortex.search_calls and cortex.search_calls[0]["query"].strip()


class _SeedFallthroughCortex:
    """Returns an EMPTY graph for the first N seed terms then a populated graph —
    models the DXB case where the old magic seed word ("architecture") matched no
    entity but other structural terms ("api"/"endpoint") match the real graph."""

    def __init__(self, *, match_terms, payload, stats_payload):
        self._match = set(match_terms)
        self._payload = payload
        self._stats = stats_payload
        self.search_calls = []

    async def graph_search(self, project_key, query, limit=8, expand=False):
        self.search_calls.append({"project": project_key, "query": query,
                                  "limit": limit, "expand": expand})
        return self._payload if query in self._match else {}

    async def get_graph_stats(self, project_key):
        return self._stats


@pytest.mark.asyncio
async def test_graph_seed_falls_through_to_a_matching_term():
    """DXB regression: when the leading seed terms match NO entity, the seed view
    keeps trying and renders the first term that returns nodes (graph not blank)."""
    # Only "endpoint" returns data; the earlier candidates ("api", "service", ...)
    # all return {} — the seed must not stop at the first empty result.
    cortex = _SeedFallthroughCortex(
        match_terms={"endpoint"}, payload=_SEARCH, stats_payload=_STATS
    )
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/dxb")
    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"], "seed view must render the existing graph, not blank"
    # It tried more than one term and landed on the matching one.
    tried = [c["query"] for c in cortex.search_calls]
    assert "endpoint" in tried
    assert len(tried) > 1


# ---------------------------------------------------------------------------
#  Graceful degrade — a down/empty Cortex never 500s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_degrades_to_empty_on_down_cortex():
    """A graph_search that RAISES (and stats that raise) degrades to
    {nodes:[], edges:[], stats:{...0}} with HTTP 200 — never a 500."""
    cortex = FakeCortexForGraph(search_raises=True, stats_raises=True)
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/kaidera-os/search", params={"q": "anything"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"] == []
    assert data["edges"] == []
    assert data["stats"]["shown_nodes"] == 0
    assert data["stats"]["shown_edges"] == 0
    assert data["stats"]["own_nodes"] is None
    assert data["stats"]["total_nodes"] is None


@pytest.mark.asyncio
async def test_graph_empty_payload_is_clean_empty():
    """An empty (but reachable) Cortex → empty nodes/edges, zeroed rendered counts, 200."""
    cortex = FakeCortexForGraph(search_payload={}, stats_payload={})
    app = _make_app(cortex=cortex)
    async with _client(app) as c:
        resp = await c.get("/graph/kaidera-os/search", params={"q": "nothing"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"] == []
    assert data["edges"] == []
    assert data["stats"]["shown_nodes"] == 0


@pytest.mark.asyncio
async def test_graph_no_cortex_on_state_degrades():
    """If app.state.cortex is None (the client failed to construct) the route still
    answers an empty graph, never a 500/AttributeError."""
    app = FastAPI()
    app.include_router(graph_router)
    app.state.cortex = None
    async with _client(app) as c:
        resp = await c.get("/graph/kaidera-os/search", params={"q": "x"})
    assert resp.status_code == 200
    assert resp.json()["nodes"] == []
