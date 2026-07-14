"""Self-contained installs do not read development host files."""

from __future__ import annotations

from app import deploy_mode, providers


def test_default_is_dev(monkeypatch):
    monkeypatch.delenv(deploy_mode.ENV_VAR, raising=False)
    assert deploy_mode.deploy_mode() == deploy_mode.DEV
    assert deploy_mode.is_dev()
    assert not deploy_mode.is_selfcontained()


def test_selfcontained_only_on_exact_value(monkeypatch):
    monkeypatch.setenv(deploy_mode.ENV_VAR, "selfcontained")
    assert deploy_mode.is_selfcontained()
    monkeypatch.setenv(deploy_mode.ENV_VAR, "SelfContained")
    assert deploy_mode.is_selfcontained()
    monkeypatch.setenv(deploy_mode.ENV_VAR, "garbage")
    assert deploy_mode.is_dev()


def test_selfcontained_does_not_read_development_env_file(monkeypatch):
    monkeypatch.setenv(deploy_mode.ENV_VAR, "selfcontained")
    assert providers._env_file_value("KAIDERA_MANIFOLD_API_KEY") == ""

    from app.settings_module import service as svc

    assert svc.group_catalog_models([]) == {"providers": []}
