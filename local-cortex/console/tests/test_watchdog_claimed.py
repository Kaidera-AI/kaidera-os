"""Watchdog Cortex I/O contract.

The watchdog supervises every autonomous project from one console process. Its I/O
must therefore use explicitly project-scoped Cortex HTTP calls; a Cortex CLI process
correctly rejects a foreign ``CORTEX_PROJECT`` from the Kaidera OS workspace.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.watchdog import CortexWatchdogOps


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.created = {"id": "watchdog-signal-1"}

    async def get_handoffs(self, project, status=None):
        self.calls.append(("get_handoffs", project, status))
        return []

    async def get_agents(self, project):
        self.calls.append(("get_agents", project))
        return [
            {
                "name": "marlow",
                "role": "cmo",
                "interactive": True,
                "designation": "interactive",
                "capabilities": {"designation": "interactive"},
            }
        ]

    async def search(self, project, query, limit=12, rerank=False):
        self.calls.append(("search", project, query, limit, rerank))
        return [{"text": query}]

    async def get_handoff(self, project, handoff_id):
        self.calls.append(("get_handoff", project, handoff_id))
        claimed = datetime.now(timezone.utc) - timedelta(seconds=1800)
        return {"id": handoff_id, "claimed_at": claimed.isoformat()}

    async def complete_handoff(self, project, handoff_id, agent=""):
        self.calls.append(("complete", project, handoff_id, agent))
        return True

    async def release_handoff(self, project, handoff_id, agent="", reason=""):
        self.calls.append(("release", project, handoff_id, agent, reason))
        return True

    async def create_handoff(self, project, from_agent, body):
        self.calls.append(("create", project, from_agent, body))
        return self.created


@pytest.mark.asyncio
async def test_ops_request_claimed_handoffs_for_foreign_project():
    client = FakeClient()
    ops = CortexWatchdogOps(project="", client=client)

    await ops.get_handoffs("marketing")

    assert client.calls == [("get_handoffs", "marketing", "claimed")]


@pytest.mark.asyncio
async def test_success_marker_uses_project_scoped_cortex_search():
    client = FakeClient()
    ops = CortexWatchdogOps(project="", client=client)

    assert await ops.has_success_marker("marketing", "abcdef12") is True
    assert client.calls == [
        ("search", "marketing", "COMPLETED abcdef12", 12, False)
    ]


@pytest.mark.asyncio
async def test_claimed_age_reads_full_handoff_over_api():
    client = FakeClient()
    ops = CortexWatchdogOps(project="", client=client)

    age = await ops.claimed_age_seconds("marketing", "abcdef12")

    assert age is not None and 1700 < age < 1900
    assert client.calls == [("get_handoff", "marketing", "abcdef12")]


@pytest.mark.asyncio
async def test_complete_and_release_use_scoped_client_methods_with_reason():
    client = FakeClient()
    ops = CortexWatchdogOps(project="", client=client)

    assert await ops.complete("marketing", "done-1") is True
    assert await ops.release("marketing", "stuck-1", "no heartbeat") is True

    assert client.calls == [
        ("complete", "marketing", "done-1", ""),
        ("release", "marketing", "stuck-1", "", "no heartbeat"),
    ]


@pytest.mark.asyncio
async def test_escalate_creates_lead_handoff_as_resolved_project_lead():
    client = FakeClient()
    ops = CortexWatchdogOps(project="", client=client)

    ok = await ops.escalate(
        "marketing",
        {"id": "abcdef1234567890", "claimed_by": "saul@marketing"},
        "claimed 1800s, no success marker",
    )

    assert ok is True
    create = next(call for call in client.calls if call[0] == "create")
    _, project, from_agent, body = create
    assert project == "marketing"
    assert from_agent == "marlow"
    assert body["from_role"] == "lead"
    assert body["to_role"] == "lead"
    assert body["to_agent"] == "marlow"
    assert body["priority"] == "high"
    assert "[WATCHDOG-SIGNAL]" in body["summary"]
    assert "abcdef12" in body["summary"]


@pytest.mark.asyncio
async def test_escalate_reports_failed_cortex_create():
    client = FakeClient()
    client.created = {"ok": False, "error": "writer rejected"}
    ops = CortexWatchdogOps(project="", client=client)

    ok = await ops.escalate(
        "marketing", {"id": "abcdef1234567890"}, "stale run"
    )

    assert ok is False


@pytest.mark.asyncio
async def test_cortex_client_get_handoffs_passes_status_param():
    captured = {}

    class FakeHttp:
        async def get(self, path, headers=None, params=None):
            captured["path"] = path
            captured["params"] = params

            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"handoffs": []}

            return Response()

    from app.cortex_client import CortexClient

    client = CortexClient.__new__(CortexClient)
    client._client = FakeHttp()
    client._scoped_headers = lambda project: {"X-Project": project}

    await client.get_handoffs("marketing")
    assert captured["params"] is None

    await client.get_handoffs("marketing", status="claimed")
    assert captured["params"] == {"status": "claimed"}
