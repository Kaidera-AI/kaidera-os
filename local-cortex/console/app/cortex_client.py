"""Async httpx client over the live local Cortex API (read-only).

Surface: GET /health, GET /projects, GET /roster, GET /handoffs,
GET /state, GET /board, GET /projects/{key}/runtime, GET /history,
GET /search, GET /skills, GET /counts/{table}, GET /decisions/stats,
GET /decisions/recent-count, GET /messages/counts/by-agent-role,
GET /graph/stats, GET /cortex-graph/stats, GET /cortex-graph-search,
GET /admin/cortex/entities.
Most calls are read-only. The mutating methods are: the narrow dispatch-lifecycle
pair claim_handoff / complete_handoff (POST /handoffs/{id}/claim, PUT
/handoffs/{id}/complete — the autonomous + "Approve & Run" claim→complete cycle)
plus release_handoff (POST /handoffs/{id}/release — the orchestrator's conservative
reclaim of a genuinely-orphaned claim back to pending);
post_artifact (POST /artifacts — the Explain L5 persistence seam); the registration
writes create_agent / remove_agent / create_project (POST /agents,
POST /admin/agents/remove, POST /projects — the in-console registration UX +
the explicit override→registry PROMOTE, feature-gap #81); ingest_knowledge
(POST /knowledge/ingest — project-pack seed import into Cortex); bind_skill
(POST /skills/{slug}/bind — the Skills tab's deliver-to-subject write, writer-gated);
and set_project_repo_root (PATCH /projects/{key}, admin-token-gated — the in-app
project-folder editor). Every other call stays read-only. All mutators
graceful-degrade (None/False on error, never raise); the admin-gated ones source the
token backend-only and never expose it.

Base URL is configurable via the CORTEX_BASE_URL env var
(default: http://localhost:8501).

Scoped reads (roster, handoffs) send the X-Project and X-Agent-Name headers
as required by the Cortex RLS surface.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx

# Default points at the Cortex API service for this Kaidera OS deployment.
def _resolve_base_url() -> str:
    """Resolve the Cortex API base URL from the environment.

    Prefers ``CORTEX_API_URL`` — the convention the compose file, ``.envrc`` and
    the boot probe all use — falling back to the legacy ``CORTEX_BASE_URL`` and
    then localhost. This is what lets the SAME image work on the host (where
    ``CORTEX_API_URL`` is ``localhost:8501``) and in the container (where it is
    ``cortex-api:8501``); reading only ``CORTEX_BASE_URL`` silently pinned the
    client to localhost and blanked the whole Cortex surface inside Docker.
    """
    return os.environ.get(
        "CORTEX_API_URL",
        os.environ.get("CORTEX_BASE_URL", "http://localhost:8501"),
    ).rstrip("/")


CORTEX_BASE_URL = _resolve_base_url()

# Read-only console acts as a low-privilege service identity; the API requires
# X-Agent-Name on scoped (RLS) reads. Deployments can override this when a
# project-specific registered writer is needed for writer-gated operations.
CONSOLE_AGENT = os.environ.get("CORTEX_CONSOLE_AGENT", "console")

# Generous-but-bounded timeout — the Cortex API is local, so this mostly
# guards against a hung/restarting container rather than slow networks.
DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=2.0)

# Where the admin token lives on disk when it isn't exported into the console
# process env. The console usually runs WITHOUT CORTEX_ADMIN_TOKEN in its env
# (it's a gitignored local secret), so this file fallback is the path that
# actually works for the in-app project-folder edit. Resolved relative to this
# module: app/ → console/ → local-cortex/.env.
ADMIN_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class AdminTokenMissing(RuntimeError):
    """Raised when the Cortex admin token can't be sourced (env nor .env).

    The project-folder save catches this to render a graceful 'admin token not
    configured' message instead of crashing — the read-only surfaces are
    unaffected (they never need the admin token)."""


def _parse_env_file(path: Path, key: str) -> str | None:
    """Pull a single KEY=value out of a dotenv-style file (best-effort).

    Tolerant of `export KEY=`, surrounding quotes, inline blanks, and comment
    lines. Returns the stripped value, or None if the file is unreadable or the
    key is absent/empty. Never raises — a missing/locked .env just yields None."""
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            name, val = line.split("=", 1)
            if name.strip() != key:
                continue
            val = val.strip()
            if (len(val) >= 2) and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            return val or None
    except OSError:
        return None
    return None


def resolve_admin_token() -> str | None:
    """Source the Cortex admin token, env FIRST then the local .env fallback.

    Order: `os.environ['CORTEX_ADMIN_TOKEN']` (if set + non-blank), else parse it
    out of `local-cortex/.env` (the path that actually works — the console
    process env usually lacks it). Returns None if neither has it, so the caller
    can degrade gracefully. The token is NEVER returned to / rendered in the
    browser; it stays backend-only (used only as the PATCH auth header)."""
    env_val = (os.environ.get("CORTEX_ADMIN_TOKEN") or "").strip()
    if env_val:
        return env_val
    return _parse_env_file(ADMIN_ENV_FILE, "CORTEX_ADMIN_TOKEN")


def masked_admin_token() -> str | None:
    """The configured admin token, MASKED for display (only the last 4 chars survive) — so the
    operator can recognise + verify WHICH token is in use without the full secret leaving the
    server. None when no token is configured. The RAW token still never reaches the browser."""
    token = resolve_admin_token()
    if not token:
        return None
    return ("•" * 8) + token[-4:] if len(token) >= 4 else "••••••••"


class CortexClient:
    """Thin async wrapper around the Cortex HTTP API.

    One shared httpx.AsyncClient is reused for connection pooling. Create it
    on app startup and close it on shutdown (see app.main lifespan).
    """

    def __init__(
        self,
        base_url: str = CORTEX_BASE_URL,
        agent: str = CONSOLE_AGENT,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _scoped_headers(self, project_key: str) -> dict[str, str]:
        """Headers required for project-scoped (RLS-enforced) reads."""
        return {
            "X-Project": project_key,
            "X-Agent-Name": self.agent,
        }

    # ----- health -------------------------------------------------------

    async def get_health(self) -> dict[str, Any]:
        """GET /health. Returns the raw health dict, or a synthetic
        'unreachable' shape if the API cannot be reached (so the dashboard
        still renders a clear red pill instead of erroring out)."""
        try:
            resp = await self._client.get("/health")
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "status": "unreachable",
                "surface_version": None,
                "error": str(exc),
            }

    async def get_platform_config(self) -> dict[str, Any]:
        """GET /admin/cortex/config using the backend-only admin token.

        This is operational Cortex config (embedding/rerank/search models), not
        console-local UI state. The token never reaches the browser; callers map a
        missing token/unreachable Cortex to a soft UI error.
        """
        token = resolve_admin_token()
        if not token:
            raise AdminTokenMissing("CORTEX_ADMIN_TOKEN is not configured")
        resp = await self._client.get(
            "/admin/cortex/config",
            headers={"X-Cortex-Admin-Token": token},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def update_platform_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """PATCH /admin/cortex/config using the backend-only admin token."""
        token = resolve_admin_token()
        if not token:
            raise AdminTokenMissing("CORTEX_ADMIN_TOKEN is not configured")
        resp = await self._client.patch(
            "/admin/cortex/config",
            json=dict(patch or {}),
            headers={"X-Cortex-Admin-Token": token},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def get_embedding_backlog(self, project_key: str) -> dict[str, Any]:
        """GET /beat/embeddings/backlog using the backend-only admin token."""
        token = resolve_admin_token()
        if not token:
            raise AdminTokenMissing("CORTEX_ADMIN_TOKEN is not configured")
        resp = await self._client.get(
            "/beat/embeddings/backlog",
            headers={**self._scoped_headers(project_key), "X-Cortex-Admin-Token": token},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def backfill_embeddings(self, project_key: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST /beat/embeddings/backfill using the backend-only admin token."""
        token = resolve_admin_token()
        if not token:
            raise AdminTokenMissing("CORTEX_ADMIN_TOKEN is not configured")
        resp = await self._client.post(
            "/beat/embeddings/backfill",
            json=dict(body or {}),
            headers={**self._scoped_headers(project_key), "X-Cortex-Admin-Token": token},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def get_embedding_backfill_job(self, project_key: str, job_id: str) -> dict[str, Any]:
        """GET /beat/embeddings/jobs/{id} using the backend-only admin token."""
        token = resolve_admin_token()
        if not token:
            raise AdminTokenMissing("CORTEX_ADMIN_TOKEN is not configured")
        resp = await self._client.get(
            f"/beat/embeddings/jobs/{job_id}",
            headers={**self._scoped_headers(project_key), "X-Cortex-Admin-Token": token},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def verify_admin(self) -> str:
        """Verify the console's admin token against the SAME require-admin gate that project
        registration hits — so a token problem is visible in Settings BEFORE it bites at project
        creation. Read-only probe (`GET /beat/roles`, an admin-gated read; no side effects).
        Returns one of:
          * 'ok'          — the token is accepted (2xx/4xx-not-403 = auth passed);
          * 'mismatch'    — 403: the console + cortex-api disagree on CORTEX_ADMIN_TOKEN
                            (cortex-api wasn't recreated after the token landed in .env);
          * 'no_token'    — none configured (resolve_admin_token found nothing);
          * 'unreachable' — Cortex is down / unreachable.
        Never raises."""
        token = resolve_admin_token()
        if not token:
            return "no_token"
        try:
            resp = await self._client.get(
                "/beat/roles", headers={"X-Cortex-Admin-Token": token}
            )
            return "mismatch" if resp.status_code == 403 else "ok"
        except httpx.HTTPError:
            return "unreachable"

    # ----- projects -----------------------------------------------------

    async def get_projects(self) -> list[dict[str, Any]]:
        """GET /projects. Returns the full projects list (callers filter on
        status). Returns [] on error so the page degrades gracefully."""
        try:
            resp = await self._client.get("/projects")
            resp.raise_for_status()
            data = resp.json()
            return data.get("projects", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError):
            return []

    async def get_active_projects(self) -> list[dict[str, Any]]:
        """Convenience: only status == 'active' projects, preserving API order.

        The API orders by creation time. That makes first paint deterministic:
        on a fresh install with one startup-created project, the console opens
        that project; later Add Project writes append to the registry instead of
        silently becoming the default because of a name sort.
        """
        projects = await self.get_projects()
        return [p for p in projects if p.get("status") == "active"]

    async def get_project(self, project_key: str) -> dict[str, Any] | None:
        """Look up a single active project by key (used by the partial route)."""
        for project in await self.get_active_projects():
            if project.get("project_key") == project_key:
                return project
        return None

    async def set_project_repo_root(
        self, project_key: str, repo_root: str
    ) -> dict[str, Any]:
        """PATCH /projects/{project_key} — change a project's canonical working
        folder (repo_root). The ONE admin-authed (mutating) call on this client;
        every other method stays read-only.

        Sends `{"repo_root": "<abs path>"}` with the `X-Cortex-Admin-Token`  # fitness:allow-literal "repo_root" is a real wire key, not a project literal (false match on 'root')
        header (sourced backend-only via resolve_admin_token — env first, then the
        local-cortex/.env fallback; the token is never exposed to the browser).
        The endpoint returns `{project_key, repo_root, previous_repo_root}`, which
        we return so the caller can show previous → new.

        Validation: `repo_root` MUST be a non-blank absolute path (we reject
        relative paths client-side before the network call). Raises:
          * ValueError              — blank / non-absolute path
          * AdminTokenMissing       — no token in env nor .env (graceful 'not
                                      configured' path; nothing is sent)
          * httpx.HTTPStatusError   — the API rejected the PATCH (4xx/5xx)
          * httpx.HTTPError         — transport/timeout failure
        The caller (main.set_repo_root_save) maps these onto user-facing banners.
        """
        path = (repo_root or "").strip()
        if not path:
            raise ValueError("repo_root is required")
        if not path.startswith("/"):
            raise ValueError("repo_root must be an absolute path")

        token = resolve_admin_token()
        if not token:
            raise AdminTokenMissing("CORTEX_ADMIN_TOKEN is not configured")

        resp = await self._client.patch(
            f"/projects/{project_key}",
            json={"repo_root": path},  # fitness:allow-literal Cortex field name (the `root` agent-name pattern is a false positive)
            headers={
                "X-Cortex-Admin-Token": token,
                "X-Project": project_key,
                "X-Agent-Name": self.agent,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    # ----- registration writes (feature-gap #81) ------------------------
    #
    # Three narrow mutating calls behind the in-console registration UX (add an
    # agent, deregister an agent, add a project). They write to the LIVE Cortex
    # registry over its HTTP API ONLY — never psql — and every one honours the
    # same GRACEFUL-DEGRADE contract as the dispatch/artifact mutators above:
    # return None/False (never raise) on any error, short-circuit a blank required
    # arg WITHOUT a network call, and (for the admin-gated pair) source the
    # X-Cortex-Admin-Token SERVER-SIDE — it is NEVER returned to the browser.

    async def create_agent(
        self,
        project_key: str,
        *,
        name: str,
        role: str,
        capabilities: dict[str, Any] | None = None,
        writer_scope: str | None = None,
        role_description: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any] | None:
        """POST /agents — register (or UPSERT) one agent on a project's roster.

        Mirrors the Cortex `AgentRegister` model: `name` + `role` are required;
        `capabilities` (the harness/model/reasoning/writer_scope blob), `writer_scope`,
        and `role_description` are optional enrichers. The route gates the CALLER
        (`X-Agent-Name`) — this client's configured service identity — NOT the subject being registered, so an existing
        writer can add a brand-new agent as DATA. It is the SAME endpoint
        `cortex-add-agent` uses, and the SAME endpoint the explicit override→registry
        PROMOTE re-posts to (the conflict-update jsonb-MERGES `capabilities`, so
        re-registering an agent with merged harness/model/reasoning persists them additively).

        Headers: `X-Agent-Name` (the caller/writer) + `X-Project` (RLS scope). NO admin
        token (this is a writer-gated, not admin-gated, route).

        GRACEFUL-DEGRADE: returns the API's result dict on a 2xx; returns None on a
        blank name/role/project (no request issued), or ANY 4xx/5xx, transport, or
        parse error — it NEVER raises. A None means the registry write didn't land
        (the caller surfaces a soft signal); the local override save is unaffected."""
        proj = (project_key or "").strip()
        nm = (name or "").strip()
        rl = (role or "").strip()
        if not proj or not nm or not rl:
            return None
        body: dict[str, Any] = {
            "name": nm,
            "role": rl,
            "capabilities": dict(capabilities or {}),
        }
        ws = (writer_scope or "").strip()
        if ws:
            body["writer_scope"] = ws
        rd = (role_description or "").strip()
        if rd:
            body["role_description"] = rd
        try:
            resp = await self._client.post(
                "/agents",
                json=body,
                headers={"X-Project": proj, "X-Agent-Name": (caller or "").strip() or self.agent},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def remove_agent(self, project_key: str, name: str) -> bool:
        """POST /admin/agents/remove — deregister (deactivate) one agent from a
        project's roster, preserving its history.

        Roster-only removal (the agents row + all of the agent's decisions/lessons/
        handoffs are KEPT; it just stops counting toward the live roster/writer set).
        ADMIN-TOKEN gated: the token is sourced backend-only via `resolve_admin_token`
        (env first, then the local-cortex/.env fallback) and sent as the
        `X-Cortex-Admin-Token` header — it is NEVER returned to the browser. The body
        is the Cortex `AgentRemove` model: `{project, agent_name}`.

        GRACEFUL-DEGRADE: returns True on a 2xx (including the API's idempotent
        already-absent / already-inactive no-op — the roster ends in the intended
        state). Returns False WITHOUT a request on a blank project/agent OR when no
        admin token is configured (the 'admin not configured' path), and False on ANY
        4xx/5xx, transport, or parse error — it NEVER raises."""
        proj = (project_key or "").strip()
        nm = (name or "").strip()
        if not proj or not nm:
            return False
        token = resolve_admin_token()
        if not token:
            return False
        try:
            resp = await self._client.post(
                "/admin/agents/remove",
                json={"project": proj, "agent_name": nm},
                headers={"X-Cortex-Admin-Token": token},
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def create_project(
        self,
        *,
        project_key: str,
        display_name: str | None = None,
        repo_root: str | None = None,
        repo_type: str | None = None,
        default_agent: str | None = None,
        agents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """POST /projects — register (or update) a Cortex project.

        Mirrors the Cortex `ProjectRegister` model: `project_key` is required;
        `display_name`, `repo_root` (the canonical working folder — the API needs a
        repo_root OR at least one roots[] entry), `repo_type`, `default_agent`, and
        an optional initial `agents` roster are optional. ADMIN-TOKEN gated (the same
        backend-only token sourcing + header as `set_project_repo_root` / `remove_agent`;
        the token is NEVER exposed).

        GRACEFUL-DEGRADE: returns the API's result dict on a 2xx; returns None WITHOUT
        a request on a blank project_key OR when no admin token is configured, and None
        on ANY 4xx/5xx, transport, or parse error — it NEVER raises."""
        key = (project_key or "").strip()
        if not key:
            return None
        token = resolve_admin_token()
        if not token:
            return None
        body: dict[str, Any] = {"project_key": key}
        dn = (display_name or "").strip()
        if dn:
            body["display_name"] = dn
        root = (repo_root or "").strip()
        if root:
            body["repo_root"] = root  # fitness:allow-literal Cortex field name (the `root` agent-name pattern is a false positive)
        rt = (repo_type or "").strip()
        if rt:
            body["repo_type"] = rt
        da = (default_agent or "").strip()
        if da:
            body["default_agent"] = da
        if agents:
            body["agents"] = agents
        try:
            resp = await self._client.post(
                "/projects",
                json=body,
                headers={"X-Cortex-Admin-Token": token},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def ingest_knowledge(
        self,
        project_key: str,
        *,
        content: str,
        source_file: str,
        category: str | None = None,
        section: str | None = None,
        on_conflict: str = "update",
    ) -> dict[str, Any] | None:
        """POST /knowledge/ingest — import one project-scoped knowledge row.

        Used by Add Project when an installed project pack includes Cortex seed
        files. The source path is the pack-relative installed file path under
        `.kaidera-os/project-packs/...`, so repeated imports are idempotent and
        project-local. No filesystem fallback or second memory plane is added.

        GRACEFUL-DEGRADE: returns the API's result dict on 2xx; returns None on
        blank project/content/source_file or any HTTP/transport/parse failure.
        """
        proj = (project_key or "").strip()
        text = content if isinstance(content, str) else ""
        source = (source_file or "").strip()
        if not proj or not text.strip() or not source:
            return None
        body: dict[str, Any] = {
            "content": text,
            "source_file": source,
            "on_conflict": (on_conflict or "update").strip() or "update",
        }
        cat = (category or "").strip()
        if cat:
            body["category"] = cat
        sec = (section or "").strip()
        if sec:
            body["section"] = sec
        try:
            resp = await self._client.post(
                "/knowledge/ingest",
                json=body,
                headers={"X-Project": proj, "X-Agent-Name": self.agent},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    # ----- roster -------------------------------------------------------

    async def get_roster(self, project_key: str) -> list[dict[str, Any]]:
        """GET /roster scoped to a project. Returns the agents list ([] on error)."""
        try:
            resp = await self._client.get(
                "/roster", headers=self._scoped_headers(project_key)
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("agents", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError):
            return []

    # ----- skills (catalogue + bind) -----------------------------------

    async def get_skills(self, project_key: str) -> list[dict[str, Any]]:
        """GET /skills scoped to a project. Returns the skills list ([] on error).

        The Cortex skills surface lists EVERY global skill (scope='global', the shared
        skills repo, sentinel project '*') PLUS this project's own project/agent-scoped
        skills — so the response is the catalogue the operator can browse + bind. Each row
        is shaped {id, project, skill_slug, name, description, skill_type, scope, body_ref,
        body_hash, version, status, trust_tier, ...}. Sends the X-Project + X-Agent-Name
        headers like the other RLS reads. Graceful-degrade: [] on any transport/parse
        error so the Skills tab renders an empty catalogue rather than erroring out."""
        try:
            resp = await self._client.get(
                "/skills", headers=self._scoped_headers(project_key)
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("skills", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError):
            return []

    async def bind_skill(
        self, project_key: str, slug: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """POST /skills/{slug}/bind — deliver a registered skill to a subject (a role or a
        single agent) so it reaches that subject at boot.

        Mirrors the `cortex-skill bind` CLI's write: the body is the Cortex bind model
        `{subject_kind, subject}` (the route also accepts an optional `project`). The
        route gates the CALLER (`X-Agent-Name` — this client's configured service writer)
        — NOT the subject — so an existing writer can bind a skill
        as DATA. Headers: `X-Agent-Name` (the caller/writer) + `X-Project` (RLS scope). NO
        admin token (a writer-gated route, like create_agent).

        GRACEFUL-DEGRADE (mirrors every sibling mutator): returns the API's binding row on
        a 2xx; returns None WITHOUT a request on a blank slug/subject, and None on ANY
        4xx/5xx, transport, or parse error — it NEVER raises (a down Cortex must not crash
        the bind path that called it)."""
        sl = (slug or "").strip()
        subject = str(payload.get("subject") or "").strip() if isinstance(payload, dict) else ""
        if not sl or not subject:
            return None
        body: dict[str, Any] = {
            "subject_kind": str(payload.get("subject_kind") or "role").strip() or "role",
            "subject": subject,
        }
        proj_override = str(payload.get("project") or "").strip() if isinstance(payload, dict) else ""
        if proj_override:
            body["project"] = proj_override
        try:
            resp = await self._client.post(
                f"/skills/{sl}/bind",
                json=body,
                headers={"X-Project": project_key, "X-Agent-Name": self.agent},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    # ----- runtime (rich agents) ---------------------------------------

    async def get_runtime(self, project_key: str) -> dict[str, Any]:
        """GET /projects/{key}/runtime. The richer per-project view: each agent
        carries name/role/model plus a `capabilities` dict (harness, provider,
        thinking, description, writer_scope, ...). Returns the raw runtime dict
        ({} on error) so callers can fall back to /roster."""
        try:
            resp = await self._client.get(
                f"/projects/{project_key}/runtime",
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def get_agents(self, project_key: str) -> list[dict[str, Any]]:
        """Best-effort rich agent list for a project: prefer /runtime (name,
        role, model, capabilities, writer_scope), fall back to /roster when the
        runtime surface is unavailable. Always returns a list ([] on total
        failure) so the agents column degrades gracefully."""
        runtime = await self.get_runtime(project_key)
        agents = runtime.get("agents") if isinstance(runtime, dict) else None
        if agents:
            return agents
        return await self.get_roster(project_key)

    # ----- handoffs -----------------------------------------------------

    async def get_handoffs(
        self, project_key: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /handoffs scoped to a project. Returns the handoffs list ([] on error).

        The list endpoint is STATUS-SCOPED. With no ``status`` it returns PENDING
        handoffs — the dispatch queue the orchestrator scans for new work. Claimed
        (in-flight) handoffs are NOT in that default list; pass ``status="claimed"``
        to get them — that is what the PM watchdog supervises."""
        params = {"status": status} if status else None
        try:
            resp = await self._client.get(
                "/handoffs", headers=self._scoped_headers(project_key), params=params
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("handoffs", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError):
            return []

    async def get_handoff(
        self, project_key: str, handoff_id: str
    ) -> dict[str, Any] | None:
        """GET one full handoff by id or id prefix, scoped to a project."""
        hid = (handoff_id or "").strip()
        if not hid:
            return None
        try:
            resp = await self._client.get(
                f"/handoffs/{hid}", headers=self._scoped_headers(project_key)
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def create_handoff(
        self, project_key: str, from_agent: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /handoffs as a registered project writer.

        Scheduled jobs and extensions use this to turn triggers into ordinary Cortex
        handoffs. Dispatch still owns execution; this method never claims or runs
        the work. Returns the Cortex response on success or a soft
        `{ok:false,error}` dict on any validation/transport failure.
        """
        project = (project_key or "").strip()
        agent = (from_agent or "").strip()
        if not project or not agent:
            return {"ok": False, "error": "project and from_agent are required"}
        try:
            resp = await self._client.post(
                "/handoffs",
                headers={"X-Project": project, "X-Agent-Name": agent},
                json=body or {},
            )
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = str(resp.json().get("detail") or "")
                except Exception:
                    detail = resp.text[:240]
                return {"ok": False, "error": detail or f"HTTP {resp.status_code}"}
            data = resp.json()
            return data if isinstance(data, dict) else {"ok": False, "error": "invalid Cortex response"}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    async def claim_handoff(
        self, project_key: str, handoff_id: str, agent: str
    ) -> bool:
        """POST /handoffs/{id}/claim AS `agent` — the autonomous orchestrator's
        idempotency primitive. The Cortex claim endpoint flips a PENDING handoff
        to `claimed` ONLY when the claiming X-Agent-Name matches the handoff's
        `to_agent` (or one of that agent's roles matches `to_role`), so the loop
        claims as the RESOLVED TARGET agent — the same agent it is about to run.

        A successful claim (HTTP 200) means THIS process owns the dispatch and the
        row will not be re-picked by another loop pass (or another console). A 404
        means it was already claimed / not pending / not targeted at this agent —
        treated as "someone else has it" (return False, skip). This is the ONLY
        mutating call the loop makes, and it is intentionally narrow (claim only;
        never complete/log).

        Returns True iff the claim succeeded; False on any 4xx/5xx or transport
        error (the loop then skips the handoff — never double-dispatches)."""
        hid = (handoff_id or "").strip()
        who = (agent or "").strip()
        if not hid or not who:
            return False
        try:
            resp = await self._client.post(
                f"/handoffs/{hid}/claim",
                headers={"X-Project": project_key, "X-Agent-Name": who},
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def release_handoff(
        self,
        project_key: str,
        handoff_id: str,
        agent: str = "",
        reason: str = "",
    ) -> bool:
        """POST /handoffs/{id}/release — UNCLAIM a handoff (claimed → back to pending).

        The reclaim counterpart of `claim_handoff`: the orchestrator's conservative
        "reclaim orphaned claim" pass uses this to release a handoff that was CLAIMED
        by an agent that never ran it (e.g. an agent whose loop was disabled), so the
        row returns to PENDING and the existing dispatch path can re-pick it. The
        Cortex release endpoint flips a `claimed` row back to `pending` and clears
        claimed_by/claimed_at; on this surface X-Agent-Name is OPTIONAL (the API can
        back-fill the actor from claimed_by), so we send it only when given.

        A successful release (HTTP 200) returns True. A 404 means the handoff was not
        found / not claimable — treated as "nothing to do" (False). This is a NARROW
        mutating call (release only); it never claims, completes, or logs. It is
        IDEMPOTENT on Cortex's end (releasing an already-pending row is harmless), and
        the orchestrator guards it further (it only releases a genuinely-orphaned,
        no-run, aged claim).

        GRACEFUL-DEGRADE (mirrors every sibling): a blank id short-circuits to False
        WITHOUT a request, and ANY 4xx/5xx or transport error returns False rather
        than raising — a down Cortex must never crash the reconcile loop that called
        it (the orphaned claim simply stays put and is re-evaluated next sweep)."""
        hid = (handoff_id or "").strip()
        if not hid:
            return False
        headers = {"X-Project": project_key}
        who = (agent or "").strip()
        if who:
            headers["X-Agent-Name"] = who
        try:
            resp = await self._client.post(
                f"/handoffs/{hid}/release",
                headers=headers,
                json={"reason": reason} if reason else None,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def complete_handoff(
        self, project_key: str, handoff_id: str, agent: str = ""
    ) -> bool:
        """PUT /handoffs/{id}/complete — close a handoff once its run SUCCEEDED.

        The completion counterpart of `claim_handoff`, used by the "Approve & Run"
        cycle (Milestone 1 T9) to finish the lifecycle a claim opened: a run that
        streamed to a successful terminal status completes its handoff so it leaves
        the active queue (and a later loop pass / the watchdog doesn't re-pick it).
        The Cortex complete endpoint flips a row to `completed` and stamps
        completed_at; on this surface X-Agent-Name is OPTIONAL (the API back-fills
        the actor from claimed_by/from_agent), so we send it only when given.

        A successful completion (HTTP 200) returns True. A 404 means the handoff was
        already completed / not found — treated as "nothing to do" (False). This is
        a NARROW mutating call (complete only); it never claims or logs.

        GRACEFUL-DEGRADE (mirrors every sibling): a blank id short-circuits to False
        WITHOUT a request, and ANY 4xx/5xx or transport error returns False rather
        than raising — a down Cortex must never crash the run path that called it
        (the run already streamed; the watchdog re-completes a silent miss)."""
        hid = (handoff_id or "").strip()
        if not hid:
            return False
        headers = {"X-Project": project_key}
        who = (agent or "").strip()
        if who:
            headers["X-Agent-Name"] = who
        try:
            resp = await self._client.put(
                f"/handoffs/{hid}/complete",
                headers=headers,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ----- state (vitals) ----------------------------------------------

    async def get_state(self, project_key: str) -> dict[str, Any]:
        """GET /state scoped to a project. Returns the raw state dict, or an
        empty dict on error so the vitals strip degrades to '—' rather than
        erroring out. Callers read state['summary'] for the live counters."""
        try:
            resp = await self._client.get(
                "/state", headers=self._scoped_headers(project_key)
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    # ----- epics (project-scoped epic + increment progress) ------------

    async def get_epics(self, project_key: str) -> dict[str, Any]:
        """GET /epics scoped to a project. Returns the raw epics dict, shaped
        {"project": str, "epics": [{epic_id, title, status, overall_pct,
        increments: [{num, title, status, pct}], updated_at}]}.

        Graceful-degrade: on any transport/timeout/parse error (or a project
        that simply has no epics) we return {"epics": []} so callers render the
        'continuous · no epics' / empty state rather than erroring out. We never
        fabricate progress — an unreachable surface looks the same as 'no epics'.
        """
        try:
            resp = await self._client.get(
                "/epics", headers=self._scoped_headers(project_key)
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("epics"), list):
                return data
            return {"epics": []}
        except (httpx.HTTPError, ValueError):
            return {"epics": []}

    # ----- board (tasks) -----------------------------------------------

    async def get_board(self, project_key: str) -> list[dict[str, Any]]:
        """GET /board scoped to a project. Returns the tasks list
        ([] on error)."""
        try:
            resp = await self._client.get(
                "/board", headers=self._scoped_headers(project_key)
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("tasks", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError):
            return []

    # ----- history (recent messages, agent activity feed) --------------

    async def get_history(
        self, project_key: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """GET /history scoped to a project. Returns the raw `messages` list
        ([] on error), each row shaped {when, agent_name, role, content}.

        NOTE: the API ignores the `agent_name` query filter (verified against
        the live surface), so callers must filter client-side by `agent_name`.
        `content` is often noisy/truncated tool-call JSON — the agent-detail
        view summarizes each row into a readable line (see main._activity_feed).
        We request a generous `limit` so a specific agent's rows are more likely
        to appear in the (server-capped) window."""
        try:
            resp = await self._client.get(
                "/history",
                params={"last": limit},
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("messages", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError):
            return []

    # ----- search (decisions / lessons / graph mix) --------------------

    async def search(
        self, project_key: str, query: str, limit: int = 12, *, rerank: bool = True
    ) -> list[dict[str, Any]]:
        """GET /search scoped to a project. Returns the `results` list ([] on
        error), each row roughly {id?, text, source, category, relevance, ...}.

        `source` distinguishes the layer (decisions / lessons / graph / ...);
        the History view uses this for its recent-decisions feed. A blank query
        yields [] (the API needs a term).

        `rerank` (default True = the server default): pass False on LATENCY-sensitive,
        broad context/seed pulls (history feed, etc.) where the ~3s nv-rerank-qa-mistral-4b
        pass buys nothing — the HNSW retrieval alone is ~190ms. Keep True for a user's
        precise query where result ordering matters."""
        if not query:
            return []
        try:
            params: dict[str, Any] = {"q": query, "limit": limit}
            if not rerank:
                params["rerank"] = "false"
            resp = await self._client.get(
                "/search",
                params=params,
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError):
            return []

    # ----- L5 artifacts (Explain capability: write + lookup) -----------

    async def post_artifact(
        self,
        project_key: str,
        agent: str,
        *,
        source_file: str,
        content_hash: str,
        modality: str = "html",
        raw_content: str,
        caption: str | None = None,
        neighborhood_text: str | None = None,
        source_doc_metadata: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        edge_type: str | None = None,
        target_type: str | None = None,
        target_ref: str | None = None,
    ) -> str | None:
        """POST /artifacts — persist ONE Cortex L5 artifact and return its id.

        The ONE artifact-WRITE on this client (the Explain capability's persistence
        seam). Mirrors the Cortex `ArtifactIngestRequest` model exactly: `source_file`
        + `content_hash` (a 64-char sha256 hex) are required; `modality` defaults to
        `html`; `raw_content` carries the generated document; `caption` /
        `neighborhood_text` / `source_doc_metadata` / `metadata` enrich retrieval.
        When the `edge_type` / `target_type` / `target_ref` triple is supplied it
        records an artifact_edges row (e.g. `explains` → the explained target). The API
        upserts on `(project, source_file, content_hash)`, so a re-post of the same
        content is idempotent. Writes via the HTTP endpoint ONLY (never psql).

        Headers: `X-Agent-Name` (the registered writer — the route requires it) +
        `X-Project` (RLS scope). The `source_type`/`extraction_method` are pinned to the
        generated-API-capture contract (`api_capture` / `generated`).

        GRACEFUL-DEGRADE (mirrors every sibling): returns the artifact id on a 200/201,
        and None on ANY 4xx/5xx, transport, or parse error — it NEVER raises. The Explain
        run treats the L5 write as best-effort: a None here means the run still succeeds,
        only without a persisted artifact (the caller logs it)."""
        sf = (source_file or "").strip()
        ch = (content_hash or "").strip()
        if not sf or not ch:
            return None
        body: dict[str, Any] = {
            "source_file": sf,
            "content_hash": ch,
            "modality": modality,
            "source_type": "api_capture",
            "extraction_method": "generated",
            "raw_content": raw_content,
        }
        if caption is not None:
            body["caption"] = caption
        if neighborhood_text is not None:
            body["neighborhood_text"] = neighborhood_text
        if source_doc_metadata is not None:
            body["source_doc_metadata"] = source_doc_metadata
        if metadata is not None:
            body["metadata"] = metadata
        if edge_type and target_type and target_ref:
            body["edge_type"] = edge_type
            body["target_type"] = target_type
            body["target_ref"] = target_ref
        who = (agent or self.agent or "").strip()
        try:
            resp = await self._client.post(
                "/artifacts",
                json=body,
                headers={"X-Project": project_key, "X-Agent-Name": who},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                aid = data.get("artifact_id") or data.get("id")
                return str(aid) if aid else None
            return None
        except (httpx.HTTPError, ValueError):
            return None

    async def get_artifact_by_source_file(
        self, project_key: str, source_file: str
    ) -> dict[str, Any] | None:
        """Find ONE L5 artifact by its unique `source_file` (GET /search, artifacts).

        The read counterpart of `post_artifact`: the Explain SPA lands on a run, reads
        its `artifact_id`, and needs the artifact's metadata to render it. The Cortex
        search surface (`search_type=artifacts`) is the only public read of L5, so we
        query it for the `source_file` token and pick the row whose `meta` (the artifact's
        `source_file`) matches EXACTLY — `source_file` is unique per project, so the match
        is unambiguous. Returns the matched search row (shaped `{id, text, meta, category,
        source, score, tier}`) or None on a miss.

        NOTE (documented gap): the search row's `text` is a TRUNCATED preview
        (`LEFT(raw_content, 300)`), not the full document — the current Cortex API exposes
        no full-artifact-by-id read, so a faithful full-HTML render needs that endpoint
        (a follow-up). This lookup is enough to resolve the id + caption + modality.

        GRACEFUL-DEGRADE: None on a blank input, an empty result, or ANY transport/parse
        error — never raises (a down Cortex must not crash the read path)."""
        sf = (source_file or "").strip()
        if not sf:
            return None
        try:
            resp = await self._client.get(
                "/search",
                params={"q": sf, "type": "artifacts", "limit": 20},
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        results = data.get("results", []) if isinstance(data, dict) else []
        if not isinstance(results, list):
            return None
        # Exact match on the artifact's source_file (carried as `meta`). source_file is
        # unique per project, so the first exact match is THE artifact.
        for row in results:
            if isinstance(row, dict) and (row.get("meta") or "").strip() == sf:
                return row
        return None

    # ----- analytics counts --------------------------------------------

    async def get_count(self, project_key: str, table: str) -> int | None:
        """GET /counts/{table} for a project. Returns the integer row count, or
        None on error / missing (the Analytics cards render '—' for None)."""
        try:
            resp = await self._client.get(
                f"/counts/{table}",
                params={"project": project_key},
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            val = data.get("count") if isinstance(data, dict) else None
            return val if isinstance(val, int) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def get_decision_stats(self, project_key: str) -> dict[str, Any]:
        """GET /decisions/stats for a project. Returns the raw stats dict
        ({} on error): {total, processed, unprocessed, invalidated, active,
        by_agent: {agent:count}}. Analytics uses by_agent for its per-agent bars."""
        try:
            resp = await self._client.get(
                "/decisions/stats",
                params={"project": project_key},
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def get_decisions_recent_count(
        self, project_key: str, since_iso: str
    ) -> int | None:
        """GET /decisions/recent-count?since=<ISO> for a project. Returns the
        count of decisions logged since the ISO timestamp, or None on error.
        The API caps the window (max_window_days); a too-old `since` is clamped
        server-side."""
        try:
            resp = await self._client.get(
                "/decisions/recent-count",
                params={"project": project_key, "since": since_iso},
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            val = data.get("count") if isinstance(data, dict) else None
            return val if isinstance(val, int) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def get_message_counts_by_agent_role(
        self, project_key: str
    ) -> list[dict[str, Any]]:
        """GET /messages/counts/by-agent-role for a project. Returns the `rows`
        list ([] on error), each {agent_name, role, count}. Analytics rolls
        these up per agent for the activity-volume breakdown."""
        try:
            resp = await self._client.get(
                "/messages/counts/by-agent-role",
                params={"project": project_key},
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("rows", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError):
            return []

    # ----- knowledge graph (L3 code graph + L4 entities) ---------------

    async def get_graph_stats(self, project_key: str) -> dict[str, Any]:
        """GET /graph/stats. Returns the raw stats dict ({} on error):
        {repos: [{name, nodes, edges, path}], total_nodes, total_edges}.

        The RESPONSE lists every code-graph repo on the box (not just this
        project's), but the endpoint still REQUIRES the X-Project header (it
        400s without it), so we pass scoped headers. The Graph view picks out
        the selected project's repo + shows the cross-repo totals for context."""
        try:
            resp = await self._client.get(
                "/graph/stats", headers=self._scoped_headers(project_key)
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def get_cortex_graph_stats(self, project_key: str) -> dict[str, Any]:
        """GET /cortex-graph/stats. Returns the project-scoped L4 graph stats:
        {entity_count, relationship_count, source_counts, backlog}.

        This is the Cortex-native entity graph/backlog read. It complements
        `get_graph_stats`, which is the L3 code-graph worker aggregate."""
        try:
            resp = await self._client.get(
                "/cortex-graph/stats", headers=self._scoped_headers(project_key)
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def get_cortex_memory_graph(
        self, project_key: str, limit: int = 500
    ) -> dict[str, Any]:
        """GET /cortex-graph/memory. Returns the project-scoped L4 memory graph."""
        try:
            resp = await self._client.get(
                "/cortex-graph/memory",
                params={"limit": max(1, min(int(limit or 500), 2000))},
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def graph_search(
        self, project_key: str, query: str, limit: int = 8, expand: bool = False
    ) -> dict[str, Any]:
        """GET /cortex-graph-search?q=<term> scoped to a project. Returns the
        raw dual-level result dict ({} on error):
        {high_level: [...], low_level: [{id, entity_type, name, description,
        score}], relationships: [{source, source_type, relationship_type,
        target, target_type, description}]}.

        `expand=True` asks the server to include one-hop relationship context.
        A blank query yields {} (the API needs a term)."""
        if not query:
            return {}
        try:
            params: dict[str, Any] = {"q": query, "limit": limit}
            if expand:
                params["expand"] = "true"
            resp = await self._client.get(
                "/cortex-graph-search",
                params=params,
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def get_cortex_entities(
        self, project_key: str, search: str = "", limit: int = 12
    ) -> tuple[list[dict[str, Any]], bool]:
        """GET /admin/cortex/entities — the raw L4 entity browse (admin surface).

        Returns (entities, reachable). The admin compatibility surface is
        TOKEN-GATED, so on this read-only console it is usually NOT reachable —
        we return ([], False) on any auth/transport error and the Graph view
        falls back to the (always-reachable) /cortex-graph-search entity list.
        Only when the endpoint genuinely answers do we return (rows, True)."""
        try:
            params: dict[str, Any] = {"limit": limit}
            if search:
                params["search"] = search
            resp = await self._client.get(
                "/admin/cortex/entities",
                params=params,
                headers=self._scoped_headers(project_key),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                rows = data.get("entities") or data.get("results") or []
                return (rows if isinstance(rows, list) else [], True)
            if isinstance(data, list):
                return (data, True)
            return ([], True)
        except (httpx.HTTPError, ValueError):
            return ([], False)

    # ----- live event feed (GET /events SSE bridge) --------------------

    async def stream_events(
        self, project_key: str
    ) -> AsyncGenerator[bytes, None]:
        """Connect to the Cortex `GET /events` SSE bridge (project-scoped) and
        yield its raw `text/event-stream` bytes for re-streaming to a browser.

        The Cortex `/events` endpoint parks on the Postgres `cortex_events`
        NOTIFY condition and pushes a frame the instant a row (handoff, decision,
        etc.) lands for the project — true event-driven push, not polling. It
        REQUIRES the `X-Project` + `X-Agent-Name` headers (RLS), which a browser
        `EventSource` cannot set; the console-side `/stream` proxy uses THIS to
        bridge those headers in (the proxy adds nothing else, it just relays).

        Yields raw chunks (NOT decoded/parsed) so the proxy is a transparent
        passthrough — heartbeat comments (`: ping`) and `event:`/`data:` frames
        flow through untouched. We open the stream with NO read timeout (an idle
        SSE connection between events is normal); the connect timeout still
        guards a dead/restarting container. On any transport error the generator
        simply stops (the proxy then closes the browser stream cleanly).

        This is READ-ONLY: a GET against the event bridge. It never mutates
        Cortex (the console's read-only invariant holds)."""
        # No read timeout: an SSE feed is idle between events by design. Keep the
        # connect timeout so a dead container fails fast rather than hanging.
        sse_timeout = httpx.Timeout(None, connect=2.0)
        try:
            async with self._client.stream(
                "GET",
                "/events",
                headers={
                    **self._scoped_headers(project_key),
                    "Accept": "text/event-stream",
                },
                timeout=sse_timeout,
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
        except (httpx.HTTPError, ValueError):
            # Upstream unreachable / dropped → stop yielding; caller closes the
            # browser stream. We never surface a raw transport error as an event.
            return
