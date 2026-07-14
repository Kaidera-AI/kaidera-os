"""CortexMemoryPort adapter — wraps `cortex_client.CortexClient`.

The imperative-shell adapter (`app/adapters/`) that IMPLEMENTS the pure
`CortexMemoryPort` Protocol (`app/domain/ports.py`) over the EXISTING async
`CortexClient`. Arrows point inward (ratified design §3): the domain port stays
pure; this adapter is the boundary that talks to the Cortex HTTP API.

THIN + ADDITIVE: it does NOT reimplement any HTTP / graceful-degrade logic — every
method delegates 1:1 to the matching `CortexClient` method (which already returns
[]/{}/None/False on a down Cortex, never raising). The adapter's only jobs are:

  * BIND the project_key — the port is project-agnostic (the console is one
    low-privilege reader per project), so the adapter injects the bound project
    into the underlying scoped calls; callers pass only the operation args.
  * MAP the boot probe — `CortexClient` has no `/boot`; the console uses the
    (always-present) health surface as its cheap liveness boot, so `boot()`
    delegates to `get_health()`.
  * DEGRADE `log` to a safe no-op — the read-only console `CortexClient` exposes
    no write-log method (it never mutates Cortex beyond the narrow claim/complete
    dispatch lifecycle), so `log(...)` is a no-op here. A write-capable adapter
    (platform) would POST /log; keeping `log` on the port means callers can be
    written against the abstraction today and gain real logging by swapping the
    adapter — never silently bypassing it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.cortex_client import CortexClient

log = logging.getLogger("console.cortex_memory")


class CortexMemoryAdapter:
    """`CortexMemoryPort` over a `CortexClient`, bound to one project_key (thin
    pass-through). Satisfies the `CortexMemoryPort` Protocol structurally."""

    def __init__(self, client: CortexClient, *, project_key: str) -> None:
        self._client = client
        self._project_key = project_key

    async def boot(self) -> dict[str, Any]:
        """Cheap liveness boot for the bound project — delegates to the always-
        present health surface (the read-only console's session-start probe)."""
        return await self._client.get_health()

    async def search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        return await self._client.search(self._project_key, query, limit=limit)

    async def get_handoffs(
        self, *, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        return await self._client.get_handoffs(self._project_key, status=status)

    async def claim_handoff(self, handoff_id: str, agent: str) -> bool:
        return await self._client.claim_handoff(self._project_key, handoff_id, agent)

    async def complete_handoff(self, handoff_id: str, agent: str = "") -> bool:
        return await self._client.complete_handoff(self._project_key, handoff_id, agent)

    async def get_history(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return await self._client.get_history(self._project_key, limit=limit)

    async def log(self, agent: str, event_type: str, summary: str) -> None:
        """No-op on the read-only console (it never writes Cortex logs). A write-
        capable adapter (platform) overrides this with a POST /log. Best-effort —
        never raises; the call is silently dropped so a caller written against the
        port still runs against the read-only surface."""
        return None


__all__ = ["CortexMemoryAdapter"]
