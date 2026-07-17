"""The `settings` feature module — operational settings behind the SDK ports
(Track A, the 3rd carve).

The THIRD feature module carved out of `app/main.py` + `app/settings.py` behind the
SDK ports (analytics was first, agents second; dispatch / runs follow). It owns the
OPERATIONAL settings substance: app/system settings (load/upsert), per-agent config
(get/resolve/save overrides), designation normalise + the one-time seed, and the
project autonomy/propose-mode flags.

PACKAGE NAME NOTE: the package is `app.settings_module`, NOT `app.settings`, because
`app/settings.py` ALREADY EXISTS (the app-DB-backed legacy facade the HTML routes
still use — imported as `settings_store` in `main.py`); a Python package
`app/settings/` cannot coexist with the module file `app/settings.py`. Naming the
carved module `settings_module` keeps the carve strictly additive while
`app.settings` remains the schema and fallback facade.
The import-linter independence contract pins it as `app.settings_module`.

A clean VERTICAL slice, layered like the rest of the SDK (arrows point inward):

  * `service.py` — the config LOGIC (`SettingsService`). Depends ONLY on
    `domain.ports.OperationalStorePort` (the app-DB operational surface —
    `load_app_settings` / `upsert_app_settings` / `load_agent_overrides` /
    `get_agent_override` / `save_agent_override` + the project-flag methods); it
    imports NOTHING outward (no fastapi / httpx / subprocess / psycopg2 / asyncpg)
    and never reaches back into `app.main`, the concrete `app.appdb` /
    `app.adapters`, or the legacy `app.settings` facade. The designation/cleaning/
    seed logic moved 1:1 from `settings.normalize_designation` /
    `settings._clean_override` / `settings._DESIGNATION_SEED` /
    `settings.seed_agent_overrides` + the resolve/save/flag guards from
    `settings.get_agent_designation` / `save_agent_override` /
    `is_project_autonomous` / `is_propose_mode`.
  * `api.py` — the imperative SHELL: a FastAPI `APIRouter` whose
    `GET /settings/{project}/app`, `GET /settings/{project}/agents/{agent}/config`,
    and `GET /settings/{project}/flags` resolve the `OperationalStorePort` from
    `app.state` (via `Depends`), construct the service over it, and return the
    shaped JSON. The only part that imports fastapi.

`main.py` mounts the router additively and the existing HTML System page +
Configure card + the inline agent-config save delegate their config substance to
`SettingsService` — one source of the config logic, multiple surfaces. The save
path writes through the service → port into the canonical app-DB settings store.

The module-isolation arrow is enforced by `.importlinter`'s
`modules-are-independent` (independence) contract: `app.settings_module` may import
the ports/domain but not the other feature modules — the 5th fitness gate fails if
it regresses.
"""

from app.settings_module.api import router
from app.settings_module.service import SettingsService

__all__ = ["SettingsService", "router"]
