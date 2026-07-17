"""Imperative shell — the ADAPTERS that implement the domain ports (I/O lives here).

Adapters depend on `app/domain/` (the pure functional core), never the reverse
(arrows point inward, ratified design §3). Unlike the domain, modules here MAY
import outward — asyncpg / httpx / subprocess — because they are the boundary that
talks to Postgres, the Cortex API, and the harness CLIs.

Each adapter wraps the EXISTING concrete code over one port (thin, additive — not
a rewrite), inheriting the house graceful-degrade contract (a down dependency
returns empty/None/no-op and never raises into a caller):

  * `runstate_pg.RunStatePgStore`        → `domain.runstate.RunStatePort`  (M1; over the shared `appdb.AppDB` asyncpg pool)
  * `llm_harness.HarnessLLMAdapter`      → `domain.ports.LLMPort`           (over `harness_runner.stream_chat`)
  * `cortex_memory.CortexMemoryAdapter`  → `domain.ports.CortexMemoryPort`  (over `cortex_client.CortexClient`)
  * `opstore.AppDbOperationalStore`      → `domain.ports.OperationalStorePort` (over `appdb.AppDB` + `SettingsDB`)
  * `harness_local.LocalHarnessAdapter`  → `domain.harness.HarnessPort`     (over the existing host-side `subprocess.Popen` worker spawn; I1)
  * `harness_remote.RemoteHarnessAdapter`→ `domain.harness.HarnessPort`     (over `POST /spawn` · `/cancel` to the host harness-service via httpx; I2 — the container→host wire)
"""

from app.adapters.cortex_memory import CortexMemoryAdapter
from app.adapters.harness_local import LocalHarnessAdapter
from app.adapters.harness_remote import RemoteHarnessAdapter
from app.adapters.llm_harness import HarnessLLMAdapter
from app.adapters.opstore import AppDbOperationalStore
from app.adapters.runstate_pg import RunStatePgStore

__all__ = [
    "RunStatePgStore",
    "HarnessLLMAdapter",
    "CortexMemoryAdapter",
    "AppDbOperationalStore",
    "LocalHarnessAdapter",
    "RemoteHarnessAdapter",
]
