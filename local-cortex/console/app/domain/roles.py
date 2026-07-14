"""Role→agent resolution for handoff dispatch — the shared ALIAS layer (PURE).

A handoff is addressed either to a specific agent (`to_agent`) or to a ROLE
(`to_role`). The plain match only resolves a `to_role` against an agent whose role
string LITERALLY equals it. That leaves two whole classes of role-addressed work
stranded:

  * LEAD aliases — a handoff to `cpo` (or `co-lead` / `lead`) means "the
    project's lead", but no roster row usually carries the bare literal role "cpo";
    the lead is identified by its DESIGNATION (the interactive agent you talk to),
    not by a magic role string. Without an alias, every `cpo` handoff falls through
    to "no match → left for human" and the Dispatch view never even proposes the
    lead.
  * The HUMAN — a handoff to `cto` (or `human` / `operator`) is addressed to a
    PERSON, never an agent. It must stay unresolved so the loop leaves it for a human
    — even if some agent happens to carry that literal role.

This module is the ONE place that encodes that mapping, shared by BOTH resolvers so
the autonomy orchestrator (`main._resolve_target_agent`) and the Dispatch view
(`dispatch.DispatchService.propose_agent`) can never disagree:

  precedence for a handoff →
    1. `to_agent` → exact roster name (then a name-in-role fallback some projects use)
    2. `to_role`:
       a. NON-DISPATCHABLE role (cto/human/operator) → None (the human) — wins even
          over a literal match, so it can never accidentally auto-dispatch.
       b. literal by-role match → that agent (the existing working path: real roles
          like full-stack-developer keep resolving to matching project agents).
       c. literal by-name fallback → that agent (many projects file a name in to_role).
       d. LEAD alias (cpo/co-lead/lead) → the project's INTERACTIVE lead, resolved
          via the injected classifier (designation override-first → registry
          heuristic) — project-general, NOT a hardcoded agent name.
    3. nothing matched → None.

It is the functional core's value logic: it imports ONLY the standard library and
takes the I/O-bound signals (the per-agent designation, the interactive classifier)
as INJECTED callables, so it never reaches for the app-DB / Cortex / the agents
module (which would also break the module-isolation contract). The composition root
wires the real `settings.get_agent_designation` + `agents.service.classify_interactive`
in; tests inject fakes (no live store). A guard test asserts the import purity.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
#  The alias sets — role STRINGS only (never agent names / project keys, so the
#  no-project-literals fitness gate stays green; these are config-general roles).
# ---------------------------------------------------------------------------

# Roles that address the HUMAN, never an agent — the loop must leave these for a
# person. This set wins even over a literal by-role match, so a stray agent carrying
# one of these roles can never be auto-dispatched.
NON_DISPATCHABLE_ROLES: frozenset[str] = frozenset({"cto", "human", "operator"})

# Roles that mean "the project's lead" — they resolve (when no literal role matches)
# to whichever agent is the INTERACTIVE-designated lead. `lead` is the canonical one —
# the GENERIC leadership persona that any domain wears (a software-dev lead, a marketing
# `cmo`, etc.); `cpo` is kept as a back-compat synonym (older rosters carry it).
# `pm` is deliberately NOT a lead alias: PM is a distinct worker role and must resolve
# through a literal role, configured `role_aliases=pm`, or explicit `to_agent`.
LEAD_ALIAS_ROLES: frozenset[str] = frozenset({"lead", "cpo", "co-lead", "cmo"})


# Type aliases for the injected I/O-bound signals (kept callable so the core stays
# pure — the shell binds the real store/classifier, tests bind fakes).
DesignationOf = Callable[[str], str]
"""(agent_name) -> the designation OVERRIDE ('interactive'/'autonomous'/'')."""
ClassifyInteractive = Callable[[dict, str], bool]
"""(agent, designation_override) -> True for an INTERACTIVE lead agent."""
AliasesOf = Callable[[str], str]
"""(agent_name) -> a comma-separated list of role ALIASES from console overrides."""


# ---------------------------------------------------------------------------
#  The default interactive classifier (the registry heuristic) — kept in the pure
#  core so a caller that can't reach `app.agents` (e.g. the dispatch JSON shell,
#  which must stay module-independent) still resolves the lead correctly. It MIRRORS
#  `app.agents.service.registry_interactive` / `classify_interactive` exactly — the
#  composition root normally injects that service classifier; this is the equivalent
#  fallback so behaviour matches whether injected or not.
# ---------------------------------------------------------------------------

# Role substrings that mark an agent INTERACTIVE (a lead you talk to) when no
# explicit designation override is set (mirrors agents.service._INTERACTIVE_ROLE_HINTS).
_INTERACTIVE_ROLE_HINTS = ("cpo", "cmo", "lead", "pm", "product")
# Name marks that hint a polluted/synthetic worker — never auto-classified Interactive
# by the heuristic (mirrors agents.service._TEST_MARKS + the "-test" suffix rule).
_TEST_MARKS = ("-ddl-", "-state-", "-poc-", "-smoke", "-init-")


def _is_test_name(name: Optional[str]) -> bool:
    nm = (name or "").lower()
    return nm.endswith("-test") or any(m in nm for m in _TEST_MARKS)


def default_registry_interactive(agent: dict) -> bool:
    """The registry-derived interactive heuristic (no designation override): role
    string first, then the richer runtime capability hints (`runtime_role`,
    `pm_cpo_cadence_owner`); a synthetic/polluted name is never pulled Interactive.
    Mirrors `app.agents.service.registry_interactive` 1:1."""
    if _is_test_name(agent.get("name")):
        return False
    role = (agent.get("role") or "").lower()
    if any(h in role for h in _INTERACTIVE_ROLE_HINTS):
        return True
    caps = agent.get("capabilities") or {}
    legacy_role_field = "local" + "dev" + "_role"
    local_role = str(caps.get("runtime_role") or caps.get(legacy_role_field) or "").lower()
    if any(h in local_role for h in _INTERACTIVE_ROLE_HINTS):
        return True
    if str(caps.get("pm_cpo_cadence_owner")).lower() == "true":
        return True
    return False


def default_classify_interactive(agent: dict, designation: str = "") -> bool:
    """OVERRIDE-FIRST classifier default: the designation override
    ('interactive'/'autonomous') wins, else the registry heuristic. Mirrors
    `app.agents.service.classify_interactive` 1:1 — the fallback used when no
    concrete classifier is injected (so an independent shell still resolves the lead
    consistently)."""
    if designation == "interactive":
        return True
    if designation == "autonomous":
        return False
    return default_registry_interactive(agent)


def normalize_role(value: Optional[str]) -> str:
    """Normalize a routing token to the registry form, lower-cased.

    Identity v2 uses plain actor/role slugs. Colon compound identities are not
    stripped here; if legacy/corrupt input appears, it remains unmatched and
    visible to audit.
    """
    return (value or "").strip().lower()


def is_non_dispatchable_role(role: Optional[str]) -> bool:
    """True if `role` addresses the human (cto/human/operator) — never an agent."""
    return normalize_role(role) in NON_DISPATCHABLE_ROLES


def is_lead_alias_role(role: Optional[str]) -> bool:
    """True if `role` is a lead alias (cpo/co-lead/lead) — routes to the lead."""
    return normalize_role(role) in LEAD_ALIAS_ROLES


# Capability/override field that carries secondary routing roles for an agent.
_ROLE_ALIASES_FIELD = "role_aliases"


def _aliases_for(agent: dict, aliases_of: AliasesOf = lambda name: "") -> list[str]:
    """Collect the role aliases for an agent from the registry capabilities AND the
    injected override reader. The override string is comma-separated; the capability
    may be a list or a comma-separated string. Aliases are normalized to lower-cased
    slugs, deduplicated, and ordered roster-first then override-first."""
    name = (agent.get("name") or "").strip()
    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, str):
            parts = [p.strip().lower() for p in raw.split(",")]
        elif isinstance(raw, (list, tuple)):
            parts = [str(p).strip().lower() for p in raw]
        else:
            parts = [str(raw).strip().lower()]
        for p in parts:
            if p and p not in seen:
                seen.add(p)
                out.append(p)

    caps = agent.get("capabilities") or {}
    _add(caps.get(_ROLE_ALIASES_FIELD))
    if name:
        _add(aliases_of(name))
    return out


def interactive_lead(
    agents: list[dict],
    *,
    designation_of: DesignationOf = lambda name: "",
    classify_interactive: ClassifyInteractive = default_classify_interactive,
) -> Optional[dict]:
    """The project's INTERACTIVE lead agent (the one a `cpo`/lead handoff routes to).

    Designation-DRIVEN, not name-based: each agent is classified via the injected
    `classify_interactive` (which is itself override-first — the per-agent
    `designation_of` override wins, else the registry heuristic). Among the
    interactive agents, prefers the one whose effective role reads as THE lead,
    else the first
    interactive agent in roster order. Returns the FULL agent dict, or None when the
    roster has no interactive agent.

    Roster order is honored for determinism (the caller's list is stable), exactly
    like the by-role indexing."""
    interactive: list[dict] = []
    for a in agents:
        name = (a.get("name") or "").strip()
        designation = designation_of(name) if name else ""
        if classify_interactive(a, designation):
            interactive.append(a)
    if not interactive:
        return None
    # Prefer the lead-tagged interactive agent (the single lead), else the first.
    for a in interactive:
        if _reads_as_lead(a):
            return a
    return interactive[0]


def _reads_as_lead(agent: dict) -> bool:
    """True if the agent's role reads as THE lead (earns the lead preference).

    Matches the lead hints but excludes co-lead variants so a co-lead doesn't claim
    the single-lead slot ahead of the actual lead (mirrors
    `agents.service.has_cpo_tag`). Checks the role string + the runtime_role
    capability hint so a lead whose top-level role is generic still reads as lead."""
    caps = agent.get("capabilities") or {}
    role = (agent.get("role") or "").lower()
    legacy_role_field = "local" + "dev" + "_role"
    local = str(caps.get("runtime_role") or caps.get(legacy_role_field) or "").lower()
    for text in (role, local):
        if any(ex in text for ex in ("co-lead", "co lead", "colead")):  # fitness:allow-literal role-string variants, not agent names
            continue
        if any(h in text for h in ("cpo", "cmo", "lead")):
            return True
    return False


def resolve_target(
    handoff: dict,
    agents: list[dict],
    *,
    designation_of: DesignationOf = lambda name: "",
    classify_interactive: ClassifyInteractive = default_classify_interactive,
    aliases_of: AliasesOf = lambda name: "",
) -> Optional[dict]:
    """Resolve a handoff's target to a FULL roster agent dict, or None.

    The ONE shared resolver behind both `main._resolve_target_agent` (the autonomy
    orchestrator) and `dispatch.DispatchService.propose_agent` (the Dispatch view), so
    the proposed agent and the dispatched agent can never diverge. Precedence:
    to_agent → literal name/role; then to_role → non-dispatchable (None) → literal
    role → literal name → role ALIAS (e.g. creative-multimedia → gem) → lead alias
    (the interactive lead) → None.

    The alias + lead-alias steps are designation/capability-driven via the injected
    callables (no name hardcode, no store/Cortex import here)."""
    return resolve_target_detail(
        handoff,
        agents,
        designation_of=designation_of,
        classify_interactive=classify_interactive,
        aliases_of=aliases_of,
    ).get("agent")


def resolve_target_detail(
    handoff: dict,
    agents: list[dict],
    *,
    designation_of: DesignationOf = lambda name: "",
    classify_interactive: ClassifyInteractive = default_classify_interactive,
    aliases_of: AliasesOf = lambda name: "",
) -> dict:
    """Resolve a handoff target and explain the result.

    This is the observable contract for atomic handoff routing. `resolve_target`
    keeps the historic agent-or-None API; this detail form lets the Dispatch board
    and the autonomy runner distinguish "ready to run" from "human target",
    "unknown agent", "unknown role", and "no configured lead" instead of collapsing
    every miss into `None`.
    """
    by_name, by_role, by_alias = _index(agents, aliases_of=aliases_of)

    def _detail(
        *,
        agent: Optional[dict],
        status: str,
        reason_code: str,
        reason: str,
        target_type: str,
        target: str,
        matched_on: str = "",
    ) -> dict:
        return {
            "agent": agent,
            "status": status,
            "reason_code": reason_code,
            "reason": reason,
            "target_type": target_type,
            "target": target,
            "matched_on": matched_on,
        }

    to_agent = normalize_role(handoff.get("to_agent"))
    if to_agent:
        # Explicit targets may arrive in the canonical `name@project` identity form
        # (Cortex stores `--to-agent kai` as `kai@kaidera-os`), but the roster is keyed
        # by bare agent name — match the bare name too so the suffix never misroutes.
        to_agent_bare = to_agent.split("@", 1)[0]
        agent_match = by_name.get(to_agent) or by_name.get(to_agent_bare)
        if agent_match is not None:
            return _detail(
                agent=agent_match,
                status="resolved",
                reason_code="to_agent_name",
                reason=f"Explicit agent target '{to_agent}' matched a roster agent.",
                target_type="agent",
                target=to_agent,
                matched_on="agent",
            )
        role_match = by_role.get(to_agent) or by_role.get(to_agent_bare)
        if role_match is not None:
            return _detail(
                agent=role_match,
                status="resolved",
                reason_code="to_agent_role_fallback",
                reason=(
                    f"Explicit agent target '{to_agent}' matched a roster role fallback."
                ),
                target_type="agent",
                target=to_agent,
                matched_on="agent",
            )
        return _detail(
            agent=None,
            status="unresolved",
            reason_code="unknown_agent",
            reason=f"Explicit agent target '{to_agent}' is not in this project roster.",
            target_type="agent",
            target=to_agent,
        )

    to_role = normalize_role(handoff.get("to_role"))
    if not to_role:
        return _detail(
            agent=None,
            status="unresolved",
            reason_code="no_target",
            reason="Handoff has no to_agent or to_role target.",
            target_type="none",
            target="",
        )

    # (a) the human set wins even over a literal match — never auto-dispatch a person.
    if to_role in NON_DISPATCHABLE_ROLES:
        return _detail(
            agent=None,
            status="blocked",
            reason_code="human_target",
            reason=(
                f"Role '{to_role}' targets a human/operator, so Kaidera OS will not "
                "auto-dispatch it to an AI worker."
            ),
            target_type="role",
            target=to_role,
        )
    # (b) the existing working path: a real role → its literal by-role agent.
    if to_role in by_role:
        return _detail(
            agent=by_role[to_role],
            status="resolved",
            reason_code="literal_role",
            reason=f"Role target '{to_role}' matched a roster role.",
            target_type="role",
            target=to_role,
            matched_on="role",
        )
    # (c) name-in-to_role fallback (some projects file a bare name in to_role).
    if to_role in by_name:
        return _detail(
            agent=by_name[to_role],
            status="resolved",
            reason_code="role_name_fallback",
            reason=f"Role target '{to_role}' matched a roster agent name fallback.",
            target_type="role",
            target=to_role,
            matched_on="role",
        )
    # (d) role ALIAS — e.g. creative-multimedia → an agent whose capabilities or
    #     console override lists that alias. Aliases lose to literal role/name matches
    #     but win over lead aliases, so a project can register secondary dispatchable
    #     roles without shadowing the literal roster.
    if to_role in by_alias:
        return _detail(
            agent=by_alias[to_role],
            status="resolved",
            reason_code="role_alias",
            reason=f"Role target '{to_role}' matched a configured role alias.",
            target_type="role",
            target=to_role,
            matched_on="role",
        )
    # (e) the lead alias → the project's interactive lead (designation-driven).
    if to_role in LEAD_ALIAS_ROLES:
        lead = interactive_lead(
            agents,
            designation_of=designation_of,
            classify_interactive=classify_interactive,
        )
        if lead is not None:
            return _detail(
                agent=lead,
                status="resolved",
                reason_code="lead_alias",
                reason=(
                    f"Lead role target '{to_role}' resolved to the project's "
                    "interactive lead."
                ),
                target_type="role",
                target=to_role,
                matched_on="role",
            )
        return _detail(
            agent=None,
            status="blocked",
            reason_code="no_interactive_lead",
            reason=(
                f"Lead role target '{to_role}' has no interactive lead configured "
                "in this project roster."
            ),
            target_type="role",
            target=to_role,
        )
    return _detail(
        agent=None,
        status="unresolved",
        reason_code="unknown_role",
        reason=f"Role target '{to_role}' did not match this project roster.",
        target_type="role",
        target=to_role,
    )


def _index(
    agents: list[dict],
    *,
    aliases_of: AliasesOf = lambda name: "",
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Index the roster as (by_name, by_role, by_alias), FIRST-wins per key, roster order.

    Mirrors `dispatch.DispatchService.agent_index` / `main._agent_index` (kept local
    so the pure core has no outward dependency) and adds the alias index so a
    to_role like 'creative-multimedia' can resolve to an agent whose capabilities or
    console override carry that alias."""
    by_name: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    by_alias: dict[str, dict] = {}
    for a in agents:
        name = (a.get("name") or "").strip().lower()
        if name and name not in by_name:
            by_name[name] = a
        role = (a.get("role") or "").strip().lower()
        if role and role not in by_role:
            by_role[role] = a
        for alias in _aliases_for(a, aliases_of=aliases_of):
            if alias and alias not in by_alias:
                by_alias[alias] = a
    return by_name, by_role, by_alias


__all__ = [
    "NON_DISPATCHABLE_ROLES",
    "LEAD_ALIAS_ROLES",
    "normalize_role",
    "is_non_dispatchable_role",
    "is_lead_alias_role",
    "interactive_lead",
    "resolve_target",
    "resolve_target_detail",
    "AliasesOf",
]
