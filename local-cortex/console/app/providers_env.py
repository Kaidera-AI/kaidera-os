"""Shared provider env/auth resolution — the ONE copy of the low-level helpers.

This is the single source of truth for the four tiny "where does a provider key /
how do I reach the harness-service" helpers that `app/providers.py` (the catalog
data layer) and `app/provider_check.py` (the key-connectivity probe) used to
re-implement side-by-side. Before this module a change to HOW the host reads
provider auth (e.g. the self-contained gate, a new env-var alias, the harness
host/port/token wiring) had to be made in N places and could drift between them.

What lives here (canonical, behaviour-preserving):
  * `harness_base_url()` / `harness_headers()` — the host harness-service base URL
    and Bearer headers (the PI model-bridge + explain calls go through these).
  * `env_file_value(var)` — the deploy-mode-gated, read-only `local-cortex/.env`
    reader (the deepest credential fallback; self-contained mode short-circuits).
  * `env_vars_for(setting_key)` — the settings-key -> real env-var-name resolver,
    including the OLLAMA_API_KEY alias.
  * `pi_auth_file()` / `pi_auth_api_key(provider)` — the read-only `~/.pi` auth.json
    fallback (also self-contained-gated).

DEPENDENCY SHAPE: a leaf — it imports only stdlib + `app.deploy_mode` +
`app.settings` (exactly what the originals reached for). The two consumer modules
keep their existing module-private names (`_env_file_value`, `_pi_auth_api_key`,
`_env_vars_for_setting` / `_env_vars_for_field`, `_harness_base_url`,
`_harness_headers`) as thin wrappers that delegate here — so those names stay
patchable in tests AND every internal call site is unchanged.

SELF-CONTAINED MODE (`app/deploy_mode.py`): the host-file fallbacks
(`env_file_value`, `pi_auth_api_key`) short-circuit to "" — auth then comes only
from the app-DB settings store + the env the app injects. Local development mode keeps
the convenience fallbacks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import deploy_mode
from . import settings as settings_store

# ---------------------------------------------------------------------------
#  settings-store key -> the REAL environment variable the harness/.env uses
# ---------------------------------------------------------------------------
# So a provider can read as configured off its .env even when the console store is
# empty. This is the SUPERSET map (it carries the Bedrock SigV4 pair that only the
# catalog layer asks about); the probe only ever queries the Bearer-key subset, and
# the values on every shared key are identical, so a single map serves both callers
# with byte-identical results.
_SETTING_ENV_VAR: dict[str, str] = {
    "kaidera_manifold_api_key": "KAIDERA_MANIFOLD_API_KEY",
    "kaidera_manifold_base_url": "KAIDERA_MANIFOLD_BASE_URL",
    "kaidera_manifold_project_id": "KAIDERA_MANIFOLD_PROJECT_ID",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "fireworks_api_key": "FIREWORKS_API_KEY",
    "groq_api_key": "GROQ_API_KEY",
    "siliconflow_api_key": "SILICONFLOW_API_KEY",
    "dashscope_api_key": "DASHSCOPE_API_KEY",
    "alibaba_cloud_api_key": "ALIBABA_CLOUD_API_KEY",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "together_api_key": "TOGETHER_API_KEY",
    "cohere_api_key": "COHERE_API_KEY",
    "nvidia_api_key": "NVIDIA_API_KEY",
    "inception_api_key": "INCEPTION_API_KEY",
    "moonshot_api_key": "MOONSHOT_API_KEY",
    "perplexity_api_key": "PERPLEXITY_API_KEY",
    "xai_api_key": "XAI_API_KEY",
    "ollama_cloud_api_key": "OLLAMA_CLOUD_API_KEY",
    "aws_access_key_id": "AWS_ACCESS_KEY_ID",
    "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
    "aws_region": "AWS_REGION",
}

_SETTING_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    # The PI Ollama Cloud extension and Ollama's own docs use OLLAMA_API_KEY, while
    # the console's provider field is named ollama_cloud_api_key. Support both.
    "ollama_cloud_api_key": ("OLLAMA_CLOUD_API_KEY", "OLLAMA_API_KEY"),
    # Alibaba Cloud Model Studio shares the same credential namespace as DashScope.
    "alibaba_cloud_api_key": ("ALIBABA_CLOUD_API_KEY", "DASHSCOPE_API_KEY"),
    # NVIDIA documents the hosted NIM API key as NVIDIA_API_KEY; allow the common
    # NIM-specific alias too so deployments can keep their existing secret name.
    "nvidia_api_key": ("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY"),
    # AWS SDKs commonly accept either variable; prefer AWS_REGION but honor both.
    "aws_region": ("AWS_REGION", "AWS_DEFAULT_REGION"),
}


def env_vars_for(setting_key: str) -> tuple[str, ...]:
    """The real env-var name(s) a settings key resolves to (alias tuple first,
    else the single mapped var, else empty)."""
    aliases = _SETTING_ENV_ALIASES.get(setting_key)
    if aliases:
        return aliases
    var = _SETTING_ENV_VAR.get(setting_key)
    return (var,) if var else ()


def env_file_value(var: str) -> str:
    """Best-effort read of `var` from local-cortex/.env (the real harness env file,
    one KEY=VALUE per line). Strips matching quotes. Read-only; NEVER writes the
    file and never raises (absent / unreadable → ""). This is the deepest credential
    fallback so the catalog status / key probe reflect the running config, not just
    the sandbox store.

    SELF-CONTAINED MODE: never reads the host `local-cortex/.env` — auth comes from
    the app-DB settings store + the process env the app injects (see
    `app/deploy_mode.py`)."""
    if deploy_mode.is_selfcontained():
        return ""
    try:
        path = settings_store.CONSOLE_DIR.parent / ".env"
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        return ""
    prefix = f"{var}="
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        val = stripped[len(prefix):].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        return val.strip()
    return ""


def harness_base_url() -> str:
    """Base URL of the host harness-service (the PI model-bridge / explain target).
    `HARNESS_SERVICE_HOST` / `HARNESS_SERVICE_PORT` override the container defaults."""
    host = os.environ.get("HARNESS_SERVICE_HOST", "host.docker.internal")
    port = os.environ.get("HARNESS_SERVICE_PORT", "8766")
    return f"http://{host}:{port}"


def harness_headers() -> dict[str, str]:
    """Bearer headers for the host harness-service (empty when no token is set)."""
    token = (os.environ.get("HARNESS_SERVICE_TOKEN", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def pi_auth_file() -> Path:
    """PI's host-side auth file. `PI_AUTH_FILE` lets tests/ops override the path."""
    override = (os.environ.get("PI_AUTH_FILE") or "").strip()
    return Path(override).expanduser() if override else Path.home() / ".pi" / "agent" / "auth.json"


def pi_auth_api_key(provider: str) -> str:
    """Read one provider API key from PI's auth.json without ever exposing it.

    PI extensions store API-key logins under provider names. Some use direct
    API-key shape (`{"type":"api_key","key":"..."}`); PI OAuth extensions such
    as Ollama Cloud store the API key as an OAuth-shaped `access` token. This is
    a read-only fallback for the host chat runner and local provider probes;
    malformed/missing files degrade to "".

    SELF-CONTAINED MODE: never touches the Mac user's `~/.pi` — auth must come from
    the app-DB settings store only (see `app/deploy_mode.py`). Local development mode
    keeps the convenience fallback.
    """
    if deploy_mode.is_selfcontained():
        return ""
    provider = (provider or "").strip()
    if not provider:
        return ""
    try:
        raw = json.loads(pi_auth_file().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    entry = raw.get(provider)
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    typ = str(entry.get("type") or "api_key").strip().lower()
    if typ not in {"api_key", "apikey", "bearer", "token", "oauth", "oauth2"}:
        return ""
    for field in (
        "key",
        "api_key",
        "apiKey",
        "token",
        "access",
        "access_token",
        "accessToken",
        "refresh",
        "refresh_token",
        "refreshToken",
    ):
        val = entry.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


__all__ = [
    "env_vars_for",
    "env_file_value",
    "harness_base_url",
    "harness_headers",
    "pi_auth_file",
    "pi_auth_api_key",
]
