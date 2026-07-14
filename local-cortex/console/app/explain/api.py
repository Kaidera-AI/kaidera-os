"""Explain API — the imperative shell (FastAPI router) for the Explain capability.

The ONLY part of the explain module that imports fastapi. Four endpoints:

  * `POST /explain/{project}` — START a generation. Resolves the project's repo_root
    (the working folder), validates the target (kind ∈ project|file|blast|dir|diff + the
    per-kind input), mints a `run_id`, opens a `run_state` row (lease_owner='explain',
    so the SPA's `/runstate/stream` follows it), and FORWARDS the spawn to the HOST
    harness-service `POST /explain` via httpx (the container can't read the repo / run
    cortex-graph; the host can) — EXACTLY like the chat remote path. Returns
    `{run_id, accepted}`.

  * `GET /explain/{project}/result/{run_id}` — the persisted artifact for a run. The
    Explain run writes the document to L5 at the deterministic `source_file =
    explain/{run_id}.html`; we look it up via `CortexClient.get_artifact_by_source_file`
    and return `{artifact_id, caption, html, target_kind, target_path, created_at}`.

  * `GET /explain/{project}/list` — the gallery: recent explain runs enumerated from the
    console's OWN `run_state` (`lease_owner='explain'`), NOT Cortex search. Cortex
    artifact search is content-relevance, not prefix enumeration, so it can't reliably
    list explainers; run_state is the authoritative source (every explain run is a row).
    Each item carries `run_id` (first-class) + the `artifact_id`/target/caption from the
    run's `metadata` sidecar; the SPA's "View" loads the run's HTML via `/runs/run/{id}`.
    (L5 stays the CONTENT-recall path — `cortex-graph-search` — untouched.)

  * `GET /explain/{project}/export/{run_id}` — a project-scoped ``.tar.gz`` containing
    the complete generated HTML and an export manifest. It reads the full run transcript,
    so no workspace path or temporary file is involved.

GRACEFUL-DEGRADE rides through from the clients: a down harness-service → accepted=False
(a clean rejection, never a 500 crash); a down/None/raising run-state store → an empty
gallery list; a down/empty Cortex → a 404 on a missing result.

HOST-SERVICE WIRE: the harness-service base URL + bearer token resolve from the SAME env
the `RemoteHarnessAdapter` uses (`HARNESS_SERVICE_HOST`/`HARNESS_SERVICE_PORT`/
`HARNESS_SERVICE_TOKEN`), so the console and the orchestrator's worker-spawn target the
one host service. The httpx client is injectable (a `Depends`) so the wire is tested with
an `httpx.MockTransport` — no live host service.

PATH NOTE (additive, non-colliding): everything lives under the distinct `/explain/...`
prefix, so it can never shadow `/runs/...`, `/runstate/...`, `/dispatch/...`, or
`/agents/...`.

NOTE (documented API gap): the Cortex search surface returns a TRUNCATED preview of an
artifact's `raw_content` (LEFT(.,300)), and there is no full-artifact-by-id read endpoint
yet — so `html` in the result is that preview. A faithful full-HTML render in the
sandboxed iframe needs a `GET /artifacts/{id}` (a follow-up); the run's `output` spans
(via `/runs/run/{run_id}`) carry the full generated document in the meantime.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import uuid
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..explain_run import _validate_html, extract_html_document

router = APIRouter(prefix="/explain", tags=["explain"])

# The kinds the Explain context assembler understands (mirrors explain_context).
_VALID_KINDS = ("project", "file", "blast", "dir", "diff")


def _harness_base_url() -> str:
    """The host harness-service base URL from the env (the SAME resolution the
    RemoteHarnessAdapter uses), so the console forwards to the one host service the
    orchestrator's worker-spawn also targets."""
    host = os.environ.get("HARNESS_SERVICE_HOST", "host.docker.internal")
    port = os.environ.get("HARNESS_SERVICE_PORT", "8766")
    return f"http://{host}:{port}"


def _harness_headers() -> dict[str, str]:
    """Bearer auth header when a token is configured (omitted when blank — the host
    service may have auth disabled; that is its call)."""
    token = (os.environ.get("HARNESS_SERVICE_TOKEN", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _cortex(request: Request):
    """The shared `CortexClient` (the artifact write/read seam) from app.state."""
    return getattr(request.app.state, "cortex", None)


def _runstate(request: Request):
    """The `RunStatePort` SSOT store from app.state (or None when the app-DB is down)."""
    return getattr(request.app.state, "runstate", None)


def _http_client(request: Request) -> Optional[httpx.AsyncClient]:
    """An injectable httpx client for the host-service forward. Tests stash an
    `httpx.AsyncClient` over a MockTransport at `app.state.explain_http`; production
    leaves it unset and we build a short-lived client per request."""
    return getattr(request.app.state, "explain_http", None)


def _run_output(run: Any) -> str:
    """Concatenate output spans from either a RunRecord or a test/dict adapter."""
    chunks: list[str] = []
    for span in getattr(run, "spans", None) or []:
        if isinstance(span, dict):
            kind = span.get("kind") or "output"
            value = span.get("text") or ""
        else:
            kind = getattr(span, "kind", None) or "output"
            value = getattr(span, "text", None) or ""
        if kind == "output":
            chunks.append(str(value))
    return "".join(chunks)


async def _gallery_status(store: Any, run: Any, run_id: str) -> Any:
    """Present a false-negative error with valid HTML as recovered.

    The run header remains immutable. Only errored rows pay for a hydrated transcript
    read, and a failed read keeps the original status.
    """
    status = getattr(run, "status", None)
    if str(status or "").lower() not in {"error", "errored"}:
        return status
    try:
        hydrated = await store.get_run(run_id)
    except Exception:
        return status
    html = extract_html_document(_run_output(hydrated)) if hydrated is not None else ""
    return "recovered" if html and _validate_html(html) is None else status


def _safe_archive_stem(run_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in run_id)
    return (cleaned[:64] or "run").strip("-") or "run"


def _add_tar_file(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    info.mode = 0o644
    info.mtime = 0
    archive.addfile(info, io.BytesIO(content))


@router.post("/{project}")
async def start_explain(project: str, request: Request) -> JSONResponse:
    """Start ONE explain generation for `project`.

    Body (JSON): `{kind, path?, fn_name?, git_rev?, harness?, model?}`. `kind` ∈
    project|file|blast|dir|diff. The repo is the project's repo_root (the working folder Cortex
    records) — NOT client-supplied, so the host reads the right tree.

    The project-bound explainer posts only `{kind:'project'}` — no typing. The explain
    WRITER agent is the project's resolved LEAD (default_agent → designation-driven lead
    via the agents service), and harness/model default to that lead's currently-selected
    routing (`_chat_routing_for`), with body harness/model as an explicit OVERRIDE
    (override-first). When no lead resolves (no default_agent / empty roster / down store),
    it falls back to the console reader + None routing so the explainer still runs.

    Mints a `run_id`, opens a run_state row (lease_owner='explain'), and forwards the
    spawn to the host harness-service. Returns `{run_id, accepted}` (202 on accept, 200
    with accepted=false when the host seam rejects — the SPA shows a clear 'host
    unavailable').

    Validation: an unknown/blank `kind` → 400; a `file`/`dir` kind with no `path`, a
    `blast` with no `fn_name`, or a missing project repo_root → 400."""
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    kind = (str(body.get("kind") or "")).strip().lower()
    if kind not in _VALID_KINDS:
        return JSONResponse(
            {"error": f"kind must be one of {', '.join(_VALID_KINDS)}"}, status_code=400
        )
    path = (str(body.get("path") or "")).strip()
    fn_name = (str(body.get("fn_name") or body.get("fn") or "")).strip()
    git_rev = (str(body.get("git_rev") or "")).strip()
    if kind in ("file", "dir") and not path:
        return JSONResponse({"error": f"kind '{kind}' requires a path"}, status_code=400)
    if kind == "blast" and not fn_name:
        return JSONResponse({"error": "kind 'blast' requires a fn_name"}, status_code=400)

    cortex = _cortex(request)
    # Resolve the project's working folder (repo_root) — the host reads THIS tree.
    repo_root = ""
    # The writer agent: the CortexClient's configured agent (env CORTEX_CONSOLE_AGENT,
    # the registered low-privilege console reader). No bare agent literal — sourced from
    # config so the no-project-literals gate stays green and it's drop-in across projects.
    # This is the FALLBACK writer; the project-bound explainer prefers the resolved LEAD.
    agent = (os.environ.get("CORTEX_CONSOLE_AGENT", "") or "").strip()
    proj: dict[str, Any] | None = None
    if cortex is not None:
        try:
            proj = await cortex.get_project(project)
        except Exception:
            proj = None
        if isinstance(proj, dict):
            # The working-folder field is the Cortex /projects API key (config-as-data),
            # not an agent/project literal — a gate false positive on the field name here.
            repo_root = (proj.get("repo_root") or "").strip()  # fitness:allow-literal Cortex API field name
        agent = agent or (getattr(cortex, "agent", "") or "")

    run_id = str(uuid.uuid4())
    body_harness = (str(body.get("harness") or "")).strip() or None
    body_model = (str(body.get("model") or "")).strip() or None

    # --- PROJECT-BOUND WRITER RESOLUTION (graceful-degrade) ------------------------
    # The explain WRITER agent is the project's resolved LEAD (default_agent →
    # designation-driven lead via the agents service), with the lead's currently-selected
    # harness/model. Body harness/model remain an explicit OVERRIDE (override-first). When
    # NO lead resolves (no default_agent / empty roster / down store), we keep today's
    # console-reader fallback + None routing so the explainer still runs and existing
    # behavior holds. Every new read is wrapped → None (house law). All app.main /
    # app.agents.api imports MUST be LAZY (handler-local): app.main imports app.explain at
    # module load, so a top-level import here would create a startup cycle.
    lead: str | None = None
    roster: list[Any] = []
    if cortex is not None:
        # (a) the explicit default_agent off the already-fetched project dict (no second
        # round-trip).
        if isinstance(proj, dict):
            try:
                lead = (str(proj.get("default_agent") or "")).strip() or None
            except Exception:
                lead = None
        # (b) else resolve the designation-driven lead via the agents service.
        if not lead:
            try:
                from app.agents.api import build_service, get_operational_store

                store = get_operational_store(request)
                roster = await cortex.get_agents(project) or []
                catalog = await build_service(store).list_agents(project, roster)
                lead = (catalog.get("lead") or "").strip() or None
            except Exception:
                lead = None
                roster = []

    # Harness/model default to the lead's resolved routing (override-first: body wins).
    harness = body_harness
    model = body_model
    if lead:
        agent = lead
        # Derive the lead's currently-selected harness/model via the shared routing seam
        # (coerce_model / harness defaults / extension override all baked in). Body values
        # still win. A real failure degrades to the body values (or None) — never raises.
        try:
            from app.agents.service import AgentsService
            from app.harness import harness_default_model
            from app.main import _chat_routing_for

            if not roster and cortex is not None:
                roster = await cortex.get_agents(project) or []
            record = AgentsService.find_agent(roster, lead)
            if isinstance(record, dict):
                eff_harness, eff_model, _ = _chat_routing_for(record, project)
                if body_harness:
                    harness = body_harness
                    model = body_model or harness_default_model(body_harness)
                else:
                    harness = eff_harness or None
                    model = body_model or (eff_model or None)
        except Exception:
            harness = body_harness
            model = body_model

    if not agent:
        return JSONResponse(
            {"error": "no console writer agent configured"}, status_code=400
        )
    if not repo_root or not repo_root.startswith("/"):
        return JSONResponse(
            {"error": f"project '{project}' has no absolute repo_root configured"},
            status_code=400,
        )

    # Open the run_state row up front (lease_owner='explain', NO handoff) so the SPA's
    # /runstate/stream can follow ?run=run_id immediately. Best-effort: a down store
    # leaves it unwritten but the run still dispatches (the host runner re-opens it).
    store = _runstate(request)
    if store is not None:
        try:
            await store.start_run(
                run_id=run_id,
                project=project,
                agent=agent,
                agent_display=agent,
                handoff_id=None,
                harness=harness,
                model=model,
                lease_owner="explain",
            )
        except Exception:
            pass

    # Forward the spawn to the HOST harness-service (httpx — the chat remote path's twin).
    spawn_body: dict[str, Any] = {
        "run_id": run_id,
        "project": project,
        "agent": agent,
        "kind": kind,
        "repo": repo_root,
    }
    if path:
        spawn_body["path"] = path
    if fn_name:
        spawn_body["fn_name"] = fn_name
    if git_rev:
        spawn_body["git_rev"] = git_rev
    if harness:
        spawn_body["harness"] = harness
    if model:
        spawn_body["model"] = model

    accepted = False
    error: str | None = None
    # Spawn-mode gate mirrors the chat path (main.py): only "remote" forwards to the host
    # harness-service over the container→host bridge. A NATIVE console (selfcontained VM —
    # no `host.docker.internal`) spawns run-explain directly, else the forward fails with a
    # DNS error ("Name or service not known"). This is the Explain twin of chat's local spawn.
    spawn_mode = os.environ.get("HARNESS_SPAWN_MODE", "").strip().lower()
    if spawn_mode == "remote":
        injected = _http_client(request)
        client = injected or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        try:
            resp = await client.post(
                f"{_harness_base_url()}/explain",
                json=spawn_body,
                headers=_harness_headers(),
            )
            accepted = resp.status_code == 202
            if not accepted:
                error = f"host harness-service returned {resp.status_code}"
        except httpx.HTTPError as exc:
            error = str(exc)
        finally:
            if injected is None:
                try:
                    await client.aclose()
                except Exception:
                    pass
    else:
        # Native: spawn run-explain as its own detached process, exactly as the
        # harness-service does (same argv, per-run stderr logfile, host app-DB DSN).
        try:
            import subprocess
            from app.harness_service import (
                _default_run_explain_script,
                _open_run_stderr,
                _worker_env,
            )

            explain_script = (
                os.environ.get("RUN_EXPLAIN_SCRIPT", "").strip()
                or _default_run_explain_script()
            )
            argv = [explain_script, agent, project, run_id, "--kind", kind, "--repo", repo_root]
            if path:
                argv += ["--path", path]
            if fn_name:
                argv += ["--fn", fn_name]
            if git_rev:
                argv += ["--git-rev", git_rev]
            if harness:
                argv += ["--harness", harness]
            if model:
                argv += ["--model", model]
            subprocess.Popen(  # noqa: S603 — argv is a LIST (no shell), inputs validated above
                argv,
                stdout=subprocess.DEVNULL,
                stderr=_open_run_stderr(run_id),
                env=_worker_env(),
                text=True,
                start_new_session=True,
            )
            accepted = True
        except Exception as exc:  # OSError (missing script) or an import hiccup → errored run
            error = str(exc)

    # A rejected spawn → mark the pre-opened run errored so the SPA's pane shows it
    # (best-effort). The run never silently dies.
    if not accepted and store is not None:
        try:
            await store.set_status(run_id, "error", error=error or "host explain rejected")
        except Exception:
            pass

    return JSONResponse(
        {"run_id": run_id, "accepted": accepted, "error": error},
        status_code=202 if accepted else 200,
    )


@router.get("/{project}/result/{run_id}")
async def explain_result(project: str, run_id: str, request: Request) -> JSONResponse:
    """The persisted L5 artifact for an explain run.

    The run wrote the document at the deterministic `source_file = explain/{run_id}.html`;
    we resolve it via `CortexClient.get_artifact_by_source_file`. Returns
    `{artifact_id, caption, html, modality, target_kind, target_path, created_at}` — a
    404 when the artifact isn't found yet (still generating / the L5 write degraded) or
    Cortex is down. `html` is the Cortex search PREVIEW (see the module gap note); the
    SPA renders it sandboxed."""
    cortex = _cortex(request)
    if cortex is None:
        return JSONResponse({"error": "cortex unavailable"}, status_code=404)
    source_file = f"explain/{run_id}.html"
    row = None
    try:
        row = await cortex.get_artifact_by_source_file(project, source_file)
    except Exception:
        row = None
    if not isinstance(row, dict):
        return JSONResponse(
            {"error": f"no explain artifact for run {run_id}"}, status_code=404
        )
    # The Cortex search row exposes the preview as `text` and the source_file as `meta`;
    # it carries no separate caption field, so the preview doubles as the caption hint.
    preview = row.get("text", "")
    return JSONResponse(
        {
            "artifact_id": row.get("id"),
            "caption": preview,
            "html": preview,
            "modality": row.get("category", "html"),
            "source_file": source_file,
            "run_id": run_id,
        }
    )


@router.get("/{project}/export/{run_id}")
async def export_explainer(project: str, run_id: str, request: Request) -> Response:
    """Export one project's complete explainer as a bounded ``.tar.gz`` archive.

    The generated document is stored in the run transcript and, when Cortex is
    available, duplicated in L5. Reading the transcript preserves the full document and
    also lets older runs with harmless harness preamble text export successfully. The
    archive uses fixed internal names, never reads the workspace filesystem, and verifies
    the run belongs to the requested project before returning bytes.
    """
    store = _runstate(request)
    if store is None:
        return JSONResponse({"error": "run store unavailable"}, status_code=503)
    try:
        run = await store.get_run(run_id)
    except Exception:
        run = None
    if run is None or getattr(run, "project", None) != project:
        return JSONResponse({"error": "explainer not found"}, status_code=404)
    if getattr(run, "lease_owner", None) != "explain":
        return JSONResponse({"error": "explainer not found"}, status_code=404)

    html = extract_html_document(_run_output(run))
    invalid = _validate_html(html)
    if invalid is not None:
        return JSONResponse(
            {"error": f"explainer document is not exportable: {invalid}"},
            status_code=409,
        )

    metadata = getattr(run, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
    manifest = {
        "format": "kaidera.explainer.export.v1",
        "project": project,
        "run_id": run_id,
        "status": getattr(run, "status", None),
        "agent": getattr(run, "agent", None),
        "harness": getattr(run, "harness", None),
        "model": getattr(run, "model", None),
        "started_at": getattr(run, "started_at", None),
        "updated_at": getattr(run, "updated_at", None),
        "ended_at": getattr(run, "ended_at", None),
        "artifact_id": metadata.get("artifact_id"),
        "caption": metadata.get("caption"),
        "target_kind": metadata.get("target_kind"),
        "target_path": metadata.get("target_path"),
        "files": ["explainer.html"],
    }
    stem = _safe_archive_stem(run_id)
    root = f"kaidera-explainer-{stem}"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        _add_tar_file(archive, f"{root}/explainer.html", html.encode("utf-8"))
        _add_tar_file(
            archive,
            f"{root}/manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    return Response(
        content=payload.getvalue(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{root}.tar.gz"',
            "Cache-Control": "no-store",
        },
    )


# How many recent explainers the gallery lists. Env-overridable; a tunable bound, not a
# per-project literal.
_GALLERY_LIMIT = 50


@router.get("/{project}/list")
async def explain_list(project: str, request: Request) -> JSONResponse:
    """The Explain gallery — enumerated from the console's OWN `run_state`, NOT Cortex
    search.

    WHY (the live-testing bug this fixes): Cortex artifact search is CONTENT-RELEVANCE,
    not prefix enumeration — an explain artifact is found by its content (e.g. the file
    it explains) but NOT by the literal "explain/" prefix, so `search("explain/")` can
    never reliably list all explainers. Every explain run is a `lease_owner='explain'`
    run_state row, so the run_state IS the authoritative enumeration source. (L5 stays
    the CONTENT-recall path — `cortex-graph-search` by content — untouched.)

    Reads the `RunStatePort`: `recent(project, lease_owner='explain')` → recent explain
    run HEADERS, newest-first. Shapes each into a gallery item:
      `{run_id, target_kind, target_path, caption, created_at, artifact_id, status,
        source_file, modality}`
    The `artifact_id` + `target_*` + `caption` come from the run's `metadata` sidecar
    (stamped by `explain_run.set_status('ok', …)`); `run_id` is first-class (the SPA's
    "View" loads the run's HTML via `GET /runs/run/{run_id}` — the unchanged render
    path), and `source_file` = the deterministic `explain/{run_id}.html` (SPA
    back-compat). Empty list on a down/None/raising run-state store (graceful-degrade —
    never a 500)."""
    store = _runstate(request)
    items: list[dict[str, Any]] = []
    if store is not None:
        runs: list[Any] = []
        try:
            runs = await store.recent(
                project, limit=_GALLERY_LIMIT, lease_owner="explain"
            )
        except Exception:
            runs = []
        for run in runs or []:
            run_id = getattr(run, "run_id", None)
            if not run_id:
                continue
            status = await _gallery_status(store, run, str(run_id))
            meta = getattr(run, "metadata", None)
            if not isinstance(meta, dict):
                meta = {}
            artifact_id = meta.get("artifact_id")
            target_kind = meta.get("target_kind")
            target_path = meta.get("target_path")
            # Caption: the explain run's stored title, else a target label, else the run
            # short — a row always renders with something readable.
            caption = (meta.get("caption") or "").strip()
            if not caption:
                if target_kind or target_path:
                    caption = f"Explain {target_kind or 'code'}: {target_path or ''}".strip()
                else:
                    caption = f"Explainer {str(run_id)[:8]}"
            items.append(
                {
                    "run_id": run_id,
                    "artifact_id": artifact_id,
                    "caption": caption,
                    "target_kind": target_kind,
                    "target_path": target_path,
                    "created_at": getattr(run, "started_at", None),
                    "status": status,
                    # The deterministic L5 source_file (the SPA keeps deriving run_id
                    # from it as a fallback; run_id is now first-class regardless).
                    "source_file": f"explain/{run_id}.html",
                    "modality": "html",
                }
            )
    return JSONResponse({"artifacts": items})


__all__ = ["router"]
