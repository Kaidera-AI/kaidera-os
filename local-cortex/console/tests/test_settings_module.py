"""Track A step 4 — the third feature-module carve: `app/settings/`.

The settings feature (app/system settings + per-agent config + designation
normalize/seed + project autonomy/propose-mode flags) is lifted out of
`app/main.py` + `app/settings.py` into a clean vertical module behind the
`OperationalStorePort`. It follows the PATTERN the analytics (1st) and agents
(2nd) carves established; the remaining carves (dispatch / runs) follow in turn.

The module has three parts and these tests pin each:

  * `app/settings/service.py` — the config LOGIC (`SettingsService`). It depends
    ONLY on `domain.ports.OperationalStorePort` (the app-DB operational surface —
    `load_app_settings` / `upsert_app_settings` for app/system settings,
    `load_agent_overrides` / `get_agent_override` / `save_agent_override` for
    per-agent config, and the project autonomy/propose-mode flag methods). It
    imports NOTHING outward (no fastapi / httpx / subprocess / psycopg2 / asyncpg)
    and never reaches back into `app.main`, the concrete `appdb`/`adapters`, or
    `app.settings` itself. The designation-normalise + the field-cleaning logic moved
    1:1 from `settings.normalize_designation` / `settings._clean_override`, and the
    per-agent resolve/save + project-flag guards from `settings.get_agent_designation` /
    `save_agent_override` / `is_project_autonomous` / `is_propose_mode`. → tested against
    a FAKE port (no DB). (The one-time designation SEED is no longer module-local: it is
    PROJECT-SUPPLIED DATA loaded by `settings.seed_agent_overrides`; the harness names no
    worker — § pure-runtime / zero AI Workers, v0.1.112.)

  * `app/settings/api.py` — a FastAPI `APIRouter` (the imperative shell — MAY
    import fastapi) whose JSON endpoints construct the service over the port
    (resolved from `app.state` via `Depends`) and return JSON. → tested by driving
    the route functions directly with a fake port (no ASGI / live DB), the same
    idiom as `test_analytics_module.py` / `test_agents_module.py`.

    CRITICAL routing-collision guard (per the agents carve): the existing
    `POST /agents/{p}/{a}/config` + `POST /agents/{p}/{a}/chat` are LIVE two-/three-
    segment `/agents/...` routes, and the existing HTML `GET /settings/{page}` is a
    one-segment `/settings/...` route. The module's JSON routes deliberately use a
    distinct `/settings/{project}/...` shape (two-plus segments, all GET) so they
    can NEVER shadow either — pinned by `test_router_paths_do_not_collide`.

These tests are written BEFORE the implementation (strict TDD) and match the
existing fake-driven, no-DB style (`test_agents_module.py`).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
#  Fake OperationalStorePort — serves scripted app-settings / overrides / flags.
# ---------------------------------------------------------------------------


class FakeOpStore:
    """Structural `OperationalStorePort` stand-in for the settings service.

    Implements the app-settings (`load_app_settings`/`upsert_app_settings`),
    per-agent override (`load_agent_overrides`/`get_agent_override`/
    `save_agent_override`), and project-flag (autonomy + propose-mode) methods the
    settings service calls — an in-memory dict each (no DB). `down` simulates a
    degraded store: every read returns its empty default and every write returns
    False (the house-law graceful degrade — exactly what the real adapter does
    when the app-DB is unreachable)."""

    def __init__(self, *, app_settings=None, overrides=None, autonomy=None,
                 propose=None, down=False):
        self._app = dict(app_settings or {})
        self._overrides = {k: dict(v) for k, v in (overrides or {}).items()}
        self._autonomy = dict(autonomy or {})
        self._propose = dict(propose or {})
        self._down = down
        self.calls: list[str] = []

    # -- liveness --
    def available(self) -> bool:
        self.calls.append("available")
        return not self._down

    # -- app / system settings --
    def load_app_settings(self) -> dict:
        self.calls.append("load_app_settings")
        return {} if self._down else dict(self._app)

    def upsert_app_settings(self, items: dict) -> bool:
        self.calls.append("upsert_app_settings")
        if self._down:
            return False
        self._app.update(items)
        return True

    # -- per-agent overrides --
    def load_agent_overrides(self) -> dict:
        self.calls.append("load_agent_overrides")
        return {} if self._down else {k: dict(v) for k, v in self._overrides.items()}

    def get_agent_override(self, project: str, agent: str) -> dict:
        self.calls.append("get_agent_override")
        if self._down:
            return {}
        key = f"{(project or '').strip().lower()}:{(agent or '').strip().lower()}"
        return dict(self._overrides.get(key, {}))

    def save_agent_override(self, project: str, agent: str, entry: dict) -> bool:
        self.calls.append("save_agent_override")
        if self._down:
            return False
        key = f"{(project or '').strip().lower()}:{(agent or '').strip().lower()}"
        if entry:
            self._overrides[key] = dict(entry)
        else:
            self._overrides.pop(key, None)  # empty entry → DELETE row
        return True

    # -- project flags --
    def is_project_autonomous(self, project: str) -> bool:
        self.calls.append("is_project_autonomous")
        if self._down:
            return False
        return bool(self._autonomy.get((project or "").strip().lower()))

    def set_project_autonomy(self, project: str, enabled: bool,
                             updated_by=None) -> bool:
        self.calls.append("set_project_autonomy")
        if self._down:
            return False
        self._autonomy[(project or "").strip().lower()] = bool(enabled)
        return True

    def list_autonomous_projects(self) -> list:
        self.calls.append("list_autonomous_projects")
        if self._down:
            return []
        return [k for k, v in self._autonomy.items() if v]

    def is_propose_mode(self, project: str) -> bool:
        self.calls.append("is_propose_mode")
        if self._down:
            return False
        return bool(self._propose.get((project or "").strip().lower()))

    def set_propose_mode(self, project: str, enabled: bool, updated_by=None) -> bool:
        self.calls.append("set_propose_mode")
        if self._down:
            return False
        self._propose[(project or "").strip().lower()] = bool(enabled)
        return True


# Realistic scripted data (the shapes the SettingsDB/adapter returns).
SAMPLE_APP_SETTINGS = {
    "theme": "light",
    "poll_interval_secs": 10,
    "harness_default": "claude-code",
}
SAMPLE_OVERRIDES = {
    "kaidera-os:ren": {"designation": "interactive", "role": "CPO / lead"},
    "kaidera-os:kai": {"designation": "interactive", "role": "co-lead"},
    "kaidera-os:bob": {"harness": "claude-code", "model": "claude-opus-4-8[1m]"},
}


# ---------------------------------------------------------------------------
#  service.py — app/system settings (load/upsert through the port)
# ---------------------------------------------------------------------------


def test_load_app_settings_reads_port():
    """`SettingsService.load_app_settings` returns the port's app-settings map
    (the raw key→value rows behind the System page), consulting the port."""
    from app.settings_module.service import SettingsService

    store = FakeOpStore(app_settings=SAMPLE_APP_SETTINGS)
    svc = SettingsService(store=store)
    out = svc.load_app_settings()

    assert out == SAMPLE_APP_SETTINGS
    assert "load_app_settings" in store.calls


def test_upsert_app_settings_writes_port():
    """`SettingsService.upsert_app_settings` upserts key→value rows through the
    port (the System-save path's durable write) and returns True on success."""
    from app.settings_module.service import SettingsService

    store = FakeOpStore(app_settings=dict(SAMPLE_APP_SETTINGS))
    svc = SettingsService(store=store)

    assert svc.upsert_app_settings({"theme": "dark"}) is True
    assert "upsert_app_settings" in store.calls
    # the write actually landed in the (fake) store
    assert store.load_app_settings()["theme"] == "dark"


# ---------------------------------------------------------------------------
#  service.py — per-agent config (get / resolve / save through the port)
# ---------------------------------------------------------------------------


def test_load_overrides_and_get_one():
    """`load_overrides` returns the whole per-agent override map and
    `get_override` returns one agent's entry — both cleaned to known string
    fields (lifted from `settings.load_agent_overrides`/`get_agent_override`)."""
    from app.settings_module.service import SettingsService

    svc = SettingsService(store=FakeOpStore(overrides=SAMPLE_OVERRIDES))

    all_ov = svc.load_overrides()
    assert set(all_ov) == {"kaidera-os:ren", "kaidera-os:kai", "kaidera-os:bob"}
    assert svc.get_override("kaidera-os", "ren")["role"] == "CPO / lead"
    # case-insensitive key composition (the "{project}:{agent}" store key)
    assert svc.get_override("kaidera-os", "REN")["designation"] == "interactive"
    # unknown agent → {} (no override)
    assert svc.get_override("kaidera-os", "nobody") == {}


def test_resolve_designation_override_first_then_blank():
    """`resolve_designation` returns the console designation override
    ("interactive"/"autonomous") or "" (no override → caller uses the registry
    heuristic). Lifted 1:1 from `settings.get_agent_designation`; an unknown/junk
    stored designation normalises to "". Accepts a pre-loaded map (loop reuse)."""
    from app.settings_module.service import SettingsService

    svc = SettingsService(store=FakeOpStore(overrides=SAMPLE_OVERRIDES))

    assert svc.resolve_designation("kaidera-os", "ren") == "interactive"
    # bob carries only harness/model → no designation override → ""
    assert svc.resolve_designation("kaidera-os", "bob") == ""
    # unknown agent → ""
    assert svc.resolve_designation("kaidera-os", "nobody") == ""

    # pre-loaded map path (avoids a per-agent store read in a loop)
    overrides = svc.load_overrides()
    assert svc.resolve_designation("kaidera-os", "kai", overrides=overrides) == "interactive"


def test_save_override_merges_and_clears():
    """`save_override` MERGES the submitted fields over the stored entry: a
    non-blank value sets it, a blank value CLEARS that field (falls back to the
    registry value), designation is validated, and an agent left with no
    overrides is dropped. Lifted 1:1 from `settings.save_agent_override` — the
    canonical-source semantics (it writes through the port, where it writes
    today) are unchanged. Returns the post-save effective entry."""
    from app.settings_module.service import SettingsService

    store = FakeOpStore(overrides={"kaidera-os:bob": {"harness": "claude-code",
                                                    "model": "old-model"}})
    svc = SettingsService(store=store)

    # set model (non-blank) + clear harness (blank) on bob
    eff = svc.save_override("kaidera-os", "bob",
                            {"model": "claude-opus-4-8[1m]", "harness": ""})
    assert eff == {"model": "claude-opus-4-8[1m]"}
    assert "save_agent_override" in store.calls
    # the store reflects the merge (harness cleared, model updated)
    assert store.get_agent_override("kaidera-os", "bob") == {"model": "claude-opus-4-8[1m]"}

    # an invalid designation is dropped (validated → ""), not persisted
    eff2 = svc.save_override("kaidera-os", "bob", {"designation": "bogus"})
    assert "designation" not in eff2

    # clearing the last field drops the agent entry entirely
    eff3 = svc.save_override("kaidera-os", "bob", {"model": ""})
    assert eff3 == {}
    assert svc.get_override("kaidera-os", "bob") == {}


def test_clean_override_validates_fields():
    """The pure `clean_override` keeps only the known AGENT_OVERRIDE_FIELDS with
    non-empty string values, and validates `designation` to a known value (junk
    dropped). Lifted 1:1 from `settings._clean_override`."""
    from app.settings_module import service as settings_service

    cleaned = settings_service.clean_override({
        "harness": "claude-code",
        "model": "  ",                 # blank → dropped
        "designation": "INTERACTIVE",  # case-insensitive → normalised
        "role": "CPO / lead",
        "unknown_field": "x",          # not a known field → dropped
    })
    assert cleaned == {
        "harness": "claude-code",
        "designation": "interactive",
        "role": "CPO / lead",
    }
    # a junk designation is dropped entirely
    assert settings_service.clean_override({"designation": "bogus"}) == {}
    # a non-dict → {}
    assert settings_service.clean_override("nope") == {}


# ---------------------------------------------------------------------------
#  service.py — designation normalize + the one-time seed (pure)
# ---------------------------------------------------------------------------


def test_normalize_designation_pure():
    """`normalize_designation` coerces to a known value or "" (lifted 1:1 from
    `settings.normalize_designation`)."""
    from app.settings_module import service as settings_service

    assert settings_service.normalize_designation("Interactive") == "interactive"
    assert settings_service.normalize_designation("AUTONOMOUS") == "autonomous"
    assert settings_service.normalize_designation("worker") == ""
    assert settings_service.normalize_designation(None) == ""
    assert settings_service.DESIGNATION_INTERACTIVE == "interactive"
    assert settings_service.DESIGNATION_AUTONOMOUS == "autonomous"


# NOTE: the old dead `settings_module.service.seed_overrides` (module fn) +
# `SettingsService.seed_overrides` (method) were DELETED in v0.1.112 — the harness is a
# pure runtime and names no worker, so the one-time seed is now PROJECT-SUPPLIED DATA
# loaded by the legacy facade `settings.seed_agent_overrides` (env-driven, EMPTY by
# default), not a hardcoded console-local seed. Their two tests are replaced by the
# config-driven test below, which pins the new data-driven loader + greenfield default.


def test_designation_seed_is_project_supplied_data_empty_by_default(monkeypatch):
    """`settings._load_designation_seed` is PROJECT-SUPPLIED DATA, EMPTY by default.

    Greenfield (no `KAIDERA_DESIGNATION_SEED`, no default project → no profile to read) →
    empty seed → the harness stamps no worker into app data (§ pure-runtime / zero AI
    Workers). A deployment supplies its project's policy as inline JSON or a `.json` path;
    the loader parses + shape-filters it (well-formed `project:agent` -> {field: str}
    entries only) and tolerates malformed input by yielding empty (never raises)."""
    from app import settings as settings_store

    # true greenfield: no env override AND no default project resolved (so there is no
    # profile to fall back to) → empty seed (no worker names baked in). Neutralise the
    # profile default source by pinning the active-project resolver to "".
    monkeypatch.delenv("KAIDERA_DESIGNATION_SEED", raising=False)
    monkeypatch.setattr(settings_store, "_seed_active_project_key", lambda: "")
    assert settings_store._load_designation_seed() == {}

    # a deployment supplies its project's seed as inline JSON (the kaidera-os shape)
    monkeypatch.setenv(
        "KAIDERA_DESIGNATION_SEED",
        '{"kaidera-os:ren": {"designation": "interactive", "role": "CPO / lead"},'
        ' "kaidera-os:kai": {"designation": "interactive", "role": "co-lead"}}',
    )
    seed = settings_store._load_designation_seed()
    assert seed["kaidera-os:ren"]["designation"] == "interactive"
    assert seed["kaidera-os:ren"]["role"] == "CPO / lead"
    assert seed["kaidera-os:kai"]["role"] == "co-lead"

    # malformed JSON / wrong shape → empty (tolerant, never raises)
    monkeypatch.setenv("KAIDERA_DESIGNATION_SEED", "{not json")
    assert settings_store._load_designation_seed() == {}
    monkeypatch.setenv("KAIDERA_DESIGNATION_SEED", '["a list, not a dict"]')
    assert settings_store._load_designation_seed() == {}


def test_designation_seed_loads_from_json_file(monkeypatch, tmp_path):
    """A deployment may point `KAIDERA_DESIGNATION_SEED` at a `.json` DATA FILE (the
    project-supplied profile, e.g. kaidera-os-kai-ren.designations.json). The loader reads
    it, ignores any non-`project:agent` keys (a leading ``_comment``), and shape-filters
    the rest."""
    from app import settings as settings_store

    data_file = tmp_path / "kaidera-os-kai-ren.designations.json"
    data_file.write_text(
        '{"_comment": "project-supplied data", '
        '"kaidera-os:ren": {"designation": "interactive", "role": "CPO / lead"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("KAIDERA_DESIGNATION_SEED", str(data_file))
    seed = settings_store._load_designation_seed()
    assert seed == {"kaidera-os:ren": {"designation": "interactive", "role": "CPO / lead"}}
    assert "_comment" not in seed  # non-"project:agent" keys are dropped


# ---------------------------------------------------------------------------
#  service.py — project flags (autonomy + propose-mode, fail-safe OFF)
# ---------------------------------------------------------------------------


def test_project_flags_read_write_failsafe():
    """The project-flag surface (autonomy + propose-mode) passes through the port
    with the fail-safe-OFF + blank-project guards lifted 1:1 from
    `settings.is_project_autonomous` / `is_propose_mode` etc."""
    from app.settings_module.service import SettingsService

    store = FakeOpStore(autonomy={"kaidera-os": True}, propose={"kaidera-os": False})
    svc = SettingsService(store=store)

    assert svc.is_project_autonomous("kaidera-os") is True
    assert svc.is_project_autonomous("other") is False   # no row → OFF
    assert svc.is_project_autonomous("") is False         # blank → OFF (no call)
    assert svc.is_propose_mode("kaidera-os") is False
    assert "kaidera-os" in svc.list_autonomous_projects()

    # writes flip the flag (and re-read authoritative)
    assert svc.set_project_autonomy("other", True) is True
    assert svc.is_project_autonomous("other") is True
    assert svc.set_propose_mode("kaidera-os", True) is True
    assert svc.is_propose_mode("kaidera-os") is True
    # a blank project is rejected on write (no port call, returns False)
    assert svc.set_project_autonomy("", True) is False


def test_service_graceful_when_store_down():
    """A down store: reads return their empty default (app settings {}, overrides
    {}, designation "", flags OFF), writes return False — never raises (the house
    law). Mirrors the SettingsDB UNAVAILABLE → safe-default contract."""
    from app.settings_module.service import SettingsService

    svc = SettingsService(store=FakeOpStore(down=True))

    assert svc.load_app_settings() == {}
    assert svc.upsert_app_settings({"theme": "dark"}) is False
    assert svc.load_overrides() == {}
    assert svc.get_override("kaidera-os", "ren") == {}
    assert svc.resolve_designation("kaidera-os", "ren") == ""
    assert svc.save_override("kaidera-os", "ren", {"role": "x"}) == {}
    assert svc.is_project_autonomous("kaidera-os") is False
    assert svc.is_propose_mode("kaidera-os") is False
    assert svc.list_autonomous_projects() == []


def test_service_no_store_is_safe():
    """Constructed with NO store, the read surfaces degrade to their empty
    defaults and writes are False — so a caller that holds only the pure helpers
    (normalize/clean/seed) can use the service without a store."""
    from app.settings_module.service import SettingsService

    svc = SettingsService()  # no store
    assert svc.load_app_settings() == {}
    assert svc.load_overrides() == {}
    assert svc.resolve_designation("kaidera-os", "ren") == ""
    assert svc.upsert_app_settings({"x": 1}) is False
    assert svc.save_override("kaidera-os", "ren", {"role": "x"}) == {}


def test_override_store_key_pure():
    """`override_store_key` composes the lower-cased blank-safe "{project}:{agent}"
    key (lifted 1:1 from `settings._override_store_key`)."""
    from app.settings_module import service as settings_service

    assert settings_service.override_store_key("kaidera-os", "Ren") == "kaidera-os:ren"
    assert settings_service.override_store_key(None, "x") == ":x"
    assert settings_service.override_store_key("  P  ", "  A  ") == "p:a"


def test_service_depends_only_on_port_not_outward():
    """GUARD: `app/settings_module/service.py` imports NOTHING outward (no fastapi
    / httpx / subprocess / psycopg2 / asyncpg) and does NOT reach for `app.main`,
    the concrete `app.appdb` / `app.adapters`, or `app.settings` (the legacy
    store) — only the domain port.

    Parsed via `ast` (a name in a comment/docstring can't fool it), mirroring
    `test_ports_purity.py` / the analytics + agents guards. This is the
    module-isolation rule the `.importlinter` independence contract also enforces
    at the graph level."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "settings_module" / "service.py"
    ).read_text()
    tree = ast.parse(src)
    top: set[str] = set()
    dotted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top.add(a.name.split(".")[0])
                dotted.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top.add(node.module.split(".")[0])
                dotted.add(node.module)

    forbidden = {"fastapi", "starlette", "httpx", "subprocess", "psycopg2", "asyncpg"}
    assert not (top & forbidden), (
        f"service.py must not import outward I/O libs, got: {sorted(top & forbidden)}"
    )
    # No reaching back into the blob, the concrete adapters/db, or the legacy
    # settings facade — the service depends on the domain port only.
    assert "app.main" not in dotted, "service.py must not import app.main"
    assert "app.settings" not in dotted, (
        "service.py must not import the legacy app.settings facade"
    )
    assert not any(
        m == "app.appdb" or m.startswith("app.adapters") for m in dotted
    ), "service.py must depend on the domain port, not the concrete appdb/adapters"


# ---------------------------------------------------------------------------
#  api.py — the FastAPI router (imperative shell; builds svc over the port)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_app_settings_endpoint():
    """Driving the `GET /settings/{project}/app` handler directly returns the
    app/system settings + store-liveness (no ASGI / live DB) — fake port."""
    from app.settings_module import api as settings_api

    # the store holds typed-SCHEMA / retired keys (theme, poll_interval_secs,
    # harness_default — all filtered from the raw editor) PLUS one genuine
    # operator-added extra, which the raw editor SHOULD surface.
    store = FakeOpStore(app_settings={**SAMPLE_APP_SETTINGS, "operator_extra": "keep"})
    result = await settings_api.app_settings_endpoint("kaidera-os", store=store)

    assert result["project"] == "kaidera-os"
    # the raw-editor surface excludes the typed System fields + the retired keys — only
    # the genuine operator extra remains (S6: no duplication, no internal leakage).
    assert result["settings"] == {"operator_extra": "keep"}
    assert result["store_connected"] is True


@pytest.mark.asyncio
async def test_router_agent_config_endpoint():
    """Driving the `GET /settings/{project}/agents/{agent}/config` handler returns
    one agent's resolved override + designation (override-first)."""
    from app.settings_module import api as settings_api

    store = FakeOpStore(overrides=SAMPLE_OVERRIDES)
    result = await settings_api.agent_config_endpoint(
        "kaidera-os", "ren", store=store
    )

    assert result["project"] == "kaidera-os"
    assert result["agent"] == "ren"
    assert result["override"]["role"] == "CPO / lead"
    assert result["designation"] == "interactive"


@pytest.mark.asyncio
async def test_router_flags_endpoint():
    """Driving the `GET /settings/{project}/flags` handler returns the project's
    autonomy + propose-mode flags (fail-safe OFF when unset)."""
    from app.settings_module import api as settings_api

    store = FakeOpStore(autonomy={"kaidera-os": True}, propose={"kaidera-os": False})
    result = await settings_api.flags_endpoint("kaidera-os", store=store)

    assert result["project"] == "kaidera-os"
    assert result["autonomous"] is True
    assert result["propose_mode"] is False


def test_router_paths_do_not_collide():
    """The module's JSON routes use a distinct `/settings/{project}/...` shape
    (two-plus segments) so they can NEVER shadow either the existing one-segment
    HTML `GET /settings/{page}` tab route OR the live `POST /agents/{p}/{a}/config`
    + `/chat` routes (different root + method).

    This is the strictly-additive routing-collision guard the agents carve
    mandated. The WRITE endpoints (Track C) reuse the SAME `/settings/{project}/...`
    JSON shape but with the POST method, so they share the read paths' leaves
    without shadowing the live HTML POSTs (proven separately by
    `test_write_routes_do_not_collide_with_live_html_posts`)."""
    from app.settings_module.api import router

    paths = {r.path for r in router.routes}
    # the module's deeper /settings/{project}/... leaves …
    assert "/settings/{project}/app" in paths
    assert "/settings/{project}/agents/{agent}/config" in paths
    assert "/settings/{project}/flags" in paths
    # … and it does NOT claim the one-segment HTML tab path, nor any /agents/ path.
    assert "/settings/{page}" not in paths
    assert not any(p.startswith("/agents/") for p in paths), (
        "the settings router must not own any /agents/... path (no collision with "
        "the live POST /agents/{p}/{a}/config + /chat routes)"
    )


# ---------------------------------------------------------------------------
#  api.py — the WRITE endpoints (Track C: the SPA settings write path)
#
#  Three collision-free JSON POSTs under the SAME `/settings/{project}/...` shape
#  the reads use, delegating to the OperationalStorePort setters via the service:
#    * POST /settings/{project}/flags                  → set autonomy / propose-mode
#    * POST /settings/{project}/app                    → upsert app/system settings
#    * POST /settings/{project}/agents/{agent}/config  → save an agent override
#  Driven directly with a fake port (no ASGI / live DB), the established idiom; each
#  asserts the store received the right call, a graceful-degrade when down, and the
#  routes stay collision-free with the live HTML POSTs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_set_flags_endpoint_autonomy_and_propose():
    """`POST /settings/{project}/flags` flips autonomy and/or propose-mode through
    the port (the same effect as the live autonomy toggle, just JSON) and echoes
    the authoritative post-write flag state. A field OMITTED from the body is left
    untouched; a present field is written. Returns `{project, autonomous,
    propose_mode, ok}`."""
    from app.settings_module import api as settings_api

    store = FakeOpStore(autonomy={"kaidera-os": False}, propose={"kaidera-os": False})

    # set BOTH flags on
    result = await settings_api.set_flags_endpoint(
        "kaidera-os", {"autonomous": True, "propose_mode": True}, store=store
    )
    assert result["project"] == "kaidera-os"
    assert result["autonomous"] is True
    assert result["propose_mode"] is True
    assert result["ok"] is True
    assert "set_project_autonomy" in store.calls
    assert "set_propose_mode" in store.calls
    # the writes actually landed in the (fake) store (authoritative re-read)
    assert store.is_project_autonomous("kaidera-os") is True
    assert store.is_propose_mode("kaidera-os") is True


@pytest.mark.asyncio
async def test_router_set_flags_endpoint_partial_only_writes_present_field():
    """`POST /settings/{project}/flags` only writes the flags PRESENT in the body —
    an omitted flag is not touched (so the SPA can toggle one switch without
    clobbering the other)."""
    from app.settings_module import api as settings_api

    store = FakeOpStore(autonomy={"kaidera-os": True}, propose={"kaidera-os": True})

    # body carries ONLY propose_mode=false → autonomy is left untouched
    result = await settings_api.set_flags_endpoint(
        "kaidera-os", {"propose_mode": False}, store=store
    )
    assert result["propose_mode"] is False
    assert result["autonomous"] is True  # untouched (still on)
    assert "set_propose_mode" in store.calls
    assert "set_project_autonomy" not in store.calls  # the omitted flag is not written


@pytest.mark.asyncio
async def test_router_set_app_settings_endpoint():
    """`POST /settings/{project}/app` upserts app/system key→value rows through the
    port (`upsert_app_settings`) and returns the authoritative post-write settings
    map + store-liveness. Returns `{project, settings, store_connected, ok}`."""
    from app.settings_module import api as settings_api

    store = FakeOpStore(app_settings=dict(SAMPLE_APP_SETTINGS))

    result = await settings_api.set_app_settings_endpoint(
        "kaidera-os", {"settings": {"harness_default": "claude-code", "operator_extra": "v"}},
        store=store,
    )
    assert result["project"] == "kaidera-os"
    assert result["ok"] is True
    assert result["store_connected"] is True
    # the WRITE is unfiltered — both the typed field and the extra land in the durable store
    assert "upsert_app_settings" in store.calls
    assert store.load_app_settings()["harness_default"] == "claude-code"
    assert store.load_app_settings()["operator_extra"] == "v"
    # the RETURNED map is the raw-editor SURFACE: the typed System field (harness_default)
    # is filtered out (it's edited in the typed form); only the genuine extra appears.
    assert "harness_default" not in result["settings"]
    assert result["settings"]["operator_extra"] == "v"


@pytest.mark.asyncio
async def test_router_save_agent_config_endpoint():
    """`POST /settings/{project}/agents/{agent}/config` saves a console-local agent
    override (designation/harness/model/…) via the service's `save_override` (MERGE
    semantics: a non-blank value sets, a blank value clears), then returns the
    agent's post-save effective override + resolved designation. Returns
    `{project, agent, override, designation, ok}`."""
    from app.settings_module import api as settings_api

    store = FakeOpStore(overrides={"kaidera-os:bob": {"harness": "claude-code",
                                                    "model": "old-model"}})

    result = await settings_api.save_agent_config_endpoint(
        "kaidera-os", "bob",
        {"override": {"model": "claude-opus-4-8[1m]", "harness": ""}},
        store=store,
    )
    assert result["project"] == "kaidera-os"
    assert result["agent"] == "bob"
    assert result["ok"] is True
    # the merge applied: model set, harness cleared (the canonical save semantics)
    assert result["override"] == {"model": "claude-opus-4-8[1m]"}
    assert "save_agent_override" in store.calls
    assert store.get_agent_override("kaidera-os", "bob") == {"model": "claude-opus-4-8[1m]"}

    # a designation override resolves on the way back out
    result2 = await settings_api.save_agent_config_endpoint(
        "kaidera-os", "bob", {"override": {"designation": "autonomous"}}, store=store
    )
    assert result2["designation"] == "autonomous"


@pytest.mark.asyncio
async def test_router_save_agent_config_rejects_second_orchestrator():
    """Only one deterministic orchestrator role may be assigned per project."""
    from app.settings_module import api as settings_api

    store = FakeOpStore(overrides={"kaidera-os:ops": {"role": "orchestrator"}})

    result = await settings_api.save_agent_config_endpoint(
        "kaidera-os", "bob", {"override": {"role": "orchestrator"}}, store=store, cortex=None
    )

    assert result["ok"] is False
    assert "Only one deterministic orchestrator" in result["error"]
    assert "save_agent_override" not in store.calls
    assert store.get_agent_override("kaidera-os", "bob") == {}


@pytest.mark.asyncio
async def test_router_write_endpoints_graceful_when_store_down():
    """A down store: every write endpoint reports `ok=false` and the fail-safe
    state (flags OFF, an empty settings map / override) — never a 500 (the house
    law). Mirrors the read endpoints' graceful-degrade."""
    from app.settings_module import api as settings_api

    store = FakeOpStore(down=True)

    flags = await settings_api.set_flags_endpoint(
        "kaidera-os", {"autonomous": True, "propose_mode": True}, store=store
    )
    assert flags["ok"] is False
    assert flags["autonomous"] is False and flags["propose_mode"] is False

    app_res = await settings_api.set_app_settings_endpoint(
        "kaidera-os", {"settings": {"theme": "dark"}}, store=store
    )
    assert app_res["ok"] is False
    assert app_res["store_connected"] is False
    assert app_res["settings"] == {}

    cfg = await settings_api.save_agent_config_endpoint(
        "kaidera-os", "ren", {"override": {"role": "x"}}, store=store
    )
    assert cfg["ok"] is False
    assert cfg["override"] == {}


def test_router_exposes_write_routes():
    """The module's APIRouter exposes the three WRITE routes as POSTs under the same
    `/settings/{project}/...` JSON shape the reads use (so `main` can
    `include_router` them additively — one router, read GETs + write POSTs)."""
    from app.settings_module.api import router

    posts = {r.path for r in router.routes if "POST" in getattr(r, "methods", set())}
    assert "/settings/{project}/flags" in posts
    assert "/settings/{project}/app" in posts
    assert "/settings/{project}/agents/{agent}/config" in posts


def test_write_routes_do_not_collide_with_live_html_posts():
    """COLLISION GUARD (Track C): the new write POSTs share the read JSON shape
    `/settings/{project}/...` (two-plus segments) and must NOT shadow ANY of the
    live HTML `POST /settings/...` routes, which all use either a LITERAL first
    segment under `/settings/` (`system`, `configure`, `projects/...`) or a deeper
    `system/...` shape — never the `{project}/{leaf}` shape the JSON owns.

    A FastAPI/Starlette path with a literal segment (`/settings/system`) and one
    with a single param (`/settings/{project}/flags`) are DIFFERENT route shapes:
    a one-segment request like `/settings/system` can't match a two-segment route,
    and `/settings/kaidera-os/app` (param) only matches the literal `/settings/system`
    if `kaidera-os == system`, which it can't (different segment COUNT for `app`/
    `flags`, and `configure` is one segment). The agents-config write
    `/settings/{project}/agents/{agent}/config` (4 segments) likewise can't hit the
    live `POST /agents/{p}/{a}/config` (different root). This test pins the shapes
    so a future literal `POST /settings/<x>` can't silently collide."""
    from app.settings_module.api import router

    # The live HTML POST /settings/... routes (transcribed from main.py) — every one
    # has a LITERAL first segment under /settings/ or is a deeper system/... path.
    live_html_settings_posts = {
        "/settings/projects/{project_key}/folder",
        "/settings/system",
        "/settings/system/test-key",
        "/settings/system/custom-provider",
        "/settings/system/custom-provider/delete",
        "/settings/configure",
    }
    write_posts = {
        r.path for r in router.routes if "POST" in getattr(r, "methods", set())
    }
    # The module owns NONE of the live HTML POST paths …
    assert not (write_posts & live_html_settings_posts), (
        f"write routes collide with live HTML POSTs: "
        f"{sorted(write_posts & live_html_settings_posts)}"
    )
    # Its write leaves are exactly the community JSON shape.
    assert write_posts == {
        "/settings/{project}/flags",
        "/settings/{project}/app",
        "/settings/{project}/agents/{agent}/config",
        # The explicit "Promote to registry" action (feature-gap #81): a DISTINCT
        # `promote` leaf — different trailing segment from the `config` sibling, so it
        # can't shadow it, and still the single-param `{project}/...` shape.
        "/settings/{project}/agents/{agent}/promote",
        "/settings/{project}/workspace",
    }
    # EVERY write leaf is the single-param `{project}/...` shape (no literal first
    # segment), so none can shadow a literal-first live HTML POST.
    assert all(p.startswith("/settings/{project}/") for p in write_posts)
    # and it still owns NO /agents/... path (the live agent-config POST root).
    assert not any(p.startswith("/agents/") for p in write_posts)


def test_router_is_apirouter_with_routes():
    """`app.settings_module.api.router` is a FastAPI APIRouter exposing the
    settings JSON paths under the module's prefix (so `main` can `include_router`
    it additively)."""
    from fastapi import APIRouter

    from app.settings_module.api import router

    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    assert "/settings/{project}/app" in paths
    assert "/settings/{project}/agents/{agent}/config" in paths
    assert "/settings/{project}/flags" in paths


def test_module_exports_service_and_router():
    """`app.settings_module` re-exports the service + router (the module's public
    face)."""
    import app.settings_module as settings_mod

    assert hasattr(settings_mod, "SettingsService")
    assert hasattr(settings_mod, "router")
