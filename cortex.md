# Cortex Session Rules

## Purpose

This file defines project-agnostic operating rules for Kaidera OS workers.
It must not name a default customer, project, local workspace, or worker roster.
Live project identity comes from Cortex at runtime.

## Source Of Truth

Use this order when context conflicts:

1. The current user instruction.
2. The active `CORTEX_PROJECT` and live `cortex-boot <worker>` output.
3. The project's Cortex registry row, runtime profile, roster, and workspace root.
4. This rules file.
5. Imported reference material.

Never infer the current project from this repository name, a historical document,
or another deployment's generated files.

## Session Start

```bash
export PATH="$PATH:$(pwd)/.agents/scripts"
export CORTEX_PROJECT=<project-key>
cortex-boot <worker-name>
```

If `CORTEX_PROJECT` is missing, stop and set it explicitly. Kaidera OS must fail
closed instead of guessing a baked project key.

## First Project And Add Project Flow

A fresh install starts with no baked project and no baked workers.

The first project is created from first-screen startup inputs:

- project name and key
- workspace root
- project scope
- first lead worker name and display name
- team template
- provider settings

Additional projects are created through the Add Project flow or the projects API.
Generated runtime files are deployment state and must not be committed:

- `.agents/config/runtime.yaml`
- `.agents/config/workspace.json`
- `.agents/config/beat.env`
- generated worker identity files
- local Cortex logs, memory mirrors, and runtime state

## Project Isolation

Every command, memory write, handoff, run, and workspace operation belongs to one
explicit Cortex project.

- Do not read or write another project's Cortex rows unless the user gives a
  specific one-command override.
- Do not borrow another project's worker names, roles, settings, repo root,
  handoffs, or memory.
- Do not ingest this repository root as project memory. Point ingestion at an
  explicit project/customer corpus directory.
- Treat deployment packs and extensions as separate from core Kaidera OS.

## Worker Identity

Human-readable identity is:

```text
<worker-name>@<project-key>
```

Use that form in log summaries, handoff summaries, progress notes, and blocked
notices. CLI arguments use the plain worker name.

Do not construct or store legacy short project hashes. If docs and live Cortex
disagree, trust live Cortex.

## Cortex Access

Use API-backed CLI commands or HTTP endpoints.

Do not directly mutate Cortex Postgres, worker containers, or generated files as
a normal workflow. If the API does not expose a required operation, treat that as
a tooling gap and add the endpoint or operator command.

Normal surfaces:

- boot: `cortex-boot <worker>`
- handoffs: `cortex-handoff --mine <worker>`, `--claim`, `--complete`
- logs: `cortex-log <worker> decision|lesson|progress "..."`
- search: `cortex-search "query"`
- project state: `cortex-projects`, `cortex-roster`, `cortex-state`

## Handoff Discipline

Before editing, read the current boot context and handoff queue.

- Claim work before changing code or docs.
- Keep edits scoped to the claimed handoff or explicit user instruction.
- Run focused verification before completing.
- Complete only after evidence exists.
- If blocked, create a consult handoff with concrete options.
- If no relevant work exists, idle cleanly instead of inventing work.

## Runtime Boundary

Kaidera OS core owns:

- harness runtime
- provider settings
- Cortex API integration
- project and worker registration
- run state and transcript streaming
- package verification and update plumbing

Kaidera OS core does not own:

- customer-specific knowledge
- customer-specific frontend wrappers
- project-specific worker prompts
- generated deployment state
- legacy autonomous launchers

Project extensions must be loaded explicitly through deployment configuration,
not imported by core by default.

## Heartbeat

The retained `beat` directory is heartbeat/control plumbing only. It must not
ship named worker loops or baked project launchers. If a deployment needs
autonomous scheduling, it must be configured from project data and first-screen
inputs, not from source defaults.

## Redis And Data Stores

The Cortex stack shipped with Kaidera OS is Postgres-backed. Do not reintroduce Redis as a
required runtime dependency for core memory, events, or queue state.

## Session End

Record durable context when work materially changes the project:

```bash
cortex-log <worker> decision "<worker>@<project> completed <work>; evidence: <test or check>"
```

Use handoffs for coordination and decisions/lessons for durable memory. Keep
summaries concise, project-scoped, and free of another deployment's assumptions.
