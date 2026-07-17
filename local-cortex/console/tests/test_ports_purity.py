"""Tests for the pure community SDK ports."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

DOMAIN_PORTS = Path(__file__).resolve().parents[1] / "app" / "domain" / "ports.py"
FORBIDDEN_IMPORTS = {"httpx", "fastapi", "subprocess", "psycopg2", "asyncpg"}
EXPECTED_METHODS: dict[str, set[str]] = {
    "LLMPort": {"stream"},
    "CortexMemoryPort": {
        "boot",
        "search",
        "get_handoffs",
        "claim_handoff",
        "complete_handoff",
        "get_history",
        "log",
    },
    "OperationalStorePort": {
        "available",
        "record_usage",
        "usage_by_model",
        "usage_by_model_provider",
        "usage_by_agent",
        "usage_by_project",
        "load_app_settings",
        "upsert_app_settings",
        "load_agent_overrides",
        "get_agent_override",
        "save_agent_override",
        "is_project_autonomous",
        "set_project_autonomy",
        "list_autonomous_projects",
        "is_propose_mode",
        "set_propose_mode",
    },
}


def test_all_ports_importable():
    from app.domain.ports import (  # noqa: F401
        CortexMemoryPort,
        LLMPort,
        OperationalStorePort,
    )


def test_ports_are_runtime_checkable_protocols():
    import app.domain.ports as ports

    for name, methods in EXPECTED_METHODS.items():
        port = getattr(ports, name)
        assert getattr(port, "_is_protocol", False)
        assert getattr(port, "_is_runtime_protocol", False)
        assert not methods - set(dir(port))


def test_llm_stream_is_async_generator_shape():
    from app.domain.ports import LLMPort

    assert inspect.isasyncgenfunction(LLMPort.stream)


def _imported_top_level_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_ports_module_imports_nothing_outward():
    leaked = _imported_top_level_modules(DOMAIN_PORTS.read_text()) & FORBIDDEN_IMPORTS
    assert not leaked
