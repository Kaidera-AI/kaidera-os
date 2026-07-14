/**
 * App — the console SPA shell. The CANONICAL three-column layout (no repetition):
 *
 *   ┌──────────┬───────────────┬────────────────────────────┐
 *   │ PROJECTS │ AGENTS+METRICS│ ⟨switcher⟩ + the main view  │
 *   │  (only)  │  (canonical)  │  Dashboard·Agent·Dispatch·  │
 *   │          │               │  Analytics·History·Graph·   │
 *   │          │               │  Explain·Settings           │
 *   └──────────┴───────────────┴────────────────────────────┘
 *
 * Each concern lives in exactly ONE place: project names (left), agent names + the
 * rollup metrics (centre), and in the main area exactly one of the selected
 * agent's runs/transcript OR a PROJECT-LEVEL view (dashboard / dispatch /
 * analytics / history / graph / explain / settings) reached through the main-area
 * switcher (MainArea) — never a duplicate of a column. The shell owns the
 * selected-project / selected-agent state and fetches ALL the shared +
 * project-scoped catalogs; the columns + the views are pure-presentational over
 * that data.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, useDispatchRun, useResource } from './api'
import { ProjectRail } from './features/ProjectRail'
import { AgentsColumn } from './features/AgentsColumn'
import { MainArea } from './features/MainArea'
import { WorkspaceColumn } from './features/WorkspaceColumn'
import type { MainView } from './features/MainArea'
import { OnboardingView } from './features/OnboardingView'
import { useSelection } from './state/useSelection'
import type {
  AgentEpicsPayload,
  AgentsCatalog,
  AnalyticsKpis,
  AppVersion,
  AppSettings,
  DispatchActivity,
  DispatchBoard,
  Project,
  ProvidersCatalog,
  ProvidersConfig,
  RunBoard,
  SkillsPayload,
  SystemSchema,
  UpdateJob,
  UpdateStatus,
  UsageBreakdown,
  Whoami,
} from './api'

// Light poll cadence for the snapshot catalogs (the live transcript is pushed by
// SSE, so these stay gentle — just enough to keep counts + the rail current).
const POLL_CATALOG = 8000
const POLL_BOARD = 6000
// Usage + settings shift slowly — a gentler refresh keeps the project views fresh
// without churn (they're shaped from durable rollups / a key→value store).
const POLL_USAGE = 15000
const POLL_SETTINGS = 20000
const POLL_VERSION = 60000
const POLL_UPDATE_JOB = 5000

export default function App() {
  const { project: selectedProject, agent, selectProject, selectAgent } = useSelection()

  // Global build stamp (same source as FastAPI/Jinja); rendered as a dedicated version
  // line under the brand/name box in the rail header (passed into ProjectRail below).
  const versionRes = useResource<AppVersion>(
    (signal) => api.appVersion(signal),
    [],
    { pollMs: POLL_VERSION },
  )
  const updateStatusRes = useResource<UpdateStatus>(
    (signal) => api.updateStatus(signal),
    [],
    { pollMs: POLL_VERSION },
  )
  const whoamiRes = useResource<Whoami>((signal) => api.whoami(signal), [], { pollMs: POLL_VERSION })
  const canManageUpdates = whoamiRes.data?.is_admin === true
  const updateJobRes = useResource<UpdateJob>(
    canManageUpdates ? (signal) => api.updateJob(signal) : null,
    [canManageUpdates],
    { pollMs: POLL_UPDATE_JOB },
  )

  // -- the project list (left column) ---------------------------------------
  const projectsRes = useResource<Project[]>(
    (signal) => api.projects(signal),
    [],
    { pollMs: 30000 },
  )
  const projects = useMemo(() => projectsRes.data ?? [], [projectsRes.data])
  const projectKeys = useMemo(() => new Set(projects.map((p) => p.project_key)), [projects])
  const projectsRouteUnavailable =
    !projectsRes.loading && !projectsRes.data && projectsRes.error !== null
  // The URL hash is only a candidate. Once /projects is known, project-scoped
  // resources must use a registered project so stale hashes (old renamed keys)
  // cannot keep driving 404ing API calls. If /projects itself is unavailable,
  // keep the legacy deep-link behavior because there is no authoritative list.
  const project =
    selectedProject && (projectKeys.has(selectedProject) || projectsRouteUnavailable)
      ? selectedProject
      : null

  // (Project auto-select lives BELOW — it reads the app-settings to honor the
  //  configured DEFAULT project on first paint; see the effect after appSettingsRes.)

  // -- the per-project catalogs (centre + main) -----------------------------
  const catalogRes = useResource<AgentsCatalog>(
    project ? (signal) => api.agents(project, signal) : null,
    [project],
    { pollMs: POLL_CATALOG },
  )
  // The col-2 Active-Epic widget + metrics block (per-epic progress + the project metric
  // counters). Degrades to null on a stale-backend 404 (AgentsColumn shows '—' + hides the epic).
  const epicsRes = useResource<AgentEpicsPayload>(
    project ? (signal) => api.agentEpics(project, signal) : null,
    [project],
    { pollMs: POLL_CATALOG },
  )
  const runBoardRes = useResource<RunBoard>(
    project ? (signal) => api.runBoard(project, signal) : null,
    [project],
    { pollMs: POLL_BOARD },
  )
  const dispatchRes = useResource<DispatchBoard>(
    project ? (signal) => api.dispatchBoard(project, signal) : null,
    [project],
    { pollMs: POLL_BOARD },
  )
  // Orchestrator activity feed + the wave plan (the Dispatch view's live feed). Degrades to
  // null on a stale-backend 404 (the DispatchView shows the idle/empty hint).
  const dispatchActivityRes = useResource<DispatchActivity>(
    project ? (signal) => api.dispatchActivity(project, signal) : null,
    [project],
    { pollMs: POLL_BOARD },
  )
  // The Approve & Run SSE controller lives HERE (the shell) so an in-flight streamed
  // run survives the dispatch board's 6s poll/refetch (the per-row run state is keyed
  // by handoff id in the hook, not rebuilt from the board).
  const dispatchRunCtl = useDispatchRun({ project })
  // After any dispatch write (autonomy toggle / approve gate) refetch the board +
  // activity so the surface lands on the authoritative post-write state.
  const onDispatchChanged = useCallback(() => {
    dispatchRes.refetch()
    dispatchActivityRes.refetch()
  }, [dispatchRes, dispatchActivityRes])
  // Project-level views reached via the main-area switcher (dispatch reuses the
  // board above for its pending badge + the Dispatch view itself).
  const usageRes = useResource<UsageBreakdown>(
    project ? (signal) => api.usage(project, signal) : null,
    [project],
    { pollMs: POLL_USAGE },
  )
  // The Analytics headline KPI strip (events/24h · active tasks · decisions · recent tokens).
  // Cortex-sourced (stands even when the usage store is offline); degrades to null on a 404.
  const kpisRes = useResource<AnalyticsKpis>(
    project ? (signal) => api.analyticsKpis(project, signal) : null,
    [project],
    { pollMs: POLL_USAGE },
  )
  // The skills catalogue (global + this project's) — the Skills tab's read. Reached via the
  // main-area switcher; degrades to null on a stale-backend 404 (SkillsView shows the empty
  // catalogue + the install bar). Refetched after an install/bind so the new skill appears.
  const skillsRes = useResource<SkillsPayload>(
    project ? (signal) => api.skills(project, signal) : null,
    [project],
    { pollMs: POLL_SETTINGS },
  )
  const onSkillsChanged = useCallback(() => {
    skillsRes.refetch()
  }, [skillsRes])
  // NOT project-gated: app_settings, the System schema, and the provider/model catalog are
  // all GLOBAL (the client resolves a null project to the `_system` scope), so they load on a
  // fresh install before any project exists — required for the keyless first-run onboarding.
  const appSettingsRes = useResource<AppSettings>(
    (signal) => api.appSettings(project ?? '', signal),
    [project],
    { pollMs: POLL_SETTINGS },
  )

  // Auto-select a project once the list arrives and either nothing is chosen OR
  // the hash-selected project is stale. PREFER the operator's configured DEFAULT
  // project (the System "Default project" setting, cortex_default_project) when
  // it's a real registered project, else the first one. This is what makes that
  // setting a REAL wired control (not a dead display): we wait for the settings
  // read (so a projects-first race can't lose the default), then pick.
  useEffect(() => {
    if (!projectsRes.data || projects.length === 0) return
    if (selectedProject && projectKeys.has(selectedProject)) return
    // Hold until the app-settings read settles (data OR a finished load), so the
    // configured default is honored rather than pre-empted by the projects list.
    if (appSettingsRes.loading && !appSettingsRes.data) return
    const configured = appSettingsRes.data?.settings?.cortex_default_project
    const fallback =
      typeof configured === 'string' && projectKeys.has(configured)
        ? configured
        : projects[0]?.project_key
    if (fallback && fallback !== selectedProject) selectProject(fallback)
  }, [
    selectedProject,
    projects,
    projectKeys,
    projectsRes.data,
    appSettingsRes.data,
    appSettingsRes.loading,
    selectProject,
  ])
  const systemSchemaRes = useResource<SystemSchema>(
    (signal) => api.systemSchema(project ?? '', signal),
    [project],
    { pollMs: POLL_SETTINGS },
  )
  const providersRes = useResource<ProvidersCatalog>(
    (signal) => api.providers(project ?? '', signal),
    [project],
    { pollMs: POLL_SETTINGS },
  )
  // The configured/active providers (the Providers control surface — key-presence +
  // Test target per provider). Degrades to null on a stale-backend 404.
  // NOT project-gated: provider keys are GLOBAL (the client resolves a null project to
  // the `_system` scope), so the preconfigured-providers list loads + keys can be added/
  // rotated during first-run setup, before any project exists.
  const providersConfigRes = useResource<ProvidersConfig>(
    (signal) => api.providersConfig(project ?? '', signal),
    [project],
    { pollMs: POLL_SETTINGS },
  )
  // The selected project's registry row (for the Cortex + Workspace tabs —
  // repo_root / status). Reuses the already-fetched projects list (no extra call).
  const projectRow = useMemo(
    () => projects.find((p) => p.project_key === project) ?? null,
    [projects, project],
  )
  // After any settings write the SettingsView calls this — refetch the settings
  // resources so the surface lands on the authoritative post-write state
  // (refetch-on-success, no optimistic complexity). `api` is the write client. The
  // projects list is refetched too so a workspace repo_root edit reflects in the row.
  const onSettingsSaved = useCallback(() => {
    appSettingsRes.refetch()
    systemSchemaRes.refetch()
    providersConfigRes.refetch()
    providersRes.refetch() // the model catalog too — so a key-add (or Refresh) reflects new models
    projectsRes.refetch()
  }, [appSettingsRes, systemSchemaRes, providersConfigRes, providersRes, projectsRes])

  // After an in-pane per-agent config save (the relocated config editor in AgentDetail):
  // refetch the agents catalog so a designation change REGROUPS the agents column
  // (interactive ↔ autonomous) — the same roster refresh the Settings→Configure save had.
  const onAgentConfigSaved = useCallback(() => {
    catalogRes.refetch()
  }, [catalogRes])

  // Registration writes (feature-gap #81): after an add-agent / add-project / deregister
  // the relevant catalog is refetched so the new/removed row appears immediately.
  const onAgentRegistered = useCallback(() => {
    catalogRes.refetch()
  }, [catalogRes])
  const onAgentRemoved = useCallback(() => {
    // The selected agent may be the one just removed — clear the selection so the pane
    // doesn't linger on a gone agent (the catalog refetch + land-on-lead effect re-picks).
    selectAgent('')
    catalogRes.refetch()
  }, [catalogRes, selectAgent])
  const onProjectRegistered = useCallback(() => {
    projectsRes.refetch()
  }, [projectsRes])
  const onApplyUpdate = useCallback(async () => {
    if (!canManageUpdates) return
    await api.applyUpdate()
    updateJobRes.refetch()
  }, [canManageUpdates, updateJobRes])

  // Rename the onboarding-seeded "lead" worker (T1.6). The console has no in-place rename
  // endpoint, so we register the new-named lead then deregister `lead` (best-effort — a
  // failed remove leaves both rows, never an error), refetch the roster, and select the new
  // agent. Guarded against the no-op / re-naming-to-itself cases. `lead` role + `interactive`
  // designation mirror the seeded lead's shape.
  const onRenameLead = useCallback(
    async (newName: string) => {
      const name = newName.trim().toLowerCase()
      if (!project || !name || name === 'lead') return
      await api.registerAgent(project, { name, role: 'lead', designation: 'interactive' })
      await api.deregisterAgent(project, 'lead').catch(() => {})
      onAgentRegistered()
      selectAgent(name)
    },
    [project, selectAgent, onAgentRegistered],
  )

  // The rail's per-project "needs attention" map (pending handoffs → the rail badge). The SPA
  // only holds a per-project pending count for the SELECTED project (its /epics metrics carry
  // the /state pending-handoffs count; the dispatch board's open count is the fallback) — other
  // rows show no badge until a cross-project source lands (a known gap, surfaced honestly rather
  // than fabricated). Keyed by project so the rail stays a pure presentation of what we know.
  const attention = useMemo(() => {
    if (!project) return {}
    const fromEpics = epicsRes.data?.metrics?.pending_handoffs
    const fromBoard = dispatchRes.data?.dispatch_count
    const pending = fromEpics ?? fromBoard ?? null
    return { [project]: { pending } }
  }, [project, epicsRes.data, dispatchRes.data])

  // Default the selected agent to the project's lead when the catalog lands and
  // no agent is chosen for this project (the canonical "land on the lead" rule).
  const catalog = catalogRes.data
  useEffect(() => {
    if (!project || !agent || !catalog) return
    const knownAgents = new Set([
      ...catalog.interactive.map((row) => row.name),
      ...catalog.autonomous.map((row) => row.name),
      ...(catalog.lead ? [catalog.lead] : []),
    ])
    if (!knownAgents.has(agent)) selectAgent('')
  }, [project, agent, catalog, selectAgent])

  useEffect(() => {
    if (!project || agent || !catalog) return
    const fallback =
      catalog.lead ??
      catalog.interactive[0]?.name ??
      catalog.autonomous[0]?.name ??
      null
    if (fallback) selectAgent(fallback)
  }, [project, agent, catalog, selectAgent])

  // FRESH INSTALL = zero projects → show the Get-Started starting point (universal config →
  // create project → meet the lead) in the main area instead of an empty dashboard. Once a project
  // exists the shell auto-selects it (effect above) and the normal columns return.
  const onboarding = !projectsRes.loading && projects.length === 0

  // The ProfileMenu (bottom of the left rail) asks the main area to show the account views —
  // Profile (any user) or Users (admins). It's a one-shot request the shell hands to MainArea,
  // which adopts it as its current view then clears it (so a later in-app tab switch isn't
  // overridden). Kept out of the URL hash (which is project/agent only) — minimal nav, no router.
  const [requestedView, setRequestedView] = useState<MainView | null>(null)
  const onNavigateView = useCallback((v: MainView) => setRequestedView(v), [])
  const onViewConsumed = useCallback(() => setRequestedView(null), [])

  return (
    <>
      <div className="flex h-full flex-col gap-3 overflow-y-auto p-3 lg:flex-row lg:overflow-hidden">
        <ProjectRail
          projects={projects}
          selected={project}
          onSelect={selectProject}
          loading={projectsRes.loading}
          error={projectsRes.error}
          attention={attention}
          registrationClient={api}
          onProjectRegistered={onProjectRegistered}
          version={versionRes.data?.version}
          updateStatus={updateStatusRes.data}
          updateJob={updateJobRes.data}
          canManageUpdates={canManageUpdates}
          onApplyUpdate={canManageUpdates ? onApplyUpdate : undefined}
          onNavigateView={onNavigateView}
        />

        <AgentsColumn
          project={project}
          catalog={catalog}
          runBoard={runBoardRes.data}
          dispatch={dispatchRes.data}
          epics={epicsRes.data}
          selectedAgent={agent}
          onSelectAgent={selectAgent}
          registrationClient={api}
          onAgentRegistered={onAgentRegistered}
          loading={catalogRes.loading}
          error={catalogRes.error}
        />

        {onboarding ? (
          <OnboardingView
            providersConfig={providersConfigRes.data}
            settingsClient={api}
            registrationClient={api}
            onSettingsSaved={onSettingsSaved}
            onProjectCreated={onProjectRegistered}
          />
        ) : (
          <MainArea
            project={project}
            agent={agent}
            runBoard={runBoardRes.data}
            onAgentConfigSaved={onAgentConfigSaved}
            registrationClient={api}
            onAgentRemoved={onAgentRemoved}
            onRenameLead={onRenameLead}
            dispatch={dispatchRes}
            dispatchActivity={dispatchActivityRes}
            dispatchClient={api}
            dispatchRunCtl={dispatchRunCtl}
            onDispatchChanged={onDispatchChanged}
            usage={usageRes}
            kpis={kpisRes}
            appSettings={appSettingsRes}
            systemSchema={systemSchemaRes}
            providers={providersRes}
            providersConfig={providersConfigRes}
            projectRow={projectRow}
            projects={projects}
            settingsClient={api}
            onSettingsSaved={onSettingsSaved}
            explainClient={api}
            planClient={api}
            graphClient={api}
            historyClient={api}
            skills={skillsRes}
            skillsClient={api}
            onSkillsChanged={onSkillsChanged}
            dashboardClient={api}
            authClient={api}
            requestedView={requestedView}
            onViewConsumed={onViewConsumed}
          />
        )}

        {/* The right column — the project's working-folder file tree (collapsible). */}
        {!onboarding && <WorkspaceColumn project={project} client={api} />}
      </div>
    </>
  )
}
