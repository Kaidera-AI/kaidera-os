"""Role→agent resolution for the autonomy dispatch path — the TWIN resolvers agree.

A handoff addressed by `to_role` (the kaidera-os dispatch queue is full of them — `cpo`,
`cto`, `full-stack-developer`, `alpha`) must resolve the SAME way in both places:

  * `main._resolve_target_agent` — the AUTONOMY ORCHESTRATOR's target resolver (the
    loop feeds the resolved agent to a run, or leaves it for a human when None), and
  * `app.dispatch.DispatchService.propose_agent` — the DISPATCH VIEW's proposal (the
    operator sees which agent each waiting handoff would go to).

Before this change a `cpo` to_role with no agent literally holding the role "cpo" fell
through to None ("no roster agent matches — left for human") in BOTH, so a cpo handoff
never surfaced a proposed agent. The shared alias layer (`app.domain.roles`) fixes it:
`cpo`/lead aliases resolve to the project's INTERACTIVE lead (designation-driven),
`cto`/human stay None, real roles keep their literal by-role match. These tests pin
that the two resolvers AGREE on every case, with the roster + designation FAKED (no
live Cortex / DB).

The fixture's interactive lead carries a GENERIC dev role (not "cpo"), so the cpo
resolution is proven to come from the alias + the interactive DESIGNATION, never an
accidental literal match or a name hardcode.
"""

from __future__ import annotations

import pytest

import app.main as main


# ---------------------------------------------------------------------------
#  Fixtures — a roster where the interactive lead has a NON-cpo literal role.
# ---------------------------------------------------------------------------
ROSTER = [
    # the interactive lead — a generic dev role, interactive ONLY by designation.
    {"name": "ren", "role": "full-stack-developer",
     "capabilities": {"display_name": "Ren", "harness": "claude-code"}},
    {"name": "bob", "role": "full-stack-developer",
     "capabilities": {"display_name": "Bob", "harness": "claude-code"}},
    {"name": "quill", "role": "qa", "capabilities": {"display_name": "Quill"}},
    {"name": "cole", "role": "orchestrator",
     "capabilities": {"display_name": "Cole"}},
]

# Designation overrides (the console app-DB layer the real resolvers read) — the
# orchestrator + the dispatch proposal both go through settings_store; we patch it.
DESIGNATIONS = {"ren": "interactive"}


@pytest.fixture(autouse=True)
def _fake_designations(monkeypatch):
    """Patch the designation store BOTH resolvers consult so the interactive lead is
    designation-driven, with NO live app-DB. Patched on every module that reads it:
    main (the orchestrator resolver) + the dispatch api builder."""
    def _get(project, agent, overrides=None):
        return DESIGNATIONS.get((agent or "").lower(), "")

    # main + settings_store share the same function object; patch at the source.
    monkeypatch.setattr(
        main.settings_store, "get_agent_designation", _get, raising=True
    )
    # the override map the dispatch proposal config-resolver reads (no overrides
    # needed for the match itself; keep it empty so harness/model fall back).
    monkeypatch.setattr(
        main.settings_store, "get_agent_override", lambda p, a: {}, raising=True
    )
    monkeypatch.setattr(
        main.settings_store, "load_agent_overrides",
        lambda: {f"kaidera-os:{n}": {"designation": d} for n, d in DESIGNATIONS.items()},
        raising=True,
    )


def _propose_name(handoff: dict) -> str | None:
    """The dispatch VIEW's proposed agent name for a handoff (None → unassigned).

    Builds a DispatchService wired with the SAME alias resolver main uses, then runs
    the pure proposal over the faked roster."""
    svc = main._dispatch_service
    by_name, by_role = svc.agent_index(ROSTER)
    # Pass the raw roster (5th arg) exactly as production's `dispatch_row` does, so the
    # lead-alias step can find the interactive lead.
    p = svc.propose_agent(handoff, by_name, by_role, "kaidera-os", ROSTER)
    return p["name"] if p else None


def _resolve_name(handoff: dict) -> str | None:
    """The ORCHESTRATOR's resolved target name for a handoff (None → left for human)."""
    target = main._resolve_target_agent(handoff, ROSTER)
    return target["name"] if target else None


def _both(handoff: dict) -> tuple[str | None, str | None]:
    """(orchestrator-resolved, dispatch-proposed) names for one handoff."""
    return _resolve_name(handoff), _propose_name(handoff)


# ---------------------------------------------------------------------------
#  The cases — each asserts the TWO resolvers AGREE + the expected outcome.
# ---------------------------------------------------------------------------


def test_cpo_to_role_resolves_to_interactive_lead_in_both():
    """`to_role=cpo` → the interactive lead (ren) in BOTH resolvers — via the alias,
    not a literal role match (no agent here holds role 'cpo')."""
    resolved, proposed = _both({"to_role": "cpo"})
    assert resolved == "ren"
    assert proposed == "ren"
    assert resolved == proposed


def test_cto_to_role_is_left_for_human_in_both():
    """`to_role=cto` → None (the human) in BOTH — never an agent."""
    resolved, proposed = _both({"to_role": "cto"})
    assert resolved is None
    assert proposed is None


def test_full_stack_developer_resolves_via_literal_role_in_both():
    """`to_role=full-stack-developer` → the agent with that literal role in BOTH (the
    existing working path is preserved). First-in-roster is ren."""
    resolved, proposed = _both({"to_role": "full-stack-developer"})
    assert resolved == "ren"   # first roster agent with the literal role
    assert proposed == "ren"
    assert resolved == proposed


def test_unknown_cross_project_role_unresolved_in_both():
    """`to_role=alpha` (cross-project, not in the roster) → None in BOTH."""
    resolved, proposed = _both({"to_role": "alpha"})
    assert resolved is None
    assert proposed is None


def test_to_agent_precedence_in_both():
    """An explicit to_agent wins over the to_role alias in BOTH resolvers."""
    resolved, proposed = _both({"to_agent": "bob", "to_role": "cpo"})
    assert resolved == "bob"
    assert proposed == "bob"
    assert resolved == proposed


def test_interactive_lead_is_designation_driven_not_a_name_literal(monkeypatch):
    """When the interactive lead is a DIFFERENT agent (bob designated interactive),
    a `cpo` handoff resolves to THAT agent in both resolvers — proving the mapping is
    designation-driven, not a hardcoded 'ren'."""
    def _get(project, agent, overrides=None):
        return "interactive" if (agent or "").lower() == "bob" else ""

    monkeypatch.setattr(main.settings_store, "get_agent_designation", _get, raising=True)
    monkeypatch.setattr(
        main.settings_store, "load_agent_overrides",
        lambda: {"kaidera-os:bob": {"designation": "interactive"}}, raising=True,
    )
    resolved, proposed = _both({"to_role": "cpo"})
    assert resolved == "bob"
    assert proposed == "bob"
    assert resolved == proposed


def test_role_alias_from_capabilities_resolves_to_same_agent_in_both():
    """`to_role=creative-multimedia` → the agent whose registry capability lists that
    alias in BOTH resolvers, so the board proposal and the orchestrator dispatch agree."""
    roster = ROSTER + [
        {"name": "gem", "role": "graphics",
         "capabilities": {"display_name": "Gem", "role_aliases": ["creative-multimedia"]}},
    ]
    resolved = _resolve_name_custom(roster, {"to_role": "creative-multimedia"})
    proposed = _propose_name_custom(roster, {"to_role": "creative-multimedia"})
    assert resolved == "gem"
    assert proposed == "gem"
    assert resolved == proposed


def test_role_alias_from_override_resolves_to_same_agent_in_both(monkeypatch):
    """`to_role=creative-multimedia` → the agent whose console override lists that
    alias in BOTH resolvers."""
    roster = ROSTER + [
        {"name": "gem", "role": "graphics", "capabilities": {"display_name": "Gem"}},
    ]

    def _get_override(project, agent):
        if (agent or "").lower() == "gem":
            return {"role_aliases": "creative-multimedia"}
        return {}

    monkeypatch.setattr(main.settings_store, "get_agent_override", _get_override, raising=True)
    resolved = _resolve_name_custom(roster, {"to_role": "creative-multimedia"})
    proposed = _propose_name_custom(roster, {"to_role": "creative-multimedia"})
    assert resolved == "gem"
    assert proposed == "gem"
    assert resolved == proposed


def _resolve_name_custom(roster: list, handoff: dict) -> str | None:
    """The ORCHESTRATOR's resolved target name for a handoff on a custom roster."""
    target = main._resolve_target_agent(handoff, roster)
    return target["name"] if target else None


def _propose_name_custom(roster: list, handoff: dict) -> str | None:
    """The dispatch VIEW's proposed agent name for a handoff on a custom roster."""
    svc = main._dispatch_service
    by_name, by_role = svc.agent_index(roster)
    p = svc.propose_agent(handoff, by_name, by_role, "kaidera-os", roster)
    return p["name"] if p else None


@pytest.mark.parametrize(
    "handoff",
    [
        {"to_role": "cpo"},
        {"to_role": "cto"},
        {"to_role": "alpha"},
        {"to_role": "full-stack-developer"},
        {"to_agent": "ren", "to_role": "cpo"},
        {"to_agent": "bob", "to_role": "full-stack-developer"},
        {"to_role": "co-lead"},
        {"to_role": "pm"},
        {},
    ],
)
def test_resolvers_agree_on_every_case(handoff):
    """SWEEP: for every dispatch-queue shape, the orchestrator's resolved agent and
    the dispatch view's proposed agent are the SAME (the proposal can never diverge
    from what would actually run)."""
    resolved, proposed = _both(handoff)
    assert resolved == proposed, f"{handoff}: resolved={resolved} proposed={proposed}"
