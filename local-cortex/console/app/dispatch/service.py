"""Dispatch BOARD feature logic — the read side of the Dispatch center, behind the
ports.

The functional core of the `dispatch` module (Track A, the FOURTH feature carve —
analytics → agents → settings preceded it). It owns the BOARD substance of the
Dispatch surface:

  1. LIST the open/pending handoffs (the dispatch queue), filtering out claimed
     (in-flight) + completed handoffs, each shaped into a render-ready row with a
     RULE-BASED proposed agent (to_agent → exact roster name, else to_role → first
     roster agent with that role; with the name-in-role fallbacks live projects
     need), sorted urgent-first (priority weight, newest-first within a band),
     capped.
  2. The board COUNTS — total open / with-a-proposal / unassigned.
  3. The project autonomy + propose-mode FLAG reads (fail-safe OFF) + the
     awaiting-approval id set (the parked-for-review handoffs the Approve button
     surfaces).

LAYER RULE (arrows point inward, ratified design §3): this module depends ONLY on
`domain.ports.CortexMemoryPort` (the dispatch queue — `get_handoffs()`) +
`domain.ports.OperationalStorePort` (the project flag reads — `is_project_autonomous`
/ `is_propose_mode`). It imports NOTHING outward (no fastapi / httpx / subprocess /
psycopg2 / asyncpg) and never reaches back into `app.main`, the concrete `app.appdb`
/ `app.adapters`, the concrete `app.harness`, or the
`app.orchestrator` imperative core. The two presentation/off-port concerns it needs
— a per-agent CONFIG resolver (the proposal's effective harness/model + labels) and
an AWAITING-APPROVAL lister (the parked-for-review id set — which is NOT on the port,
it lives in `settings.list_awaiting_approval`) — are INJECTED as plain callables
(the analytics/agents injection pattern), so the service stays free of the concrete
`harness` module and the off-port flag; the shell (`api.py`) / `main.py` inject the
real implementations when wiring, and the defaults below keep the service
self-contained for tests.

The ROSTER is passed IN by the caller (exactly as `AgentsService` takes its roster):
the board is queue-shaping over the roster, and the roster's origin (the Cortex
registry) is the caller's concern — keeping this service port-pure with no extra
Cortex coupling beyond the handoff queue it reads through the port.

The shaping + proposal logic is lifted 1:1 from `main._dispatch_is_open`,
`_agent_index`, `_normalize_target`, `_proposed_agent`, `_dispatch_row`,
`_dispatch_rows` (+ the board counts/flag reads from `_dispatch_context`), so the
carve is behaviour-preserving — `main.py` now delegates its board substance here,
making this the single source of that logic.

SCOPE — READ/BOARD ONLY. The orchestrator's IMPERATIVE core stays in `main.py` /
`orchestrator.py` for a LATER carve: `_dispatch_run` / spawn, `_pm_beat`, the
`POST /dispatch/{p}/run` (Approve & Run), the `POST /dispatch/{p}/autonomous` toggle,
the `POST .../approve` gate, and the orchestrator's live status / feed / wave
assembly. This module spawns nothing and writes nothing — it is the board read
surface.

Graceful-degrade is the house law: a down Cortex (empty queue) → an empty board; a
down operational store → both flags fail-safe OFF (an outage can only ever turn a
flag off, never on); a missing awaiting-approval lister → an empty parked set. It
never raises.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.domain import roles as role_alias
from app.domain.ports import CortexMemoryPort, OperationalStorePort

# Handoff statuses that count as "open / waiting for dispatch". A claimed/in-flight
# or completed handoff is NOT proposed for a fresh dispatch. Lifted 1:1 from
# `main._DISPATCH_OPEN_STATUSES`.
OPEN_STATUSES = ("pending", "open", "new", "unclaimed")

# Cap on open handoffs shown on the board (newest/most-urgent lead). Lifted from
# `main._DISPATCH_MAX`.
DISPATCH_MAX = 40

# Priority sort weight (urgent first). Unknown priorities sort last. Lifted from
# `main._DISPATCH_PRIORITY_ORDER`.
PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

# Clip length for the displayed summary (the full text rides along separately).
_SUMMARY_CLIP = 240


# ---------------------------------------------------------------------------
#  Pure helpers — the board/proposal primitives (lifted 1:1 from main.py)
# ---------------------------------------------------------------------------


def is_open(handoff: dict) -> bool:
    """True if a handoff is open/pending (waiting for dispatch) — not claimed,
    in-flight, or completed. Lifted 1:1 from `main._dispatch_is_open`."""
    status = (handoff.get("status") or "").strip().lower()
    if status and status not in OPEN_STATUSES:
        return False
    # A claimed handoff is in-flight even if its status string is stale.
    if handoff.get("claimed_by"):
        return False
    return True


def normalize_target(value: Optional[str]) -> str:
    """Normalize a handoff routing target to the registry token form.

    Identity v2 uses plain actor slugs and project UUID scope. Colon compound
    identities are intentionally not stripped here; if legacy/corrupt input
    appears, it should remain unmatched and visible to audit.
    """
    return (value or "").strip().lower()


def _short(text: str, n: int = 90) -> str:
    """Collapse whitespace and clip to n chars with an ellipsis. Lifted 1:1 from
    `main._short` (kept local so the service stays self-contained / port-pure)."""
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
#  Default presentation callable — so the service is self-contained (no concrete
#  harness dependency). The shell injects the real harness-backed resolver.
# ---------------------------------------------------------------------------


def _default_resolve_config(agent: dict, override: dict) -> dict:
    """Fallback per-agent config resolver when none is injected. Returns the
    proposal's effective harness/model from the override-overlaid-on-registry values
    (pass-through labels). The real injected resolver wraps `harness._registry_config`
    + `canonical_harness` + `harness_label` so the proposal's harness/model match the
    runner + the Configure card exactly."""
    caps = agent.get("capabilities") or {}
    harness = (override.get("harness") or caps.get("harness") or caps.get("provider") or "")
    harness = str(harness).strip().lower() or None
    model = (override.get("model") or agent.get("model") or caps.get("model_preference") or "")
    model = str(model).strip() or None
    return {
        "harness": harness,
        "harness_label": harness or "—",
        "model": model,
    }


# ---------------------------------------------------------------------------
#  The service
# ---------------------------------------------------------------------------


class DispatchService:
    """The dispatch BOARD: list the open/pending handoffs (each with a rule-based
    proposed agent), the board counts, and the project autonomy/propose-mode flags.

    Construct with the `CortexMemoryPort` (the dispatch queue source) + the
    `OperationalStorePort` (the project flag reads); the per-agent config resolver
    defaults to a self-contained function and can be overridden with the concrete
    `harness`-backed implementation at the shell so the proposal's harness/model
    match the rest of the UI, and the awaiting-approval lister is injected (it is NOT
    on the port — it lives in `settings.list_awaiting_approval`). The roster is
    passed to each call (the caller fetches it from Cortex), keeping the service
    port-pure.

    Both ports are optional so a caller that already holds the queue/flags (e.g.
    main.py threading them through a render path) can call the pure shaping helpers
    directly; `board(...)` reads the ports itself."""

    def __init__(
        self,
        *,
        cortex: Optional[CortexMemoryPort] = None,
        store: Optional[OperationalStorePort] = None,
        resolve_config: Callable[[dict, dict], dict] = _default_resolve_config,
        get_override: Callable[[str, str], dict] = lambda project, agent: {},
        awaiting_approval: Callable[[str], list] = lambda project: [],
        designation_of: Callable[[str, str], str] = lambda project, agent: "",
        classify_interactive: Callable[[dict, str], bool] = role_alias.default_classify_interactive,
        role_aliases_of: Callable[[str, str], str] = lambda project, agent: "",
    ) -> None:
        self._cortex = cortex
        self._store = store
        self._resolve_config = resolve_config
        # `get_override` resolves one agent's console override for the proposal's
        # harness/model (the agents-carve seam). Defaults to "no override" so the
        # service stays self-contained; the shell injects the store-backed reader.
        self._get_override = get_override
        # `awaiting_approval` lists the parked-for-review handoff ids (NOT on the
        # port). Defaults to none parked; the shell injects the real lister.
        self._awaiting_approval = awaiting_approval
        # The ROLE-ALIAS signals (so the proposal routes a `cpo`/lead to_role to the
        # project's INTERACTIVE lead, exactly like the orchestrator's resolver — the
        # proposed agent can never diverge from what would actually run). Both
        # DESIGNATION-driven, not name-based: `designation_of(project, agent)` is the
        # per-agent override (the agents-carve seam, keyed by project) and
        # `classify_interactive(agent, designation)` is the override-first classifier.
        # `designation_of` defaults to "no override" and `classify_interactive` to the
        # domain's registry-heuristic default (so an un-wired service still resolves
        # the lead from the roster, consistent with the orchestrator); the shell
        # injects the real store reader + the agents-service classifier for the
        # override-first path. The pure category sets (the
        # non-dispatchable human roles, the lead aliases) come from `app.domain.roles`,
        # the SAME source the orchestrator uses.
        self._designation_of = designation_of
        self._classify_interactive = classify_interactive
        # `role_aliases_of(project, agent)` returns the comma-separated role aliases
        # for an agent (console override or registry capability) so the Dispatch view
        # can resolve secondary roles like `creative-multimedia` the same way the
        # orchestrator does.
        self._role_aliases_of = role_aliases_of

    # -- pure shaping (no port read) -------------------------------------------

    @staticmethod
    def agent_index(agents: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
        """Index the roster for dispatch matching: (by_name, by_role).

        `by_name` maps a lower-cased agent name → the raw agent record. `by_role`
        maps a lower-cased role → the FIRST agent with that role (roster order;
        deterministic since the roster list is stable). Lifted 1:1 from
        `main._agent_index`."""
        by_name: dict[str, dict] = {}
        by_role: dict[str, dict] = {}
        for a in agents:
            name = (a.get("name") or "").strip().lower()
            if name and name not in by_name:
                by_name[name] = a
            role = (a.get("role") or "").strip().lower()
            if role and role not in by_role:
                by_role[role] = a
        return by_name, by_role

    def _alias_index(
        self, agents: list[dict], project_key: str
    ) -> dict[str, dict]:
        """Index the roster by role ALIAS (secondary dispatchable roles).

        Each agent's aliases come from its registry capabilities (`role_aliases`)
        AND the console override reader (`role_aliases_of`), normalized to lower-cased
        slugs. FIRST-wins per alias in roster order. Mirrors the domain's `_index`
        alias half so the Dispatch view agrees with the orchestrator."""
        by_alias: dict[str, dict] = {}
        for a in agents:
            name = (a.get("name") or "").strip()
            aliases: list[str] = []
            seen: set[str] = set()
            # registry capability (list or comma-separated string)
            raw = (a.get("capabilities") or {}).get("role_aliases")
            if isinstance(raw, str):
                aliases = [p.strip().lower() for p in raw.split(",") if p.strip()]
            elif isinstance(raw, (list, tuple)):
                aliases = [str(p).strip().lower() for p in raw if str(p).strip()]
            # console override
            override_aliases = (self._role_aliases_of(project_key, name) or "").split(",")
            for alias in aliases + override_aliases:
                alias = alias.strip()
                if alias and alias not in seen:
                    seen.add(alias)
                    if alias not in by_alias:
                        by_alias[alias] = a
        return by_alias

    @staticmethod
    def _roster_from_indexes(
        by_name: dict[str, dict], by_role: dict[str, dict]
    ) -> list[dict]:
        """Rebuild a stable roster from the legacy indexes.

        `propose_agent` is a long-standing helper whose signature takes indexes and
        optionally the raw roster. The detail resolver needs a roster, so tests/callers
        that omit the fifth arg still resolve literal name/role paths correctly.
        """
        out: list[dict] = []
        seen: set[int] = set()
        for source in (by_name, by_role):
            for agent in source.values():
                marker = id(agent)
                if marker not in seen:
                    seen.add(marker)
                    out.append(agent)
        return out

    def resolve_handoff(self, handoff: dict, project_key: str, agents: list[dict]) -> dict:
        """Shared dispatch-board resolution contract for one handoff.

        The returned dict is safe to expose over JSON: it deliberately omits the raw
        agent record while preserving the status/reason/match metadata operators need
        to understand why a handoff can run, is for a human, or is misconfigured.
        """
        detail = role_alias.resolve_target_detail(
            handoff,
            agents,
            designation_of=lambda name: self._designation_of(project_key, name),
            classify_interactive=self._classify_interactive,
            aliases_of=lambda name: self._role_aliases_of(project_key, name),
        )
        return {
            "agent": detail.get("agent"),
            "status": detail.get("status") or "unresolved",
            "reason_code": detail.get("reason_code") or "unknown",
            "reason": detail.get("reason") or "No dispatch target resolved.",
            "target_type": detail.get("target_type") or "none",
            "target": detail.get("target") or "",
            "matched_on": detail.get("matched_on") or "",
        }

    @staticmethod
    def public_resolution(resolution: dict) -> dict:
        """Public JSON-safe projection of a resolution detail."""
        return {
            "status": resolution.get("status") or "unresolved",
            "reason_code": resolution.get("reason_code") or "unknown",
            "reason": resolution.get("reason") or "No dispatch target resolved.",
            "target_type": resolution.get("target_type") or "none",
            "target": resolution.get("target") or "",
            "matched_on": resolution.get("matched_on") or "",
        }

    def _proposal_from_resolution(self, resolution: dict, project_key: str) -> Optional[dict]:
        """Shape a resolved agent into the proposed-agent payload."""
        agent = resolution.get("agent")
        if agent is None:
            return None
        name = agent.get("name") or ""
        override = self._get_override(project_key, name) or {}
        cfg = self._resolve_config(agent, override)
        caps = agent.get("capabilities") or {}
        return {
            "name": name,
            "display_name": caps.get("display_name") or name,
            "harness": cfg.get("harness"),
            "harness_label": cfg.get("harness_label") or "—",
            "model": cfg.get("model"),
            "matched_on": resolution.get("matched_on") or "",
            "resolution_status": resolution.get("status") or "resolved",
            "resolution_reason_code": resolution.get("reason_code") or "",
            "resolution_reason": resolution.get("reason") or "",
        }

    def propose_agent(
        self,
        handoff: dict,
        by_name: dict[str, dict],
        by_role: dict[str, dict],
        project_key: str,
        agents: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """Rule-based proposal: which roster agent should take this handoff.

        Match precedence (the dispatch routing rule — the SAME ladder the autonomy
        orchestrator's `main._resolve_target_agent` walks via `app.domain.roles`, so
        the proposal can never diverge from what would actually run):
          1. handoff.to_agent (explicit assignee) → exact roster name match
             (then a name-in-role fallback some projects need).
          2. handoff.to_role  (role routing):
             a. a NON-DISPATCHABLE human role (cto / human / operator) → None
                ('unassigned' — left for a person), winning even over a literal match.
             b. first roster agent with that LITERAL role (the existing working path).
             c. a roster-name fallback (many projects file a name in to_role).
             d. a LEAD alias (cpo / co-lead / lead) → the project's INTERACTIVE
                lead (designation-driven via the injected classifier, not a hardcoded
                name) — so a `cpo` handoff proposes the lead instead of 'unassigned'.
        Returns a small dict {name, display_name, harness, harness_label, model,
        matched_on} for the proposed agent, or None when nothing matches (→
        'unassigned').

        `agents` is the raw roster (the lead-alias step needs it to find the
        interactive lead); when omitted, the lead alias simply yields None (the prior
        behaviour) — the literal paths are unaffected.

        harness/model are the agent's EFFECTIVE config (console override wins over
        the registry value) via the INJECTED resolver — so the proposed run uses
        exactly what the operator configured, and the service stays free of the
        concrete `harness` module."""
        roster = agents if agents is not None else self._roster_from_indexes(by_name, by_role)
        resolution = self.resolve_handoff(handoff, project_key, roster)
        return self._proposal_from_resolution(resolution, project_key)

    def dispatch_row(
        self,
        handoff: dict,
        by_name: dict[str, dict],
        by_role: dict[str, dict],
        project_key: str,
        project_id: Optional[str],
        agents: Optional[list[dict]] = None,
    ) -> dict:
        """Shape ONE open handoff + its proposed dispatch into a render-ready row.

        Carries the handoff identity (uuid, summary, from, to-target,
        priority) plus the rule-based proposal (`proposed` is the agent dict or None
        → 'unassigned'). All presentation; the template does no logic. Lifted 1:1
        from `main._dispatch_row` (+ the raw `agents` threaded through so the
        proposal's lead-alias step can find the interactive lead)."""
        hid = handoff.get("id") or ""
        compound = hid
        roster = agents if agents is not None else self._roster_from_indexes(by_name, by_role)
        resolution = self.resolve_handoff(handoff, project_key, roster)
        public_resolution = self.public_resolution(resolution)
        proposed = self._proposal_from_resolution(resolution, project_key)
        to_target = (
            handoff.get("to_agent")
            or (f"role · {handoff.get('to_role')}" if handoff.get("to_role") else None)
            or "—"
        )
        priority = (handoff.get("priority") or "").strip().lower() or "normal"
        return {
            "id": hid,
            "compound": compound,
            "summary": _short(handoff.get("summary") or "", _SUMMARY_CLIP),
            "summary_full": handoff.get("summary") or "",
            "from_agent": handoff.get("from_agent") or "—",
            "to_target": to_target,
            "priority": priority,
            "proposed": proposed,
            "resolution": public_resolution,
            "resolution_status": public_resolution["status"],
            "resolution_reason_code": public_resolution["reason_code"],
            "resolution_reason": public_resolution["reason"],
            "acceptance": handoff.get("acceptance") or {},
            "evidence": handoff.get("evidence") or {},
            "retry": handoff.get("retry") or {},
            "escalation": handoff.get("escalation") or {},
            "created_at": handoff.get("created_at"),
        }

    def dispatch_rows(
        self,
        handoffs: list[dict],
        agents: list[dict],
        project_key: str,
        project_id: Optional[str],
    ) -> list[dict]:
        """Build the sorted board: open handoffs, each with a proposed agent.

        Filters to open/pending handoffs (`is_open`), proposes an agent per row
        (rule-based), and sorts urgent-major (priority weight, then newest-first) so
        the most pressing waiting work leads. Capped at `DISPATCH_MAX`. Lifted 1:1
        from `main._dispatch_rows`."""
        by_name, by_role = self.agent_index(agents)
        rows = [
            self.dispatch_row(h, by_name, by_role, project_key, project_id, agents)
            for h in handoffs
            if is_open(h)
        ]
        # newest-first within a priority band, then priority asc (urgent=0 first) —
        # two stable sorts, exactly as main._dispatch_rows does it.
        rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
        rows.sort(key=lambda r: PRIORITY_ORDER.get(r["priority"], 9))
        return rows[:DISPATCH_MAX]

    # -- the board surface (reads the ports) -----------------------------------

    async def board(
        self,
        project_key: str,
        agents: list[dict],
        *,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """The dispatch BOARD for a project: the sorted open-handoff rows (each with
        a proposed agent), the board counts (total / proposed / unassigned), and the
        project autonomy/propose-mode flags + the awaiting-approval id set.

        Reads the dispatch queue ONCE from the Cortex port (pending handoffs) and the
        two flags from the operational store. `agents` is the roster the caller
        fetched (from Cortex). Never raises — a down Cortex yields an empty queue
        (empty board), a down store yields fail-safe-OFF flags, a missing
        awaiting-approval lister yields an empty parked set."""
        handoffs = await self._get_handoffs()
        rows = self.dispatch_rows(handoffs, agents, project_key, project_id)
        proposed_n = sum(1 for r in rows if r["proposed"])

        autonomous_on = self._is_autonomous(project_key)
        propose_mode_on = self._is_propose_mode(project_key)
        awaiting_ids = set(self._list_awaiting(project_key))

        return {
            "active_view": "dispatch",
            "selected_key": project_key,
            "rows": rows,
            "dispatch_rows": rows,  # alias: the HTML template's context key
            "dispatch_count": len(rows),
            "dispatch_proposed_count": proposed_n,
            "dispatch_unassigned_count": len(rows) - proposed_n,
            "autonomous_on": autonomous_on,
            "propose_mode_on": propose_mode_on,
            "awaiting_approval_ids": awaiting_ids,
        }

    # -- port / injected access (graceful-degrade) -----------------------------

    async def _get_handoffs(self) -> list[dict]:
        """The pending dispatch queue ([] when no Cortex port / a down Cortex)."""
        if self._cortex is None:
            return []
        try:
            return await self._cortex.get_handoffs() or []
        except Exception:  # pragma: no cover - the port degrades; belt-and-braces
            return []

    def _is_autonomous(self, project_key: str) -> bool:
        """The autonomy flag (fail-safe OFF when no store / a down store)."""
        if self._store is None:
            return False
        try:
            return bool(self._store.is_project_autonomous(project_key))
        except Exception:  # pragma: no cover - fail-safe OFF
            return False

    def _is_propose_mode(self, project_key: str) -> bool:
        """The propose-mode flag (fail-safe OFF when no store / a down store)."""
        if self._store is None:
            return False
        try:
            return bool(self._store.is_propose_mode(project_key))
        except Exception:  # pragma: no cover - fail-safe OFF
            return False

    def _list_awaiting(self, project_key: str) -> list:
        """The parked-for-review handoff ids ([] when no lister / it errors)."""
        try:
            return self._awaiting_approval(project_key) or []
        except Exception:  # pragma: no cover - board decoration degrades
            return []


__all__ = [
    "DispatchService",
    "OPEN_STATUSES",
    "DISPATCH_MAX",
    "PRIORITY_ORDER",
    "is_open",
    "normalize_target",
]
