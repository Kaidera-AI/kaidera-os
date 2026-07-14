# Settings deep-dive

The Settings tab is the canonical operator surface for app-level runtime controls.
The bundled public help covers System, Cortex, Workspace, and Extensions. Project
dispatch/propose controls live on the Dashboard, and per-agent controls live in the
selected worker's Config panel. Everything durable is stored in the **app-DB**
(`app_settings`, `agent_settings`, `project_autonomy`) — the legacy JSON file is
only an upgrade seed and degraded fallback.

## System
- Typed system fields (Cortex connection, harness paths/flags, app preferences) live
  in `app_settings` as JSONB rows. `GET /settings/{project}/system-schema` returns
  the form; `POST /settings/{project}/app` upserts only the keys you changed.
- **Secrets are never rendered back.** A stored secret shows `•••• set`; a
  blank/masked submit means "leave unchanged" so you can edit non-secret fields
  without re-typing keys.
- App-wide autonomy engine settings belong here because they affect every project.
  Project-level dispatch controls do not; they belong on the Dashboard for the
  selected project.

## Cortex
- Connection status + layer health. A red Cortex read should be fixed before
  trusting graph, history, explain, or dashboard counts.
- Check: API reachable, Postgres connected, schema version, embedding backlog.
- Run-state restart health lives here too: detached worker rows are restart
  survivable; request-lived chat/approve rows are reconciled if their console PID
  disappears.

## Workspace
- One row per active project's `repo_root`. Project-scoped. Editing project A must
  never post the value for project B.
- `POST /settings/{project}/workspace` validates an absolute path and calls the
  admin-authed Cortex `set_project_repo_root`. A relative/blank path is rejected with
  a friendly error, never a 500.

## Extensions
- Installed project packs are discovered from the selected project's
  `.kaidera-os/project-packs/` directory.
- Enable/disable writes only the pack-owned `extensions.env` helper and reports
  loaded-vs-enabled drift. It does not dynamically import customer code at runtime.
- On restart, extension loading is explicit: `KAIDERA_OS_EXTENSION_MODULES` names
  pack-owned modules and `KAIDERA_OS_EXTENSION_PATHS` points at installed pack roots.
  Package code should use its own namespace, not the core console `app` package.
- Declared package portals are shown as health metadata: route prefix, target
  worker, auth strategy, stream contract, and whether the frontend asset exists
  inside the installed pack.

## Per-agent config (in the agent pane, not Settings)
- `POST /settings/{project}/agents/{agent}/config` saves a console-local override
- (designation/harness/model/reasoning/role/role_aliases/auto_dispatch) — MERGE
  semantics: a non-blank field sets it, a blank field clears it, an empty agent is
  dropped.
- **Role preset** controls capability: Interactive lead = chat + model,
  Non-interactive AI worker = model/no chat, Deterministic = no model/no chat.
- **Role** controls job: PM AI Agent, Orchestrator, developer, knowledge-keeper, etc.
- **Allow auto-run when assigned** controls whether this worker may execute handoffs assigned to
  it. It is separate from project dispatch.
- **Console-local by design.** A save does NOT touch the Cortex registry. Promote to
  the registry explicitly with the "Promote to registry" button
  (`POST /settings/{project}/agents/{agent}/promote`).

## Dashboard project controls
- **Project dispatch** (`project_autonomy`): when ON, the orchestrator may
  auto-run handoffs for this project. Fail-safe OFF — a down/absent DB reads OFF.
- **Propose mode** (`project_propose_mode`): when ON, dispatched work is queued for
  approval instead of launched. Use this first on new projects.
- Both are project-scoped and should be changed from the Dashboard for the selected
  project.

## How to configure (the order)
1. **Cortex** — confirm healthy (fix first).
2. **Workspace** — set repo roots for every project.
3. **Extensions** — inspect installed project-pack health when the project uses one.
4. **System** — set any typed preferences and app-level autonomy controls.
5. **Per-agent Config** — set role preset/role/harness/model per agent.
6. **Dashboard** — start with propose mode ON; turn project dispatch ON only when ready.

## Prerequisites
- [ ] App-DB connected (System shows `store_connected: true`)
- [ ] Cortex healthy

## Gotchas
- **Secret masking is round-trip safe only on the schema-driven save.** The raw
  upsert writes whatever you send — don't paste the `•••• set` mask back.
- **Project dispatch is not app-level autonomy.** The System toggle can stop global
  autonomy for all projects; the Dashboard toggle controls the selected project.

## Search keywords
settings, system, cortex, workspace, extensions, repo root, configure, app db, store_connected, secret, mask, dashboard, dispatch
