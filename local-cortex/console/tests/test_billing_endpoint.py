"""GET /settings/{project}/billing — the Billing tab's view: entitlement totals +
live usage counts + wallet balance + active add-ons. Management lives in the cust-portal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import license as lic
from app.settings_module.api import billing_status_endpoint


class _FakeCortex:
    def __init__(self, projects, roster):
        self._projects, self._roster = projects, roster

    async def get_projects(self):
        return self._projects

    async def get_roster(self, project):
        return self._roster


class _FakeAuthStore:
    def __init__(self, users):
        self._users = users

    async def count_users(self):
        return self._users


def _request(cortex, users=1):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cortex=cortex, auth_store=_FakeAuthStore(users))))


@pytest.mark.asyncio
async def test_billing_free_tier_usage_and_totals(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    monkeypatch.setenv("KAIDERA_OS_PORTAL_URL", "https://platform.example")
    monkeypatch.delenv("KAIDERA_OS_LICENSE_KEY", raising=False)
    cortex = _FakeCortex(projects=[{"project_key": "a"}], roster=[{"name": "w1"}, {"name": "w2"}, {"name": "w3"}])
    out = await billing_status_endpoint("a", _request(cortex))

    rows = {r["kind"]: r for r in out["entitlements"]}
    assert rows["projects"]["used"] == 1 and rows["projects"]["total"] == 1
    assert rows["workers"]["used"] == 3 and rows["workers"]["total"] == 4   # 3 of 4 used
    assert rows["teams"]["total"] == 1
    assert rows["users"]["used"] == 1 and rows["users"]["total"] == 1
    assert out["wallet"] is None          # free tier carries no wallet
    assert out["portal_url"] == "https://platform.example"


@pytest.mark.asyncio
async def test_billing_wallet_and_addons_from_grant(monkeypatch, ed25519_public_license):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    ed25519_public_license(  # public verifies only Ed25519 platform grants
        "DXB", days=365,
        features=["workers:6", "projects:2", "kaidera_os_max_users:4"],
        wallet={"balance": 42.5, "currency": "USD", "as_of": 1_700_000_000},
        addons=[{"sku": "addon:worker", "qty": 2}, {"sku": "addon:project", "qty": 1}],
    )
    cortex = _FakeCortex(projects=[{"project_key": "a"}, {"project_key": "b"}], roster=[{"name": "w1"}])
    out = await billing_status_endpoint("a", _request(cortex))

    assert out["wallet"] == {"balance": 42.5, "currency": "USD", "as_of": 1_700_000_000}
    assert {"sku": "addon:worker", "qty": 2} in out["addons"]
    rows = {r["kind"]: r for r in out["entitlements"]}
    assert rows["workers"]["total"] == 6 and rows["projects"]["total"] == 2
    assert rows["users"]["total"] == 4
    assert rows["projects"]["used"] == 2   # both projects counted


@pytest.mark.asyncio
async def test_billing_degrades_without_cortex(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "public")
    out = await billing_status_endpoint("a", _request(None))   # no cortex
    rows = {r["kind"]: r for r in out["entitlements"]}
    assert rows["workers"]["used"] is None   # usage unknown, never raises
    assert rows["workers"]["total"] == 4
    assert rows["users"]["total"] == 1


@pytest.mark.asyncio
async def test_billing_dev_edition_unlimited(monkeypatch):
    monkeypatch.setenv("KAIDERA_OS_EDITION", "dev")
    cortex = _FakeCortex(projects=[{"project_key": "a"}], roster=[{"name": "w1"}])
    out = await billing_status_endpoint("a", _request(cortex))
    rows = {r["kind"]: r for r in out["entitlements"]}
    assert rows["workers"]["total"] is None   # unlimited -> null
