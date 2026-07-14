"""Self-contained mode reads NO host-user files (the redistributable guarantee).

The distributable runs as a native console on a fresh Linux VM with no Mac host. In
`KAIDERA_DEPLOY_MODE=selfcontained` the auth path must NEVER read `~/.pi`, `~/.claude`,
or `local-cortex/.env` — auth comes only from the app-DB settings store + the process
env the app injects. `dev` keeps the host-file fallbacks.

See `app/deploy_mode.py` + the `scripts/fitness/check-selfcontained-no-host.sh` gate.
"""

from __future__ import annotations

import json

from app import deploy_mode, provider_check, providers


def test_default_is_dev(monkeypatch):
    monkeypatch.delenv(deploy_mode.ENV_VAR, raising=False)
    assert deploy_mode.deploy_mode() == deploy_mode.DEV
    assert deploy_mode.is_dev()
    assert not deploy_mode.is_selfcontained()


def test_selfcontained_only_on_exact_value(monkeypatch):
    monkeypatch.setenv(deploy_mode.ENV_VAR, "selfcontained")
    assert deploy_mode.is_selfcontained()
    monkeypatch.setenv(deploy_mode.ENV_VAR, "SelfContained")  # case-insensitive
    assert deploy_mode.is_selfcontained()
    monkeypatch.setenv(deploy_mode.ENV_VAR, "garbage")  # unknown -> dev
    assert deploy_mode.is_dev()


def test_selfcontained_does_not_read_pi_auth_file(tmp_path, monkeypatch):
    # A real PI-style auth file WITH a usable key present on disk.
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"ollama-cloud": {"type": "api_key", "key": "SECRET"}}))
    monkeypatch.setenv("PI_AUTH_FILE", str(auth))

    # dev: the host fallback reads the key.
    monkeypatch.setenv(deploy_mode.ENV_VAR, "dev")
    assert providers._pi_auth_api_key("ollama-cloud") == "SECRET"

    # selfcontained: the host file is NEVER touched.
    monkeypatch.setenv(deploy_mode.ENV_VAR, "selfcontained")
    assert providers._pi_auth_api_key("ollama-cloud") == ""


def test_selfcontained_does_not_read_env_file(monkeypatch):
    # Both `_env_file_value` copies (providers + provider_check) short-circuit.
    monkeypatch.setenv(deploy_mode.ENV_VAR, "selfcontained")
    assert providers._env_file_value("ANTHROPIC_API_KEY") == ""
    assert provider_check._env_file_value("ANTHROPIC_API_KEY") == ""
