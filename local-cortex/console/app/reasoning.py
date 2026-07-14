"""Per-provider reasoning/thinking — the connector registry + the standard core.

THE ARCHITECTURE (CTO principle, doc 15): a per-provider **connector registry**
carries every API-guide nuance — the native-request-param *pattern* and the
per-model reasoning *levels*. That registry is the ONLY place provider-specific
knowledge ("hardcoding") lives. A **standard core** then applies whatever level
the operator chose against whatever the connectors discovered, with one set of
safety rules. No bespoke provider logic lives outside the registry data.

Three things the registry encodes, per provider (doc 15 §3 matrix):
  1. ``pattern``        — which of the 6 native-param shapes the provider uses
                          (§4). This is how a level turns into a request field.
  2. ``levels``         — the curated, ordered effort ladder the provider/model
                          supports (the discovery default when no live API tells
                          us; live levels override per-(provider,model)).
  3. per-model overrides — clamps/skips for the models whose ladder differs from
                          their provider default (grok-3-mini → low/high only,
                          grok-4 → no param at all, base kimi-k2 → not a reasoner).

THE SAFETY CONTRACT (this is the live call path for every kaidera agent):
  ``apply_reasoning`` emits a reasoning param ONLY when the resolved
  ``(provider, model)`` actually supports reasoning AND the requested level is
  valid for THAT model. Anything else → emit NOTHING (a correct, thinking-off
  call). We NEVER send a param a model rejects — grok-4 rejects
  ``reasoning_effort`` (400), base kimi-k2 isn't a reasoning model, ollama
  "minimal" 400s. When unsure, send nothing. A regression here 400s real calls,
  so every branch is conservative.

Provider facts verified against official docs, 2026-06-21 (see doc 15 §3/§7).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
#  Level vocabulary + normalization
# ---------------------------------------------------------------------------
#
# The console/registry stores a free-ish thinking string ("low"/"medium"/"high",
# sometimes "med"/"on"/"true"/"off"). Normalize to canonical effort tokens before
# matching against a model's ladder so a stored "med" or "on" is never silently
# dropped (the §5 bug class). Tokens that mean "no thinking" normalize to "off".

# canonical effort ladder, low→high (the union across providers).
_EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
_EFFORT_RANK = {lvl: i for i, lvl in enumerate(_EFFORT_ORDER)}

# values that mean "turn thinking OFF" (an explicit disable, not a level).
_OFF_TOKENS = frozenset({"", "off", "none", "no", "false", "disabled", "disable"})

# stored-string → canonical-token aliases (everything is lower-cased first).
_LEVEL_ALIASES: dict[str, str] = {
    "med": "medium",
    "mid": "medium",
    "moderate": "medium",
    "min": "minimal",
    "xhi": "xhigh",
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "maximum": "max",
    # a bare "on"/"true"/"enabled"/"yes" means "thinking on, default level" — the
    # core resolves it to the model's mid/default tier (see resolve_level).
    "on": "_on_",
    "true": "_on_",
    "enabled": "_on_",
    "enable": "_on_",
    "yes": "_on_",
}


def normalize_level(level: str | None) -> str:
    """Lower-case + alias a stored thinking string to a canonical token.

    Returns "" for an OFF/empty value, "_on_" for a bare enable, else a canonical
    effort token ("low"/"medium"/"high"/"xhigh"/"max"/"ultra"/"minimal") or the raw
    lower-cased string when it's something we don't recognize (the caller then
    matches it against the model's ladder and drops it if absent)."""
    s = (level or "").strip().lower()
    if s in _OFF_TOKENS:
        return ""
    return _LEVEL_ALIASES.get(s, s)


def is_off(level: str | None) -> bool:
    """True when the stored value explicitly means 'no thinking'."""
    return normalize_level(level) == ""


# ---------------------------------------------------------------------------
#  The connector registry (per-provider, doc 15 §3) — DATA ONLY.
# ---------------------------------------------------------------------------
#
# Each provider entry:
#   pattern        — the native-param key (see PATTERNS below) the apply core uses.
#   default_levels — the curated effort ladder for models on this provider that
#                    don't carry a richer live ladder (the discovery fallback).
#   model_levels   — per-model overrides: an exact ladder for a specific model id
#                    (matched on a normalized substring, so "grok-3-mini" matches
#                    "grok-3-mini-2026..."). [] means "model reasons but with NO
#                    selectable level" (emit the on/off toggle only) — distinct
#                    from a model ABSENT here (uses default_levels) and from a
#                    model in `no_reasoning` (emit NOTHING).
#   no_reasoning   — substrings of model ids that are NOT reasoning models on this
#                    provider (base kimi-k2, grok-4 always-on, etc). Highest
#                    precedence: matches here → never emit a reasoning param.
#   family_levels  — optional (provider, model-substring) → ladder, for providers
#                    whose param/ladder branches by model FAMILY (Groq, Together).
#
# Levels are sourced from doc 15 §3; refreshed with the catalog cron exactly like
# the OpenRouter price xref. Adding a provider = add one entry here (+ wire its
# pattern if new), never bespoke apply code.

# the six native-param patterns (doc 15 §4). The apply core dispatches on these.
PATTERN_EFFORT = "reasoning_effort"            # top-level reasoning_effort=<level>
PATTERN_OR_REASONING = "or_reasoning"          # reasoning={"effort": <level>}  (OpenRouter)
PATTERN_THINKING_TOGGLE = "thinking_toggle"    # thinking={"type": enabled|disabled}
PATTERN_ENABLE_THINKING = "enable_thinking"    # enable_thinking=<bool> (+thinking_budget)
PATTERN_TOGETHER = "together"                  # reasoning={"enabled": bool} / chat_template_kwargs
PATTERN_GROQ = "groq"                          # gpt-oss reasoning_effort / qwen3 reasoning_format
PATTERN_ANTHROPIC = "anthropic"               # thinking={"type":"adaptive"} + reasoning_effort

# providers that branch param + ladder by model family (handled inside the core).
_GROQ_GPT_OSS = "openai/gpt-oss"


CONNECTORS: dict[str, dict[str, Any]] = {
    # 1) reasoning_effort top-level + per-model clamp -----------------------
    "kaidera-manifold": {
        # Manifold exposes the OpenAI-compatible reasoning field. Its live
        # /models rows are authoritative; the empty fallback avoids inventing a
        # ladder when the platform catalog is unavailable.
        "pattern": PATTERN_EFFORT,
        "default_levels": [],
        "model_levels": {},
        "no_reasoning": [],
    },
    "openai": {
        "pattern": PATTERN_EFFORT,
        # list API is silent on effort → curated. (Chat-Completions effort set.)
        "default_levels": ["minimal", "low", "medium", "high", "xhigh"],
        "model_levels": {},
        "no_reasoning": [],
    },
    "xai": {
        "pattern": PATTERN_EFFORT,
        "default_levels": ["low", "high"],  # conservative xAI default
        "model_levels": {
            # grok-3-mini accepts ONLY low/high (medium/minimal 400).
            "grok-3-mini": ["low", "high"],
            # grok-4-fast / 4.3 take the full ladder.
            "grok-4-fast": ["low", "medium", "high"],
            "grok-4.3": ["low", "medium", "high"],
        },
        # grok-4 (non-fast) is ALWAYS-ON and REJECTS reasoning_effort → send nothing.
        "no_reasoning": ["grok-4-0", "grok-4-1", "grok-4-2"],
    },
    "perplexity": {
        "pattern": PATTERN_EFFORT,
        # depth-only, can't disable; fixed enum (no /models endpoint).
        "default_levels": ["minimal", "low", "medium", "high"],
        "model_levels": {},
        "no_reasoning": [],
    },
    "fireworks": {
        "pattern": PATTERN_EFFORT,
        "default_levels": ["low", "medium", "high"],
        "model_levels": {},
        # base kimi-k2 is NOT a reasoning model — only kimi-k2-thinking reasons.
        # Matching is "kimi-k2" but NOT when "-thinking" is present (handled in core).
        "no_reasoning": ["kimi-k2"],
        "no_reasoning_unless": ["kimi-k2-thinking"],
    },
    "inception": {
        "pattern": PATTERN_EFFORT,
        "default_levels": [],  # diffusion models — no curated reasoning ladder known
        "model_levels": {},
        "no_reasoning": [],
    },
    # ollama-cloud: the /v1 compat path ACCEPTS reasoning_effort (doc 15 §5) for
    # any model — it just no-ops for non-gpt-oss families (qwen3/deepseek use the
    # native /api/chat `think` boolean, the deferred "full fix"). Sending it is
    # SAFE (no 400) so we emit it for the whole lane; the ONLY 400 guard is the
    # core's "minimal"→"low" clamp (ollama 400s "minimal"). This is the §5 bug
    # fix: a selected level is never silently dropped for ollama-cloud.
    "ollama-cloud": {
        "pattern": PATTERN_EFFORT,
        "default_levels": ["low", "medium", "high"],
        "model_levels": {},
        "no_reasoning": [],
    },
    # 2) enable_thinking + thinking_budget top-level -----------------------
    "siliconflow": {
        "pattern": PATTERN_ENABLE_THINKING,
        "default_levels": [],  # boolean toggle (+ budget) — no effort ladder
        "model_levels": {},
        "no_reasoning": [],
    },
    "dashscope": {
        "pattern": PATTERN_ENABLE_THINKING,
        "default_levels": [],
        "model_levels": {},
        "no_reasoning": [],
    },
    "alibaba-cloud": {  # DashScope-intl — same shape
        "pattern": PATTERN_ENABLE_THINKING,
        "default_levels": [],
        "model_levels": {},
        "no_reasoning": [],
    },
    # 3) thinking:{type: enabled|disabled} — DeepSeek + Moonshot (shared) ----
    "deepseek": {
        "pattern": PATTERN_THINKING_TOGGLE,
        "default_levels": [],  # binary
        "model_levels": {},
        "no_reasoning": [],
    },
    "moonshot": {
        "pattern": PATTERN_THINKING_TOGGLE,
        "default_levels": [],
        "model_levels": {},
        # kimi-k2.7-code is always-on (no toggle needed) — emit nothing.
        "no_reasoning": ["kimi-k2.7-code", "kimi-k2-7-code"],
    },
    # 4) Together — 3 families (bool / gpt-oss effort / qwen3 template kwargs) -
    "together": {
        "pattern": PATTERN_TOGETHER,
        "default_levels": [],  # hybrid bool default
        "model_levels": {},
        "no_reasoning": [],
        "family_levels": {
            "gpt-oss": ["low", "medium", "high"],
        },
    },
    # 5) Groq — gpt-oss effort+include_reasoning; qwen3 reasoning_format -----
    "groq": {
        "pattern": PATTERN_GROQ,
        "default_levels": [],
        "model_levels": {
            "gpt-oss": ["low", "medium", "high"],
        },
        "no_reasoning": [],
    },
    # 6) OpenRouter (unified reasoning={effort}) + Anthropic-direct ----------
    "openrouter": {
        "pattern": PATTERN_OR_REASONING,
        # the cross-provider lever; real per-model levels come from the live
        # supported_efforts[] (discovery). Curated fallback = the common ladder.
        "default_levels": ["low", "medium", "high"],
        "model_levels": {},
        "no_reasoning": [],
    },
    "anthropic": {
        "pattern": PATTERN_ANTHROPIC,
        # live API already gives the per-model effort tree
        # (capabilities.effort.{low,medium,high,max,xhigh}); this is the fallback.
        "default_levels": ["low", "medium", "high", "max", "xhigh"],
        "model_levels": {},
        "no_reasoning": [],
    },
}


def _norm_id(model: str | None) -> str:
    """Lower-case a model id and collapse '.'/':' to '-' for substring matching
    (so "grok-3-mini" matches "grok-3-mini-2026...", and "kimi-k2.7" ~ "kimi-k2-7")."""
    return (model or "").strip().lower().replace(".", "-").replace(":", "-")


def _connector(provider: str) -> dict[str, Any] | None:
    """The connector entry for a provider. ``custom:*`` providers have no known
    nuance → None (the core then emits nothing, the safe default)."""
    return CONNECTORS.get((provider or "").strip().lower())


def connector_known(provider: str) -> bool:
    """True when the registry has authoritative knowledge of this provider. The
    discovery layer uses this to NEVER clear a row's reasoning data for a provider
    we don't model (we only clear a stale placeholder when we KNOW the model is a
    non-reasoner)."""
    return _connector(provider) is not None


# ---------------------------------------------------------------------------
#  DISCOVERY — the curated levels for a (provider, model)
# ---------------------------------------------------------------------------

def curated_levels(provider: str, model: str | None) -> list[str]:
    """The curated reasoning ladder for a (provider, model) from the connector
    registry, used by B2 discovery to seed the catalog when no live API ladder
    exists. Returns:
      * []  when the model is a known NON-reasoner on this provider (so the
            catalog shows 'no reasoning'), OR the provider is unknown.
      * the model's specific ladder (model_levels / family_levels) when present.
      * the provider default ladder otherwise.
    NOTE: an empty list here is ambiguous between 'binary toggle' and 'non-
    reasoner'; discovery layers a separate ``reasons`` flag (see reasons())."""
    conn = _connector(provider)
    if not conn:
        return []
    nid = _norm_id(model)
    if _is_non_reasoner(conn, nid):
        return []
    # exact/substring model override
    for key, levels in (conn.get("model_levels") or {}).items():
        if _norm_id(key) in nid:
            return list(levels)
    for key, levels in (conn.get("family_levels") or {}).items():
        if _norm_id(key) in nid:
            return list(levels)
    return list(conn.get("default_levels") or [])


def reasons(provider: str, model: str | None) -> bool:
    """Does this (provider, model) support reasoning AT ALL (a ladder OR a binary
    toggle)? True unless the provider is unknown or the model is a known non-
    reasoner. Used so the catalog can show a toggle-only model as reasoning-capable
    even though its curated ladder is empty."""
    conn = _connector(provider)
    if not conn:
        return False
    nid = _norm_id(model)
    if _is_non_reasoner(conn, nid):
        return False
    # a toggle provider (empty default ladder, no per-model ladder) still reasons.
    return True


def _is_non_reasoner(conn: dict[str, Any], nid: str) -> bool:
    """True when this normalized model id is a known NON-reasoner on its provider.

    Honors ``no_reasoning`` (substrings that don't reason) and the
    ``no_reasoning_unless`` carve-out (e.g. fireworks 'kimi-k2' doesn't reason
    EXCEPT 'kimi-k2-thinking')."""
    unless = [_norm_id(u) for u in (conn.get("no_reasoning_unless") or [])]
    if any(u in nid for u in unless):
        return False
    return any(_norm_id(s) in nid for s in (conn.get("no_reasoning") or []))


# ---------------------------------------------------------------------------
#  RESOLUTION — pick the level to actually send for a (provider, model)
# ---------------------------------------------------------------------------

def resolve_level(
    provider: str,
    model: str | None,
    level: str | None,
    *,
    available_levels: list[str] | None = None,
) -> str | None:
    """Resolve the stored thinking string to the effort token to SEND for this
    (provider, model), or None to send nothing.

    Rules (conservative):
      * provider unknown / model is a known non-reasoner  → None.
      * stored value means OFF                            → None for ladder
        providers (no field), or handled as an explicit disable by toggle
        patterns (see apply_reasoning) — resolve_level itself returns None.
      * bare "on" / "true"                                → the model's DEFAULT
        tier (its highest-but-one, or the single available level).
      * a canonical level present in the model's ladder   → that level.
      * a canonical level ABOVE the model's max           → CLAMP down to the max.
      * a canonical level BELOW/between, not present       → nearest supported at
        or below it (never up past the requested intent's floor); if none at or
        below, the model's lowest level.
      * an unrecognized token                              → None (don't guess).

    For TOGGLE providers (empty ladder but the model reasons), returns the
    sentinel "_toggle_" so apply_reasoning knows to emit the enable form."""
    conn = _connector(provider)
    if not conn:
        return None
    nid = _norm_id(model)
    if available_levels is None and _is_non_reasoner(conn, nid):
        return None

    norm = normalize_level(level)
    if norm == "":
        return None  # OFF → nothing (toggle disable handled by apply_reasoning)

    if available_levels is None:
        ladder = curated_levels(provider, model)
        toggle_only = not ladder and reasons(provider, model)
    else:
        advertised = [str(item).strip().lower() for item in available_levels if str(item).strip()]
        if not advertised:
            return None
        toggle_only = advertised == ["supported"]
        ladder = [normalize_level(item) for item in advertised if item != "supported"]
        ladder = [item for item in ladder if item not in {"", "_on_"}]
    if not ladder:
        # The provider says this model reasons but exposes no selectable ladder.
        return "_toggle_" if toggle_only else None

    if norm == "_on_":
        # bare enable → the model's "default" tier: medium if present, else the
        # highest level at or below medium, else the top of the ladder.
        if "medium" in ladder:
            return "medium"
        below = [lv for lv in ladder if _EFFORT_RANK.get(lv, 0) <= _EFFORT_RANK["medium"]]
        return (below or ladder)[-1] if (below or ladder) else None

    if norm in ladder:
        return norm

    rank = _EFFORT_RANK.get(norm)
    if rank is None:
        return None  # unknown token, not in ladder → don't guess, send nothing.

    # CLAMP: requested rank above the model's max → the model's max.
    ranks = [(_EFFORT_RANK[lv], lv) for lv in ladder if lv in _EFFORT_RANK]
    if not ranks:
        return None
    ranks.sort()
    max_rank, max_lv = ranks[-1]
    if rank > max_rank:
        return max_lv
    # otherwise pick the highest supported level at or below the request; if the
    # request is below the model's minimum, use the minimum.
    at_or_below = [lv for r, lv in ranks if r <= rank]
    if at_or_below:
        return at_or_below[-1]
    return ranks[0][1]  # request below the floor → the lowest supported level


# ---------------------------------------------------------------------------
#  APPLY — the standard core: write the native param onto an OpenAI-compat body
# ---------------------------------------------------------------------------

def apply_reasoning(
    provider: str,
    model: str | None,
    level: str | None,
    payload: dict[str, Any],
    *,
    available_levels: list[str] | None = None,
) -> dict[str, Any]:
    """Mutate ``payload`` (an OpenAI-compatible chat body) with the provider's
    native reasoning param for the chosen ``level`` — or leave it UNTOUCHED when
    nothing should be sent. Returns the same payload for convenience.

    This is the standard core: it dispatches purely on the connector's ``pattern``
    and the resolved level. The ONLY provider-specific knowledge it consults is
    the registry. Safety: it emits a param ONLY for a (provider, model) the
    registry says reasons, at a level valid for that model. Anything uncertain →
    no param (a correct thinking-off call). Anthropic-direct is handled by
    ``anthropic_thinking_fields`` (different body shape), not here."""
    conn = _connector(provider)
    if not conn:
        return payload  # unknown provider (incl. custom:*) → never send a param.

    pattern = conn.get("pattern")
    explicit_off = is_off(level)
    resolved = resolve_level(
        provider,
        model,
        level,
        available_levels=available_levels,
    )
    known_non_reasoner = (
        available_levels == []
        or (available_levels is None and _is_non_reasoner(conn, _norm_id(model)))
    )

    # Anthropic uses a different request body (messages API) — not applied here.
    if pattern == PATTERN_ANTHROPIC:
        return payload

    # ---- toggle patterns: respect an explicit OFF as a disable -------------
    if pattern == PATTERN_THINKING_TOGGLE:
        # DeepSeek / Moonshot: thinking={"type": enabled|disabled}.
        if known_non_reasoner:
            return payload
        if explicit_off:
            payload["thinking"] = {"type": "disabled"}
        elif resolved is not None:
            payload["thinking"] = {"type": "enabled"}
        return payload

    if pattern == PATTERN_ENABLE_THINKING:
        # SiliconFlow / DashScope: top-level enable_thinking bool.
        if known_non_reasoner:
            return payload
        if explicit_off:
            payload["enable_thinking"] = False
        elif resolved is not None:
            payload["enable_thinking"] = True
        return payload

    # From here, a None resolution means "send nothing".
    if resolved is None:
        # one exception: Together/Groq toggle families may still want an explicit
        # disable; but to stay maximally safe we only DISABLE for the dedicated
        # toggle patterns above. Ladder/effort patterns simply omit on OFF.
        return payload

    if pattern == PATTERN_EFFORT:
        # OpenAI / xAI / Perplexity / Fireworks / ollama-cloud(gpt-oss).
        eff = resolved if resolved != "_toggle_" else None
        if eff is None:
            return payload
        # ollama-cloud: gate to gpt-oss on the /v1 path + never send "minimal".
        gate = conn.get("effort_only_if")
        if gate and not any(_norm_id(g) in _norm_id(model) for g in gate):
            return payload  # non-gpt-oss ollama on /v1 → no effort param (safe).
        if provider == "ollama-cloud" and eff == "minimal":
            eff = "low"  # ollama 400s "minimal".
        payload["reasoning_effort"] = eff
        return payload

    if pattern == PATTERN_OR_REASONING:
        # OpenRouter unified reasoning={"effort": ...}.
        eff = resolved if resolved != "_toggle_" else None
        if eff is None:
            return payload
        payload["reasoning"] = {"effort": eff}
        return payload

    if pattern == PATTERN_GROQ:
        # Groq: gpt-oss → reasoning_effort + include_reasoning; qwen3 → reasoning_format.
        nid = _norm_id(model)
        if _norm_id(_GROQ_GPT_OSS) in nid:
            eff = resolved if resolved != "_toggle_" else None
            if eff is not None:
                payload["reasoning_effort"] = eff
                payload["include_reasoning"] = True
        else:
            # qwen3 / others on Groq: surface reasoning content (no effort ladder).
            payload["reasoning_format"] = "parsed"
        return payload

    if pattern == PATTERN_TOGETHER:
        # Together: gpt-oss → reasoning_effort; qwen3 → chat_template_kwargs;
        # hybrid default → reasoning={"enabled": True}.
        nid = _norm_id(model)
        if "gpt-oss" in nid:
            eff = resolved if resolved != "_toggle_" else None
            if eff is not None:
                payload["reasoning_effort"] = eff
        elif "qwen3" in nid or "qwen-3" in nid:
            payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = True
        else:
            payload["reasoning"] = {"enabled": True}
        return payload

    return payload


# ---------------------------------------------------------------------------
#  Anthropic-direct (messages API body) — its own shape, not OpenAI-compat
# ---------------------------------------------------------------------------

def anthropic_thinking_fields(
    model: str | None,
    level: str | None,
    *,
    available_levels: list[str] | None = None,
) -> dict[str, Any]:
    """The extra top-level fields to MERGE into an Anthropic /v1/messages body to
    turn on extended thinking at ``level``, or {} to send nothing.

    Opus 4.7+ uses ``thinking={"type":"adaptive"}`` + a top-level
    ``reasoning_effort`` (the legacy ``budget_tokens`` 400s on 4.7+). We emit the
    adaptive block + effort ONLY when a real level resolves for the model; an OFF/
    empty/unknown value → {} (no thinking, the correct quiet default)."""
    eff = resolve_level(
        "anthropic",
        model,
        level,
        available_levels=available_levels,
    )
    if eff is None or eff == "_toggle_":
        # _toggle_ shouldn't happen (anthropic has a ladder), but be safe.
        return {}
    return {"thinking": {"type": "adaptive"}, "reasoning_effort": eff}


# ---------------------------------------------------------------------------
#  OUTPUT (B4) — pull the reasoning/thinking text out of a provider response
# ---------------------------------------------------------------------------

def extract_reasoning_text(provider: str, data: dict[str, Any]) -> str:
    """Best-effort: the model's reasoning/thinking text from an OpenAI-compat
    chat response, read from the field THIS provider uses (doc 15 §3):
      * message.reasoning_content — DeepSeek/Moonshot/SiliconFlow/DashScope/xAI/Fireworks
      * message.reasoning         — Together/Groq
    Returns "" when absent. Inline ``<think>`` (Perplexity/Together-R1) is left in
    the main content for the caller to strip if desired. Never raises."""
    try:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            return ""
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(msg, dict):
            return ""
        for field in ("reasoning_content", "reasoning"):
            val = msg.get(field)
            if isinstance(val, str) and val.strip():
                return val
        return ""
    except Exception:  # noqa: BLE001 — output parsing must never break the call.
        return ""
