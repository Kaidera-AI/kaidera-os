"""The per-provider reasoning connector registry + standard core (app.reasoning).

Covers the kaidera per-model reasoning feature (doc 15):
  * the APPLY dispatcher — each of the 6 native-param patterns (§4) writes the
    right field for the right (provider, model);
  * the SAFETY contract — a param is emitted ONLY when the (provider, model)
    actually reasons AND the level is valid for THAT model; everything uncertain
    (grok-4 reject, base kimi-k2 non-reasoner, ollama "minimal", unknown/custom
    provider, off/empty) emits NOTHING. A regression here 400s the LIVE kaidera
    call path, so the skip/clamp cases are asserted explicitly;
  * the DISCOVERY map — curated_levels / reasons / the per-model overrides used to
    seed the catalog when no live API ladder exists.

These are pure-data assertions (no network, no app boot)."""

from __future__ import annotations

import pytest

from app import reasoning as R


def _apply(provider: str, model: str, level: str | None) -> dict:
    """Run the core against an empty OpenAI-compat body and return ONLY the
    reasoning fields it added (so {} == 'sent nothing')."""
    payload: dict = {"model": model, "messages": []}
    R.apply_reasoning(provider, model, level, payload)
    return {k: v for k, v in payload.items() if k not in ("model", "messages")}


# ---------------------------------------------------------------------------
#  Normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("medium", "medium"),
        ("med", "medium"),       # alias
        ("MED", "medium"),       # case
        ("xhi", "xhigh"),
        ("maximum", "max"),
        ("on", "_on_"),
        ("true", "_on_"),
        ("", ""),
        ("off", ""),
        ("none", ""),
        ("disabled", ""),
        ("false", ""),
    ],
)
def test_normalize_level(raw, expected):
    assert R.normalize_level(raw) == expected


def test_is_off():
    assert R.is_off("off") and R.is_off("") and R.is_off("none")
    assert not R.is_off("low")


# ---------------------------------------------------------------------------
#  APPLY — pattern 1: reasoning_effort top-level (OpenAI / xAI / Perplexity / …)
# ---------------------------------------------------------------------------

def test_openai_effort_passthrough_and_alias():
    assert _apply("openai", "gpt-5.5", "xhigh") == {"reasoning_effort": "xhigh"}
    assert _apply("openai", "gpt-5.5", "med") == {"reasoning_effort": "medium"}


def test_openai_bare_on_resolves_to_default_tier():
    # a bare "on" → the model's default (medium when present).
    assert _apply("openai", "gpt-5.5", "on") == {"reasoning_effort": "medium"}


def test_openai_off_sends_nothing():
    assert _apply("openai", "gpt-5.5", "off") == {}
    assert _apply("openai", "gpt-5.5", "") == {}


# ---- SAFETY: per-model clamp + skip (the 400 guards) ----------------------

def test_grok4_rejects_effort_sends_nothing():
    # grok-4 (non-fast) is always-on and REJECTS reasoning_effort (400).
    assert _apply("xai", "grok-4-0709", "high") == {}
    assert _apply("xai", "grok-4-1", "low") == {}


def test_grok3_mini_clamps_to_low_high_only():
    # grok-3-mini accepts ONLY low/high; xhigh clamps down to high, medium (not in
    # the ladder) → the nearest supported at or below → low. Never an invalid level.
    assert _apply("xai", "grok-3-mini", "low") == {"reasoning_effort": "low"}
    assert _apply("xai", "grok-3-mini", "high") == {"reasoning_effort": "high"}
    assert _apply("xai", "grok-3-mini-2026", "xhigh") == {"reasoning_effort": "high"}
    assert _apply("xai", "grok-3-mini", "medium") == {"reasoning_effort": "low"}


def test_grok4_fast_takes_full_ladder():
    assert _apply("xai", "grok-4-fast", "high") == {"reasoning_effort": "high"}


def test_fireworks_base_kimi_is_not_a_reasoner():
    # base kimi-k2 does NOT reason; only kimi-k2-thinking does.
    assert _apply("fireworks", "accounts/fireworks/models/kimi-k2", "high") == {}
    assert _apply(
        "fireworks", "accounts/fireworks/models/kimi-k2-thinking", "high"
    ) == {"reasoning_effort": "high"}


def test_ollama_never_sends_minimal_400_guard():
    # ollama 400s "minimal" → the core clamps it to "low" (never silently dropped).
    assert _apply("ollama-cloud", "gpt-oss-120b", "minimal") == {"reasoning_effort": "low"}
    # and a normal level always lands (the §5 silent-drop bug fix).
    assert _apply("ollama-cloud", "qwen3-coder:480b", "high") == {"reasoning_effort": "high"}


def test_ollama_off_sends_nothing():
    assert _apply("ollama-cloud", "gpt-oss-120b", "off") == {}


def test_unknown_and_custom_provider_send_nothing():
    # no registry entry → we have no nuance → never emit a param (the safe default).
    assert _apply("custom:my-llm", "some-model", "high") == {}
    assert _apply("totally-unknown", "x", "high") == {}


# ---------------------------------------------------------------------------
#  APPLY — pattern 6 cont.: OpenRouter unified reasoning={effort}
# ---------------------------------------------------------------------------

def test_openrouter_unified_reasoning_block():
    assert _apply("openrouter", "anthropic/claude-opus", "high") == {
        "reasoning": {"effort": "high"}
    }
    assert _apply("openrouter", "anthropic/claude-opus", "off") == {}


# ---------------------------------------------------------------------------
#  APPLY — pattern 3: thinking:{type: enabled|disabled} (DeepSeek + Moonshot)
# ---------------------------------------------------------------------------

def test_deepseek_thinking_toggle_enable_disable():
    assert _apply("deepseek", "deepseek-v4", "on") == {"thinking": {"type": "enabled"}}
    assert _apply("deepseek", "deepseek-v4", "high") == {"thinking": {"type": "enabled"}}
    # an explicit OFF is a real disable for a toggle provider.
    assert _apply("deepseek", "deepseek-v4", "off") == {"thinking": {"type": "disabled"}}


def test_moonshot_shares_deepseek_path_and_always_on_model_skips():
    assert _apply("moonshot", "kimi-k2.7-pro", "on") == {"thinking": {"type": "enabled"}}
    # kimi-k2.7-code is always-on → no toggle (emit nothing).
    assert _apply("moonshot", "kimi-k2.7-code", "on") == {}


# ---------------------------------------------------------------------------
#  APPLY — pattern 2: enable_thinking top-level (SiliconFlow / DashScope)
# ---------------------------------------------------------------------------

def test_enable_thinking_bool():
    assert _apply("siliconflow", "deepseek-v3.1", "high") == {"enable_thinking": True}
    assert _apply("siliconflow", "deepseek-v3.1", "off") == {"enable_thinking": False}
    assert _apply("dashscope", "qwen3-235b", "on") == {"enable_thinking": True}


# ---------------------------------------------------------------------------
#  APPLY — pattern 5: Groq (gpt-oss effort+include_reasoning / qwen3 format)
# ---------------------------------------------------------------------------

def test_groq_gpt_oss_vs_qwen3_branch():
    assert _apply("groq", "openai/gpt-oss-120b", "high") == {
        "reasoning_effort": "high",
        "include_reasoning": True,
    }
    # qwen3 / others on Groq → surface reasoning (no effort ladder).
    assert _apply("groq", "qwen3-32b", "high") == {"reasoning_format": "parsed"}


# ---------------------------------------------------------------------------
#  APPLY — pattern 4: Together (3 families)
# ---------------------------------------------------------------------------

def test_together_three_families():
    assert _apply("together", "openai/gpt-oss-120b", "high") == {"reasoning_effort": "high"}
    assert _apply("together", "qwen3-235b", "on") == {
        "chat_template_kwargs": {"enable_thinking": True}
    }
    # hybrid default → reasoning={"enabled": True}.
    assert _apply("together", "deepseek-r1", "on") == {"reasoning": {"enabled": True}}


# ---------------------------------------------------------------------------
#  Anthropic-direct (its own messages-body shape)
# ---------------------------------------------------------------------------

def test_anthropic_thinking_fields_emit_adaptive_plus_effort():
    assert R.anthropic_thinking_fields("claude-opus-4-8", "max") == {
        "thinking": {"type": "adaptive"},
        "reasoning_effort": "max",
    }
    assert R.anthropic_thinking_fields("claude-opus-4-8", "xhigh") == {
        "thinking": {"type": "adaptive"},
        "reasoning_effort": "xhigh",
    }


def test_anthropic_off_emits_nothing():
    assert R.anthropic_thinking_fields("claude-opus-4-8", "off") == {}
    assert R.anthropic_thinking_fields("claude-opus-4-8", "") == {}
    assert R.anthropic_thinking_fields("claude-opus-4-8", None) == {}


def test_anthropic_apply_reasoning_is_a_noop_on_openai_body():
    # the OpenAI-compat core must NOT touch an Anthropic body (different shape).
    assert _apply("anthropic", "claude-opus-4-8", "high") == {}


# ---------------------------------------------------------------------------
#  DISCOVERY — curated_levels / reasons / per-model overrides
# ---------------------------------------------------------------------------

def test_curated_levels_provider_default_and_model_override():
    assert R.curated_levels("openai", "gpt-5.5") == ["minimal", "low", "medium", "high", "xhigh"]
    assert R.curated_levels("xai", "grok-3-mini") == ["low", "high"]
    assert R.curated_levels("anthropic", "claude-opus-4-8") == ["low", "medium", "high", "max", "xhigh"]


def test_curated_levels_non_reasoner_is_empty():
    assert R.curated_levels("xai", "grok-4-0709") == []
    assert R.curated_levels("fireworks", "accounts/fireworks/models/kimi-k2") == []


def test_curated_levels_unless_carveout():
    # kimi-k2-thinking is the carve-out from the kimi-k2 non-reasoner rule.
    assert R.curated_levels(
        "fireworks", "accounts/fireworks/models/kimi-k2-thinking"
    ) == ["low", "medium", "high"]


def test_reasons_flag():
    assert R.reasons("openai", "gpt-5.5") is True
    assert R.reasons("deepseek", "deepseek-v4") is True       # toggle still reasons
    assert R.reasons("xai", "grok-4-0709") is False           # always-on, no param
    assert R.reasons("fireworks", "accounts/fireworks/models/kimi-k2") is False
    assert R.reasons("cohere", "command-r") is False          # unknown provider


def test_connector_known():
    assert R.connector_known("openai") is True
    assert R.connector_known("cohere") is False
    assert R.connector_known("custom:x") is False


def test_resolve_level_clamp_and_floor():
    # above max → clamp to max.
    assert R.resolve_level("xai", "grok-3-mini", "xhigh") == "high"
    # toggle providers → the sentinel.
    assert R.resolve_level("deepseek", "deepseek-v4", "high") == "_toggle_"
    # off / non-reasoner → None.
    assert R.resolve_level("openai", "gpt-5.5", "off") is None
    assert R.resolve_level("xai", "grok-4-0709", "high") is None


def test_live_model_levels_override_curated_ladders():
    payload = {"model": "gpt-future", "messages": []}
    R.apply_reasoning(
        "openai",
        "gpt-future",
        "ultra",
        payload,
        available_levels=["low", "ultra"],
    )
    assert payload["reasoning_effort"] == "ultra"

    suppressed = {"model": "gpt-5.5", "messages": []}
    R.apply_reasoning(
        "openai",
        "gpt-5.5",
        "high",
        suppressed,
        available_levels=[],
    )
    assert "reasoning_effort" not in suppressed


def test_manifold_uses_its_live_per_model_effort_ladder():
    payload = {"model": "provider/model", "messages": []}
    R.apply_reasoning(
        "kaidera-manifold",
        "provider/model",
        "max",
        payload,
        available_levels=["low", "high", "max"],
    )
    assert payload["reasoning_effort"] == "max"


# ---------------------------------------------------------------------------
#  OUTPUT (B4) — read the right reasoning field per provider
# ---------------------------------------------------------------------------

def test_extract_reasoning_text_reads_both_field_names():
    rc = {"choices": [{"message": {"reasoning_content": "step-by-step"}}]}
    assert R.extract_reasoning_text("deepseek", rc) == "step-by-step"
    r = {"choices": [{"message": {"reasoning": "thinking…"}}]}
    assert R.extract_reasoning_text("together", r) == "thinking…"


def test_extract_reasoning_text_absent_is_empty_and_never_raises():
    assert R.extract_reasoning_text("openai", {"choices": [{"message": {"content": "hi"}}]}) == ""
    assert R.extract_reasoning_text("openai", {}) == ""
    assert R.extract_reasoning_text("openai", {"choices": "garbage"}) == ""
