"""Feature-gap #81 — unit tests for the CortexClient REGISTRATION write methods.

Three narrow mutating calls that let the in-console registration UX write to the
live Cortex registry over its HTTP API (never psql):

  * ``create_agent``   → ``POST /agents``                (register/upsert; the
                          client's agent is the CALLER ``X-Agent-Name`` — a
                          registered writer — gating the write, NOT the subject)
  * ``remove_agent``   → ``POST /admin/agents/remove``   (admin-token gated)
  * ``create_project`` → ``POST /projects``              (admin-token gated)

Every one honours the house GRACEFUL-DEGRADE contract the sibling mutators keep
(`cortex_client.py` claim/complete/set_project_repo_root): a 4xx/5xx or transport
error returns ``None``/``False`` rather than raising into the route, a blank
required arg short-circuits WITHOUT a network call, and the admin-gated pair source
the ``X-Cortex-Admin-Token`` SERVER-SIDE (it is asserted present on the wire but is
NEVER returned to / rendered for the caller).

Driven over an httpx ``MockTransport`` so the HTTP shape (method, path, scoped
headers, body) is asserted without a live Cortex — the same contract-level approach
the existing ``test_cortex_client_complete`` / ``_artifacts`` suites use.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import cortex_client as cc
from app.cortex_client import CortexClient


def _client_with_transport(handler, *, agent: str = "ren") -> CortexClient:
    """A CortexClient whose shared httpx.AsyncClient routes through `handler`
    (an httpx.MockTransport callable) — no network, no live Cortex."""
    client = CortexClient(base_url="http://cortex.test", agent=agent)
    client._client = httpx.AsyncClient(
        base_url="http://cortex.test",
        transport=httpx.MockTransport(handler),
    )
    return client


# ===========================================================================
#  create_agent → POST /agents  (caller-gated; NOT admin-gated)
# ===========================================================================


@pytest.mark.asyncio
async def test_create_agent_posts_register_with_caller_and_body():
    """A 200 from POST /agents returns the registered agent name, and the request
    carries the CLIENT'S agent as the caller (X-Agent-Name) + the X-Project scope +
    a body matching the AgentRegister model (name/role/capabilities/writer_scope)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["x_project"] = request.headers.get("X-Project")
        seen["x_agent"] = request.headers.get("X-Agent-Name")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"registered": True, "agent": "newbie", "role": "qa"})

    client = _client_with_transport(handler, agent="ren")
    try:
        out = await client.create_agent(
            "kaidera-os",
            name="newbie",
            role="qa",
            capabilities={"harness": "claude-code", "model": "opus"},
            writer_scope="work",
            role_description="quality keeper",
        )
    finally:
        await client.aclose()

    assert out == {"registered": True, "agent": "newbie", "role": "qa"}
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/agents")
    assert seen["x_project"] == "kaidera-os"
    # The client's own agent is the CALLER that gates the write (not the subject).
    assert seen["x_agent"] == "ren"
    body = seen["body"]
    assert body["name"] == "newbie"
    assert body["role"] == "qa"
    assert body["capabilities"] == {"harness": "claude-code", "model": "opus"}
    assert body["writer_scope"] == "work"
    assert body["role_description"] == "quality keeper"


@pytest.mark.asyncio
async def test_create_agent_caller_override_uses_subject_not_console_agent():
    """The optional `caller` overrides X-Agent-Name (the writer that gates the POST). The PROMOTE
    path passes the SUBJECT agent as caller, so a turnkey-project promote is authorised by a writer
    ON that project — not the console's fixed identity (CONSOLE_AGENT defaults to the kaidera-os
    writer 'ren', which is never a writer on e.g. 'marketing', so promote was rejected). caller=None
    keeps the legacy default. Regression for the reported 'registry sync failed' on a turnkey."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x_agent"] = request.headers.get("X-Agent-Name")
        return httpx.Response(200, json={"registered": True, "agent": "wren", "role": "cmo"})

    client = _client_with_transport(handler, agent="ren")
    try:
        await client.create_agent("marketing", name="wren", role="cmo", caller="wren")
        assert seen["x_agent"] == "wren"  # subject-as-caller (the fix), NOT "ren"
        await client.create_agent("kaidera-os", name="x", role="dev")
        assert seen["x_agent"] == "ren"   # caller=None → the console agent (legacy default kept)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_create_agent_omits_optional_fields_when_absent():
    """Only the required name+role (+ any given capabilities) go on the wire; the
    optional writer_scope / role_description are omitted when not supplied."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"registered": True, "agent": "x", "role": "dev"})

    client = _client_with_transport(handler)
    try:
        out = await client.create_agent("kaidera-os", name="x", role="dev")
    finally:
        await client.aclose()

    assert out is not None
    body = seen["body"]
    assert body["name"] == "x"
    assert body["role"] == "dev"
    assert "writer_scope" not in body
    assert "role_description" not in body
    # capabilities defaults to an empty object (valid for AgentRegister).
    assert body.get("capabilities") == {}


@pytest.mark.asyncio
async def test_create_agent_blank_name_or_role_short_circuits():
    """A blank name or role returns None WITHOUT issuing a request (mirrors the
    blank-id guards on the sibling mutators)."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"registered": True})

    client = _client_with_transport(handler)
    try:
        assert await client.create_agent("kaidera-os", name="  ", role="dev") is None
        assert await client.create_agent("kaidera-os", name="x", role="") is None
        assert await client.create_agent("  ", name="x", role="dev") is None
    finally:
        await client.aclose()
    assert called["n"] == 0, "a blank required arg must not hit the network"


@pytest.mark.asyncio
async def test_create_agent_graceful_degrade_on_error_status():
    """A 403 (caller not a registered writer) returns None — never raises."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "not a registered writer"})

    client = _client_with_transport(handler)
    try:
        out = await client.create_agent("kaidera-os", name="x", role="dev")
    finally:
        await client.aclose()
    assert out is None


@pytest.mark.asyncio
async def test_create_agent_graceful_degrade_on_transport_error():
    """A transport failure (down Cortex) returns None, not an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cortex unreachable")

    client = _client_with_transport(handler)
    try:
        out = await client.create_agent("kaidera-os", name="x", role="dev")
    finally:
        await client.aclose()
    assert out is None


# ===========================================================================
#  remove_agent → POST /admin/agents/remove  (admin-token gated)
# ===========================================================================


@pytest.mark.asyncio
async def test_remove_agent_posts_admin_remove_with_token(monkeypatch):
    """A 200 from POST /admin/agents/remove returns True; the request carries the
    admin token header (sourced server-side) + the {project, agent_name} body."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: "secret-token")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Cortex-Admin-Token")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"removed": True, "agent": "gone"})

    client = _client_with_transport(handler)
    try:
        ok = await client.remove_agent("kaidera-os", "gone")
    finally:
        await client.aclose()

    assert ok is True
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/admin/agents/remove")
    assert seen["token"] == "secret-token"
    assert seen["body"] == {"project": "kaidera-os", "agent_name": "gone"}


@pytest.mark.asyncio
async def test_remove_agent_returns_true_on_idempotent_noop(monkeypatch):
    """A 200 with removed:false (already-absent / already-inactive idempotent no-op)
    still counts as success (True) — the roster ends in the intended state."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: "t")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"removed": False, "already_absent": True})

    client = _client_with_transport(handler)
    try:
        ok = await client.remove_agent("kaidera-os", "ghost")
    finally:
        await client.aclose()
    assert ok is True


@pytest.mark.asyncio
async def test_remove_agent_no_token_returns_false_no_request(monkeypatch):
    """With NO admin token configured, remove_agent returns False WITHOUT issuing a
    request (the graceful 'admin not configured' path — nothing is sent)."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: None)
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"removed": True})

    client = _client_with_transport(handler)
    try:
        ok = await client.remove_agent("kaidera-os", "x")
    finally:
        await client.aclose()
    assert ok is False
    assert called["n"] == 0, "no token → no network call"


@pytest.mark.asyncio
async def test_remove_agent_blank_args_short_circuit(monkeypatch):
    """A blank project or agent returns False WITHOUT a request (even with a token)."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: "t")
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"removed": True})

    client = _client_with_transport(handler)
    try:
        assert await client.remove_agent("kaidera-os", "  ") is False
        assert await client.remove_agent("", "x") is False
    finally:
        await client.aclose()
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_remove_agent_graceful_degrade_on_error(monkeypatch):
    """A 401/403 (bad token) or transport error returns False — never raises."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: "t")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad admin token"})

    client = _client_with_transport(handler)
    try:
        ok = await client.remove_agent("kaidera-os", "x")
    finally:
        await client.aclose()
    assert ok is False


# ===========================================================================
#  create_project → POST /projects  (admin-token gated)
# ===========================================================================


@pytest.mark.asyncio
async def test_create_project_posts_with_token_and_body(monkeypatch):
    """A 200 from POST /projects returns the result dict; the request carries the
    admin token + a body matching the ProjectRegister model (project_key,
    display_name, repo_root)."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: "admintok")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Cortex-Admin-Token")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"project_key": "demo", "registered": True})

    client = _client_with_transport(handler)
    try:
        out = await client.create_project(
            project_key="demo",
            display_name="Demo Project",
            repo_root="/abs/demo",
            default_agent="lead",
            agents=[{"name": "lead", "role": "lead", "capabilities": {"designation": "interactive"}}],
        )
    finally:
        await client.aclose()

    assert out == {"project_key": "demo", "registered": True}
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/projects")
    assert seen["token"] == "admintok"
    body = seen["body"]
    assert body["project_key"] == "demo"
    assert body["display_name"] == "Demo Project"
    # The repo_root maps onto the registry's working-folder field.
    assert body["repo_root"] == "/abs/demo"
    assert body["default_agent"] == "lead"
    assert body["agents"] == [{"name": "lead", "role": "lead", "capabilities": {"designation": "interactive"}}]


@pytest.mark.asyncio
async def test_create_project_omits_optional_fields_when_absent(monkeypatch):
    """Only project_key (+ any given display_name/repo_root) go on the wire; blank
    optionals are omitted so the API applies its own defaults."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: "t")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"project_key": "p", "registered": True})

    client = _client_with_transport(handler)
    try:
        out = await client.create_project(project_key="p", repo_root="/abs/p")
    finally:
        await client.aclose()

    assert out is not None
    body = seen["body"]
    assert body["project_key"] == "p"
    assert "display_name" not in body  # omitted, not sent blank


@pytest.mark.asyncio
async def test_create_project_blank_key_short_circuits(monkeypatch):
    """A blank project_key returns None WITHOUT a request."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: "t")
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"registered": True})

    client = _client_with_transport(handler)
    try:
        out = await client.create_project(project_key="   ", repo_root="/abs/x")
    finally:
        await client.aclose()
    assert out is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_create_project_no_token_returns_none_no_request(monkeypatch):
    """With NO admin token, create_project returns None WITHOUT a request."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: None)
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"registered": True})

    client = _client_with_transport(handler)
    try:
        out = await client.create_project(project_key="p", repo_root="/abs/p")
    finally:
        await client.aclose()
    assert out is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_create_project_graceful_degrade_on_error(monkeypatch):
    """A 400 (bad body) or transport error returns None — never raises."""
    monkeypatch.setattr(cc, "resolve_admin_token", lambda: "t")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = _client_with_transport(handler)
    try:
        out = await client.create_project(project_key="p", repo_root="/abs/p")
    finally:
        await client.aclose()
    assert out is None
