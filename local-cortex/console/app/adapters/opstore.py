"""OperationalStorePort adapter — wraps `appdb.AppDB` + `appdb.SettingsDB`.

The imperative-shell adapter (`app/adapters/`) that IMPLEMENTS the pure
`OperationalStorePort` Protocol (`app/domain/ports.py`) over the EXISTING app-DB
accessors. Arrows point inward (ratified design §3): the domain port stays pure;
this adapter is the boundary that talks to the operational (non-Cortex) app-DB.

THIN + ADDITIVE: it does NOT reimplement any SQL or pool logic — it UNIFIES the
two existing accessors behind one port:
  * `appdb.AppDB`       — the async (asyncpg) usage-telemetry + analytics surface.
  * `appdb.SettingsDB`  — the sync (psycopg2) settings + agent-config + project
                          flags surface.
Each method delegates 1:1 to the matching concrete method.

The ONE behaviour the adapter adds is mapping the SettingsDB `UNAVAILABLE`
sentinel (and the flag semantics) to the port's safe defaults — exactly the
fallback `app/settings.py` already does, lifted here so the port surface NEVER
leaks the sentinel:
  * a flag read (autonomy / propose-mode) → `False` on UNAVAILABLE (fail-safe:
    a degraded DB never enables autonomy or blocks a dispatch),
  * a map/list read → `{}` / `[]` on UNAVAILABLE (callers fall back to the JSON
    seed),
The async usage/analytics methods already return empty defaults on a down app-DB
(AppDB swallows failures), so those pass straight through.
"""

from __future__ import annotations

from typing import Any, Optional

from app.appdb import UNAVAILABLE, AppDB, SettingsDB, settings_db


class AppDbOperationalStore:
    """`OperationalStorePort` over `AppDB` (async usage/analytics) + `SettingsDB`
    (sync settings/config/flags). Satisfies the Protocol structurally.

    Defaults bind the module-level shared `settings_db` so production wiring only
    needs to pass the app's `AppDB`; tests inject fakes for both."""

    def __init__(
        self,
        *,
        appdb: AppDB,
        settings_db: SettingsDB = settings_db,
    ) -> None:
        self._appdb = appdb
        self._settings = settings_db

    # -- store liveness (sync — AppDB) -----------------------------------------

    def available(self) -> bool:
        return self._appdb.available()

    # -- usage telemetry + analytics (async — AppDB) ---------------------------

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
        return await self._appdb.record_usage(
            project, agent, harness, model, provider,
            tokens_in, tokens_out, cost_est,
        )

    async def usage_by_model(self, project: str) -> list[dict[str, Any]]:
        return await self._appdb.usage_by_model(project)

    async def usage_by_model_provider(self, project: str) -> list[dict[str, Any]]:
        return await self._appdb.usage_by_model_provider(project)

    async def usage_by_agent(self, project: str) -> list[dict[str, Any]]:
        return await self._appdb.usage_by_agent(project)

    async def usage_by_project(self, project: str) -> dict[str, Any]:
        return await self._appdb.usage_by_project(project)

    # -- console settings (sync — SettingsDB) ----------------------------------

    def load_app_settings(self) -> dict[str, Any]:
        val = self._settings.load_app_settings()
        return val if val is not UNAVAILABLE else {}

    def upsert_app_settings(self, items: dict[str, Any]) -> bool:
        return self._settings.upsert_app_settings(items)

    # -- per-agent config (sync) -----------------------------------------------

    def load_agent_overrides(self) -> dict[str, dict[str, str]]:
        val = self._settings.load_agent_overrides()
        if val is UNAVAILABLE:
            return {}
        return val

    def get_agent_override(self, project: str, agent: str) -> dict[str, str]:
        val = self._settings.get_agent_override(project, agent)
        if val is UNAVAILABLE:
            return {}
        return val

    def save_agent_override(
        self, project: str, agent: str, entry: dict[str, str]
    ) -> bool:
        return self._settings.save_agent_override(project, agent, entry)

    # -- project flags (sync) — autonomy + propose-mode ------------------------

    def is_project_autonomous(self, project: str) -> bool:
        """Fail-safe: UNAVAILABLE → False (a degraded DB never enables autonomy),
        matching `settings.is_project_autonomous`."""
        val = self._settings.get_project_autonomy(project)
        return bool(val) if val is not UNAVAILABLE else False

    def set_project_autonomy(
        self, project: str, enabled: bool, updated_by: Optional[str] = None
    ) -> bool:
        return self._settings.set_project_autonomy(project, enabled, updated_by)

    def list_autonomous_projects(self) -> list[str]:
        val = self._settings.list_autonomous_projects()
        return val if val is not UNAVAILABLE else []

    def is_propose_mode(self, project: str) -> bool:
        """Fail-safe: UNAVAILABLE → False (auto-spawn; a degraded DB never blocks a
        dispatch), matching `settings.is_propose_mode`."""
        val = self._settings.get_project_propose_mode(project)
        return bool(val) if val is not UNAVAILABLE else False

    def set_propose_mode(
        self, project: str, enabled: bool, updated_by: Optional[str] = None
    ) -> bool:
        return self._settings.set_project_propose_mode(project, enabled, updated_by)


__all__ = ["AppDbOperationalStore"]
