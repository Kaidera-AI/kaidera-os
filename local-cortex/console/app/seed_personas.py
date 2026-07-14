"""Default-persona seeder — stand up a fresh project's standard roster.

The persona model (2026-06-13): an interactive **Lead** (the agent you talk to — a
software-dev lead, a marketing CMO, any domain wears it) + autonomous **AI workers** (the
agreed worker terminology). A fresh project (e.g. a new project on a new VM) starts
fully-staffed via `seed_default_personas()` instead of hand-creating each agent.

This writes the APP-side config (harness/designation/role override) for each persona.
Registering the agents in the Cortex roster (so they show in `cortex-roster`) is the
caller's/CLI's step — pass a `register` callable to do both in one pass. The default
harness follows the deploy mode (kaidera when self-contained, claude-code on the Mac),
matching `agents.api._resolve_default_harness`.
"""

from __future__ import annotations

from typing import Any, Callable

from . import deploy_mode
from . import settings as settings_store

#: The standard starting roster — one Lead + the AI-worker specialists. `agent` is the
#: short handle; `role` is the domain hat; `persona` is the behavioural template.
DEFAULT_ROSTER: list[dict[str, str]] = [
    {"agent": "lead",   "designation": "interactive", "role": "lead",                 "persona": "Lead"},
    {"agent": "dev",    "designation": "autonomous",  "role": "full-stack-developer", "persona": "AI worker"},
    {"agent": "keeper", "designation": "autonomous",  "role": "knowledge-keeper",     "persona": "AI worker"},
    {"agent": "qa",     "designation": "autonomous",  "role": "qa",                   "persona": "AI worker"},
]


def default_harness() -> str:
    """The own-harness lane in the self-contained distributable; claude-code in dev mode.

    Mirrors `agents.api._resolve_default_harness` so a seeded roster matches what a
    hand-created fresh agent would default to.
    """
    return "kaidera" if deploy_mode.is_selfcontained() else "claude-code"  # fitness:allow-literal canonical harness id (own-harness runtime), not a per-project literal


def seed_default_personas(
    project: str,
    *,
    harness: str = "",
    overwrite: bool = False,
    save: Callable[[str, str, dict[str, Any]], Any] | None = None,
    register: Callable[[str, dict[str, str]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Seed the standard Lead + AI-worker roster for `project`.

    Idempotent by default: an agent that already has an app override is left untouched
    unless `overwrite=True`. Writes the harness/designation/role app config via `save`
    (defaults to `settings.save_agent_override`); when a `register` callable is given it
    is also invoked per agent to add it to the Cortex roster. Returns one result row per
    persona ({agent, action, persona, role, harness}).
    """
    h = harness or default_harness()
    save = save or settings_store.save_agent_override
    existing = settings_store.load_agent_overrides()
    results: list[dict[str, Any]] = []
    for row in DEFAULT_ROSTER:
        agent, role, designation, persona = row["agent"], row["role"], row["designation"], row["persona"]
        if not overwrite and existing.get(f"{project}:{agent}"):
            results.append({"agent": agent, "action": "exists", "persona": persona, "role": role})
            continue
        save(project, agent, {"harness": h, "designation": designation, "role": role})
        if register is not None:
            register(agent, {"role": role, "designation": designation})
        results.append(
            {"agent": agent, "action": "seeded", "persona": persona, "role": role, "harness": h}
        )
    return results


__all__ = ["DEFAULT_ROSTER", "default_harness", "seed_default_personas"]
