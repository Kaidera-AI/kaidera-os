"""Harness-service Increment 2 — tests for the RemoteHarnessAdapter (the wire seam).

`app/adapters/harness_remote.py` implements the pure `HarnessPort`
(`app/domain/harness.py`) over **httpx** — the CONTAINER-side adapter that POSTs to
the HOST-resident harness-service (`app/harness_service.py`). It is the remote twin
of the I1 `LocalHarnessAdapter`: where the local adapter shells `subprocess.Popen`
in-process, this one crosses the host boundary with `POST {base_url}/spawn` (bearer
`HARNESS_SERVICE_TOKEN`) and maps the JSON reply back to a `SpawnHandle`; `cancel_run`
POSTs `/cancel/{run_id}`.

These tests drive the real adapter over an httpx `MockTransport` (so NOTHING real is
spawned and NO socket is opened) and assert the wire contract:
  * the adapter SATISFIES `HarnessPort` (structural isinstance),
  * a 202 → `SpawnHandle(accepted=True, exit_code=None)` — the async "dispatched"
    shape (the worker reports its terminal state later via run-state),
  * a 4xx/5xx → `SpawnHandle(accepted=False, error="<status>")` (NEVER raises),
  * an `httpx.ConnectError` / `TimeoutException` → `accepted=False` (NEVER raises),
  * the request carries `Authorization: Bearer <token>` + the SpawnRequest fields
    (`run_id`/`project`/`agent`/`handoff_id`) in the JSON body,
  * `cancel_run` 200 `{"cancelled": true}` → True; a transport error → False (no raise).

Same graceful-degrade contract as every sibling adapter: a down host service / a
timeout is reported, never raised — a broken spawn path must not crash the dispatch
loop.
"""

from __future__ import annotations

import httpx
import pytest

TOKEN = "test-bearer-token"  # fitness:allow-literal test fixture, not a real secret


def _adapter(handler, **overrides):
    """A RemoteHarnessAdapter whose injected httpx.AsyncClient routes through
    `handler` (an httpx.MockTransport callable) — no network, no live host service."""
    from app.adapters.harness_remote import RemoteHarnessAdapter

    client = httpx.AsyncClient(
        base_url="http://host.test",
        transport=httpx.MockTransport(handler),
    )
    kwargs = dict(base_url="http://host.test", token=TOKEN, http_client=client)
    kwargs.update(overrides)
    return RemoteHarnessAdapter(**kwargs)


def _req(**overrides):
    from app.domain.harness import SpawnRequest

    base = dict(
        run_id="run-1",
        project="proj-x",
        agent="worker-a",
        handoff_id="h-123",
    )
    base.update(overrides)
    return SpawnRequest(**base)


def _chat_req(**overrides):
    from app.domain.harness import ChatSpawnRequest

    base = dict(
        run_id="crun-1",
        project="proj-x",
        agent="kai",
        message="hello there",
    )
    base.update(overrides)
    return ChatSpawnRequest(**base)


def test_adapter_satisfies_harness_port():
    from app.domain.harness import HarnessPort

    adapter = _adapter(lambda req: httpx.Response(202, json={}))
    assert isinstance(adapter, HarnessPort), "RemoteHarnessAdapter must satisfy HarnessPort"


@pytest.mark.asyncio
async def test_spawn_202_is_accepted_async_dispatched_shape():
    """A 202 Accepted → the async 'dispatched' SpawnHandle: accepted=True with
    exit_code=None (the worker's terminal state arrives later via run-state)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = _json.loads(request.content.decode() or "{}")
        return httpx.Response(202, json={"run_id": "run-1", "accepted": True})

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_run(_req(repo_root="/work/proj"))
    finally:
        await adapter.aclose()

    # The async dispatched shape.
    assert handle.run_id == "run-1"
    assert handle.accepted is True
    assert handle.exit_code is None
    assert handle.error is None
    # Wire shape: POST /spawn with the bearer token + the SpawnRequest body fields.
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/spawn")
    assert seen["auth"] == f"Bearer {TOKEN}"
    body = seen["body"]
    assert body["run_id"] == "run-1"
    assert body["project"] == "proj-x"
    assert body["agent"] == "worker-a"
    assert body["handoff_id"] == "h-123"
    assert body["repo_root"] == "/work/proj"


@pytest.mark.asyncio
async def test_spawn_5xx_is_rejected_with_status_error():
    """A 503 from the host service → accepted=False with error='503' (NEVER raises)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "service down"})

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_run(_req())
    finally:
        await adapter.aclose()

    assert handle.accepted is False
    assert handle.exit_code is None
    assert handle.error == "503"


@pytest.mark.asyncio
async def test_spawn_4xx_is_rejected_with_status_error():
    """A 401 (bad/missing token at the service) → accepted=False, error='401'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_run(_req())
    finally:
        await adapter.aclose()

    assert handle.accepted is False
    assert handle.error == "401"


@pytest.mark.asyncio
async def test_spawn_connect_error_is_rejected_not_raised():
    """An httpx.ConnectError (the host service is down / unreachable) is NEVER
    raised — it is reported as accepted=False + the error string (fire-and-forget)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_run(_req())
    finally:
        await adapter.aclose()

    assert handle.accepted is False
    assert handle.exit_code is None
    assert handle.error and "refused" in handle.error


@pytest.mark.asyncio
async def test_spawn_timeout_is_rejected_not_raised():
    """An httpx.TimeoutException (the host service hung) is NEVER raised — reported
    as accepted=False + the error string."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_run(_req())
    finally:
        await adapter.aclose()

    assert handle.accepted is False
    assert handle.error and "timed out" in handle.error.lower()


@pytest.mark.asyncio
async def test_cancel_run_200_true_returns_true_and_posts_with_bearer():
    """cancel_run POSTs /cancel/{run_id} with the bearer token; a 200
    {"cancelled": true} → True."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"cancelled": True})

    adapter = _adapter(handler)
    try:
        ok = await adapter.cancel_run("run-7")
    finally:
        await adapter.aclose()

    assert ok is True
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/cancel/run-7")
    assert seen["auth"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_cancel_run_200_false_returns_false():
    """A 200 {"cancelled": false} (unknown / already-gone run) → False."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cancelled": False})

    adapter = _adapter(handler)
    try:
        ok = await adapter.cancel_run("run-unknown")
    finally:
        await adapter.aclose()

    assert ok is False


@pytest.mark.asyncio
async def test_cancel_run_connect_error_returns_false_not_raised():
    """A transport error on cancel → False (best-effort, NEVER raises)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = _adapter(handler)
    try:
        ok = await adapter.cancel_run("run-7")
    finally:
        await adapter.aclose()

    assert ok is False


@pytest.mark.asyncio
async def test_cancel_run_5xx_returns_false():
    """Any non-200 from /cancel → False (best-effort cancel never escalates)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    adapter = _adapter(handler)
    try:
        ok = await adapter.cancel_run("run-7")
    finally:
        await adapter.aclose()

    assert ok is False


# ---------------------------------------------------------------------------
#  spawn_chat (Increment 4) — POST /chat to the host service. Same wire contract +
#  same fire-and-forget graceful-degrade as spawn_run, but for INTERACTIVE chat.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_chat_202_is_accepted_async_dispatched_shape():
    """A 202 → SpawnHandle(accepted=True, exit_code=None) — the chat runner reports
    its terminal state later via the run-state store (the UI reads /runstate/stream)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = _json.loads(request.content.decode() or "{}")
        return httpx.Response(202, json={"run_id": "crun-1", "accepted": True})

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_chat(_chat_req(repo_root="/work/proj"))
    finally:
        await adapter.aclose()

    assert handle.run_id == "crun-1"
    assert handle.accepted is True
    assert handle.exit_code is None
    assert handle.error is None
    # Wire shape: POST /chat with the bearer token + the ChatSpawnRequest body fields.
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/chat")
    assert seen["auth"] == f"Bearer {TOKEN}"
    body = seen["body"]
    assert body["run_id"] == "crun-1"
    assert body["project"] == "proj-x"
    assert body["agent"] == "kai"
    assert body["message"] == "hello there"
    assert body["repo_root"] == "/work/proj"
    # session_id is part of the serialized DTO (None when not set — single-shot).
    assert "session_id" in body and body["session_id"] is None


@pytest.mark.asyncio
async def test_spawn_chat_carries_session_id_in_body():
    """Multi-turn chat (Inc B): a ChatSpawnRequest with a session_id serializes it into
    the POST /chat body (so the host service forwards it to the chat runner)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["body"] = _json.loads(request.content.decode() or "{}")
        return httpx.Response(202, json={"run_id": "crun-1", "accepted": True})

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_chat(_chat_req(session_id="sess-77"))
    finally:
        await adapter.aclose()

    assert handle.accepted is True
    assert seen["body"]["session_id"] == "sess-77"


@pytest.mark.asyncio
async def test_spawn_chat_5xx_is_rejected_with_status_error():
    """A 503 → accepted=False with error='503' (NEVER raises)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "service down"})

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_chat(_chat_req())
    finally:
        await adapter.aclose()

    assert handle.accepted is False
    assert handle.error == "503"


@pytest.mark.asyncio
async def test_spawn_chat_connect_error_is_rejected_not_raised():
    """An httpx.ConnectError (host service down) → accepted=False + the error
    string; NEVER raised (fire-and-forget — a broken chat seam must not crash)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_chat(_chat_req())
    finally:
        await adapter.aclose()

    assert handle.accepted is False
    assert handle.error and "refused" in handle.error


@pytest.mark.asyncio
async def test_spawn_chat_timeout_is_rejected_not_raised():
    """An httpx.TimeoutException → accepted=False + the error string; never raised."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    adapter = _adapter(handler)
    try:
        handle = await adapter.spawn_chat(_chat_req())
    finally:
        await adapter.aclose()

    assert handle.accepted is False
    assert handle.error and "timed out" in handle.error.lower()


# ---------------------------------------------------------------------------
#  CHAT FILE-ATTACHMENTS (feature-gap step 6, Inc A) — upload_attachment posts the
#  base64 bytes to the host `/upload` and returns the host_path; spawn_chat carries
#  attachment_paths in the /chat body. upload_attachment NEVER raises (degrades to "").
# ---------------------------------------------------------------------------

import base64


@pytest.mark.asyncio
async def test_upload_attachment_posts_base64_returns_host_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"host_path": "/host/att/att-1/notes.txt"})

    adapter = _adapter(handler)
    async with adapter._client:
        host_path = await adapter.upload_attachment(
            "att-1", "notes.txt", base64.b64encode(b"hi").decode("ascii")
        )
    assert host_path == "/host/att/att-1/notes.txt"
    assert seen["url"].endswith("/upload")
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["body"]["attachment_id"] == "att-1"
    assert seen["body"]["filename"] == "notes.txt"
    assert "data" in seen["body"]


@pytest.mark.asyncio
async def test_upload_attachment_5xx_degrades_to_empty_string():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    adapter = _adapter(handler)
    async with adapter._client:
        host_path = await adapter.upload_attachment("att-1", "x.txt", "ZGF0YQ==")
    assert host_path == ""  # degrade, never raise


@pytest.mark.asyncio
async def test_upload_attachment_connect_error_degrades_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("host down")

    adapter = _adapter(handler)
    async with adapter._client:
        host_path = await adapter.upload_attachment("att-1", "x.txt", "ZGF0YQ==")
    assert host_path == ""


@pytest.mark.asyncio
async def test_upload_attachment_missing_host_path_degrades_to_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})  # no host_path key

    adapter = _adapter(handler)
    async with adapter._client:
        host_path = await adapter.upload_attachment("att-1", "x.txt", "ZGF0YQ==")
    assert host_path == ""


@pytest.mark.asyncio
async def test_spawn_chat_carries_attachment_paths_in_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(202, json={})

    adapter = _adapter(handler)
    async with adapter._client:
        handle = await adapter.spawn_chat(
            _chat_req(attachment_paths=["/host/a.txt", "/host/b.txt"])
        )
    assert handle.accepted is True
    assert seen["body"]["attachment_paths"] == ["/host/a.txt", "/host/b.txt"]
