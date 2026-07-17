/**
 * MainArea — the MAIN column: the view switcher + the active project/agent view.
 *
 * Honours the CANONICAL no-repeat rule (the CTO's hard directive). The left column
 * stays projects-only and the 2nd stays agents+metrics; the project-LEVEL views
 * (dashboard / dispatch / analytics / history / graph / explain / settings)
 * describe the PROJECT, not a column concern, so they are reached HERE via a small
 * segmented control in the main-area header —
 * never duplicated into a column. The main area shows exactly ONE of:
 *
 *   Dashboard → DashboardView (the project landing overview)
 *   Agent     → AgentDetail   (the selected agent's detail + its live SSE run)
 *   Dispatch  → DispatchView  (the project's open-handoff board)
 *   Analytics → AnalyticsView (the project's usage + est-cost breakdown)
 *   History   → HistoryView   (the cross-agent activity timeline)
 *   Graph     → GraphView     (the Cortex knowledge/code graph)
 *   Explain   → ExplainView   (the code/system explainer)
 *   Settings  → SettingsView  (the canonical settings surface)
 *   Help      → HelpView      (embedded operator docs)
 *
 * The shell (App) owns the (project, agent) selection + fetches the shared
 * catalogs; this component owns only the local "which main view" tab state and is
 * pure-presentational over the data passed in. The project-scoped resources
 * (dispatch board / usage / settings / dashboard sources) are fetched by the shell
 * and threaded in, so a project switch refreshes them with no extra wiring here.
 */

import { useEffect, useState } from 'react'
import { cx } from '../components/ui'
import { DashboardView } from './DashboardView'
import type { DashboardClient } from './DashboardView'
import { AgentDetail } from './AgentDetail'
import type { AgentConfigEditorClient } from './AgentConfigEditor'
import type { DeregisterClient } from './RegistrationForms'
import { DispatchView } from './DispatchView'
import type { DispatchClient } from './DispatchView'
import { AnalyticsView } from './AnalyticsView'
import { SettingsView } from './SettingsView'
import type { SettingsWriteClient } from './SettingsView'
import { ExplainView } from './ExplainView'
import type { ExplainClient, ExplainInitialTarget } from './ExplainView'
import { PlanView } from './PlanView'
import type { PlanClient } from './PlanView'
import { ThemeToggle } from '../components/ThemeToggle'
import { GraphView } from './GraphView'
import type { GraphClient } from './GraphView'
import { HistoryView } from './HistoryView'
import type { HistoryClient } from './HistoryView'
import { SkillsView } from './SkillsView'
import type { SkillsClient } from './SkillsView'
import { ProfileView } from './ProfileView'
import type { ProfileClient } from './ProfileView'
import { UsersView } from './UsersView'
import type { UsersClient } from './UsersView'
import { HelpView } from './HelpView'
import type {
  AppSettings,
  DispatchActivity,
  DispatchBoard,
  DispatchRunController,
  Project,
  RunBoard,
  SkillsPayload,
  SystemSchema,
  UsageBreakdown,
  AnalyticsKpis,
} from '../api'
import type { Resource } from '../api'

export type MainView =
  | 'dashboard'
  | 'agent'
  | 'dispatch'
  | 'analytics'
  | 'settings'
  | 'explain'
  | 'plan'
  | 'graph'
  | 'history'
  | 'skills'
  | 'help'
  // Account views — reached from the ProfileMenu (NOT the main switcher TABS): keeps the
  // top segmented control focused on project/agent work and the account nav minimal.
  | 'profile'
  | 'users'

const TABS: { id: MainView; label: string }[] = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'agent', label: 'Worker' },
  { id: 'dispatch', label: 'Dispatch' },
  { id: 'analytics', label: 'Analytics' },
  { id: 'history', label: 'History' },
  { id: 'graph', label: 'Graph' },
  { id: 'explain', label: 'Explain' },
  { id: 'plan', label: 'Plan' },
  { id: 'skills', label: 'Skills' },
  { id: 'settings', label: 'Settings' },
  { id: 'help', label: 'Help' },
]

interface MainAreaProps {
  project: string | null
  agent: string | null
  runBoard: RunBoard | null
  /** The in-pane per-agent config-editor data client (the `api` object). */
  agentConfigClient?: AgentConfigEditorClient
  /** Called after a successful in-pane agent-config save — the shell regroups the roster. */
  onAgentConfigSaved?: () => void
  /** Optional registration client (feature-gap #81) — enables the "Deregister" action in the
   * agent config modal (deregisterAgent). When absent, no remove action. */
  registrationClient?: DeregisterClient
  /** Called after a successful deregister — the shell refetches the roster + clears the selection. */
  onAgentRemoved?: () => void
  /** Rename the seeded "lead" worker (T1.6) — threaded down to AgentDetail's header affordance. */
  onRenameLead?: (newName: string) => void
  dispatch: Resource<DispatchBoard>
  /** Orchestrator activity feed + the wave plan (GET …/activity) — the Dispatch view's feed. */
  dispatchActivity: Resource<DispatchActivity>
  /** The dispatch write client (the `api` object) — autonomy toggle + approve gate. */
  dispatchClient: DispatchClient
  /** The Approve & Run SSE controller (owned by the shell; survives board refetch). */
  dispatchRunCtl: DispatchRunController
  /** Called after any successful dispatch write — the shell refetches board + activity. */
  onDispatchChanged: () => void
  usage: Resource<UsageBreakdown>
  /** The Analytics headline KPI strip (GET …/kpis). Optional — the strip hides until it lands. */
  kpis?: Resource<AnalyticsKpis>
  appSettings: Resource<AppSettings>
  /** The typed System schema used by Settings. */
  systemSchema: Resource<SystemSchema>
  /** The selected project's registry row (Cortex + Workspace tabs). */
  projectRow: Project | null
  /** ALL active projects (the Settings → Workspace tab is a multi-project repo-root editor).
   * Reuses the shell's already-fetched /projects list — no extra call. */
  projects?: Project[]
  /** The settings write client (the `api` object) + the refetch the shell wires. */
  settingsClient: SettingsWriteClient
  onSettingsSaved: () => void
  /** The Explain client (the `api` object): postExplain + getExplainList + run (full HTML). */
  explainClient: ExplainClient
  /** The Plan client (the `api` object): getPlanList + getPlanFile. */
  planClient: PlanClient
  /** The Graph client (the `api` object): graph(project) + graphSearch(project, q). */
  graphClient: GraphClient
  /** The History client (the `api` object): history(project, limit?). */
  historyClient: HistoryClient
  /** The skills catalogue (global + this project's) — GET /skills/{project}. */
  skills: Resource<SkillsPayload>
  /** The Skills write client (the `api` object): installSkill + bindSkill. */
  skillsClient: SkillsClient
  /** Called after any successful skills write (install/bind) — the shell refetches the catalogue. */
  onSkillsChanged: () => void
  /** The Dashboard client (the `api` object): agentEpics + analyticsKpis + dispatchBoard + history.
   * The project-LANDING overview composes from these existing endpoints (no new backend). */
  dashboardClient: DashboardClient
  /** The auth client (the `api` object) for the account views: profile read/update + (admin) user CRUD. */
  authClient: ProfileClient & UsersClient
  /** A one-shot view the ProfileMenu asked to show (Profile/Users). MainArea adopts it then
   * calls onViewConsumed so a later in-app tab switch isn't overridden. */
  requestedView?: MainView | null
  /** Called once MainArea has adopted `requestedView` (clears the one-shot request in the shell). */
  onViewConsumed?: () => void
}

/** The segmented control: Dashboard · Agent · Dispatch · Analytics · History · Graph · Explain · Settings · Help. */
function MainSwitcher({
  view,
  onSelect,
  pending,
}: {
  view: MainView
  onSelect: (v: MainView) => void
  pending: number
}) {
  return (
    <div
      role="tablist"
      aria-label="Main view"
      className="glass-soft flex min-w-0 flex-1 flex-wrap items-center gap-0.5 rounded-xl p-1"
    >
      {TABS.map((t) => {
        const active = t.id === view
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onSelect(t.id)}
            className={cx(
              'relative shrink-0 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              active
                ? 'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/40'
                : 'text-ink-400 hover:bg-base-800/50 hover:text-ink-200',
            )}
          >
            {t.label}
            {t.id === 'dispatch' && pending > 0 && (
              <span
                className="ml-1.5 rounded-full bg-mint-500/20 px-1.5 py-0.5 text-[9px] font-bold tabular-nums text-mint-300"
                aria-label={`${pending} pending`}
              >
                {pending}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

export function MainArea({
  project,
  agent,
  runBoard,
  agentConfigClient,
  onAgentConfigSaved,
  registrationClient,
  onAgentRemoved,
  onRenameLead,
  dispatch,
  dispatchActivity,
  dispatchClient,
  dispatchRunCtl,
  onDispatchChanged,
  usage,
  kpis,
  appSettings,
  systemSchema,
  projectRow,
  projects,
  settingsClient,
  onSettingsSaved,
  explainClient,
  planClient,
  graphClient,
  historyClient,
  skills,
  skillsClient,
  onSkillsChanged,
  dashboardClient,
  authClient,
  requestedView,
  onViewConsumed,
}: MainAreaProps) {
  // The project-LANDING default is the Dashboard (the CTO's "bring back the dashboard"): when a
  // project is selected and no agent is picked, the main area shows the project overview — NOT an
  // empty agent pane. A deep-link that already names an agent (hash `#/<project>/<agent>`) lands
  // on the Agent view instead (the user navigated straight to that agent). A project switch lands
  // back on the Dashboard; a deliberate agent re-pick switches to the Agent view.
  const [view, setView] = useState<MainView>(agent ? 'agent' : 'dashboard')
  // Compose-with-Explain: when a file/function node in the Graph view fires "Explain this",
  // we switch to the Explain tab pre-filled with that target. The pending target is handed to
  // ExplainView as its initial target (one-shot — it seeds the picker on mount/change).
  const [explainSeed, setExplainSeed] = useState<ExplainInitialTarget | null>(null)
  const onExplainTarget = (target: { kind: 'file' | 'blast'; value: string }) => {
    setExplainSeed({ ...target, nonce: Date.now() })
    setView('explain')
  }
  const onExplainProject = () => {
    setExplainSeed({ kind: 'project', value: '', nonce: Date.now() })
    setView('explain')
  }
  // A project switch resets the main view to the DASHBOARD (the project landing) — a
  // project-scoped view shouldn't persist across projects, and the overview is where a
  // project switch should land. Done by the React "adjust state during render" pattern
  // (storing the project the view belongs to) rather than an effect, so there's no
  // synchronous setState-in-effect cascade — mirrors the key-scoped derivation the rest
  // of the app uses. The agent baseline is reset IN THE SAME pass so the shell's
  // auto-land-on-lead for the new project is seen as the baseline (it does NOT bounce
  // off the Dashboard); only a DELIBERATE agent pick within the project switches to the
  // Agent view.
  const [viewProject, setViewProject] = useState<string | null>(project)
  const [viewAgent, setViewAgent] = useState<string | null>(agent)
  if (project !== viewProject) {
    setViewProject(project)
    setViewAgent(agent)
    setExplainSeed(null)
    setView('dashboard')
  } else if (agent !== viewAgent) {
    // The selected agent changed WITHIN the same project. A null→value change is the shell's
    // automatic "land on the lead" for this project — that must NOT pull focus off the
    // Dashboard landing (otherwise the overview would flash and vanish). A value→value change
    // is a DELIBERATE re-pick (the operator clicked a different agent), which surfaces the
    // Agent pane. Picking an agent is the gesture that leaves the landing.
    const wasAutoLand = viewAgent === null
    setViewAgent(agent)
    if (agent && !wasAutoLand) setView('agent')
  }

  // Adopt a one-shot view the ProfileMenu requested (Profile/Users). It wins over the
  // project-switch reset above because it runs AFTER it in the same render. Tracked with the
  // SAME "adjust state during render" pattern as viewProject/viewAgent above (state, not a ref
  // read during render) so it's applied exactly once per distinct request. The shell's one-shot
  // is cleared in an effect (never a parent setState during render).
  const [adopted, setAdopted] = useState<MainView | null>(null)
  if (requestedView && requestedView !== adopted) {
    setAdopted(requestedView)
    setView(requestedView)
  } else if (!requestedView && adopted !== null) {
    setAdopted(null)
  }
  useEffect(() => {
    if (requestedView) onViewConsumed?.()
  }, [requestedView, onViewConsumed])

  const pending = dispatch.data?.dispatch_count ?? 0

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-3 max-lg:order-1 max-lg:h-[calc(100vh-1.5rem)] max-lg:min-h-[40rem] max-lg:w-full max-lg:flex-none">
      {/* The main-area header — the canonical switcher home (no column repeats it).
          The theme toggle sits top-right (the only global control up here). */}
      <div className="flex shrink-0 items-center justify-between gap-3">
        <MainSwitcher view={view} onSelect={setView} pending={pending} />
        <ThemeToggle />
      </div>

      {/* The active view fills the rest of the main column. */}
      <div className="main-view-container flex min-h-0 flex-1">
        {view === 'dashboard' && (
          <DashboardView
            project={project}
            projectRow={projectRow}
            client={dashboardClient}
            onExplainProject={onExplainProject}
          />
        )}
        {view === 'agent' && (
          <AgentDetail
            project={project}
            agent={agent}
            runBoard={runBoard}
            configClient={agentConfigClient}
            onConfigSaved={onAgentConfigSaved}
            registrationClient={registrationClient}
            onAgentRemoved={onAgentRemoved}
            onRenameLead={onRenameLead}
            repoRoot={projectRow?.repo_root ?? null}
          />
        )}
        {view === 'dispatch' && (
          <DispatchView
            project={project}
            board={dispatch.data}
            loading={dispatch.loading}
            error={dispatch.error}
            activity={dispatchActivity.data}
            client={dispatchClient}
            runCtl={dispatchRunCtl}
            onChanged={onDispatchChanged}
          />
        )}
        {view === 'analytics' && (
          <AnalyticsView
            project={project}
            usage={usage.data}
            kpis={kpis?.data ?? null}
            loading={usage.loading}
            error={usage.error}
          />
        )}
        {view === 'settings' && (
          <SettingsView
            project={project}
            appSettings={appSettings.data}
            systemSchema={systemSchema.data}
            projectRow={projectRow}
            projects={projects}
            loading={appSettings.loading}
            error={appSettings.error}
            client={settingsClient}
            onSaved={onSettingsSaved}
          />
        )}
        {view === 'explain' && (
          <ExplainView project={project} client={explainClient} initialTarget={explainSeed} />
        )}
        {view === 'plan' && project && <PlanView project={project} client={planClient} />}
        {view === 'graph' && (
          <GraphView project={project} client={graphClient} onExplainTarget={onExplainTarget} />
        )}
        {view === 'history' && <HistoryView project={project} client={historyClient} />}
        {view === 'skills' && (
          <SkillsView
            project={project}
            skills={skills.data?.skills ?? null}
            loading={skills.loading}
            error={skills.error}
            client={skillsClient}
            onChanged={onSkillsChanged}
          />
        )}
        {view === 'help' && <HelpView />}
        {view === 'profile' && <ProfileView client={authClient} />}
        {view === 'users' && <UsersView client={authClient} />}
      </div>
    </div>
  )
}
