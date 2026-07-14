"""Settings API — the imperative shell for the `settings` module.

A FastAPI `APIRouter` exposing the operational settings surface as typed JSON. This
is the ONLY part of the module that imports fastapi (the layer rule: the service is
pure; the shell does I/O + wiring). Three read endpoints:

  * `GET /settings/{project}/app`                    — the app/system settings map
                                                       + store-liveness,
  * `GET /settings/{project}/agents/{agent}/config`  — one agent's resolved override
                                                       + effective designation,
  * `GET /settings/{project}/flags`                  — the project's autonomy +
                                                       propose-mode flags.

Each endpoint resolves the `OperationalStorePort` from `app.state` (the adapter the
app wired at startup) via `Depends` — so the route depends on the PORT, not the
concrete store — constructs the `SettingsService` over it, and returns the shaped
JSON.

`main.py` mounts this additively (`app.include_router(settings_module.router)`); the
existing HTML System page + Configure card + the inline agent-config save delegate
their config substance to the SAME `SettingsService`, so the JSON API and the HTML
surfaces share one source of the config logic.

CRITICAL routing-collision guard (per the agents carve): the module's JSON routes
use a distinct `/settings/{project}/...` shape — TWO-plus segments — so
they can NEVER shadow:
  * the existing one-segment HTML tab route `GET /settings/{page}` (a one-segment
    request like `/settings/system` never matches a two-segment route, and a
    two-segment request like `/settings/<project>/app` never matches `{page}`),
  * the LIVE HTML `POST /settings/...` routes, which ALL carry a LITERAL first
    segment under `/settings/` (`/settings/system`, `/settings/system/...`,
    `/settings/configure`, `/settings/projects/{project_key}/folder`) — none is the
    `{project}/{leaf}` JSON shape, so a literal `system`/`configure` can't match the
    `{project}` param for the `app`/`flags`/`agents/...` leaves (different segment
    count, or a fixed literal vs a param), nor
  * the LIVE `POST /agents/{p}/{a}/config` + `POST /agents/{p}/{a}/chat` routes
    (a different root `/agents/...` AND, for the read GETs, a different method) —
    this router owns NO `/agents/...` path.
The router is also registered BEFORE the HTML routes (FastAPI matches the
first-registered route for a given shape), so the additive mount is strictly safe.

Matching WRITE endpoints (Track C — the SPA settings write path) reuse the SAME
`/settings/{project}/...` JSON shape but with POST, delegating to the
`OperationalStorePort` setters via the service: `POST /settings/{project}/flags`
(autonomy + propose-mode), `POST /settings/{project}/app` (upsert app/system
settings), and `POST /settings/{project}/agents/{agent}/config` (save one agent's
CONSOLE-LOCAL override — the registry is NOT touched on save). Each echoes the
authoritative post-write state (an `ok` flag + the re-read value).

A sibling EXPLICIT action, `POST /settings/{project}/agents/{agent}/promote`, is the
ONLY path that writes the Cortex registry from this surface: it pushes one agent's
current effective override into the registry on demand (the "Promote to registry"
button), returning a graceful `{ok, error?}`. Overrides are console-local by default
(feature-gap #81, the CTO's reversed decision); promotion is a deliberate commit.

Graceful-degrade rides through from the service/store: a down store yields the
empty app-settings map / `{}` override / fail-safe-OFF flags on a read, and an
`ok=false` + the same fail-safe state on a write — never a 500."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import socket
from typing import Any, Awaitable, Callable, Optional, Protocol
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, Request

from app import auth as auth_module
from app import platform_config
from app.domain.ports import ModelCatalogPort, OperationalStorePort
from app.settings_module import service as settings_service
from app.settings_module.service import SettingsService

router = APIRouter(prefix="/settings", tags=["settings"])

# All settings WRITE routes are admin-gated (enterprise): require_admin_if_auth no-ops
# when auth is OFF (dev mode stays open) and enforces admin when auth is ON. Closes the
# privilege-escalation where any logged-in user could flip flags / save configs / promote
# to the registry / add provider URLs / move the workspace.
_ADMIN = Depends(auth_module.require_admin_if_auth)

_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata"}
_SETTINGS_IO_LOCK = asyncio.Lock()


async def _settings_io(fn: Callable[[], Any]) -> Any:
    """Run sync settings-store work off the ASGI loop and serialize it.

    The operational settings store is backed by a shared sync psycopg connection.
    Calling it directly from async handlers starves the single uvicorn event loop
    when app-DB is slow; calling it concurrently from worker threads can also race
    the shared connection. This funnel prevents both failure modes.
    """
    async with _SETTINGS_IO_LOCK:
        return await asyncio.to_thread(fn)


def _provider_url_blocked(base_url: str) -> Optional[str]:
    """SSRF guard for an operator-supplied provider `base_url`: block ONLY the
    cloud-metadata endpoint (the classic SSRF target — 169.254.169.254 / link-local),
    and ALLOW localhost + private LAN so legitimate local LLM servers (ollama, vLLM,
    LM Studio) still work. Returns a reason string when blocked, else None."""
    if not base_url:
        return None
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return "invalid provider URL"
    if not host:
        return None
    if host in _METADATA_HOSTS:
        return "refusing a cloud-metadata provider URL"
    try:
        for info in socket.getaddrinfo(host, None):
            if ipaddress.ip_address(info[4][0]).is_link_local:  # 169.254/16, fe80::/10
                return "refusing a link-local (cloud-metadata) provider URL"
    except Exception:
        pass  # unresolvable host → let the downstream call fail naturally
    return None


def get_operational_store(request: Request) -> OperationalStorePort:
    """Resolve the `OperationalStorePort` for the request.

    Prefers a pre-wired `app.state.opstore` (an `AppDbOperationalStore`); falls back
    to wrapping the live `app.state.appdb` so the route works even before the app
    explicitly stashes the adapter. Constructed at this composition seam so callers
    receive the PORT, never the concrete store (mirrors the analytics/agents
    resolver)."""
    state = request.app.state
    store = getattr(state, "opstore", None)
    if store is not None:
        return store
    from app.adapters.opstore import AppDbOperationalStore

    return AppDbOperationalStore(appdb=state.appdb)


def build_service(store: OperationalStorePort) -> SettingsService:
    """Construct the settings service over the port (the operational data source)."""
    return SettingsService(store=store)


# ---------------------------------------------------------------------------
#  Composition seams for the NEW [API]-gap endpoints (step 3a). Each resolves the
#  CONCRETE dependency HERE (the shell is allowed I/O); the handler takes it as a
#  parameter (so it can be driven directly with a fake — the module test idiom) and
#  delegates the SHAPING to the pure service helpers. The pure service never imports
#  any of these (the module-isolation contract holds — proven by the guard test).
#
#  The four small ports/callables the new endpoints need (no app-wide Protocol
#  exists for these legacy surfaces yet, so they're scoped narrowly here):
#    * the System SCHEMA (the field/group/type/secret definitions) — sourced from
#      the legacy `app.settings` facade (a UI/schema concern), masked by the service.
#    * the model catalog — the existing `ModelCatalogPort` adapter.
#    * the custom-provider store — `app.settings`'s add/remove/view helpers.
#    * the key-test probe — `app.provider_check.test_provider`.
#    * the repo_root admin client — `app.state.cortex` (the one admin-authed call).
# ---------------------------------------------------------------------------


class CustomProviderStorePort(Protocol):
    """The narrow custom-provider surface the JSON mirrors need (the SAME methods
    the legacy `app.settings` facade exposes + the HTML routes use). Add/remove +
    a MASKED view (the raw api_key never echoed)."""

    def add_custom_provider(self, name: str, base_url: str, api_key: str) -> dict: ...
    def remove_custom_provider(self, provider_id: str) -> bool: ...
    def view_custom_providers(self) -> list[dict]: ...


class RepoRootClientPort(Protocol):
    """The one admin-authed call the workspace mirror needs (PATCH a project's
    repo_root). The token is sourced + sent SERVER-SIDE and never returned."""

    async def set_project_repo_root(self, project_key: str, repo_root: str) -> dict: ...


class ProviderConfigSourcePort(Protocol):
    """The narrow built-in-provider config source the Providers-config view needs:
    given the current settings values, return the per-provider key-presence + label
    + test target — NEVER a raw key. (The SAME info `providers.builtin_provider_config`
    computes from the store/env.)"""

    def builtin_provider_config(self, values: dict) -> list[dict]: ...


def get_system_schema() -> list[dict[str, Any]]:
    """The raw System SCHEMA (groups → fields) — sourced from the legacy
    `app.settings` facade (the schema/form is a UI concern, NOT port logic). The
    service masks secrets on top of this. Imported at the seam so the pure service
    never depends on `app.settings`."""
    from app import harness as harness_module
    from app import settings as settings_store

    schema = copy.deepcopy(settings_store.SCHEMA)
    visible_harnesses = [row["value"] for row in harness_module.harness_options()]
    for group in schema:
        for field in group.get("fields") or []:
            if field.get("key") == "harness_default":
                field["options"] = visible_harnesses
    return schema


def get_model_catalog(request: Request) -> ModelCatalogPort:
    """Resolve the `ModelCatalogPort` (the live provider/model catalog). Prefers a
    pre-wired `app.state.model_catalog`; else constructs the thin `ProvidersModelCatalog`
    adapter (stateless — it wraps the module-level `providers` cache)."""
    cat = getattr(request.app.state, "model_catalog", None)
    if cat is not None:
        return cat
    from app.adapters.model_catalog import ProvidersModelCatalog

    return ProvidersModelCatalog()


def get_custom_provider_store() -> CustomProviderStorePort:
    """Resolve the custom-provider store — the legacy `app.settings` facade
    (the SAME app-DB-backed store the live HTML custom-provider routes persist into)."""
    from app import settings as settings_store

    return settings_store


def get_provider_config_source() -> ProviderConfigSourcePort:
    """Resolve the built-in-provider config source — the `providers` module (the
    SAME place the catalog reads provider key-presence from the store/env/.env).
    Imported at the seam so the pure service never depends on `app.providers`."""
    from app import providers as providers_mod

    return providers_mod


def get_key_test() -> Callable[..., Awaitable[dict]]:
    """Resolve the provider key-test probe — `provider_check.test_provider` (the
    SAME read-only probe the live HTML test-key route uses; never spends tokens)."""
    from app import provider_check

    return provider_check.test_provider


def get_repo_root_client(request: Request) -> RepoRootClientPort:
    """Resolve the repo_root admin client — `app.state.cortex` (the one admin-authed
    PATCH path; the token is sourced + sent backend-only and never exposed)."""
    return request.app.state.cortex


def get_registry_sync_client(request: Request):
    """Resolve the Cortex client for the EXPLICIT override→registry PROMOTE (feature-gap
    #81) — `app.state.cortex`, or None if not wired (the read-only/degraded path).

    Promotion is BEST-EFFORT: a None client (or any write failure) just means the
    promote endpoint reports a soft `{ok:false}`; the console-local override is
    untouched. Resolved at this seam so the handler can be driven directly with a fake
    (the module test idiom)."""
    return getattr(request.app.state, "cortex", None)


def _raw_editor_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Shape the map the raw App-settings editor surfaces: keep ONLY the genuinely-
    extra keys. Delegates to `app.settings.surface_extra_settings` (the ONE place the
    exclusion set lives), which drops the typed SCHEMA fields (shown in the System
    form — no duplication), the provider secrets (owned by the Providers tab), the
    retired keys, and the internal/structural blobs (`agent_overrides`,
    `custom_providers`, and `_`-prefixed markers like `_designation_seed_applied`). A
    degraded import returns the map unchanged (belt-and-braces — never a 500). The
    STORE is untouched; this is a SURFACE filter only."""
    try:
        from app import settings as settings_store

        return settings_store.surface_extra_settings(settings)
    except Exception:  # pragma: no cover - app.settings always imports here
        return settings


_ORCHESTRATOR_ROLE = "orchestrator"


def _is_orchestrator_role(role: Any) -> bool:
    return str(role or "").strip().lower() == _ORCHESTRATOR_ROLE


def _override_agent_from_key(project: str, key: str) -> str | None:
    prefix = f"{(project or '').strip().lower()}:"
    if not key.startswith(prefix):
        return None
    return key[len(prefix):].strip().lower() or None


async def _orchestrator_role_conflict(
    *,
    cortex: Any,
    project: str,
    agent: str,
    override: dict[str, Any],
    overrides: dict[str, dict[str, str]],
) -> str | None:
    """Return the conflicting orchestrator agent name, if a save would create two."""
    if not _is_orchestrator_role(override.get("role")):
        return None

    target = (agent or "").strip().lower()
    for key, entry in (overrides or {}).items():
        other = _override_agent_from_key(project, str(key))
        if other and other != target and _is_orchestrator_role(entry.get("role")):
            return other

    get_agents = getattr(cortex, "get_agents", None)
    if not callable(get_agents):
        return None
    try:
        roster = await get_agents(project)
    except Exception:
        return None

    for row in roster or []:
        name = str((row or {}).get("name") or "").strip().lower()
        if not name or name == target:
            continue
        local_role = (overrides or {}).get(settings_service.override_store_key(project, name), {}).get("role")
        effective_role = local_role if local_role is not None else (row or {}).get("role")
        if _is_orchestrator_role(effective_role):
            return name
    return None


def _live_cortex_base_url() -> str:
    """The ACTUAL Cortex API base URL the console talks to (env-resolved at startup) —
    surfaced in the readonly System field so it reflects the REAL connection (on a VM the
    in-network DSN, not the localhost default), never a stored-but-ignored value. Empty
    string if it can't be read (belt-and-braces — never a 500)."""
    try:
        from app.cortex_client import CORTEX_BASE_URL

        return str(CORTEX_BASE_URL or "")
    except Exception:  # pragma: no cover - cortex_client always imports here
        return ""


def _inject_readonly_value(payload: dict[str, Any], key: str, value: str) -> None:
    """Override one field's `value` in a built system-schema payload — used to surface a
    LIVE value for a readonly/informational field. A no-op for a blank value or an absent
    field. Mutates `payload` in place."""
    if not value:
        return
    for g in payload.get("groups", []):
        for f in g.get("fields", []):
            if f.get("key") == key:
                f["value"] = value
                return


@router.get("/{project}/app")
async def app_settings_endpoint(
    project: str,
    store: OperationalStorePort = Depends(get_operational_store),
) -> dict[str, Any]:
    """`GET /settings/{project}/app` — the app/system settings (the raw key→value
    rows behind the System page) + store-liveness, as JSON. Includes `project` in
    the payload. A down store yields an empty map with `store_connected=false`
    (never a 500).

    CANONICALIZATION (Track 2): the provider-credential keys are FILTERED OUT of the
    surfaced map — the raw App-settings editor must not expose them (the Providers
    tab is their single home). The underlying store still holds them; only the
    surfaced keys are filtered."""
    svc = build_service(store)
    settings, connected = await _settings_io(
        lambda: (_raw_editor_settings(svc.load_app_settings()), bool(store.available()))
    )
    return {
        "project": project,
        "settings": settings,
        "store_connected": connected,
    }


@router.get("/{project}/agents/{agent}/config")
async def agent_config_endpoint(
    project: str,
    agent: str,
    store: OperationalStorePort = Depends(get_operational_store),
) -> dict[str, Any]:
    """`GET /settings/{project}/agents/{agent}/config` — one agent's console-local
    override (harness/model/reasoning/designation/role) + its effective designation
    (override-first), as JSON. Includes `project` + `agent`. An agent with no
    override yields an empty `override` + `designation=""` (the caller falls back to
    the registry value) — never a 500.

    PATH NOTE (collision-free): this lives under `/settings/...`, NOT `/agents/...`,
    so it can never shadow the LIVE `POST /agents/{project}/{agent}/config` (the
    inline-header save) — different root AND different method."""
    svc = build_service(store)
    override, designation = await _settings_io(
        lambda: (svc.get_override(project, agent), svc.resolve_designation(project, agent))
    )
    return {
        "project": project,
        "agent": agent,
        "override": override,
        "designation": designation,
    }


@router.get("/{project}/flags")
async def flags_endpoint(
    project: str,
    store: OperationalStorePort = Depends(get_operational_store),
) -> dict[str, Any]:
    """`GET /settings/{project}/flags` — the project's operational kill-switches:
    `autonomous` (autonomous dispatch) + `propose_mode` (the training-wheels
    approval gate), as JSON. Both fail-safe OFF (a no-row / down store reads
    False) — never a 500."""
    svc = build_service(store)
    autonomous, propose_mode = await _settings_io(
        lambda: (svc.is_project_autonomous(project), svc.is_propose_mode(project))
    )
    return {
        "project": project,
        "autonomous": autonomous,
        "propose_mode": propose_mode,
    }


# ---------------------------------------------------------------------------
#  WRITE endpoints (Track C — the SPA settings write path)
#
#  The collision-free JSON write side: the SAME `/settings/{project}/...` shape as
#  the reads, but POST, each delegating to the service (which delegates to the
#  `OperationalStorePort` setters). Each returns the AUTHORITATIVE post-write state
#  (an `ok` flag + the value re-read through the service) so the SPA refetch-on-
#  success lands on the truth. A down store reports `ok=false` + the fail-safe state
#  (never a 500) — the same house-law graceful-degrade as the reads.
#
#  Body shape: each parses a small JSON object (the `payload` body param). FastAPI
#  parses the request body into it; the tests drive the handlers directly with the
#  same dict (no ASGI), the established module idiom.
# ---------------------------------------------------------------------------


@router.post("/{project}/flags")
async def set_flags_endpoint(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: OperationalStorePort = Depends(get_operational_store),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """`POST /settings/{project}/flags` — set the project's `autonomous` and/or
    `propose_mode` kill-switches (the same effect as the live HTML autonomy toggle,
    just JSON). Only the flags PRESENT in the body are written (an omitted flag is
    left untouched, so the SPA can toggle one switch without clobbering the other);
    each present flag is coerced to bool and persisted through the port's setter.
    Echoes `{project, autonomous, propose_mode, ok}` re-read AFTER the write
    (authoritative). `ok` is the AND of the writes attempted; a down store / blank
    project → `ok=false` + fail-safe-OFF flags (never a 500)."""
    svc = build_service(store)
    def write_and_read() -> tuple[bool, bool, bool]:
        ok = True
        if "autonomous" in payload:
            ok = svc.set_project_autonomy(project, bool(payload["autonomous"]),
                                          updated_by="console-spa") and ok
        if "propose_mode" in payload:
            ok = svc.set_propose_mode(project, bool(payload["propose_mode"]),
                                      updated_by="console-spa") and ok
        return (
            bool(ok),
            svc.is_project_autonomous(project),
            svc.is_propose_mode(project),
        )

    ok, autonomous, propose_mode = await _settings_io(write_and_read)
    return {
        "project": project,
        "autonomous": autonomous,
        "propose_mode": propose_mode,
        "ok": bool(ok),
    }


@router.post("/{project}/app")
async def set_app_settings_endpoint(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: OperationalStorePort = Depends(get_operational_store),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """`POST /settings/{project}/app` — upsert app/system settings (the durable
    key→value rows behind the System page) through the port's `upsert_app_settings`.
    The body's `settings` object is the {key: value} map to upsert (a partial map —
    only the supplied keys are written). Echoes `{project, settings, store_connected,
    ok}` with the settings map re-read AFTER the write (authoritative). A down store
    / an empty map → `ok=false` + the empty/last map + `store_connected=false`
    (never a 500)."""
    svc = build_service(store)
    items = payload.get("settings")
    if not isinstance(items, dict):
        items = {}
    ok = svc.upsert_app_settings(items)
    # The echoed map is the raw-editor's authoritative refetch — filter the provider
    # secrets out of it too (they're owned by the Providers tab). The WRITE itself is
    # unfiltered: a provider key saved via the Providers tab still persists (the store
    # is untouched); this only shapes what the raw editor SEES.
    def write_and_read() -> tuple[bool, dict[str, Any], bool]:
        ok = svc.upsert_app_settings(items)
        return (
            bool(ok),
            _raw_editor_settings(svc.load_app_settings()),
            bool(store.available()),
        )

    ok, settings, connected = await _settings_io(write_and_read)
    return {
        "project": project,
        "settings": settings,
        "store_connected": connected,
        "ok": bool(ok),
    }


@router.post("/{project}/agents/{agent}/config")
async def save_agent_config_endpoint(
    project: str,
    agent: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: OperationalStorePort = Depends(get_operational_store),
    cortex: Any = Depends(get_registry_sync_client),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """`POST /settings/{project}/agents/{agent}/config` — save a console-local agent
    override (designation/harness/model/reasoning/role) via the service's
    `save_override` MERGE semantics: a non-blank field SETS it, a blank field CLEARS
    it (falls back to the registry value), designation is validated, and an agent
    left with no overrides is dropped. The body's `override` object carries the
    fields to merge. Echoes `{project, agent, override, designation, ok}` where
    `override` is the post-save EFFECTIVE entry and `designation` is the resolved
    override-first designation. A down store → `ok=false` + `{}` override (never a 500).

    CONSOLE-LOCAL BY DESIGN (feature-gap #81, the CTO's reversed decision): a save
    writes ONLY the console-local override — it does NOT touch the Cortex registry (the
    registry stays authoritative). Committing the config to the registry is an EXPLICIT,
    on-demand gesture via the separate `POST /settings/{project}/agents/{agent}/promote`
    endpoint (the "Promote to registry" button), never automatic on save.

    PATH NOTE (collision-free): lives under `/settings/...`, NOT `/agents/...`, so it
    can never shadow the LIVE `POST /agents/{project}/{agent}/config` (the inline-
    header save) — a different root."""
    svc = build_service(store)
    override = payload.get("override")
    if not isinstance(override, dict):
        override = {}

    conflict = await _orchestrator_role_conflict(
        cortex=cortex,
        project=project,
        agent=agent,
        override=override,
        overrides=svc.load_overrides(),
    )
    if conflict:
        return {
            "project": project,
            "agent": agent,
            "override": svc.get_override(project, agent),
            "designation": svc.resolve_designation(project, agent),
            "ok": False,
            "error": (
                f"Only one deterministic orchestrator is allowed per project. "
                f"Unset '{conflict}' as orchestrator before assigning '{agent}'."
            ),
        }

    def write_and_read() -> tuple[dict[str, str], str, bool]:
        effective = svc.save_override(project, agent, override)
        return (
            effective,
            svc.resolve_designation(project, agent),
            bool(store.available()),
        )

    effective, designation, connected = await _settings_io(write_and_read)

    return {
        "project": project,
        "agent": agent,
        "override": effective,
        "designation": designation,
        "ok": connected,
        "error": None if connected else "settings store unavailable",
    }


@router.post("/{project}/agents/{agent}/promote")
async def promote_agent_config_endpoint(
    project: str,
    agent: str,
    store: OperationalStorePort = Depends(get_operational_store),
    cortex: Any = Depends(get_registry_sync_client),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """`POST /settings/{project}/agents/{agent}/promote` — the EXPLICIT "Promote to
    registry" action (feature-gap #81). Pushes the agent's CURRENT effective console
    override (harness/model/reasoning/writer_scope) + its current role INTO the Cortex
    registry on demand via `registry_sync.promote_agent_to_registry` (`POST /agents`
    UPSERT — the conflict-update jsonb-MERGES `capabilities`, so the config persists
    additively). Does NOT mutate the console-local override (read-only over it).

    Returns `{ok, error?}`: `ok=true` when the registry write landed; a GRACEFUL
    `{ok:false, error}` when it didn't (no cortex wired / the console's caller isn't a
    registered writer / Cortex unreachable / an unresolvable role). NEVER a 500, and the
    admin/writer credential is NEVER echoed.

    PATH NOTE (collision-free): the distinct `promote` LEAF under `/settings/{project}/
    agents/{agent}/...` can't shadow the sibling `config` save (a different trailing
    segment) nor the live `/agents/...` routes (a different URL prefix)."""
    svc = build_service(store)
    # Promotion pushes the CURRENT effective console override (no save required first).
    effective = svc.get_override(project, agent)
    ok = await _promote_override_to_registry(cortex, project, agent, effective)
    if ok:
        return {"ok": True, "error": None}
    return {
        "ok": False,
        "error": (
            "Couldn't promote to the registry. Promotion needs a reachable Cortex and "
            "the console's writer authorised to register agents on this project (and the "
            "agent must have a resolvable role)."
        ),
    }


async def _promote_override_to_registry(
    cortex: Any, project: str, agent: str, effective: dict[str, Any]
) -> bool:
    """Best-effort: re-register `agent` in the Cortex registry carrying its CURRENT
    EFFECTIVE override (harness/model/reasoning/writer_scope) + its current role — the
    explicit-promote write.

    Resolves the agent's current registry record (role/capabilities) from the live
    roster (`cortex.get_agents`) for the role + the additive capability merge, then
    delegates the write to `registry_sync.promote_agent_to_registry` (which itself
    swallows every failure). Returns the soft success flag. NEVER raises — a None
    cortex, an empty roster, or any error degrades to False so the promote endpoint
    reports `{ok:false}` rather than 500-ing. Imported lazily to keep the module's
    import graph flat (the pure service never imports this shell glue)."""
    if cortex is None:
        return False
    try:
        from app.registry_sync import promote_agent_to_registry

        roster = await cortex.get_agents(project)
        record = _find_roster_agent(roster, agent)
        return await promote_agent_to_registry(
            cortex, project, agent, effective, record
        )
    except Exception:  # belt-and-braces — promote must never raise out of the endpoint
        return False


def _find_roster_agent(roster: Any, agent: str) -> Optional[dict]:
    """Case-insensitive lookup of one agent record in a roster list ({} fields
    tolerated). Returns the record or None. Pure + total."""
    name = (agent or "").strip().lower()
    if not name or not isinstance(roster, list):
        return None
    for row in roster:
        if isinstance(row, dict) and str(row.get("name") or "").strip().lower() == name:
            return row
    return None


# ===========================================================================
#  STEP 3a — the [API]-gap endpoints that unblock the SPA settings tabs.
#
#  HTML-only today (the legacy console exposes them; the SPA can't reach them with
#  no JSON endpoint). Each is ADDITIVE + collision-free under the module's
#  `/settings/{project}/...` JSON shape (a distinct LEAF per endpoint — never a
#  literal first segment, so it can't shadow the live HTML `POST /settings/system…`
#  / `/settings/projects/…` / `GET /settings/{page}` routes), and goes through the
#  ports/service helpers (the I/O is resolved at the seams above; the shaping +
#  secret-masking is the pure service). Every one graceful-degrades — never a 500.
# ===========================================================================


@router.get("/{project}/system-schema")
async def system_schema_endpoint(
    project: str,
    store: OperationalStorePort = Depends(get_operational_store),
    schema: list[dict[str, Any]] = Depends(get_system_schema),
) -> dict[str, Any]:
    """`GET /settings/{project}/system-schema` — the System form as JSON: typed
    groups → fields, each with its CURRENT value EXCEPT SECRETS.

    A secret field returns ONLY `is_set` (bool) + a masked placeholder — the raw
    secret value is NEVER in the response (the load-bearing contract — a test
    asserts no raw secret ever appears). The current values come from the store
    (the durable app_settings rows behind the System page); a missing/down store
    falls back to each field's schema DEFAULT (the form still renders) — never a
    500. Includes `project` + `store_connected`."""
    svc = build_service(store)
    values, connected = await _settings_io(
        lambda: (svc.load_app_settings(), bool(store.available()))
    )  # {} when down → build_system_schema uses defaults
    payload = settings_service.build_system_schema(schema, values)
    # cortex_base_url is READONLY + informational — show the ACTUAL Cortex API the console
    # talks to (env-resolved at startup), never a stored-but-ignored value. On a VM this is
    # the in-network DSN, not the localhost default; surfacing the real one is the honest read.
    _inject_readonly_value(payload, "cortex_base_url", _live_cortex_base_url())
    payload["project"] = project
    payload["store_connected"] = connected
    return payload


@router.get("/{project}/providers")
async def providers_endpoint(
    project: str,
    refresh: bool = False,
    catalog: ModelCatalogPort = Depends(get_model_catalog),
) -> dict[str, Any]:
    """`GET /settings/{project}/providers` — the live model catalog grouped by
    provider: `{providers:[{name, models:[{model,type,reasoning_tiers,
    input_price_per_mtok,output_price_per_mtok,context_window,source,freshness}]}]}`.

    Fetches via the `ModelCatalogPort` (which never raises — degrades to the
    cached/empty catalog). A fetch error here degrades to a PARTIAL/EMPTY catalog
    (`{providers: []}`) rather than a 500 — belt-and-braces over the port's own
    graceful-degrade. Includes `project`."""
    if refresh:
        # #131 — force a LIVE catalog re-fetch (bypass the ~15-min TTL), warming the
        # in-memory cache so the list below reads fresh data. Best-effort; the read
        # never blocks on it (a fetch failure just falls back to the existing cache).
        try:
            from app import providers as _providers_mod

            await _providers_mod.get_catalog(force=True)
        except Exception:
            pass
    try:
        models = await catalog.list_models()
    except Exception:  # the port shouldn't raise, but degrade to empty if it does
        models = []
    payload = settings_service.group_catalog_models(models)
    # EDITION gate: in the PUBLIC build the live catalog shows ONLY the visible providers
    # (Manifold) — so a leftover non-Manifold key can't surface its models here either.
    try:
        from app import providers as _providers_mod

        allowed = set(_providers_mod.visible_providers())
        if allowed != set(_providers_mod.PROVIDER_ORDER):
            payload["providers"] = [
                g for g in payload.get("providers", []) if g.get("name") in allowed
            ]
    except Exception:
        pass
    # #133 — attach each provider's account balance/credits. The balances were computed
    # in get_catalog() (cached + warmed by refresh above), so reuse them, no extra network.
    # Graceful: any issue → no balance field (the UI simply shows none for that provider).
    try:
        from app import providers as _providers_mod

        cat = await _providers_mod.get_catalog()
        bmap = {
            g.get("provider"): g.get("balance")
            for g in cat.get("groups", [])
            if g.get("balance")
        }
        for grp in payload.get("providers", []):
            bal = bmap.get(grp.get("name"))
            if bal:
                grp["balance"] = bal
    except Exception:
        pass
    payload["project"] = project
    return payload


@router.get("/{project}/providers/config")
async def providers_config_endpoint(
    project: str,
    store: OperationalStorePort = Depends(get_operational_store),
    cfg_source: ProviderConfigSourcePort = Depends(get_provider_config_source),
    custom_store: CustomProviderStorePort = Depends(get_custom_provider_store),
) -> dict[str, Any]:
    """`GET /settings/{project}/providers/config` — the CONFIGURED/ACTIVE providers
    for the Providers control surface: per-provider `{name, label, key_is_set,
    is_custom, testable, provider_ref, key_field?, base_url?}` — which providers have
    a key set + a Test target, NEVER a raw key.

    Combines the BUILT-IN providers' key-presence (from `cfg_source.builtin_provider_config`,
    scored against the durable app_settings rows — falling back to env/.env for the
    real running config) with the MASKED custom-provider list. This is the data the
    Providers tab renders so it can be the ONE home for provider keys/config (the
    System tab + the raw editor no longer carry them).

    The current values come from the store (the durable app_settings rows); a
    missing/down store still yields the built-in provider list (key-presence resolves
    via env/.env in the source) + the customs — `store_connected=false`, never a 500.
    The secret-masking contract holds: only `key_is_set` (a bool) + the masked custom
    display ever appear. Includes `project` + `store_connected`."""
    svc = build_service(store)
    values, customs, connected = await _settings_io(
        lambda: (
            svc.load_app_settings(),
            _safe_custom_view(custom_store),
            bool(store.available()),
        )
    )  # {} when down → the source falls back to env/.env
    try:
        built_ins = cfg_source.builtin_provider_config(values)
    except Exception:  # belt-and-braces — the source shouldn't raise; degrade if it does
        built_ins = []
    payload = settings_service.build_providers_config(built_ins, customs)
    payload["project"] = project
    payload["store_connected"] = connected
    return payload


@router.get("/{project}/license")
async def license_status_endpoint(project: str) -> dict[str, Any]:
    """`GET /settings/{project}/license` — the current license posture for the
    Settings → License panel: edition, validity, customer/expiry, and the resolved
    entitlements (unlocked harnesses + capacity caps). Read-only; never the raw token;
    never raises. DEV reports edition='dev' + all-permissive."""
    import math

    from app import edition
    from app import license as lic_mod

    st = lic_mod.license_status()
    ent = lic_mod.entitlements()
    advanced = {
        "manifold_access": ent.has_advanced("manifold_access"),
    }
    return {
        "project": project,
        "edition": edition.edition(),
        "required": st["required"],
        "valid": st["valid"],
        "reason": st["reason"],
        "customer": st.get("customer"),
        "expires": st.get("expires"),
        "features": st.get("features", []),
        "in_grace": ent.in_grace,
        "hard_gate": lic_mod.license_gate_status(surface="app"),
        "all_harnesses": "*" in ent.harnesses,
        "harnesses": sorted(h for h in ent.harnesses if h != "*"),
        "advanced": advanced,
        # JSON can't carry inf — unlimited caps (DEV / :unlimited) report as null.
        "limits": {k: (None if v == math.inf else int(v)) for k, v in ent.limits.items()},
    }


@router.post("/{project}/license/login")
async def license_login_endpoint(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: OperationalStorePort = Depends(get_operational_store),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """Password-login activation against the Kaidera AI platform license surface.

    The platform returns a narrow ``kaidera_os_license_session`` token. The backend stores
    that token, pulls the signed customer grant, and stores the platform-minted Manifold
    inference key only when the grant includes ``manifold_access``. Password/MFA values
    are never persisted or echoed.
    """
    from app import license_client

    svc = build_service(store)
    settings = await _settings_io(lambda: svc.load_app_settings())
    result = await license_client.login(
        str(payload.get("email") or ""),
        str(payload.get("password") or ""),
        mfa_code=str(payload.get("mfa_code") or "").strip() or None,
        settings=settings,
        save_settings=svc.upsert_app_settings,
    )
    return {"project": project, **result.to_dict()}


@router.post("/{project}/license/activate")
async def license_activate_endpoint(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: OperationalStorePort = Depends(get_operational_store),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """Activate this install against the Kaidera AI platform.

    This is the online counterpart to manual token import. It is intentionally soft:
    platform/network/signature/store failures return `{ok:false,error}` and never
    block the existing local grant/free tier.
    """
    from app import license_client

    svc = build_service(store)
    settings = await _settings_io(lambda: svc.load_app_settings())
    result = await license_client.activate(
        str(payload.get("org_login_token") or ""),
        settings=settings,
        save_settings=svc.upsert_app_settings,
    )
    return {"project": project, **result.to_dict()}


@router.post("/{project}/license/heartbeat")
async def license_heartbeat_endpoint(
    project: str,
    store: OperationalStorePort = Depends(get_operational_store),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """Refresh the stored grant against the Kaidera AI platform.

    Runs the same soft transport as the future background refresh loop; useful today
    for "Refresh now" and operator diagnostics before the platform service is live.
    """
    from app import license_client

    svc = build_service(store)
    settings = await _settings_io(lambda: svc.load_app_settings())
    result = await license_client.heartbeat(
        settings=settings,
        save_settings=svc.upsert_app_settings,
    )
    return {"project": project, **result.to_dict()}


@router.post("/{project}/license/restore")
@router.post("/{project}/license/enable")
@router.post("/{project}/license/expire")
async def license_customer_action_endpoint(
    project: str,
    request: Request,
    store: OperationalStorePort = Depends(get_operational_store),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """Run a platform customer license action (restore/enable/expire) via the stored
    license-session token, then refresh the local signed grant/key state."""
    from app import license_client

    action = request.url.path.rstrip("/").rsplit("/", 1)[-1]
    svc = build_service(store)
    settings = await _settings_io(lambda: svc.load_app_settings())
    result = await license_client.customer_action(
        action,
        settings=settings,
        save_settings=svc.upsert_app_settings,
    )
    return {"project": project, **result.to_dict()}


@router.get("/{project}/license/releases/{channel}")
async def license_releases_endpoint(
    project: str,
    channel: str,
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """Fetch advisory platform release metadata for the signed update path."""
    from app import license_client

    result = await license_client.releases(channel)
    return {"project": project, **result.to_dict()}


@router.get("/{project}/billing")
async def billing_status_endpoint(project: str, request: Request) -> dict[str, Any]:
    """`GET /settings/{project}/billing` — the Billing tab's view: per-entitlement USAGE
    (counted live from Cortex) vs the entitled TOTAL, plus the wallet balance + active
    add-ons from the grant. Read-only; never raises. Buying add-ons / topping up the wallet
    lives in the Kaidera AI cust-portal (`portal_url`), not here. Teams usage is implicit
    (one team per project) until teams are first-class."""
    import math

    from app import edition
    from app import license as lic_mod

    ent = lic_mod.entitlements()
    cortex = getattr(request.app.state, "cortex", None)

    projects_used: Optional[int] = None
    workers_used: Optional[int] = None
    users_used: Optional[int] = None
    try:
        if cortex is not None:
            projects = await cortex.get_projects() or []
            projects_used = len([p for p in projects if isinstance(p, dict)])
            roster = await cortex.get_roster(project) or []
            workers_used = len([a for a in roster if isinstance(a, dict)])
    except Exception:
        pass
    try:
        store = auth_module.get_auth_store(request)
        users_used = int(await store.count_users())
    except Exception:
        users_used = None

    def _total(kind: str) -> Optional[int]:
        v = ent.limit_for(kind)
        return None if v == math.inf else int(v)

    teams_used = 1 if projects_used else (0 if projects_used == 0 else None)
    entitlements = [
        {"kind": "projects", "label": "Projects", "addon": "addon:project",
         "used": projects_used, "total": _total("projects")},
        {"kind": "teams", "label": "AI Worker Teams", "addon": "addon:team",
         "used": teams_used, "total": _total("teams")},
        {"kind": "workers", "label": "AI Workers", "addon": "addon:worker",
         "used": workers_used, "total": _total("workers")},
        {"kind": "users", "label": "Users", "addon": "addon:user",
         "used": users_used, "total": _total("users")},
    ]
    return {
        "project": project,
        "edition": edition.edition(),
        "valid": ent.valid,
        "in_grace": ent.in_grace,
        "customer": ent.customer,
        "wallet": ent.wallet,            # {balance, currency, as_of} or null
        "addons": list(ent.addons),
        "harnesses": sorted(h for h in ent.harnesses if h != "*"),
        "all_harnesses": "*" in ent.harnesses,
        "entitlements": entitlements,
        "portal_url": platform_config.portal_url(),
    }


@router.post("/{project}/custom-providers")
async def custom_provider_add_endpoint(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: CustomProviderStorePort = Depends(get_custom_provider_store),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """`POST /settings/{project}/custom-providers` — add an operator-defined custom
    provider (`name` + `base_url` + `api_key`) via the SAME store the live HTML route
    persists into. Echoes `{project, ok, added, error, custom_providers}` where
    `custom_providers` is the refreshed MASKED list (the raw api_key is NEVER echoed
    — `has_key` + a masked display only). A blank name / write failure is a graceful
    `ok=false` + `error` (never a 500)."""
    name = str(payload.get("name") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()

    added: Optional[str] = None
    error: Optional[str] = None
    blocked = _provider_url_blocked(base_url)
    if not name:
        error = "A provider name is required."
    elif blocked:
        error = blocked  # SSRF guard: refuse a cloud-metadata URL before persisting it
    else:
        try:
            entry = await _settings_io(lambda: store.add_custom_provider(name, base_url, api_key))
            added = entry.get("name") if isinstance(entry, dict) else name
        except ValueError as exc:
            error = str(exc)
        except OSError as exc:
            error = f"write failed: {exc}"
        except Exception:  # belt-and-braces — a store hiccup degrades, never 500s
            error = "couldn't add the provider."

    return {
        "project": project,
        "ok": error is None,
        "added": added,
        "error": error,
        "custom_providers": await _settings_io(lambda: _safe_custom_view(store)),
    }


@router.post("/{project}/custom-providers/delete")
async def custom_provider_delete_endpoint(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    store: CustomProviderStorePort = Depends(get_custom_provider_store),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """`POST /settings/{project}/custom-providers/delete` — remove a custom provider
    by `id` (or `name`) via the SAME store the live HTML route uses. Echoes
    `{project, ok, removed, error, custom_providers}` with the refreshed masked list;
    `removed` is False when no row matched (still `ok=true` — a no-op, not a crash).
    A write failure is a graceful `ok=false` + `error` (never a 500)."""
    provider_id = str(payload.get("id") or payload.get("name") or "").strip()

    removed = False
    error: Optional[str] = None
    try:
        removed = bool(await _settings_io(lambda: store.remove_custom_provider(provider_id)))
    except OSError as exc:
        error = f"write failed: {exc}"
    except Exception:  # belt-and-braces
        error = "couldn't remove the provider."

    return {
        "project": project,
        "ok": error is None,
        "removed": removed,
        "error": error,
        "custom_providers": await _settings_io(lambda: _safe_custom_view(store)),
    }


def _safe_custom_view(store: CustomProviderStorePort) -> list[dict[str, Any]]:
    """The refreshed MASKED custom-provider list, or [] if the view can't be read
    (the raw api_key is never in this — the store's view masks it)."""
    try:
        return list(store.view_custom_providers())
    except Exception:  # pragma: no cover - belt-and-braces; the view shouldn't raise
        return []


@router.post("/{project}/provider-key-test")
async def provider_key_test_endpoint(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    key_test: Callable[..., Awaitable[dict]] = Depends(get_key_test),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """`POST /settings/{project}/provider-key-test` — probe a provider key and return
    `{project, ok, detail, status, label}`. Reuses the legacy READ-ONLY probe
    (`provider_check.test_provider`; lists models / key info — NEVER a completion, so
    it spends no tokens).

    Body: `provider` (a built-in secret-key field like `anthropic_api_key`, or
    `custom:<id>`) + EITHER `key` (test a freshly-typed key, pre-save feedback) OR
    nothing / `use_stored:true` (fall back to the stored key, then the env/.env key
    the harness actually runs with). The key is NEVER echoed back — only `ok` + a
    human `detail`. Never a 500 (the probe returns a structured result for every
    failure mode — bad key, unreachable, not testable)."""
    provider = str(payload.get("provider") or payload.get("field") or "").strip()
    # An explicit key tests that value; absence (or use_stored) → None = use the
    # stored/env key (the probe resolves it server-side). A masked sentinel is
    # treated as "no typed key" so the probe falls back to the stored secret.
    raw_key = payload.get("key")
    use_stored = bool(payload.get("use_stored"))
    if use_stored or raw_key is None:
        value: Optional[str] = None
    else:
        value = str(raw_key)
        if value.strip() in ("", settings_service.SECRET_MASK):
            value = None

    try:
        result = await key_test(provider, value)
    except Exception:  # the probe is graceful by design; degrade if it ever isn't
        result = {"ok": False, "status": "error",
                  "message": "couldn't run the key test.", "label": provider or "provider"}
    result = result if isinstance(result, dict) else {}
    return {
        "project": project,
        "ok": bool(result.get("ok")),
        "detail": result.get("message") or "",
        "status": result.get("status") or "",
        "label": result.get("label") or "",
    }


@router.post("/{project}/workspace")
async def workspace_endpoint(
    project: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    repo_client: RepoRootClientPort = Depends(get_repo_root_client),
    _admin: Any = _ADMIN,
) -> dict[str, Any]:
    """`POST /settings/{project}/workspace` — set a project's canonical working
    folder (`repo_root`) via the existing admin path (the in-app version of the
    repo_root CLI fix). The body's `repo_root` is the new ABSOLUTE path; the project
    set is `project_key` from the body, else the path `{project}` (the selected
    project the SPA is editing).

    Calls the ONE admin-authed Cortex method (`set_project_repo_root` → PATCH
    /projects/{key} with the `X-Cortex-Admin-Token` header). The token is sourced +
    sent SERVER-SIDE and NEVER exposed in the response. Echoes `{project,
    project_key, ok, repo_root, previous_repo_root, error}`. Every failure mode is a
    graceful `ok=false` + a clear `error` (never a 500):
      * blank / relative path      → 'must be an absolute path'
      * admin token not configured → 'admin token not configured' (nothing sent)
      * API / transport error      → the surfaced detail."""
    target_key = str(payload.get("project_key") or project or "").strip()
    new_root = str(payload.get("repo_root") or "").strip()  # fitness:allow-literal "repo_root" is the Cortex field name (the literals gate's `root` agent-name pattern is a false positive here)

    repo_root: Optional[str] = None
    previous: Optional[str] = None
    error: Optional[str] = None
    try:
        result = await repo_client.set_project_repo_root(target_key, new_root)
        result = result if isinstance(result, dict) else {}
        repo_root = result.get("repo_root") or new_root  # fitness:allow-literal Cortex field name (see above)
        previous = result.get("previous_repo_root")  # fitness:allow-literal Cortex field name (see above)
    except ValueError as exc:
        error = str(exc)
    except _admin_token_missing() as exc:  # AdminTokenMissing — resolved lazily
        error = (
            "admin token not configured — set CORTEX_ADMIN_TOKEN in the environment "
            "or in local-cortex/.env to edit project folders."
        )
        _ = exc
    except Exception as exc:  # httpx status/transport errors → the surfaced detail
        error = _explain_repo_root_error(exc)

    return {
        "project": project,
        "project_key": target_key,
        "ok": error is None,
        "repo_root": repo_root,  # fitness:allow-literal Cortex field name (the `root` agent-name pattern is a false positive)
        "previous_repo_root": previous,  # fitness:allow-literal Cortex field name (see above)
        "error": error,
    }


def _admin_token_missing() -> type[BaseException]:
    """The `AdminTokenMissing` exception type (imported lazily so the pure-ish shell
    doesn't hard-import `cortex_client` at module load — keeps the import graph
    flat). Falls back to a never-matching sentinel if it can't be imported."""
    try:
        from app.cortex_client import AdminTokenMissing

        return AdminTokenMissing
    except Exception:  # pragma: no cover - cortex_client is always importable here
        class _NeverMatches(BaseException):
            ...

        return _NeverMatches


def _explain_repo_root_error(exc: Exception) -> str:
    """Turn a repo_root PATCH failure into a short, human, NON-leaky error string
    (the admin token never appears). Maps httpx status/transport errors when httpx
    is present; falls back to the exception text otherwise."""
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            detail = ""
            try:
                detail = (exc.response.json() or {}).get("detail") or ""
            except (ValueError, AttributeError):
                detail = exc.response.text[:200] if exc.response is not None else ""
            code = exc.response.status_code if exc.response is not None else "?"
            return f"Cortex rejected the change ({code}){': ' + detail if detail else ''}"
        if isinstance(exc, httpx.HTTPError):
            return f"couldn't reach Cortex: {exc}"
    except Exception:  # pragma: no cover - httpx import/branch guard
        pass
    return f"couldn't set the project folder: {exc}"


__all__ = [
    "router",
    "app_settings_endpoint",
    "agent_config_endpoint",
    "flags_endpoint",
    "set_flags_endpoint",
    "set_app_settings_endpoint",
    "save_agent_config_endpoint",
    "promote_agent_config_endpoint",
    "license_status_endpoint",
    "license_login_endpoint",
    "license_activate_endpoint",
    "license_heartbeat_endpoint",
    "license_customer_action_endpoint",
    "billing_status_endpoint",
    "system_schema_endpoint",
    "providers_endpoint",
    "providers_config_endpoint",
    "custom_provider_add_endpoint",
    "custom_provider_delete_endpoint",
    "provider_key_test_endpoint",
    "workspace_endpoint",
    "get_operational_store",
    "build_service",
    "get_system_schema",
    "get_model_catalog",
    "get_custom_provider_store",
    "get_provider_config_source",
    "get_key_test",
    "get_repo_root_client",
]
