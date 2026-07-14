"""Pure shaping: Cortex dual-level graph search → bounded cytoscape-agnostic nodes/edges.

NO I/O — stdlib only. The Cortex `/cortex-graph-search` returns a dual-level payload
({high_level, low_level, relationships}); `shape_graph` turns it into
`{nodes, edges, stats}` where:

  - `nodes`  — one node per entity name, `{id, label, full, kind, etype, desc, hit}`.
               `kind` ∈ code | mem | work (the node-kind palette); `hit=1` for a direct
               search-entity, `0` for a synthesised 1-hop neighbour. Cytoscape-AGNOSTIC
               (the SPA maps id/label/kind onto cytoscape elements).
  - `edges`  — `{id, source, target, label}` from the relationships (the relationship_type
               is the label). An undirected pair+type is de-duplicated.
  - HARD-BOUNDED at `GRAPH_NODE_CAP` nodes (search hits first, then the most-connected
               neighbours) + `GRAPH_EDGE_CAP` edges (only those whose BOTH endpoints
               survived the node cap) — this is what keeps a ~5,868-node / 44k-edge graph
               from ever shipping whole to the browser. PORTED from `main._graph_elements`.

`stats` is computed by the api layer (it needs `/graph/stats`); this module returns the
RENDERED node/edge counts + the per-kind tally only.
"""

from __future__ import annotations

from typing import Any

# HARD CAP on shown nodes (the legacy `_GRAPH_NODE_CAP`). A real project graph can run to
# thousands of nodes / tens of thousands of edges — drawing all of it would hang the browser,
# so we show only the search hits + their 1-hop neighbours, capped here.
GRAPH_NODE_CAP = 140
# Companion edge cap (keeps the canvas legible even with a few highly-connected hubs).
GRAPH_EDGE_CAP = 320

# Max label / description lengths (kept compact for the canvas + the inspector).
_LABEL_MAX = 42
_DESC_MAX = 220


def _short(text: str, n: int) -> str:
    """Collapse whitespace and clip to n chars with an ellipsis (a local copy of
    main._short, so this module carries no console dependency)."""
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def entity_kind(entity_type: str) -> str:
    """Map a graph entity_type to one of the prototype's 3 node-kind families:
    'code' (file/function/module/class/...), 'work' (handoff/task/agent), else 'mem'
    (concept/decision/lesson/tool/...). Drives the node colour in the SPA palette."""
    et = (entity_type or "").lower()
    if et in ("file", "function", "module", "class", "method", "callsite", "code"):
        return "code"
    if et in ("handoff", "task", "agent", "work_product"):
        return "work"
    return "mem"


def shape_graph(search: dict[str, Any] | None) -> dict[str, Any]:
    """Shape one `/cortex-graph-search` payload into bounded `{nodes, edges, ...}`.

    `search` is the raw dual-level dict (or {} / None on a down Cortex). Returns
    `{nodes, edges, shown_nodes, shown_edges, kind_counts}` — the api layer folds
    `/graph/stats` in around this for the full `stats` block. Always returns a valid
    (possibly empty) shape; never raises on malformed input."""
    hi = search.get("high_level") if isinstance(search, dict) else None
    lo = search.get("low_level") if isinstance(search, dict) else None
    rels = search.get("relationships") if isinstance(search, dict) else None
    hi = hi if isinstance(hi, list) else []
    lo = lo if isinstance(lo, list) else []
    rels = rels if isinstance(rels, list) else []

    # ---- 1. collect candidate nodes from the direct entity hits ----------------
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
                "label": _short(name, _LABEL_MAX),
                "full": name,
                "etype": etype,
                "kind": entity_kind(etype),
                "desc": _short(e.get("description") or "", _DESC_MAX),
                "hit": 1 if hit else 0,
                "deg": 0,
            }
        else:
            if hit:
                existing["hit"] = 1
                existing["etype"] = etype
                existing["kind"] = entity_kind(etype)
            if not existing["desc"] and e.get("description"):
                existing["desc"] = _short(e.get("description") or "", _DESC_MAX)

    # low-level first (concrete file/tool/entity hits), then high-level concepts.
    for e in lo:
        _add_entity(e, hit=True)
    for e in hi:
        _add_entity(e, hit=True)

    # ---- 2. fold relationship endpoints in as NEIGHBOUR nodes ------------------
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
        if src not in nodes:
            _add_entity(
                {"name": src, "entity_type": r.get("source_type") or "entity"}, hit=False
            )
        if tgt not in nodes:
            _add_entity(
                {"name": tgt, "entity_type": r.get("target_type") or "entity"}, hit=False
            )
        key = "::".join(sorted((src, tgt)) + [str(rtype)])
        if key in seen_edge_keys:
            continue
        seen_edge_keys.add(key)
        raw_edges.append({"source": src, "target": tgt, "label": str(rtype)})
        nodes[src]["deg"] += 1
        nodes[tgt]["deg"] += 1

    # ---- 3. bound the node set: all hits + the most-connected neighbours --------
    hit_nodes = [n for n in nodes.values() if n["hit"]]
    nbr_nodes = [n for n in nodes.values() if not n["hit"]]
    nbr_nodes.sort(key=lambda n: n["deg"], reverse=True)
    budget = max(0, GRAPH_NODE_CAP - len(hit_nodes))
    kept_nodes = hit_nodes[:GRAPH_NODE_CAP] + nbr_nodes[:budget]
    kept_ids = {n["id"] for n in kept_nodes}

    # ---- 4. keep only edges whose BOTH endpoints survived the node cap ---------
    kept_edges: list[dict] = []
    for e in raw_edges:
        if e["source"] in kept_ids and e["target"] in kept_ids:
            kept_edges.append(e)
        if len(kept_edges) >= GRAPH_EDGE_CAP:
            break

    # ---- 5. emit the cytoscape-agnostic node/edge dicts + tallies --------------
    kind_counts = {"code": 0, "mem": 0, "work": 0}
    out_nodes: list[dict] = []
    for n in kept_nodes:
        kind_counts[n["kind"]] = kind_counts.get(n["kind"], 0) + 1
        out_nodes.append(
            {
                "id": n["id"],
                "label": n["label"],
                "full": n["full"],
                "kind": n["kind"],
                "etype": n["etype"],
                "desc": n["desc"],
                "hit": n["hit"],
            }
        )
    out_edges: list[dict] = []
    for i, e in enumerate(kept_edges):
        out_edges.append(
            {
                "id": f"e{i}",
                "source": e["source"],
                "target": e["target"],
                "label": e["label"],
            }
        )

    return {
        "nodes": out_nodes,
        "edges": out_edges,
        "shown_nodes": len(out_nodes),
        "shown_edges": len(out_edges),
        "kind_counts": kind_counts,
    }


def shape_memory_graph(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Shape `/cortex-graph/memory` into the same bounded graph payload.

    Unlike `shape_graph`, every returned node is a direct memory hit. We still
    cap the browser payload to the same bounds so large project memories stay
    interactive.
    """
    raw_nodes = raw.get("nodes") if isinstance(raw, dict) else None
    raw_edges = raw.get("edges") if isinstance(raw, dict) else None
    raw_sources = raw.get("sources") if isinstance(raw, dict) else None
    raw_source_edges = raw.get("source_edges") if isinstance(raw, dict) else None
    raw_nodes = raw_nodes if isinstance(raw_nodes, list) else []
    raw_edges = raw_edges if isinstance(raw_edges, list) else []
    raw_sources = raw_sources if isinstance(raw_sources, list) else []
    raw_source_edges = raw_source_edges if isinstance(raw_source_edges, list) else []

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw_nodes:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("label") or row.get("id") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        etype = str(row.get("entity_type") or row.get("type") or "entity")
        nodes.append({
            "id": name,
            "label": _short(name, _LABEL_MAX),
            "full": name,
            "kind": entity_kind(etype),
            "etype": etype,
            "desc": _short(str(row.get("description") or ""), _DESC_MAX),
            "hit": 1,
            "source_count": row.get("source_count"),
            "updated_at": row.get("updated_at"),
        })
        if len(nodes) >= GRAPH_NODE_CAP:
            break

    for row in raw_sources:
        if len(nodes) >= GRAPH_NODE_CAP:
            break
        if not isinstance(row, dict):
            continue
        node_id = str(row.get("id") or "").strip()
        label = str(row.get("label") or row.get("source_id") or node_id).strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        etype = str(row.get("source_type") or row.get("type") or "source")
        nodes.append({
            "id": node_id,
            "label": _short(label, _LABEL_MAX),
            "full": label,
            "kind": entity_kind(etype),
            "etype": etype,
            "desc": _short(str(row.get("description") or ""), _DESC_MAX),
            "hit": 1,
            "source_count": 1,
            "updated_at": row.get("updated_at"),
            "source_table": row.get("source_table"),
            "source_id": row.get("source_id"),
        })

    kept = {node["id"] for node in nodes}
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()
    for row in raw_edges:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip()
        target = str(row.get("target") or "").strip()
        label = str(row.get("relationship_type") or row.get("label") or "related")
        if not source or not target or source not in kept or target not in kept:
            continue
        key = "::".join(sorted((source, target)) + [label])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append({
            "id": str(row.get("id") or f"e{len(edges)}"),
            "source": source,
            "target": target,
            "label": label,
        })
        if len(edges) >= GRAPH_EDGE_CAP:
            break
    for row in raw_source_edges:
        if len(edges) >= GRAPH_EDGE_CAP:
            break
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip()
        target = str(row.get("target") or "").strip()
        label = str(row.get("relationship_type") or row.get("label") or "source")
        if not source or not target or source not in kept or target not in kept:
            continue
        key = "::".join(sorted((source, target)) + [label])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append({
            "id": str(row.get("id") or f"source-e{len(edges)}"),
            "source": source,
            "target": target,
            "label": label,
        })

    kind_counts = {"code": 0, "mem": 0, "work": 0}
    for node in nodes:
        kind = node["kind"]
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    return {
        "nodes": nodes,
        "edges": edges,
        "shown_nodes": len(nodes),
        "shown_edges": len(edges),
        "kind_counts": kind_counts,
    }


def repo_stats(stats: dict[str, Any] | None, project_key: str) -> dict[str, Any]:
    """Fold a `/graph/stats` payload into the headline-stats block for the project.

    Returns `{own_nodes, own_edges, total_nodes, total_edges, repo_count, repos}` where
    `own_*` are this project's own code-graph repo counts (match on name == project_key),
    `total_*` are the cross-repo totals, and `repos` is a small per-repo table (top by node
    count, own flagged). Every count is None when absent so the SPA renders '—'. Never
    raises on malformed/empty input (a down Cortex yields all-None / empty)."""
    repos = stats.get("repos") if isinstance(stats, dict) else None
    repos = repos if isinstance(repos, list) else []
    project_norm = (project_key or "").lower()

    def _is_own(row: dict[str, Any]) -> bool:
        return str(row.get("name") or "").lower() == project_norm

    own = next(
        (r for r in repos if isinstance(r, dict) and _is_own(r)), None
    )
    repo_rows = sorted(
        (
            {
                "name": r.get("name"),
                "nodes": r.get("nodes") or 0,
                "edges": r.get("edges") or 0,
                "is_own": _is_own(r),
            }
            for r in repos
            if isinstance(r, dict) and r.get("name")
        ),
        key=lambda r: r["nodes"],
        reverse=True,
    )[:8]
    return {
        "own_nodes": own.get("nodes") if own else None,
        "own_edges": own.get("edges") if own else None,
        "total_nodes": stats.get("total_nodes") if isinstance(stats, dict) else None,
        "total_edges": stats.get("total_edges") if isinstance(stats, dict) else None,
        "repo_count": len(repos),
        "repos": repo_rows,
    }


__all__ = ["shape_graph", "repo_stats", "entity_kind", "GRAPH_NODE_CAP", "GRAPH_EDGE_CAP"]
