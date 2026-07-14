"""Contract tests for the Cortex MCP server (B.1.2 — full 24-tool surface).

Run:  pytest .agents/api/tests/test_mcp.py -v

Live HTTP round-trip tests live in test_mcp_live.py and skip when
cortex-api is unreachable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── Expected tool surface (sync with .agents/api/MCP_SERVER_DESIGN.md §5) ──
EXPECTED_TOOLS: set[str] = {
    # Identity + boot
    "cortex_bootstrap",
    "cortex_boot",
    "cortex_persona",
    # Handoffs
    "cortex_handoff_list",
    "cortex_handoff_get",
    "cortex_handoff_create",
    "cortex_handoff_claim",
    "cortex_handoff_complete",
    # Memory writes
    "cortex_log_decision",
    "cortex_log_lesson",
    "cortex_log_event",
    "cortex_diary_write",
    "cortex_beat_heartbeat",
    "cortex_beat_claim_done",
    # Search + retrieval
    "cortex_search",
    "cortex_graph_search",
    "cortex_entities_search",
    "cortex_history",
    # Code graph
    "cortex_graph_blast",
    "cortex_graph_callers",
    "cortex_graph_impact",
    "cortex_graph_stats",
    # Diagnostic
    "cortex_doctor",
    "cortex_verify_decision",
    "cortex_state",
    "cortex_roster",
}


@pytest.fixture
def mcp_module():
    """Import .agents/api/mcp_server.py without polluting sys.modules."""
    here = Path(__file__).resolve().parent
    src = here.parent / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("cortex_mcp_under_test", src)
    assert spec and spec.loader, f"could not load spec for {src}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registered_tools(mcp) -> set[str]:
    """FastMCP exposes registered tools across SDK versions via different paths."""
    if hasattr(mcp, "_tool_manager"):
        return {t.name for t in mcp._tool_manager.list_tools()}
    if hasattr(mcp, "_tools"):
        return set(mcp._tools.keys())
    pytest.fail("FastMCP instance has no recognised tool registry attribute")


def test_imports_cleanly(mcp_module):
    """Module loads without ImportError and exposes the FastMCP instance."""
    assert hasattr(mcp_module, "mcp")


@pytest.mark.asyncio
async def test_lifespan_requires_explicit_project(mcp_module, monkeypatch):
    monkeypatch.setattr(mcp_module, "CORTEX_PROJECT", "")

    with pytest.raises(RuntimeError, match="CORTEX_PROJECT is required"):
        async with mcp_module.lifespan(None):
            pass


def test_server_metadata(mcp_module):
    """Server name + version are set per design doc."""
    assert mcp_module.SERVER_NAME == "cortex"
    assert mcp_module.SERVER_VERSION == "0.1.0"


def test_full_tool_surface_registered(mcp_module):
    """B.1.2 ships 24 tools per design §5."""
    registered = _registered_tools(mcp_module.mcp)
    missing = EXPECTED_TOOLS - registered
    extra = registered - EXPECTED_TOOLS
    assert not missing, f"missing tools: {sorted(missing)}"
    # Extras are warnings, not failures — extending the surface is fine.
    if extra:
        print(f"\nINFO: extra tools registered (not in EXPECTED_TOOLS): {sorted(extra)}")
    assert len(registered) >= len(EXPECTED_TOOLS), (
        f"registered {len(registered)} tools, expected >= {len(EXPECTED_TOOLS)}"
    )


def test_main_entrypoint_exists(mcp_module):
    """main() exists and is callable (does not invoke; would block on stdio)."""
    assert callable(getattr(mcp_module, "main", None))


def test_stdin_watchdog_exists(mcp_module):
    """The mandatory stdin-EOF watchdog is present (per design §6)."""
    assert callable(getattr(mcp_module, "_stdin_watchdog", None))
    assert callable(getattr(mcp_module, "_setup_pgroup", None))


def test_helpers_exist(mcp_module):
    """Helper functions used by tools are defined."""
    for name in ("_safe_call", "_post_with_agent", "_put_with_agent", "_http"):
        assert callable(getattr(mcp_module, name, None)), f"missing helper: {name}"


def test_bearer_token_scaffold(mcp_module):
    """Bearer-token scaffold (kept as defense-in-depth after B.5 promotion)."""
    assert callable(getattr(mcp_module, "_check_bearer", None))
    assert hasattr(mcp_module, "_TRANSPORT")
    assert hasattr(mcp_module, "_BEARER_TOKEN")
    assert mcp_module._TRANSPORT in ("stdio", "streamable-http")


def test_bearer_auth_middleware_class(mcp_module):
    """B.5 proper integration: BearerAuthMiddleware ASGI class present."""
    cls = getattr(mcp_module, "BearerAuthMiddleware", None)
    assert cls is not None, "BearerAuthMiddleware not defined"
    # Must accept an ASGI app in __init__ and be callable as ASGI
    assert callable(cls)
    # Quick instantiation smoke (uses dummy app)
    async def dummy_app(scope, receive, send):
        return
    middleware = cls(dummy_app)
    assert middleware.app is dummy_app
    assert callable(middleware._reject)


def test_tool_groups_present(mcp_module):
    """Sanity check — at least 3 tools in each design-doc group are registered."""
    registered = _registered_tools(mcp_module.mcp)
    groups = {
        "identity_boot": {"cortex_bootstrap", "cortex_boot", "cortex_persona"},
        "handoffs": {
            "cortex_handoff_list", "cortex_handoff_get", "cortex_handoff_create",
            "cortex_handoff_claim", "cortex_handoff_complete",
        },
        "memory_writes": {
            "cortex_log_decision", "cortex_log_lesson",
            "cortex_log_event", "cortex_diary_write",
        },
        "search": {
            "cortex_search", "cortex_graph_search",
            "cortex_entities_search", "cortex_history",
        },
        "code_graph": {
            "cortex_graph_blast", "cortex_graph_callers",
            "cortex_graph_impact", "cortex_graph_stats",
        },
        "diagnostic": {
            "cortex_doctor", "cortex_verify_decision",
            "cortex_state", "cortex_roster",
        },
    }
    for group, tools in groups.items():
        registered_in_group = tools & registered
        assert len(registered_in_group) >= 3, (
            f"group '{group}' has {len(registered_in_group)} tools registered "
            f"(expected >= 3): {sorted(registered_in_group)}"
        )


@pytest.mark.asyncio
async def test_handoff_list_filters_with_agent_query_param(mcp_module, monkeypatch):
    """The API's role-aware mine filter is `agent=`, not `to_role=`."""

    calls = []

    async def fake_safe_call(ctx, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"handoffs": []}

    monkeypatch.setattr(mcp_module, "_safe_call", fake_safe_call)

    result = await mcp_module.cortex_handoff_list(
        SimpleNamespace(),
        agent="alpha",
        status="pending",
    )

    assert result == {"handoffs": []}
    assert calls == [
        (
            "GET",
            "/handoffs",
            {"params": {"status": "pending", "agent": "alpha"}},
        )
    ]


@pytest.mark.asyncio
async def test_handoff_claim_sends_beat_heartbeat_after_success(mcp_module):
    """MCP claim wiring emits the first Beat heartbeat for active tracking."""

    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def put(self, path, json=None, headers=None):
            self.calls.append(("PUT", path, json, headers))
            return Response({"claimed": True})

        async def post(self, path, json=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            if path.endswith("/claim-with-budget"):
                return Response({"claimed": True, "budget": {"allow_llm": True}})
            return Response({"task": {"state": "executing"}})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_claim(
        "77ae5f47-0000-0000-0000-000000000000",
        "root",
        ctx,
    )

    assert result == {
        "claimed": True,
        "beat_heartbeat": {"task": {"state": "executing"}},
    }
    assert http.calls == [
        (
            "PUT",
            "/handoffs/77ae5f47-0000-0000-0000-000000000000/claim",
            {},
            {"X-Agent-Name": "root"},
        ),
        (
            "POST",
            "/beat/tasks/77ae5f47-0000-0000-0000-000000000000/heartbeat",
            {"evidence_summary": "claimed handoff"},
            {"X-Agent-Name": "root"},
        ),
    ]


@pytest.mark.asyncio
async def test_handoff_claim_failure_skips_beat_heartbeat(mcp_module):
    """Failed MCP claims return detail without active tracking."""

    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def put(self, path, json=None, headers=None):
            self.calls.append(("PUT", path, json, headers))
            return Response({"claimed": False, "reason": "already claimed"})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_claim(
        "77ae5f47-0000-0000-0000-000000000000",
        "root",
        ctx,
    )

    assert result["claimed"] is False
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_handoff_complete_sends_beat_claim_done_after_success(mcp_module):
    """MCP complete wiring emits the Beat claim-done terminal signal."""

    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs.get("json"), kwargs.get("headers")))
            return Response({"ok": True})

        async def post(self, path, json=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            return Response({"task": {"state": "verified"}})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_complete(
        "77ae5f47-0000-0000-0000-000000000000",
        ctx,
        agent="root",
        outcome="completed",
        evidence_summary="ship verified",
    )

    assert result == {
        "ok": True,
        "beat_claim_done": {"task": {"state": "verified"}},
    }
    assert http.calls == [
        (
            "PUT",
            "/handoffs/77ae5f47-0000-0000-0000-000000000000/complete",
            None,
            None,
        ),
        (
            "POST",
            "/beat/tasks/77ae5f47-0000-0000-0000-000000000000/claim-done",
            {"outcome": "completed", "evidence_summary": "ship verified"},
            {"X-Agent-Name": "root"},
        ),
    ]


@pytest.mark.asyncio
async def test_handoff_complete_no_agent_skips_claim_done(mcp_module):
    """Back-compat: cortex_handoff_complete called without `agent` arg
    flips local state but skips the claim-done leg.
    """

    class Response:
        headers = {"content-type": "application/json"}
        text = "{}"

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class HTTP:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs.get("json"), kwargs.get("headers")))
            return Response({"ok": True})

        async def post(self, path, json=None, headers=None):
            self.calls.append(("POST", path, json, headers))
            return Response({})

    http = HTTP()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"http": http})
    )

    result = await mcp_module.cortex_handoff_complete(
        "77ae5f47-0000-0000-0000-000000000000",
        ctx,
    )

    assert result == {"ok": True}
    # Only the local complete call fired; no /beat/tasks/.../claim-done.
    assert len(http.calls) == 1
    assert http.calls[0][0] == "PUT"
    assert "/complete" in http.calls[0][1]
