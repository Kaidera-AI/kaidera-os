import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import pytest


WORKER_PATH = Path(__file__).resolve().parents[1] / "worker.py"


def load_worker(name: str):
    spec = importlib.util.spec_from_file_location(name, WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resolve_repo_translates_absolute_host_path_under_projects_mount(tmp_path):
    module = load_worker("graph_worker_resolve_host_path_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "Library" / "CloudStorage" / "Drive" / "Marketing"
    repo.mkdir(parents=True)

    project, path = module._resolve_repo(
        "/Users/alice/Library/CloudStorage/Drive/Marketing"
    )

    assert project == "Marketing"
    assert path == repo


def test_resolve_repo_short_circuits_before_deep_search(tmp_path):
    module = load_worker("graph_worker_resolve_short_circuit_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "Drive" / "Marketing"
    repo.mkdir(parents=True)

    def fail_deep_search(project: str):
        raise AssertionError(f"unexpected deep search for {project}")

    module._deep_project_candidates = fail_deep_search

    project, path = module._resolve_repo("/Users/alice/Drive/Marketing")

    assert project == "Marketing"
    assert path == repo


def test_resolve_repo_falls_back_to_nested_project_name_search(tmp_path):
    module = load_worker("graph_worker_resolve_nested_search_test")
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/different/root"
    repo = module.PROJECTS_DIR / "some" / "deep" / "Marketing"
    repo.mkdir(parents=True)

    project, path = module._resolve_repo("/Users/alice/Drive/Marketing")

    assert project == "Marketing"
    assert path == repo


@pytest.mark.asyncio
async def test_build_imports_existing_graph_for_non_git_workspace(tmp_path):
    module = load_worker("graph_worker_import_existing_graph_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    module.HOST_PROJECTS_ROOT = "/Users/alice"
    repo = module.PROJECTS_DIR / "Drive" / "Marketing"
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(parents=True)
    source_db = graph_dir / "graph.db"
    con = sqlite3.connect(source_db)
    con.execute("CREATE TABLE nodes (id TEXT)")
    con.execute("CREATE TABLE edges (id TEXT)")
    con.executemany("INSERT INTO nodes (id) VALUES (?)", [("a",), ("b",)])
    con.execute("INSERT INTO edges (id) VALUES ('e1')")
    con.commit()
    con.close()
    stale_dir = module.GRAPHS_DIR / "Marketing"
    stale_dir.mkdir(parents=True)
    (stale_dir / "graph.db").write_text("stale", encoding="utf-8")

    result = await module.build(
        module.BuildBody(repo="/Users/alice/Drive/Marketing", full=True, embed=False)
    )

    assert result["status"] == "imported-existing-graph"
    assert result["nodes"] == 2
    assert result["edges"] == 1
    assert (module.GRAPHS_DIR / "Marketing" / "graph.db").read_bytes() == source_db.read_bytes()


@pytest.mark.asyncio
async def test_build_explicitly_imports_existing_graph_for_git_repo(tmp_path, monkeypatch):
    module = load_worker("graph_worker_explicit_import_test")
    module.GRAPHS_DIR = tmp_path / "graphs"
    module.PROJECTS_DIR = tmp_path / "projects"
    repo = module.PROJECTS_DIR / "kaidera-os"
    (repo / ".git").mkdir(parents=True)
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir()
    source_db = graph_dir / "graph.db"
    con = sqlite3.connect(source_db)
    con.execute("CREATE TABLE nodes (id TEXT)")
    con.execute("CREATE TABLE edges (id TEXT)")
    con.execute("INSERT INTO nodes (id) VALUES ('a')")
    con.execute("INSERT INTO edges (id) VALUES ('e1')")
    con.commit()
    con.close()

    monkeypatch.setattr(
        module,
        "_run_bcrg",
        lambda *_args, **_kwargs: pytest.fail("explicit import must not rebuild"),
    )

    result = await module.build(
        module.BuildBody(repo="kaidera-os", import_existing=True, embed=False)
    )

    assert result["status"] == "imported-existing-graph"
    assert result["nodes"] == 1
    assert result["edges"] == 1


def test_run_bcrg_reports_timeout_as_actionable_gateway_timeout(monkeypatch):
    module = load_worker("graph_worker_timeout_test")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="uv", timeout=7)

    monkeypatch.setattr(module.subprocess, "run", timeout)

    with pytest.raises(module.HTTPException) as exc_info:
        module._run_bcrg("print('{}')", timeout=7)

    assert exc_info.value.status_code == 504
    assert "--local-worker" in exc_info.value.detail
    assert "--import-existing" in exc_info.value.detail


@pytest.mark.asyncio
async def test_prune_dry_run_reports_stale_graph_dirs_without_deleting(tmp_path):
    module = load_worker("graph_worker_prune_dry_run_test")
    module.GRAPHS_DIR = tmp_path
    (tmp_path / "active").mkdir()
    (tmp_path / "active" / "graph.db").write_text("active", encoding="utf-8")
    (tmp_path / "stale").mkdir()
    (tmp_path / "stale" / "graph.db").write_text("stale", encoding="utf-8")

    result = await module.prune(
        module.PruneBody(active_projects=["active"], dry_run=True)
    )

    assert result["dry_run"] is True
    assert [item["name"] for item in result["candidates"]] == ["stale"]
    assert result["pruned"] == []
    assert (tmp_path / "stale" / "graph.db").exists()


@pytest.mark.asyncio
async def test_prune_apply_deletes_only_stale_graph_dirs(tmp_path):
    module = load_worker("graph_worker_prune_apply_test")
    module.GRAPHS_DIR = tmp_path
    (tmp_path / "active").mkdir()
    (tmp_path / "active" / "graph.db").write_text("active", encoding="utf-8")
    (tmp_path / "stale").mkdir()
    (tmp_path / "stale" / "graph.db").write_text("stale", encoding="utf-8")

    result = await module.prune(
        module.PruneBody(active_projects=["active"], dry_run=False)
    )

    assert [item["name"] for item in result["pruned"]] == ["stale"]
    assert (tmp_path / "active" / "graph.db").exists()
    assert not (tmp_path / "stale").exists()
