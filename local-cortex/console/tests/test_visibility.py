"""Agent-visibility helper tests.

The former ``_enrich_run_from_cortex`` tests were RETIRED in Milestone 1 T7: the
~2s Cortex re-grep is deleted (the worker now writes spans directly to the
RunState SSOT store, and the agent-detail pane reads them from there — see
tests/test_agent_runs_store.py for the store-backed read-path coverage). What
remains here is the lead/CPO resolution that picks which interactive agent leads
the fleet — unrelated to the run-state read model and still load-bearing.
"""

from app.main import _lead_agent_name


def test_lead_agent_prefers_cpo_tag_over_alphabetical():
    # The lead/CPO is the interactive agent with the CPO tag — NOT just the first
    # interactive (which is alphabetical). For kaidera-os that's Ren, not Kai.
    groups = {"interactive": [{"name": "kai", "cpo_tag": False}, {"name": "ren", "cpo_tag": True}]}
    assert _lead_agent_name(groups) == "ren"


def test_lead_agent_falls_back_then_none():
    assert _lead_agent_name({"interactive": [{"name": "kai", "cpo_tag": False}]}) == "kai"
    assert _lead_agent_name({"interactive": [], "autonomous": [{"name": "bob"}]}) is None
    assert _lead_agent_name({}) is None
