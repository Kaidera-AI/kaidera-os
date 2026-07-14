"""Feature-gap #81 — the override→registry EXPLICIT PROMOTE (the CTO's reversed choice).

Console agent-config overrides stay console-LOCAL by default (the registry stays
authoritative). A deliberate "Promote to registry" action pushes ONE agent's current
effective config (harness/model/reasoning/writer_scope, carried in `capabilities`) +
its current role INTO the live Cortex registry ON DEMAND — NOT on every save. This
preserves the console-local/registry boundary and gives an explicit commit gesture.

This module owns that promote logic, split so it's testable WITHOUT a live Cortex:

  * ``build_registry_sync_payload(override, agent_record)`` — PURE. Maps the saved
    EFFECTIVE override (+ the agent's registry record, for its current role/caps)
    onto the `{role, capabilities, writer_scope}` an `AgentRegister` UPSERT needs.
    The capabilities carry harness/model/reasoning (the override's config fields)
    MERGED over the agent's existing capabilities (the conflict-update jsonb-merges,
    so a re-register persists them additively). I/O-free, never raises.

  * ``promote_agent_to_registry(cortex, project, agent, override, agent_record)``
    — ASYNC, BEST-EFFORT. Builds the payload + calls ``cortex.create_agent`` and
    returns a soft bool (did the registry write land?). A registry failure (None
    from create_agent, a None cortex, a raise) returns False WITHOUT propagating —
    the explicit-promote endpoint surfaces the soft outcome to the operator.

VERIFIED (load-bearing): the Cortex `POST /agents` conflict-update does
``capabilities = COALESCE(agents.capabilities,'{}') || EXCLUDED.capabilities`` (a
jsonb MERGE), so re-registering with the merged capabilities PERSISTS harness/model/
reasoning/writer_scope. The promote therefore works directly via `create_agent`.
"""

from __future__ import annotations

import pytest

from app.registry_sync import (
    build_registry_sync_payload,
    promote_agent_to_registry,
)


# ===========================================================================
#  build_registry_sync_payload — PURE mapping
# ===========================================================================


def test_payload_maps_override_config_into_capabilities():
    """The saved effective override's harness/model/reasoning/role aliases land in capabilities;
    writer_scope is surfaced both in capabilities AND as the top-level writer_scope."""
    override = {
        "harness": "claude-code",
        "model": "opus",
        "reasoning": "high",
        "role_aliases": "creative-multimedia",
        "writer_scope": "work",
        "role": "qa lead",
    }
    agent_record = {"name": "newbie", "role": "qa", "capabilities": {"existing": "x"}}

    payload = build_registry_sync_payload(override, agent_record)

    # role: the override role wins over the registry role and is registry-normalized.
    assert payload["role"] == "qa-lead"
    caps = payload["capabilities"]
    assert caps["harness"] == "claude-code"
    assert caps["model"] == "opus"
    assert caps["reasoning"] == "high"
    assert caps["role_aliases"] == "creative-multimedia"
    # existing capabilities are preserved (merge, not replace).
    assert caps["existing"] == "x"
    # writer_scope is the top-level register field.
    assert payload["writer_scope"] == "work"


def test_payload_falls_back_to_registry_role_when_override_role_blank():
    """With no override role, the agent's current registry role is used (create_agent
    REQUIRES a role; we never send a blank)."""
    override = {"harness": "pi", "model": "gpt-5"}
    agent_record = {"name": "x", "role": "full-stack-developer", "capabilities": {}}

    payload = build_registry_sync_payload(override, agent_record)
    assert payload["role"] == "full-stack-developer"


def test_payload_normalizes_ui_role_to_registry_slug():
    """Human-facing role labels are normalized before POST /agents."""
    payload = build_registry_sync_payload(
        {"role": "CMO / Marketing Lead"},
        {"name": "marlow", "role": "cmo", "capabilities": {}},
    )

    assert payload["role"] == "cmo-marketing-lead"


def test_payload_omits_blank_config_fields():
    """Only the NON-blank config fields go into the synced capabilities (a blank
    field was cleared in the override — we don't push an empty value that would
    clobber the registry's harness/model on the merge)."""
    override = {"harness": "claude-code", "model": "", "reasoning": "   "}
    agent_record = {"name": "x", "role": "dev", "capabilities": {}}

    caps = build_registry_sync_payload(override, agent_record)["capabilities"]
    assert caps["harness"] == "claude-code"
    assert "model" not in caps
    assert "reasoning" not in caps


def test_payload_writer_scope_absent_when_not_overridden():
    """No writer_scope in the override → no top-level writer_scope on the payload
    (so create_agent omits it + the API keeps the agent's stored scope)."""
    override = {"harness": "claude-code"}
    agent_record = {"name": "x", "role": "dev", "capabilities": {}}

    payload = build_registry_sync_payload(override, agent_record)
    assert payload.get("writer_scope") in (None, "")


def test_payload_handles_missing_agent_record_gracefully():
    """A None / empty agent record yields an empty role (the caller skips the sync
    when it can't resolve a role) — never raises."""
    payload = build_registry_sync_payload({"harness": "x"}, None)
    assert payload["role"] == ""
    assert isinstance(payload["capabilities"], dict)


# ===========================================================================
#  promote_agent_to_registry — ASYNC, best-effort (the explicit-promote write)
# ===========================================================================


class _FakeCortex:
    """Records create_agent calls; `result` controls the simulated outcome."""

    def __init__(self, result=None):
        self.result = result
        self.calls: list[dict] = []

    async def create_agent(self, project_key, *, name, role, capabilities,
                           writer_scope=None, role_description=None, caller=None):
        self.calls.append(
            {
                "project_key": project_key,
                "name": name,
                "role": role,
                "capabilities": capabilities,
                "writer_scope": writer_scope,
                "caller": caller,
            }
        )
        return self.result


@pytest.mark.asyncio
async def test_promote_calls_create_agent_with_merged_capabilities():
    """A successful promote calls create_agent with the agent name, the resolved role,
    and the merged capabilities (harness/model/reasoning) — and returns True."""
    cortex = _FakeCortex(result={"registered": True, "agent": "newbie"})
    override = {"harness": "claude-code", "model": "opus", "reasoning": "high", "role": "qa"}
    agent_record = {"name": "newbie", "role": "qa", "capabilities": {"keep": 1}}

    ok = await promote_agent_to_registry(
        cortex, "kaidera-os", "newbie", override, agent_record
    )

    assert ok is True
    assert len(cortex.calls) == 1
    call = cortex.calls[0]
    assert call["project_key"] == "kaidera-os"
    assert call["name"] == "newbie"
    assert call["role"] == "qa"
    # The promote authorises the write as the SUBJECT (an existing project writer), not the
    # console's fixed CONSOLE_AGENT — so it works on any turnkey project (v0.1.124 fix).
    assert call["caller"] == "newbie"
    assert call["capabilities"]["harness"] == "claude-code"
    assert call["capabilities"]["model"] == "opus"
    assert call["capabilities"]["reasoning"] == "high"
    assert call["capabilities"]["keep"] == 1  # merged, not replaced


@pytest.mark.asyncio
async def test_promote_failure_does_not_raise_returns_false():
    """A registry-write FAILURE (create_agent returns None — the graceful-degrade
    signal) returns False, NOT an exception — so the promote endpoint reports a soft
    failure instead of 500-ing."""
    cortex = _FakeCortex(result=None)  # create_agent degraded
    ok = await promote_agent_to_registry(
        cortex, "kaidera-os", "x", {"harness": "claude-code"}, {"name": "x", "role": "dev"}
    )
    assert ok is False  # soft failure signal, no raise


@pytest.mark.asyncio
async def test_promote_none_cortex_is_soft_false():
    """A None cortex client (the read-only/degraded path) is a soft False, no raise
    (and obviously no call)."""
    ok = await promote_agent_to_registry(
        None, "kaidera-os", "x", {"harness": "claude-code"}, {"name": "x", "role": "dev"}
    )
    assert ok is False


@pytest.mark.asyncio
async def test_promote_skips_when_no_role_resolvable():
    """When neither the override nor the registry record yields a role, the promote is
    SKIPPED (create_agent never called) and returns False — we never POST a blank
    role (which the API would 400)."""
    cortex = _FakeCortex(result={"registered": True})
    ok = await promote_agent_to_registry(
        cortex, "kaidera-os", "x", {"harness": "claude-code"}, {"name": "x", "role": ""}
    )
    assert ok is False
    assert cortex.calls == []


@pytest.mark.asyncio
async def test_promote_swallows_a_raising_create_agent():
    """Even if create_agent itself raises (belt-and-braces — the real one shouldn't),
    the promote swallows it and returns False — the endpoint never 500s."""

    class _Raising:
        async def create_agent(self, *a, **k):
            raise RuntimeError("boom")

    ok = await promote_agent_to_registry(
        _Raising(), "kaidera-os", "x", {"harness": "claude-code"}, {"name": "x", "role": "dev"}
    )
    assert ok is False


@pytest.mark.asyncio
async def test_promote_blank_agent_or_project_is_soft_false():
    """A blank agent or project short-circuits to False without a call."""
    cortex = _FakeCortex(result={"registered": True})
    assert await promote_agent_to_registry(cortex, "kaidera-os", "  ", {}, {"role": "d"}) is False
    assert await promote_agent_to_registry(cortex, "", "x", {}, {"role": "d"}) is False
    assert cortex.calls == []
