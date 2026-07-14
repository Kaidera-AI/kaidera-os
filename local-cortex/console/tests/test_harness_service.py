"""Harness-service Increment 2 — tests for the host harness-service (POST /spawn etc).

`app/harness_service.py` is the STANDALONE host-resident FastAPI app the container's
`RemoteHarnessAdapter` calls across the host boundary. It owns the harness CLIs +
their interactive OAuth login (which live on the host, not in the container) and
spawns the `run-agent` worker as its OWN detached OS process. Endpoints:
  * POST /spawn         — bearer-authed; validates the body; `subprocess.Popen(
                          [RUN_AGENT_SCRIPT, agent, handoff_id, project, run_id],
                          start_new_session=True, …)`; registers the proc; 202.
  * POST /cancel/{id}   — SIGTERM the registered proc (best-effort); 200 {cancelled}.
  * GET  /health        — 200 {"ok": true} (the I3 container reachability probe).

These tests drive the real app via an in-process httpx `ASGITransport` (no socket,
no uvicorn) with an INJECTED fake `popen` (so NOTHING real is spawned) and assert:
  * a valid token + body → 202 and popen called with the 5-arg argv +
    start_new_session=True,
  * a wrong / missing token (when a token IS set) → 401,
  * a body missing run_id → 422,
  * an OSError on spawn → 500 {"accepted": false, "error": …},
  * POST /cancel/{known} → 200 {"cancelled": true} + SIGTERM sent to that proc,
  * POST /cancel/{unknown} → 200 {"cancelled": false} (NOT a 404),
  * GET /health → 200 {"ok": true},
  * a blank token disables auth (a startup WARNING is logged) — /spawn works
    without an Authorization header.

Increment 4 ADDS `POST /chat` — the HOST seam for INTERACTIVE chat (so a containerized
console can run a chat turn on the host, which has the CLIs). It MIRRORS /spawn exactly
(bearer-gated, validation, OSError→500, registry+reaper) but spawns the CHAT runner
(`scripts/run-chat`) with the chat argv `[run_chat_script, agent, project, run_id,
message]`. These tests assert:
  * a valid token + chat body → 202 and popen called with the chat-runner argv +
    start_new_session=True,
  * a wrong / missing token → 401, a body missing run_id (or message) → 422,
  * an OSError on spawn → 500 {"accepted": false, "error": …}.
"""

from __future__ import annotations

import signal
import os

import httpx
import pytest

TOKEN = "svc-secret"  # fitness:allow-literal test fixture, not a real secret
SCRIPT = "/fake/run-agent"
CHAT_SCRIPT = "/fake/run-chat"
EXPLAIN_SCRIPT = "/fake/run-explain"


class _FakeProc:
    """Stand-in for subprocess.Popen — records the argv + kwargs, simulates a still-
    running child (poll() → None) and records SIGTERM. A class flag makes construct
    raise OSError (the script-missing path)."""

    raise_oserror: bool = False

    def __init__(self, argv, **kwargs):
        if _FakeProc.raise_oserror:
            raise OSError("No such file or directory: 'run-agent'")
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.returncode = None
        self.signals: list[int] = []
        self.terminated = False

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.terminated = True
        self.signals.append(signal.SIGTERM)

    def kill(self):
        self.returncode = -9


class _FakePopenFactory:
    """A callable `popen` that constructs _FakeProc and remembers the last one (so a
    test can assert the argv/kwargs and inspect SIGTERM after a /cancel)."""

    def __init__(self):
        self.last: _FakeProc | None = None
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        proc = _FakeProc(argv, **kwargs)
        self.last = proc
        return proc


def _make_app(*, token=TOKEN, popen=None, script=SCRIPT, chat_script=CHAT_SCRIPT,
              explain_script=EXPLAIN_SCRIPT):
    """Build the harness-service ASGI app with an injected fake popen + a fixed
    token/script — no real CLIs, no OAuth, no detached process."""
    from app.harness_service import create_app

    popen = popen or _FakePopenFactory()
    app = create_app(
        token=token, popen=popen,
        run_agent_script=script, run_chat_script=chat_script,
        run_explain_script=explain_script,
    )
    return app, popen


def _explain_body(**overrides) -> dict:
    base = dict(
        run_id="erun-1", project="proj-x", agent="kai",
        kind="file", repo="/abs/repo", path="mod.py",
    )
    base.update(overrides)
    return base


def _chat_body(**overrides) -> dict:
    base = dict(run_id="crun-1", project="proj-x", agent="kai", message="hello there")
    base.update(overrides)
    return base


def _client(app) -> httpx.AsyncClient:
    """An in-process httpx client over the ASGI app (no socket / uvicorn)."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc.test")


def _auth(token=TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _body(**overrides) -> dict:
    base = dict(run_id="run-1", project="proj-x", agent="worker-a", handoff_id="h-123")
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_fakeproc():
    _FakeProc.raise_oserror = False
    yield
    _FakeProc.raise_oserror = False


@pytest.mark.asyncio
async def test_health_ok():
    app, _ = _make_app()
    async with _client(app) as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_pi_models_endpoint_returns_host_catalog(monkeypatch):
    from app import pi_catalog

    async def fake_groups():
        return [
            {
                "provider": "openai-codex",
                "label": "OpenAI Codex",
                "rows": [{"id": "gpt-5.5", "display_name": "GPT-5.5", "type": "chat"}],
            }
        ]

    monkeypatch.setattr(pi_catalog, "list_pi_model_groups", fake_groups)
    app, _ = _make_app()

    async with _client(app) as c:
        resp = await c.get("/models/pi", headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["groups"][0]["provider"] == "openai-codex"


@pytest.mark.asyncio
async def test_pi_models_endpoint_requires_auth():
    app, popen = _make_app()

    async with _client(app) as c:
        resp = await c.get("/models/pi")

    assert resp.status_code == 401
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_spawn_valid_token_and_body_returns_202_and_spawns_detached(tmp_path):
    app, popen = _make_app()
    repo_root = str(tmp_path)
    async with _client(app) as c:
        resp = await c.post("/spawn", headers=_auth(), json=_body(repo_root=repo_root))

    assert resp.status_code == 202
    data = resp.json()
    assert data["run_id"] == "run-1"
    assert data["accepted"] is True
    # popen was called with the EXACT 5-arg argv, detached into its own session.
    assert popen.calls == 1
    proc = popen.last
    assert proc is not None
    assert proc.argv == [SCRIPT, "worker-a", "h-123", "proj-x", "run-1"]
    assert proc.kwargs.get("start_new_session") is True
    assert proc.kwargs.get("cwd") == repo_root
    env = proc.kwargs.get("env") or {}
    assert env.get("CORTEX_PROJECT") == "proj-x"
    assert env.get("KAIDERA_AGENT_WORKSPACE") == repo_root
    assert env.get("PATH", "").startswith(os.path.join(repo_root, ".agents", "scripts"))


@pytest.mark.asyncio
async def test_spawn_wrong_token_is_401():
    app, popen = _make_app()
    async with _client(app) as c:
        resp = await c.post("/spawn", headers=_auth("nope"), json=_body())
    assert resp.status_code == 401
    # No spawn happened on an auth failure.
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_spawn_missing_token_is_401_when_token_set():
    app, popen = _make_app()
    async with _client(app) as c:
        resp = await c.post("/spawn", json=_body())  # no Authorization header
    assert resp.status_code == 401
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_spawn_missing_run_id_is_422():
    app, popen = _make_app()
    body = _body()
    body.pop("run_id")
    async with _client(app) as c:
        resp = await c.post("/spawn", headers=_auth(), json=body)
    assert resp.status_code == 422
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_spawn_missing_handoff_id_is_422():
    app, popen = _make_app()
    body = _body()
    body.pop("handoff_id")
    async with _client(app) as c:
        resp = await c.post("/spawn", headers=_auth(), json=body)
    assert resp.status_code == 422
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_spawn_oserror_is_500():
    app, popen = _make_app()
    _FakeProc.raise_oserror = True
    async with _client(app) as c:
        resp = await c.post("/spawn", headers=_auth(), json=_body())
    assert resp.status_code == 500
    data = resp.json()
    assert data.get("accepted") is False
    assert data.get("error")


@pytest.mark.asyncio
async def test_cancel_known_run_sigterms_and_returns_true():
    app, popen = _make_app()
    async with _client(app) as c:
        # First spawn so there is a live proc to cancel.
        await c.post("/spawn", headers=_auth(), json=_body(run_id="run-cancel"))
        proc = popen.last
        resp = await c.post("/cancel/run-cancel", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}
    # The registered proc got SIGTERM (best-effort terminate).
    assert proc is not None
    assert signal.SIGTERM in proc.signals


@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_false_not_404():
    app, _ = _make_app()
    async with _client(app) as c:
        resp = await c.post("/cancel/does-not-exist", headers=_auth())
    # Unknown id is a clean {"cancelled": false}, NOT a 404.
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": False}


@pytest.mark.asyncio
async def test_cancel_wrong_token_is_401():
    app, popen = _make_app()
    async with _client(app) as c:
        await c.post("/spawn", headers=_auth(), json=_body(run_id="run-c2"))
        resp = await c.post("/cancel/run-c2", headers=_auth("nope"))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_blank_token_disables_auth():
    """A blank HARNESS_SERVICE_TOKEN ⇒ auth disabled: /spawn works WITHOUT an
    Authorization header (a startup WARNING is logged elsewhere)."""
    app, popen = _make_app(token="")
    async with _client(app) as c:
        resp = await c.post("/spawn", json=_body())  # no auth header
    assert resp.status_code == 202
    assert popen.calls == 1


# ---------------------------------------------------------------------------
#  POST /chat (Increment 4) — the HOST seam for INTERACTIVE chat. Mirrors /spawn
#  exactly, but spawns the CHAT runner with the chat argv
#  [run_chat_script, agent, project, run_id, message].
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_valid_token_and_body_returns_202_and_spawns_detached(tmp_path):
    app, popen = _make_app()
    repo_root = str(tmp_path)
    async with _client(app) as c:
        resp = await c.post("/chat", headers=_auth(), json=_chat_body(repo_root=repo_root))

    assert resp.status_code == 202
    data = resp.json()
    assert data["run_id"] == "crun-1"
    assert data["accepted"] is True
    # popen was called with the CHAT argv (script, agent, project, run_id, message),
    # detached into its own session.
    assert popen.calls == 1
    proc = popen.last
    assert proc is not None
    assert proc.argv == [CHAT_SCRIPT, "kai", "proj-x", "crun-1", "hello there"]
    assert proc.kwargs.get("start_new_session") is True
    assert proc.kwargs.get("cwd") == repo_root
    env = proc.kwargs.get("env") or {}
    assert env.get("CORTEX_PROJECT") == "proj-x"
    assert env.get("KAIDERA_AGENT_WORKSPACE") == repo_root
    # No session_id in the body → no --session-id flag leaks into the argv.
    assert "--session-id" not in proc.argv


@pytest.mark.asyncio
async def test_chat_with_session_id_inserts_session_flag_in_argv():
    """Multi-turn chat (Inc B): a session_id in the body inserts `--session-id <id>`
    into the chat-runner argv (BEFORE the message, which the runner joins from the
    remaining argv) so the host runner threads the conversation."""
    app, popen = _make_app()
    async with _client(app) as c:
        resp = await c.post("/chat", headers=_auth(), json=_chat_body(session_id="sess-77"))

    assert resp.status_code == 202
    proc = popen.last
    assert proc is not None
    # argv: [script, agent, project, run_id, --session-id, <id>, message]. The flag +
    # value sit before the message so run-chat's argv parser consumes them and the
    # remaining argv is the message.
    assert proc.argv == [
        CHAT_SCRIPT, "kai", "proj-x", "crun-1", "--session-id", "sess-77", "hello there",
    ]


@pytest.mark.asyncio
async def test_chat_wrong_token_is_401():
    app, popen = _make_app()
    async with _client(app) as c:
        resp = await c.post("/chat", headers=_auth("nope"), json=_chat_body())
    assert resp.status_code == 401
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_chat_missing_token_is_401_when_token_set():
    app, popen = _make_app()
    async with _client(app) as c:
        resp = await c.post("/chat", json=_chat_body())  # no Authorization header
    assert resp.status_code == 401
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_chat_missing_run_id_is_422():
    app, popen = _make_app()
    body = _chat_body()
    body.pop("run_id")
    async with _client(app) as c:
        resp = await c.post("/chat", headers=_auth(), json=body)
    assert resp.status_code == 422
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_chat_missing_message_is_422():
    app, popen = _make_app()
    body = _chat_body()
    body.pop("message")
    async with _client(app) as c:
        resp = await c.post("/chat", headers=_auth(), json=body)
    assert resp.status_code == 422
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_chat_oserror_is_500():
    app, popen = _make_app()
    _FakeProc.raise_oserror = True
    async with _client(app) as c:
        resp = await c.post("/chat", headers=_auth(), json=_chat_body())
    assert resp.status_code == 500
    data = resp.json()
    assert data.get("accepted") is False
    assert data.get("error")


@pytest.mark.asyncio
async def test_chat_blank_token_disables_auth():
    """A blank token ⇒ /chat works WITHOUT an Authorization header (mirrors /spawn)."""
    app, popen = _make_app(token="")
    async with _client(app) as c:
        resp = await c.post("/chat", json=_chat_body())  # no auth header
    assert resp.status_code == 202
    assert popen.calls == 1


@pytest.mark.asyncio
async def test_chat_run_is_registered_and_cancellable():
    """A /chat spawn is registered in the SAME proc registry as /spawn, so /cancel
    SIGTERMs it (the reaper + cancel cover chat runs identically)."""
    app, popen = _make_app()
    async with _client(app) as c:
        await c.post("/chat", headers=_auth(), json=_chat_body(run_id="crun-cancel"))
        proc = popen.last
        resp = await c.post("/cancel/crun-cancel", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}
    assert proc is not None
    assert signal.SIGTERM in proc.signals


# ---------------------------------------------------------------------------
#  CHAT FILE-ATTACHMENTS (feature-gap step 6, Inc A) — the host `/upload` seam +
#  the `/chat` argv carrying `--attachment-paths`.
#
#  The container's RemoteHarnessAdapter forwards an uploaded file's bytes to the
#  HOST (the host has the disk the chat runner reads); the host writes it under
#  HARNESS_ATTACHMENT_DIR (same `_is_within` gate) and returns {host_path}. /chat
#  then carries the resolved host paths as `--attachment-paths a,b`.
# ---------------------------------------------------------------------------

import base64


def _upload_body(**overrides) -> dict:
    base = dict(
        attachment_id="att-1",
        filename="notes.txt",
        data=base64.b64encode(b"hello attachment").decode("ascii"),
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_upload_writes_host_side_returns_host_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ATTACHMENT_DIR", str(tmp_path / "host-attach"))
    app, _ = _make_app()
    async with _client(app) as c:
        resp = await c.post("/upload", headers=_auth(), json=_upload_body())
    assert resp.status_code == 200
    out = resp.json()
    host_path = out["host_path"]
    # The bytes really landed at the returned host path, under the attachment dir.
    from pathlib import Path
    p = Path(host_path)
    assert p.read_bytes() == b"hello attachment"
    assert str(tmp_path) in host_path


@pytest.mark.asyncio
async def test_upload_bad_token_is_401(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ATTACHMENT_DIR", str(tmp_path / "host-attach"))
    app, _ = _make_app()
    async with _client(app) as c:
        resp = await c.post("/upload", headers=_auth("wrong"), json=_upload_body())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_upload_escaping_filename_is_400_or_403(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ATTACHMENT_DIR", str(tmp_path / "host-attach"))
    app, _ = _make_app()
    async with _client(app) as c:
        resp = await c.post(
            "/upload", headers=_auth(), json=_upload_body(filename="../escape.txt")
        )
    assert resp.status_code in (400, 403)


@pytest.mark.asyncio
async def test_upload_bad_base64_is_400(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ATTACHMENT_DIR", str(tmp_path / "host-attach"))
    app, _ = _make_app()
    async with _client(app) as c:
        resp = await c.post(
            "/upload", headers=_auth(), json=_upload_body(data="!!!notb64!!!")
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_chat_argv_carries_attachment_paths(tmp_path):
    """When the ChatBody carries attachment_paths, /chat inserts
    `--attachment-paths a,b` before the message in the spawned argv."""
    app, popen = _make_app()
    body = _chat_body(attachment_paths=["/host/a.txt", "/host/b.txt"])
    async with _client(app) as c:
        resp = await c.post("/chat", headers=_auth(), json=body)
    assert resp.status_code == 202
    argv = popen.last.argv
    assert "--attachment-paths" in argv
    i = argv.index("--attachment-paths")
    assert argv[i + 1] == "/host/a.txt,/host/b.txt"
    # The message is still the LAST argv element (after the flag + value).
    assert argv[-1] == "hello there"


@pytest.mark.asyncio
async def test_chat_argv_no_attachments_unchanged(tmp_path):
    """No attachment_paths → the argv has no --attachment-paths flag (back-compat)."""
    app, popen = _make_app()
    async with _client(app) as c:
        resp = await c.post("/chat", headers=_auth(), json=_chat_body())
    assert resp.status_code == 202
    assert "--attachment-paths" not in popen.last.argv


# ---------------------------------------------------------------------------
#  POST /explain (Explain capability) — the HOST seam for visual-explainer
#  generation. Mirrors /chat (bearer-gated, registry+reaper, OSError→500) but spawns
#  the EXPLAIN runner (scripts/run-explain) and validates the repo is absolute.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_valid_body_returns_202_and_spawns_detached():
    app, popen = _make_app()
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth(), json=_explain_body())
    assert resp.status_code == 202
    data = resp.json()
    assert data["run_id"] == "erun-1"
    assert data["accepted"] is True
    proc = popen.last
    assert proc.argv == [
        EXPLAIN_SCRIPT, "kai", "proj-x", "erun-1",
        "--kind", "file", "--repo", "/abs/repo", "--path", "mod.py",
    ]
    assert proc.kwargs.get("start_new_session") is True


@pytest.mark.asyncio
async def test_explain_blast_argv_carries_fn():
    app, popen = _make_app()
    body = _explain_body(kind="blast", path=None, fn_name="do_thing")
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth(), json=body)
    assert resp.status_code == 202
    argv = popen.last.argv
    assert "--fn" in argv and argv[argv.index("--fn") + 1] == "do_thing"
    assert "--kind" in argv and argv[argv.index("--kind") + 1] == "blast"


@pytest.mark.asyncio
async def test_explain_project_argv_has_no_required_path():
    app, popen = _make_app()
    body = _explain_body(kind="project", path=None)
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth(), json=body)
    assert resp.status_code == 202
    argv = popen.last.argv
    assert "--kind" in argv and argv[argv.index("--kind") + 1] == "project"
    assert "--path" not in argv


@pytest.mark.asyncio
async def test_explain_diff_argv_carries_git_rev_and_routing():
    app, popen = _make_app()
    body = _explain_body(kind="diff", path=None, git_rev="abc123",
                         harness="pi", model="gpt")
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth(), json=body)
    assert resp.status_code == 202
    argv = popen.last.argv
    assert argv[argv.index("--git-rev") + 1] == "abc123"
    assert argv[argv.index("--harness") + 1] == "pi"
    assert argv[argv.index("--model") + 1] == "gpt"


@pytest.mark.asyncio
async def test_explain_non_absolute_repo_is_400():
    app, popen = _make_app()
    body = _explain_body(repo="relative/repo")  # not absolute
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth(), json=body)
    assert resp.status_code == 400
    assert resp.json().get("accepted") is False
    assert popen.calls == 0  # nothing spawned


@pytest.mark.asyncio
async def test_explain_blank_run_id_is_422():
    app, popen = _make_app()
    body = _explain_body(run_id="")  # present-but-blank
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth(), json=body)
    assert resp.status_code == 422
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_explain_missing_kind_is_422():
    app, popen = _make_app()
    body = _explain_body()
    body.pop("kind")
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth(), json=body)
    assert resp.status_code == 422
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_explain_wrong_token_is_401():
    app, popen = _make_app()
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth("nope"), json=_explain_body())
    assert resp.status_code == 401
    assert popen.calls == 0


@pytest.mark.asyncio
async def test_explain_oserror_is_500():
    app, popen = _make_app()
    _FakeProc.raise_oserror = True
    async with _client(app) as c:
        resp = await c.post("/explain", headers=_auth(), json=_explain_body())
    assert resp.status_code == 500
    assert resp.json().get("accepted") is False


@pytest.mark.asyncio
async def test_explain_run_is_registered_and_cancellable():
    """An /explain spawn is registered in the SAME proc registry as /spawn + /chat, so
    /cancel terminates it (covers explain runs too)."""
    app, popen = _make_app()
    async with _client(app) as c:
        await c.post("/explain", headers=_auth(), json=_explain_body(run_id="erun-cancel"))
        proc = popen.last
        resp = await c.post("/cancel/erun-cancel", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}
    assert signal.SIGTERM in proc.signals
