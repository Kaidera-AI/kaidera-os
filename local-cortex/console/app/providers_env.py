"""Manifold configuration resolution for the open-source edition."""

from __future__ import annotations

from . import deploy_mode
from . import settings as settings_store

_SETTING_ENV_VAR: dict[str, str] = {
    "kaidera_manifold_api_key": "KAIDERA_MANIFOLD_API_KEY",
    "kaidera_manifold_base_url": "KAIDERA_MANIFOLD_BASE_URL",
    "kaidera_manifold_project_id": "KAIDERA_MANIFOLD_PROJECT_ID",
}
_SETTING_ENV_ALIASES: dict[str, tuple[str, ...]] = {}


def env_vars_for(setting_key: str) -> tuple[str, ...]:
    var = _SETTING_ENV_VAR.get(setting_key)
    return (var,) if var else ()


def env_file_value(var: str) -> str:
    """Read a Manifold setting from the local development .env file."""
    if var not in _SETTING_ENV_VAR.values() or deploy_mode.is_selfcontained():
        return ""
    try:
        text = (settings_store.CONSOLE_DIR.parent / ".env").read_text(encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        return ""
    prefix = f"{var}="
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value.strip()
    return ""


__all__ = ["env_file_value", "env_vars_for"]
