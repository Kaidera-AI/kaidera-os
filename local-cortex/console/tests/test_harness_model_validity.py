"""Feature #99 — harness/model VALIDITY (constrain the model to its harness + coerce
invalid pairs).

The CTO saw IMPOSSIBLE stored pairs (e.g. an agent whose harness is `claude-code`
but whose model is `gemini-3.1-pro-preview` — claude-code can't run a Gemini model),
and the dogfood PROVED it isn't cosmetic: an agent configured with a harness/model
its harness can't run fails to execute. The SPA model dropdown already CONSTRAINS on
EDIT (it repopulates from `models_by_harness`); the gap is STORED/registry pairs that
are already invalid (a stale override after a harness change, or a registry capability
carrying a cross-harness model).

The fix lives at the harness data layer:
  * `valid_model_for_harness(harness, model) -> bool` — is this model in that
    harness's `HARNESS_MODELS` (fixed lanes) / is the harness a catalog lane (any
    catalog model is allowed) / safe defaults for an unknown harness or blank model.
  * `coerce_model(harness, model) -> str | None` — returns the model UNCHANGED when
    it's valid for the harness, else the harness's DEFAULT model (the first entry in
    that harness's `HARNESS_MODELS`); a catalog lane / unknown harness passes the
    model through (no fixed list to coerce against).

These are pure (no I/O); the resolution-path + config-view application is covered in
`test_harness_model_coercion_applied.py`.

Written BEFORE the implementation (STRICT TDD).
"""

from __future__ import annotations

from app import harness as h


# ---------------------------------------------------------------------------
#  valid_model_for_harness — the predicate.
# ---------------------------------------------------------------------------


def test_valid_model_for_harness_keeps_a_valid_fixed_pair():
    """A model that IS in the harness's fixed list is valid."""
    assert h.valid_model_for_harness("claude-code", "opus") is True
    assert h.valid_model_for_harness("claude-code", "claude-opus-4-8[1m]") is True
    assert h.valid_model_for_harness("pi", "gpt-5.5") is True


def test_valid_model_for_harness_rejects_a_cross_harness_model():
    """A model from a DIFFERENT harness's list is INVALID for this harness — the
    impossible pair the CTO caught (claude-code carrying a Gemini model)."""
    assert h.valid_model_for_harness("claude-code", "gemini-3.1-pro") is False
    assert h.valid_model_for_harness("claude-code", "gemini-3.1-pro-preview") is False
    # a codex/pi model under claude-code is equally impossible
    assert h.valid_model_for_harness("claude-code", "gpt-5.5") is False


def test_valid_model_for_harness_resolves_aliases():
    """The harness arg is canonicalised before validating its model list."""
    assert h.valid_model_for_harness("anthropic", "opus") is True


def test_valid_model_for_harness_accepts_operator_added_claude_models(monkeypatch):
    """Claude Code is a fixed subscription lane, but operators can add newly exposed
    aliases/full ids before the next Kaidera OS release. Those rows must be visible to
    validation so the runtime does not coerce them back to the default."""
    from app import settings

    monkeypatch.setattr(
        settings,
        "load_harness_model_overrides",
        lambda: {"claude-code": [{"value": "claude-future-5", "label": "Future 5"}]},
    )

    assert h.valid_model_for_harness("claude-code", "claude-future-5") is True
    assert h.coerce_model("claude-code", "claude-future-5") == "claude-future-5"
    assert any(
        opt["value"] == "claude-future-5"
        for opt in h.harness_model_options("claude-code")
    )


def test_valid_model_for_harness_catalog_lane_allows_any_model():
    """A CATALOG lane (kaidera/pi) has no fixed list — its models
    come from the live Providers catalog — so ANY non-blank model is accepted (we
    can't enumerate the catalog here, and over-coercing would wipe a valid catalog
    pick)."""
    assert h.valid_model_for_harness("kaidera", "anthropic/claude-opus") is True
    assert h.valid_model_for_harness("kaidera", "openai/gpt-5.5") is True
    assert h.valid_model_for_harness("pi", "fireworks/accounts/fireworks/models/kimi-k2p6") is True


def test_valid_model_for_harness_blank_model_is_valid():
    """A blank/None model is treated as valid (there's nothing to coerce — the
    routing layer fills the harness default separately)."""
    assert h.valid_model_for_harness("claude-code", None) is True
    assert h.valid_model_for_harness("claude-code", "") is True
    assert h.valid_model_for_harness("claude-code", "   ") is True


def test_valid_model_for_harness_unknkaidera_is_permissive():
    """An UNKNOWN harness (not one of the five, no fixed list) can't be validated
    against a list, so any model is accepted (we never guess a coercion target for a
    harness we don't model)."""
    assert h.valid_model_for_harness("some-future-harness", "whatever-model") is True
    assert h.valid_model_for_harness(None, "whatever-model") is True


# ---------------------------------------------------------------------------
#  coerce_model — the corrector.
# ---------------------------------------------------------------------------


def test_coerce_model_keeps_a_valid_model():
    """A valid pair is returned UNCHANGED."""
    assert h.coerce_model("claude-code", "opus") == "opus"
    assert h.coerce_model("claude-code", "claude-opus-4-8[1m]") == "claude-opus-4-8[1m]"
    assert h.coerce_model("pi", "gpt-5.5") == "gpt-5.5"


def test_coerce_model_replaces_a_cross_harness_model_with_the_harness_default():
    """An INVALID (cross-harness) model is coerced to the harness's DEFAULT model
    (the first entry in that harness's fixed list) — so a run NEVER spawns with an
    impossible pair."""
    # claude-code + a Gemini model → the claude-code default (its first model).
    default_cc = h.HARNESS_MODELS["claude-code"][0]["value"]
    assert h.coerce_model("claude-code", "gemini-3.1-pro-preview") == default_cc
    assert h.coerce_model("claude-code", "gpt-5.5") == default_cc


def test_coerce_model_blank_model_passes_through():
    """A blank/None model is returned as-is (None/"" — there's nothing to coerce; the
    routing layer fills the default separately)."""
    assert h.coerce_model("claude-code", None) is None
    assert h.coerce_model("claude-code", "") == ""


def test_coerce_model_catalog_lane_passes_through():
    """A catalog lane (kaidera) has no fixed list, so the model is returned
    unchanged (a valid catalog pick must never be wiped)."""
    assert h.coerce_model("kaidera", "anthropic/claude-opus") == "anthropic/claude-opus"


def test_coerce_model_unknkaidera_passes_through():
    """An unknown harness has no default to coerce to, so the model is returned
    unchanged (never guess a target for a harness we don't model)."""
    assert h.coerce_model("future-harness", "x-model") == "x-model"


# ---------------------------------------------------------------------------
#  attachment capabilities — image support is per harness/model.
# ---------------------------------------------------------------------------


def test_supports_vision_attachments_for_pi_image_models_only():
    """The pi catalog comment is executable policy: spark is text-only; the other
    verified pi models can receive image attachments through the chat path."""
    assert h.supports_vision_attachments("pi", "gpt-5.4") is True
    assert h.supports_vision_attachments("pi", "gpt-5.3-codex") is True
    assert h.supports_vision_attachments("pi", "gpt-5.3-codex-spark") is False


def test_supports_vision_attachments_rejects_unknown_or_non_pi_pairs():
    assert h.supports_vision_attachments("claude-code", "opus") is False
    assert h.supports_vision_attachments("codex", "gpt-5.5") is False
    assert h.supports_vision_attachments("pi", "not-a-pi-model") is False
    assert h.supports_vision_attachments("pi", None) is False


def test_attachment_capabilities_are_serialisable_and_reasoned():
    on = h.attachment_capabilities("pi", "gpt-5.4")
    off = h.attachment_capabilities("pi", "gpt-5.3-codex-spark")
    assert on == {
        "text": True,
        "image": True,
        "reason": "vision-capable pi model",
    }
    assert off["text"] is True
    assert off["image"] is False
    assert "not readable" in off["reason"]
