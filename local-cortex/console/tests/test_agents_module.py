"""Track A step 3 — the second feature-module carve: `app/agents/`.

The agents CATALOG feature (the READ/catalog side of the agents surface — list a
project's roster grouped Interactive vs Autonomous, and resolve one agent with its
effective config + designation + role) is lifted out of `app/main.py`'s blob into a
clean vertical module behind the SDK ports. It follows the PATTERN the analytics
carve established (the first carve), and the remaining carves (settings / dispatch /
runs) follow in turn.

The module has three parts and these tests pin each:

  * `app/agents/service.py` — the catalog LOGIC (`AgentsService`). It depends ONLY
    on `domain.ports.OperationalStorePort` (the per-agent override store —
    `load_agent_overrides` / `get_agent_override`) plus two INJECTED presentation
    callables (a per-agent config resolver + a config-view shaper — the analytics
    formatter-injection pattern), and the ROSTER is passed IN by the caller (the
    same way analytics takes `agents=`; the caller fetches it from Cortex). It
    imports NOTHING outward (no fastapi / httpx / subprocess / psycopg2 / asyncpg)
    and never reaches back into `app.main`, the concrete `appdb`/`adapters`, or the
    concrete `harness`/`providers`. The classification logic moved 1:1 from
    `main._agent_view` / `_group_agents` / `_classify_interactive` / `_has_cpo_tag`
    / `_registry_interactive` / `_orchestrator_label` / `_lead_agent_name`. →
    tested against a FAKE port (no DB).

  * `app/agents/api.py` — a FastAPI `APIRouter` (the imperative shell — MAY import
    fastapi) whose `GET /agents/{project}` (the roster catalog) + `GET
    /agents/{project}/{agent}` (one agent's detail) construct the service over the
    port (resolved from `app.state` via `Depends`) and the roster (fetched from
    Cortex) and return JSON. → tested by driving the route functions directly with a
    fake port + a fake roster source (no ASGI / live DB), the same idiom as
    `test_analytics_module.py` / `test_dispatch_run_route.py`.

These tests are written BEFORE the implementation (strict TDD) and match the
existing fake-driven, no-DB style (`test_analytics_module.py`).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
#  Fake OperationalStorePort — serves scripted per-agent overrides (no DB).
# ---------------------------------------------------------------------------


class FakeOpStore:
    """Structural `OperationalStorePort` stand-in for the agents service.

    Only the per-agent CONFIG methods the catalog service calls are implemented
    (`load_agent_overrides` for the grouped list, `get_agent_override` for one
    agent's detail). Overrides are keyed "{project}:{agent}" (lower-cased), the
    shape `SettingsDB.load_agent_overrides` returns. `down` simulates a degraded
    store (every read returns its empty default — the house-law graceful degrade)."""

    def __init__(self, *, overrides=None, down=False):
        self._overrides = dict(overrides or {})
        self._down = down
        self.calls: list[str] = []

    def load_agent_overrides(self) -> dict:
        self.calls.append("load_agent_overrides")
        if self._down:
            return {}
        return {k: dict(v) for k, v in self._overrides.items()}

    def get_agent_override(self, project: str, agent: str) -> dict:
        self.calls.append("get_agent_override")
        if self._down:
            return {}
        key = f"{(project or '').strip().lower()}:{(agent or '').strip().lower()}"
        return dict(self._overrides.get(key, {}))


# A realistic project roster (the shape Cortex `get_agents` returns). Mixes:
#   * a runtime row with a rich capabilities block (ren — display_name + role),
#   * a co-lead whose top-level role is generic but whose capabilities mark it (kai),
#   * an orchestrator-role agent (cole),
#   * a plain autonomous worker (bob),
#   * a polluted/synthetic test name (kai-ddl-7 — must NOT be pulled Interactive).
SAMPLE_ROSTER = [
    {
        "name": "ren",
        "role": "full-stack-developer",
        "model": "claude-opus-4-8[1m]",
        "capabilities": {
            "display_name": "Ren",
            "harness": "claude-code",
            "kaidera_os_role": "co-lead",
        },
    },
    {
        "name": "kai",
        "role": "full-stack-developer",
        "capabilities": {
            "display_name": "Kai",
            "pm_cpo_cadence_owner": "true",
        },
    },
    {
        "name": "cole",
        "role": "orchestrator",
        "capabilities": {"display_name": "Cole"},
    },
    {
        "name": "bob",
        "role": "full-stack-developer",
        "capabilities": {"display_name": "Bob"},
    },
    {
        "name": "kai-ddl-7",
        "role": "lead",  # synthetic role contains 'lead' but the name is polluted
        "capabilities": {},
    },
]

# Console-local overrides keyed "{project}:{agent}". Ren is forced Interactive with a
# CPO/lead role; Kai is Interactive co-lead (no CPO tag); Bob is forced Interactive by
# an explicit designation override even though its registry role is autonomous.
SAMPLE_OVERRIDES = {
    "kaidera-os:ren": {"designation": "interactive", "role": "CPO / lead"},
    "kaidera-os:kai": {"designation": "interactive", "role": "co-lead"},
    "kaidera-os:bob": {"designation": "interactive"},
}


# ---------------------------------------------------------------------------
#  service.py — the catalog/classification logic moved out of main.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_groups_and_classifies():
    """`AgentsService.list_agents` splits the roster Interactive vs Autonomous,
    OVERRIDE-FIRST (the per-agent designation override wins over the registry
    heuristic), resolves the orchestrator label + the lead — the catalog substance
    lifted from `main._group_agents`/`_orchestrator_label`/`_lead_agent_name`."""
    from app.agents.service import AgentsService

    store = FakeOpStore(overrides=SAMPLE_OVERRIDES)
    svc = AgentsService(store=store)
    cat = await svc.list_agents("kaidera-os", SAMPLE_ROSTER)

    inter = {v["name"]: v for v in cat["interactive"]}
    auto = {v["name"]: v for v in cat["autonomous"]}

    # ren/kai/bob are Interactive (override-first); cole/kai-ddl-7 are Autonomous.
    assert set(inter) == {"ren", "kai", "bob"}
    assert set(auto) == {"cole", "kai-ddl-7"}

    # the effective role honors the console role override
    assert inter["ren"]["role"] == "CPO / lead"
    # the CPO tag: ren reads as CPO/lead → tagged; kai is a co-lead → NOT tagged
    assert inter["ren"]["cpo_tag"] is True
    assert inter["kai"]["cpo_tag"] is False
    # an explicit designation override is marked as such
    assert inter["bob"]["designation_override"] is True
    # the synthetic name is flagged (a nudge, not authority)
    assert auto["kai-ddl-7"]["is_test"] is True

    # each group is sorted by display name
    assert [v["display_name"] for v in cat["interactive"]] == sorted(
        v["display_name"] for v in cat["interactive"]
    )

    # the orchestrator label resolves from the orchestrator-role agent (display name)
    assert cat["orchestrator"] == "Cole"
    # the lead lands on the CPO-tagged interactive agent
    assert cat["lead"] == "ren"

    # it actually CONSULTED the override store
    assert "load_agent_overrides" in store.calls


@pytest.mark.asyncio
async def test_get_agent_resolves_config_designation_role():
    """`AgentsService.get_agent` finds one agent and resolves its effective config
    (harness/model via the injected resolver), designation, role, and the per-agent
    config-view — the detail substance lifted from `main._agent_detail_view`."""
    from app.agents.service import AgentsService

    store = FakeOpStore(overrides=SAMPLE_OVERRIDES)

    # Inject a config-view shaper (the analytics formatter-injection pattern): the
    # real one wraps harness.agent_config_view; here a recording stub proves the
    # service passes the resolved override + registry-designation through to it.
    seen = {}

    def fake_config_view(agent, override, catalog_groups, registry_designation, pi_catalog_groups=None):
        seen["agent"] = agent.get("name")
        seen["override"] = dict(override)
        seen["registry_designation"] = registry_designation
        return {"name": agent.get("name"), "designation": registry_designation}

    svc = AgentsService(store=store, config_view=fake_config_view)
    detail = await svc.get_agent(
        "kaidera-os", "ren", SAMPLE_ROSTER, catalog_groups=[{"label": "Anthropic"}]
    )

    assert detail is not None
    assert detail["agent"]["name"] == "ren"
    assert detail["agent"]["display_name"] == "Ren"
    # the effective role + designation resolve override-first
    assert detail["role"] == "CPO / lead"
    assert detail["designation"] == "interactive"
    # the registry-derived designation is surfaced for the "registry: …" hint
    assert detail["registry_designation"] == "interactive"  # ren's caps mark it lead
    # the injected config-view was driven with the resolved override + reg designation
    assert detail["config_view"]["name"] == "ren"
    assert seen["override"]["role"] == "CPO / lead"
    assert seen["registry_designation"] == "interactive"

    # it consulted the per-agent override read
    assert "get_agent_override" in store.calls


@pytest.mark.asyncio
async def test_get_agent_unknown_returns_none():
    """An agent not in the roster resolves to None (the caller degrades to the
    Dashboard — never a blank pane / 500), mirroring `main._find_agent` → None."""
    from app.agents.service import AgentsService

    svc = AgentsService(store=FakeOpStore(overrides=SAMPLE_OVERRIDES))
    assert await svc.get_agent("kaidera-os", "nobody", SAMPLE_ROSTER) is None


@pytest.mark.asyncio
async def test_list_agents_graceful_when_store_down():
    """A down override store yields the registry-heuristic classification (no
    override wins), never raises — the house law. With no overrides, only the
    registry-interactive agents (kai via the cadence flag, ren via kaidera_os_role)
    land Interactive; the explicit-override-only bob falls back to Autonomous."""
    from app.agents.service import AgentsService

    svc = AgentsService(store=FakeOpStore(down=True))
    cat = await svc.list_agents("kaidera-os", SAMPLE_ROSTER)

    inter = {v["name"] for v in cat["interactive"]}
    auto = {v["name"] for v in cat["autonomous"]}
    # ren (kaidera_os_role co-lead) + kai (cadence owner) are registry-interactive;
    # bob (interactive ONLY via the now-gone override) falls back to autonomous.
    assert "ren" in inter and "kai" in inter
    assert "bob" in auto
    # the orchestrator still resolves (it's role-based, not override-based)
    assert cat["orchestrator"] == "Cole"


@pytest.mark.asyncio
async def test_list_agents_empty_roster():
    """An empty roster → empty groups, no orchestrator/lead — never raises."""
    from app.agents.service import AgentsService

    svc = AgentsService(store=FakeOpStore())
    cat = await svc.list_agents("kaidera-os", [])
    assert cat["interactive"] == []
    assert cat["autonomous"] == []
    assert cat["orchestrator"] is None
    assert cat["lead"] is None


def test_agent_view_resolves_config_via_injected_resolver():
    """The synchronous `agent_view` shaping uses the INJECTED config resolver for
    the card's harness/model (so the service stays free of the concrete `harness`
    module — the analytics injection pattern). The default resolver keeps it
    self-contained; here a stub proves the wiring."""
    from app.agents.service import AgentsService

    def fake_resolve_config(agent, override):
        # a resolved-config stand-in (the real one wraps harness._registry_config +
        # canonical_harness + harness_label + the model-label map).
        return {
            "harness": override.get("harness") or "claude-code",
            "harness_label": "Claude Code",
            "model": override.get("model") or "claude-opus-4-8[1m]",
            "model_label": "Opus 4.8 (1M context)",
            "thinking": (agent.get("capabilities") or {}).get("thinking"),
        }

    svc = AgentsService(store=FakeOpStore(), resolve_config=fake_resolve_config)
    view = svc.agent_view(
        {"name": "ren", "role": "full-stack-developer",
         "capabilities": {"display_name": "Ren"}},
        designation="interactive",
        role_override="CPO / lead",
        override={"model": "claude-opus-4-8[1m]"},
    )
    assert view["name"] == "ren"
    assert view["display_name"] == "Ren"
    assert view["initials"] == "RE"
    assert view["harness"] == "claude-code"
    assert view["harness_label"] == "Claude Code"
    assert view["model_label"] == "Opus 4.8 (1M context)"
    assert view["role"] == "CPO / lead"
    assert view["interactive"] is True
    assert view["cpo_tag"] is True
    # the row subtitle is built from the resolved labels
    assert "Claude Code" in view["row_sub"]


def test_classification_helpers_pure():
    """The classification primitives lifted from main.py behave 1:1: the CPO tag
    excludes co-lead, the registry heuristic ignores polluted names, and the
    test-name nudge matches the known synthetic marks."""
    from app.agents import service as agents_service

    # Domain leads earn it; a co-lead does NOT (it's a co-lead, not THE lead).
    assert agents_service.has_cpo_tag("CPO / lead") is True
    assert agents_service.has_cpo_tag("CMO") is True
    assert agents_service.has_cpo_tag("co-lead") is False
    assert agents_service.has_cpo_tag("full-stack-developer") is False

    # registry heuristic: role hint OR kaidera_os_role OR the cadence-owner flag
    assert agents_service.registry_interactive(
        {"name": "x", "role": "product lead"}
    ) is True
    assert agents_service.registry_interactive(
        {"name": "marlow", "role": "cmo"}
    ) is True
    assert agents_service.registry_interactive(
        {"name": "x", "role": "dev", "capabilities": {"pm_cpo_cadence_owner": "true"}}
    ) is True
    # a polluted/synthetic name whose role contains 'lead' is NOT pulled Interactive
    assert agents_service.registry_interactive(
        {"name": "x-ddl-1", "role": "lead"}
    ) is False

    # the synthetic-name nudge
    assert agents_service.is_test_name("foo-test") is True
    assert agents_service.is_test_name("x-state-9") is True
    assert agents_service.is_test_name("ren") is False


def test_service_depends_only_on_port_not_outward():
    """GUARD: `app/agents/service.py` imports NOTHING outward (no fastapi / httpx /
    subprocess / psycopg2 / asyncpg) and does NOT reach for `app.main`, the concrete
    `app.appdb` / `app.adapters`, or the concrete `app.harness` / `app.providers` —
    only the domain port (+ the injected callables).

    Parsed via `ast` (a name in a comment/docstring can't fool it), mirroring
    `test_ports_purity.py` / the analytics guard. This is the module-isolation rule
    the `.importlinter` independence contract also enforces at the graph level."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "app" / "agents" / "service.py"
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
    # No reaching back into the blob, the concrete adapters/db, or the concrete
    # harness/providers (those are injected as callables at the shell).
    assert "app.main" not in dotted, "service.py must not import app.main"
    assert not any(
        m == "app.appdb"
        or m == "app.harness"
        or m == "app.providers"
        or m.startswith("app.adapters")
        for m in dotted
    ), "service.py must depend on the domain port + injected callables, not concretes"


# ---------------------------------------------------------------------------
#  api.py — the FastAPI router (imperative shell; builds svc over the port)
# ---------------------------------------------------------------------------


class FakeRosterSource:
    """A stand-in for the Cortex roster source the route uses (the real route
    fetches it from `cortex.get_agents`). Returns a scripted roster."""

    def __init__(self, roster):
        self._roster = roster

    async def get_agents(self, project_key):
        return list(self._roster)


@pytest.mark.asyncio
async def test_router_list_endpoint_returns_catalog():
    """Driving the `GET /agents/{project}` handler directly returns the service's
    grouped catalog (no ASGI / live DB) — fake port + fake roster source."""
    from app.agents import api as agents_api

    store = FakeOpStore(overrides=SAMPLE_OVERRIDES)
    roster = FakeRosterSource(SAMPLE_ROSTER)
    result = await agents_api.list_endpoint("kaidera-os", store=store, roster=roster)

    assert result["project"] == "kaidera-os"
    assert {v["name"] for v in result["interactive"]} == {"ren", "kai", "bob"}
    assert {v["name"] for v in result["autonomous"]} == {"cole", "kai-ddl-7"}
    assert result["orchestrator"] == "Cole"


@pytest.mark.asyncio
async def test_router_detail_endpoint_returns_agent():
    """Driving the `GET /agents/{project}/{agent}` handler directly returns one
    agent's resolved detail; an unknown agent → 404 (never a 500)."""
    from fastapi import HTTPException

    from app.agents import api as agents_api

    store = FakeOpStore(overrides=SAMPLE_OVERRIDES)
    roster = FakeRosterSource(SAMPLE_ROSTER)

    detail = await agents_api.detail_endpoint(
        "kaidera-os", "ren", store=store, roster=roster
    )
    assert detail["project"] == "kaidera-os"
    assert detail["agent"]["name"] == "ren"
    assert detail["designation"] == "interactive"

    with pytest.raises(HTTPException) as ei:
        await agents_api.detail_endpoint(
            "kaidera-os", "nobody", store=store, roster=roster
        )
    assert ei.value.status_code == 404


def test_router_detail_path_does_not_shadow_html_pane():
    """The JSON detail lives at the THREE-segment `/agents/{project}/{agent}/detail`
    leaf — NOT the two-segment `/agents/{project}/{agent}` the existing HTML
    agent-detail pane owns — so mounting the router additively can never shadow the
    HTML pane (the strictly-additive carve constraint)."""
    from app.agents.api import router

    paths = {r.path for r in router.routes}
    # the detail path carries the distinct /detail leaf …
    assert "/agents/{project}/{agent}/detail" in paths
    # … and the bare two-segment HTML-pane path is NOT claimed by this router.
    assert "/agents/{project}/{agent}" not in paths


def test_router_is_apirouter_with_routes():
    """`app.agents.api.router` is a FastAPI APIRouter exposing the catalog + detail
    paths under the module's prefix (so `main` can `include_router` it additively)."""
    from fastapi import APIRouter

    from app.agents.api import router

    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    assert "/agents/{project}" in paths
    assert "/agents/{project}/{agent}/detail" in paths


def test_module_exports_service_and_router():
    """`app.agents` re-exports the service + router (the module's public face)."""
    import app.agents as agents

    assert hasattr(agents, "AgentsService")
    assert hasattr(agents, "router")


# ---------------------------------------------------------------------------
#  config-catalog — the FULL harness→model+reasoning option catalog as JSON
#
#  The SPA Configure experience needs EVERY harness's model + reasoning option
#  sets up-front so it can re-populate the model/reasoning dropdowns CLIENT-SIDE
#  when an agent's harness <select> changes (no per-keystroke round-trip — the
#  same client-side repopulation the legacy `_settings_configure.html` does from
#  `harness_js_map`). `GET /agents/{project}/{agent}/detail` only returns the
#  CURRENT agent's resolved option set (the effective harness), so a dedicated
#  catalog endpoint exposes the whole map sourced from `harness.HARNESS_MODELS` /
#  `HARNESS_REASONING` (+ the live Providers catalog for the kaidera/pi
#  catalog lanes, grouped by provider).
#
#  Layer rule (same as the rest of the module): a PURE service shaper
#  (`build_config_catalog`, fed the harness constants + the providers catalog
#  groups) + the api shell. TDD'd against the REAL harness constants (they ARE
#  the contract) + a fake catalog-groups list (no providers fetch / no DB).
# ---------------------------------------------------------------------------


# A fake Providers catalog `groups` list (the shape app.providers.view_catalog()
# ['groups'] returns) — the kaidera/pi (catalog) model source. Mixes a chat
# row + an embedding row to prove only chat rows are offered as model options.
FAKE_CATALOG_GROUPS = [
    {
        "provider": "kaidera-manifold",
        "label": "Kaidera AI Manifold",
        "configured": True,
        "rows": [
            {
                "id": "ollama-cloud/minimax-m3",
                "display_name": "MiniMax M3",
                "type": "chat",
                "reasoning_levels": ["low", "medium", "high"],
            },
            {
                "id": "vendor/future-model",
                "display_name": "Future Model",
                "type": "chat",
                "reasoning_levels": ["low", "future"],
            },
            {"id": "text-embedding-x", "display_name": "Embedding X", "type": "embedding"},
        ],
    },
]

FAKE_PI_CATALOG_GROUPS = [
    {
        "provider": "openai-codex",
        "label": "OpenAI Codex",
        "rows": [
            {"id": "gpt-5.5", "display_name": "GPT-5.5", "type": "chat"},
        ],
    },
    {
        "provider": "fireworks",
        "label": "Fireworks",
        "rows": [
            {
                "id": "fireworks/accounts/fireworks/models/kimi-k2p6",
                "display_name": "Kimi K2.6",
                "type": "chat",
            },
        ],
    },
    {
        "provider": "ollama-cloud",
        "label": "Ollama Cloud",
        "rows": [
            {
                "id": "ollama-cloud/qwen3-coder:480b",
                "display_name": "qwen3-coder:480b",
                "type": "chat",
            },
        ],
    },
]


def test_build_config_catalog_covers_every_harness():
    """`build_config_catalog` returns the full Configure catalog the SPA needs:
    the harness <select> options (spec order), the per-harness model option sets,
    and the per-harness reasoning option sets — sourced 1:1 from the real
    `harness` constants so the SPA's dropdowns match the runner + the HTML."""
    from app.agents.service import build_config_catalog
    from app import harness as harness_cfg

    cat = build_config_catalog(harness_cfg, FAKE_CATALOG_GROUPS, FAKE_PI_CATALOG_GROUPS)

    # harnesses: value+label, in product order (claude-code · codex · kaidera · pi).
    assert [h["value"] for h in cat["harnesses"]] == harness_cfg.HARNESS_ORDER
    assert {h["value"] for h in cat["harnesses"]} == set(harness_cfg.HARNESSES)
    by_key = {h["value"]: h for h in cat["harnesses"]}
    assert by_key["claude-code"]["label"] == "Claude Code"
    # each harness carries its lane metadata (drives the small badge) +
    # model_source identifies each dynamic catalog protocol.
    assert by_key["kaidera"]["model_source"] == "catalog"
    assert by_key["pi"]["model_source"] == "pi-catalog"
    assert by_key["claude-code"]["model_source"] == "claude-catalog"
    assert by_key["codex"]["model_source"] == "codex-catalog"

    # models_by_harness: the FIXED lanes carry their per-harness {value,label} sets
    # 1:1 from HARNESS_MODELS.
    mbh = cat["models_by_harness"]
    builtin_claude_count = len(harness_cfg.HARNESS_MODELS["claude-code"])
    assert mbh["claude-code"][:builtin_claude_count] == harness_cfg.HARNESS_MODELS["claude-code"]
    assert mbh["codex"] == harness_cfg.HARNESS_MODELS["codex"]
    assert {m["value"] for m in mbh["pi"]} == {
        "gpt-5.5",
        "fireworks/accounts/fireworks/models/kimi-k2p6",
        "ollama-cloud/qwen3-coder:480b",
    }

    # reasoning_by_harness: per-harness levels 1:1 from HARNESS_REASONING, shaped
    # as {value,label} option objects (uniform with the model options).
    rbh = cat["reasoning_by_harness"]
    assert [o["value"] for o in rbh["claude-code"]] == harness_cfg.HARNESS_REASONING["claude-code"]
    assert [o["value"] for o in rbh["codex"]] == harness_cfg.HARNESS_REASONING["codex"]

    # defaults: the SPA seeds a brand-new override row with these.
    assert cat["default_harness"] == "claude-code"
    assert cat["default_model"]  # the default claude-code model id


def test_build_config_catalog_merges_operator_added_claude_models(monkeypatch):
    """Operator-added Claude Code models ride in the config catalog as additive rows
    and remain separated in custom_models_by_harness so the SPA can append safely."""
    from app.agents.service import build_config_catalog
    from app import harness as harness_cfg
    from app import settings

    monkeypatch.setattr(
        settings,
        "load_harness_model_overrides",
        lambda: {"claude-code": [{"value": "claude-future-5", "label": "Future 5"}]},
    )

    cat = build_config_catalog(harness_cfg, [], [])
    assert {"value": "claude-future-5", "label": "Future 5"} in cat["models_by_harness"]["claude-code"]
    assert cat["custom_models_by_harness"] == {
        "claude-code": [{"value": "claude-future-5", "label": "Future 5"}]
    }


def test_build_config_catalog_uses_dynamic_public_default(monkeypatch):
    """A public build exposes Kaidera AI first and still seeds a runnable model."""
    from app.agents.service import build_config_catalog
    from app import harness as harness_cfg

    monkeypatch.setattr(harness_cfg, "visible_harness_order", lambda: ["kaidera"])
    cat = build_config_catalog(harness_cfg, FAKE_CATALOG_GROUPS, FAKE_PI_CATALOG_GROUPS)

    assert cat["default_harness"] == "kaidera"
    assert cat["default_model"] in {
        row["value"] for row in cat["models_by_harness"]["kaidera"]
    }


def test_build_config_catalog_emits_per_model_reasoning_for_kaidera():
    """B3: the kaidera catalog lane exposes `reasoning_by_model` (the SELECTED
    model's own discovered levels) AND carries `reasoning_levels` on each model
    option. A non-reasoning model is OMITTED from the map (the SPA hides the
    dropdown); a binary-toggle placeholder (["supported"]) maps to a single 'on'."""
    from app import harness as harness_cfg
    from app.agents.service import build_config_catalog

    groups = [
        {
            "provider": "kaidera-manifold",
            "label": "Kaidera AI Manifold",
            "configured": True,
            "rows": [
                {"id": "vendor/reasoning", "display_name": "Reasoning", "type": "chat",
                 "reasoning_levels": ["minimal", "low", "medium", "high", "xhigh"]},
                {"id": "vendor/plain", "display_name": "Plain",
                 "type": "chat", "reasoning_levels": []},
                {"id": "vendor/thinking", "display_name": "Thinking",
                 "type": "chat", "reasoning_levels": ["low", "medium", "high"]},
                {"id": "vendor/toggle", "display_name": "Toggle", "type": "chat",
                 "reasoning_levels": ["supported"]},
            ],
        },
    ]
    cat = build_config_catalog(harness_cfg, groups, None)

    rbm = cat["reasoning_by_model"]
    # the selected model's own levels are keyed by the (namespaced) model value.
    assert [o["value"] for o in rbm["kaidera:kaidera-manifold/vendor/reasoning"]] == [
        "minimal", "low", "medium", "high", "xhigh"
    ]
    assert [o["value"] for o in rbm["kaidera:kaidera-manifold/vendor/thinking"]] == [
        "low", "medium", "high"
    ]
    assert [o["value"] for o in rbm["kaidera:kaidera-manifold/vendor/toggle"]] == ["on"]
    assert "kaidera:kaidera-manifold/vendor/plain" not in rbm

    # each kaidera model option ALSO carries its raw reasoning_levels.
    by_value = {m["value"]: m for m in cat["models_by_harness"]["kaidera"]}
    assert by_value["kaidera-manifold/vendor/reasoning"]["reasoning_levels"] == [
        "minimal", "low", "medium", "high", "xhigh"
    ]
    assert by_value["kaidera-manifold/vendor/plain"]["reasoning_levels"] == []


def test_build_config_catalog_groups_catalog_lanes_by_provider():
    """The kaidera/pi (catalog) lanes' models come from the Providers catalog,
    GROUPED by provider, CHAT models only (embeddings excluded) — so the SPA can
    render `<optgroup>`s. Each option carries its provider for the grouping."""
    from app.agents.service import build_config_catalog
    from app import harness as harness_cfg

    cat = build_config_catalog(harness_cfg, FAKE_CATALOG_GROUPS, FAKE_PI_CATALOG_GROUPS)
    mbh = cat["models_by_harness"]

    # Kaidera gets only Manifold chat rows; the embedding row is dropped.
    own = mbh["kaidera"]
    values = {m["value"] for m in own}
    assert values == {
        "kaidera-manifold/ollama-cloud/minimax-m3",
        "kaidera-manifold/vendor/future-model",
    }
    assert "text-embedding-x" not in values
    # each option carries a provider tag for client-side <optgroup> grouping
    by_value = {m["value"]: m for m in own}
    assert by_value["kaidera-manifold/ollama-cloud/minimax-m3"]["provider"] == "kaidera-manifold"
    assert by_value["kaidera-manifold/vendor/future-model"]["provider"] == "kaidera-manifold"
    assert by_value["kaidera-manifold/ollama-cloud/minimax-m3"]["label"] == "MiniMax M3"

    pi = mbh["pi"]
    pi_by_value = {m["value"]: m for m in pi}
    assert pi_by_value["gpt-5.5"]["provider"] == "openai-codex"
    assert pi_by_value["fireworks/accounts/fireworks/models/kimi-k2p6"]["provider"] == "fireworks"
    assert pi_by_value["ollama-cloud/qwen3-coder:480b"]["provider"] == "ollama-cloud"


def test_build_config_catalog_does_not_bridge_pi_providers_into_kaidera():
    """PI's own provider catalog never becomes a direct Kaidera provider lane."""
    from app import harness as harness_cfg
    from app.agents.service import build_config_catalog

    provider_groups = [
        {
            "provider": "direct-provider",
            "label": "Direct Provider",
            "configured": True,
            "rows": [
                {"id": "direct-model", "display_name": "Direct Model", "type": "chat"},
            ],
        }
    ]

    cat = build_config_catalog(harness_cfg, provider_groups, FAKE_PI_CATALOG_GROUPS)
    own = cat["models_by_harness"]["kaidera"]
    assert own == []


def test_build_config_catalog_no_provider_keys_empty_catalog_lane():
    """No configured providers (empty catalog groups) → the catalog lanes have an
    empty model list (the SPA shows the "add a provider key" hint), never raises."""
    from app.agents.service import build_config_catalog
    from app import harness as harness_cfg

    cat = build_config_catalog(harness_cfg, [], [])
    assert cat["models_by_harness"]["kaidera"] == []
    assert cat["models_by_harness"]["pi"] == harness_cfg.HARNESS_MODELS["pi"]
    # the fixed lanes are unaffected by an empty catalog
    builtin_claude_count = len(harness_cfg.HARNESS_MODELS["claude-code"])
    assert cat["models_by_harness"]["claude-code"][:builtin_claude_count] == harness_cfg.HARNESS_MODELS["claude-code"]


@pytest.mark.asyncio
async def test_router_config_catalog_endpoint_returns_catalog():
    """Driving the `GET /agents/{project}/config-catalog` handler directly returns
    the full harness/model/reasoning catalog as JSON (no ASGI / live DB). The
    providers catalog is resolved via the injected catalog source; here a fake
    returns the scripted groups."""
    from app.agents import api as agents_api

    async def fake_catalog_source():
        return FAKE_CATALOG_GROUPS

    async def fake_pi_catalog_source():
        return FAKE_PI_CATALOG_GROUPS

    async def fake_claude_catalog_source():
        return [{
            "value": "fable",
            "label": "Fable",
            "reasoning_levels": ["low", "medium", "high", "xhigh", "max"],
        }]

    async def fake_codex_catalog_source():
        return [{
            "value": "gpt-5.6-sol",
            "label": "GPT-5.6-Sol",
            "reasoning_levels": ["low", "medium", "high", "xhigh", "max", "ultra"],
        }]

    result = await agents_api.config_catalog_endpoint(
        "kaidera-os",
        catalog_source=fake_catalog_source,
        pi_catalog_source=fake_pi_catalog_source,
        claude_catalog_source=fake_claude_catalog_source,
        codex_catalog_source=fake_codex_catalog_source,
    )

    assert result["project"] == "kaidera-os"
    assert [h["value"] for h in result["harnesses"]][0] == "claude-code"
    assert result["models_by_harness"]["codex"]
    assert result["models_by_harness"]["codex"][0]["value"] == "gpt-5.6-sol"
    assert [
        row["value"] for row in result["reasoning_by_model"]["codex:gpt-5.6-sol"]
    ] == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert result["reasoning_by_harness"]["claude-code"]
    assert result["default_harness"] == "claude-code"


def test_reasoning_map_namespaces_same_model_id_by_harness():
    from app import harness as harness_cfg
    from app.agents.service import build_config_catalog

    pi_groups = [{
        "provider": "openai-codex",
        "label": "OpenAI Codex",
        "rows": [{
            "id": "gpt-shared",
            "display_name": "GPT Shared",
            "type": "chat",
            "reasoning_levels": ["off", "low", "high"],
        }],
    }]
    result = build_config_catalog(
        harness_cfg,
        [],
        pi_groups,
        claude_model_options=[],
        codex_model_options=[{
            "value": "gpt-shared",
            "label": "GPT Shared",
            "reasoning_levels": ["low", "medium", "ultra"],
        }],
    )

    assert [
        row["value"] for row in result["reasoning_by_model"]["codex:gpt-shared"]
    ] == ["low", "medium", "ultra"]
    assert [
        row["value"] for row in result["reasoning_by_model"]["pi:gpt-shared"]
    ] == ["off", "low", "high"]


@pytest.mark.asyncio
async def test_router_config_catalog_endpoint_degrades_on_catalog_failure():
    """A providers-catalog fetch failure degrades to the FIXED lanes only (empty
    catalog lanes) — never a 500 (the house-law graceful degrade). The fixed
    subscription lanes still render their full model sets."""
    from app.agents import api as agents_api

    async def boom_catalog_source():
        raise RuntimeError("providers offline")

    async def boom_pi_catalog_source():
        raise RuntimeError("pi offline")

    async def boom_subscription_catalog_source():
        raise RuntimeError("subscription CLI offline")

    result = await agents_api.config_catalog_endpoint(
        "kaidera-os",
        catalog_source=boom_catalog_source,
        pi_catalog_source=boom_pi_catalog_source,
        claude_catalog_source=boom_subscription_catalog_source,
        codex_catalog_source=boom_subscription_catalog_source,
    )
    # fixed lanes intact; catalog lanes empty; no raise
    assert result["models_by_harness"]["claude-code"]
    assert result["models_by_harness"]["kaidera"] == []
    assert result["models_by_harness"]["pi"]  # fixed PI fallback when host PI is down


def test_router_config_catalog_path_does_not_shadow_html_or_detail():
    """The catalog lives at the TWO-segment `/agents/{project}/config-catalog`
    leaf, registered on the router (mounted BEFORE the HTML routes), so it can't be
    shadowed by the HTML agent-detail pane, and its literal `config-catalog` leaf
    can't be mistaken for the 3-segment `/detail` JSON route."""
    from app.agents.api import router

    paths = {r.path for r in router.routes}
    assert "/agents/{project}/config-catalog" in paths
    # still additive — the bare HTML-pane path stays unclaimed by this router
    assert "/agents/{project}/{agent}" not in paths


# --- chat history endpoint (reload-safe chat) -------------------------------


@pytest.mark.asyncio
async def test_chat_history_endpoint_returns_turns_for_session():
    """`GET /agents/{p}/{a}/chat/history?session_id=…` returns the session's prior
    turns as `{user, reply}` oldest-first, reusing load_session_history. A None
    store / blank session degrades to `{turns: []}` — never a 500."""
    from app.agents import api as agents_api
    from app.domain.runstate import RunRecord, RunSpan

    class _FakeStore:
        async def recent(self, project=None, limit=20, *, session_id=None, lease_owner=None):
            # one completed chat turn for this session
            return [
                RunRecord(
                    run_id="r1",
                    project="kaidera-os",
                    agent="kai",
                    session_id="sess-1",
                    status="ok",
                    spans=[
                        RunSpan(seq=1, kind="input", text="hello"),
                        RunSpan(seq=2, kind="output", text="hi there"),
                    ],
                )
            ]

        async def get_run(self, run_id):
            return None  # not used — recent() already returned hydrated spans

    result = await agents_api.chat_history_endpoint(
        "kaidera-os", "kai", "sess-1", store=_FakeStore()
    )
    assert result["project"] == "kaidera-os"
    assert result["agent"] == "kai"
    assert result["session_id"] == "sess-1"
    assert result["turns"] == [{"user": "hello", "reply": "hi there"}]


@pytest.mark.asyncio
async def test_chat_history_endpoint_degrades_on_blank_session_or_no_store():
    """A blank session_id or a None store yields `{turns: []}` — the composer renders
    empty and a fresh chat still works (house-law graceful degrade, never a 500)."""
    from app.agents import api as agents_api

    blank = await agents_api.chat_history_endpoint(
        "kaidera-os", "kai", "  ", store=object()
    )
    assert blank["turns"] == []

    no_store = await agents_api.chat_history_endpoint(
        "kaidera-os", "kai", "sess-1", store=None
    )
    assert no_store["turns"] == []


def test_router_chat_history_path_is_registered_and_collision_free():
    """The chat-history leaf is a 4-segment GET distinct from the POST chat routes
    and the 3-segment /detail route."""
    from app.agents.api import router

    paths = {r.path for r in router.routes}
    assert "/agents/{project}/{agent}/chat/history" in paths
