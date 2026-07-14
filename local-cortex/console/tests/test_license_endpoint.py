"""GET /settings/{project}/license — the Settings → License panel's data source."""

from __future__ import annotations

import pytest

from app import license as lic
from app.settings_module import api as settings_api
from app.settings_module.api import license_status_endpoint


class _Store:
    def __init__(self, app_settings=None):
        self._app = dict(app_settings or {})

    def available(self):
        return True

    def load_app_settings(self):
        return dict(self._app)

    def upsert_app_settings(self, items):
        self._app.update(items)
        return True


@pytest.mark.asyncio
async def test_license_endpoint_public_free_tier(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    out = await license_status_endpoint("proj")
    assert out["edition"] == "public"
    assert out["valid"] is False
    assert out["harnesses"] == ["kaidera"]
    assert out["limits"] == {"projects": 1, "teams": 1, "workers": 4, "users": 1}
    assert out["advanced"] == {"manifold_access": False}
    assert "token" not in out  # never leaks the raw token


@pytest.mark.asyncio
async def test_license_endpoint_with_grant(monkeypatch):
    monkeypatch.setattr("app.license._require_ed25519", lambda: False)
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY",
                       lic.generate_license("DXB", days=365, features=["harness:codex", "workers:8", "kaidera_os_max_users:3", "manifold_access"]))
    out = await license_status_endpoint("proj")
    assert out["valid"] is True and out["customer"] == "DXB"
    assert set(out["harnesses"]) == {"kaidera", "codex"}
    assert out["limits"]["workers"] == 8
    assert out["limits"]["users"] == 3
    assert out["advanced"]["manifold_access"] is True


@pytest.mark.asyncio
async def test_license_endpoint_dev_is_unlimited(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    out = await license_status_endpoint("proj")
    assert out["edition"] == "dev"
    assert out["all_harnesses"] is True
    assert out["limits"]["workers"] is None  # inf -> null (JSON-safe)


@pytest.mark.asyncio
async def test_license_activate_endpoint_delegates_to_transport(monkeypatch):
    calls = {}

    class _Result:
        def to_dict(self):
            return {"action": "activate", "ok": True, "stored": True}

    async def fake_activate(token, *, settings, save_settings):
        calls["token"] = token
        calls["settings"] = settings
        calls["saved"] = save_settings({"license_install_id": "i1"})
        return _Result()

    monkeypatch.setattr("app.license_client.activate", fake_activate)
    store = _Store({"existing": "yes"})
    out = await settings_api.license_activate_endpoint(
        "proj", {"org_login_token": "session-token"}, store=store, _admin=None,
    )
    assert out == {"project": "proj", "action": "activate", "ok": True, "stored": True}
    assert calls == {"token": "session-token", "settings": {"existing": "yes"}, "saved": True}


@pytest.mark.asyncio
async def test_license_login_endpoint_delegates_to_transport(monkeypatch):
    calls = {}

    class _Result:
        def to_dict(self):
            return {"action": "login", "ok": True, "stored": True, "manifold_key_stored": True}

    async def fake_login(email, password, *, mfa_code, settings, save_settings):
        calls["email"] = email
        calls["password"] = password
        calls["mfa_code"] = mfa_code
        calls["settings"] = settings
        calls["saved"] = save_settings({"license_session_token": "sess"})
        return _Result()

    monkeypatch.setattr("app.license_client.login", fake_login)
    store = _Store({"existing": "yes"})
    out = await settings_api.license_login_endpoint(
        "proj", {"email": "ops@example.com", "password": "secret", "mfa_code": "123456"}, store=store, _admin=None,
    )
    assert out == {"project": "proj", "action": "login", "ok": True, "stored": True, "manifold_key_stored": True}
    assert calls == {
        "email": "ops@example.com",
        "password": "secret",
        "mfa_code": "123456",
        "settings": {"existing": "yes"},
        "saved": True,
    }


@pytest.mark.asyncio
async def test_license_heartbeat_endpoint_delegates_to_transport(monkeypatch):
    calls = {}

    class _Result:
        def to_dict(self):
            return {"action": "heartbeat", "ok": False, "error": "no valid license grant to heartbeat"}

    async def fake_heartbeat(*, settings, save_settings):
        calls["settings"] = settings
        calls["saved"] = save_settings({"license_last_sync": 1})
        return _Result()

    monkeypatch.setattr("app.license_client.heartbeat", fake_heartbeat)
    store = _Store({"license_key": "x"})
    out = await settings_api.license_heartbeat_endpoint("proj", store=store, _admin=None)
    assert out["project"] == "proj"
    assert out["action"] == "heartbeat"
    assert out["ok"] is False
    assert calls == {"settings": {"license_key": "x"}, "saved": True}


@pytest.mark.asyncio
async def test_license_releases_endpoint_delegates_to_transport(monkeypatch):
    calls = {}

    class _Result:
        def to_dict(self):
            return {
                "action": "releases",
                "ok": True,
                "latest_release": {"version": "0.1.999"},
            }

    async def fake_releases(channel):
        calls["channel"] = channel
        return _Result()

    monkeypatch.setattr("app.license_client.releases", fake_releases)
    out = await settings_api.license_releases_endpoint("proj", "stable", _admin=None)

    assert out == {
        "project": "proj",
        "action": "releases",
        "ok": True,
        "latest_release": {"version": "0.1.999"},
    }
    assert calls == {"channel": "stable"}
