"""Harness → model → reasoning relationships (R4c) — the Configure data layer.

Encodes the per-harness model + reasoning/effort maps and assembles the
render-ready per-agent Configure view model:

  * `harness ∈ {claude-code, codex, pi}` selects an external CLI execution
    lane. Each harness owns its authentication outside Kaidera OS.
  * MODELS are per-harness:
      - claude-code → live CLI-advertised aliases + operator/fallback models
      - codex       → live Codex app-server catalog (curated fallback)
      - pi          → the host PI catalog (`pi --list-models`), grouped by provider
                       (OpenAI-Codex subscription plus any provider/API PI can see)
  * REASONING/EFFORT is per-harness (§4 / §6):
      - claude-code {low, medium, high, xhigh, max}
      - codex       per-model (currently low through xhigh/max/ultra)
      - pi          per-model from `pi --list-models` + live `--thinking` choices

  * The CURRENT EFFECTIVE config for an agent is the registry value (from the
    /projects/{key}/runtime `capabilities`: harness/provider, model /
    model_preference, thinking) overlaid with any console-local override
    (app.settings.get_agent_override). The override wins for display.

CONSOLE-LOCAL BY DESIGN (feature-gap #81, the CTO's reversed decision): the per-agent
overrides this view feeds stay console-local in the app-DB settings store as the fast
display/routing overlay, and a SAVE writes ONLY that override — the Cortex agent
registry (`capabilities`, E006 Inc04 — the source of truth) is NOT touched on save.
Committing the config to the registry is the explicit "Promote to registry" action via
`registry_sync.promote_agent_to_registry` (`POST /agents` UPSERT). This VIEW is
read/shape-only (see app.registry_sync).

Pure data + view-shaping: nothing here writes Cortex or calls a model service.
Harness catalogs come from their host CLIs, with curated lists used only as
outage fallbacks.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
#  Harness definitions — external CLI lanes only.
# ---------------------------------------------------------------------------
#
# Order is the product order: claude-code · codex · pi.
# `lane` is informational and `model_source` identifies the host-CLI catalog.

HARNESS_ORDER = ["claude-code", "codex", "pi"]

HARNESSES: dict[str, dict[str, Any]] = {
    "claude-code": {
        "label": "Claude Code",
        "lane": "subscription",
        "lane_label": "subscription · subprocess",
        "model_source": "claude-catalog",
    },
    "codex": {
        "label": "Codex",
        "lane": "subscription",
        "lane_label": "subscription · subprocess",
        "model_source": "codex-catalog",
    },
    "pi": {
        "label": "pi",
        # pi is operationally a SUBSCRIPTION subprocess lane, but its CLI also
        # exposes provider/API models visible to the logged-in PI environment. The
        # SPA catalog therefore comes from the host service's `pi --list-models`
        # bridge, with HARNESS_MODELS["pi"] kept as a safe fallback/default.
        "lane": "subscription",
        "lane_label": "subscription · subprocess",
        "model_source": "pi-catalog",
    },
}

# ---------------------------------------------------------------------------
#  Per-harness MODEL sets. PI uses the host catalog with this list as fallback.
# ---------------------------------------------------------------------------
#
# Each entry is {value, label}. Values are the model ids the harness's --model /
# -m flag accepts (claude short aliases and codex slugs). Subscription entries
# are a current fallback only; app.codex_catalog replaces them with the installed
# CLI's picker-visible model/list response whenever discovery succeeds.

HARNESS_MODELS: dict[str, list[dict[str, Any]]] = {
    "claude-code": [
        {"value": "opus", "label": "Opus 4.8"},
        {"value": "claude-opus-4-8[1m]", "label": "Opus 4.8 (1M context)"},
        {"value": "sonnet", "label": "Sonnet 4.7"},
        {"value": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
        {"value": "haiku", "label": "Haiku 4.5"},
        {"value": "fable", "label": "Fable 5"},
        {"value": "claude-fable-5", "label": "Fable 5 (full id)"},
    ],
    # Fallback for an absent/old Codex CLI. The normal source is the installed CLI's
    # stable app-server `model/list` response, including model-specific effort levels.
    # This snapshot was verified against codex-cli 0.144.1 on 2026-07-10.
    "codex": [
        {"value": "gpt-5.6-sol", "label": "GPT-5.6-Sol", "is_default": True,
         "reasoning_levels": ["low", "medium", "high", "xhigh", "max", "ultra"]},
        {"value": "gpt-5.6-terra", "label": "GPT-5.6-Terra",
         "reasoning_levels": ["low", "medium", "high", "xhigh", "max", "ultra"]},
        {"value": "gpt-5.6-luna", "label": "GPT-5.6-Luna",
         "reasoning_levels": ["low", "medium", "high", "xhigh", "max"]},
        {"value": "gpt-5.5", "label": "GPT-5.5",
         "reasoning_levels": ["low", "medium", "high", "xhigh"]},
        {"value": "gpt-5.4", "label": "GPT-5.4",
         "reasoning_levels": ["low", "medium", "high", "xhigh"]},
        {"value": "gpt-5.4-mini", "label": "GPT-5.4-Mini",
         "reasoning_levels": ["low", "medium", "high", "xhigh"]},
        {"value": "gpt-5.3-codex-spark", "label": "GPT-5.3-Codex-Spark",
         "reasoning_levels": ["low", "medium", "high", "xhigh"]},
    ],
    # pi drives the OpenAI Codex / ChatGPT subscription via the `pi` CLI
    # (`--provider openai-codex`). These are the provider `openai-codex` models
    # VERIFIED live via `pi --list-models` on pi 0.80.3 (2026-07-10): the exact
    # ids pi lists under that provider — do NOT invent ids. gpt-5.3-codex-spark
    # is the confirmed default (text-only); the rest support images. pi
    # authenticates via the openai-codex OAuth (`~/.pi/agent/auth.json`), NOT an
    # API key, so the harness's stripped child env (no OPENAI_API_KEY) is correct.
    "pi": [
        {"value": "gpt-5.5", "label": "GPT-5.5"},
        {"value": "gpt-5.4", "label": "GPT-5.4"},
        {"value": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"value": "gpt-5.3-codex", "label": "GPT-5.3 Codex"},
        {"value": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark"},
        {"value": "gpt-5.2", "label": "GPT-5.2"},
    ],
}

# ---------------------------------------------------------------------------
#  Per-harness REASONING / EFFORT levels.
# ---------------------------------------------------------------------------
#
# claude-code {low,medium,high,xhigh,max}; codex uses model/list per-model levels;
# PI entries below are only outage fallbacks; live catalogs carry exact levels.

HARNESS_REASONING: dict[str, list[str]] = {
    "claude-code": ["low", "medium", "high", "xhigh", "max"],
    # Union fallback only. The SPA uses each discovered model's exact ladder.
    "codex": ["low", "medium", "high", "xhigh", "max", "ultra"],
    # pi's `--thinking` levels, VERIFIED via `pi --help` / `pi --list-models` on
    # pi 0.80.3 (2026-07-10): off|minimal|low|medium|high|xhigh. (`minimal` maps
    # to `low` for openai-codex models; we still offer the full CLI level set.)
    "pi": ["off", "minimal", "low", "medium", "high", "xhigh"],
}

# How an agent's registry `capabilities.provider` (or a legacy harness alias)
# maps onto our canonical harness keys, so an agent stored as provider
# "openai-codex" still resolves to a real dropdown option.
_HARNESS_ALIASES: dict[str, str] = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "anthropic": "claude-code",
    "codex": "codex",
    "openai-codex": "codex",
    "openai": "codex",
    "pi": "pi",
}


def canonical_harness(value: str | None) -> str | None:
    """Resolve a registry harness/provider string to a canonical harness key.

    Tries an exact key, then the alias table, then a blank → None. An unknown
    non-blank value is returned lower-cased as-is (so the UI can still show it as
    a current value even if it isn't one of the four known options)."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v in HARNESSES:
        return v
    if v in _HARNESS_ALIASES:
        return _HARNESS_ALIASES[v]
    return v or None


def harness_label(value: str | None) -> str:
    """Human label for a (possibly non-canonical) harness value."""
    canon = canonical_harness(value)
    if canon and canon in HARNESSES:
        return HARNESSES[canon]["label"]
    return value or "—"


CUSTOM_MODEL_HARNESSES = {"claude-code"}


def custom_harness_model_options(harness: str | None) -> list[dict[str, str]]:
    """Operator-added fixed-lane model options for one harness."""
    canon = canonical_harness(harness)
    if canon not in CUSTOM_MODEL_HARNESSES:
        return []
    try:
        from app import settings as _settings

        return list(_settings.load_harness_model_overrides().get(canon or "", []))
    except Exception:
        return []


def harness_model_options(harness: str | None) -> list[dict[str, Any]]:
    """Live/cached model options plus curated and operator-added fallbacks.

    Claude and Codex discovery is primed asynchronously by the console, then read
    here without I/O. Claude help is illustrative rather than exhaustive, so those
    rows augment the curated/operator list. Codex app-server returns the complete
    picker catalog, so a live result replaces its fallback snapshot.
    """
    canon = canonical_harness(harness)
    base = list(HARNESS_MODELS.get(canon or "", []))
    discovered: list[dict[str, Any]] = []
    try:
        if canon == "claude-code":
            from app import claude_catalog

            discovered = claude_catalog.cached_claude_model_options()
        elif canon == "codex":
            from app import codex_catalog

            discovered = codex_catalog.cached_codex_model_options()
    except Exception:
        discovered = []

    if canon == "codex" and discovered:
        candidates = discovered
    else:
        candidates = [*discovered, *base]
    candidates.extend(custom_harness_model_options(canon))

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for opt in candidates:
        value = str(opt.get("value") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(
            {
                **opt,
                "value": value,
                "label": str(opt.get("label") or value).strip() or value,
            }
        )
    return out


# ---------------------------------------------------------------------------
#  Harness/model VALIDITY (feature #99) — constrain the model to its harness.
# ---------------------------------------------------------------------------
#
# The CTO saw IMPOSSIBLE stored pairs (e.g. harness claude-code + model
# gemini-3.1-pro-preview — claude-code can't run a Gemini model), and the dogfood
# proved an agent configured with a harness/model its harness can't run fails to
# execute. The SPA dropdown already constrains on EDIT (models_by_harness); these
# two helpers close the gap for STORED/registry pairs that are already invalid (a
# stale override after a harness change, a registry capability with a cross-harness
# model) by coercing at the RESOLUTION layer — so a run never spawns an impossible
# pair AND the UI never displays one.
#
# Dynamic Codex and PI lanes have
# dynamic model lists, so we can't enumerate-validate here; any non-blank model is
# accepted (over-coercing would wipe a valid catalog pick). An UNKNOWN harness (not
# one of the three) likewise has no fixed list / default, so it's permissive (we never
# guess a coercion target for a harness we don't model). A blank model is "nothing to
# coerce" — the routing layer fills the harness default itself.


def harness_default_model(harness: str | None) -> str | None:
    """The DEFAULT model id for a harness — the FIRST entry in its fixed
    `HARNESS_MODELS` list (the coercion target). None for an unknown harness."""
    canon = canonical_harness(harness)
    models = harness_model_options(canon)
    if models:
        if canon == "codex":
            recommended = next((row for row in models if row.get("is_default")), None)
            if recommended:
                return str(recommended["value"])
        return models[0]["value"]
    return None


def valid_model_for_harness(harness: str | None, model: str | None) -> bool:
    """True if `model` is a VALID model for `harness`.

    Rules (harness canonicalised first, so a legacy alias resolves against the right
    list):
      * a blank/None model → True (nothing to validate; the default is filled later);
      * Claude → the model must be in its discovered/built-in/operator list;
      * Codex/PI → True for any non-blank model (dynamic list);
      * an UNKNOWN harness → True (no list to validate against — stay permissive).
    """
    m = (model or "").strip()
    if not m:
        return True
    canon = canonical_harness(harness)
    spec = HARNESSES.get(canon or "")
    if spec is None:
        return True  # unknown harness — nothing to validate against
    if spec.get("model_source") in ("codex-catalog", "pi-catalog"):
        return True  # dynamic lane — the live catalog is the source, not a fixed list
    return any(opt["value"] == m for opt in harness_model_options(canon))


def coerce_model(harness: str | None, model: str | None) -> str | None:
    """Return `model` UNCHANGED when it's valid for `harness`, else the harness's
    DEFAULT model (`harness_default_model`) — so a resolved pair is never impossible.

    A blank model, a catalog-lane model, or an unknown harness passes through
    unchanged (there's nothing valid to coerce TO — see `valid_model_for_harness`). A
    fixed-lane model that isn't in the harness's list is replaced with that harness's
    default; if (defensively) the harness has no default, the original is returned
    rather than blanking the field."""
    if valid_model_for_harness(harness, model):
        return model
    return harness_default_model(harness) or model


# ---------------------------------------------------------------------------
#  Attachment input capabilities (feature #103) — images are per harness/model.
# ---------------------------------------------------------------------------
#
# Text attachments are prompt-inlined for every chat lane. Image attachments are only
# readable when the selected harness/model pair has a vision-capable transport. Today
# the fixed evidence in this module is the pi provider list: gpt-5.3-codex-spark is
# the default text-only model; the remaining verified pi models support image input.

PI_TEXT_ONLY_ATTACHMENT_MODELS = {"gpt-5.3-codex-spark"}
PI_VISION_ATTACHMENT_MODELS = {"gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"}


def supports_vision_attachments(harness: str | None, model: str | None) -> bool:
    """True when image attachments can be surfaced to the selected chat lane.

    The predicate is deliberately narrower than "model has image support": it answers
    whether THIS console chat path can hand the uploaded image to the harness in a
    useful way. Unsupported/unknown pairs keep the existing honest fallback note.
    """
    m = (model or "").strip()
    if not m:
        return False
    canon = canonical_harness(harness)
    if canon != "pi":
        return False
    # PI is now catalog-backed, so valid_model_for_harness is deliberately
    # permissive. Keep image support evidence-based instead of marking every dynamic
    # provider model as readable.
    if "/" in m:
        provider, native = m.split("/", 1)
        if provider != "openai-codex":
            return False
        m = native
    return m in PI_VISION_ATTACHMENT_MODELS and m not in PI_TEXT_ONLY_ATTACHMENT_MODELS


def attachment_capabilities(harness: str | None, model: str | None) -> dict[str, Any]:
    """Small serialisable capability map for attachment-aware callers/tests."""
    image = supports_vision_attachments(harness, model)
    return {
        "text": True,
        "image": image,
        "reason": (
            "vision-capable pi model"
            if image
            else "image attachments are not readable by this harness/model"
        ),
    }


# ---------------------------------------------------------------------------
#  Static JS map — harness → {models, reasoning} for the client-side dropdown
#  re-population when the harness <select> changes (no round-trip for the
#  fixed lanes; PI's SPA catalog is assembled from the host PI catalog.
# ---------------------------------------------------------------------------

def harness_js_map(
    catalog_groups: list[dict[str, Any]],
    pi_catalog_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the JS-consumable map the Configure page uses to re-populate the
    model + reasoning dropdowns when an agent's harness changes, with no server
    round-trip.

    Shape:
        {
          "harnesses": {
             "claude-code": {
                "model_source": "fixed",
                "models": [{value,label}, ...],
                "reasoning": ["low", ...],
             },
             ...,
             "pi": {"model_source": "pi-catalog", "models": [...], "reasoning": [...]},
          },
          "pi_catalog": [ {provider, label, models:[{value,label}]} ],  # for pi lane
        }
    `pi_catalog_groups` is the host PI `pi --list-models` catalog; when absent the
    pi harness falls back to its fixed HARNESS_MODELS list."""
    harnesses: dict[str, Any] = {}
    reasoning_by_model: dict[str, list[str]] = {}
    for key in visible_harness_order():
        spec = HARNESSES[key]
        models = harness_model_options(key)
        # pi-catalog: use live pi groups when available, else the fixed fallback.
        if spec["model_source"] == "pi-catalog" and pi_catalog_groups:
            pi_flat: list[dict[str, Any]] = []
            for g in pi_catalog_groups:
                for row in g.get("rows", []):
                    rid = row.get("id")
                    if not rid or (row.get("type") or "chat") != "chat":
                        continue
                    pi_flat.append({
                        "value": str(rid),
                        "label": row.get("display_name") or rid,
                        "reasoning_levels": list(row.get("reasoning_levels") or []),
                    })
            models = pi_flat if pi_flat else models
        if spec["model_source"] in {
            "claude-catalog", "codex-catalog", "pi-catalog"
        }:
            for option in models:
                value = str(option.get("value") or "")
                if value and "reasoning_levels" in option:
                    levels = list(option.get("reasoning_levels") or [])
                    reasoning_by_model[f"{key}:{value}"] = (
                        ["on"] if levels == ["supported"] else levels
                    )
        harnesses[key] = {
            "model_source": spec["model_source"],
            "models": models,
            "reasoning": HARNESS_REASONING.get(key, []),
        }

    # pi_catalog: the live host PI model groups (provider-grouped, for the SPA).
    pi_catalog: list[dict[str, Any]] = []
    for g in pi_catalog_groups or []:
        options = [
            {
                "value": row["id"],
                "label": row.get("display_name") or row["id"],
                "reasoning_levels": list(row.get("reasoning_levels") or []),
            }
            for row in g.get("rows", [])
            if (row.get("type") or "chat") == "chat"
        ]
        if options:
            pi_catalog.append(
                {
                    "provider": g.get("provider"),
                    "label": g.get("label") or g.get("provider"),
                    "models": options,
                }
            )

    return {
        "harnesses": harnesses,
        "catalog": [],
        "pi_catalog": pi_catalog,
        "reasoning_by_model": reasoning_by_model,
    }


# ---------------------------------------------------------------------------
#  Per-agent Configure view model
# ---------------------------------------------------------------------------

def _flat_model_reasoning_levels(
    model: str | None,
    models: list[dict[str, Any]],
) -> list[str] | None:
    """Per-model effort levels from a flat subscription-harness catalog."""
    if not model:
        return None
    for option in models:
        if option.get("value") != model:
            continue
        if "reasoning_levels" not in option:
            return None
        return list(option.get("reasoning_levels") or [])
    return None


def _registry_config(agent: dict[str, Any]) -> dict[str, str | None]:
    """Pull an agent's REGISTRY harness/model/reasoning from its runtime record.

    Reads capabilities.harness (or .provider as a legacy fallback) → canonical
    harness key; model (top-level) or capabilities.model_preference; and
    capabilities.thinking as the reasoning level. Any absent field is None."""
    caps = agent.get("capabilities") or {}
    harness = canonical_harness(caps.get("harness") or caps.get("provider"))
    model = str(agent.get("model") or caps.get("model_preference") or "").strip()
    reasoning = caps.get("thinking")
    return {
        "harness": harness,
        "model": model or None,
        "reasoning": (str(reasoning) if reasoning else None),
    }


def _model_options_for(
    harness: str | None,
    catalog_groups: list[dict[str, Any]],
    pi_catalog_groups: list[dict[str, Any]] | None = None,
):
    """Return (flat_models, grouped_models) for the model <select> given the
    EFFECTIVE harness. Fixed lanes use their list; PI uses its live catalog with
    a fixed fallback.
    An unknown harness gets an empty fixed list (the current value still shows)."""
    canon = canonical_harness(harness)
    spec = HARNESSES.get(canon or "")
    if spec and spec["model_source"] == "pi-catalog":
        pi_groups = pi_catalog_groups or []
        if pi_groups:
            # Merge live pi catalog groups into flat options, provider-prefixed
            # for non-openai-codex providers (so the runner can route them).
            flat: list[dict[str, Any]] = []
            for g in pi_groups:
                for row in g.get("rows", []):
                    rid = row.get("id")
                    if not rid:
                        continue
                    value = str(rid)
                    flat.append({
                        "value": value,
                        "label": row.get("display_name") or rid,
                        "reasoning_levels": list(row.get("reasoning_levels") or []),
                    })
            return flat, None
        # No live pi catalog — fall back to the fixed list.
        return harness_model_options(canon), None
    return harness_model_options(canon), None


# The designation <select> options for the Configure card (value + label). The
# "" option means "no override — use the registry-derived classification". Three
# tiers, two independent capabilities (chat? model?) — see app.domain.designation:
#   interactive   — a Lead you chat with:        chat ✓  +  LLM/model ✓
#   autonomous    — a non-interactive AI worker:  chat ✗  +  LLM/model ✓
#   deterministic — a pure-code "mini" agent:     chat ✗  +  LLM/model ✗ (not an AI worker)
# (The stored value stays "autonomous" — the label says "Non-interactive" to match
# the product wording without a data migration.)
DESIGNATION_OPTIONS: list[dict[str, str]] = [
    {"value": "", "label": "Registry default"},
    {"value": "interactive", "label": "Interactive · Lead (chat + model)"},
    {"value": "autonomous", "label": "Non-interactive · AI worker (model, no chat)"},
    {"value": "deterministic", "label": "Deterministic · mini agent (no model, no chat)"},
]


def agent_config_view(
    agent: dict[str, Any],
    override: dict[str, str],
    catalog_groups: list[dict[str, Any]],
    registry_designation: str = "",
    pi_catalog_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Shape ONE agent into the Configure-row view model the template renders.

    Layers the console-local `override` over the registry config: the EFFECTIVE
    value (what the dropdown selects + the summary line shows) is the override
    when present, else the registry value. Also flags whether each field is
    overridden (so the UI can mark a console-local change vs the registry).

    The Configure card now also edits the agent PROFILE: a `designation`
    (Interactive Lead / Autonomous / registry-default) that drives the col-2
    grouping override-first, and a free-text `role` override. `registry_designation`
    is the heuristic-derived classification ("interactive"/"autonomous"), supplied
    by the caller (main._registry_interactive) for the "registry: …" hint and as
    the effective value when no designation override is set.

    Emits the model dropdown options for the EFFECTIVE harness (fixed list or
    grouped catalog) plus the reasoning levels for that harness, so the row
    renders correct options on first paint; the client-side harness-change
    handler swaps them afterwards from harness_js_map()."""
    name = agent.get("name") or ""
    caps = agent.get("capabilities") or {}
    display = caps.get("display_name") or name
    initials = (display[:2] or name[:2] or "··").upper()

    reg = _registry_config(agent)

    # Effective = override wins; else registry.
    eff_harness = override.get("harness") or reg["harness"]
    eff_model = override.get("model") or reg["model"]
    eff_reasoning = override.get("reasoning") or reg["reasoning"]

    canon_harness = canonical_harness(eff_harness)

    # VALIDITY (feature #99): the EFFECTIVE model must be runnable on the EFFECTIVE
    # harness. A stored/registry pair can already be impossible (a stale override
    # after a harness change, or a registry capability carrying a cross-harness
    # model). Coerce it to the harness default so the card's controls never SELECT an
    # impossible pair (the runtime path coerces identically), and surface a subtle
    # hint (model_coerced + the original) so the operator sees why the shown model
    # differs from what was stored.
    model_coerced = False
    model_invalid_original: str | None = None
    if eff_model and not valid_model_for_harness(canon_harness, eff_model):
        model_invalid_original = eff_model
        eff_model = coerce_model(canon_harness, eff_model)
        model_coerced = eff_model != model_invalid_original

    flat_models, grouped_models = _model_options_for(
        canon_harness, catalog_groups, pi_catalog_groups
    )
    # Dynamic CLI catalogs may expose model-specific reasoning levels.
    spec_for_reasoning = HARNESSES.get(canon_harness or "")
    model_source = spec_for_reasoning.get("model_source") if spec_for_reasoning else ""
    if model_source in {"claude-catalog", "codex-catalog", "pi-catalog"}:
        per_model = _flat_model_reasoning_levels(eff_model, flat_models)
        if per_model is not None:
            # ["supported"] (toggle-only) → a single "on" option for the UI.
            reasoning_levels = ["on"] if per_model == ["supported"] else per_model
        else:
            reasoning_levels = HARNESS_REASONING.get(canon_harness or "", [])
    else:
        reasoning_levels = HARNESS_REASONING.get(canon_harness or "", [])

    # profile: designation + role overrides. The role override wins for display;
    # the designation override wins for classification (else the registry value).
    ov_designation = (override.get("designation") or "").strip().lower()
    reg_role = agent.get("role") or "—"
    eff_role = (override.get("role") or "").strip() or reg_role
    eff_designation = ov_designation or (registry_designation or "")
    ov_auto_dispatch = (override.get("auto_dispatch") or "").strip().lower()
    cap_auto_dispatch = (caps.get("auto_dispatch") or "")
    eff_auto_dispatch = (
        ov_auto_dispatch
        if ov_auto_dispatch in {"true", "false"}
        else str(cap_auto_dispatch).strip().lower()
    )
    if eff_auto_dispatch not in {"true", "false"}:
        eff_auto_dispatch = "false"

    return {
        "name": name,
        "display_name": display,
        "initials": initials,
        # effective role (override wins) — shown in the card header
        "role": eff_role,
        # registry (source-of-truth-for-now) values, for the "registry: …" hint
        "reg_harness": reg["harness"],
        "reg_harness_label": harness_label(reg["harness"]),
        "reg_model": reg["model"],
        "reg_reasoning": reg["reasoning"],
        "reg_role": reg_role,
        "reg_designation": registry_designation or "",
        # effective (override-overlaid) values — what the controls select
        "harness": canon_harness,
        "harness_label": harness_label(canon_harness),
        "model": eff_model,
        "reasoning": eff_reasoning,
        "designation": eff_designation,
        "auto_dispatch": eff_auto_dispatch == "true",
        # VALIDITY (feature #99): True when the stored model was invalid for the
        # effective harness and we coerced it to the harness default; the original
        # (impossible) stored value rides along for the UI hint copy.
        "model_coerced": model_coerced,
        "model_invalid_original": model_invalid_original,
        # which fields are console-overridden (≠ registry)
        "ov_harness": bool(override.get("harness")),
        "ov_model": bool(override.get("model")),
        "ov_reasoning": bool(override.get("reasoning")),
        "ov_designation": bool(ov_designation),
        "ov_role": bool((override.get("role") or "").strip()),
        "ov_auto_dispatch": bool(ov_auto_dispatch),
        "has_override": bool(override),
        # dropdown option sets for the EFFECTIVE harness (first paint)
        "model_is_catalog": grouped_models is not None,
        "model_options": flat_models,
        "model_groups": grouped_models or [],
        "reasoning_levels": reasoning_levels,
        "designation_options": DESIGNATION_OPTIONS,
    }


def visible_harness_order() -> list[str]:
    """Return every external harness shipped by the community edition."""
    return list(HARNESS_ORDER)


def harness_options() -> list[dict[str, str]]:
    """The harness <select> options (value + label), in product order."""
    return [{"value": k, "label": HARNESSES[k]["label"]} for k in visible_harness_order()]
