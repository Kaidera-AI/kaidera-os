"""Soft online license transport (app/license_client.py)."""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

from app import license as lic
from app import license_client
from app import license_refresh


class Store:
    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.writes: list[dict] = []

    def save(self, items):
        self.writes.append(dict(items))
        self.data.update(items)
        return True


@pytest.mark.asyncio
async def test_login_stores_session_grant_and_platform_manifold_key(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    store = Store()
    grant = lic.generate_license(
        "Acme", days=30,
        features=["workers:8", "kaidera_os_max_users:5", "manifold_access"],
        org_id="org_1", license_id="lic_1", install_id="install-platform",
    )
    posts: list[tuple[str, dict, dict | None]] = []
    gets: list[tuple[str, dict | None]] = []

    async def post(url, payload, headers=None):
        posts.append((url, payload, headers))
        if url.endswith("/api/v1/license/login"):
            return 200, {
                "license_token": "lic-session",
                "expires_at": "2026-07-04T00:00:00Z",
                "scopes": ["license:read"],
                "org_id": "org_1",
            }
        if url.endswith("/api/v1/license/activate"):
            assert headers == {"X-Kaidera-OS-License-Token": "lic-session"}
            assert payload["install_id"]
            assert "org_login_token" not in payload
            return 200, {
                "grant": grant,
                "install_id": "install-platform",
                "license_id": "lic_1",
            }
        if url.endswith(("/api/v1/license/customer/manifold-key", "/api/v1/license/customer/manifold/key")):
            return 404, {"detail": "Not Found"}
        assert url.endswith("/api/v1/manifold/keys")
        assert headers == {
            "X-Kaidera-OS-License-Token": "lic-session",
            "Authorization": "Bearer lic-session",
        }
        assert payload["key_type"] == "inference"
        return 201, {
            "key": "mf-platform-key",
            "project_id": "project-1",
            "manifold_base_url": "https://platform.example/v1",
        }

    async def get(url, headers=None):
        gets.append((url, headers))
        return 200, {
            "org_id": "org_1",
            "license": {
                "license_id": "lic_1",
                "status": "active",
                "entitlement_snapshot": {},
            },
            "seats": [],
            "entitlements": {},
        }

    res = await license_client.login(
        "ops@example.com",
        "secret",
        mfa_code="123456",
        settings=store.data,
        save_settings=store.save,
        post_json=post,
        get_json=get,
        base_url="https://platform.test",
    )

    assert res.ok and res.stored and res.grant_valid
    assert res.action == "login"
    assert res.customer == "Acme"
    assert res.org_id == "org_1"
    assert res.scopes == ["license:read"]
    assert res.manifold_enabled is True
    assert res.manifold_key_stored is True
    assert res.manifold_project_id_stored is True
    assert posts[0] == (
        "https://platform.test/api/v1/license/login",
        {"email": "ops@example.com", "password": "secret", "mfa_code": "123456"},
        None,
    )
    assert gets[0] == (
        "https://platform.test/api/v1/license/customer/summary",
        {"X-Kaidera-OS-License-Token": "lic-session"},
    )
    assert store.data[license_client.LICENSE_SESSION_TOKEN_KEY] == "lic-session"
    assert store.data[license_client.LICENSE_KEY] == grant
    assert store.data[license_client.LICENSE_ID_KEY] == "lic_1"
    assert store.data[license_client.MANIFOLD_API_KEY] == "mf-platform-key"
    assert store.data[license_client.MANIFOLD_BASE_URL_KEY] == "https://platform.example/v1"
    assert store.data[license_client.MANIFOLD_PROJECT_ID_KEY] == "project-1"


@pytest.mark.asyncio
async def test_login_fail_closes_when_summary_unreachable(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    store = Store({license_client.LICENSE_KEY: "old", license_client.MANIFOLD_API_KEY: "mf-old"})

    async def post(url, _payload, _headers=None):
        assert url.endswith("/api/v1/license/login")
        return 200, {"license_token": "lic-session"}

    async def get(_url, _headers=None):
        return 503, {"error": "platform unavailable"}

    res = await license_client.login(
        "ops@example.com",
        "secret",
        settings=store.data,
        save_settings=store.save,
        post_json=post,
        get_json=get,
        base_url="https://platform.test",
    )

    assert not res.ok
    assert res.revoked is True
    assert store.data[license_client.LICENSE_SESSION_TOKEN_KEY] == "lic-session"
    assert store.data[license_client.REVOKED_KEY] is True
    assert store.data[license_client.MANIFOLD_API_KEY] == ""
    assert store.data[license_client.MANIFOLD_PROJECT_ID_KEY] == ""


@pytest.mark.asyncio
async def test_activate_stores_verified_platform_grant(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    store = Store()
    grant = lic.generate_license(
        "Acme", days=30, features=["harness:codex", "workers:8"],
        license_id="lic_123", install_id="install-platform",
    )
    seen = {}

    async def post(url, payload, headers=None):
        seen["url"] = url
        seen["payload"] = payload
        seen["headers"] = headers
        return 200, {"grant": grant, "install_id": "install-platform"}

    res = await license_client.activate(
        "org-session-token", settings=store.data, save_settings=store.save,
        post_json=post, base_url="https://platform.test/",
    )

    assert res.ok and res.stored and res.grant_valid
    assert seen["url"] == "https://platform.test/api/v1/license/activate"
    assert seen["headers"] == {"X-Kaidera-OS-License-Token": "org-session-token"}
    assert seen["payload"]["org_login_token"] == "org-session-token"
    assert seen["payload"]["app_version"]
    assert seen["payload"]["machine_fp"]
    assert store.data[license_client.LICENSE_KEY] == grant
    assert store.data[license_client.INSTALL_ID_KEY] == "install-platform"


@pytest.mark.asyncio
async def test_activate_rejects_invalid_grant(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    store = Store()

    async def post(_url, _payload):
        return 200, {"grant": "not-a-valid-grant", "install_id": "install-platform"}

    res = await license_client.activate(
        "org-session-token", settings=store.data, save_settings=store.save, post_json=post,
    )

    assert not res.ok
    assert "invalid grant" in (res.error or "")
    assert license_client.LICENSE_KEY not in store.data


@pytest.mark.asyncio
async def test_heartbeat_stores_refreshed_grant_and_latest_release(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    old = lic.generate_license("Acme", days=30, license_id="lic_123", features=["workers:5"])
    store = Store({license_client.LICENSE_KEY: old, license_client.INSTALL_ID_KEY: "install-1"})
    new = lic.generate_license("Acme", days=60, license_id="lic_123", features=["workers:9"])
    latest = {"version": "0.1.999", "sha256": "abc", "artifact_url": "https://x", "required": False}
    seen = {}

    async def post(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return 200, {"grant": new, "revoked": False, "latest_release": latest}

    res = await license_client.heartbeat(
        settings=store.data, save_settings=store.save, post_json=post,
        base_url="https://platform.test",
    )

    assert res.ok and res.stored and res.grant_valid
    assert seen["url"] == "https://platform.test/license/heartbeat"
    assert seen["payload"]["license_id"] == "lic_123"
    assert seen["payload"]["install_id"] == "install-1"
    assert store.data[license_client.LICENSE_KEY] == new
    assert store.data[license_client.LATEST_RELEASE_KEY] == latest


@pytest.mark.asyncio
async def test_session_heartbeat_uses_live_header_and_path(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    old = lic.generate_license(
        "Acme", days=30, license_id="lic_123", features=["workers:5", "manifold_access"]
    )
    new = lic.generate_license(
        "Acme", days=60, license_id="lic_123", features=["workers:9", "manifold_access"]
    )
    store = Store({
        license_client.LICENSE_KEY: old,
        license_client.LICENSE_SESSION_TOKEN_KEY: "lic-session",
        license_client.LICENSE_ID_KEY: "lic_123",
        license_client.INSTALL_ID_KEY: "install-1",
        license_client.MANIFOLD_API_KEY: "mf-key",
        license_client.MANIFOLD_PROJECT_ID_KEY: "project-1",
    })
    seen = {}

    async def post(url, payload, headers=None):
        seen.update(url=url, payload=payload, headers=headers)
        return 200, {"grant": new, "revoked": False, "server_time": 123, "skew_seconds": 0}

    res = await license_client.heartbeat(
        settings=store.data,
        save_settings=store.save,
        post_json=post,
        base_url="https://platform.test",
    )

    assert res.ok and res.grant_valid
    assert seen["url"] == "https://platform.test/api/v1/license/heartbeat"
    assert seen["headers"] == {"X-Kaidera-OS-License-Token": "lic-session"}
    assert seen["payload"]["license_id"] == "lic_123"
    assert store.data[license_client.LICENSE_KEY] == new
    assert store.data[license_client.REVOKED_KEY] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_path"),
    [
        ("restore", "/api/v1/license/customer/licenses/lic_123/restore"),
        ("enable", "/api/v1/license/customer/licenses/lic_123/seats/install-1/enable"),
        ("expire", "/api/v1/license/customer/licenses/lic_123/expire"),
    ],
)
async def test_customer_actions_use_live_id_scoped_routes(monkeypatch, action, expected_path):
    store = Store({
        license_client.LICENSE_SESSION_TOKEN_KEY: "lic-session",
        license_client.LICENSE_ID_KEY: "lic_123",
        license_client.INSTALL_ID_KEY: "install-1",
    })
    seen = {}

    async def post(url, payload, headers=None):
        seen.update(url=url, payload=payload, headers=headers)
        return 200, {"action": action}

    async def synced(token, **kwargs):
        return license_client.LicenseTransportResult(action=kwargs["action"], ok=True)

    monkeypatch.setattr(license_client, "_sync_session_summary", synced)
    res = await license_client.customer_action(
        action,
        settings=store.data,
        save_settings=store.save,
        post_json=post,
        base_url="https://platform.test",
    )

    assert res.ok
    assert seen["url"] == f"https://platform.test{expected_path}"
    assert seen["headers"] == {"X-Kaidera-OS-License-Token": "lic-session"}
    assert action in seen["payload"]["reason"]
    if action == "expire":
        assert store.data[license_client.REVOKED_KEY] is True
        assert store.data[license_client.MANIFOLD_API_KEY] == ""
        assert store.data[license_client.MANIFOLD_PROJECT_ID_KEY] == ""


@pytest.mark.asyncio
async def test_heartbeat_refuses_manual_grant_without_license_id(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    store = Store({license_client.LICENSE_KEY: lic.generate_license("Manual", days=30)})

    res = await license_client.heartbeat(settings=store.data, save_settings=store.save)

    assert not res.ok
    assert "no license_id" in (res.error or "")


@pytest.mark.asyncio
async def test_transport_errors_are_soft(monkeypatch):
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    store = Store()

    async def post(_url, _payload):
        raise RuntimeError("platform offline")

    res = await license_client.activate(
        "org-session-token", settings=store.data, save_settings=store.save, post_json=post,
    )

    assert not res.ok
    assert "platform offline" in (res.error or "")


@pytest.mark.asyncio
async def test_releases_fetches_advisory_release_metadata():
    latest = {
        "version": "0.1.999",
        "sha256": "abc",
        "artifact_url": "https://platform.test/releases/kaidera-os.tar.gz",
        "required": False,
        "notes": "test",
    }
    seen = {}

    async def get(url):
        seen["url"] = url
        return 200, latest

    res = await license_client.releases("stable", get_json=get, base_url="https://platform.test/")

    assert res.ok
    assert res.action == "releases"
    assert seen["url"] == "https://platform.test/api/v1/license/releases/stable"
    assert res.latest_release == latest


@pytest.mark.asyncio
async def test_start_device_flow_uses_pkce_challenge():
    seen = {}

    async def post(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return 200, {
            "device_code": "device-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://platform.test/device",
            "interval": 2,
        }

    res = await license_client.start_device_flow(post_json=post, base_url="https://platform.test")
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(res["code_verifier"].encode("ascii")).digest()
    ).decode("ascii").rstrip("=")

    assert res["ok"] is True
    assert seen["url"] == "https://platform.test/oauth/device_authorization"
    assert seen["payload"]["client_id"] == "kaidera-os-license"
    assert seen["payload"]["code_challenge_method"] == "S256"
    assert seen["payload"]["code_challenge"] == expected_challenge


@pytest.mark.asyncio
async def test_start_device_flow_rejects_incomplete_platform_response():
    async def post(_url, _payload):
        return 200, {"device_code": "device-1"}

    res = await license_client.start_device_flow(post_json=post)

    assert res["ok"] is False
    assert "missing" in res["error"]


@pytest.mark.asyncio
async def test_poll_device_flow_statuses():
    calls = []

    async def post(_url, payload):
        calls.append(payload)
        if len(calls) == 1:
            return 428, {"error": "authorization_pending"}
        return 200, {"access_token": "org-session"}

    pending = await license_client.poll_device_flow("device-1", "verifier", post_json=post)
    done = await license_client.poll_device_flow("device-1", "verifier", post_json=post)

    assert pending == {"status": "pending"}
    assert done == {"status": "done", "org_login_token": "org-session"}
    assert calls[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"


@pytest.mark.asyncio
async def test_poll_device_flow_reports_denied_and_expired():
    async def denied(_url, _payload):
        return 403, {"error": "access_denied"}

    async def expired(_url, _payload):
        return 400, {"error": "expired_token"}

    assert (await license_client.poll_device_flow("d", "v", post_json=denied)) == {
        "status": "error",
        "message": "Device login was denied",
    }
    assert (await license_client.poll_device_flow("d", "v", post_json=expired)) == {
        "status": "error",
        "message": "Device login code expired",
    }


@pytest.mark.asyncio
async def test_heartbeat_forever_runs_once_and_stops(monkeypatch):
    store = Store({"license_key": "existing"})
    stop = asyncio.Event()
    seen: list[dict] = []

    async def fake_heartbeat(*, settings, save_settings):
        seen.append(settings)
        save_settings({license_client.LAST_SYNC_KEY: 123})
        stop.set()
        return license_client.LicenseTransportResult(
            action="heartbeat", ok=True, stored=True, grant_valid=True,
        )

    monkeypatch.setattr(license_client, "heartbeat", fake_heartbeat)

    await license_refresh.heartbeat_forever(
        load_settings=lambda: dict(store.data),
        save_settings=store.save,
        stop=stop,
        initial_delay_s=0,
        interval_s=60,
    )

    assert seen == [{"license_key": "existing"}]
    assert store.data[license_client.LAST_SYNC_KEY] == 123
