"""In-console SKILLS routes — the SPA Skills-tab backend.

Three additive console routes that front the live Cortex skills surface so the SPA's
Skills tab can browse the catalogue, install a skill from a GitHub URL, and bind a
skill to an agent or role — the console's in-app way to manage skills without dropping
to the `cortex-skill` CLI:

  * GET  /skills/{project}              → CortexClient.get_skills        (read; catalogue)
        the global + this-project skills, shaped `{skills: [...]}` for the SPA.
  * POST /skills/{project}/install      → shell out to `cortex-skill install`  (write)
        body: {url, scope?} — clones/registers the skill, then returns the REFRESHED
        catalogue (`{ok, error, skills}`) so the tab re-renders without a second call.
  * POST /skills/{project}/{slug}/bind  → CortexClient.bind_skill        (write)
        body: {subject, subject_kind?} — delivers the skill to a role (default) or agent.

The READ + BIND routes proxy the shared `CortexClient` on `app.state.cortex` (the same
`get_skills` / `bind_skill` seams the SPA needs), resolved at a `Depends` seam so the
handlers can be driven directly with a fake in tests (the registration_api / settings
idiom). The INSTALL route shells out to the `cortex-skill` CLI — clone + SKILL.md parse +
registration live in that one tool, so the console reuses it rather than re-implementing
the clone/register flow (the same reuse the watchdog makes for `cortex-*` CLIs). The CLI
dir is resolved like the watchdog's (`<repo>/.agents/scripts`, env-overridable
`CORTEX_CLI_DIR`) and put on the subprocess PATH; `CORTEX_PROJECT` is set so the CLI
registers/binds against the selected project.

HOUSE LAW — every route GRACEFUL-DEGRADES + is TOKEN-SAFE:
  * input is validated first (blank url / blank subject) → a friendly `ok=false` + a clear
    `error` WITHOUT touching Cortex / spawning the CLI;
  * the write itself is best-effort — a degraded read/write (None from the client, a non-
    zero CLI exit / spawn failure / timeout) becomes a soft `ok=false` + a friendly,
    NON-LEAKY error;
  * never a 500.

`main.py` mounts this additively (`app.include_router(skills_api.router)`). The distinct
`/skills/{project}` (+ `/install`, `/{slug}/bind`) shapes can't shadow any existing route
(no other surface owns the `/skills/...` route prefix).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, Request

from . import auth as auth_module
from .kaidera_agent import _ssrf_blocked

router = APIRouter(prefix="/skills", tags=["skills"])

# The only remote hosts an install may clone from. A skill install spawns `git clone`
# on the agent's deployment box, so an unvalidated URL is an SSRF lever (git:// →
# internal host, file:// → local disk, http://169.254.169.254 → cloud-metadata). We
# allow only these well-known code-forge hosts over https:// (plus any *.github.com,
# e.g. an enterprise subdomain), and additionally DNS-resolve the host through the same
# `_ssrf_blocked` guard the kaidera web_fetch uses. A bare local filesystem path is
# allowed for the dev/test workflow only when it actually exists.
_INSTALL_HOST_ALLOWLIST = frozenset({"github.com", "gitlab.com", "bitbucket.org"})

# The valid `--scope` values the install form may set (mirrors the CLI). An unknown /
# blank scope is dropped so the CLI applies its own precedence (frontmatter → global).
_SCOPES = ("global", "project", "agent")

def _default_cli_dir() -> str:
    """Find <repo>/.agents/scripts without assuming a fixed source depth.

    Host source lives under local-cortex/console/app; the container image lives
    under /app/app. A fixed parents[N] crashes in the image, so walk upward and
    degrade to /app/.agents/scripts when the CLIs are intentionally absent.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".agents" / "scripts"
        if candidate.is_dir():
            return str(candidate)
    return "/app/.agents/scripts"


# The cortex-* CLIs live at <repo>/.agents/scripts; the console's PATH (launchd/nohup)
# does NOT include it, so a bare `cortex-skill` would fail to spawn. Resolve it by
# walking upward, env-overridable, and put it on the subprocess PATH. Never a hardcoded
# personal path (the no-project-literals gate). Mirrors watchdog._CLI_DIR +
# explain_context._graph_blast_script, but is safe inside the container image.
_CLI_DIR: str = os.environ.get(
    "CORTEX_CLI_DIR",
    _default_cli_dir(),
)
_SKILL_CLI: str = os.environ.get("CORTEX_SKILL_CLI", str(Path(_CLI_DIR) / "cortex-skill"))

# A bounded wall-clock cap for the install shellout — a clone can take a moment, but a hung
# git/network must not block the request forever. Env-overridable.
_INSTALL_TIMEOUT_S: float = float(os.environ.get("CORTEX_SKILL_INSTALL_TIMEOUT_S", "120"))


def get_cortex(request: Request):
    """Resolve the Cortex client for the skills read/bind — `app.state.cortex`, or None if
    not wired (degraded). Resolved at this seam so the handlers can be driven directly with
    a fake (the module test idiom)."""
    return getattr(request.app.state, "cortex", None)


def _clean(value: Any) -> str:
    """A stripped string, or '' for None/blank. Total + pure."""
    return str(value).strip() if value is not None else ""


def _install_env(project: str) -> dict[str, str]:
    """The subprocess env for the `cortex-skill install` shellout: the inherited env with
    the CLI dir on PATH (so `cortex-skill`'s sibling helpers resolve) + `CORTEX_PROJECT`
    set to the selected project (so the CLI registers against it). Mirrors the watchdog's
    `_WD_ENV` + the `{**env, CORTEX_PROJECT: project}` overlay it uses for every CLI call.

    `GIT_TERMINAL_PROMPT=0` so a clone that hits an auth wall fails fast instead of
    hanging on a username/password prompt that no one can answer (the request would
    otherwise block until the install timeout)."""
    return {
        **os.environ,
        "PATH": os.environ.get("PATH", "") + os.pathsep + _CLI_DIR,
        "CORTEX_PROJECT": project,
        "GIT_TERMINAL_PROMPT": "0",
        # Pin git to https only: even if the URL parse/SSRF check were fooled, the clone
        # can't be downgraded to file://, git://, ssh:// or ext:: (RCE/exfil protocols).
        "GIT_ALLOW_PROTOCOL": "https",
    }


def _run_install(url: str, scope: str, project: str) -> tuple[bool, str | None]:
    """Shell out to `cortex-skill install <url> [--scope <scope>]` (a LIST argv — no shell,
    no injection) and return (ok, error). On a 0 exit → (True, None); on a non-zero exit /
    timeout / spawn failure → (False, "<reason>") so the caller surfaces a friendly error.
    NEVER raises — a CLI failure must degrade, not 500 the route."""
    argv = [_SKILL_CLI, "install", url]
    if scope in _SCOPES:
        argv += ["--scope", scope]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_S,
            env=_install_env(project),
        )
    except subprocess.TimeoutExpired:
        return False, f"Installing the skill timed out after {int(_INSTALL_TIMEOUT_S)}s."
    except (OSError, ValueError) as exc:
        return False, f"Couldn't run the cortex-skill installer: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        # Surface a bounded slice of the CLI's own error (the clone/parse/register reason).
        detail = err.splitlines()[-1] if err else ""
        return False, (
            "The skill installer failed"
            + (f": {detail[:200]}" if detail else f" (exit {proc.returncode}).")
        )
    return True, None


def _validate_install_url(url: str) -> str | None:
    """Vet an install source BEFORE it reaches `git clone`. Returns an error string to
    reject, or None to allow.

    A skill install clones an agent-supplied URL on the deployment box, so an unvetted
    value is an SSRF lever. Policy:
      * an https:// URL whose host (lowercased, trailing-dot-stripped) is in the
        forge allowlist OR ends with `.github.com` — AND whose host does not DNS-resolve
        to a private / loopback / link-local / reserved / metadata address
        (`_ssrf_blocked`, the same guard web_fetch uses);
      * a local filesystem path that EXISTS (the dev/test workflow);
      * everything else is rejected (http://, git@…, ssh://, file://, a non-allowlisted
        host) with a clear reason."""
    raw = (url or "").strip()
    if not raw:
        return "A GitHub URL (or local path) is required."

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()

    if scheme == "https":
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            return "That URL has no host."
        if host not in _INSTALL_HOST_ALLOWLIST and not host.endswith(".github.com"):
            return (
                f"'{host}' is not an allowed skill source. Install from "
                "github.com, gitlab.com, or bitbucket.org."
            )
        reason = _ssrf_blocked(raw)
        if reason:
            return f"Refused — that URL resolves to {reason}."
        return None

    # No https scheme: allow a bare local filesystem path only if it actually exists
    # (dev/test). Anything with another scheme (http/git/ssh/file/…) is rejected.
    if not scheme and os.path.exists(raw):
        return None

    return (
        "Only https:// URLs from github.com / gitlab.com / bitbucket.org, or an "
        "existing local path, can be installed."
    )


# ---------------------------------------------------------------------------
#  GET /skills/{project} → get_skills  (read — the catalogue)
# ---------------------------------------------------------------------------


@router.get("/{project}")
async def list_skills_route(
    project: str,
    cortex: Any = Depends(get_cortex),
) -> dict[str, Any]:
    """`GET /skills/{project}` — the skills catalogue for `project`: every GLOBAL skill plus
    this project's own project/agent-scoped skills, via `CortexClient.get_skills` (`GET
    /skills`). Returns `{skills: [...]}` (each row: slug · name · description · scope ·
    version · status · ...). A down/None Cortex degrades to `{skills: []}` — never a 500."""
    if cortex is None:
        return {"skills": []}
    try:
        skills = await cortex.get_skills(_clean(project))
    except Exception:  # the client graceful-degrades to []; belt-and-braces here
        skills = []
    return {"skills": skills if isinstance(skills, list) else []}


# ---------------------------------------------------------------------------
#  POST /skills/{project}/install → cortex-skill install  (write — shellout)
# ---------------------------------------------------------------------------


@router.post("/{project}/install")
async def install_skill_route(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    cortex: Any = Depends(get_cortex),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    """`POST /skills/{project}/install` — install a skill from a GitHub URL (or local path)
    by shelling out to `cortex-skill install <url> [--scope <scope>]` with `CORTEX_PROJECT`
    set to `project`. The CLI clones/copies the source, parses its SKILL.md, registers it via
    Cortex, and copies it into the shared skills repo. On success the REFRESHED catalogue is
    returned so the SPA re-renders without a second call: `{ok, error, skills}`. A blank url
    is a friendly `ok=false` WITHOUT spawning; a non-zero CLI exit / timeout / spawn failure
    is a soft `ok=false` + a clear error. Never a 500."""
    url = _clean(payload.get("url"))
    scope = _clean(payload.get("scope")).lower()
    if not url:
        return {"ok": False, "error": "A GitHub URL (or local path) is required.", "skills": []}

    # SSRF gate: vet the source BEFORE spawning the cloning installer. On rejection we
    # return the reason WITHOUT shelling out (no clone of an internal/metadata target).
    url_error = _validate_install_url(url)
    if url_error:
        return {"ok": False, "error": url_error, "skills": []}

    ok, error = _run_install(url, scope, _clean(project))

    # Refresh the catalogue regardless (so the tab shows current state even after a partial
    # failure); the read graceful-degrades to [] on its own.
    skills: list[dict[str, Any]] = []
    if cortex is not None:
        try:
            fetched = await cortex.get_skills(_clean(project))
            skills = fetched if isinstance(fetched, list) else []
        except Exception:
            skills = []
    return {"ok": ok, "error": error, "skills": skills}


# ---------------------------------------------------------------------------
#  POST /skills/{project}/{slug}/bind → bind_skill  (write — deliver to subject)
# ---------------------------------------------------------------------------


@router.post("/{project}/{slug}/bind")
async def bind_skill_route(
    project: str,
    slug: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    cortex: Any = Depends(get_cortex),
    _admin: Any = Depends(auth_module.require_admin_if_auth),
) -> dict[str, Any]:
    """`POST /skills/{project}/{slug}/bind` — deliver a registered skill to a subject via
    `CortexClient.bind_skill` (`POST /skills/{slug}/bind`). The body is `{subject,
    subject_kind?}` (`subject_kind` defaults to `role`; pass `agent` to bind a single
    agent). Echoes `{ok, slug, subject, error}`; a blank subject is a friendly `ok=false`
    WITHOUT a call, and a degraded write (Cortex unreachable / the console's writer isn't
    authorised) is a soft `ok=false` + a clear error. Never a 500."""
    sl = _clean(slug)
    subject = _clean(payload.get("subject"))
    kind = _clean(payload.get("subject_kind")).lower() or "role"
    if not subject:
        return {"ok": False, "slug": sl, "subject": None,
                "error": "Pick an agent or role to assign this skill to."}
    if cortex is None:
        return {"ok": False, "slug": sl, "subject": subject,
                "error": "Cortex is unavailable — couldn't reach the registry to bind the skill."}

    try:
        result = await cortex.bind_skill(
            _clean(project), sl, {"subject": subject, "subject_kind": kind}
        )
    except Exception:  # the client graceful-degrades to None; belt-and-braces here
        result = None

    if not result:
        return {
            "ok": False,
            "slug": sl,
            "subject": subject,
            "error": (
                "Cortex didn't bind the skill. The console's writer may not be authorised "
                "to bind skills on this project, or Cortex is unreachable."
            ),
        }
    return {"ok": True, "slug": sl, "subject": subject, "error": None}


__all__ = [
    "router",
    "list_skills_route",
    "install_skill_route",
    "bind_skill_route",
    "get_cortex",
]
