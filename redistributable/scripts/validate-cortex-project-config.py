#!/usr/bin/env python3
"""Validate Cortex redistributable project config without external packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


STANDARD_ROLES = {
    "lead",
    "worker",
    "cpo",
    "backend-specialist",
    "frontend-specialist",
    "full-stack-senior-developer",
    "knowledge-keeper",
    "qa-testing",
    "cortex-architect",
    "generalist",
    "orchestrator",
    "designer"
}
VALID_HARNESSES = {
    "kaidera",
    "codex",
    "claude",
    "claude-code",
    "gemini",
    "pi",
    "manual",
    "none",
}
VALID_COLORS = {"blue", "cyan", "green", "magenta", "red", "yellow", "white"}
REQUIRED_MODEL_TYPES = {"llm", "embedding", "reranking"}
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"ERROR: config not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {path}: {exc}")


def require(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def validate(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    require(config.get("schema_version") == "1.0", errors, "schema_version must be 1.0")

    project = config.get("project")
    require(isinstance(project, dict), errors, "project must be an object")
    if isinstance(project, dict):
        key = project.get("key")
        require(isinstance(key, str) and bool(SLUG_RE.match(key)), errors, "project.key must be a slug")
        for field in ("name", "root", "team_name"):
            require(isinstance(project.get(field), str) and bool(project[field].strip()), errors, f"project.{field} is required")
        for field in ("profile_globs", "knowledge_globs"):
            value = project.get(field)
            if value is not None:
                require(isinstance(value, list), errors, f"project.{field} must be an array when present")
                if isinstance(value, list):
                    for index, item in enumerate(value):
                        require(isinstance(item, str) and bool(item.strip()), errors, f"project.{field}[{index}] must be a non-empty string")

    roles = config.get("roles", [])
    require(isinstance(roles, list), errors, "roles must be an array when present")
    role_slugs: set[str] = set(STANDARD_ROLES)
    if isinstance(roles, list):
        for index, role in enumerate(roles):
            require(isinstance(role, dict), errors, f"roles[{index}] must be an object")
            if not isinstance(role, dict):
                continue
            slug = role.get("slug")
            require(isinstance(slug, str) and bool(SLUG_RE.match(slug)), errors, f"roles[{index}].slug must be a slug")
            if isinstance(slug, str):
                role_slugs.add(slug)
            require(isinstance(role.get("label"), str) and bool(role["label"].strip()), errors, f"roles[{index}].label is required")

    agents = config.get("agents")
    require(isinstance(agents, list) and bool(agents), errors, "agents must be a non-empty array")
    agent_names: set[str] = set()
    pane_titles: set[str] = set()
    if isinstance(agents, list):
        for index, agent in enumerate(agents):
            require(isinstance(agent, dict), errors, f"agents[{index}] must be an object")
            if not isinstance(agent, dict):
                continue
            name = agent.get("name")
            require(isinstance(name, str) and bool(SLUG_RE.match(name)), errors, f"agents[{index}].name must be a slug")
            if isinstance(name, str):
                require(name not in agent_names, errors, f"duplicate agent name: {name}")
                agent_names.add(name)
            role = agent.get("role")
            require(isinstance(role, str) and bool(SLUG_RE.match(role)), errors, f"agents[{index}].role must be a slug")
            if isinstance(role, str):
                require(role in role_slugs, errors, f"agent {name or index} references unknown role: {role}")
            harness = agent.get("harness")
            require(harness in VALID_HARNESSES, errors, f"agent {name or index} harness must be one of {sorted(VALID_HARNESSES)}")
            pane = agent.get("pane")
            require(isinstance(pane, dict), errors, f"agent {name or index} pane must be an object")
            if isinstance(pane, dict):
                title = pane.get("title")
                require(isinstance(title, str) and bool(title.strip()), errors, f"agent {name or index} pane.title is required")
                if isinstance(title, str):
                    require(title not in pane_titles, errors, f"duplicate pane title: {title}")
                    pane_titles.add(title)
                color = pane.get("color")
                require(color in VALID_COLORS, errors, f"agent {name or index} pane.color must be one of {sorted(VALID_COLORS)}")

    model_requirements = config.get("model_requirements")
    require(isinstance(model_requirements, list), errors, "model_requirements must be an array")
    model_types: set[str] = set()
    if isinstance(model_requirements, list):
        for index, requirement in enumerate(model_requirements):
            require(isinstance(requirement, dict), errors, f"model_requirements[{index}] must be an object")
            if not isinstance(requirement, dict):
                continue
            kind = requirement.get("type")
            require(isinstance(kind, str), errors, f"model_requirements[{index}].type is required")
            if isinstance(kind, str):
                model_types.add(kind)
            require(isinstance(requirement.get("required"), bool), errors, f"model_requirements[{index}].required must be boolean")
            require(isinstance(requirement.get("purpose"), str) and bool(requirement["purpose"].strip()), errors, f"model_requirements[{index}].purpose is required")
    require(REQUIRED_MODEL_TYPES.issubset(model_types), errors, "model_requirements must include llm, embedding, and reranking")

    beat = config.get("beat")
    require(isinstance(beat, dict), errors, "beat must be an object")
    if isinstance(beat, dict):
        orchestrator = beat.get("orchestrator_agent")
        require(isinstance(orchestrator, str) and orchestrator in agent_names, errors, "beat.orchestrator_agent must reference an agent name")
        cadence = beat.get("cadence_minutes")
        require(isinstance(cadence, int) and cadence > 0, errors, "beat.cadence_minutes must be a positive integer")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Cortex redistributable project config.")
    parser.add_argument("config", help="Path to a cortex project config JSON file.")
    args = parser.parse_args(argv)

    config = load_json(Path(args.config))
    if not isinstance(config, dict):
        print("ERROR: config root must be an object", file=sys.stderr)
        return 1
    errors = validate(config)
    if errors:
        print("ERROR: invalid cortex project config:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"Valid Cortex project config: {args.config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
