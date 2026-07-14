"""Project-pack portal runtime contracts.

This module is intentionally project-agnostic. A package can declare a thin
portal, but Kaidera OS owns the runtime endpoints and stream semantics. Keeping the
contract here prevents each customer portal from rediscovering or hardcoding how
to follow harness output.
"""

from __future__ import annotations

from typing import Any


RUNSTATE_SSE_CONTRACT = "runstate-sse"


def runstate_sse_contract(*, agent: str | None = None) -> dict[str, Any]:
    """Return the canonical thin-portal stream contract.

    The placeholders are deliberate: the installed pack may have a default
    project, but the active deployment chooses the project key at runtime. The
    chat POST emits an ``event: run`` frame with ``{"run_id": "..."}``; portals
    then open the run-pinned run-state SSE stream so refresh/reconnect replays
    the same transcript the admin console uses.
    """
    agent_value = (agent or "{agent}").strip() or "{agent}"
    return {
        "contract": RUNSTATE_SSE_CONTRACT,
        "chat_endpoint_template": f"/agents/{{project}}/{agent_value}/chat",
        "stream_endpoint_template": "/runstate/stream?project={project}&run={run_id}",
        "run_endpoint_template": "/runs/run/{run_id}",
        "chat_events": ["run", "error", "done"],
        "stream_events": ["runstate"],
        "selected_payload": {
            "path": "data.selected",
            "segments_path": "data.selected.segments",
            "segment_fields": ["kind", "text"],
            "status_path": "data.selected.status",
        },
        "rules": [
            "Send chat messages to chat_endpoint_template.",
            "Read run_id from the chat POST event named run.",
            "Open stream_endpoint_template with that run_id for durable replay.",
            "Render every selected.segments entry in order; do not infer output from the POST body.",
        ],
    }


def contract_for_stream(stream_contract: str | None, *, agent: str | None = None) -> dict[str, Any] | None:
    """Map a manifest stream_contract value to the Kaidera OS-owned runtime contract."""
    if (stream_contract or "").strip() == RUNSTATE_SSE_CONTRACT:
        return runstate_sse_contract(agent=agent)
    return None


__all__ = [
    "RUNSTATE_SSE_CONTRACT",
    "contract_for_stream",
    "runstate_sse_contract",
]
