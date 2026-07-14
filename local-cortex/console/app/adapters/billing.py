"""BillingPort adapter — the usage/cost writer (stub over the usage path).

The imperative-shell adapter (`app/adapters/`) that IMPLEMENTS the pure
`BillingPort` Protocol (`app/domain/ports.py`). Arrows point inward (ratified
design §3): the domain port stays pure; this adapter is the boundary that records
billable usage.

THIN + ADDITIVE — A STUB FOR NOW: per the ratified design, `BillingPort` is "a
usage/cost writer (stub now → metering service later)". Today it is a thin façade
over the SAME `appdb.AppDB.record_usage` usage path the routes already use — it
writes one `usage_events` row. The `run_id` is accepted (so a future metering
backend can correlate per run) but the current app-DB schema keys usage by
project/agent/run-attributes, not run_id, so it is carried for the interface and
not yet persisted — the seam where a real metering/billing service plugs in LATER
without touching callers.

GRACEFUL-DEGRADE (house law): `AppDB.record_usage` already swallows DB failures
and returns False; this adapter additionally guards against ANY unexpected error
from a swapped-in backend so a billing write can NEVER raise into a run path.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("console.billing")


class OperationalStoreBilling:
    """`BillingPort` over the app-DB usage path (stub). Constructed with the
    object that exposes `record_usage(...)` (the app's `AppDB`, or the
    `OperationalStorePort` adapter). Satisfies the `BillingPort` Protocol
    structurally.

    The constructor accepts the writer positionally OR as `appdb=` for clarity at
    the call site."""

    def __init__(self, appdb: Any) -> None:
        self._appdb = appdb

    async def record_usage(
        self,
        *,
        run_id: str,
        tokens_in: Optional[int],
        tokens_out: Optional[int],
        cost: Optional[float],
        project: Optional[str] = None,
        agent: Optional[str] = None,
        harness: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> bool:
        """Record one run's usage/cost via the underlying `record_usage` usage
        path. `run_id` is accepted for a future metering backend (not persisted by
        the current schema). Never raises — a down/failed backend returns False so
        billing can't break the run."""
        try:
            return await self._appdb.record_usage(
                project=project,
                agent=agent,
                harness=harness,
                model=model,
                provider=provider,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_est=cost,
            )
        except Exception as exc:  # never raise into a run path
            log.warning("billing record_usage failed (ignored, run proceeds): %s", exc)
            return False


__all__ = ["OperationalStoreBilling"]
