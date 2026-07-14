import importlib.util
import json
from pathlib import Path

import pytest
from starlette.requests import Request


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


def admin_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/graph/prune",
            "headers": [(b"x-cortex-admin-token", b"cortex-local-admin")],
            "query_string": b"",
        }
    )


def allow_registered_project(module, monkeypatch, *, repo_root="/projects/kaidera-os"):
    async def fake_registered(project):
        return {"project_key": project, "repo_root": repo_root}

    monkeypatch.setattr(module, "require_registered_project", fake_registered)


@pytest.mark.asyncio
async def test_cortex_graph_project_stats_is_project_scoped(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_l4_stats_test")
    calls = []

    class Conn:
        pass

    async def fake_ensure_graph_schema(conn):
        calls.append(("ensure_graph_schema", conn))

    async def fake_ensure_work_products_schema(conn):
        calls.append(("ensure_work_products_schema", conn))

    async def fake_graph_stats(conn, project):
        calls.append(("graph_stats", conn, project))
        return {
            "entity_count": 7,
            "relationship_count": 11,
            "source_counts": {"decisions": 3, "lessons": 1, "knowledge": 2, "work_products": 1},
            "backlog": {"decisions": 2, "lessons": 0, "knowledge": 0, "work_products": 0},
        }

    conn = Conn()
    monkeypatch.setattr(module, "acquire_scoped", lambda project: FakeAcquire(conn))
    monkeypatch.setattr(module, "ensure_graph_schema", fake_ensure_graph_schema)
    monkeypatch.setattr(module, "ensure_work_products_schema", fake_ensure_work_products_schema)
    monkeypatch.setattr(module, "graph_stats", fake_graph_stats)

    result = await module.cortex_graph_project_stats(x_project="marketing")

    assert result["entity_count"] == 7
    assert result["relationship_count"] == 11
    assert result["source_counts"]["work_products"] == 1
    assert calls == [
        ("ensure_graph_schema", conn),
        ("ensure_work_products_schema", conn),
        ("graph_stats", conn, "marketing"),
    ]


@pytest.mark.asyncio
async def test_cortex_memory_graph_uses_existing_relationship_schema(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_memory_graph_test")
    calls = []

    class Conn:
        async def fetchval(self, sql, *args):
            if "to_regclass('public.work_products')" in sql:
                return False
            raise AssertionError(f"Unexpected fetchval SQL: {sql}")

        async def fetch(self, sql, *args):
            calls.append((sql, args))
            if "FROM cortex_entities" in sql:
                assert "jsonb_typeof(properties->'source_refs') = 'array'" in sql
                return [
                    {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "name": "Marlow",
                        "entity_type": "agent",
                        "description": "lead agent",
                        "source_refs": [],
                        "source_count": 3,
                        "updated_at": "2026-06-27T00:00:00+00:00",
                    },
                    {
                        "id": "22222222-2222-4222-8222-222222222222",
                        "name": "Publishing cadence",
                        "entity_type": "concept",
                        "description": "scheduled work",
                        "source_refs": [],
                        "source_count": 1,
                        "updated_at": "2026-06-27T00:00:00+00:00",
                    },
                ]
            if "FROM cortex_relationships r" in sql:
                assert "r.updated_at" not in sql
                assert "ORDER BY r.created_at DESC" in sql
                return [
                    {
                        "id": "33333333-3333-4333-8333-333333333333",
                        "relationship_type": "owns",
                        "description": "",
                        "source_id": "11111111-1111-4111-8111-111111111111",
                        "source": "Marlow",
                        "source_type": "agent",
                        "target_id": "22222222-2222-4222-8222-222222222222",
                        "target": "Publishing cadence",
                        "target_type": "concept",
                    }
                ]
            raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def fake_ensure_graph_schema(_conn):
        return None

    async def fake_ensure_work_products_schema(_conn):
        return None

    conn = Conn()
    monkeypatch.setattr(module, "acquire_scoped", lambda project: FakeAcquire(conn))
    monkeypatch.setattr(module, "ensure_graph_schema", fake_ensure_graph_schema)
    monkeypatch.setattr(module, "ensure_work_products_schema", fake_ensure_work_products_schema)

    result = await module.cortex_memory_graph(x_project="marketing", limit=50)

    assert result["project"] == "marketing"
    assert [node["name"] for node in result["nodes"]] == ["Marlow", "Publishing cadence"]
    assert result["edges"][0]["relationship_type"] == "owns"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_graph_build_full_request_returns_pollable_job(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_job_test")
    allow_registered_project(module, monkeypatch)

    async def fake_create_job(project, body):
        assert project == "kaidera-os"
        assert body.repo == "kaidera-os"
        assert body.full is True
        return "44444444-4444-4444-4444-444444444444"

    async def fake_run_job(_project, _job_id, _body):
        return None

    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return object()

    monkeypatch.setattr(module, "create_graph_build_job", fake_create_job)
    monkeypatch.setattr(module, "run_graph_build_job", fake_run_job)
    monkeypatch.setattr(module.asyncio, "create_task", fake_create_task)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(repo="kaidera-os", full=True),
        x_project="kaidera-os",
    )

    assert result.status_code == 202
    payload = json.loads(result.body)
    assert payload["job_id"] == "44444444-4444-4444-4444-444444444444"
    assert payload["status_url"] == "/graph/build/jobs/44444444-4444-4444-4444-444444444444"
    assert payload["status"] == "queued"
    assert scheduled


@pytest.mark.asyncio
async def test_graph_build_sync_request_still_proxies_immediately(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_sync_test")
    allow_registered_project(module, monkeypatch)

    async def fake_execute(body):
        assert body.repo == "kaidera-os"
        assert body.full is False
        assert body.async_job is False
        return {"ok": True, "repo": body.repo}

    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(repo="kaidera-os"),
        x_project="kaidera-os",
    )

    assert result == {"ok": True, "repo": "kaidera-os"}


@pytest.mark.asyncio
async def test_graph_build_rejects_cross_project_repo(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_scope_test")
    allow_registered_project(module, monkeypatch)

    async def fail_execute(_body):
        raise AssertionError("cross-project graph request reached the worker")

    monkeypatch.setattr(module, "execute_graph_build_request", fail_execute)

    with pytest.raises(module.HTTPException) as exc_info:
        await module.graph_build_proxy(
            module.GraphBuildRequest(repo="/projects/marketing"),
            x_project="kaidera-os",
        )

    assert exc_info.value.status_code == 403
    assert "scoped project" in exc_info.value.detail


@pytest.mark.asyncio
async def test_graph_build_accepts_registered_repo_root_with_different_basename(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_repo_root_test")
    repo_root = "/projects/marketing-workspace"
    allow_registered_project(module, monkeypatch, repo_root=repo_root)

    async def fake_execute(body):
        return {"repo": body.repo}

    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(repo=repo_root),
        x_project="marketing",
    )

    assert result == {"repo": repo_root}


@pytest.mark.asyncio
async def test_graph_build_import_existing_flag_reaches_worker(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_import_test")

    async def fake_proxy(worker_url, path, *, method="GET", payload=None, timeout=120.0):
        assert worker_url == module.GRAPH_WORKER_URL
        assert path == "/build"
        assert method == "POST"
        assert timeout == 650.0
        assert payload == {
            "repo": "kaidera-os",
            "full": False,
            "embed": False,
            "import_existing": True,
        }
        return {"status": "imported-existing-graph"}

    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)

    result = await module.execute_graph_build_request(
        module.GraphBuildRequest(
            repo="kaidera-os",
            embed=False,
            import_existing=True,
        )
    )

    assert result == {"status": "imported-existing-graph"}


@pytest.mark.asyncio
async def test_graph_build_full_sync_override_proxies_immediately(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_full_sync_test")
    allow_registered_project(module, monkeypatch)

    async def fake_execute(body):
        assert body.repo == "kaidera-os"
        assert body.full is True
        assert body.sync is True
        return {"ok": True, "full": body.full}

    async def fail_create_job(_project, _body):
        raise AssertionError("sync override should not create a graph build job")

    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)
    monkeypatch.setattr(module, "create_graph_build_job", fail_create_job)

    result = await module.graph_build_proxy(
        module.GraphBuildRequest(repo="kaidera-os", full=True, sync=True),
        x_project="kaidera-os",
    )

    assert result == {"ok": True, "full": True}


@pytest.mark.asyncio
async def test_run_graph_build_job_records_completed_result(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_runner_test")
    updates = []

    async def fake_update(project, job_id, **kwargs):
        updates.append((project, job_id, kwargs))

    async def fake_execute(body):
        assert body.repo == "kaidera-os"
        return {"nodes": 10, "edges": 2}

    monkeypatch.setattr(module, "update_graph_build_job", fake_update)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    await module.run_graph_build_job(
        "kaidera-os",
        "55555555-5555-5555-5555-555555555555",
        module.GraphBuildRequest(repo="kaidera-os"),
    )

    assert updates[0][2] == {"status": "running"}
    assert updates[1][2] == {"status": "completed", "result": {"nodes": 10, "edges": 2}}


@pytest.mark.asyncio
async def test_run_graph_build_job_records_failure(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_build_failure_test")
    updates = []

    async def fake_update(project, job_id, **kwargs):
        updates.append((project, job_id, kwargs))

    async def fake_execute(_body):
        raise RuntimeError("worker timed out")

    monkeypatch.setattr(module, "update_graph_build_job", fake_update)
    monkeypatch.setattr(module, "execute_graph_build_request", fake_execute)

    await module.run_graph_build_job(
        "kaidera-os",
        "66666666-6666-6666-6666-666666666666",
        module.GraphBuildRequest(repo="kaidera-os"),
    )

    assert updates[0][2] == {"status": "running"}
    assert updates[1][2]["status"] == "failed"
    assert "RuntimeError: worker timed out" in updates[1][2]["error"]


@pytest.mark.asyncio
async def test_graph_prune_uses_active_projects_and_keep_overrides(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_graph_prune_test")
    module.ADMIN_TOKEN = "cortex-local-admin"

    class Conn:
        async def fetch(self, sql, *args):
            assert "FROM cortex_projects" in sql
            assert "status = 'active'" in sql
            return [{"project_key": "kaidera-os"}, {"project_key": "dxb"}]

    async def fake_proxy(worker_url, path, *, method="GET", payload=None, timeout=120.0):
        assert worker_url == module.GRAPH_WORKER_URL
        assert path == "/prune"
        assert method == "POST"
        assert timeout == 30.0
        assert payload == {
            "active_projects": ["dxb", "kaidera-os", "manual-keep"],
            "dry_run": True,
        }
        return {"dry_run": True, "candidates": []}

    monkeypatch.setattr(module, "pool_admin", FakePool(Conn()))
    monkeypatch.setattr(module, "proxy_worker_json", fake_proxy)

    result = await module.graph_prune_proxy(
        module.GraphPruneRequest(dry_run=True, keep_projects=["manual-keep"]),
        admin_request(),
    )

    assert result == {"dry_run": True, "candidates": []}
