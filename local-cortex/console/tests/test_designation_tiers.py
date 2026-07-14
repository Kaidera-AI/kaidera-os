"""Three-tier agent designation (interactive / autonomous / deterministic) — the
capability rules (chat? model?) and the classification guard.

Locks the contract for the tier added in v0.1.105:
  * `deterministic` is a recognised designation,
  * chat is enabled ONLY for `interactive`,
  * `deterministic` is NOT an AI worker (no model); interactive + autonomous are,
  * `classify_interactive` treats `deterministic` as non-interactive and does NOT
    fall through to the registry heuristic (which could otherwise flag a lead-named
    deterministic agent Interactive and wrongly give it a chat box).
"""

from __future__ import annotations

from app.agents import service as svc
from app.domain import designation as d


def test_three_tiers_recognised_others_blank():
    assert d.normalize_designation("interactive") == "interactive"
    assert d.normalize_designation("autonomous") == "autonomous"
    assert d.normalize_designation("deterministic") == "deterministic"
    assert d.normalize_designation("DETERMINISTIC") == "deterministic"  # case-insensitive
    assert d.normalize_designation("bogus") == ""
    assert d.normalize_designation(None) == ""


def test_chat_enabled_only_for_interactive():
    assert d.is_chat_enabled("interactive") is True
    assert d.is_chat_enabled("autonomous") is False
    assert d.is_chat_enabled("deterministic") is False
    assert d.is_chat_enabled("") is False


def test_ai_worker_is_everything_but_deterministic():
    assert d.is_ai_worker("interactive") is True
    assert d.is_ai_worker("autonomous") is True
    assert d.is_ai_worker("deterministic") is False
    # unset → the registry heuristic only ever yields interactive/autonomous, both
    # AI workers, so the safe default is True (has a model).
    assert d.is_ai_worker("") is True


def test_classify_deterministic_never_interactive():
    # A lead-named agent the registry heuristic WOULD call interactive…
    lead_like = {"name": "lead", "role": "lead"}
    assert svc.registry_interactive(lead_like) is True
    # …but an explicit deterministic/autonomous override classifies non-interactive,
    # without falling through to that heuristic (the chat-box leak this guards).
    assert svc.classify_interactive(lead_like, "deterministic") is False
    assert svc.classify_interactive(lead_like, "autonomous") is False
    assert svc.classify_interactive(lead_like, "interactive") is True
    # No override → the heuristic still applies (unchanged behaviour).
    assert svc.classify_interactive(lead_like, "") is True
