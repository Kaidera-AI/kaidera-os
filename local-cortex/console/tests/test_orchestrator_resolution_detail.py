"""Autonomous dispatch target-resolution reasons.

The Dispatch board exposes structured routing reasons for unassigned handoffs.
The scheduler must report the same contract in its activity feed so autonomous
mode is diagnosable without guessing from a generic "no roster match" message.
"""

from __future__ import annotations

import pytest

import app.orchestrator as orch
from app.orchestrator import Orchestrator


class FakeCortex:
    def __init__(self, agents: list[dict]) -> None:
        self._agents = agents

    async def get_agents(self, project: str) -> list[dict]:
        return list(self._agents)


def _make_orch(agents: list[dict]) -> Orchestrator:
    return Orchestrator(
        cortex=FakeCortex(agents),
        appdb=object(),
        harness_runner=object(),
        chat_routing_for=lambda agent, project: ("pi", "model", "high"),
        record_usage=None,
        find_agent=lambda agents, name: None,
        resolve_target=lambda handoff, agents: None,
        classify_interactive=lambda agent, desig: desig == "interactive",
        project_identity=lambda cortex, project: None,
        agent_view=lambda a: a,
    )


@pytest.fixture(autouse=True)
def _patch_scheduler_settings(monkeypatch):
    async def _on_projects():
        return ["proj"]

    async def _designation(project: str, agent: str) -> str:
        return ""

    async def _aliases(project: str, agent: str) -> str:
        return ""

    monkeypatch.setattr(orch, "_autonomous_projects_async", _on_projects)
    monkeypatch.setattr(orch, "_agent_designation_async", _designation)
    monkeypatch.setattr(orch, "_agent_role_aliases_async", _aliases)


async def _dispatch_once(o: Orchestrator, handoff: dict) -> dict:
    await o._maybe_dispatch("proj", handoff, source="test", wave_ctx=({}, {}))
    return o.feed.recent("proj", limit=1)[0]


@pytest.mark.asyncio
async def test_human_target_skip_uses_shared_reason():
    """A human/operator role is a deliberate block, not a warning-level miss."""
    o = _make_orch([
        {"name": "worker-a", "role": "cto", "capabilities": {}},
    ])

    item = await _dispatch_once(
        o,
        {"id": "h-human1", "status": "pending", "to_role": "cto"},
    )

    assert item["kind"] == "skipped"
    assert item["level"] == "info"
    assert "human_target" in item["text"]
    assert "will not auto-dispatch" in item["text"]


@pytest.mark.asyncio
async def test_unknown_agent_skip_uses_shared_reason():
    """An explicit missing worker is an unresolved target with a precise warning."""
    o = _make_orch([
        {"name": "worker-a", "role": "builder", "capabilities": {}},
    ])

    item = await _dispatch_once(
        o,
        {"id": "h-miss1", "status": "pending", "to_agent": "missing-worker"},
    )

    assert item["kind"] == "skipped"
    assert item["level"] == "warn"
    assert "unknown_agent" in item["text"]
    assert "not in this project roster" in item["text"]


@pytest.mark.asyncio
async def test_missing_interactive_lead_skip_uses_shared_reason():
    """A lead alias with no configured interactive lead blocks with a useful reason."""
    o = _make_orch([
        {"name": "worker-a", "role": "builder", "capabilities": {}},
    ])

    item = await _dispatch_once(
        o,
        {"id": "h-lead1", "status": "pending", "to_role": "cpo"},
    )

    assert item["kind"] == "skipped"
    assert item["level"] == "warn"
    assert "no_interactive_lead" in item["text"]
    assert "no interactive lead configured" in item["text"]


class FakeCortexWithCreate(FakeCortex):
    """FakeCortex that also records create_handoff (the unroutable escalation net)."""

    def __init__(self, agents):
        super().__init__(agents)
        self.created: list[tuple[str, str, dict]] = []

    async def create_handoff(self, project_key, from_agent, body):
        self.created.append((project_key, from_agent, dict(body)))
        return {"id": "esc-1"}


@pytest.mark.asyncio
async def test_unroutable_handoff_escalates_to_lead_once():
    """A handoff to an invented role (e.g. 'scmo') must file ONE [UNROUTABLE]
    escalation to the lead instead of dying silently (ultrareview 2026-07-02)."""
    cortex = FakeCortexWithCreate([{"name": "worker-a", "role": "builder", "capabilities": {}}])
    o = _make_orch([])
    o._cortex = cortex

    handoff = {"id": "h-scmo001", "status": "pending", "to_role": "scmo",
               "from_agent": "cole@marketing", "summary": "PM alarm: bank empty"}
    await o._maybe_dispatch("proj", handoff, source="test", wave_ctx=({}, {}))

    assert len(cortex.created) == 1
    project, from_agent, body = cortex.created[0]
    assert (project, from_agent) == ("proj", "cole")      # name@project -> bare filer
    assert body["to_role"] == "lead" and "[UNROUTABLE]" in body["summary"]
    assert "scmo" in body["summary"]

    # Re-seeing the same handoff never re-files the escalation.
    o._dispatched.discard(("proj", "h-scmo001"))
    await o._maybe_dispatch("proj", handoff, source="test", wave_ctx=({}, {}))
    assert len(cortex.created) == 1


@pytest.mark.asyncio
async def test_human_target_never_escalates():
    cortex = FakeCortexWithCreate([{"name": "worker-a", "role": "cto", "capabilities": {}}])
    o = _make_orch([])
    o._cortex = cortex

    await o._maybe_dispatch(
        "proj",
        {"id": "h-human2", "status": "pending", "to_role": "cto", "from_agent": "cole"},
        source="test", wave_ctx=({}, {}),
    )
    assert cortex.created == []  # deliberate human block, not an error
