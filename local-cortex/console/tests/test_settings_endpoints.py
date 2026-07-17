"""Community settings API tests.

The public edition exposes typed system settings and Cortex workspace mapping. It
has no provider, credential-probe, or licensing routes.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


class FakeRepoRootClient:
    def __init__(self, *, result=None, error=None):
        self._result = result or {}
        self._error = error
        self.calls: list[tuple[str, str]] = []

    async def set_project_repo_root(self, project_key, repo_root):
        self.calls.append((project_key, repo_root))
        if self._error is not None:
            raise self._error
        return dict(self._result)


SAMPLE_SCHEMA = [
    {
        "id": "cortex",
        "title": "Cortex connection",
        "fields": [
            {
                "key": "cortex_base_url",
                "label": "Base URL",
                "type": "text",
                "default": "http://localhost:8501",
                "hint": "Cortex API URL",
            },
            {
                "key": "cortex_admin_token",
                "label": "Admin token",
                "type": "secret",
                "default": "",
                "hint": "Optional administrative token",
            },
        ],
    },
    {
        "id": "app",
        "title": "App preferences",
        "fields": [
            {
                "key": "poll_interval_secs",
                "label": "Poll interval",
                "type": "number",
                "default": 10,
                "hint": "Seconds",
            },
            {
                "key": "harness_autostart",
                "label": "Auto-start",
                "type": "bool",
                "default": False,
                "hint": "Start enabled harnesses",
            },
        ],
    },
]
SECRET_VALUE = "community-secret-must-not-leak"


def _fields(payload):
    return {field["key"]: field for group in payload["groups"] for field in group["fields"]}


def test_system_schema_uses_visible_external_harnesses(monkeypatch):
    from app import harness
    from app.settings_module import api as settings_api

    monkeypatch.setattr(
        harness,
        "harness_options",
        lambda: [
            {"value": "claude-code", "label": "Claude Code"},
            {"value": "codex", "label": "Codex"},
            {"value": "pi", "label": "PI"},
        ],
    )
    schema = settings_api.get_system_schema()
    field = next(
        field
        for group in schema
        for field in group["fields"]
        if field["key"] == "harness_default"
    )
    assert field["options"] == ["claude-code", "codex", "pi"]


def test_build_system_schema_types_and_secret_masking():
    from app.settings_module import service as svc

    payload = svc.build_system_schema(
        SAMPLE_SCHEMA,
        {
            "cortex_base_url": "http://localhost:8501",
            "cortex_admin_token": SECRET_VALUE,
            "poll_interval_secs": 30,
            "harness_autostart": True,
        },
    )
    fields = _fields(payload)
    assert fields["cortex_base_url"]["value"] == "http://localhost:8501"
    assert fields["poll_interval_secs"]["value"] == 30
    assert fields["harness_autostart"]["value"] is True
    assert fields["cortex_admin_token"]["is_set"] is True
    assert fields["cortex_admin_token"].get("value", "") == ""
    assert SECRET_VALUE not in json.dumps(payload)


@pytest.mark.asyncio
async def test_system_schema_endpoint_masks_secret():
    from tests.test_settings_module import FakeOpStore
    from app.settings_module import api as settings_api

    result = await settings_api.system_schema_endpoint(
        "kaidera-os",
        store=FakeOpStore(
            app_settings={
                "cortex_admin_token": SECRET_VALUE,
                "cortex_base_url": "http://localhost:8501",
            }
        ),
        schema=SAMPLE_SCHEMA,
    )
    assert result["project"] == "kaidera-os"
    assert _fields(result)["cortex_admin_token"]["is_set"] is True
    assert SECRET_VALUE not in json.dumps(result)


@pytest.mark.asyncio
async def test_system_schema_endpoint_degrades_to_defaults():
    from tests.test_settings_module import FakeOpStore
    from app.settings_module import api as settings_api

    result = await settings_api.system_schema_endpoint(
        "kaidera-os", store=FakeOpStore(down=True), schema=SAMPLE_SCHEMA
    )
    fields = _fields(result)
    assert fields["cortex_base_url"]["value"] == "http://localhost:8501"
    assert fields["cortex_admin_token"]["is_set"] is False
    assert result["store_connected"] is False


@pytest.mark.asyncio
async def test_workspace_set_repo_root():
    from app.settings_module import api as settings_api

    client = FakeRepoRootClient(
        result={
            "project_key": "kaidera-os",
            "repo_root": "/abs/new",
            "previous_repo_root": "/abs/old",
        }
    )
    result = await settings_api.workspace_endpoint(
        "kaidera-os", {"repo_root": "/abs/new"}, repo_client=client
    )
    assert result["ok"] is True
    assert result["repo_root"] == "/abs/new"
    assert client.calls == [("kaidera-os", "/abs/new")]
    assert "token" not in json.dumps(result).lower()


@pytest.mark.asyncio
async def test_workspace_errors_are_graceful():
    from app.settings_module import api as settings_api

    client = FakeRepoRootClient(error=ValueError("repo_root must be an absolute path"))
    result = await settings_api.workspace_endpoint(
        "kaidera-os", {"repo_root": "relative/path"}, repo_client=client
    )
    assert result["ok"] is False
    assert "absolute" in result["error"]


@pytest.mark.asyncio
async def test_workspace_missing_admin_token_is_graceful():
    from app.cortex_client import AdminTokenMissing
    from app.settings_module import api as settings_api

    client = FakeRepoRootClient(
        error=AdminTokenMissing("CORTEX_ADMIN_TOKEN is not configured")
    )
    result = await settings_api.workspace_endpoint(
        "kaidera-os", {"repo_root": "/abs/new"}, repo_client=client
    )
    assert result["ok"] is False
    assert "token" in result["error"].lower()


def test_community_settings_routes_only():
    from app.settings_module.api import router

    paths = {route.path for route in router.routes}
    assert "/settings/{project}/system-schema" in paths
    assert "/settings/{project}/workspace" in paths
    assert not any("provider" in path or "license" in path for path in paths)


def test_settings_service_imports_nothing_outward():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "settings_module"
        / "service.py"
    ).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"fastapi", "starlette", "httpx", "subprocess", "psycopg2", "asyncpg"}
    assert not imported & forbidden
