import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MainArea } from './MainArea'
import type { SettingsWriteClient } from './SettingsView'
import type { AgentConfigEditorClient } from './AgentConfigEditor'
import type { DispatchClient } from './DispatchView'
import type { ExplainClient } from './ExplainView'
import type { PlanClient } from './PlanView'
import type { GraphClient } from './GraphView'
import type { HistoryClient } from './HistoryView'
import type { SkillsClient } from './SkillsView'
import type { DashboardClient } from './DashboardView'
import type {
  AgentEpicsPayload,
  AnalyticsKpis,
  AppSettings,
  DispatchActivity,
  DispatchBoard,
  DispatchRunController,
  DispatchRunState,
  GraphPayload,
  HistoryPayload,
  Project,
  Resource,
  RunBoard,
  RunTranscript,
  SkillsPayload,
  SystemSchema,
  UsageBreakdown,
} from '../api'

// An EMPTY graph payload: GraphView skips the cytoscape mount when there are no nodes, so
// the switcher tests never touch the (jsdom-impossible) WebGL canvas. The GraphView's own
// data wiring + controls are covered in GraphView.test.tsx with an injected fake mounter.
const EMPTY_GRAPH: GraphPayload = {
  nodes: [],
  edges: [],
  stats: {
    own_nodes: null, own_edges: null, total_nodes: null, total_edges: null,
    repo_count: 0, repos: [], shown_nodes: 0, shown_edges: 0,
    total_shown_nodes: null, kind_counts: { code: 0, mem: 0, work: 0 },
    node_cap: 140, capped: false,
  },
}

const settingsClient: SettingsWriteClient = {
  setAppSetting: vi.fn().mockResolvedValue({ project: 'kaidera-os', settings: {}, store_connected: true, ok: true }),
  setAppSettings: vi.fn().mockResolvedValue({ project: 'kaidera-os', settings: {}, store_connected: true, ok: true }),
  setWorkspace: vi.fn().mockResolvedValue({ project: 'kaidera-os', project_key: 'kaidera-os', ok: true, repo_root: null, previous_repo_root: null, error: null }),
  cortexConfig: vi.fn().mockResolvedValue({
    ok: true,
    error: null,
    config: {
      embedding_provider: 'openrouter',
      embedding_model: 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
      embedding_dims: 768,
      rerank_enabled: true,
      rerank_provider: 'nvidia',
      rerank_model: 'nv-rerank-qa-mistral-4b:1',
    },
  }),
  setCortexConfig: vi.fn().mockResolvedValue({
    ok: true,
    error: null,
    config: {
      embedding_provider: 'openrouter',
      embedding_model: 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
      embedding_dims: 768,
      rerank_enabled: true,
      rerank_provider: 'nvidia',
      rerank_model: 'nv-rerank-qa-mistral-4b:1',
    },
  }),
}

// The in-pane per-agent config-editor client (the switcher tests keep agent=null, so the
// editor never fetches; its own behaviour is covered in AgentConfigEditor/AgentDetail tests).
const agentConfigClient: AgentConfigEditorClient = {
  configCatalog: vi.fn(),
  agentConfigView: vi.fn(),
  setAgentConfig: vi.fn().mockResolvedValue({ project: 'kaidera-os', agent: '', override: {}, designation: '', ok: true }),
  promoteAgent: vi.fn().mockResolvedValue({ ok: true, error: null }),
}

function res<T>(data: T | null, over: Partial<Resource<T>> = {}): Resource<T> {
  return { data, error: null, loading: false, refetch: () => {}, ...over }
}

// A no-op EventSource so a test that mounts AgentDetail with a real agent (the deep-link /
// auto-land cases) subscribes via useRunStateStream but never opens a real (jsdom-impossible)
// SSE channel. jsdom has no EventSource; the agent-null tests never touch it.
class NoopEventSource {
  url: string
  constructor(url: string) {
    this.url = url
  }
  addEventListener() {}
  removeEventListener() {}
  close() {}
}
beforeEach(() => {
  vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
})
afterEach(() => {
  vi.unstubAllGlobals()
})

const dispatch: Resource<DispatchBoard> = res<DispatchBoard>({
  project: 'kaidera-os',
  rows: [],
  dispatch_count: 2,
  dispatch_proposed_count: 0,
  dispatch_unassigned_count: 0,
  autonomous_on: false,
  propose_mode_on: false,
  awaiting_approval_ids: [],
})
const usage: Resource<UsageBreakdown> = res<UsageBreakdown>({
  project: 'kaidera-os',
  store_connected: true,
  total_runs: 0,
  total_tokens: 0,
  total_tokens_h: null,
  by_model_bars: [],
  by_model_table: [],
  model_count: 0,
  by_provider: [],
  by_provider_bars: [],
  provider_count: 0,
  rows: [],
  agent_count: 0,
  agents_with_usage: 0,
  cost_rows: [],
  project_cost: null,
  project_cost_h: 'n/a',
  priced_agent_count: 0,
})
const appSettings: Resource<AppSettings> = res<AppSettings>({
  project: 'kaidera-os',
  settings: {},
  store_connected: true,
})
const systemSchema: Resource<SystemSchema> = res<SystemSchema>({
  project: 'kaidera-os',
  groups: [],
  store_connected: true,
})
const projectRow: Project = { project_key: 'kaidera-os', display_name: 'Kaidera OS', status: 'active' }

const dispatchActivity: Resource<DispatchActivity> = res<DispatchActivity>({
  project: 'kaidera-os',
  activity: [],
  activity_count: 0,
  waves: [],
  waves_any: false,
  loop_running: false,
  inflight: 0,
  cap: 3,
  no_orch: false,
})
const IDLE_RUN: DispatchRunState = { running: false, output: '', runId: null, error: null, done: false }
const dispatchClient: DispatchClient = {
  approveHandoff: vi.fn().mockResolvedValue(undefined),
}
const dispatchRunCtl: DispatchRunController = {
  run: vi.fn().mockResolvedValue(undefined),
  stateFor: () => IDLE_RUN,
}
const explainClient: ExplainClient = {
  postExplain: vi.fn().mockResolvedValue({ run_id: 'r', accepted: true, error: null }),
  getExplainList: vi.fn().mockResolvedValue([]),
  run: vi.fn(),
}
const planClient: PlanClient = {
  getPlanList: vi.fn().mockResolvedValue([]),
  getPlanFile: vi.fn().mockResolvedValue({ path: '', text: '' }),
}
const graphClient: GraphClient = {
  graph: vi.fn().mockResolvedValue(EMPTY_GRAPH),
  graphSearch: vi.fn().mockResolvedValue(EMPTY_GRAPH),
}
// An EMPTY history payload: the switcher tests only verify the History tab mounts; the view's
// own data wiring (timeline + decisions + count) is covered in HistoryView.test.tsx.
const EMPTY_HISTORY: HistoryPayload = { events: [], decisions: [], agent_count: 0 }
const historyClient: HistoryClient = {
  history: vi.fn().mockResolvedValue(EMPTY_HISTORY),
}
// An EMPTY skills catalogue: the switcher tests only verify the Skills tab mounts; the view's
// own data wiring (catalogue + install + assign) is covered in SkillsView.test.tsx.
const EMPTY_SKILLS: Resource<SkillsPayload> = res<SkillsPayload>({ skills: [] })
const skillsClient: SkillsClient = {
  installSkill: vi.fn().mockResolvedValue({ ok: true, error: null, skills: [] }),
  bindSkill: vi.fn().mockResolvedValue({ ok: true, slug: '', subject: '', error: null }),
}
// The Dashboard composes from the existing endpoints; the switcher tests only verify it mounts
// as the default landing. Its own data wiring is covered in DashboardView.test.tsx.
const EMPTY_EPICS: AgentEpicsPayload = {
  project: 'kaidera-os',
  epic: { mode: 'continuous', epic_count: 0, epics: [], label: 'continuous · no epics' },
  metrics: { active_tasks: null, pending_tasks: null, pending_handoffs: null, events_24h: null },
}
const EMPTY_KPIS: AnalyticsKpis = {
  project: 'kaidera-os', events_24h: null, active_tasks: null, pending_handoffs: null,
  decisions_recent: null, window_days: 7, tokens_recent: 0, tokens_recent_h: null,
}
const dashboardClient: DashboardClient = {
  agentEpics: vi.fn().mockResolvedValue(EMPTY_EPICS),
  analyticsKpis: vi.fn().mockResolvedValue(EMPTY_KPIS),
  dispatchBoard: vi.fn().mockResolvedValue(dispatch.data!),
  history: vi.fn().mockResolvedValue(EMPTY_HISTORY),
  runBoard: vi.fn().mockResolvedValue({
    project: 'kaidera-os',
    active: [],
    active_count: 0,
    recent: [],
    recent_count: 0,
  } satisfies RunBoard),
  run: vi.fn().mockResolvedValue({
    run_id: '',
    project: 'kaidera-os',
    agent: null,
    agent_display: null,
    handoff_id: null,
    handoff_short: null,
    model: null,
    harness: null,
    status: 'ok',
    running: false,
    started_ts: null,
    updated_ts: null,
    started_ago: '',
    updated_ago: '',
    status_label: 'completed',
    error: null,
    ended_ts: null,
    ended_ago: '',
    segments: [],
    body: '',
    truncated: false,
  } satisfies RunTranscript),
}
const authClient = {
  authProfile: vi.fn().mockResolvedValue({ name: 'User', email: '', is_admin: false, role: 'user', status: 'active' }),
  updateProfile: vi.fn().mockResolvedValue({ ok: true, user: { name: 'User', email: '', is_admin: false, role: 'user', status: 'active' } }),
  authUsers: vi.fn().mockResolvedValue({ users: [] }),
  createAuthUser: vi.fn().mockResolvedValue({ ok: true, user: { name: 'User', email: '', is_admin: false, role: 'user', status: 'active' } }),
  updateAuthUser: vi.fn().mockResolvedValue({ ok: true, user: { name: 'User', email: '', is_admin: false, role: 'user', status: 'active' } }),
  deleteAuthUser: vi.fn().mockResolvedValue({ ok: true, removed: true }),
}

function renderMainArea() {
  // agent=null keeps AgentDetail in its placeholder (no SSE EventSource opened).
  return render(
    <MainArea
      project="kaidera-os"
      agent={null}
      runBoard={null}
      agentConfigClient={agentConfigClient}
      onAgentConfigSaved={() => {}}
      dispatch={dispatch}
      dispatchActivity={dispatchActivity}
      dispatchClient={dispatchClient}
      dispatchRunCtl={dispatchRunCtl}
      onDispatchChanged={() => {}}
      usage={usage}
      appSettings={appSettings}
      systemSchema={systemSchema}
      projectRow={projectRow}
      settingsClient={settingsClient}
      onSettingsSaved={() => {}}
      explainClient={explainClient}
      planClient={planClient}
      graphClient={graphClient}
      historyClient={historyClient}
      skills={EMPTY_SKILLS}
      skillsClient={skillsClient}
      onSkillsChanged={() => {}}
      dashboardClient={dashboardClient}
      authClient={authClient}
    />,
  )
}

/** Render the main area with an AGENT pre-selected (the agent-pane default lands on the agent). */
function renderWithAgent(agent: string) {
  return render(
    <MainArea
      project="kaidera-os"
      agent={agent}
      runBoard={null}
      agentConfigClient={agentConfigClient}
      onAgentConfigSaved={() => {}}
      dispatch={dispatch}
      dispatchActivity={dispatchActivity}
      dispatchClient={dispatchClient}
      dispatchRunCtl={dispatchRunCtl}
      onDispatchChanged={() => {}}
      usage={usage}
      appSettings={appSettings}
      systemSchema={systemSchema}
      projectRow={projectRow}
      settingsClient={settingsClient}
      onSettingsSaved={() => {}}
      explainClient={explainClient}
      planClient={planClient}
      graphClient={graphClient}
      historyClient={historyClient}
      skills={EMPTY_SKILLS}
      skillsClient={skillsClient}
      onSkillsChanged={() => {}}
      dashboardClient={dashboardClient}
      authClient={authClient}
    />,
  )
}

describe('MainArea switcher', () => {
  it('renders the segmented control (Dashboard · Agent · Dispatch · Analytics · History · Graph · Explain · Skills · Settings · Help)', () => {
    renderMainArea()
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(
      expect.arrayContaining([
        'Dashboard',
        'Worker',
        expect.stringContaining('Dispatch'),
        'Analytics',
        'History',
        'Graph',
        'Explain',
        'Skills',
        'Settings',
        'Help',
      ]),
    )
    // Dashboard is FIRST (the project landing)
    expect(tabs[0]).toHaveTextContent('Dashboard')
    // Skills sits right before Settings (the switcher order)
    const labels = tabs.map((t) => t.textContent)
    expect(labels.indexOf('Skills')).toBe(labels.indexOf('Settings') - 1)
  })

  it('shows the DASHBOARD as the default project landing (no agent picked)', async () => {
    renderMainArea()
    // the Dashboard tab is selected by default
    expect(screen.getByRole('tab', { name: 'Dashboard' })).toHaveAttribute('aria-selected', 'true')
    // the project overview header renders (NOT an empty agent pane)
    expect(await screen.findByTestId('dashboard-header')).toBeInTheDocument()
    expect(screen.queryByText(/select a worker/i)).not.toBeInTheDocument()
  })

  it('launches project-level Explain from the Dashboard', async () => {
    const user = userEvent.setup()
    renderMainArea()

    await user.click(await screen.findByRole('button', { name: /explain project/i }))

    expect(screen.getByRole('tab', { name: 'Explain' })).toHaveAttribute('aria-selected', 'true')
    // The project seed lands on the one-click project flow (Advanced stays collapsed).
    expect(screen.getByRole('button', { name: /generate project explainer/i })).not.toBeDisabled()
    expect(screen.queryByLabelText('Explain target kind')).not.toBeInTheDocument()
  })

  it('lands on the Agent view when deep-linked straight to an agent (#/project/agent)', () => {
    // mounted WITH an agent already named (the hash deep-link) → the agent pane shows
    renderWithAgent('ren')
    expect(screen.getByRole('tab', { name: /Worker/ })).toHaveAttribute('aria-selected', 'true')
    // the agent placeholder is gone (an agent IS selected) + the dashboard header isn't shown
    expect(screen.queryByText(/select a worker/i)).not.toBeInTheDocument()
    expect(screen.queryByTestId('dashboard-header')).not.toBeInTheDocument()
  })

  it('does NOT leave the Dashboard when the shell auto-lands on the lead (null→lead)', async () => {
    // mount with no agent → Dashboard; then the shell auto-selects the lead (a null→value
    // change in the SAME project) — that must NOT pull focus off the landing.
    const { rerender } = renderMainArea()
    expect(await screen.findByTestId('dashboard-header')).toBeInTheDocument()
    rerender(
      <MainArea
        project="kaidera-os"
        agent="ren"
        runBoard={null}
        agentConfigClient={agentConfigClient}
        onAgentConfigSaved={() => {}}
        dispatch={dispatch}
        dispatchActivity={dispatchActivity}
        dispatchClient={dispatchClient}
        dispatchRunCtl={dispatchRunCtl}
        onDispatchChanged={() => {}}
        usage={usage}
        appSettings={appSettings}
        systemSchema={systemSchema}
        projectRow={projectRow}
        settingsClient={settingsClient}
        onSettingsSaved={() => {}}
        explainClient={explainClient}
        planClient={planClient}
        graphClient={graphClient}
        historyClient={historyClient}
        skills={EMPTY_SKILLS}
        skillsClient={skillsClient}
        onSkillsChanged={() => {}}
      dashboardClient={dashboardClient}
      authClient={authClient}
      />,
    )
    // still on the Dashboard (the auto-land didn't bounce us into the Agent pane)
    expect(screen.getByRole('tab', { name: 'Dashboard' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('dashboard-header')).toBeInTheDocument()
  })

  it('switches to the Explain view (the code-explainer picker)', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: 'Explain' }))
    // the picker opens project-bound: the one-click primary button, kind selector behind Advanced
    expect(screen.getByRole('button', { name: /generate project explainer/i })).toBeInTheDocument()
    expect(screen.queryByLabelText('Explain target kind')).not.toBeInTheDocument()
    // the agent placeholder is gone
    expect(screen.queryByText(/select a worker/i)).not.toBeInTheDocument()
  })

  it('switches to the Graph view (the knowledge-graph canvas)', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: 'Graph' }))
    // the graph view's header + the bounded-view stats header are present
    expect(screen.getByRole('heading', { name: 'Knowledge graph' })).toBeInTheDocument()
    expect(screen.getByRole('searchbox', { name: /search the/i })).toBeInTheDocument()
    // the agent placeholder is gone
    expect(screen.queryByText(/select a worker/i)).not.toBeInTheDocument()
  })

  it('switches to the History view (the cross-agent activity timeline)', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: 'History' }))
    // the history view's header is present + it fetched the timeline for the project
    expect(screen.getByRole('heading', { name: 'Activity history' })).toBeInTheDocument()
    expect(historyClient.history).toHaveBeenCalledWith(
      'kaidera-os',
      undefined,
      expect.anything(),
      { includeDecisions: true },
    )
    // the agent placeholder is gone
    expect(screen.queryByText(/select a worker/i)).not.toBeInTheDocument()
  })

  it('switches to the Skills view (the skills catalogue + install bar)', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: 'Skills' }))
    // the skills view's header + the install bar are present
    expect(screen.getByRole('heading', { name: 'Skills' })).toBeInTheDocument()
    expect(screen.getByLabelText('Skill GitHub URL')).toBeInTheDocument()
    // the agent placeholder is gone
    expect(screen.queryByText(/select a worker/i)).not.toBeInTheDocument()
  })

  it('switches to the Agent view (the placeholder when no agent is picked) via the tab', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: 'Worker' }))
    expect(screen.getByRole('tab', { name: 'Worker' })).toHaveAttribute('aria-selected', 'true')
    // AgentDetail placeholder (no agent selected)
    expect(screen.getByText(/select a worker/i)).toBeInTheDocument()
  })

  it('surfaces the pending-dispatch badge on the Dispatch tab', () => {
    renderMainArea()
    // the dispatch board count (2) shows as the tab badge
    expect(screen.getByLabelText('2 pending')).toBeInTheDocument()
  })

  it('switches to the Dispatch view when its tab is clicked', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: /Dispatch/ }))
    expect(screen.getByText('Dispatch board')).toBeInTheDocument()
    // the agent placeholder is gone
    expect(screen.queryByText(/select a worker/i)).not.toBeInTheDocument()
  })

  it('switches to the Analytics view', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: 'Analytics' }))
    expect(screen.getByText('Analytics · usage')).toBeInTheDocument()
  })

  it('switches to the Settings view (the canonical settings surface, now tabbed)', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: 'Settings' }))
    // the view's heading (distinct from the "Settings" tab button)
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument()
    // the surface is now WRITABLE (Track C) — the read-only badge is gone.
    expect(screen.queryByText('read-only')).not.toBeInTheDocument()
    // the settings sub-nav is present; project autonomy moved to the Dashboard.
    expect(screen.getByRole('tab', { name: 'System' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Flags' })).not.toBeInTheDocument()
  })

  it('switches to the Help view (embedded operator docs)', async () => {
    const user = userEvent.setup()
    renderMainArea()
    await user.click(screen.getByRole('tab', { name: 'Help' }))
    expect(screen.getByRole('heading', { name: 'Help' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Getting Started' })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: 'Getting started with Kaidera OS' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Search help guides' })).toBeInTheDocument()
  })
})
