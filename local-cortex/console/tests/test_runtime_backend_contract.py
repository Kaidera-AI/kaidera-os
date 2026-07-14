"""E008 RuntimeBackend contract tests.

The runtime seam is separate from HarnessPort: HarnessPort launches host workers;
RuntimeBackend owns lifecycle operations such as stream/send/status/stop/reattach.
This file guards the pure domain contract before any Herdr adapter is wired.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

DOMAIN_RUNTIME = Path(__file__).resolve().parents[1] / "app" / "domain" / "runtime.py"
FORBIDDEN_IMPORTS = {"httpx", "fastapi", "subprocess", "psycopg2", "asyncpg"}
EXPECTED_METHODS = {"start_run", "stream", "send", "status", "stop", "reattach"}


def test_runtime_module_file_exists():
    assert DOMAIN_RUNTIME.is_file()


def test_runtime_port_and_dtos_importable():
    from app.domain.runtime import (  # noqa: F401
        RuntimeBackend,
        RuntimeEvent,
        RuntimeRef,
        RuntimeRun,
        RuntimeStartRequest,
        RuntimeStatus,
    )


def test_runtime_backend_protocol_shape():
    from app.domain.runtime import RuntimeBackend

    assert getattr(RuntimeBackend, "_is_protocol", False)
    assert getattr(RuntimeBackend, "_is_runtime_protocol", False)
    missing = EXPECTED_METHODS - set(dir(RuntimeBackend))
    assert not missing
    for name in EXPECTED_METHODS - {"stream"}:
        assert inspect.iscoroutinefunction(getattr(RuntimeBackend, name))
    assert inspect.isasyncgenfunction(RuntimeBackend.stream)


def test_runtime_dtos_roundtrip_and_re_resolvable_ref():
    from app.domain.runtime import (
        RUNTIME_BACKEND_HERDR_VISIBLE,
        RuntimeEvent,
        RuntimeRef,
        RuntimeRun,
        RuntimeStartRequest,
        RuntimeStatus,
    )

    ref = RuntimeRef(
        backend=RUNTIME_BACKEND_HERDR_VISIBLE,
        session_name="e008-demo",
        workspace_id="w123",
        workspace_label="kaidera-os",
        tab_id="w123:1",
        tab_label="kai",
        pane_id="w123-1",
        pane_label="kai-run",
        protocol=12,
        version="0.6.8",
        metadata={"resolver": "label-first"},
    )
    assert dataclasses.asdict(ref)["pane_id"] == "w123-1"
    assert dataclasses.asdict(ref)["metadata"]["resolver"] == "label-first"

    req = RuntimeStartRequest(
        run_id="run-1",
        project="kaidera-os",
        agent="kai",
        cwd="/repo",
        argv=["echo", "ok"],
        env={"CORTEX_PROJECT": "kaidera-os"},
        handoff_id="h-1",
        harness="pi",
        model="gpt-5.5",
        visible=True,
    )
    assert dataclasses.asdict(req)["argv"] == ["echo", "ok"]
    assert dataclasses.asdict(req)["visible"] is True

    run = RuntimeRun(run_id="run-1", backend=RUNTIME_BACKEND_HERDR_VISIBLE, status="running", ref=ref)
    evt = RuntimeEvent(run_id="run-1", seq=1, kind="output", text="bounded output")
    status = RuntimeStatus(
        run_id="run-1",
        backend=RUNTIME_BACKEND_HERDR_VISIBLE,
        status="running",
        ref=ref,
        agent_status="working",
    )
    assert dataclasses.asdict(run)["accepted"] is True
    assert dataclasses.asdict(evt)["seq"] == 1
    assert dataclasses.asdict(status)["agent_status"] == "working"


def test_runtime_backend_selection_preserves_direct_default_and_rollback():
    from app.domain.runtime import (
        RUNTIME_BACKEND_DIRECT,
        RUNTIME_BACKEND_HERDR_VISIBLE,
        select_runtime_backend,
    )

    defaulted = select_runtime_backend()
    assert defaulted.backend == RUNTIME_BACKEND_DIRECT
    assert defaulted.reason == "direct-default"

    disabled = select_runtime_backend(RUNTIME_BACKEND_HERDR_VISIBLE, herdr_visible_enabled=False)
    assert disabled.backend == RUNTIME_BACKEND_DIRECT
    assert disabled.requested == RUNTIME_BACKEND_HERDR_VISIBLE
    assert disabled.reason == "herdr-visible-disabled"

    enabled = select_runtime_backend(RUNTIME_BACKEND_HERDR_VISIBLE, herdr_visible_enabled=True)
    assert enabled.backend == RUNTIME_BACKEND_HERDR_VISIBLE
    assert enabled.reason == "herdr-visible-dev-gate"

    unknown = select_runtime_backend("not-a-runtime", herdr_visible_enabled=True)
    assert unknown.backend == RUNTIME_BACKEND_DIRECT
    assert unknown.reason == "unknown-requested-backend"


def _imported_top_level_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def test_runtime_module_imports_nothing_outward():
    imported = _imported_top_level_modules(DOMAIN_RUNTIME.read_text())
    leaked = imported & FORBIDDEN_IMPORTS
    assert not leaked, f"app/domain/runtime.py imports outward libraries: {sorted(leaked)}"
