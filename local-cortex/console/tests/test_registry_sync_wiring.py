"""Feature-gap #81 — overrides are console-LOCAL on save; promotion is EXPLICIT.

The CTO REVERSED the earlier auto-sync-on-save decision: a config SAVE must write ONLY
the console-local override (the registry stays authoritative), and a separate, explicit
"Promote to registry" action pushes that agent's current effective config into Cortex
ON DEMAND.

This file proves BOTH halves of that contract:

  * a config SAVE no longer touches the registry — neither the SPA JSON save
    (`settings_module.api.save_agent_config_endpoint`, `POST /settings/{p}/agents/{a}/
    config`) nor the HTML inline-header save (`main.agent_config_save`,
    `POST /agents/{p}/{a}/config`) calls `create_agent`; the local override still lands.
  * the EXPLICIT promote endpoint (`settings_module.api.promote_agent_config_endpoint`,
    `POST /settings/{p}/agents/{a}/promote`) builds the payload from the agent's CURRENT
    effective override + role and calls `create_agent`, returning `{ok}` on success and a
    graceful `{ok:false, error}` (never a raise) when the registry write is degraded.

The pure promote logic (`app.registry_sync`) is unit-tested in `test_registry_sync.py`;
this file is the WIRING: save-does-not-sync + the promote endpoint actually calls it.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _open_local_mode(monkeypatch):
    """These wiring tests assert config-save behavior, not auth policy."""
    from app import auth

    monkeypatch.setattr(auth, "auth_enabled", lambda: False)


# ---------------------------------------------------------------------------
#  Shared fakes
# ---------------------------------------------------------------------------


class FakeRegistryCortex:
    """A duck-typed Cortex client: records create_agent calls and serves a fixed roster
    for the agent-role lookup. `sync_result` controls the simulated registry-write
    outcome (None = a degraded/failed write)."""

    def __init__(self, *, roster=None, sync_result=None):
        self._roster = list(roster or [])
        self.sync_result = sync_result
        self.create_calls: list[dict] = []

    async def get_agents(self, project_key):
        return list(self._roster)

    async def get_project(self, project_key):
        # The HTML save path re-resolves the project for the swapped sub-line; a
        # minimal row is enough (it never depends on the registry write).
        return {"project_key": project_key, "display_name": project_key}

    async def create_agent(self, project_key, *, name, role, capabilities,
                           writer_scope=None, role_description=None, caller=None):
        self.create_calls.append(
            {"project_key": project_key, "name": name, "role": role,
             "capabilities": capabilities, "writer_scope": writer_scope, "caller": caller}
        )
        return self.sync_result


# ===========================================================================
#  A config SAVE is console-LOCAL ONLY — it must NOT write the registry
# ===========================================================================


@pytest.mark.asyncio
async def test_spa_config_save_does_NOT_sync_to_registry():
    """The SPA save persists ONLY the local override — create_agent is NEVER called and
    there is no `registry_synced` flag in the response (save no longer promotes)."""
    from app.settings_module import api as settings_api
    from tests.test_settings_module import FakeOpStore

    store = FakeOpStore(overrides={"kaidera-os:newbie": {"harness": "claude-code"}})
    cortex = FakeRegistryCortex(
        roster=[{"name": "newbie", "role": "qa", "capabilities": {"keep": 1}}],
        sync_result={"registered": True, "agent": "newbie"},
    )

    result = await settings_api.save_agent_config_endpoint(
        "kaidera-os", "newbie",
        {"override": {"model": "opus"}},
        store=store,
    )

    # The LOCAL save happened (the merge applied + persisted).
    assert result["ok"] is True
    assert result["override"]["model"] == "opus"
    assert store.get_agent_override("kaidera-os", "newbie")["model"] == "opus"
    # The registry was NOT touched, and no soft-sync flag is emitted on save.
    assert cortex.create_calls == []
    assert "registry_synced" not in result


@pytest.mark.asyncio
async def test_html_config_save_does_NOT_sync_to_registry(monkeypatch):
    """The HTML inline-header save persists the local override and NEVER calls
    create_agent — proving the registry write is gone from the second save surface."""
    from app import main as main_mod

    # Keep the local override write off the real settings store (return the effective
    # override directly), so the test is hermetic and asserts the absence of a sync.
    monkeypatch.setattr(
        main_mod.settings_store, "save_agent_override",
        lambda project, agent, override: {"harness": "claude-code", "model": "opus"},
    )

    cortex = FakeRegistryCortex(
        roster=[{"name": "newbie", "role": "qa", "capabilities": {}}],
        sync_result={"registered": True},
    )
    from app.main import app
    app.state.cortex = cortex

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/agents/kaidera-os/newbie/config",
            data={"harness": "claude-code", "model": "opus"},
        )

    assert resp.status_code == 200
    assert "saved" in resp.text.lower()
    # The save is console-local only — no registry write fired.
    assert cortex.create_calls == []


@pytest.mark.asyncio
async def test_legacy_configure_save_does_NOT_sync_to_registry(monkeypatch):
    """The legacy Configure-card save (`POST /settings/configure`) also writes ONLY the
    local override — create_agent is never called from it."""
    from app import main as main_mod

    monkeypatch.setattr(
        main_mod.settings_store, "save_agent_override",
        lambda project, agent, override: {"harness": "claude-code"},
    )

    cortex = FakeRegistryCortex(
        roster=[{"name": "newbie", "role": "qa", "capabilities": {}}],
        sync_result={"registered": True},
    )
    from app.main import app
    app.state.cortex = cortex

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/settings/configure", data={"agent": "newbie", "harness": "claude-code"}
        )

    assert resp.status_code == 200
    assert "saved" in resp.text.lower()
    assert cortex.create_calls == []


# ===========================================================================
#  The EXPLICIT promote endpoint — POST /settings/{p}/agents/{a}/promote
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_endpoint_pushes_effective_config_to_registry():
    """The promote endpoint reads the agent's CURRENT effective override + role and
    calls create_agent with the merged capabilities, returning `{ok: True}`."""
    from app.settings_module import api as settings_api
    from tests.test_settings_module import FakeOpStore

    # The agent already has a console-local override (harness + model) — promotion pushes
    # exactly that current state, no save needed first.
    store = FakeOpStore(overrides={"kaidera-os:newbie": {"harness": "claude-code", "model": "opus"}})
    cortex = FakeRegistryCortex(
        roster=[{"name": "newbie", "role": "qa", "capabilities": {"keep": 1}}],
        sync_result={"registered": True, "agent": "newbie"},
    )

    result = await settings_api.promote_agent_config_endpoint(
        "kaidera-os", "newbie", store=store, cortex=cortex
    )

    assert result["ok"] is True
    assert result.get("error") in (None, "")
    # create_agent fired with the effective override's merged capabilities + the role.
    assert len(cortex.create_calls) == 1
    call = cortex.create_calls[0]
    assert call["project_key"] == "kaidera-os"
    assert call["name"] == "newbie"
    assert call["role"] == "qa"
    assert call["capabilities"]["harness"] == "claude-code"
    assert call["capabilities"]["model"] == "opus"
    assert call["capabilities"]["keep"] == 1  # merged over existing caps


@pytest.mark.asyncio
async def test_promote_endpoint_degraded_write_is_graceful_not_500():
    """A degraded registry write (create_agent → None) is a graceful `{ok:false, error}`
    — never a raise/500. (The console-local override is untouched.)"""
    from app.settings_module import api as settings_api
    from tests.test_settings_module import FakeOpStore

    store = FakeOpStore(overrides={"kaidera-os:newbie": {"harness": "claude-code"}})
    cortex = FakeRegistryCortex(
        roster=[{"name": "newbie", "role": "qa", "capabilities": {}}],
        sync_result=None,  # registry write degraded
    )

    result = await settings_api.promote_agent_config_endpoint(
        "kaidera-os", "newbie", store=store, cortex=cortex
    )

    assert result["ok"] is False
    assert result["error"]  # a human, non-empty error string
    assert len(cortex.create_calls) == 1  # it was attempted


@pytest.mark.asyncio
async def test_promote_endpoint_no_cortex_is_graceful_not_500():
    """With NO cortex client wired (read-only/degraded), promote is a graceful
    `{ok:false, error}` with no call — never a 500."""
    from app.settings_module import api as settings_api
    from tests.test_settings_module import FakeOpStore

    store = FakeOpStore(overrides={"kaidera-os:newbie": {"harness": "claude-code"}})
    result = await settings_api.promote_agent_config_endpoint(
        "kaidera-os", "newbie", store=store, cortex=None
    )
    assert result["ok"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_promote_endpoint_does_not_expose_admin_token():
    """The promote response NEVER carries a token-shaped field (the write is writer-
    gated, not admin; belt-and-braces that nothing leaks)."""
    from app.settings_module import api as settings_api
    from tests.test_settings_module import FakeOpStore

    store = FakeOpStore(overrides={"kaidera-os:newbie": {"harness": "claude-code"}})
    cortex = FakeRegistryCortex(
        roster=[{"name": "newbie", "role": "qa", "capabilities": {}}],
        sync_result=None,
    )
    result = await settings_api.promote_agent_config_endpoint(
        "kaidera-os", "newbie", store=store, cortex=cortex
    )
    blob = " ".join(str(v) for v in result.values()).lower()
    assert "token" not in blob
