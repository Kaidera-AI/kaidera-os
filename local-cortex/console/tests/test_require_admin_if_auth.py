"""The auth-mode-aware admin gate (auth.require_admin_if_auth) that protects the
privileged mutation routes (agent/project registration, skill install/bind).

Money-path: it MUST no-op when auth is off (kaidera-os open mode) and enforce admin
when auth is on (enterprise) — getting this wrong either breaks kaidera-os or leaves
the privilege-escalation hole the review found.
"""

from __future__ import annotations

import pytest

from app import auth


class _Req:  # a stand-in Request; the gate only forwards it to current_user_from_request
    pass


@pytest.mark.asyncio
async def test_noop_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(auth, "auth_enabled", lambda: False)
    # Must not even consult the session store when auth is off.
    async def _boom(_req):
        raise AssertionError("current_user_from_request must not be called when auth is off")
    monkeypatch.setattr(auth, "current_user_from_request", _boom)
    assert await auth.require_admin_if_auth(_Req()) is None


@pytest.mark.asyncio
async def test_401_when_enabled_and_no_session(monkeypatch):
    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    async def _none(_req):
        return None
    monkeypatch.setattr(auth, "current_user_from_request", _none)
    with pytest.raises(auth.HTTPException) as ei:
        await auth.require_admin_if_auth(_Req())
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_403_when_enabled_and_non_admin(monkeypatch):
    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    async def _member(_req):
        return {"role": "member", "email": "u@x.io"}
    monkeypatch.setattr(auth, "current_user_from_request", _member)
    with pytest.raises(auth.HTTPException) as ei:
        await auth.require_admin_if_auth(_Req())
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_passes_when_enabled_and_admin(monkeypatch):
    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    async def _admin(_req):
        return {"role": "admin", "email": "a@x.io"}
    monkeypatch.setattr(auth, "current_user_from_request", _admin)
    out = await auth.require_admin_if_auth(_Req())
    assert out and out["role"] == "admin"
