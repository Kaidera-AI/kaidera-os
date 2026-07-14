"""CortexClient L5 artifact methods — `post_artifact` + `get_artifact_by_source_file`.

`post_artifact` is the Explain capability's persistence seam: it POSTs the Cortex
`POST /artifacts` endpoint (the `ArtifactIngestRequest` model) and returns the artifact
id, GRACEFUL-DEGRADING to None on ANY error (it NEVER raises — the Explain run treats the
L5 write as best-effort). `get_artifact_by_source_file` is the read counterpart: it queries
the Cortex search surface (`search_type=artifacts`) and returns the row whose `source_file`
matches exactly, or None.

Driven over an httpx `MockTransport` (no live Cortex), asserting the HTTP shape (method,
path, headers, the body fields that mirror the verified model) + the degrade contract.
"""

from __future__ import annotations

import httpx
import pytest

from app.cortex_client import CortexClient


def _client_with_transport(handler) -> CortexClient:
    """A CortexClient whose shared httpx.AsyncClient routes through `handler` — no
    network, no live Cortex."""
    client = CortexClient(base_url="http://cortex.test", agent="ren")
    client._client = httpx.AsyncClient(
        base_url="http://cortex.test",
        transport=httpx.MockTransport(handler),
    )
    return client


# ---------------------------------------------------------------------------
#  search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_defaults_to_server_rerank_policy():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [{"text": "hit"}]})

    client = _client_with_transport(handler)
    try:
        results = await client.search("kaidera-os", "dashboard", limit=7)
    finally:
        await client.aclose()

    assert results == [{"text": "hit"}]
    assert seen["params"] == {"q": "dashboard", "limit": "7"}


@pytest.mark.asyncio
async def test_search_can_disable_rerank_for_seed_feeds():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": []})

    client = _client_with_transport(handler)
    try:
        await client.search("kaidera-os", "recent decisions", limit=5, rerank=False)
    finally:
        await client.aclose()

    assert seen["params"] == {
        "q": "recent decisions",
        "limit": "5",
        "rerank": "false",
    }


# ---------------------------------------------------------------------------
#  get_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_history_uses_cortex_last_query_param():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["x_project"] = request.headers.get("X-Project")
        return httpx.Response(200, json={"messages": [{"content": "hello"}]})

    client = _client_with_transport(handler)
    try:
        rows = await client.get_history("kaidera-os", limit=40)
    finally:
        await client.aclose()

    assert rows == [{"content": "hello"}]
    assert seen["path"] == "/history"
    assert seen["params"] == {"last": "40"}
    assert seen["x_project"] == "kaidera-os"


# ---------------------------------------------------------------------------
#  post_artifact
# ---------------------------------------------------------------------------

_HASH = "a" * 64  # a valid 64-char sha256 hex


@pytest.mark.asyncio
async def test_post_artifact_posts_and_returns_id():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["x_project"] = request.headers.get("X-Project")
        seen["x_agent"] = request.headers.get("X-Agent-Name")
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"artifact_id": "art-99", "modality": "html"})

    client = _client_with_transport(handler)
    try:
        aid = await client.post_artifact(
            "kaidera-os", "kai",
            source_file="explain/run-1.html",
            content_hash=_HASH,
            modality="html",
            raw_content="<!DOCTYPE html><html></html>",
            caption="A title",
            neighborhood_text="Explain: file x.py — A title",
            source_doc_metadata={"explain_kind": "file"},
            metadata={"capability": "explain"},
            edge_type="explains",
            target_type="file",
            target_ref="x.py",
        )
    finally:
        await client.aclose()

    assert aid == "art-99"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/artifacts")
    assert seen["x_project"] == "kaidera-os"
    assert seen["x_agent"] == "kai"
    body = seen["body"]
    # Mirrors the verified ArtifactIngestRequest model + the generated-capture contract.
    assert body["source_file"] == "explain/run-1.html"
    assert body["content_hash"] == _HASH
    assert body["modality"] == "html"
    assert body["source_type"] == "api_capture"
    assert body["extraction_method"] == "generated"
    assert body["raw_content"].startswith("<!DOCTYPE html>")
    assert body["caption"] == "A title"
    assert body["neighborhood_text"].startswith("Explain: file")
    assert body["source_doc_metadata"] == {"explain_kind": "file"}
    assert body["metadata"] == {"capability": "explain"}
    assert body["edge_type"] == "explains"
    assert body["target_type"] == "file"
    assert body["target_ref"] == "x.py"


@pytest.mark.asyncio
async def test_post_artifact_returns_id_when_only_id_key():
    """The endpoint may return `id` instead of `artifact_id` — both are accepted."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "art-id-only"})

    client = _client_with_transport(handler)
    try:
        aid = await client.post_artifact(
            "kaidera-os", "kai", source_file="explain/r.html",
            content_hash=_HASH, raw_content="<html></html>",
        )
    finally:
        await client.aclose()
    assert aid == "art-id-only"


@pytest.mark.asyncio
async def test_post_artifact_omits_edge_when_partial():
    """An incomplete edge triple is NOT sent (the API requires all three together)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"artifact_id": "x"})

    client = _client_with_transport(handler)
    try:
        await client.post_artifact(
            "kaidera-os", "kai", source_file="explain/r.html",
            content_hash=_HASH, raw_content="<html></html>",
            edge_type="explains",  # but no target_type/target_ref
        )
    finally:
        await client.aclose()
    assert "edge_type" not in seen["body"]
    assert "target_type" not in seen["body"]


@pytest.mark.asyncio
async def test_post_artifact_5xx_returns_none_no_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = _client_with_transport(handler)
    try:
        aid = await client.post_artifact(
            "kaidera-os", "kai", source_file="explain/r.html",
            content_hash=_HASH, raw_content="<html></html>",
        )
    finally:
        await client.aclose()
    assert aid is None  # degrade, never raise


@pytest.mark.asyncio
async def test_post_artifact_connect_error_returns_none_no_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cortex down")

    client = _client_with_transport(handler)
    try:
        aid = await client.post_artifact(
            "kaidera-os", "kai", source_file="explain/r.html",
            content_hash=_HASH, raw_content="<html></html>",
        )
    finally:
        await client.aclose()
    assert aid is None


@pytest.mark.asyncio
async def test_post_artifact_blank_inputs_short_circuit():
    """A blank source_file / content_hash returns None WITHOUT a request (defensive)."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"artifact_id": "x"})

    client = _client_with_transport(handler)
    try:
        assert await client.post_artifact(
            "kaidera-os", "kai", source_file="", content_hash=_HASH, raw_content="<html>"
        ) is None
        assert await client.post_artifact(
            "kaidera-os", "kai", source_file="explain/r.html", content_hash="", raw_content="<html>"
        ) is None
    finally:
        await client.aclose()
    assert called["n"] == 0


# ---------------------------------------------------------------------------
#  get_artifact_by_source_file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_artifact_by_source_file_hit():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["x_project"] = request.headers.get("X-Project")
        return httpx.Response(200, json={"results": [
            {"id": "other", "text": "...", "meta": "explain/zzz.html", "source": "artifacts"},
            {"id": "art-7", "text": "<title>A</title>...", "meta": "explain/run-9.html",
             "category": "html", "source": "artifacts"},
        ]})

    client = _client_with_transport(handler)
    try:
        row = await client.get_artifact_by_source_file("kaidera-os", "explain/run-9.html")
    finally:
        await client.aclose()

    assert row is not None
    assert row["id"] == "art-7"  # the EXACT source_file match, not the first row
    assert "type=artifacts" in seen["url"]
    assert "explain%2Frun-9.html" in seen["url"] or "explain/run-9.html" in seen["url"]
    assert seen["x_project"] == "kaidera-os"


@pytest.mark.asyncio
async def test_get_artifact_by_source_file_miss_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"id": "x", "meta": "explain/other.html", "source": "artifacts"},
        ]})

    client = _client_with_transport(handler)
    try:
        row = await client.get_artifact_by_source_file("kaidera-os", "explain/nope.html")
    finally:
        await client.aclose()
    assert row is None


@pytest.mark.asyncio
async def test_get_artifact_by_source_file_error_returns_none_no_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = _client_with_transport(handler)
    try:
        row = await client.get_artifact_by_source_file("kaidera-os", "explain/r.html")
    finally:
        await client.aclose()
    assert row is None


@pytest.mark.asyncio
async def test_get_artifact_by_source_file_blank_short_circuits():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    client = _client_with_transport(handler)
    try:
        assert await client.get_artifact_by_source_file("kaidera-os", "") is None
    finally:
        await client.aclose()
    assert called["n"] == 0
