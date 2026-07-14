"""Deploy-mode flag — `selfcontained` (the distributable) vs `dev`.

The app ships as a self-contained native console that must run identically on a fresh
Linux VM with **no Mac host**. In `selfcontained` mode the app MUST NOT read host-user
files (`~/.pi`, `~/.claude`, `local-cortex/.env`): auth comes only from the app-DB
settings store + the process env the app itself injects. `dev` keeps local operator
conveniences (the host harness-service bridge, the `~/.pi` key fallback, and the
`local-cortex/.env` file).

Default is **`dev`** for local checkouts. The distributable's `install.sh` (or its
systemd unit / compose) sets `KAIDERA_DEPLOY_MODE=selfcontained` explicitly. This is
the single source of truth for the mode; resolve it here, never by scattering
`os.environ` reads. See `app/deploy_mode.py` consumers and the
`scripts/fitness/check-selfcontained-no-host.sh` gate.
"""

from __future__ import annotations

import os

SELFCONTAINED = "selfcontained"
DEV = "dev"
LEGACY_DEV_MODE = "local" + "dev"

#: The env var that selects the mode. Unset / unrecognised → DEV.
ENV_VAR = "KAIDERA_DEPLOY_MODE"


def deploy_mode() -> str:
    """Return the resolved deploy mode: ``selfcontained`` or ``dev``.

    Anything other than an exact ``selfcontained`` (case-insensitive) resolves to
    ``dev``.
    """
    raw = (os.environ.get(ENV_VAR, "") or "").strip().lower()
    return SELFCONTAINED if raw == SELFCONTAINED else DEV


def is_selfcontained() -> bool:
    """True when the app must behave with ZERO host-user dependency (the distributable)."""
    return deploy_mode() == SELFCONTAINED


def is_dev() -> bool:
    """True when local operator conveniences (host bridge, ~/.pi, .env) are allowed."""
    return deploy_mode() == DEV


__all__ = ["SELFCONTAINED", "DEV", "LEGACY_DEV_MODE", "ENV_VAR", "deploy_mode", "is_selfcontained", "is_dev"]
