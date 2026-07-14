"""cortex-graph-worker — L3 code graph internal API.

Internal-only, no auth (cortex-api is the trusted proxy). Exposes:
  GET  /health                      → {ok: true, graphs_dir: str, projects_seen: int}
  GET  /stats                       → cross-repo aggregate (DuckDB ATTACH over volume)
  POST /build {repo: str, full?: bool}  → build/update a repo's graph
  POST /blast {repo, files: [str]}      → impact radius
  POST /callers {repo, target}          → callers/callees/imports/etc.
  POST /impact {repo, files}            → impact analysis
  POST /large-fn {repo}                 → largest functions

This is the scaffold (Phase C.2 deliverable). The DuckDB ATTACH stats are
live and use the migrated graph.db files. /build/blast/callers/impact/large-fn
delegate to better-code-review-graph via subprocess until Phase C.2 wires
them in-process.
"""

from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import asyncio
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

GRAPHS_DIR = Path(os.environ.get("CORTEX_GRAPHS_DIR", "/var/lib/cortex/graphs"))
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/projects"))
HOST_PROJECTS_ROOT = os.path.normpath(os.environ.get("HOST_PROJECTS_ROOT", ""))

app = FastAPI(title="cortex-graph-worker", version="0.1.0")


class BuildBody(BaseModel):
    repo: str  # project name; resolved to /projects/<repo>
    full: bool = False
    embed: bool = True
    import_existing: bool = False


class PruneBody(BaseModel):
    active_projects: list[str]
    dry_run: bool = True


class BlastBody(BaseModel):
    repo: str
    files: list[str]
    depth: int = 2
    max_results: int = 100


class CallersBody(BaseModel):
    repo: str
    target: str
    pattern: str = "callers_of"
    max_results: int = 100


class ImpactBody(BaseModel):
    repo: str
    base: str = "HEAD~1"
    max_results: int = 100


class LargeFnBody(BaseModel):
    repo: str
    min_lines: int = 200
    kind: Optional[str] = None
    limit: int = 100


def _project_graph_db(project: str) -> Path:
    """Per-project graph.db path inside the cortex-graphs volume."""
    return GRAPHS_DIR / project / "graph.db"


def _host_relative_candidate(repo: str) -> Path | None:
    """Translate a host absolute path into the /projects bind mount.

    Example: HOST_PROJECTS_ROOT=/workspace/projects and repo=/workspace/projects/Drive/App
    resolves to /projects/Drive/App inside the container. This is string-based
    on purpose: the original host path does not exist inside the container, so
    Path.resolve()/relative_to() would be wrong here.
    """
    if not HOST_PROJECTS_ROOT or not os.path.isabs(repo):
        return None
    raw = os.path.normpath(repo)
    host = HOST_PROJECTS_ROOT
    if raw == host:
        return PROJECTS_DIR
    if raw.startswith(host + os.sep):
        return PROJECTS_DIR / os.path.relpath(raw, host)
    return None


def _deep_project_candidates(project: str) -> list[Path]:
    """Last-resort bounded search for nested project folders under /projects."""
    if not PROJECTS_DIR.exists():
        return []
    out: list[Path] = []
    for candidate in PROJECTS_DIR.rglob(project):
        if candidate.is_dir():
            out.append(candidate)
            if len(out) >= 8:
                break
    return sorted(out)


def _resolve_repo(repo: str) -> tuple[str, Path]:
    """Resolve a repo name/path under /projects and keep graph writes in the volume."""
    project = Path(repo).name
    checked: list[Path] = []

    direct_candidates: list[Path] = []
    host_candidate = _host_relative_candidate(repo)
    if host_candidate is not None:
        direct_candidates.append(host_candidate)
    if not os.path.isabs(repo):
        direct_candidates.append(PROJECTS_DIR / repo)
    direct_candidates.append(PROJECTS_DIR / project)

    for candidate in direct_candidates:
        checked.append(candidate)
        if candidate.is_dir():
            return project, candidate

    # Avoid recursive home-directory scans unless the exact/direct project path
    # failed. On macOS installs `/projects` can be the whole user home.
    shallow_candidates = sorted(PROJECTS_DIR.glob(f"*/{project}")) if PROJECTS_DIR.exists() else []
    for candidate in shallow_candidates:
        checked.append(candidate)
        if candidate.is_dir():
            return project, candidate

    for candidate in _deep_project_candidates(project):
        checked.append(candidate)
        if candidate.is_dir():
            return project, candidate

    checked_msg = ", ".join(str(candidate) for candidate in checked[:5])
    raise HTTPException(404, f"repo not found; checked {checked_msg}")


def _tool_preamble(project: str, repo_root: Path) -> str:
    """Patch better-code-review-graph storage away from the read-only repo mount."""
    graphs_dir = str(GRAPHS_DIR)
    project_json = json.dumps(project)
    graphs_json = json.dumps(graphs_dir)
    repo_root_json = json.dumps(str(repo_root))
    return f"""
from pathlib import Path
import better_code_review_graph.tools as tools

_CORTEX_GRAPH_PROJECT = {project_json}
_CORTEX_GRAPHS_DIR = Path({graphs_json})
_CORTEX_REPO_ROOT = Path({repo_root_json})

def _cortex_volume_db_path(_repo_root):
    graph_dir = _CORTEX_GRAPHS_DIR / _CORTEX_GRAPH_PROJECT
    graph_dir.mkdir(parents=True, exist_ok=True)
    return graph_dir / "graph.db"

def _cortex_prepare_graph_git_marker(repo_root):
    graph_dir = _CORTEX_GRAPHS_DIR / _CORTEX_GRAPH_PROJECT
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph_git = graph_dir / ".git"
    repo_git = Path(repo_root) / ".git"
    if not repo_git.exists() or graph_git.is_dir():
        return

    gitdir = repo_git
    if repo_git.is_file():
        content = repo_git.read_text(encoding="utf-8").strip()
        prefix = "gitdir:"
        if content.lower().startswith(prefix):
            gitdir = Path(content[len(prefix):].strip())
            if not gitdir.is_absolute():
                gitdir = (repo_git.parent / gitdir).resolve()

    marker = f"gitdir: {{gitdir}}\\n"
    if not graph_git.exists() or graph_git.read_text(encoding="utf-8", errors="ignore") != marker:
        graph_git.write_text(marker, encoding="utf-8")

_cortex_prepare_graph_git_marker(_CORTEX_REPO_ROOT)
tools.get_db_path = _cortex_volume_db_path
"""


def _has_git_repo(repo_path: Path) -> bool:
    return (repo_path / ".git").exists()


def _existing_graph_db(repo_path: Path) -> Path | None:
    db = repo_path / ".code-review-graph" / "graph.db"
    return db if db.exists() and db.stat().st_size > 0 else None


def _sqlite_count(db_path: Path, table: str) -> int:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            con.close()
    except Exception:
        return 0


def _import_existing_graph(project: str, repo_path: Path) -> dict:
    """Import a repo-local .code-review-graph DB into the managed graph volume.

    Some turnkey/customer project folders are workspaces rather than git repos.
    better-code-review-graph migrations need git metadata, but those workspaces
    may already carry a valid `.code-review-graph/graph.db` produced by the old
    host workflow. In that case, reuse it instead of forcing a doomed rebuild.
    """
    source = _existing_graph_db(repo_path)
    if source is None:
        raise HTTPException(400, "repo is not a git repository and no .code-review-graph/graph.db exists to import")

    graph_dir = GRAPHS_DIR / project
    graph_dir.mkdir(parents=True, exist_ok=True)
    for old in graph_dir.glob("graph.db*"):
        old.unlink()
    for src in sorted(source.parent.glob("graph.db*")):
        if src.is_file():
            shutil.copy2(src, graph_dir / src.name)

    dest = graph_dir / "graph.db"
    return {
        "status": "imported-existing-graph",
        "project": project,
        "repo": str(repo_path),
        "source": str(source),
        "graph_db": str(dest),
        "nodes": _sqlite_count(dest, "nodes"),
        "edges": _sqlite_count(dest, "edges"),
    }


def _run_bcrg(code: str, *, timeout: int = 120) -> dict:
    try:
        proc = subprocess.run(
            ["uv", "tool", "run", "--from", "better-code-review-graph", "python3", "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            504,
            f"code graph operation exceeded {timeout}s; build with --local-worker and import with --import-existing",
        ) from exc
    if proc.returncode != 0:
        raise HTTPException(500, proc.stderr[-1000:] or proc.stdout[-1000:])
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"worker returned non-JSON output: {proc.stdout[-1000:]}") from exc


def _list_graph_dbs() -> list[tuple[str, Path]]:
    """Walk the cortex-graphs volume for all populated graph.db files."""
    if not GRAPHS_DIR.exists():
        return []
    out = []
    for proj_dir in sorted(GRAPHS_DIR.iterdir()):
        if proj_dir.is_dir():
            db = proj_dir / "graph.db"
            if db.exists() and db.stat().st_size > 0:
                out.append((proj_dir.name, db))
    return out


def _safe_graph_dir(name: str) -> Path:
    """Resolve a graph project dir without allowing path traversal."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(400, f"unsafe graph project name: {name!r}")
    path = (GRAPHS_DIR / name).resolve()
    root = GRAPHS_DIR.resolve()
    if path == root or root not in path.parents:
        raise HTTPException(400, f"unsafe graph project path: {path}")
    return path


@app.get("/health")
async def health():
    return {
        "ok": True,
        "graphs_dir": str(GRAPHS_DIR),
        "projects_dir": str(PROJECTS_DIR),
        "graphs_dir_exists": GRAPHS_DIR.exists(),
        "projects_dir_exists": PROJECTS_DIR.exists(),
    }


@app.get("/stats")
async def stats():
    return await asyncio.to_thread(_collect_stats)


def _collect_stats() -> dict:
    """Cross-repo aggregate via bounded SQLite reads.

    Keep this off the asyncio event loop: graph DBs can live on mounted drives,
    and a slow filesystem must not block `/health` or other worker requests.
    """
    graphs = _list_graph_dbs()
    if not graphs:
        return {"total_nodes": 0, "total_edges": 0, "repos": []}

    out = {"repos": [], "total_nodes": 0, "total_edges": 0}
    for name, path in graphs:
        try:
            nodes = _sqlite_count(path, "nodes")
            edges = _sqlite_count(path, "edges")
            out["repos"].append({"name": name, "nodes": nodes, "edges": edges, "path": str(path)})
            out["total_nodes"] += nodes
            out["total_edges"] += edges
        except Exception as exc:
            out["repos"].append({"name": name, "error": str(exc)})
    return out


@app.post("/prune")
async def prune(body: PruneBody):
    """Dry-run/apply deletion of graph.db dirs not present in active_projects."""
    active = {Path(project).name for project in body.active_projects if project}
    candidates = []
    pruned = []

    for name, db_path in _list_graph_dbs():
        if name in active:
            continue
        graph_dir = _safe_graph_dir(name)
        size_bytes = db_path.stat().st_size if db_path.exists() else 0
        item = {
            "name": name,
            "path": str(graph_dir),
            "graph_db": str(db_path),
            "size_bytes": size_bytes,
        }
        candidates.append(item)
        if not body.dry_run:
            shutil.rmtree(graph_dir)
            pruned.append(item)

    return {
        "dry_run": body.dry_run,
        "active_projects": sorted(active),
        "candidates": candidates,
        "pruned": pruned,
    }


@app.post("/build")
async def build(body: BuildBody):
    """Build/update a repo's graph using better-code-review-graph (host-bind)."""
    project, repo_path = _resolve_repo(body.repo)
    if body.import_existing:
        return await asyncio.to_thread(_import_existing_graph, project, repo_path)
    if not _has_git_repo(repo_path):
        return await asyncio.to_thread(_import_existing_graph, project, repo_path)
    full_flag = "True" if body.full else "False"
    embed_flag = "True" if body.embed else "False"
    code = _tool_preamble(project, repo_path) + f"""
import json
from better_code_review_graph.tools import build_or_update_graph, embed_graph

repo_root = {json.dumps(str(repo_path))}
r = build_or_update_graph(full_rebuild={full_flag}, repo_root=repo_root)
if isinstance(r, str):
    r = json.loads(r)
if {embed_flag}:
    e = embed_graph(repo_root=repo_root)
    if isinstance(e, str):
        e = json.loads(e)
    r["embeddings"] = e
print(json.dumps(r))
"""
    return await asyncio.to_thread(_run_bcrg, code, timeout=600)


@app.post("/blast")
async def blast(body: BlastBody):
    """Blast radius via better-code-review-graph get_impact_radius."""
    project, repo_path = _resolve_repo(body.repo)
    code = _tool_preamble(project, repo_path) + f"""
import json
from better_code_review_graph.tools import get_impact_radius

r = get_impact_radius(
    changed_files={json.dumps(body.files)},
    max_depth={body.depth},
    max_results={body.max_results},
    repo_root={json.dumps(str(repo_path))},
)
if isinstance(r, str):
    r = json.loads(r)
print(json.dumps(r))
"""
    return await asyncio.to_thread(_run_bcrg, code, timeout=120)


@app.post("/callers")
async def callers(body: CallersBody):
    project, repo_path = _resolve_repo(body.repo)
    code = _tool_preamble(project, repo_path) + f"""
import json
from better_code_review_graph.tools import query_graph

r = query_graph(
    pattern={json.dumps(body.pattern)},
    target={json.dumps(body.target)},
    repo_root={json.dumps(str(repo_path))},
)
if isinstance(r, str):
    r = json.loads(r)
results = r.get("results", [])
r["results"] = results[:{body.max_results}]
r["total_results"] = len(results)
print(json.dumps(r))
"""
    return await asyncio.to_thread(_run_bcrg, code, timeout=60)


@app.post("/impact")
async def impact(body: ImpactBody):
    project, repo_path = _resolve_repo(body.repo)
    code = _tool_preamble(project, repo_path) + f"""
import json
from better_code_review_graph.tools import get_review_context

r = get_review_context(
    max_depth=2,
    include_source=False,
    repo_root={json.dumps(str(repo_path))},
    base={json.dumps(body.base)},
)
if isinstance(r, str):
    r = json.loads(r)
guidance = r.get("review_guidance") or {{}}
if "warnings" in guidance:
    guidance["warnings"] = guidance["warnings"][:{body.max_results}]
print(json.dumps(r))
"""
    return await asyncio.to_thread(_run_bcrg, code, timeout=120)


@app.post("/large-fn")
async def large_fn(body: LargeFnBody):
    project, repo_path = _resolve_repo(body.repo)
    code = _tool_preamble(project, repo_path) + f"""
import json
from better_code_review_graph.tools import find_large_functions

r = find_large_functions(
    min_lines={body.min_lines},
    kind={body.kind!r},
    limit={body.limit},
    repo_root={json.dumps(str(repo_path))},
)
if isinstance(r, str):
    r = json.loads(r)
print(json.dumps(r))
"""
    return await asyncio.to_thread(_run_bcrg, code, timeout=60)
