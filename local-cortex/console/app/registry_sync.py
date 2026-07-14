"""Override→registry EXPLICIT PROMOTE — feature-gap #81 (the CTO's reversed choice).

The console keeps a fast, console-local app-DB override store (keyed
`{project}:{agent}`) for an agent's harness/model/reasoning/designation/role. By
DESIGN these overrides stay console-LOCAL: the Cortex registry (`capabilities`, E006
Inc04) stays authoritative, and a config SAVE writes ONLY the local override (a
registry round-trip on every keystroke would be slow + couple the UI to Cortex, and
silently mutating the source-of-truth on every edit erases a deliberate boundary).

PROMOTION is an EXPLICIT, on-demand gesture: a "Promote to registry" action pushes
ONE agent's CURRENT effective config (harness/model/reasoning/writer_scope) + its
current role INTO the registry when the operator chooses to commit it — never
automatically on save.

LOAD-BEARING (verified against `.agents/api/main.py` `register_agent`): the
`POST /agents` conflict-update does
    capabilities = COALESCE(agents.capabilities,'{}'::jsonb) || EXCLUDED.capabilities
— a jsonb MERGE. So re-registering an agent with the merged capabilities PERSISTS
harness/model/reasoning/writer_scope additively (matching keys overwrite, others
keep). The promote therefore works directly via `CortexClient.create_agent` — no
new Cortex endpoint is needed.

BEST-EFFORT is the contract: the explicit-promote endpoint
(`POST /settings/{project}/agents/{agent}/promote` in `settings_module/api.py`) calls
``promote_agent_to_registry`` and surfaces the soft outcome to the operator
(`{ok, error?}`); a registry-write failure (caller-not-a-writer / down Cortex) is a
graceful False, never a 500. The console-local override is untouched either way.

This module is PURE-ish: `build_registry_sync_payload` is I/O-free; the async
`promote_agent_to_registry` does exactly one optional Cortex write through the
injected client and swallows every failure. It imports nothing outward (no fastapi/
httpx) — the client is duck-typed (anything with an async `create_agent`).
"""

from __future__ import annotations

import re
from typing import Any

# The override/capability CONFIG fields that carry an agent's execution routing.
# These map 1:1 from a saved override into the registry `capabilities` blob (they
# are capability FIELD names, not project keys / agent names).
_CONFIG_FIELDS = ("harness", "model", "reasoning", "role_aliases", "auto_dispatch")


def _clean_str(value: Any) -> str:
    """A stripped string, or '' for None/blank/whitespace. Total + pure."""
    return str(value).strip() if value is not None else ""


def _clean_role(value: Any) -> str:
    """Registry role slug derived from the UI-facing role field."""
    raw = _clean_str(value).lower()
    return re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")


def build_registry_sync_payload(
    override: dict[str, Any] | None,
    agent_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """Map a saved EFFECTIVE override (+ the agent's registry record) onto the
    ``{role, capabilities, writer_scope}`` an `AgentRegister` UPSERT needs.

    * role          — the override's role if set, else the agent's current registry
                      role (create_agent REQUIRES a role; we never synthesise a blank
                      here — an unresolvable role yields "" and the caller skips).
    * capabilities  — the agent's existing capabilities MERGED with the override's
                      NON-blank config fields (harness/model/reasoning). A blank field
                      (cleared in the override) is NOT pushed, so it can't clobber the
                      registry value on the jsonb merge. writer_scope is folded in too
                      when overridden.
    * writer_scope  — surfaced as the top-level register field when overridden (so
                      create_agent forwards it); absent otherwise.

    Pure + total: tolerates a None override / None record (→ empty role, empty-ish
    capabilities) and never raises.
    """
    ov = dict(override or {})
    rec = dict(agent_record or {})

    # Start from the agent's existing capabilities so the merge is additive (mirrors
    # the API's jsonb `||` — keep everything, overwrite only the changed keys).
    existing_caps = rec.get("capabilities")
    capabilities: dict[str, Any] = dict(existing_caps) if isinstance(existing_caps, dict) else {}

    for field in _CONFIG_FIELDS:
        val = _clean_str(ov.get(field))
        if val:
            capabilities[field] = val

    writer_scope = _clean_str(ov.get("writer_scope"))
    if writer_scope:
        capabilities["writer_scope"] = writer_scope

    role = _clean_role(ov.get("role")) or _clean_role(rec.get("role"))

    payload: dict[str, Any] = {"role": role, "capabilities": capabilities}
    if writer_scope:
        payload["writer_scope"] = writer_scope
    return payload


async def promote_agent_to_registry(
    cortex: Any,
    project_key: str,
    agent: str,
    override: dict[str, Any] | None,
    agent_record: dict[str, Any] | None,
) -> bool:
    """BEST-EFFORT push of an agent's current effective override to the Cortex
    registry — the EXPLICIT "Promote to registry" write.

    Builds the payload via `build_registry_sync_payload` and calls
    ``cortex.create_agent`` (the `POST /agents` UPSERT) to re-register the agent with
    the merged capabilities + its current role. Returns True iff the registry write
    landed; otherwise a SOFT False.

    GRACEFUL-DEGRADE contract: returns False WITHOUT a call on a blank agent/project,
    a None cortex client, or an unresolvable role (we never POST a blank role — the
    API would 400). ANY failure of the write itself — create_agent returning None (its
    own graceful-degrade), or even an unexpected raise (belt-and-braces) — is swallowed
    and returned as False. This function NEVER raises, so the promote endpoint reports
    a soft outcome rather than 500-ing, and the console-local override is untouched.
    """
    proj = _clean_str(project_key)
    name = _clean_str(agent)
    if not proj or not name or cortex is None:
        return False

    payload = build_registry_sync_payload(override, agent_record)
    role = payload.get("role") or ""
    if not role:
        # No role to register against — skip rather than POST an invalid body.
        return False

    try:
        # Caller = the SUBJECT agent itself, NOT the console's fixed identity. CONSOLE_AGENT
        # defaults to one configured writer name, which is not a registered writer on an
        # arbitrary turnkey project, so POST /agents was rejected ("writer not authorised on
        # this project"). For a promote the subject is an EXISTING registered project writer, so
        # it UPSERTs its own registry record — works on any project with no project-specific
        # writer name baked in.
        result = await cortex.create_agent(
            proj,
            name=name,
            role=role,
            capabilities=payload.get("capabilities") or {},
            writer_scope=payload.get("writer_scope"),
            caller=name,
        )
    except Exception:
        # Belt-and-braces: create_agent already graceful-degrades to None, but the
        # promote must NEVER bubble an exception into the endpoint (it would 500).
        return False
    return result is not None


__all__ = ["build_registry_sync_payload", "promote_agent_to_registry"]
