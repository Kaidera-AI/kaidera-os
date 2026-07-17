"""SDK ports — the three core abstractions the console depends on (PURE).

This is the functional core for the SDK's lego sockets (Track A, ratified design
§3). It defines `Protocol` interfaces for the three ports that sit
alongside the Milestone-1 `RunStatePort` (`app/domain/runstate.py`):

  * `LLMPort`             — the harness-agnostic chat/dispatch event stream.
  * `CortexMemoryPort`    — the Cortex memory ops the console uses (read + the
                            narrow claim/complete dispatch lifecycle).
  * `OperationalStorePort`— the app-DB operational surface (usage telemetry +
                            analytics + settings + agent-config + project flags).

It is deliberately PURE: it imports ONLY the standard library — NO httpx /
fastapi / subprocess / psycopg2 / asyncpg. Arrows point inward: the adapters in
`app/adapters/` IMPLEMENT these Protocols over the existing concrete code
(`harness_runner`, `cortex_client`, `appdb`); every
caller depends on the Protocol, never the concrete. A guard test
(`tests/test_ports_purity.py`) asserts the import purity, exactly like the
RunStatePort guard.

Why Protocols (structural typing): the existing concrete objects and the thin
adapters can be swapped behind FastAPI `Depends` with near-zero call-site churn,
and a local adapter (subprocess harness, loopback Cortex, app-DB) can be replaced
by a platform adapter (remote harness-service, platform Cortex, metering service)
with no change to the modules that depend on the port. Each port is
`runtime_checkable` so an adapter/stub can be structurally verified in tests.

The method signatures are lifted 1:1 from the concrete code so the wrappers
implement them faithfully:
  * `LLMPort.stream`            ← `harness_runner.stream_chat`
  * `CortexMemoryPort.*`        ← `cortex_client.CortexClient` public methods
  * `OperationalStorePort.*`    ← `appdb.AppDB` + `appdb.SettingsDB` public surface
"""

from __future__ import annotations

from typing import (
    Any,
    AsyncIterator,
    Optional,
    Protocol,
    runtime_checkable,
)

# ---------------------------------------------------------------------------
#  LLMPort — the harness-agnostic event stream (← harness_runner.stream_chat)
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMPort(Protocol):
    """Drive a chat/dispatch turn on the agent's configured harness and yield its
    streamed reply events.

    This is the proto-port the ratified design names: `harness_runner.stream_chat`
    already yields a harness-AGNOSTIC typed event stream (session / delta /
    thinking / tool / result / error / done) and always terminates with one
    `done`, so every harness lane (claude-code | codex | pi |
    graceful) is uniform behind this surface. The local adapter spawns a
    subprocess; a platform adapter could call a remote harness-service — the
    caller never changes.

    `stream` is an async generator: each item is one event dict with a `type`
    key (see `harness_runner` EVENTS)."""

    async def stream(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        harness: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the harness's reply events for `prompt`. `model` / `system` /
        `harness` / `reasoning` are the per-agent routing the composer/dispatch
        resolved. The `reasoning` level is forwarded to the harness (it must not
        be dropped — pi/claude map it to their thinking-effort levels).

        Declared as an async generator (the `yield` below makes it one, and makes
        the Protocol member `isasyncgenfunction`); it yields event dicts. The bare
        `yield` is unreachable scaffolding — implementations override the whole
        method — but it pins the structural shape of the port (mirrors
        `RunStatePort.subscribe`)."""
        if False:  # pragma: no cover - shape-only; implementations override this
            yield {}


# ---------------------------------------------------------------------------
#  CortexMemoryPort — the Cortex memory ops the console uses (← CortexClient)
# ---------------------------------------------------------------------------


@runtime_checkable
class CortexMemoryPort(Protocol):
    """The Cortex memory + coordination operations the console actually calls.

    Lifted from `cortex_client.CortexClient`'s public surface — the reads the
    views use (boot/search/handoffs/history) plus the narrow MUTATING dispatch
    lifecycle (`claim_handoff` / `complete_handoff`) that the autonomous loop +
    "Approve & Run" drive. `log` is on the surface because callers log
    decisions/lessons; the read-only console adapter degrades it to a safe no-op
    (a platform adapter would POST /log).

    The port is project-AGNOSTIC: the adapter binds the project_key (the console
    acts as one low-privilege reader per project), so callers pass only the
    operation args. Implementations graceful-degrade (a down Cortex returns
    empty/None/False, never raises) — the console's read-only invariant + the
    house law."""

    async def boot(self) -> dict[str, Any]:
        """Session-start context for the bound project (health/identity probe).
        The console uses the health surface as its cheap liveness boot; a richer
        adapter can return the full `/boot` payload."""
        ...

    async def search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """GET /search scoped to the bound project — decisions/lessons/graph mix
        ([] on error / blank query)."""
        ...

    async def get_handoffs(
        self, *, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """GET /handoffs scoped to the bound project. No status → PENDING (the
        dispatch queue); `status="claimed"` → the in-flight set the watchdog
        supervises. [] on error."""
        ...

    async def claim_handoff(self, handoff_id: str, agent: str) -> bool:
        """POST /handoffs/{id}/claim AS `agent` (the orchestrator's idempotency
        primitive). True iff the claim succeeded; False on any error (skip)."""
        ...

    async def complete_handoff(self, handoff_id: str, agent: str = "") -> bool:
        """PUT /handoffs/{id}/complete — close a handoff once its run succeeded.
        True on 200; False on any error (the watchdog re-completes a silent
        miss)."""
        ...

    async def get_history(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """GET /history scoped to the bound project — recent messages / the agent
        activity feed ([] on error)."""
        ...

    async def log(
        self,
        agent: str,
        event_type: str,
        summary: str,
    ) -> None:
        """Log a decision / lesson to Cortex. The read-only console adapter is a
        safe no-op (it never mutates Cortex); a write-capable adapter POSTs /log.
        Best-effort: never raises into a caller."""
        ...


# ---------------------------------------------------------------------------
#  OperationalStorePort — the app-DB operational surface (← AppDB + SettingsDB)
# ---------------------------------------------------------------------------


@runtime_checkable
class OperationalStorePort(Protocol):
    """The operational (NON-Cortex) store: usage telemetry + analytics, console
    settings, per-agent config, and the project autonomy/propose-mode flags.

    Lifted from `appdb.AppDB` (async usage/analytics) + `appdb.SettingsDB` (sync
    settings/agent-config/flags). This unifies the two app-DB accessors behind one
    port so the modules (`settings`, `analytics`, `dispatch`) depend on the
    abstraction, not the concrete psycopg2/asyncpg objects. Implementations
    graceful-degrade EXACTLY like the underlying: a down app-DB makes a write
    return False and a read return its empty default (the adapter maps the
    SettingsDB `UNAVAILABLE` sentinel to a safe default so this surface never
    leaks it).

    The async methods (usage/analytics) are over the asyncpg pool; the settings/
    config/flag methods are sync (the SettingsDB is psycopg2, called from sync
    code paths) — kept sync here so the wrapper is a 1:1 pass-through for this
    additive step rather than forcing an async restructure of every caller."""

    # -- store liveness (sync — cheap, non-blocking) ---------------------------

    def available(self) -> bool:
        """Best-effort store-liveness flag for the UI: True once a connect has
        succeeded (cheap + non-blocking). The analytics view reads this to choose
        the 'connected' vs 'usage store not connected' state."""
        ...

    # -- usage telemetry + analytics (async — AppDB / asyncpg) -----------------

    async def record_usage(
        self,
        *,
        project: Optional[str],
        agent: Optional[str],
        harness: Optional[str],
        model: Optional[str],
        provider: Optional[str],
        tokens_in: Optional[int],
        tokens_out: Optional[int],
        cost_est: Optional[float],
    ) -> bool:
        """Insert ONE usage_events row. True on a successful write, False when the
        app-DB is down / the write failed (never raises — telemetry can't break a
        run)."""
        ...

    async def usage_by_model(self, project: str) -> list[dict[str, Any]]:
        """Per-model usage rollup for a project ([] when down)."""
        ...

    async def usage_by_model_provider(self, project: str) -> list[dict[str, Any]]:
        """Per model×provider usage rollup for a project ([] when down)."""
        ...

    async def usage_by_agent(self, project: str) -> list[dict[str, Any]]:
        """Per-agent usage rollup for a project ([] when down)."""
        ...

    async def usage_by_project(self, project: str) -> dict[str, Any]:
        """Project-wide usage totals (zeroed-but-present when down)."""
        ...

    # -- console settings (sync — SettingsDB / psycopg2) -----------------------

    def load_app_settings(self) -> dict[str, Any]:
        """The whole app_settings map {key: value}. {} when the DB can't answer
        (the adapter maps UNAVAILABLE → {} so callers fall back to the JSON
        seed)."""
        ...

    def upsert_app_settings(self, items: dict[str, Any]) -> bool:
        """Upsert many app_settings key→value rows. True on success, False when
        down."""
        ...

    # -- per-agent config (sync) -----------------------------------------------

    def load_agent_overrides(self) -> dict[str, dict[str, str]]:
        """All per-agent overrides as {"{project}:{agent}": {field: str}}. {} when
        the DB can't answer."""
        ...

    def get_agent_override(self, project: str, agent: str) -> dict[str, str]:
        """One agent's override dict ({} if no row / all-NULL / DB down)."""
        ...

    def save_agent_override(
        self, project: str, agent: str, entry: dict[str, str]
    ) -> bool:
        """Persist one agent's COMPLETE override row (UPSERT; an empty entry
        DELETEs). True on success, False when down."""
        ...

    # -- project flags (sync) — autonomy + propose-mode kill-switches ----------

    def is_project_autonomous(self, project: str) -> bool:
        """Whether autonomous dispatch is ON for a project. False (ship-dark) when
        no row OR the DB is down — autonomy is never accidentally enabled."""
        ...

    def set_project_autonomy(
        self, project: str, enabled: bool, updated_by: Optional[str] = None
    ) -> bool:
        """Flip the autonomous-dispatch switch (UPSERT). True on success, False
        when down."""
        ...

    def list_autonomous_projects(self) -> list[str]:
        """The set of projects with autonomy ON. [] (idle) when the DB is down."""
        ...

    def is_propose_mode(self, project: str) -> bool:
        """Whether propose-mode (training-wheels approval gate) is ON for a
        project. False (auto-spawn) when no row OR the DB is down."""
        ...

    def set_propose_mode(
        self, project: str, enabled: bool, updated_by: Optional[str] = None
    ) -> bool:
        """Flip the propose-mode gate (UPSERT). True on success, False when
        down."""
        ...


__all__ = [
    "LLMPort",
    "CortexMemoryPort",
    "OperationalStorePort",
]
