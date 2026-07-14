# Console SPA — the refined frontend client (Track C)

A single-page app that is a **pure thin client of the backend catalogs** the
Track A SDK modules expose. It renders the console's primary view —
**projects → agents + metrics → the selected agent's live run transcript** —
plus the **project-level views** (dispatch board / analytics / settings) reached
through a main-area switcher, over the typed REST + SSE API, with
**glass-morphism + Kaidera AI branding**.

> Track C, step 1: the foundation + the primary view. **Step 2 (this build):** the
> three project-level views + the main-area switcher (see
> [The main-area switcher + project views](#the-main-area-switcher--the-project-level-views)).
> Settings is **read-only** this step — the write path is the next step (see
> [Next steps](#next-steps)).

---

## Stack

| Concern | Choice |
|---|---|
| Framework | **React 19** + **TypeScript 6** |
| Build / dev | **Vite 8** (dev server + proxy, production bundle) |
| Styling | **Tailwind CSS v4** (`@tailwindcss/vite`, `@theme` tokens) |
| Tests | **Vitest 4** + Testing Library + jsdom |
| Lint | ESLint 9 (flat config) + `typescript-eslint` + react-hooks |

No runtime UI dependencies beyond React — the glass design system is a small set
of local components, and the API client is plain `fetch` + `EventSource`.

---

## The layout — canonical, no repetition (the CTO directive)

The defining rule (build-out roadmap, *UI/UX directive*): **each concern lives in
exactly ONE place** — no concern is duplicated across columns.

```
┌────────────┬──────────────────────┬──────────────────────────────┐
│  PROJECTS  │   AGENTS + METRICS   │  ⟨ Agent · Dispatch ·         │
│   (only)   │     (canonical)      │    Analytics · Settings ⟩    │  ← switcher
│            │                      │ ┌──────────────────────────┐ │
│ project    │ ▸ rollup metrics:    │ │ the SELECTED main view:  │ │
│ list /     │   agents · pending · │ │  Agent → detail + SSE run│ │
│ selector   │   runs               │ │  Dispatch → the board    │ │
│            │ ▸ Interactive group  │ │  Analytics → usage/cost  │ │
│            │ ▸ Autonomous group   │ │  Settings → flags + app  │ │
└────────────┴──────────────────────┴─└──────────────────────────┘─┘
  ProjectRail      AgentsColumn          MainArea (switcher + view)
```

- **Left column = `ProjectRail` — PROJECTS ONLY.** It lists projects and is the
  selector. It does **not** list agents, agent names, or settings. Its single
  job is "which project am I looking at".
- **2nd column = `AgentsColumn` — the ONE canonical home for AGENTS + METRICS.**
  The roster (Interactive vs Autonomous groups) **and** the project rollup
  (agents count, pending dispatch, active runs). The left column never repeats
  this.
- **Main area = `MainArea` — a switcher over ONE of four views.** A small
  segmented control (`Agent · Dispatch · Analytics · Settings`) in the main-area
  header picks what the main column shows: the selected agent's detail (the
  default), or one of the three **project-level** views. See the next section.

Each concern appears once: **project names** (left), **agent names + the rollup
metrics** (centre), and in the main area exactly one of the **selected agent's
runs/transcript** OR a **project-level** view — never a duplicate of a column.

---

## The main-area switcher + the project-level views

The dispatch board, analytics, and settings describe the **PROJECT**, not a single
agent and not a column concern — so the no-repeat rule keeps them **out of every
column** (the left stays projects-only, the 2nd stays agents+metrics) and reaches
them through a **clean segmented control in the main-area header**:

```
⟨ Agent · Dispatch · Analytics · Settings ⟩
```

`MainArea` (`src/features/MainArea.tsx`) owns the tab state and renders exactly ONE
view at a time. **Agent** is the default; selecting a project resets the switcher
back to **Agent** (a project-scoped view shouldn't persist across a project
switch — done by the "adjust state during render" pattern, no setState-in-effect).
The **Dispatch** tab carries a small live badge of the pending-dispatch count.

| Tab | View | Reads | Shows |
|---|---|---|---|
| **Agent** | `AgentDetail` | `/agents/.../detail` + the run board + SSE | the selected agent's detail + its live run (unchanged from step 1) |
| **Dispatch** | `DispatchView` | `GET /dispatch/{project}/board` | the open/pending handoff queue — each row's summary, from→to, priority, and the rule-based **proposed** agent (or `unassigned`); the board counts (waiting / proposed / unassigned / awaiting) + the autonomy & propose-mode flags. **Backend-sorted urgent-first.** |
| **Analytics** | `AnalyticsView` | `GET /analytics/{project}/usage` | the usage + est-cost breakdown as glass stat cards: a project rollup (total tokens, est. cost, agents-used, runs), usage-by-model & by-provider bars, and a per-agent token/cost table |
| **Settings** | `SettingsView` | `GET /settings/{project}/app` + `/flags` | the **single canonical settings surface** — the autonomy/propose-mode flags as on/off cards + the app/system key→value settings list. **READ-ONLY this step** (the write path is the next task) |

**How each honours no-repeat:**
- The switcher is the **only** place these project views are reachable — none is
  duplicated into a column.
- **Pending dispatch** still lives canonically as the `AgentsColumn` rollup pill;
  the Dispatch tab badge is the SAME number (sourced from the same board resource
  the shell already polls), surfaced on the tab as a glance affordance, not a
  second home for the metric.
- **Settings** is the one canonical settings home — deliberately absent from the
  primary view (step 1 noted this) and never split across columns.

**Graceful-degrade** rides through every view: a stale backend (a module route
404s — see [the live note](#a-backend-note-the-live-stale-build)) surfaces as the
view's error hint; a connected-but-empty board/usage/settings shows its empty
state; a down store shows the `store offline` / fail-safe-OFF affordance. None of
them crash. The shell fetches the project-scoped resources (board / usage / flags /
app-settings) and threads them in, so a project switch refreshes them with no extra
wiring in `MainArea`.

---

## The design system — glass morphism + Kaidera AI

`@theme` tokens in `src/index.css` define the palette; a few reusable components
in `src/components/` carry the surface treatment.

- **Dark base** with two ambient mint glows bleeding from the corners.
- **Mint / teal accent** (`--color-mint-*`) matched to the Kaidera AI logo's
  isometric-cube mark — the cube is reused verbatim as the SPA logo + favicon
  (`src/components/Logo.tsx`, `public/favicon.svg`, paths lifted from
  `app/static/kaidera-logo-official-white.svg`).
- **Translucent panels** — `.glass` / `.glass-soft` utilities: low-opacity fill,
  `backdrop-blur` + saturate, a hairline mint edge, and a faint inner top-glow.

Reusable components:

| Component | Role |
|---|---|
| `GlassPanel` | a full-height structural region (the columns) |
| `GlassCard` | an inset interactive surface (an agent row, a tile) |
| `StatPill` | a compact `value / LABEL` metric chip |
| `StatusDot` | the run-state indicator (queued / running / completed / errored), pulses when running |
| `BrandLockup` / `CubeMark` | the Kaidera AI cube + wordmark |

Tasteful, not gaudy: one accent hue, restrained glow, generous spacing.

---

## The API contract it consumes

The SPA reads the clean module JSON catalogs (Track A) + the live SSE. Types in
`src/api/types.ts` are transcribed **1:1 from the module `service.py` payloads**
(verified in-process against the live backend, not guessed). The typed client is
`src/api/client.ts`; the project is the context for every per-project call.

| Endpoint | Returns | Used by |
|---|---|---|
| `GET /projects` | `Project[]` (Cortex active projects) | `ProjectRail` |
| `GET /agents/{project}` | `AgentsCatalog` (interactive/autonomous + orchestrator + lead) | `AgentsColumn` |
| `GET /agents/{project}/{agent}/detail` | `AgentDetail` (resolved view + designation + config-view) | `AgentDetail` header |
| `GET /runs/{project}` | `RunBoard` (active + recent + counts) | metrics + the agent run rail |
| `GET /runs/run/{run_id}` | `RunTranscript` (one run WITH body) | the REST first-paint transcript |
| `GET /runs/{project}/by-handoff/{hid}` | `RunTranscript` | (client method; next-step use) |
| `GET /dispatch/{project}/board` | `DispatchBoard` (queue + counts + flags) | `pending` metric (AgentsColumn) **+ the Dispatch view** |
| `GET /analytics/{project}/usage` | `UsageBreakdown` (usage + est-cost rollups) | **the Analytics view** |
| `GET /settings/{project}/app` · `/flags` | `AppSettings` · `ProjectFlags` | **the Settings view (read-only)** |
| **SSE** `GET /runstate/stream?project=&agent=&run=` | `event: runstate` → `RunStateFrame` | `useRunStateStream` → the live transcript |

### The live channel — the thin-SSE-client model

`useRunStateStream` (`src/api/useRunStateStream.ts`) subscribes to
`/runstate/stream` scoped to `(project, agent[, run])`. Each `event: runstate`
frame carries a **fresh read-model** (`RunStateFrame.selected` — the same
view-model the REST first-paint uses, so they cannot disagree). The hook surfaces
the newest frame; `AgentDetail` prefers it and falls back to the REST
`GET /runs/run/{id}` transcript until the first frame arrives. State is
key-scoped so a stale transcript never leaks across an agent/project switch.

`useResource` (`src/api/useResource.ts`) wraps the REST catalogs with
loading/error state + a gentle poll (the snapshots — counts + the rail — refresh
on a light interval; the live transcript itself is SSE-pushed, never polled).

---

## Develop / build

```bash
cd local-cortex/console/spa
npm install

# Dev server on :5173, proxying the backend catalogs/SSE to 127.0.0.1:8765.
npm run dev          # → http://localhost:5173

npm run build        # tsc -b + vite build → dist/
npm run preview      # serve the production build locally

npm run lint         # eslint (flat config)
npm run typecheck    # tsc -b --noEmit
npm run test         # vitest run (unit + hook + client tests)
```

### Dev proxy

The Vite dev server (`vite.config.ts`) proxies these path prefixes to the running
console backend so the SPA runs on its own port in dev with same-origin
`fetch()` / `EventSource`:

```
/projects  /agents  /runs  /dispatch  /analytics  /settings  /runstate  →  http://127.0.0.1:8765
```

In production the SPA is served by the console itself (same origin), so the same
relative paths resolve without a proxy.

---

## Structure

```
spa/
├── index.html                 # dark theme, Kaidera AI title + cube favicon
├── vite.config.ts             # React + Tailwind plugins, dev proxy, vitest config
├── public/favicon.svg         # the mint cube on a dark tile
└── src/
    ├── main.tsx               # React root
    ├── App.tsx                # the shell — owns (project, agent) state, fetches catalogs
    ├── index.css              # Tailwind v4 @theme tokens + glass utilities
    ├── api/
    │   ├── types.ts           # typed view of the module JSON (1:1 with service.py)
    │   ├── client.ts          # the typed REST client (fetch)
    │   ├── useRunStateStream.ts  # the SSE hook (live transcript)
    │   ├── useResource.ts     # REST fetch hook (loading/error + poll)
    │   └── index.ts           # barrel
    ├── components/
    │   ├── glass.tsx          # GlassPanel / GlassCard / StatPill / StatusDot
    │   ├── ui.ts              # cx() + statusKind() helpers (non-component)
    │   └── Logo.tsx           # CubeMark + BrandLockup
    ├── features/
    │   ├── ProjectRail.tsx    # LEFT — projects only
    │   ├── AgentsColumn.tsx   # 2nd — agents + metrics (canonical)
    │   ├── MainArea.tsx       # MAIN — the switcher + the active view
    │   ├── AgentDetail.tsx    # MAIN/Agent — selected agent + live run
    │   ├── RunTranscriptView.tsx  # the transcript renderer
    │   ├── DispatchView.tsx   # MAIN/Dispatch — the open-handoff board
    │   ├── AnalyticsView.tsx  # MAIN/Analytics — usage + est-cost
    │   └── SettingsView.tsx   # MAIN/Settings — flags + app settings (read-only)
    ├── state/
    │   └── useSelection.ts    # (project, agent) selection + URL-hash sync
    └── test/setup.ts          # vitest + Testing Library setup
```

---

## Next steps

- **Settings WRITE path** — the next task. Turn `SettingsView` from read-only into
  a write surface: toggle the autonomy / propose-mode flags + edit the app/system
  settings, posting through the settings module's write endpoints. The read view +
  the canonical-surface placement built here are the foundation.
- **Polish + full live QA** — once a Track-A-current backend is running on :8765
  (the module JSON routes live), do an end-to-end live pass of all four views
  against real data (the unit tests cover render + every degrade branch with
  fixtures; the live pass is the remaining confidence step).

### A backend note: the live stale build

`GET /projects` as a **JSON** route + the module JSON routers (`/agents`, `/runs`,
`/dispatch`, `/analytics`, `/settings`) are the contract the SPA consumes. They are
mounted in `app/main.py`, **but the process currently running on :8765 is a stale
pre-Track-A build** — its `/` still serves the old HTML console (200) while every
module JSON route 404s. That is expected and **handled by graceful degrade**: each
view shows its error hint (not a crash) on a 404, so a stale server is never read
as a code bug. Confirm a Track-A-current console build is running before the live
QA pass.
