"""Chat file-attachments (feature-gap step 6, Inc A) — the upload route
(`POST /agents/{p}/{a}/chat/upload`) + the `client_run_id` sharing in `agent_chat`.

The SPA pre-mints a `client_run_id` (uuid4) and uses it for BOTH the upload(s) and the
chat send, so the uploaded bytes land under the SAME run the chat turn writes to. This
route receives one base64-encoded file (NO python-multipart — base64-in-JSON, preserving
the no-multipart discipline), confines + writes it via `app.attachments.receive_upload`,
and returns `{attachment_id, filename, size_bytes}` — NEVER the host path (the absolute
on-disk location must never cross to the client).

These tests drive the real app over an in-process httpx `ASGITransport` (no socket, no
live harness) and assert:
  * a valid body → 200 + {attachment_id, filename, size_bytes}, and the host_path is
    NOT in the response;
  * an oversized body → 400 (the per-file cap);
  * an escaping filename (`../`) → 400/403 (the sandbox gate);
  * a bad-base64 body → 400.

`agent_chat`'s `client_run_id` validation (uuid4 + not-an-existing-run, else mint) is
unit-tested directly against the helper.
"""

from __future__ import annotations

import base64
import uuid

import httpx
import pytest

import app.main as main_mod


# ---------------------------------------------------------------------------
#  Upload route — ASGITransport, hermetic sandbox
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """An httpx AsyncClient over the real ASGI app, with the attachments sandbox pointed
    at a temp dir (hermetic). No live harness / DB needed for the upload route."""
    # This suite drives the REAL ASGI stack (incl. the auth middleware) to test the
    # upload SANDBOX, not auth. Declare test mode so auth fails OPEN (mirrors kaidera-os);
    # without it the v0.1.143 fail-closed default 401s every non-public path.
    monkeypatch.setenv("KAIDERA_DEPLOY_MODE", "test")
    monkeypatch.setenv("HARNESS_ATTACHMENTS_ROOT", str(tmp_path / "attach"))
    transport = httpx.ASGITransport(app=main_mod.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.mark.asyncio
async def test_upload_valid_returns_id_and_no_host_path(client):
    run_id = str(uuid.uuid4())
    body = {
        "run_id": run_id,
        "filename": "notes.txt",
        "content_type": "text/plain",
        "data": _b64(b"hello attachment"),
    }
    async with client as c:
        resp = await c.post("/agents/kaidera-os/kai/chat/upload", json=body)
    assert resp.status_code == 200
    out = resp.json()
    assert out["filename"] == "notes.txt"
    assert out["size_bytes"] == len(b"hello attachment")
    assert out["attachment_id"]
    # The absolute host path must NEVER cross to the client.
    assert "host_path" not in out
    assert tmp_attach_not_leaked(out)


def tmp_attach_not_leaked(out: dict) -> bool:
    """No value in the response should look like an absolute filesystem path."""
    for v in out.values():
        if isinstance(v, str) and v.startswith("/"):
            return False
    return True


@pytest.mark.asyncio
async def test_upload_oversized_is_400(client, monkeypatch):
    monkeypatch.setattr(main_mod.attachments_module, "MAX_FILE_BYTES", 4)
    run_id = str(uuid.uuid4())
    body = {
        "run_id": run_id,
        "filename": "big.txt",
        "content_type": "text/plain",
        "data": _b64(b"way too big to fit"),
    }
    async with client as c:
        resp = await c.post("/agents/kaidera-os/kai/chat/upload", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_escaping_filename_is_rejected(client):
    run_id = str(uuid.uuid4())
    body = {
        "run_id": run_id,
        "filename": "../escape.txt",
        "content_type": "text/plain",
        "data": _b64(b"x"),
    }
    async with client as c:
        resp = await c.post("/agents/kaidera-os/kai/chat/upload", json=body)
    assert resp.status_code in (400, 403)


@pytest.mark.asyncio
async def test_upload_bad_base64_is_400(client):
    run_id = str(uuid.uuid4())
    body = {
        "run_id": run_id,
        "filename": "x.txt",
        "content_type": "text/plain",
        "data": "!!!not base64!!!",
    }
    async with client as c:
        resp = await c.post("/agents/kaidera-os/kai/chat/upload", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_missing_run_id_is_400(client):
    body = {"filename": "x.txt", "content_type": "text/plain", "data": _b64(b"x")}
    async with client as c:
        resp = await c.post("/agents/kaidera-os/kai/chat/upload", json=body)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
#  client_run_id validation helper — uuid4 + not an existing run, else mint
# ---------------------------------------------------------------------------

def test_valid_client_run_id_accepts_uuid4():
    rid = str(uuid.uuid4())
    assert main_mod._valid_client_run_id(rid) == rid


def test_valid_client_run_id_rejects_garbage():
    assert main_mod._valid_client_run_id("not-a-uuid") is None
    assert main_mod._valid_client_run_id("") is None
    assert main_mod._valid_client_run_id(None) is None
    # A non-v4 uuid (version nibble != 4) is rejected.
    assert main_mod._valid_client_run_id("00000000-0000-1000-8000-000000000000") is None


def test_valid_client_run_id_rejects_path_y_input():
    assert main_mod._valid_client_run_id("../../etc/passwd") is None
    assert main_mod._valid_client_run_id("abc/def") is None
