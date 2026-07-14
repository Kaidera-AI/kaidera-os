"""Harness-service (Increment 2) — the HOST-resident worker-spawn service.

A STANDALONE FastAPI app the container's `RemoteHarnessAdapter`
(`app/adapters/harness_remote.py`) calls across the host boundary. It is deliberately
NOT mounted in `app/main.py` and NOT added to the container image: it runs on the
HOST, where the harness CLIs (`claude-code` / `pi`) and their interactive OAuth login
live. The autonomous loop runs in a container; the spawn it needs must happen on the
host — so the container POSTs here and this service shells `run-agent` as its OWN
detached OS process (identical argv to the in-process I1 spawn).

THE WIRE (mirrors `RemoteHarnessAdapter` + the `SpawnRequest`/`SpawnHandle` DTOs):
  * `POST /spawn`        — bearer-authed; body = the serialized `SpawnRequest`
                           (`run_id`/`project`/`agent`/`handoff_id` required; routing
                           optional). Spawns `[RUN_AGENT_SCRIPT, agent, handoff_id,
                           project, run_id]` detached; 202 `{run_id, accepted:true}`.
                           An `OSError` on spawn → 500 `{accepted:false, error}`.
  * `POST /chat`         — bearer-authed; body = the serialized `ChatSpawnRequest`
                           (`run_id`/`project`/`agent`/`message` required; routing
                           optional). The INTERACTIVE-chat host seam (harness-service
                           I4): spawns `[RUN_CHAT_SCRIPT, agent, project, run_id,
                           message]` detached so the chat turn runs on the HOST (which
                           has the CLIs); the chat runner writes the reply to the
                           run-state row the console pre-created (the UI reads
                           `/runstate/stream`). 202 `{run_id, accepted:true}`; an
                           `OSError` → 500 `{accepted:false, error}`. Mirrors /spawn.
  * `POST /cancel/{id}`  — SIGTERM the registered proc (best-effort), drop it from the
                           registry; 200 `{cancelled: bool}` (unknown id → false, NOT
                           404 — cancel is idempotent/best-effort). Covers chat runs too.
  * `GET  /health`       — 200 `{"ok": true}` (the I3 container reachability probe).

AUTH: a shared bearer token (`HARNESS_SERVICE_TOKEN`). A BLANK token DISABLES auth and
logs a startup WARNING (a loopback service is still authed by default — blank is an
explicit, noisy opt-out). The compare is constant-time (`secrets.compare_digest`).

SECURITY: binds LOOPBACK ONLY (`127.0.0.1`) under `__main__`; the container reaches it
via the docker host-gateway (I3 wiring), never the public network. argv is a LIST (no
shell → no injection surface), exactly like the I1 local adapter.

HOUSEKEEPING: a background reap task polls the registry every ~10s and drops finished
procs (so the dict can't grow unbounded across a long-lived host service). It is pure
housekeeping — it does NOT touch run-state (the worker owns its terminal state via the
run-state store the orchestrator reads).

TESTABILITY: `create_app(token=…, popen=…, run_agent_script=…)` injects a fake `popen`
+ a fixed token/script, so the wire + spawn contract is asserted in-process (httpx
`ASGITransport`) with NOTHING real spawned and NO OAuth. The module-level `app =
create_app()` is the production default (env-resolved).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import secrets
import shutil
import subprocess
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Callable, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("harness-service")

# How often the background reaper sweeps the registry for finished procs (seconds).
_REAP_INTERVAL_S = 10.0

# CHAT FILE-ATTACHMENTS (feature-gap step 6, Inc A) — the HOST attachment store.
# The container's RemoteHarnessAdapter forwards an uploaded file's bytes here (the host
# has the disk the chat runner reads); we write it under HARNESS_ATTACHMENT_DIR (default
# ~/.harness-attachments/) keyed by the attachment_id, and return the host path the /chat
# argv then carries as `--attachment-paths`. Caps + the sandbox gate are SELF-CONTAINED
# here (the service is host-resident + standalone) and config-as-data (env-overridable).
_ATTACH_MAX_FILE_BYTES = int(os.environ.get("HARNESS_ATTACH_MAX_FILE_BYTES", str(2 * 1024 * 1024)))
# How long a host attachment subdir lives before the startup sweep removes it (24h).
_ATTACH_MAX_AGE_S = float(os.environ.get("HARNESS_ATTACH_MAX_AGE_S", str(24 * 3600)))

# WORKER STDERR LOGFILES (run-state visibility fix) — the host worker's stderr goes to a
# per-run logfile here (NOT subprocess.DEVNULL): a real file fd is drained by the OS, so
# there's no 64 KB PIPE deadlock, AND the worker's errors (e.g. an app-DB connect failure)
# are finally diagnosable. Caps + the startup sweep mirror the attachment store.
_RUN_MAX_AGE_S = float(os.environ.get("HARNESS_RUNS_MAX_AGE_S", str(24 * 3600)))


def _runs_dir() -> Path:
    """The HOST run-log store root — ``HARNESS_RUNS_DIR`` if set, else ``~/.harness-runs``.
    Read at call time so the env can point it at a temp dir for tests."""
    env = os.environ.get("HARNESS_RUNS_DIR", "").strip()
    return Path(env).expanduser() if env else (Path.home() / ".harness-runs")


def _safe_run_token(run_id: str) -> str:
    """A filesystem-safe basename for a run's logfile (run_ids are uuid4s, but be defensive
    against a hand-crafted id: strip path separators / NUL, keep it a single component)."""
    rid = (run_id or "").strip().replace("\x00", "")
    rid = rid.replace("/", "_").replace("\\", "_")
    rid = rid.strip(". ") or "run"
    return rid[:128]


def _open_run_stderr(run_id: str) -> Any:
    """Open (append) the per-run stderr logfile under the runs dir and return the file
    object to hand to Popen(stderr=…). The OS writes the worker's stderr straight to this
    fd — nothing to drain, so no PIPE deadlock (strictly better than DEVNULL). On ANY
    failure (un-writable dir, etc.) fall back to DEVNULL so a spawn is never blocked by a
    logging problem."""
    try:
        d = _runs_dir()
        d.mkdir(parents=True, exist_ok=True)
        return open(d / f"{_safe_run_token(run_id)}.stderr", "ab", buffering=0)
    except OSError as exc:  # pragma: no cover - defensive: logging must never block a spawn
        log.warning("could not open per-run stderr logfile (falling back to DEVNULL): %s", exc)
        return subprocess.DEVNULL


def _sweep_run_logs() -> None:
    """Best-effort delete of per-run stderr logfiles older than ``_RUN_MAX_AGE_S`` (a
    startup sweep, mirroring the attachment sweep). NEVER raises — pure housekeeping."""
    root = _runs_dir()
    if not root.is_dir():
        return
    cutoff = time.time() - _RUN_MAX_AGE_S
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        with suppress(OSError):
            if child.is_file() and child.stat().st_mtime < cutoff:
                child.unlink()


def _usable_repo_root(repo_root: Optional[str]) -> str | None:
    """Return a mounted project root or None for the safe legacy cwd fallback."""
    root = (repo_root or "").strip()
    return root if root and os.path.isdir(root) else None


def _worker_env(project: Optional[str] = None, repo_root: Optional[str] = None) -> dict:
    """The environment for a spawned worker: a COPY of the service env with
    ``HARNESS_APPDB_DSN`` FORCED to the HOST DSN (``appdb.host_appdb_dsn()``), then
    scoped to the selected project when project/repo metadata is available.

    THE FIX: the worker runs on the HOST but inherits THIS service's env. If the service
    was launched from a shell carrying the in-CONTAINER DSN (``harness-appdb:5432``), the
    worker would inherit it and every run-state write would silently no-op (the host can't
    resolve that hostname) — the run then sticks at ``queued`` with no pid/spans and the
    console can't SHOW it. Forcing the host DSN here makes the worker's run-state writes
    LAND regardless of what the service inherited. Project scoping prevents a host
    service launched from the Kaidera OS repo from leaking that cwd/scope into a customer
    project worker. All other vars pass through unchanged."""
    env = dict(os.environ)
    try:
        from app.appdb import host_appdb_dsn

        env["HARNESS_APPDB_DSN"] = host_appdb_dsn()
    except Exception:  # pragma: no cover - defensive: a resolver hiccup must not block spawn
        env.setdefault(
            "HARNESS_APPDB_DSN", "postgresql://harness:harness@localhost:5500/harness_app"
        )
    with suppress(Exception):
        from app.harness_runner import _apply_project_workspace

        env = _apply_project_workspace(env, project, repo_root)
    return env


def _attachment_dir() -> Path:
    """The HOST attachment store root — `HARNESS_ATTACHMENT_DIR` if set, else
    `~/.harness-attachments`. Read at call time so the env can point it at a temp dir for
    tests."""
    env = os.environ.get("HARNESS_ATTACHMENT_DIR", "").strip()
    base = Path(env).expanduser() if env else (Path.home() / ".harness-attachments")
    return base


def _attach_is_within(path: Path, base_dir: Path) -> bool:
    """True if `path` is `base_dir` or a descendant (both resolved). A LOCAL COPY of the
    workspace/attachments confinement gate — the host service is standalone, so it carries
    its own copy rather than importing one."""
    if path == base_dir:
        return True
    try:
        return path.is_relative_to(base_dir)  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - very old Python
        try:
            path.relative_to(base_dir)
            return True
        except ValueError:
            return False


def _attach_safe_target(attachment_id: str, filename: str) -> Path:
    """Resolve `<HARNESS_ATTACHMENT_DIR>/<attachment_id>/<basename(filename)>` and confirm
    it does not escape the attachment store. Rejects a NUL/`..`/absolute input + an
    escaping attachment_id BEFORE any write (raises ValueError on an escape — the route
    maps it to a 400/403). Mirrors `attachments.safe_attachment_path`."""
    aid = (attachment_id or "").strip()
    if not aid or "\x00" in aid or "/" in aid or "\\" in aid or aid in (".", ".."):
        raise ValueError("invalid attachment id")
    name = (filename or "").strip().replace("\\", "/")
    if "\x00" in name:
        raise ValueError("invalid filename")
    if name.startswith("/") or ".." in name.split("/"):
        raise ValueError("attachment path escapes store")
    base = os.path.basename(name)
    if not base or base in (".", ".."):
        raise ValueError("invalid filename")
    store_root = _attachment_dir().resolve()
    final = (store_root / aid / base).resolve(strict=False)
    if not _attach_is_within(final, store_root):
        raise ValueError("attachment path escapes store")
    return final


def _sweep_host_attachments() -> None:
    """Best-effort delete of host attachment subdirs older than `_ATTACH_MAX_AGE_S` (a
    startup sweep, mirroring the console's). NEVER raises — pure housekeeping."""
    root = _attachment_dir()
    if not root.is_dir():
        return
    cutoff = time.time() - _ATTACH_MAX_AGE_S
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        with suppress(OSError):
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)


def _default_run_agent_script() -> str:
    """Resolve the `run-agent` script as an ABSOLUTE path — the SAME derivation the
    orchestrator uses (this file lives at console/app/, the script at
    console/scripts/run-agent, so it's ../scripts/run-agent), env-overridable with
    `ORCH_RUN_AGENT`. Never a hardcoded personal path (the no-project-literals gate)."""
    return os.environ.get(
        "ORCH_RUN_AGENT",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "run-agent",
        ),
    )


def _default_run_chat_script() -> str:
    """Resolve the `run-chat` script as an ABSOLUTE path (the chat twin of
    `_default_run_agent_script`): this file lives at console/app/, the script at
    console/scripts/run-chat, so it's ../scripts/run-chat — env-overridable with
    `ORCH_RUN_CHAT`. Never a hardcoded personal path (the no-project-literals gate)."""
    return os.environ.get(
        "ORCH_RUN_CHAT",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "run-chat",
        ),
    )


def _default_run_explain_script() -> str:
    """Resolve the `run-explain` script as an ABSOLUTE path (the Explain twin of
    `_default_run_chat_script`): this file lives at console/app/, the script at
    console/scripts/run-explain, so it's ../scripts/run-explain — env-overridable with
    `ORCH_RUN_EXPLAIN`. Never a hardcoded personal path (the no-project-literals gate)."""
    return os.environ.get(
        "ORCH_RUN_EXPLAIN",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "run-explain",
        ),
    )


class SpawnBody(BaseModel):
    """The /spawn request body — the serialized `SpawnRequest`. The four required
    fields mirror the domain DTO; routing (`harness`/`model`) + `run_timeout_s` are
    optional (the worker re-resolves routing today; carried for a future direct
    route). A missing required field yields FastAPI's automatic 422."""

    run_id: str
    project: str
    agent: str
    handoff_id: str
    harness: Optional[str] = None
    model: Optional[str] = None
    repo_root: Optional[str] = None
    run_timeout_s: float = 900.0


class ChatBody(BaseModel):
    """The /chat request body — the serialized `ChatSpawnRequest` (harness-service I4).
    The four required fields mirror the domain DTO; `message` is the operator's text
    and there is NO `handoff_id` (a chat is free-standing). Routing (`harness`/`model`/
    `reasoning`) + `run_timeout_s` are optional. A missing required field → FastAPI's
    automatic 422."""

    run_id: str
    project: str
    agent: str
    message: str
    harness: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    repo_root: Optional[str] = None
    # The per-conversation grouping key (multi-turn chat, Inc B) — forwarded to the chat
    # runner as `--session-id <id>` so the host turn threads the conversation. Optional;
    # None for a single-shot turn (the argv stays the 5-arg form, unchanged).
    session_id: Optional[str] = None
    # The HOST attachment paths for this turn (chat file-attachments, step 6) — forwarded
    # to the chat runner as `--attachment-paths a,b` so the host turn inlines the files.
    # Optional + additive; empty for a turn with no attachments (the argv stays the
    # existing form, unchanged).
    attachment_paths: List[str] = []
    run_timeout_s: float = 900.0


class ExplainBody(BaseModel):
    """The /explain request body — ONE visual-explainer generation (Explain capability).

    The HOST seam for Explain (so a containerized console can generate on the host, which
    has the repo + the harness CLIs + the cortex-graph CLIs). MIRRORS ChatBody's shape:
    `run_id`/`project`/`agent`/`kind` are required (`kind` ∈ project|file|blast|dir|diff); `repo`
    is the project working folder (validated ABSOLUTE by the route); `path`/`fn_name`/
    `git_rev` are the per-kind inputs (optional); routing (`harness`/`model`) is optional.
    A missing required field → FastAPI's automatic 422."""

    run_id: str
    project: str
    agent: str
    kind: str
    repo: str
    path: Optional[str] = None
    fn_name: Optional[str] = None
    git_rev: Optional[str] = None
    harness: Optional[str] = None
    model: Optional[str] = None
    run_timeout_s: float = 180.0


class UploadBody(BaseModel):
    """The /upload request body — ONE chat attachment's bytes (chat file-attachments,
    step 6 Inc A). `attachment_id` keys the per-attachment host subdir; `filename` is the
    sanitized basename; `data` is the base64 bytes (NO multipart — base64-in-JSON, the
    same discipline the console's upload route follows). A missing required field →
    FastAPI's automatic 422."""

    attachment_id: str
    filename: str
    data: str


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Constant-time bearer-token gate. A blank token disables auth (the create_app
    startup logs the WARNING once). `/health` is always open (the reachability probe
    must work without the shared secret). On mismatch → 401 (before routing/validation,
    so a bad token never reaches the handler)."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token or ""

    async def dispatch(self, request: Request, call_next):
        # Health is unauthenticated — it's the container's "are you there" probe.
        if request.url.path == "/health":
            return await call_next(request)
        # Blank token ⇒ auth disabled (explicit, noisy opt-out logged at startup).
        if not self._token:
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        presented = header[len(prefix):] if header.startswith(prefix) else ""
        # Constant-time compare — never short-circuit on the first differing byte.
        if not presented or not secrets.compare_digest(presented, self._token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


def create_app(
    token: Optional[str] = None,
    popen: Callable[..., Any] = subprocess.Popen,
    run_agent_script: Optional[str] = None,
    run_chat_script: Optional[str] = None,
    run_explain_script: Optional[str] = None,
) -> FastAPI:
    """Build the host harness-service ASGI app.

    `token` defaults to `HARNESS_SERVICE_TOKEN` (blank ⇒ auth disabled + a startup
    WARNING). `popen` defaults to the real `subprocess.Popen` (tests inject a fake).
    `run_agent_script` defaults to the orchestrator's resolved absolute path;
    `run_chat_script` defaults to console/scripts/run-chat (the I4 chat host seam);
    `run_explain_script` defaults to console/scripts/run-explain (the Explain host seam)."""
    tok = token if token is not None else os.environ.get("HARNESS_SERVICE_TOKEN", "")
    tok = tok or ""
    script = run_agent_script or _default_run_agent_script()
    chat_script = run_chat_script or _default_run_chat_script()
    explain_script = run_explain_script or _default_run_explain_script()

    # The live-process registry: run_id → the spawned Popen. The reaper drops finished
    # entries; /cancel SIGTERMs + drops. Plain dict — the event loop serializes access
    # (no cross-thread mutation; the reaper runs on the same loop).
    registry: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not tok:
            log.warning(
                "HARNESS_SERVICE_TOKEN is blank — /spawn + /cancel auth is DISABLED. "
                "Set HARNESS_SERVICE_TOKEN to require the shared bearer token."
            )
        # CHAT FILE-ATTACHMENTS (step 6): a best-effort startup sweep of stale host
        # attachment dirs (older than 24h) — the backstop for any chat that didn't reach
        # its own per-run cleanup. Never blocks boot (a missing dir / error is a no-op).
        with suppress(Exception):
            _sweep_host_attachments()
        # Per-run stderr logfiles (the diagnosability store) get the same startup sweep —
        # a best-effort delete of logs older than 24h so a long-lived host service never
        # accumulates them. Never blocks boot (a missing dir / error is a no-op).
        with suppress(Exception):
            _sweep_run_logs()
        # Background reaper: drop finished procs so the registry can't grow unbounded.
        reap_task = asyncio.create_task(_reaper(registry), name="harness-reap")
        try:
            yield
        finally:
            reap_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reap_task

    app = FastAPI(
        title="Kaidera OS Harness Service (host)",
        description="Host-resident worker-spawn service (POST /spawn · /cancel).",
        lifespan=lifespan,
    )
    app.add_middleware(_BearerAuthMiddleware, token=tok)
    # Stash for introspection/tests (not part of the wire).
    app.state.registry = registry
    app.state.run_agent_script = script
    app.state.run_chat_script = chat_script
    app.state.run_explain_script = explain_script

    @app.get("/health")
    async def health() -> dict:
        """Liveness probe — always 200 {"ok": true}, no auth (I3 reachability)."""
        return {"ok": True}

    @app.get("/models/pi")
    async def pi_models() -> dict:
        """Host-side PI model catalog.

        The console container cannot enumerate PI models because the CLI/provider
        auth state lives on the host. This authenticated read-only endpoint returns
        provider-grouped rows parsed from `pi --list-models`, degrading to an empty
        group list when PI is unavailable.
        """
        from app import pi_catalog

        groups = await pi_catalog.list_pi_model_groups()
        return {"groups": groups}

    @app.get("/models/claude")
    async def claude_models() -> dict:
        """Host-side Claude Code aliases and effort levels from CLI help."""
        from app import claude_catalog

        models = await claude_catalog.list_claude_model_options()
        return {"models": models}

    @app.get("/models/codex")
    async def codex_models() -> dict:
        """Host-side Codex models and per-model effort levels from app-server."""
        from app import codex_catalog

        models = await codex_catalog.list_codex_model_options()
        return {"models": models}

    @app.post("/spawn")
    async def spawn(body: SpawnBody) -> JSONResponse:
        """Spawn the worker as its OWN detached OS process; register it by run_id.

        argv is the I1 host-spawn order [script, agent, handoff_id, project, run_id]
        (a LIST → no shell → no injection). On success → 202 {run_id, accepted:true};
        an OSError (script missing / not executable) → 500 {accepted:false, error}."""
        argv = [script, body.agent, body.handoff_id, body.project, body.run_id]
        repo_root = _usable_repo_root(body.repo_root)
        try:
            proc = popen(
                argv,
                cwd=repo_root or None,
                stdout=subprocess.DEVNULL,
                # A per-run logfile fd, NOT DEVNULL/PIPE: a PIPE deadlocks the worker
                # once its stderr exceeds the ~64 KB OS buffer (nothing drains it), and
                # DEVNULL discards the diagnostics. A real file fd is drained by the OS
                # (no deadlock) AND keeps the worker's errors — e.g. an app-DB connect
                # failure — readable at ~/.harness-runs/<run_id>.stderr.
                stderr=_open_run_stderr(body.run_id),
                # Force the worker onto the HOST app-DB DSN so its run-state writes LAND
                # even if this service inherited the in-container DSN (the visibility fix).
                env=_worker_env(body.project, repo_root),
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            # A failure to even start the worker → 500 with the reason (the adapter
            # maps a non-202 to accepted=False; the orchestrator logs an error line).
            return JSONResponse(
                {"run_id": body.run_id, "accepted": False, "error": str(exc)},
                status_code=500,
            )
        registry[body.run_id] = proc
        return JSONResponse(
            {"run_id": body.run_id, "accepted": True}, status_code=202
        )

    @app.post("/chat")
    async def chat(body: ChatBody) -> JSONResponse:
        """Spawn ONE interactive-chat turn as its OWN detached OS process; register it
        by run_id (harness-service I4 — the chat host seam). MIRRORS /spawn exactly.

        argv is the chat order [chat_script, agent, project, run_id, message] (a LIST →
        no shell → no injection). The chat runs on the HOST (which has the CLIs) and
        writes the reply to the run-state row the console pre-created (the UI reads
        /runstate/stream). On success → 202 {run_id, accepted:true}; an OSError (script
        missing / not executable) → 500 {accepted:false, error}.

        Multi-turn chat (Inc B): when the body carries a session_id, `--session-id <id>`
        is inserted BEFORE the message (the chat runner's argv parser consumes the flag
        + value and joins the remaining argv as the message) so the host turn threads
        the conversation. No session_id → the 5-arg form, unchanged (single-shot)."""
        argv = [chat_script, body.agent, body.project, body.run_id]
        repo_root = _usable_repo_root(body.repo_root)
        sess = (body.session_id or "").strip()
        if sess:
            argv += ["--session-id", sess]
        # CHAT FILE-ATTACHMENTS (step 6): pass the resolved HOST paths as a CSV BEFORE the
        # message (the chat runner's argv parser consumes the flag + value and joins the
        # rest as the message), so the host turn inlines the uploaded files. No paths →
        # the flag is omitted (the argv stays the existing form, unchanged).
        att = [p for p in (body.attachment_paths or []) if p]
        if att:
            argv += ["--attachment-paths", ",".join(att)]
        argv.append(body.message)
        try:
            proc = popen(
                argv,
                cwd=repo_root or None,
                stdout=subprocess.DEVNULL,
                # A per-run logfile fd, NOT DEVNULL/PIPE (see /spawn): drained by the OS
                # so no deadlock, and the chat-runner's errors stay diagnosable.
                stderr=_open_run_stderr(body.run_id),
                # Force the HOST app-DB DSN so the chat-runner's run-state writes land.
                env=_worker_env(body.project, repo_root),
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return JSONResponse(
                {"run_id": body.run_id, "accepted": False, "error": str(exc)},
                status_code=500,
            )
        registry[body.run_id] = proc
        return JSONResponse(
            {"run_id": body.run_id, "accepted": True}, status_code=202
        )

    @app.post("/explain")
    async def explain(body: ExplainBody) -> JSONResponse:
        """Spawn ONE visual-explainer generation as its OWN detached OS process; register
        it by run_id (the Explain host seam). MIRRORS /chat exactly (bearer-gated,
        registry + reaper, OSError→500) but spawns the EXPLAIN runner (`scripts/run-explain`).

        Generation runs on the HOST (which has the repo + the harness CLIs + the
        cortex-graph CLIs) and writes spans + terminal status (with the persisted
        artifact_id in run_state.metadata) to the run_state row the console pre-created;
        the SPA reads it via /runstate/stream + the artifact. argv is a LIST → no shell →
        no injection.

        Validation: `repo` MUST be an absolute path (a relative repo can't be resolved
        host-side → 400); a blank `run_id` → 422 (a missing field is already FastAPI's
        422). On success → 202 {run_id, accepted:true}; an OSError (script missing / not
        executable) → 500 {accepted:false, error}."""
        if not (body.run_id or "").strip():
            return JSONResponse({"detail": "run_id is required"}, status_code=422)
        if not (body.repo or "").startswith("/"):
            return JSONResponse(
                {"run_id": body.run_id, "accepted": False,
                 "error": "repo must be an absolute path"},
                status_code=400,
            )
        argv = [
            explain_script, body.agent, body.project, body.run_id,
            "--kind", body.kind, "--repo", body.repo,
        ]
        if (body.path or "").strip():
            argv += ["--path", body.path]
        if (body.fn_name or "").strip():
            argv += ["--fn", body.fn_name]
        if (body.git_rev or "").strip():
            argv += ["--git-rev", body.git_rev]
        if (body.harness or "").strip():
            argv += ["--harness", body.harness]
        if (body.model or "").strip():
            argv += ["--model", body.model]
        try:
            proc = popen(
                argv,
                stdout=subprocess.DEVNULL,
                # A per-run logfile fd, NOT DEVNULL/PIPE (see /spawn): drained by the OS
                # so no deadlock, and the explain-runner's errors stay diagnosable.
                stderr=_open_run_stderr(body.run_id),
                # Force the HOST app-DB DSN so the explain-runner's run-state writes land.
                env=_worker_env(),
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return JSONResponse(
                {"run_id": body.run_id, "accepted": False, "error": str(exc)},
                status_code=500,
            )
        registry[body.run_id] = proc
        return JSONResponse(
            {"run_id": body.run_id, "accepted": True}, status_code=202
        )

    @app.post("/upload")
    async def upload(body: UploadBody) -> JSONResponse:
        """Receive ONE chat attachment's bytes HOST-SIDE (chat file-attachments, step 6
        Inc A — the container→host attachment seam). MIRRORS /chat's auth (bearer-gated).

        The container's RemoteHarnessAdapter forwards a file's base64 bytes here so they
        land on the HOST disk the chat runner reads. We confine + write them under
        `HARNESS_ATTACHMENT_DIR/<attachment_id>/<filename>` (the same `_is_within` gate —
        an escaping filename/id is rejected and NOTHING is written), and return
        `{host_path}` (the path the /chat argv then carries as `--attachment-paths`). An
        escape / a bad-or-oversized body → 400; never a 500 on bad input."""
        try:
            target = _attach_safe_target(body.attachment_id, body.filename)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            raw = base64.b64decode(body.data or "", validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse({"error": "attachment body is not valid base64"}, status_code=400)
        if len(raw) > _ATTACH_MAX_FILE_BYTES:
            return JSONResponse(
                {"error": f"attachment too large ({len(raw)} bytes; max {_ATTACH_MAX_FILE_BYTES})"},
                status_code=400,
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(raw)
        except OSError as exc:
            return JSONResponse({"error": f"cannot write attachment: {exc}"}, status_code=500)
        return JSONResponse({"host_path": str(target)})

    @app.post("/cancel/{run_id}")
    async def cancel(run_id: str) -> dict:
        """Best-effort cancel: SIGTERM the registered proc + drop it. Unknown id →
        {"cancelled": false} (NOT 404 — cancel is idempotent/best-effort)."""
        proc = registry.pop(run_id, None)
        if proc is None:
            return {"cancelled": False}
        # SIGTERM best-effort — the child may already be gone; never raise.
        with suppress(Exception):
            proc.terminate()
        return {"cancelled": True}

    return app


async def _reaper(registry: dict[str, Any]) -> None:
    """Every ~10s, poll each registered proc and drop the finished ones. Pure
    housekeeping (keeps the registry bounded); it does NOT touch run-state — the
    worker owns its terminal status via the run-state store. Never raises out of the
    loop (a flaky poll on one proc must not stop the sweep)."""
    while True:
        await asyncio.sleep(_REAP_INTERVAL_S)
        for rid in list(registry.keys()):
            proc = registry.get(rid)
            if proc is None:
                continue
            with suppress(Exception):
                if proc.poll() is not None:
                    registry.pop(rid, None)


# The production app — env-resolved (real subprocess.Popen, HARNESS_SERVICE_TOKEN,
# the orchestrator's run-agent path). The container never imports this; only the host
# launcher (`scripts/harness-service`) runs it.
app = create_app()


if __name__ == "__main__":
    import uvicorn

    # LOOPBACK bind ONLY — the container reaches this via the docker host-gateway
    # (I3 `extra_hosts`), never the public network.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("HARNESS_SERVICE_PORT", "8766")),
    )
