"""Agents catalog feature logic — the roster READ/catalog side, behind the ports.

The functional core of the `agents` module (Track A, the SECOND feature carve —
analytics was first). It owns the catalog substance of the agents surface:

  1. LIST a project's roster, grouped Interactive (a lead you talk to) vs
     Autonomous (run by the orchestrator), classified OVERRIDE-FIRST (a per-agent
     console designation override wins over the registry-derived heuristic), plus
     the project's orchestrator label + the default lead to land on.
  2. GET one agent resolved to its effective config + designation + role + the
     per-agent config-view (the inline-editable harness/model/reasoning model).

LAYER RULE (arrows point inward, ratified design §3): this module depends ONLY on
`domain.ports.OperationalStorePort` (the abstraction over the per-agent override
store — `load_agent_overrides` / `get_agent_override`). It imports NOTHING outward
(no fastapi / httpx / subprocess / psycopg2 / asyncpg) and never reaches back into
`app.main`, the concrete `app.appdb` / `app.adapters`, or the concrete
`app.harness` / `app.providers`. The presentation pieces it needs — a per-agent
config RESOLVER (the card's effective harness/model + their human labels) and a
config-VIEW shaper (the inline-edit row model) — are INJECTED as plain callables
(the analytics pattern), so the service stays free of the concrete `harness`
module while still rendering the same labels; the shell (`api.py`) / `main.py`
inject the real `harness`-backed implementations when wiring, and the defaults
below keep the service self-contained for tests.

The ROSTER is passed IN by the caller (exactly as `AnalyticsService` takes its
`agents=` roster sourced from `cortex.get_agents`): the catalog is project-roster
shaping over the override store, and the roster's origin (the Cortex registry) is
the caller's concern — keeping this service port-pure with no Cortex coupling.

The classification + shaping logic is lifted 1:1 from `main._agent_view`,
`_group_agents`, `_classify_interactive`, `_has_cpo_tag`, `_registry_interactive`,
`_is_test_name`, `_orchestrator_label`, `_lead_agent_name` (+ the detail
resolution from `_agent_detail_view`), so the carve is behaviour-preserving —
`main.py` now delegates its catalog substance here, making this the single source
of that logic.

Graceful-degrade is the house law: a down override store (every read empty) just
means no override wins, so classification falls back to the registry heuristic and
the surface still renders — it never raises.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.domain import designation as _designation
from app.domain.ports import OperationalStorePort

# The two valid designation override values. DE-FORKED: re-exported from the single
# owner `app.domain.designation` (these ARE domain values — an override carrying one
# of them flips the Interactive/Autonomous classification ahead of the registry
# heuristic). Kept under these module-level names so the rest of this service (and
# its `__all__`) is unchanged; they can no longer drift from settings / settings_module.
DESIGNATION_INTERACTIVE = _designation.DESIGNATION_INTERACTIVE
DESIGNATION_AUTONOMOUS = _designation.DESIGNATION_AUTONOMOUS
DESIGNATION_DETERMINISTIC = _designation.DESIGNATION_DETERMINISTIC

# Role substrings that mark an agent Interactive (a lead you talk to) when no
# explicit designation override is set — the REGISTRY-DERIVED default heuristic
# (lifted from main._INTERACTIVE_ROLE_HINTS). Anything else defaults Autonomous.
_INTERACTIVE_ROLE_HINTS = ("cpo", "cmo", "lead", "pm", "product")

# Role substrings that earn the small lead tag on an Interactive row. A co-lead
# deliberately does NOT earn it (it's a co-lead, not THE lead).
_CPO_TAG_HINTS = ("cpo", "cmo", "lead")
_CPO_TAG_EXCLUDE = ("co-lead", "co lead", "colead")  # fitness:allow-literal "colead" is a role-string variant, not an agent name (the 'cole' substring is incidental)

# Name substrings / suffixes that hint a polluted / synthetic agent — rendered with
# a dim flag (a nudge, NOT authority). Lifted from main._TEST_MARKS.
_TEST_MARKS = ("-ddl-", "-state-", "-poc-", "-smoke", "-init-")

# The orchestrator role string the Autonomous-group attribution resolves against
# ("triggered by <orchestrator>"). An agent whose effective role is this is the
# project's orchestrator.
_ORCHESTRATOR_ROLE = "orchestrator"


# ---------------------------------------------------------------------------
#  Pure helpers — the classification primitives (lifted 1:1 from main.py)
# ---------------------------------------------------------------------------


def is_test_name(name: Optional[str]) -> bool:
    """True if a name reads as a polluted/synthetic worker (a `-test` suffix or one
    of the known synthetic marks). Lifted 1:1 from `main._is_test_name`."""
    nm = (name or "").lower()
    if nm.endswith("-test"):
        return True
    return any(mark in nm for mark in _TEST_MARKS)


def has_cpo_tag(role: str) -> bool:
    """True if a role string reads as THE lead (earns the small tag). Matches
    the lead hints but excludes co-lead variants so a co-lead is grouped Interactive
    without claiming the badge. Lifted 1:1 from `main._has_cpo_tag`."""
    low = (role or "").lower()
    if any(ex in low for ex in _CPO_TAG_EXCLUDE):
        return False
    return any(hint in low for hint in _CPO_TAG_HINTS)


def registry_interactive(agent: dict) -> bool:
    """The REGISTRY-DERIVED interactive heuristic (the fallback when no console
    designation override is set). Interactive = a lead you talk to. Primary signal
    is the role string; we also honor the richer runtime capability hints
    (`runtime_role`, legacy cadence owner hints) so a co-lead whose top-level role is a
    generic developer role still lands Interactive. A synthetic/polluted name is
    NOT pulled Interactive by the heuristic (those default Autonomous). Lifted 1:1
    from `main._registry_interactive`."""
    caps = agent.get("capabilities") or {}
    registry_designation = _normalize_designation(caps.get("designation"))
    if registry_designation == DESIGNATION_INTERACTIVE:
        return True
    if registry_designation in (DESIGNATION_AUTONOMOUS, DESIGNATION_DETERMINISTIC):
        return False

    if is_test_name(agent.get("name")):
        return False
    role = (agent.get("role") or "").lower()
    if any(hint in role for hint in _INTERACTIVE_ROLE_HINTS):
        return True
    legacy_role_field = "local" + "dev" + "_role"
    role_aliases = (
        "runtime_role",
        "kaidera_os_role",
        "platform_role",
        legacy_role_field,
    )
    local_role = " ".join(str(caps.get(key) or "") for key in role_aliases).lower()
    if any(hint in local_role for hint in _INTERACTIVE_ROLE_HINTS):
        return True
    # explicit cadence-owner flag (string "true" or bool True both seen)
    if str(caps.get("pm_cpo_cadence_owner")).lower() == "true":
        return True
    return False


def classify_interactive(agent: dict, designation: str = "") -> bool:
    """Classify an agent Interactive vs Autonomous — OVERRIDE-FIRST. A console
    `designation` override ("interactive"/"autonomous") ALWAYS wins; otherwise fall
    back to the registry heuristic. The single decision point for the grouping.
    Lifted 1:1 from `main._classify_interactive`."""
    if designation == DESIGNATION_INTERACTIVE:
        return True
    if designation in (DESIGNATION_AUTONOMOUS, DESIGNATION_DETERMINISTIC):
        # Both explicit non-interactive tiers (AI worker + deterministic) classify as
        # non-interactive. A `deterministic` agent must NOT fall through to the
        # registry heuristic below — that could wrongly flag it Interactive and give
        # it a chat box. Only an UNSET designation uses the heuristic.
        return False
    return registry_interactive(agent)


def _registry_designation(agent: dict) -> str:
    """The registry-heuristic designation as a value (for the "registry: …" hint +
    as the effective designation when no override is set)."""
    caps = agent.get("capabilities") or {}
    registry_designation = _normalize_designation(caps.get("designation"))
    if registry_designation:
        return registry_designation
    return DESIGNATION_INTERACTIVE if registry_interactive(agent) else DESIGNATION_AUTONOMOUS


def _override_store_key(project: Optional[str], agent: str) -> str:
    """Compose the "{project}:{agent}" override-store key (lower-cased, blank-safe).

    DELEGATES to `app.domain.designation.override_store_key` — the single owner of
    this pure helper (de-fork; this body USED to be a third copy of the one in
    settings.py + settings_module.service). Behaviour-identical."""
    return _designation.override_store_key(project, agent)


def _normalize_designation(raw: Any) -> str:
    """Coerce a stored designation to a known value or "" (no override). The store
    already cleans this, but the service stays robust to a raw value.

    DELEGATES to `app.domain.designation.normalize_designation` — the single owner of
    this pure helper (de-fork). Behaviour-identical."""
    return _designation.normalize_designation(raw)


# ---------------------------------------------------------------------------
#  Default presentation callables — so the service is self-contained (no concrete
#  harness/providers dependency). The shell injects the real harness-backed ones.
# ---------------------------------------------------------------------------


def _default_resolve_config(agent: dict, override: dict) -> dict:
    """Fallback per-agent config resolver when none is injected. Returns the card's
    effective harness/model + their (here pass-through) human labels from the
    override-overlaid-on-registry values. The real injected resolver wraps
    `harness._registry_config` + `canonical_harness` + `harness_label` + the
    model-label map so the labels match the rest of the UI exactly."""
    caps = agent.get("capabilities") or {}
    harness = (override.get("harness") or caps.get("harness") or caps.get("provider") or "")
    harness = str(harness).strip().lower() or None
    model = (override.get("model") or agent.get("model") or caps.get("model_preference") or "")
    model = str(model).strip() or None
    return {
        "harness": harness,
        "harness_label": harness or "—",
        "model": model,
        "model_label": model,
        "thinking": caps.get("thinking"),
    }


def _default_config_view(
    agent: dict, override: dict, catalog_groups: list, registry_designation: str,
    pi_catalog_groups: list | None = None,
) -> dict:
    """Fallback config-view shaper when none is injected — the minimal effective
    row model. The real injected shaper wraps `harness.agent_config_view` (the full
    inline-edit model with the dropdown option sets)."""
    caps = agent.get("capabilities") or {}
    name = agent.get("name") or ""
    display = caps.get("display_name") or name
    eff_role = (override.get("role") or "").strip() or agent.get("role") or "—"
    eff_designation = _normalize_designation(override.get("designation")) or (
        registry_designation or ""
    )
    return {
        "name": name,
        "display_name": display,
        "role": eff_role,
        "designation": eff_designation,
        "reg_designation": registry_designation or "",
    }


# ---------------------------------------------------------------------------
#  The service
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
#  Config catalog — the FULL harness→model+reasoning option sets (for the SPA)
# ---------------------------------------------------------------------------


def _catalog_chat_options(
    catalog_groups: Optional[list],
    *,
    namespace_provider: bool = False,
) -> list[dict[str, Any]]:
    """Flatten the Providers catalog `groups` into a FLAT provider-tagged option
    list — CHAT models only (the API lanes drive chat). Each option is
    {value,label,provider} so the SPA can build `<optgroup>`s client-side.
    `namespace_provider=True` prefixes saved values as `<provider>/<id>` so the
    Kaidera AI runner knows which configured provider key/base URL to use. An empty/None
    catalog → []."""
    out: list[dict[str, Any]] = []
    for g in catalog_groups or []:
        provider = g.get("provider") or ""
        for row in g.get("rows", []):
            if (row.get("type") or "chat") != "chat":
                continue
            rid = row.get("id")
            if not rid:
                continue
            value = str(rid)
            if namespace_provider and provider and not value.startswith(f"{provider}/"):
                value = f"{provider}/{value}"
            option: dict[str, Any] = {
                "value": value,
                "label": row.get("display_name") or rid,
                "provider": provider,
            }
            # Presence matters: [] means a KNOWN non-reasoning model, while a missing
            # key means an older catalog that did not advertise effort capabilities.
            if "reasoning_levels" in row:
                option["reasoning_levels"] = list(row.get("reasoning_levels") or [])
            out.append(option)
    return out


def _kaidera_catalog_groups(
    catalog_groups: Optional[list],
    pi_catalog_groups: Optional[list],
) -> list:
    """Return configured Manifold catalog rows for the Kaidera harness."""
    del pi_catalog_groups
    return [
        group
        for group in (catalog_groups or [])
        if isinstance(group, dict)
        and group.get("configured")
        and group.get("provider") == "kaidera-manifold"
    ]


def _merge_model_options(primary: Optional[list], fallback: Optional[list]) -> list:
    """Ordered model-option union; live rows win, fallbacks fill missing values."""
    out: list = []
    seen: set[str] = set()
    for option in [*(primary or []), *(fallback or [])]:
        if not isinstance(option, dict):
            continue
        value = str(option.get("value") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(dict(option))
    return out


def build_config_catalog(
    harness_cfg: Any,
    catalog_groups: Optional[list],
    pi_catalog_groups: Optional[list] = None,
    claude_model_options: Optional[list] = None,
    codex_model_options: Optional[list] = None,
) -> dict[str, Any]:
    """Assemble the full Configure CATALOG the SPA needs to render + repopulate the
    per-agent harness/model/reasoning dropdowns CLIENT-SIDE (no per-keystroke
    round-trip — the same map the legacy `_settings_configure.html` builds from
    `harness.harness_js_map`).

    `harness_cfg` is the `app.harness` module (INJECTED, not imported — keeps this
    module free of the concrete harness per the layer rule, the analytics
    injection pattern); `catalog_groups` is `app.providers.view_catalog()['groups']`
    (the kaidera catalog-lane model source) and `pi_catalog_groups` is the
    host PI `pi --list-models` catalog. Sourced 1:1 from
    Claude/Codex options come from their host CLI discovery bridges; curated
    ``HARNESS_MODELS`` rows are outage fallbacks. The SPA option sets therefore
    match the installed harnesses and the runner rather than a release-time snapshot.

    Shape:
        {
          "harnesses": [{value,label,model_source,lane,lane_label}],  # the <select>, spec order
          "models_by_harness": {harness: [{value,label,provider?,reasoning_levels?}]},  # fixed lanes 1:1; catalog lanes provider-tagged + per-model levels
          "reasoning_by_harness": {harness: [{value,label}]},         # per-harness levels (fixed lanes)
          "reasoning_by_model": {"<harness>:<model_value>": [{value,label}]},  # namespaced to avoid cross-harness model-id collisions
          "default_harness": "claude-code",
          "default_model": "<the default claude-code model id>",
        }
    A fixed lane carries its static per-harness {value,label} model set; kaidera
    carries provider-prefixed API model values; pi carries its live PI model values
    and falls back to HARNESS_MODELS["pi"] when the host PI catalog is unavailable."""
    # EDITION/LICENSE gate: PUBLIC offers only kaidera + license-granted harnesses.
    order_fn = getattr(harness_cfg, "visible_harness_order", None)
    order: list[str] = list(order_fn() if callable(order_fn) else harness_cfg.HARNESS_ORDER)
    specs: dict[str, dict] = harness_cfg.HARNESSES
    fixed_models: dict[str, list] = harness_cfg.HARNESS_MODELS
    model_options_for = getattr(harness_cfg, "harness_model_options", None)
    custom_model_options_for = getattr(harness_cfg, "custom_harness_model_options", None)
    reasoning: dict[str, list] = harness_cfg.HARNESS_REASONING

    own_catalog_groups = _kaidera_catalog_groups(catalog_groups, pi_catalog_groups)
    catalog_options = _catalog_chat_options(own_catalog_groups, namespace_provider=True)
    pi_catalog_options = _catalog_chat_options(pi_catalog_groups)

    # B3: per-model reasoning for the kaidera (catalog) lane — {model_value:
    # [{value,label}]} built from each catalog model's discovered reasoning_levels
    # (B2). The SPA's reasoning dropdown reads THIS for the selected kaidera model
    # instead of the fixed per-harness list, and hides/disables when it's empty
    # (a non-reasoning model). Other (fixed) lanes keep reasoning_by_harness.
    reasoning_by_model = _reasoning_by_model(catalog_options, harness="kaidera")

    harnesses: list[dict[str, Any]] = []
    models_by_harness: dict[str, list] = {}
    custom_models_by_harness: dict[str, list] = {}
    reasoning_by_harness: dict[str, list] = {}
    for key in order:
        spec = specs.get(key, {})
        model_source = spec.get("model_source", "fixed")
        harnesses.append(
            {
                "value": key,
                "label": spec.get("label", key),
                "model_source": model_source,
                "lane": spec.get("lane"),
                "lane_label": spec.get("lane_label"),
            }
        )
        # Dynamic lanes pull their own provider-grouped catalog; fixed lanes carry
        # their static per-harness {value,label} set. PI keeps a fixed fallback so a
        # down host service does not leave the configured subscription lane unusable.
        fallback = model_options_for(key) if callable(model_options_for) else fixed_models.get(key, [])
        if model_source == "catalog":
            models_by_harness[key] = list(catalog_options)
        elif model_source == "pi-catalog":
            models_by_harness[key] = list(pi_catalog_options or fallback)
        elif model_source == "claude-catalog":
            models_by_harness[key] = _merge_model_options(claude_model_options, fallback)
        elif model_source == "codex-catalog":
            models_by_harness[key] = list(codex_model_options or fallback)
        else:
            models_by_harness[key] = list(fallback)
        if model_source in {
            "catalog", "claude-catalog", "codex-catalog", "pi-catalog"
        }:
            reasoning_by_model.update(
                _reasoning_by_model(models_by_harness[key], harness=key)
            )
        custom = custom_model_options_for(key) if callable(custom_model_options_for) else []
        if custom:
            custom_models_by_harness[key] = list(custom)
        # reasoning levels → uniform {value,label} option objects.
        reasoning_by_harness[key] = [
            {"value": lvl, "label": lvl} for lvl in reasoning.get(key, [])
        ]

    # the runner's default routing (the same constants the api shell fills an
    # unconfigured default-lane agent with) — the SPA seeds a new override row.
    default_harness = order[0] if order else "claude-code"
    default_models = models_by_harness.get(default_harness, [])
    default_model = ""
    if default_models:
        recommended = next(
            (row for row in default_models if isinstance(row, dict) and row.get("is_default")),
            None,
        )
        selected = recommended or default_models[0]
        if isinstance(selected, dict):
            default_model = str(selected.get("value") or "")
    if not default_model:
        default_for = getattr(harness_cfg, "harness_default_model", None)
        if callable(default_for):
            default_model = str(default_for(default_harness) or "")

    return {
        "harnesses": harnesses,
        "models_by_harness": models_by_harness,
        "custom_models_by_harness": custom_models_by_harness,
        "reasoning_by_harness": reasoning_by_harness,
        "reasoning_by_model": reasoning_by_model,
        "default_harness": default_harness,
        "default_model": default_model,
    }


def _reasoning_by_model(
    catalog_options: list[dict[str, Any]],
    *,
    harness: str,
) -> dict[str, list]:
    """Map ``<harness>:<model>`` to the model's reasoning options.

    Harness namespacing is required because subscription catalogs can expose the
    same model id with different capabilities (for example Codex and PI both list
    ``gpt-5.5``).

    A known model with no levels is omitted (the model option itself retains an
    explicit ``reasoning_levels=[]``, which tells the SPA to hide the dropdown).
    The ["supported"] placeholder (reasons but no selectable ladder — a binary
    toggle provider) maps to a single {"on"} option so the operator can still turn
    thinking on; the apply core emits the provider's toggle form for it."""
    out: dict[str, list] = {}
    for opt in catalog_options or []:
        value = opt.get("value")
        if not value or "reasoning_levels" not in opt:
            continue
        levels = opt.get("reasoning_levels") or []
        if not levels:
            continue
        if levels == ["supported"]:
            out[f"{harness}:{value}"] = [{"value": "on", "label": "on"}]
        else:
            out[f"{harness}:{value}"] = [
                {"value": lvl, "label": lvl} for lvl in levels
            ]
    return out


class AgentsService:
    """The agents CATALOG: list a project's roster grouped + classified, and
    resolve one agent's effective config/designation/role.

    Construct with the `OperationalStorePort` (the per-agent override source); the
    two presentation callables default to self-contained functions and can be
    overridden with the concrete `harness`-backed implementations at the shell so
    the labels match the rest of the UI. The roster is passed to each call (the
    caller fetches it from Cortex), keeping the service port-pure."""

    def __init__(
        self,
        *,
        store: Optional[OperationalStorePort] = None,
        resolve_config: Callable[[dict, dict], dict] = _default_resolve_config,
        config_view: Callable[[dict, dict, list, str], dict] = _default_config_view,
    ) -> None:
        # `store` is optional so a caller that already holds the override map (e.g.
        # main.py threading it through a render path) can call the pure shaping
        # helpers directly; `list_agents`/`get_agent` read the store themselves.
        self._store = store
        self._resolve_config = resolve_config
        self._config_view = config_view

    # -- pure shaping (no store read) ------------------------------------------

    def agent_view(
        self,
        agent: dict,
        designation: str = "",
        role_override: str = "",
        override: Optional[dict] = None,
    ) -> dict:
        """Flatten a runtime/roster agent into the fields the agents column needs.

        `designation` (console override, "" if none) drives the Interactive vs
        Autonomous classification override-first; `role_override` wins over the
        registry role for the displayed role + the lead-tag derivation; `override`
        (the console-local row, {} if none) makes the card's harness/model show the
        SAME resolved value the runner uses (via the injected resolver). `cpo_tag`
        is True when the EFFECTIVE role of an INTERACTIVE agent reads as lead.
        Lifted 1:1 from `main._agent_view` (the harness/model resolution delegated
        to the injected resolver instead of the inline harness calls)."""
        caps = agent.get("capabilities") or {}
        name = agent.get("name") or ""
        display = caps.get("display_name") or name
        override = override or {}

        cfg = self._resolve_config(agent, override)
        harness = cfg.get("harness")
        harness_label = cfg.get("harness_label") or "—"
        model = cfg.get("model")
        model_label = cfg.get("model_label")
        thinking = cfg.get("thinking") if cfg.get("thinking") is not None else caps.get("thinking")
        effective_designation = _normalize_designation(designation) or _registry_designation(agent)
        if effective_designation == DESIGNATION_DETERMINISTIC:
            harness = None
            harness_label = "Deterministic"
            model = None
            model_label = None
            thinking = None

        writer_scope = agent.get("writer_scope") or caps.get("writer_scope")
        primary = caps.get("primary") or []
        if not isinstance(primary, list):
            primary = []

        # avatar initials (first two letters of the display name, upper).
        initials = (display[:2] or name[:2] or "··").upper()

        # one-line mono subtitle: harness · model · thinking (skip blanks), built
        # from the resolved human labels so the card reads the RESOLVED config.
        sub_parts = [
            p for p in (harness_label, model_label, thinking and f"{thinking} reasoning") if p
        ]
        row_sub = " · ".join(sub_parts) if sub_parts else (writer_scope or "—")

        # effective role: console role override wins over the registry role.
        eff_role = (role_override or "").strip() or agent.get("role") or "—"
        interactive = classify_interactive(agent, designation)
        cpo_tag = interactive and has_cpo_tag(eff_role)

        return {
            "name": name,
            "display_name": display,
            "initials": initials,
            "role": eff_role,
            "model": model,
            "model_label": model_label,
            "harness": harness,
            "harness_label": harness_label,
            "thinking": thinking,
            "writer_scope": writer_scope,
            "capabilities": primary[:3],
            "row_sub": row_sub,
            "is_test": is_test_name(name),
            "interactive": interactive,
            "designation_override": bool(designation),
            "cpo_tag": cpo_tag,
        }

    def group_agents(
        self, agents: list[dict], project: Optional[str], overrides: dict
    ) -> dict[str, list[dict]]:
        """Split agents into Interactive vs Autonomous groups (each a list of
        flattened views), sorted by display name. Classification is OVERRIDE-FIRST:
        each agent's console designation override (keyed "{project}:{agent}") wins;
        absent that, the registry heuristic decides. `overrides` is the pre-loaded
        map (read once by the caller). Lifted 1:1 from `main._group_agents`."""
        interactive: list[dict] = []
        autonomous: list[dict] = []
        for a in agents:
            name = a.get("name") or ""
            ov = overrides.get(_override_store_key(project, name), {})
            designation = _normalize_designation(ov.get("designation"))
            view = self.agent_view(a, designation, ov.get("role", ""), override=ov)
            (interactive if view["interactive"] else autonomous).append(view)
        interactive.sort(key=lambda v: v["display_name"].lower())
        autonomous.sort(key=lambda v: v["display_name"].lower())
        return {"interactive": interactive, "autonomous": autonomous}

    def orchestrator_label(
        self, agents: list[dict], project: Optional[str], overrides: dict
    ) -> Optional[str]:
        """The display name of THIS project's orchestrator-role agent (the
        Autonomous-group attribution reads "triggered by <it>"), or None when the
        project has no orchestrator. Resolved dynamically from the roster: an agent
        whose EFFECTIVE role (override-first) is the orchestrator role. Lifted 1:1
        from `main._orchestrator_label`."""
        def _candidate(agent: dict, ov: dict) -> Optional[str]:
            caps = agent.get("capabilities") or {}
            display = caps.get("display_name") or ""
            name = agent.get("name") or ""
            if display:
                return display
            return name.title() if name else None

        fallback: Optional[str] = None
        for a in agents:
            name = a.get("name") or ""
            ov = overrides.get(_override_store_key(project, name), {})
            eff_role = (ov.get("role") or "").strip() or a.get("role") or ""
            if eff_role.strip().lower() == _ORCHESTRATOR_ROLE:
                effective_designation = _normalize_designation(ov.get("designation")) or _registry_designation(a)
                label = _candidate(a, ov)
                if effective_designation == DESIGNATION_DETERMINISTIC:
                    return label
                fallback = fallback or label
        return fallback

    def lead_agent_name(self, groups: dict) -> Optional[str]:
        """The project's lead agent name — the default landing pane on a project
        switch. Prefers the interactive agent carrying the lead tag, else the first
        interactive agent, else None. Reuses the already-classified groups. Lifted
        1:1 from `main._lead_agent_name`."""
        interactive = groups.get("interactive") or []
        for v in interactive:
            if v.get("cpo_tag"):
                return v.get("name")
        return interactive[0].get("name") if interactive else None

    @staticmethod
    def find_agent(agents: list[dict], agent_name: str) -> Optional[dict]:
        """Locate one agent record (case-insensitive name match) in the roster.
        Lifted 1:1 from `main._find_agent`."""
        target = (agent_name or "").lower()
        for a in agents:
            if (a.get("name") or "").lower() == target:
                return a
        return None

    # -- the catalog surface (reads the override store) ------------------------

    async def list_agents(
        self, project: str, agents: list[dict]
    ) -> dict[str, Any]:
        """The roster CATALOG for a project: the Interactive/Autonomous groups, the
        orchestrator label, and the default lead. Reads the per-agent override map
        ONCE from the store and threads it through the grouping/orchestrator/lead
        resolution. `agents` is the roster the caller fetched (from Cortex). Never
        raises — a down store yields {} overrides → registry-heuristic grouping."""
        overrides = self._load_overrides()
        groups = self.group_agents(agents, project, overrides)
        return {
            "interactive": groups["interactive"],
            "autonomous": groups["autonomous"],
            "orchestrator": self.orchestrator_label(agents, project, overrides),
            "lead": self.lead_agent_name(groups),
        }

    async def get_agent(
        self,
        project: str,
        agent_name: str,
        agents: list[dict],
        *,
        catalog_groups: Optional[list] = None,
        pi_catalog_groups: Optional[list] = None,
    ) -> Optional[dict[str, Any]]:
        """Resolve ONE agent to its detail-header substance: the flattened view, the
        effective designation + role (override-first), the registry-derived
        designation (the "registry: …" hint), and the per-agent config-view (the
        inline-edit harness/model/reasoning model, via the injected shaper). Returns
        None when the agent isn't in the roster (the caller degrades to the
        Dashboard). Lifted from `main._agent_detail_view`'s resolution half. Never
        raises — a down store yields {} override → registry classification."""
        agent = self.find_agent(agents, agent_name)
        if agent is None:
            return None
        ov = self._get_override(project, agent.get("name") or "")
        designation = _normalize_designation(ov.get("designation"))
        view = self.agent_view(agent, designation, ov.get("role", ""), override=ov)
        reg_designation = _registry_designation(agent)
        config_view = self._config_view(
            agent, ov, catalog_groups or [], reg_designation,
            pi_catalog_groups=pi_catalog_groups,
        )
        return {
            "agent": view,
            "designation": designation or reg_designation,
            "role": view["role"],
            "registry_designation": reg_designation,
            "config_view": config_view,
        }

    # -- store access (graceful-degrade) ---------------------------------------

    def _load_overrides(self) -> dict[str, dict[str, str]]:
        """The whole per-agent override map ({} when no store / a down store)."""
        if self._store is None:
            return {}
        try:
            return self._store.load_agent_overrides() or {}
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return {}

    def _get_override(self, project: str, agent: str) -> dict[str, str]:
        """One agent's override ({} when no store / a down store)."""
        if self._store is None:
            return {}
        try:
            return self._store.get_agent_override(project, agent) or {}
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return {}


__all__ = [
    "AgentsService",
    "DESIGNATION_INTERACTIVE",
    "DESIGNATION_AUTONOMOUS",
    "has_cpo_tag",
    "registry_interactive",
    "classify_interactive",
    "is_test_name",
    "build_config_catalog",
]
