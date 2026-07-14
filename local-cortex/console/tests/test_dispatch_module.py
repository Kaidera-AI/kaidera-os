"""Track A step 4 — the fourth feature-module carve: `app/dispatch/`.

The dispatch BOARD/READ feature (the read side of the Dispatch center — list the
open/pending handoffs each with a rule-based proposed agent, the board counts, and
the project autonomy/propose-mode flag reads) is lifted out of `app/main.py`'s blob
into a clean vertical module behind the SDK ports. It follows the PATTERN the
analytics → agents → settings carves established, and the last carve (`runs`) follows
in turn.

SCOPE — READ/BOARD ONLY (like the agents catalog carve). The orchestrator's
IMPERATIVE core stays in `main.py`/`orchestrator.py` for a LATER carve: `_dispatch_run`
/ spawn, `_pm_beat`, the `POST /dispatch/{p}/run` (Approve & Run), the
`POST /dispatch/{p}/autonomous` toggle, the `POST .../approve` gate, and the
orchestrator's live status/feed/wave assembly. This module owns the board's
pure listing + counts + the two flag reads — nothing that spawns or writes.

The module has three parts and these tests pin each:

  * `app/dispatch/service.py` — the board LOGIC (`DispatchService`). It depends ONLY
    on `domain.ports.CortexMemoryPort` (the dispatch queue — `get_handoffs()`) +
    `domain.ports.OperationalStorePort` (the autonomy + propose-mode flag reads),
    plus an INJECTED per-agent config resolver + an INJECTED awaiting-approval lister
    (the analytics/agents callable-injection pattern — so the service stays free of
    the concrete `harness` module and the off-port `list_awaiting_approval`). The
    ROSTER is passed IN by the caller (the agents-carve pattern; the caller fetches
    it from Cortex). It imports NOTHING outward (no fastapi / httpx / subprocess /
    psycopg2 / asyncpg) and never reaches back into `app.main`, the concrete
    `appdb`/`adapters`, or the concrete `harness`/`orchestrator`. The shaping +
    proposal logic moved 1:1 from `main._dispatch_is_open` / `_agent_index` /
    `_normalize_target` / `_proposed_agent` / `_dispatch_row` / `_dispatch_rows` (+
    the board counts/flag reads from `_dispatch_context`). → tested against FAKE
    ports (no DB / no Cortex).

  * `app/dispatch/api.py` — a FastAPI `APIRouter` (the imperative shell — MAY import
    fastapi) whose `GET /dispatch/{project}/board` constructs the service over the
    ports (resolved from `app.state` via `Depends`) and the roster (fetched from
    Cortex) and returns JSON. → tested by driving the route function directly with
    fake ports + a fake roster source (no ASGI / live DB), the same idiom as
    `test_agents_module.py` / `test_dispatch_run_route.py`.

These tests are written BEFORE the implementation (strict TDD) and match the
existing fake-driven, no-DB style (`test_agents_module.py`).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
#  Fake ports — serve scripted handoffs + flags (no DB / no Cortex).
# ---------------------------------------------------------------------------


class FakeCortexMemory:
    """Structural `CortexMemoryPort` stand-in for the dispatch board service.

    Only `get_handoffs` is implemented (the dispatch queue the board lists). It is
    project-BOUND like the real adapter (the caller's project_key is already baked
    in), so it takes only the `status` kwarg. `down` simulates a degraded Cortex
    (returns [] — the house-law graceful degrade)."""

    def __init__(self, *, handoffs=None, down=False):
        self._handoffs = list(handoffs or [])
        self._down = down
        self.calls: list[str] = []

    async def get_handoffs(self, *, status=None):
        self.calls.append(f"get_handoffs:{status or 'pending'}")
        if self._down:
            return []
        return [dict(h) for h in self._handoffs]


class FakeOpStore:
    """Structural `OperationalStorePort` stand-in — serves the two project flags the
    board reads. `down` simulates a degraded app-DB (fail-safe OFF on both)."""

    def __init__(self, *, autonomous=False, propose=False, down=False):
        self._autonomous = autonomous
        self._propose = propose
        self._down = down
        self.calls: list[str] = []

    def is_project_autonomous(self, project: str) -> bool:
        self.calls.append("is_project_autonomous")
        if self._down:
            return False  # fail-safe OFF
        return self._autonomous

    def is_propose_mode(self, project: str) -> bool:
        self.calls.append("is_propose_mode")
        if self._down:
            return False  # fail-safe OFF
        return self._propose

    def get_agent_override(self, project: str, agent: str) -> dict:
        # The board's proposal override reader (the shell binds this when wiring the
        # config resolver). No scripted overrides here → "no override" (the proposal
        # falls back to the registry config via the injected resolver).
        self.calls.append("get_agent_override")
        return {}


# A realistic project roster (the shape Cortex `get_agents` returns) for proposal
# matching: ren (a name target), a role 'qa' agent, and an orchestrator.
SAMPLE_ROSTER = [
    {
        "name": "ren",
        "role": "full-stack-developer",
        "capabilities": {"display_name": "Ren", "harness": "claude-code"},
    },
    {
        "name": "quill",
        "role": "qa",
        "capabilities": {"display_name": "Quill"},
    },
    {
        "name": "cole",
        "role": "orchestrator",
        "capabilities": {"display_name": "Cole"},
    },
]

# A mixed handoff queue:
#   * h-open-1   — open/pending, to_agent=ren (explicit assignee → proposed: ren),
#   * h-open-2   — open/pending, to_role=qa (role routing → proposed: quill), urgent,
#   * h-open-3   — open/pending, to_agent=nobody (no roster match → unassigned),
#   * h-claimed  — claimed_by set (in-flight → NOT on the board),
#   * h-done     — status='completed' (→ NOT on the board).
SAMPLE_HANDOFFS = [
    {
        "id": "h-open-1",
        "status": "pending",
        "summary": "wire the thing",
        "from_agent": "kai",
        "to_agent": "ren",
        "priority": "high",
        "created_at": "2026-06-06T09:00:00",
    },
    {
        "id": "h-open-2",
        "status": "pending",
        "summary": "verify the carve",
        "from_agent": "kai",
        "to_role": "qa",
        "priority": "urgent",
        "created_at": "2026-06-06T10:00:00",
    },
    {
        "id": "h-open-3",
        "status": "pending",
        "summary": "do the needful",
        "from_agent": "kai",
        "to_agent": "nobody",
        "priority": "low",
        "created_at": "2026-06-06T08:00:00",
    },
    {
        "id": "h-claimed",
        "status": "claimed",
        "summary": "already running",
        "from_agent": "kai",
        "to_agent": "ren",
        "claimed_by": "ren",
        "created_at": "2026-06-06T07:00:00",
    },
    {
        "id": "h-done",
        "status": "completed",
        "summary": "finished work",
        "from_agent": "kai",
        "to_agent": "ren",
        "created_at": "2026-06-06T06:00:00",
    },
]


def _stub_resolve_config(agent: dict, override: dict) -> dict:
    """A per-agent config resolver stand-in (the real one wraps
    `harness._registry_config` + `canonical_harness` + `harness_label`). Returns the
    fields the proposed-agent dict needs. Proves the service uses the INJECTED
    resolver (so it stays free of the concrete `harness`)."""
    caps = agent.get("capabilities") or {}
    harness = override.get("harness") or caps.get("harness") or "claude-code"
    return {
        "harness": harness,
        "harness_label": "Claude Code" if harness == "claude-code" else harness,
        "model": override.get("model") or "claude-opus-4-8[1m]",
    }


# ---------------------------------------------------------------------------
#  service.py — the board/listing logic moved out of main.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_lists_open_handoffs_with_proposals():
    """`DispatchService.board` lists ONLY open/pending handoffs (claimed + completed
    are excluded), each with a rule-based proposed agent, sorted urgent-first — the
    board substance lifted from `main._dispatch_rows` / `_dispatch_context`."""
    from app.dispatch.service import DispatchService

    cortex = FakeCortexMemory(handoffs=SAMPLE_HANDOFFS)
    store = FakeOpStore(autonomous=True, propose=False)
    svc = DispatchService(
        cortex=cortex, store=store, resolve_config=_stub_resolve_config
    )
    board = await svc.board("kaidera-os", SAMPLE_ROSTER)

    rows = board["rows"]
    ids = [r["id"] for r in rows]
    # only the three open handoffs (claimed + completed are filtered out)
    assert set(ids) == {"h-open-1", "h-open-2", "h-open-3"}
    # urgent leads (priority weight), so h-open-2 (urgent) is first
    assert rows[0]["id"] == "h-open-2"

    by_id = {r["id"]: r for r in rows}
    # explicit assignee → proposed ren (matched_on agent)
    assert by_id["h-open-1"]["proposed"]["name"] == "ren"
    assert by_id["h-open-1"]["proposed"]["matched_on"] == "agent"
    # the proposal's harness comes from the INJECTED resolver
    assert by_id["h-open-1"]["proposed"]["harness"] == "claude-code"
    assert by_id["h-open-1"]["proposed"]["harness_label"] == "Claude Code"
    # role routing → proposed quill (matched_on role)
    assert by_id["h-open-2"]["proposed"]["name"] == "quill"
    assert by_id["h-open-2"]["proposed"]["matched_on"] == "role"
    # no roster match → unassigned (proposed is None)
    assert by_id["h-open-3"]["proposed"] is None
    assert by_id["h-open-3"]["resolution_status"] == "unresolved"
    assert by_id["h-open-3"]["resolution_reason_code"] == "unknown_agent"
    assert "not in this project roster" in by_id["h-open-3"]["resolution_reason"]
    assert by_id["h-open-1"]["compound"] == "h-open-1"

    # it actually pulled the dispatch queue from the Cortex port
    assert any(c.startswith("get_handoffs") for c in cortex.calls)


@pytest.mark.asyncio
async def test_board_counts_and_flags():
    """The board carries the dispatch counts (total / proposed / unassigned) and the
    autonomy + propose-mode flag reads (via the OperationalStorePort) — the
    `dispatch_count` / `dispatch_proposed_count` / `dispatch_unassigned_count` +
    `autonomous_on` / `propose_mode_on` surface lifted from `main._dispatch_context`."""
    from app.dispatch.service import DispatchService

    cortex = FakeCortexMemory(handoffs=SAMPLE_HANDOFFS)
    store = FakeOpStore(autonomous=True, propose=True)
    svc = DispatchService(
        cortex=cortex,
        store=store,
        resolve_config=_stub_resolve_config,
        awaiting_approval=lambda project: ["h-open-2"],
    )
    board = await svc.board("kaidera-os", SAMPLE_ROSTER)

    # three open rows; two have a proposed agent (ren, quill); one unassigned
    assert board["dispatch_count"] == 3
    assert board["dispatch_proposed_count"] == 2
    assert board["dispatch_unassigned_count"] == 1

    # the flags reflect the store
    assert board["autonomous_on"] is True
    assert board["propose_mode_on"] is True
    # the injected awaiting-approval lister drives the parked-for-review set
    assert board["awaiting_approval_ids"] == {"h-open-2"}

    # both flag reads went through the port
    assert "is_project_autonomous" in store.calls
    assert "is_propose_mode" in store.calls


@pytest.mark.asyncio
async def test_board_graceful_when_cortex_down():
    """A down Cortex yields an empty queue → empty board (no rows, zero counts),
    never raises — the house law. The flags still read from the store."""
    from app.dispatch.service import DispatchService

    cortex = FakeCortexMemory(down=True)
    store = FakeOpStore(autonomous=True, propose=False)
    svc = DispatchService(
        cortex=cortex, store=store, resolve_config=_stub_resolve_config
    )
    board = await svc.board("kaidera-os", SAMPLE_ROSTER)

    assert board["rows"] == []
    assert board["dispatch_count"] == 0
    assert board["dispatch_proposed_count"] == 0
    assert board["dispatch_unassigned_count"] == 0
    # the autonomy flag still resolves (store is up)
    assert board["autonomous_on"] is True


@pytest.mark.asyncio
async def test_board_graceful_when_store_down():
    """A down operational store reads BOTH flags fail-safe OFF (an outage can only
    turn a flag off, never on), never raises — the fail-safe house law. The queue
    still lists (Cortex is up)."""
    from app.dispatch.service import DispatchService

    cortex = FakeCortexMemory(handoffs=SAMPLE_HANDOFFS)
    store = FakeOpStore(down=True)
    svc = DispatchService(
        cortex=cortex, store=store, resolve_config=_stub_resolve_config
    )
    board = await svc.board("kaidera-os", SAMPLE_ROSTER)

    # the queue still renders
    assert board["dispatch_count"] == 3
    # both flags fail-safe OFF
    assert board["autonomous_on"] is False
    assert board["propose_mode_on"] is False
    # no awaiting-approval lister injected → empty set (board decoration degrades)
    assert board["awaiting_approval_ids"] == set()


def test_dispatch_is_open_pure():
    """The open/pending predicate lifted from `main._dispatch_is_open`: a claimed
    handoff is in-flight (not open) even with a stale status; a completed handoff is
    not open; a pending/blank-status unclaimed handoff is open."""
    from app.dispatch import service as dispatch_service

    assert dispatch_service.is_open({"status": "pending"}) is True
    assert dispatch_service.is_open({"status": ""}) is True  # blank → open
    assert dispatch_service.is_open({"status": "completed"}) is False
    assert dispatch_service.is_open({"status": "claimed"}) is False
    # claimed_by wins even if the status string is stale-open
    assert dispatch_service.is_open({"status": "pending", "claimed_by": "ren"}) is False


def test_propose_agent_match_precedence_pure():
    """The rule-based proposer lifted from `main._proposed_agent`: to_agent (explicit
    assignee) wins over to_role; malformed colon targets do not silently normalize;
    nothing-matches → None. Uses the pure `propose_agent` helper with the
    injected resolver (so it stays free of the concrete `harness`)."""
    from app.dispatch.service import DispatchService

    svc = DispatchService(resolve_config=_stub_resolve_config)
    by_name, by_role = svc.agent_index(SAMPLE_ROSTER)

    # explicit assignee → exact roster name
    p = svc.propose_agent({"to_agent": "ren"}, by_name, by_role, "kaidera-os")
    assert p["name"] == "ren" and p["matched_on"] == "agent"

    # malformed colon identity remains unmatched so audit can catch it.
    assert svc.propose_agent({"to_agent": "ren:abcd"}, by_name, by_role, "kaidera-os") is None

    # role routing → first roster agent with that role
    p = svc.propose_agent({"to_role": "qa"}, by_name, by_role, "kaidera-os")
    assert p["name"] == "quill" and p["matched_on"] == "role"

    # to_agent wins over to_role when both are present
    p = svc.propose_agent(
        {"to_agent": "ren", "to_role": "qa"}, by_name, by_role, "kaidera-os"
    )
    assert p["name"] == "ren"

    # nothing in the roster matches → None (→ 'unassigned')
    assert svc.propose_agent(
        {"to_agent": "nobody"}, by_name, by_role, "kaidera-os"
    ) is None


def test_propose_agent_role_alias_layer():
    """The role-ALIAS layer (shared with the orchestrator via `app.domain.roles`): a
    `cpo`/lead to_role with no literal-role match proposes the INTERACTIVE lead; a
    `cto`/human to_role is left unassigned (None) even if an agent holds that role; a
    real role keeps its literal match. The raw roster is passed (5th arg) so the lead
    finder can run — exactly as `dispatch_row` calls it."""
    from app.dispatch.service import DispatchService

    # ren has a GENERIC dev role here and is interactive ONLY via her designation
    # override — so a `cpo` alias can only reach her designation-first, never a
    # literal role match. The injected designation reader returns 'interactive' for
    # ren; the classifier defaults to the domain heuristic (override-first).
    roster = [
        {"name": "ren", "role": "full-stack-developer",
         "capabilities": {"display_name": "Ren"}},
        {"name": "bob", "role": "full-stack-developer",
         "capabilities": {"display_name": "Bob"}},
        {"name": "ctobot", "role": "cto", "capabilities": {"display_name": "CtoBot"}},
    ]
    svc = DispatchService(
        resolve_config=_stub_resolve_config,
        designation_of=lambda project, agent: "interactive" if agent == "ren" else "",
    )
    by_name, by_role = svc.agent_index(roster)

    # cpo → the interactive lead (ren), via the alias (no literal 'cpo' role here).
    p = svc.propose_agent({"to_role": "cpo"}, by_name, by_role, "kaidera-os", roster)
    assert p is not None and p["name"] == "ren" and p["matched_on"] == "role"

    # cto → None (the human) even though 'ctobot' literally carries role 'cto'.
    assert svc.propose_agent(
        {"to_role": "cto"}, by_name, by_role, "kaidera-os", roster
    ) is None
    resolution = svc.resolve_handoff({"to_role": "cto"}, "kaidera-os", roster)
    assert resolution["status"] == "blocked"
    assert resolution["reason_code"] == "human_target"

    # a real role still resolves to its literal-role agent (ren is first).
    p = svc.propose_agent(
        {"to_role": "full-stack-developer"}, by_name, by_role, "kaidera-os", roster
    )
    assert p is not None and p["name"] == "ren"

    # an unknown cross-project role → None.
    assert svc.propose_agent(
        {"to_role": "alpha"}, by_name, by_role, "kaidera-os", roster
    ) is None
    resolution = svc.resolve_handoff({"to_role": "alpha"}, "kaidera-os", roster)
    assert resolution["status"] == "unresolved"
    assert resolution["reason_code"] == "unknown_role"


def test_dispatch_row_carries_public_resolution_metadata():
    """Every row carries a JSON-safe routing reason so an unassigned handoff is
    inspectable rather than a silent `proposed: null`."""
    from app.dispatch.service import DispatchService

    roster = [
        {"name": "bob", "role": "full-stack-developer", "capabilities": {}},
    ]
    svc = DispatchService(resolve_config=_stub_resolve_config)
    by_name, by_role = svc.agent_index(roster)

    row = svc.dispatch_row(
        {
            "id": "h-human",
            "status": "pending",
            "summary": "needs owner input",
            "from_agent": "bob",
            "to_role": "cto",
            "acceptance": {"criteria": ["operator confirms"]},
            "evidence": {"required": ["summary"]},
            "retry": {"max_attempts": 0},
            "escalation": {"to_role": "owner"},
        },
        by_name,
        by_role,
        "kaidera-os",
        None,
        roster,
    )

    assert row["proposed"] is None
    assert row["resolution"] == {
        "status": "blocked",
        "reason_code": "human_target",
        "reason": row["resolution_reason"],
        "target_type": "role",
        "target": "cto",
        "matched_on": "",
    }
    assert "will not auto-dispatch" in row["resolution_reason"]
    assert row["acceptance"] == {"criteria": ["operator confirms"]}
    assert row["evidence"] == {"required": ["summary"]}
    assert row["retry"] == {"max_attempts": 0}
    assert row["escalation"] == {"to_role": "owner"}


def test_service_depends_only_on_ports_not_outward():
    """GUARD: `app/dispatch/service.py` imports NOTHING outward (no fastapi / httpx /
    subprocess / psycopg2 / asyncpg) and does NOT reach for `app.main`, the concrete
    `app.appdb` / `app.adapters`, the concrete `app.harness` / `app.providers`, or
    the `app.orchestrator` imperative core — only the domain ports (+ the injected
    callables).

    Parsed via `ast` (a name in a comment/docstring can't fool it), mirroring
    `test_ports_purity.py` / the agents guard. This is the module-isolation rule the
    `.importlinter` independence contract also enforces at the graph level."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "app" / "dispatch" / "service.py"
    ).read_text()
    tree = ast.parse(src)
    top: set[str] = set()
    dotted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top.add(a.name.split(".")[0])
                dotted.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top.add(node.module.split(".")[0])
                dotted.add(node.module)

    forbidden = {"fastapi", "starlette", "httpx", "subprocess", "psycopg2", "asyncpg"}
    assert not (top & forbidden), (
        f"service.py must not import outward I/O libs, got: {sorted(top & forbidden)}"
    )
    # No reaching back into the blob, the concrete adapters/db, the concrete
    # harness/providers, or the orchestrator imperative core.
    assert "app.main" not in dotted, "service.py must not import app.main"
    assert not any(
        m == "app.appdb"
        or m == "app.harness"
        or m == "app.providers"
        or m == "app.orchestrator"
        or m.startswith("app.adapters")
        for m in dotted
    ), "service.py must depend on the domain ports + injected callables, not concretes"


# ---------------------------------------------------------------------------
#  api.py — the FastAPI router (imperative shell; builds svc over the ports)
# ---------------------------------------------------------------------------


class FakeRosterSource:
    """A stand-in for the Cortex roster source the route uses (the real route
    fetches it from `cortex.get_agents`). Returns a scripted roster."""

    def __init__(self, roster):
        self._roster = roster

    async def get_agents(self, project_key):
        return list(self._roster)


@pytest.mark.asyncio
async def test_router_board_endpoint_returns_board():
    """Driving the `GET /dispatch/{project}/board` handler directly returns the
    service's board (rows + counts + flags), no ASGI / live DB — fake ports + fake
    roster source."""
    from app.dispatch import api as dispatch_api

    cortex = FakeCortexMemory(handoffs=SAMPLE_HANDOFFS)
    store = FakeOpStore(autonomous=True, propose=True)
    roster = FakeRosterSource(SAMPLE_ROSTER)
    result = await dispatch_api.board_endpoint(
        "kaidera-os", cortex=cortex, store=store, roster=roster
    )

    assert result["project"] == "kaidera-os"
    assert result["dispatch_count"] == 3
    assert {r["id"] for r in result["rows"]} == {"h-open-1", "h-open-2", "h-open-3"}
    assert result["autonomous_on"] is True
    assert result["propose_mode_on"] is True


def test_router_board_path_does_not_collide():
    """The board JSON route lives at `GET /dispatch/{project}/board` — a distinct
    `/board` leaf AND a GET — so mounting the router additively can NEVER shadow the
    existing POST dispatch routes (`POST /dispatch/{p}/autonomous`, `POST
    /dispatch/{p}/run`) nor the `GET /stream` SSE proxy (a different root). The
    strictly-additive carve constraint."""
    from app.dispatch.api import router

    routes = [(sorted(r.methods or []), r.path) for r in router.routes]
    paths = {p for _, p in routes}
    # the board path carries the distinct /board leaf …
    assert "/dispatch/{project}/board" in paths
    # … and this router claims NEITHER of the live two-segment POST paths.
    assert "/dispatch/{project_key}/autonomous" not in paths
    assert "/dispatch/{project_key}/run" not in paths
    assert "/stream" not in paths
    # the board route is GET-only (a query surface — never a mutation)
    for methods, path in routes:
        if path == "/dispatch/{project}/board":
            assert methods == ["GET"]


def test_router_is_apirouter_with_routes():
    """`app.dispatch.api.router` is a FastAPI APIRouter exposing the board path under
    the module's prefix (so `main` can `include_router` it additively)."""
    from fastapi import APIRouter

    from app.dispatch.api import router

    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    assert "/dispatch/{project}/board" in paths


def test_module_exports_service_and_router():
    """`app.dispatch` re-exports the service + router (the module's public face)."""
    import app.dispatch as dispatch

    assert hasattr(dispatch, "DispatchService")
    assert hasattr(dispatch, "router")
