"""Chat file-attachments (feature-gap step 6, Inc A) — the in-process `agent_chat`
enrichment path.

When the chat POST carries `attachment_ids` (the SPA uploaded files under the SAME
`client_run_id`), `agent_chat` resolves the run's sandbox files, weaves them into the
CURRENT message via `inline_attachments` BEFORE `stream_chat`, persists ONE `attachment`
span per file (so the transcript shows a chip), and cleans the sandbox on terminal.

These tests drive the route function directly + drain its `EventSourceResponse`
body_iterator (the SAME idiom as `test_chat_run_route.py`), with a fake store + a spy
`stream_chat`, so no ASGI / live harness is needed. They assert:
  * the prompt `stream_chat` receives CONTAINS the inlined attachment block
    (`[Attached: notes.txt]` + the file content);
  * an `attachment` span is written for the file (alongside the `input` span);
  * the sandbox dir is CLEANED after the terminal status;
  * BACKWARD-COMPAT: with NO attachment_ids the prompt is byte-for-byte the message
    (single-shot, unchanged) and no `attachment` span is written.
"""

from __future__ import annotations

import base64

import pytest

import app.attachments as attachments
import app.main as main_mod


# Reuse the fakes + wiring from the T10 chat-route test (same shapes).
from tests.test_chat_run_route import (  # noqa: E402
    FakeCortex,
    FakeStore,
    _Req,
    _drain,
    _events_ok,
    _install_common,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ATTACHMENTS_ROOT", str(tmp_path / "attach"))
    return tmp_path


@pytest.mark.asyncio
async def test_chat_inlines_attachment_into_prompt(monkeypatch, sandbox):
    cortex = FakeCortex()
    store = FakeStore()

    # The run id the route will use (the SPA's client_run_id). Pre-write a file into its
    # sandbox dir (as the upload route would have).
    run_id = "11111111-1111-4111-8111-111111111111"
    attachments.receive_upload(run_id, "notes.txt", _b64(b"secret recipe inside"), "text/plain")

    seen: dict = {}

    async def _spy_stream(msg, *, model=None, system=None, harness=None, reasoning=None, **kw):
        seen["prompt"] = msg
        for ev in _events_ok():
            yield ev

    _install_common(
        monkeypatch, cortex,
        form={
            "message": "please review",
            "client_run_id": run_id,
            "attachment_ids": "att-abc",
        },
    )
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    # The prompt the harness saw carries the inlined attachment block + the file content.
    assert "[Attached: notes.txt]" in seen["prompt"]
    assert "secret recipe inside" in seen["prompt"]
    assert "please review" in seen["prompt"]


@pytest.mark.asyncio
async def test_chat_inlines_image_path_for_vision_capable_pair(monkeypatch, sandbox):
    cortex = FakeCortex()
    store = FakeStore()

    run_id = "11111111-1111-4111-8111-222222222222"
    meta = attachments.receive_upload(
        run_id, "shot.png", _b64(b"\x89PNG\r\n\x1a\n\x00"), "image/png"
    )

    seen: dict = {}

    async def _spy_stream(msg, *, model=None, system=None, harness=None, reasoning=None, **kw):
        seen["prompt"] = msg
        seen["model"] = model
        seen["harness"] = harness
        for ev in _events_ok():
            yield ev

    _install_common(
        monkeypatch, cortex,
        form={
            "message": "inspect",
            "client_run_id": run_id,
            "attachment_ids": "att-img",
        },
    )
    monkeypatch.setattr(main_mod, "_chat_routing_for", lambda agent, project: ("pi", "gpt-5.4", "low"))
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    assert seen["harness"] == "pi"
    assert seen["model"] == "gpt-5.4"
    assert "[Attached image: shot.png]" in seen["prompt"]
    assert "Vision-capable attachment path" in seen["prompt"]
    assert meta.host_path in seen["prompt"]
    assert "not readable" not in seen["prompt"].lower()


@pytest.mark.asyncio
async def test_chat_writes_attachment_span(monkeypatch, sandbox):
    cortex = FakeCortex()
    store = FakeStore()
    run_id = "22222222-2222-4222-8222-222222222222"
    attachments.receive_upload(run_id, "a.txt", _b64(b"data"), "text/plain")

    _install_common(
        monkeypatch, cortex,
        form={"message": "hi", "client_run_id": run_id, "attachment_ids": "x"},
    )

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    kinds = [s["kind"] for s in store.spans]
    assert "attachment" in kinds
    att_spans = [s for s in store.spans if s["kind"] == "attachment"]
    assert any(s["text"] == "a.txt" for s in att_spans)
    # The user message still lands as an `input` span (multi-turn) — additive.
    assert "input" in kinds


@pytest.mark.asyncio
async def test_chat_cleans_sandbox_on_terminal(monkeypatch, sandbox):
    cortex = FakeCortex()
    store = FakeStore()
    run_id = "33333333-3333-4333-8333-333333333333"
    attachments.receive_upload(run_id, "tmp.txt", _b64(b"bye"), "text/plain")
    run_dir = attachments._run_dir(run_id)
    assert run_dir.exists()

    _install_common(
        monkeypatch, cortex,
        form={"message": "hi", "client_run_id": run_id, "attachment_ids": "x"},
    )

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    # The run reached terminal → its sandbox dir was cleaned.
    assert not run_dir.exists()


class _FakeHarnessPort:
    """A HarnessPort stub for the REMOTE chat path: records the upload_attachment calls
    + the ChatSpawnRequest, and accepts the spawn (the async 'dispatched' shape)."""

    def __init__(self):
        self.uploads: list[tuple[str, str, str]] = []
        self.spawned = None

    async def upload_attachment(self, attachment_id, filename, data_b64):
        self.uploads.append((attachment_id, filename, data_b64))
        return f"/host/att/{attachment_id}/{filename}"

    async def spawn_chat(self, request):
        self.spawned = request
        return type("Handle", (), {"accepted": True, "error": None})()


@pytest.mark.asyncio
async def test_chat_remote_uploads_then_spawns_with_host_paths(monkeypatch, sandbox):
    """REMOTE mode: each uploaded file is forwarded to the host via upload_attachment,
    and the resulting HOST paths are put on the ChatSpawnRequest the host runner gets."""
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    cortex = FakeCortex()
    store = FakeStore()
    port = _FakeHarnessPort()

    run_id = "44444444-4444-4444-8444-444444444444"
    attachments.receive_upload(run_id, "spec.txt", _b64(b"the spec"), "text/plain")

    _install_common(
        monkeypatch, cortex,
        form={"message": "review", "client_run_id": run_id, "attachment_ids": "x"},
    )

    resp = await main_mod.agent_chat(_Req(store, harness_port=port), "kaidera-os", "kai")
    await _drain(resp)

    # The file was forwarded to the host (base64), and the host path is on the spawn req.
    assert len(port.uploads) == 1
    assert port.uploads[0][1] == "spec.txt"
    assert port.spawned is not None
    assert port.spawned.attachment_paths == ["/host/att/" + port.uploads[0][0] + "/spec.txt"]


@pytest.mark.asyncio
async def test_chat_remote_down_upload_degrades_to_no_attachment(monkeypatch, sandbox):
    """A host-upload that returns "" (down) drops that attachment — the spawn still goes
    out (degrade, never crash), just with no host path."""
    monkeypatch.setenv("HARNESS_SPAWN_MODE", "remote")
    cortex = FakeCortex()
    store = FakeStore()

    class _DownPort(_FakeHarnessPort):
        async def upload_attachment(self, attachment_id, filename, data_b64):
            return ""  # host upload down

    port = _DownPort()
    run_id = "55555555-5555-4555-8555-555555555555"
    attachments.receive_upload(run_id, "x.txt", _b64(b"data"), "text/plain")
    _install_common(
        monkeypatch, cortex,
        form={"message": "hi", "client_run_id": run_id, "attachment_ids": "x"},
    )

    resp = await main_mod.agent_chat(_Req(store, harness_port=port), "kaidera-os", "kai")
    await _drain(resp)

    assert port.spawned is not None
    assert port.spawned.attachment_paths == []  # degraded — no host path, but spawned


@pytest.mark.asyncio
async def test_chat_no_attachments_prompt_unchanged(monkeypatch, sandbox):
    """BACKWARD-COMPAT: no attachment_ids → the prompt is byte-for-byte the message
    (single-shot, unchanged) and NO attachment span is written."""
    cortex = FakeCortex()
    store = FakeStore()

    seen: dict = {}

    async def _spy_stream(msg, *, model=None, system=None, harness=None, reasoning=None, **kw):
        seen["prompt"] = msg
        for ev in _events_ok():
            yield ev

    _install_common(monkeypatch, cortex, form={"message": "just a message"})
    monkeypatch.setattr(main_mod.harness_runner, "stream_chat", _spy_stream)

    resp = await main_mod.agent_chat(_Req(store), "kaidera-os", "kai")
    await _drain(resp)

    # No history + no attachments → the prompt is exactly the message (unchanged).
    assert seen["prompt"] == "just a message"
    assert all(s["kind"] != "attachment" for s in store.spans)
