"""Example project-pack extension module.

Real packs can expose optional hooks such as registered_agent_routing_override.
This module intentionally does nothing unless explicitly enabled by deployment
environment, proving pack code can live outside Kaidera OS core.
"""

from __future__ import annotations


def registered_agent_routing_override(agent_name: str, project_key: str, model, reasoning):
    """No-op example hook; real packs may return a harness/model/reasoning tuple."""
    return None
