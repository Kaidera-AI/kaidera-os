# Bring your first project online

A "project" in Kaidera OS is a workspace key + a repo root + a roster of agents + a
Cortex memory namespace. This guide onboards one end to end.

## 1. Register the project
- In the SPA: **Project Rail → + Add project** (a glass form: `project_key` +
  `display_name` + an absolute `repo_root`). This calls `POST /projects` (admin-gated
  when auth is on).
- Or from the CLI: `cortex-add-agent` / the project onboarding CLIs.
- The `project_key` is the slug used everywhere (`CORTEX_PROJECT`, handoff routing,
  agent identity `agent@project`). Pick something stable — renaming is painful.

## 2. Set the repo root
- **Settings → Workspace** lists every active project. Save each repo root on its
  OWN row. Repo roots are project-scoped — editing one project must never post the
  selected value for another row.
- The repo root is the folder the harness + `cortex-boot` run IN. Explain, graph, and
  workspace-scoped tools resolve paths from it. A wrong/missing repo root makes
  explain/graph return empty and workers run in the console's own folder.
- Verify: Dashboard for that project shows the repo root and a green Cortex read.

## 3. Seed the roster
- A fresh project has no agents. Add the first agent:
  **Agents column → + Add agent** (name + role + harness/model/reasoning +
  designation + writer_scope) → `POST /agents/{project}/register` (a writer-gated
  UPSERT that jsonb-merges capabilities, so re-registering is additive).
- The first agent is usually the **interactive lead** (the one you chat with). Mark
  it `designation: interactive`.
- Add **AI workers** for execution (e.g. a full-stack developer, a knowledge-keeper).
  Use the **AI worker** role preset.
- Optional: add a **Deterministic worker** or **Orchestrator** preset for packaged
  code tasks.

## 4. Pick the default agent
- The project's `default_agent` is where the console lands on project switch and the
  onboarding lead. Set it via the project metadata (the registration form's default
  agent, or `POST /projects` update). Usually your interactive lead.

## 5. Configure each agent
- Open an agent → **Config** → set role preset, role, harness, model, reasoning, and
  Allow auto-run when assigned. Use the Role preset dropdown for common roles like PM AI Agent
  and Orchestrator.
- **Promote** only when a console-local override should become registry data. Day-to-
  day config stays console-local (the registry is the source of truth; a save does
  NOT push to it).

## 6. Bring Cortex online for the project
- Confirm the project is registered in Cortex: `GET /projects/{key}` (the Dashboard
  does this). A missing project means Cortex reads 404 and the agent has no memory.
- Seed any knowledge the agents need: decisions, lessons, knowledge docs, and
  project packs.

## 7. Start controlled autonomy (later)
- Do NOT enable project dispatch on a brand-new project. Use **Dispatch with propose
  mode** first — work queues for your approval instead of launching. Approve manually
  until the roster, repo roots, and worker configuration are trusted.

## Onboarding order (the cheat sheet)
1. Register project → 2. Set repo root → 3. Seed roster (lead first) → 4. Default
   agent → 5. Configure agents → 6. Confirm Cortex → 7. Propose-mode dispatch.

## Prerequisites
- [ ] Project folder exists and is readable
- [ ] Console + Cortex + app-DB healthy (Getting Started checklist)
- [ ] At least one allowed harness/model route available for your edition

## Gotchas
- **Repo root must be absolute.** A relative path is rejected.
- **The roster is the Cortex registry, not a local file.** `get_agents` reads
  `/projects/{key}/runtime`; the console override store only layers designation/
  harness/model/role/role_aliases on top.
- **Role aliases for routing.** If you address handoffs to a role no agent literally
  holds (e.g. `creative-multimedia`), add a `role_aliases` capability/override to the
  agent that should own it.

## Search keywords
project, register, repo root, roster, default agent, onboarding, first project, register agent, project key
