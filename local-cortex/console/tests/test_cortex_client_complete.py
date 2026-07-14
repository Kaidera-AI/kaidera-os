"""Milestone 1 T9 — unit tests for `CortexClient.complete_handoff` (HTTP).

`complete_handoff` is the HTTP completion call the "Approve & Run" cycle uses to
close a handoff once its run succeeds — the mirror of the existing `claim_handoff`
(`cortex_client.py`). It PUTs the Cortex API's handoff-complete endpoint
(`PUT /handoffs/{id}/complete`, X-Project required, X-Agent-Name optional) and,
like every sibling on this client, GRACEFUL-DEGRADES: a down/erroring API returns
False rather than raising into the route (a dead Cortex must never crash a run).

We drive the real `CortexClient` over an httpx `MockTransport` so the HTTP shape
(method, path, scoped headers) is asserted without a live Cortex — the same
contract-level approach the API expresses (`.agents/api/main.py:6456`)."""

from __future__ import annotations

import httpx
import pytest

from app.cortex_client import CortexClient


def _client_with_transport(handler) -> CortexClient:
    """A CortexClient whose shared httpx.AsyncClient routes through `handler`
    (an httpx.MockTransport callable) — no network, no live Cortex."""
    client = CortexClient(base_url="http://cortex.test", agent="ren")
    # Swap the pooled client for one backed by the mock transport (same base_url).
    client._client = httpx.AsyncClient(
        base_url="http://cortex.test",
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_complete_handoff_puts_complete_endpoint_with_scoped_headers():
    """A 200 from PUT /handoffs/{id}/complete returns True, and the request carries
    the X-Project scope + the X-Agent-Name actor (mirrors claim_handoff)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["x_project"] = request.headers.get("X-Project")
        seen["x_agent"] = request.headers.get("X-Agent-Name")
        return httpx.Response(200, json={"completed": True})

    client = _client_with_transport(handler)
    try:
        ok = await client.complete_handoff("kaidera-os", "h-done-1", "kai")
    finally:
        await client.aclose()

    assert ok is True
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/handoffs/h-done-1/complete")
    assert seen["x_project"] == "kaidera-os"
    assert seen["x_agent"] == "kai"


@pytest.mark.asyncio
async def test_complete_handoff_returns_false_on_404():
    """A 404 (already completed / not found) returns False — never raises."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    client = _client_with_transport(handler)
    try:
        ok = await client.complete_handoff("kaidera-os", "h-missing", "kai")
    finally:
        await client.aclose()
    assert ok is False


@pytest.mark.asyncio
async def test_complete_handoff_graceful_degrade_on_transport_error():
    """A transport failure (down Cortex) returns False, not an exception — the
    graceful-degrade contract every CortexClient method honours."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cortex unreachable")

    client = _client_with_transport(handler)
    try:
        ok = await client.complete_handoff("kaidera-os", "h-any", "kai")
    finally:
        await client.aclose()
    assert ok is False


@pytest.mark.asyncio
async def test_complete_handoff_blank_id_is_false_no_request():
    """A blank handoff id short-circuits to False WITHOUT issuing a request
    (mirrors claim_handoff's blank-id guard)."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"completed": True})

    client = _client_with_transport(handler)
    try:
        ok = await client.complete_handoff("kaidera-os", "   ", "kai")
    finally:
        await client.aclose()
    assert ok is False
    assert called["n"] == 0, "blank id must not hit the network"


@pytest.mark.asyncio
async def test_complete_handoff_omits_agent_header_when_blank():
    """X-Agent-Name is OPTIONAL on the complete endpoint (unlike claim). When no
    agent is given we still complete (the API back-fills the actor); the header is
    simply omitted rather than sent blank."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x_agent_present"] = "X-Agent-Name" in request.headers
        seen["x_project"] = request.headers.get("X-Project")
        return httpx.Response(200, json={"completed": True})

    client = _client_with_transport(handler)
    try:
        ok = await client.complete_handoff("kaidera-os", "h-no-agent", "")
    finally:
        await client.aclose()
    assert ok is True
    assert seen["x_project"] == "kaidera-os"
    assert seen["x_agent_present"] is False


@pytest.mark.asyncio
async def test_get_handoff_reads_full_body_with_project_scope():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["project"] = request.headers.get("X-Project")
        return httpx.Response(200, json={"id": "abcdef12-full", "status": "claimed"})

    client = _client_with_transport(handler)
    try:
        handoff = await client.get_handoff("marketing", "abcdef12")
    finally:
        await client.aclose()

    assert handoff == {"id": "abcdef12-full", "status": "claimed"}
    assert seen == {
        "method": "GET",
        "path": "/handoffs/abcdef12",
        "project": "marketing",
    }


@pytest.mark.asyncio
async def test_release_handoff_posts_reason_with_project_scope():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["project"] = request.headers.get("X-Project")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"released": True})

    client = _client_with_transport(handler)
    try:
        ok = await client.release_handoff(
            "marketing", "stuck-1", reason="no heartbeat"
        )
    finally:
        await client.aclose()

    assert ok is True
    assert seen["method"] == "POST"
    assert seen["path"] == "/handoffs/stuck-1/release"
    assert seen["project"] == "marketing"
    assert '"reason":"no heartbeat"' in seen["body"]
