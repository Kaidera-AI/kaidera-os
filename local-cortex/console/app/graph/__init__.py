"""The `graph` feature module — the console-side knowledge/code-graph JSON surface.

The Cortex knowledge graph (L4 entities + L3 code-graph edges) is rendered in the SPA as
an interactive node-edge canvas (`GraphView`, cytoscape). THIS module is the clean backend
that feeds it: a small read-only FastAPI surface that shapes Cortex's dual-level
`/cortex-graph-search` payload into a cytoscape-AGNOSTIC `{nodes, edges, stats}` JSON
(id/label/kind for nodes, id/source/target/label for edges), bounded so a ~5,868-node graph
never ships whole.

  * `api.py` — a FastAPI `APIRouter` (the only part that imports fastapi):
      - `GET /graph/{project}`                   — the SEED/default view.
      - `GET /graph/{project}/search?q=&limit=`  — re-centre on a search term.

`main.py` mounts the router additively (`app.include_router(graph.router)`). It reads the
shared `CortexClient` on `app.state.cortex` (the `graph_search` + `get_graph_stats` seams,
both already graceful-degrading) — no host forward, no DB, no mutation. The shaping logic
(`shape_graph` + the node-kind palette + the ~140 node cap) is PORTED from the legacy
HTML graph view's context-builder (`main._graph_elements`) so the bounded-neighbourhood
behaviour matches, just emitted as JSON for the SPA instead of a Jinja island.

SCOPE — read + shape only. No generation, no write; this is a pure projection of the live
Cortex graph search, capped + typed for the canvas.
"""

from app.graph.api import router
from app.graph.shape import GRAPH_NODE_CAP, shape_graph

__all__ = ["router", "shape_graph", "GRAPH_NODE_CAP"]
