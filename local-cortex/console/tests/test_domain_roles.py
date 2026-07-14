"""The shared ROLE-ALIAS resolver (`app/domain/roles.py`) — the pure functional core
that lets the autonomy orchestrator + the Dispatch view route role-addressed handoffs.

The handoff dispatch queue is addressed by `to_role` (e.g. `cpo`, `cto`,
`full-stack-developer`). The plain by-role match only matches an agent whose role
string LITERALLY equals the to_role. This module adds a general alias layer on top:

  * a `cpo`/lead alias (`cpo` / `co-lead` / `lead`) resolves to the project's
    INTERACTIVE-designated lead agent — designation-driven (override-first → registry
    heuristic), NOT a hardcoded agent name (so it is project-general: any project's
    `cpo` handoff routes to THAT project's interactive lead).
  * a NON-DISPATCHABLE role (`cto` / `human` / `operator`) MUST stay unresolved
    (the human; left for a person) — even if some agent happens to carry that role.
  * a real role (`full-stack-developer`, ...) keeps resolving via the literal by-role
    match (the existing working path).
  * a role that matches nothing (e.g. a cross-project `alpha`) stays unresolved.

These tests pin the module in isolation: no live Cortex / DB. The roster + the
designation map are plain fixtures, and the interactive classifier is injected (so
the test proves the resolution is designation-driven, not a name literal). The
fixtures intentionally give the interactive lead a NON-cpo literal role so the alias
path (not an accidental literal match) is what resolves it.
"""

from __future__ import annotations

import app.domain.roles as roles


# ---------------------------------------------------------------------------
#  Fixtures — a roster + a designation map + an injected interactive classifier.
# ---------------------------------------------------------------------------
# The interactive lead ("ren") deliberately carries a GENERIC dev role here (NOT
# "cpo"), so a `cpo` to_role can only resolve to her via the ALIAS + the interactive
# designation — never via an accidental literal by-role match. This is the whole
# point: the mapping is designation-driven, project-general, not a name hardcode.
ROSTER = [
    {"name": "ren", "role": "full-stack-developer",
     "capabilities": {"display_name": "Ren"}},
    {"name": "bob", "role": "full-stack-developer",
     "capabilities": {"display_name": "Bob"}},
    {"name": "quill", "role": "qa", "capabilities": {"display_name": "Quill"}},
    {"name": "cole", "role": "orchestrator",
     "capabilities": {"display_name": "Cole"}},
]

# Designation OVERRIDES (the console app-DB layer) — ren is the interactive lead.
# Keyed by bare agent name here (the injected `designation_of` resolves the name).
DESIGNATIONS = {"ren": "interactive"}


def _designation_of(name: str) -> str:
    """Stand-in for settings_store.get_agent_designation(project, name) — the
    override-first signal the real resolver threads in."""
    return DESIGNATIONS.get((name or "").lower(), "")


def _classify_interactive(agent: dict, designation: str = "") -> bool:
    """Stand-in for the agents-service classifier: the designation override wins,
    else a tiny registry heuristic on the role string. Mirrors the real
    `agents.service.classify_interactive` contract (override-first → heuristic) so the
    test proves the alias resolves via DESIGNATION, not a hardcoded name."""
    if designation == "interactive":
        return True
    if designation == "autonomous":
        return False
    role = (agent.get("role") or "").lower()
    return any(h in role for h in ("cpo", "cmo", "lead", "pm", "product"))


# ---------------------------------------------------------------------------
#  The pure category predicates.
# ---------------------------------------------------------------------------


def test_non_dispatchable_roles_are_the_human_set():
    """cto / human / operator are the human — never an agent."""
    assert roles.is_non_dispatchable_role("cto") is True
    assert roles.is_non_dispatchable_role("human") is True
    assert roles.is_non_dispatchable_role("operator") is True
    # cased tokens still classify.
    assert roles.is_non_dispatchable_role("CTO") is True
    # a real role / a lead alias is NOT in the human set.
    assert roles.is_non_dispatchable_role("full-stack-developer") is False
    assert roles.is_non_dispatchable_role("cpo") is False
    assert roles.is_non_dispatchable_role("") is False


def test_lead_alias_roles_cover_domain_lead_synonyms():
    """Domain lead titles and generic lead synonyms map onto the interactive lead."""
    for r in ("cpo", "cmo", "co-lead", "lead"):
        assert roles.is_lead_alias_role(r) is True, r
    # cto is NOT a lead alias (it's the human), nor is a plain dev role. PM is
    # a distinct worker role and must resolve literally or through role_aliases.
    assert roles.is_lead_alias_role("cto") is False
    assert roles.is_lead_alias_role("pm") is False
    assert roles.is_lead_alias_role("full-stack-developer") is False
    assert roles.is_lead_alias_role("") is False


# ---------------------------------------------------------------------------
#  interactive_lead — the designation-driven lead finder.
# ---------------------------------------------------------------------------


def test_interactive_lead_resolves_via_designation_not_name():
    """The interactive lead is found via the injected classifier (designation
    override → heuristic), NOT by matching a hardcoded agent name. Here ren carries a
    generic dev role and is only interactive because of her designation override."""
    lead = roles.interactive_lead(
        ROSTER, designation_of=_designation_of,
        classify_interactive=_classify_interactive,
    )
    assert lead is not None
    assert lead["name"] == "ren"


def test_interactive_lead_is_project_general_picks_whoever_is_interactive():
    """A DIFFERENT project whose interactive lead is a different agent resolves to
    THAT agent — proving the finder is not ren-specific. Here 'bob' is the one with
    the interactive designation."""
    designations = {"bob": "interactive"}
    lead = roles.interactive_lead(
        ROSTER,
        designation_of=lambda n: designations.get((n or "").lower(), ""),
        classify_interactive=_classify_interactive,
    )
    assert lead is not None and lead["name"] == "bob"


def test_interactive_lead_none_when_no_interactive_agent():
    """No interactive agent in the roster → None (nothing to alias onto)."""
    autonomous_only = [
        {"name": "bob", "role": "full-stack-developer", "capabilities": {}},
        {"name": "cole", "role": "orchestrator", "capabilities": {}},
    ]
    lead = roles.interactive_lead(
        autonomous_only, designation_of=lambda n: "",
        classify_interactive=_classify_interactive,
    )
    assert lead is None


# ---------------------------------------------------------------------------
#  resolve_target — the ONE shared resolver both call sites use.
# ---------------------------------------------------------------------------


def _resolve(handoff: dict, roster=None) -> dict | None:
    return roles.resolve_target(
        handoff,
        roster if roster is not None else ROSTER,
        designation_of=_designation_of,
        classify_interactive=_classify_interactive,
    )


def test_cpo_to_role_resolves_to_interactive_lead():
    """A `cpo` to_role with no to_agent → the interactive lead (ren), via the alias —
    even though no agent here carries the literal role 'cpo'."""
    target = _resolve({"to_role": "cpo"})
    assert target is not None and target["name"] == "ren"
    detail = roles.resolve_target_detail(
        {"to_role": "cpo"},
        ROSTER,
        designation_of=_designation_of,
        classify_interactive=_classify_interactive,
    )
    assert detail["status"] == "resolved"
    assert detail["reason_code"] == "lead_alias"
    assert detail["matched_on"] == "role"


def test_role_alias_from_capabilities_resolves_before_lead_alias():
    """An alias in the registry capabilities wins over the lead-alias fallback and
    routes a non-literal role to the agent that lists it."""
    roster = ROSTER + [
        {"name": "gem", "role": "graphics",
         "capabilities": {"display_name": "Gem", "role_aliases": ["creative-multimedia"]}},
    ]
    target = _resolve({"to_role": "creative-multimedia"}, roster=roster)
    assert target is not None and target["name"] == "gem"


def test_role_alias_from_override_reader_resolves_before_lead_alias():
    """A console override role_aliases string routes a secondary role to the agent."""
    roster = ROSTER + [
        {"name": "gem", "role": "graphics", "capabilities": {"display_name": "Gem"}},
    ]
    aliases = {"gem": "creative-multimedia, multimedia-designer"}
    target = roles.resolve_target(
        {"to_role": "creative-multimedia"},
        roster,
        designation_of=_designation_of,
        classify_interactive=_classify_interactive,
        aliases_of=lambda name: aliases.get((name or "").lower(), ""),
    )
    assert target is not None and target["name"] == "gem"


def test_role_alias_does_not_shadow_literal_role_match():
    """A literal by-role match wins over an alias, so real roles stay authoritative."""
    roster = [
        {"name": "saul", "role": "creative-director",
         "capabilities": {"display_name": "Saul", "role_aliases": ["graphics"]}},
        {"name": "gem", "role": "graphics",
         "capabilities": {"display_name": "Gem"}},
    ]
    target = _resolve({"to_role": "graphics"}, roster=roster)
    assert target is not None and target["name"] == "gem"


def test_role_alias_does_not_shadow_to_agent():
    """An explicit to_agent wins over any alias."""
    roster = ROSTER + [
        {"name": "gem", "role": "graphics",
         "capabilities": {"display_name": "Gem", "role_aliases": ["creative-multimedia"]}},
    ]
    target = _resolve({"to_agent": "ren", "to_role": "creative-multimedia"}, roster=roster)
    assert target is not None and target["name"] == "ren"


def test_lead_synonym_to_roles_resolve_to_interactive_lead():
    """co-lead / lead route to the interactive lead the same way."""
    for r in ("co-lead", "lead"):
        target = _resolve({"to_role": r})
        assert target is not None and target["name"] == "ren", r


def test_pm_to_role_requires_pm_worker_or_alias_not_lead_fallback():
    """PM is a separate worker role, not a lead synonym. With no literal PM role and
    no role_aliases=pm, a pm-targeted handoff stays unresolved instead of waking the
    interactive lead."""
    assert _resolve({"to_role": "pm"}) is None
    detail = roles.resolve_target_detail(
        {"to_role": "pm"},
        ROSTER,
        designation_of=_designation_of,
        classify_interactive=_classify_interactive,
    )
    assert detail["status"] == "unresolved"
    assert detail["reason_code"] == "unknown_role"


def test_cto_to_role_stays_unresolved_even_if_an_agent_has_that_role():
    """cto → None (the human), and the non-dispatchable set wins even when an agent
    literally carries that role (so it can never accidentally auto-dispatch)."""
    assert _resolve({"to_role": "cto"}) is None
    roster_with_cto = ROSTER + [
        {"name": "ctobot", "role": "cto", "capabilities": {"display_name": "CtoBot"}},
    ]
    assert _resolve({"to_role": "cto"}, roster=roster_with_cto) is None
    detail = roles.resolve_target_detail(
        {"to_role": "cto"},
        roster_with_cto,
        designation_of=_designation_of,
        classify_interactive=_classify_interactive,
    )
    assert detail["status"] == "blocked"
    assert detail["reason_code"] == "human_target"
    assert detail["target_type"] == "role"


def test_human_and_operator_to_roles_stay_unresolved():
    """human / operator are the human too → None."""
    assert _resolve({"to_role": "human"}) is None
    assert _resolve({"to_role": "operator"}) is None


def test_real_role_resolves_via_literal_by_role_match():
    """full-stack-developer → the agent with that literal role (the existing working
    path is preserved). Here that is ren (first in roster order)."""
    target = _resolve({"to_role": "full-stack-developer"})
    assert target is not None and target["role"] == "full-stack-developer"


def test_unknown_cross_project_role_stays_unresolved():
    """A to_role that matches nothing in the roster (a cross-project `alpha`) → None."""
    assert _resolve({"to_role": "alpha"}) is None
    detail = roles.resolve_target_detail(
        {"to_role": "alpha"},
        ROSTER,
        designation_of=_designation_of,
        classify_interactive=_classify_interactive,
    )
    assert detail["status"] == "unresolved"
    assert detail["reason_code"] == "unknown_role"


def test_to_agent_takes_precedence_over_to_role_alias():
    """An explicit to_agent wins over to_role — even a cpo to_role alias."""
    target = _resolve({"to_agent": "bob", "to_role": "cpo"})
    assert target is not None and target["name"] == "bob"


def test_colon_compound_target_is_not_silently_normalized():
    """Malformed colon identities do not resolve invisibly after identity v2."""
    assert _resolve({"to_agent": "bob:5872"}) is None
    assert _resolve({"to_role": "cpo:5872"}) is None


def test_empty_handoff_resolves_to_none():
    """No to_agent / to_role at all → None."""
    assert _resolve({}) is None
    detail = roles.resolve_target_detail(
        {},
        ROSTER,
        designation_of=_designation_of,
        classify_interactive=_classify_interactive,
    )
    assert detail["status"] == "unresolved"
    assert detail["reason_code"] == "no_target"


def test_to_agent_unknown_has_explicit_resolution_reason():
    """Unknown explicit assignee is an unresolved agent target, not a silent miss."""
    detail = roles.resolve_target_detail(
        {"to_agent": "nobody"},
        ROSTER,
        designation_of=_designation_of,
        classify_interactive=_classify_interactive,
    )
    assert detail["agent"] is None
    assert detail["status"] == "unresolved"
    assert detail["reason_code"] == "unknown_agent"
    assert detail["target_type"] == "agent"


def test_lead_alias_without_interactive_lead_has_clear_blocker():
    """A lead alias without a configured interactive lead blocks with a useful reason."""
    autonomous_only = [
        {"name": "bob", "role": "full-stack-developer", "capabilities": {}},
        {"name": "cole", "role": "orchestrator", "capabilities": {}},
    ]
    detail = roles.resolve_target_detail(
        {"to_role": "cpo"},
        autonomous_only,
        designation_of=lambda n: "",
        classify_interactive=_classify_interactive,
    )
    assert detail["agent"] is None
    assert detail["status"] == "blocked"
    assert detail["reason_code"] == "no_interactive_lead"


def test_default_classifier_matches_agents_service():
    """PARITY: the domain's registry-heuristic default
    (`default_classify_interactive`) must agree with `agents.service.classify_interactive`
    on representative rosters — so the dispatch JSON board (which uses the domain
    default, being module-independent) resolves the SAME lead the HTML board does (it
    injects the agents-service classifier). A drift here would split the two boards."""
    from app.agents import service as agents_service

    samples = [
        {"name": "ren", "role": "cpo", "capabilities": {}},
        {"name": "kai", "role": "pm",
         "capabilities": {"kaidera_os_role": "pm-cpo-fullstack", "pm_cpo_cadence_owner": True}},
        {"name": "bob", "role": "full-stack-developer", "capabilities": {}},
        {"name": "quill", "role": "knowledge-keeper", "capabilities": {}},
        {"name": "cole", "role": "orchestrator", "capabilities": {}},
        {"name": "ren-smoke", "role": "cpo", "capabilities": {}},  # polluted name
    ]
    for a in samples:
        for desig in ("", "interactive", "autonomous"):
            assert roles.default_classify_interactive(a, desig) == \
                agents_service.classify_interactive(a, desig), (a["name"], desig)


def test_module_imports_nothing_outward():
    """GUARD: the domain alias module is PURE — its source imports none of the
    outward libraries (httpx / fastapi / subprocess / psycopg2 / asyncpg) and does
    not reach for app.main / app.adapters. Parsed via ast so a name in a
    comment/docstring can't fool it (mirrors test_ports_purity)."""
    import ast
    from pathlib import Path

    src = (Path(roles.__file__)).read_text()
    tree = ast.parse(src)
    forbidden = {
        "httpx", "fastapi", "starlette", "subprocess", "psycopg2", "asyncpg",
        "app.adapters", "app.main",
    }
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                seen.add(a.name.split(".")[0])
                seen.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                seen.add(node.module.split(".")[0])
                seen.add(node.module)
    assert not (seen & forbidden), f"domain roles imports outward: {seen & forbidden}"
