import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = ROOT / "local-cortex" / "containers" / "graph-worker" / "worker.py"


def _load_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("CORTEX_GRAPHS_DIR", str(tmp_path / "graphs"))
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    spec = importlib.util.spec_from_file_location("cortex_graph_worker_under_test", WORKER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _install_fake_bcrg(monkeypatch):
    package = types.ModuleType("better_code_review_graph")
    package.__path__ = []
    tools = types.ModuleType("better_code_review_graph.tools")
    monkeypatch.setitem(sys.modules, "better_code_review_graph", package)
    monkeypatch.setitem(sys.modules, "better_code_review_graph.tools", tools)
    return tools


def test_graph_worker_preamble_points_volume_git_marker_at_repo_git_dir(monkeypatch, tmp_path):
    worker = _load_worker(monkeypatch, tmp_path)
    tools = _install_fake_bcrg(monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    exec(worker._tool_preamble("kaidera-os", repo), {})

    graph_git = tmp_path / "graphs" / "kaidera-os" / ".git"
    assert graph_git.read_text(encoding="utf-8") == f"gitdir: {repo / '.git'}\n"
    assert tools.get_db_path(repo) == tmp_path / "graphs" / "kaidera-os" / "graph.db"


def test_graph_worker_preamble_resolves_repo_gitfile_targets(monkeypatch, tmp_path):
    worker = _load_worker(monkeypatch, tmp_path)
    _install_fake_bcrg(monkeypatch)
    repo = tmp_path / "worktree" / "repo"
    repo.mkdir(parents=True)
    gitdir = tmp_path / "worktree" / "actual.git"
    gitdir.mkdir()
    (repo / ".git").write_text("gitdir: ../actual.git\n", encoding="utf-8")

    exec(worker._tool_preamble("kaidera-os", repo), {})

    graph_git = tmp_path / "graphs" / "kaidera-os" / ".git"
    assert graph_git.read_text(encoding="utf-8") == f"gitdir: {gitdir.resolve()}\n"


def test_graph_worker_routes_offload_blocking_bcrg_subprocesses():
    text = WORKER_PATH.read_text(encoding="utf-8")

    assert "import asyncio" in text
    assert "await asyncio.to_thread(_run_bcrg, code, timeout=600)" in text
    assert text.count("await asyncio.to_thread(_run_bcrg, code") >= 5
    assert "return _run_bcrg(code" not in text
