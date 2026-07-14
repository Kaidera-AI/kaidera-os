import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DashboardView } from './DashboardView'
import type { DashboardClient } from './DashboardView'
import type {
  AgentEpicsPayload,
  AnalyticsKpis,
  DispatchBoard,
  HistoryPayload,
  Project,
  RunBoard,
  RunTranscript,
  ScheduledJobsResult,
} from '../api'

// ---------------------------------------------------------------------------
//  Fixtures — the existing endpoint shapes the Dashboard COMPOSES (no new backend).
// ---------------------------------------------------------------------------

function projectRow(over: Partial<Project> = {}): Project {
  return {
    project_key: 'kaidera-os',
    display_name: 'Kaidera OS',
    status: 'active',
    repo_root: '/Users/amad/DevVault/kaidera-os',
    agent_count: 3,
    ...over,
  }
}

function epics(over: Partial<AgentEpicsPayload> = {}): AgentEpicsPayload {
  return {
    project: 'kaidera-os',
    epic: {
      mode: 'epics',
      epic_count: 2,
      epics: [
        {
          epic_id: 'E007',
          title: 'Console SPA rebuild',
          status: 'active',
          overall_pct: 62,
          increment_count: 3,
          is_active: true,
          increments: [
            { num: 1, label: 'Inc 1', title: 'rail', pct: 100, status: 'done', kind: 'done' },
            { num: 2, label: 'Inc 2', title: 'dashboard', pct: 40, status: 'prog', kind: 'prog' },
            { num: 3, label: 'Inc 3', title: 'polish', pct: 0, status: 'todo', kind: 'todo' },
          ],
        },
      ],
    },
    metrics: {
      active_tasks: 4,
      pending_tasks: 7,
      pending_handoffs: 2,
      events_24h: 51,
    },
    ...over,
  }
}

function kpis(over: Partial<AnalyticsKpis> = {}): AnalyticsKpis {
  return {
    project: 'kaidera-os',
    events_24h: 51,
    active_tasks: 4,
    pending_handoffs: 2,
    decisions_recent: 9,
    window_days: 7,
    tokens_recent: 123456,
    tokens_recent_h: '123.5K',
    ...over,
  }
}

function board(over: Partial<DispatchBoard> = {}): DispatchBoard {
  return {
    project: 'kaidera-os',
    rows: [],
    dispatch_count: 2,
    dispatch_proposed_count: 1,
    dispatch_unassigned_count: 1,
    autonomous_on: false,
    propose_mode_on: true,
    awaiting_approval_ids: [],
    ...over,
  }
}

function history(over: Partial<HistoryPayload> = {}): HistoryPayload {
  return {
    events: [
      {
        ts: '2026-06-08T10:00:00Z',
        ts_ago: '5m',
        agent: 'cole',
        role: 'full-stack-developer',
        kind: 'tool',
        kind_label: 'action',
        summary: 'wrote app/main.py',
      },
      {
        ts: '2026-06-08T09:55:00Z',
        ts_ago: '10m',
        agent: 'ren',
        role: 'co-lead',
        kind: 'say',
        kind_label: 'message',
        summary: 'reviewed the dashboard plan',
      },
    ],
    decisions: [],
    agent_count: 3,
    ...over,
  }
}

function runBoard(over: Partial<RunBoard> = {}): RunBoard {
  return {
    project: 'kaidera-os',
    active: [
      {
        run_id: 'run-active-1',
        project: 'kaidera-os',
        agent: 'sample-worker',
        agent_display: 'Sample Worker',
        handoff_id: 'handoff-1',
        handoff_short: 'handoff',
        model: 'openrouter/test-model',
        harness: 'codex',
        status: 'running',
        running: true,
        started_ts: '2026-06-08T10:01:00Z',
        updated_ts: '2026-06-08T10:02:00Z',
        started_ago: '4m',
        updated_ago: '1m',
        status_label: 'running',
      },
    ],
    active_count: 1,
    recent: [
      {
        run_id: 'run-recent-1',
        project: 'kaidera-os',
        agent: 'ren',
        agent_display: 'Ren',
        handoff_id: null,
        handoff_short: null,
        model: 'openrouter/test-model',
        harness: 'codex',
        status: 'ok',
        running: false,
        started_ts: '2026-06-08T09:45:00Z',
        updated_ts: '2026-06-08T09:50:00Z',
        started_ago: '20m',
        updated_ago: '15m',
        status_label: 'completed',
      },
    ],
    recent_count: 1,
    ...over,
  }
}

function transcript(runId: string, over: Partial<RunTranscript> = {}): RunTranscript {
  const baseRow = [...runBoard().active, ...runBoard().recent].find((row) => row.run_id === runId)
  const activeSegments = [
    { kind: 'input', text: 'I need an airport operations dashboard.' },
    { kind: 'thinking', text: 'Identify dashboard intent and missing operational dimensions.' },
    { kind: 'tool', text: 'Loaded Tableau and Snowflake requirement guidance.' },
    { kind: 'output', text: 'What should the dashboard display and who will use it?' },
  ]
  const recentSegments = [
    { kind: 'input', text: 'Summarize the current project state.' },
    { kind: 'output', text: 'The project dashboard baseline is available.' },
  ]
  return {
    ...(baseRow ?? runBoard().recent[0]),
    run_id: runId,
    error: null,
    ended_ts: null,
    ended_ago: '',
    segments: runId === 'run-active-1' ? activeSegments : recentSegments,
    body: '',
    truncated: false,
    ...over,
  }
}

function scheduledJobs(over: Partial<ScheduledJobsResult> = {}): ScheduledJobsResult {
  return {
    connected: true,
    jobs: [
      {
        project: 'kaidera-os',
        id: 'lead-planning-heartbeat',
        name: 'Lead planning heartbeat',
        enabled: true,
        schedule: { kind: 'interval', every_seconds: 3600 },
        payload: {
          from_agent: 'ren',
          to_role: 'lead',
          summary: 'Review scheduled project work.',
        },
        next_run_at: '2026-06-08T11:00:00Z',
        last_run_at: null,
        last_status: null,
        last_error: null,
      },
    ],
    ...over,
  }
}

/** A fake read client recording calls + resolving with the existing-endpoint shapes. */
function fakeClient(over: Partial<DashboardClient> = {}): DashboardClient {
  return {
    agentEpics: vi.fn().mockResolvedValue(epics()),
    analyticsKpis: vi.fn().mockResolvedValue(kpis()),
    dispatchBoard: vi.fn().mockResolvedValue(board()),
    setFlags: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      autonomous: true,
      propose_mode: false,
      ok: true,
    }),
    history: vi.fn().mockResolvedValue(history()),
    runBoard: vi.fn().mockResolvedValue(runBoard()),
    run: vi.fn((runId: string) => Promise.resolve(transcript(runId))),
    scheduledJobs: vi.fn().mockResolvedValue(scheduledJobs()),
    saveScheduledJob: vi.fn().mockResolvedValue({
      ok: true,
      job: scheduledJobs().jobs[0],
    }),
    runScheduledJobNow: vi.fn().mockResolvedValue({
      ok: true,
      job: scheduledJobs().jobs[0],
    }),
    savePlanningBeat: vi.fn().mockResolvedValue({
      ok: true,
      job: {
        ...scheduledJobs().jobs[0],
        id: 'pm-planning-beat',
        name: 'PM planning beat',
      },
    }),
    deleteScheduledJob: vi.fn().mockResolvedValue({
      ok: true,
      deleted: true,
      id: scheduledJobs().jobs[0].id,
    }),
    exportAutomationFeeders: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      version: 1,
      scheduled_jobs: scheduledJobs().jobs,
      connected: true,
    }),
    importAutomationFeeders: vi.fn().mockResolvedValue({
      ok: true,
      imported: { scheduled_jobs: 1 },
      errors: [],
    }),
    ...over,
  }
}

function renderView(props: Partial<Parameters<typeof DashboardView>[0]> = {}) {
  const client = props.client ?? fakeClient()
  // pollMs=0 keeps the tests deterministic (no background interval).
  render(
    <DashboardView
      project="kaidera-os"
      projectRow={projectRow()}
      client={client}
      pollMs={0}
      {...props}
    />,
  )
  return { client }
}

describe('DashboardView — header + vitals', () => {
  it('renders the project header (display name · status · repo_root, NO hex chip)', async () => {
    renderView({ projectRow: projectRow({ project_hex: '5872' }) })
    const header = await screen.findByTestId('dashboard-header')
    expect(within(header).getByText('Kaidera OS')).toBeInTheDocument()
    // the project hex is GONE from the new identity model — no `:<hex>` / `:????` chip renders.
    expect(within(header).queryByText(/:5872/)).not.toBeInTheDocument()
    expect(within(header).queryByText(/:\?\?\?\?/)).not.toBeInTheDocument()
    // the status badge is its OWN element (exact match avoids colliding with "Active tasks")
    expect(within(header).getByText('active', { exact: true })).toBeInTheDocument()
    expect(within(header).getByText('/Users/amad/DevVault/kaidera-os')).toBeInTheDocument()
  })

  it('renders the vitals stat cards from the epics/kpis/board endpoints', async () => {
    renderView()
    const vitals = await screen.findByTestId('dashboard-vitals')
    // agents (from the project row), pending handoffs, active + pending tasks, events/24h
    expect(within(vitals).getByText('Workers').closest('[data-stat]')).toHaveTextContent('3')
    expect(within(vitals).getByText(/pending handoffs/i).closest('[data-stat]')).toHaveTextContent('2')
    expect(within(vitals).getByText(/active tasks/i).closest('[data-stat]')).toHaveTextContent('4')
    expect(within(vitals).getByText(/pending tasks/i).closest('[data-stat]')).toHaveTextContent('7')
    expect(within(vitals).getByText(/events · 24h/i).closest('[data-stat]')).toHaveTextContent('51')
  })

  it('fetches the composing endpoints for the project', async () => {
    const { client } = renderView()
    await waitFor(() => expect(client.agentEpics).toHaveBeenCalledWith('kaidera-os', expect.anything()))
    expect(client.analyticsKpis).toHaveBeenCalledWith('kaidera-os', expect.anything())
    expect(client.dispatchBoard).toHaveBeenCalledWith('kaidera-os', expect.anything())
    expect(client.history).toHaveBeenCalledWith('kaidera-os', expect.any(Number), expect.anything())
    expect(client.runBoard).toHaveBeenCalledWith('kaidera-os', expect.anything())
  })

  it('offers a project-level Explain launcher near the dashboard header', async () => {
    const user = userEvent.setup()
    const onExplainProject = vi.fn()
    renderView({ onExplainProject })

    await user.click(await screen.findByRole('button', { name: /explain project/i }))

    expect(onExplainProject).toHaveBeenCalledTimes(1)
  })

  it('renders project autonomy controls below the project working folder', async () => {
    renderView({
      client: fakeClient({
        dispatchBoard: vi.fn().mockResolvedValue(board({ autonomous_on: true, propose_mode_on: false })),
      }),
    })

    const panel = await screen.findByTestId('dashboard-autonomy')
    expect(within(panel).getByText('Project dispatch')).toBeInTheDocument()
    expect(within(panel).getByText('Propose mode')).toBeInTheDocument()
    expect(within(panel).getByText(/global autonomy engine/i)).toBeInTheDocument()
  })

  it('toggles project dispatch from the Dashboard', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      dispatchBoard: vi.fn().mockResolvedValue(board({ autonomous_on: false, propose_mode_on: false })),
    })
    renderView({ client })

    await user.click(await screen.findByRole('switch', { name: /^project dispatch$/i }))

    await waitFor(() =>
      expect(client.setFlags).toHaveBeenCalledWith('kaidera-os', { autonomous: true }),
    )
  })

  it('toggles propose mode from the Dashboard', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      dispatchBoard: vi.fn().mockResolvedValue(board({ autonomous_on: true, propose_mode_on: false })),
      setFlags: vi.fn().mockResolvedValue({
        project: 'kaidera-os',
        autonomous: true,
        propose_mode: true,
        ok: true,
      }),
    })
    renderView({ client })

    await user.click(await screen.findByRole('switch', { name: /propose mode/i }))

    await waitFor(() =>
      expect(client.setFlags).toHaveBeenCalledWith('kaidera-os', { propose_mode: true }),
    )
  })

  it('renders project automation schedules on the Dashboard', async () => {
    renderView()

    const panel = await screen.findByTestId('dashboard-automation')
    expect(within(panel).getByText('Automation feeders')).toBeInTheDocument()
    expect(within(panel).getByText('Lead planning heartbeat')).toBeInTheDocument()
    expect(within(panel).getByText(/project dispatch, propose mode, and agent auto-dispatch/i)).toBeInTheDocument()
  })

  it('saves a scheduled handoff from the Dashboard', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    renderView({ client })

    const panel = await screen.findByTestId('dashboard-automation')
    const fromAgent = within(panel).getAllByLabelText(/from agent/i)[0]
    await user.clear(fromAgent)
    await user.type(fromAgent, 'ren')
    await user.click(within(panel).getByRole('button', { name: /save schedule/i }))

    await waitFor(() => expect(client.saveScheduledJob).toHaveBeenCalled())
    const saveCalls = vi.mocked(client.saveScheduledJob!).mock.calls
    const [, body] = saveCalls[0]
    expect(body.schedule).toEqual({ kind: 'interval', every_seconds: 3600 })
    expect(body.payload).toMatchObject({
      from_agent: 'ren',
      to_role: 'lead',
      summary: 'Review scheduled project work, update the plan, and create handoffs for the team.',
    })
  })

  it('creates the PM planning beat preset from the Dashboard', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      scheduledJobs: vi.fn().mockResolvedValue(scheduledJobs({ jobs: [] })),
    })
    renderView({ client })

    const panel = await screen.findByTestId('dashboard-automation')
    await user.click(within(panel).getByRole('button', { name: /create pm beat/i }))

    await waitFor(() =>
      expect(client.savePlanningBeat).toHaveBeenCalledWith(
        'kaidera-os',
        { enabled: true, every_minutes: 240 },
      ),
    )
  })

  it('deletes a scheduled job from the Dashboard', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    renderView({ client })

    const panel = await screen.findByTestId('dashboard-automation')
    await user.click(within(panel).getAllByRole('button', { name: /delete/i })[0])

    await waitFor(() =>
      expect(client.deleteScheduledJob).toHaveBeenCalledWith(
        'kaidera-os',
        'lead-planning-heartbeat',
      ),
    )
  })

  it('exports and imports automation feeder definitions from the Dashboard', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    renderView({ client })

    const panel = await screen.findByTestId('dashboard-automation')
    await user.click(within(panel).getByRole('button', { name: /export json/i }))
    const editor = within(panel).getByLabelText(/import \/ export definitions/i)

    await waitFor(() => expect((editor as HTMLTextAreaElement).value).toContain('scheduled_jobs'))
    await user.click(within(panel).getByRole('button', { name: /import json/i }))

    await waitFor(() =>
      expect(client.importAutomationFeeders).toHaveBeenCalledWith(
        'kaidera-os',
        expect.objectContaining({ scheduled_jobs: expect.any(Array) }),
      ),
    )
  })
})

describe('DashboardView — active-epic widget', () => {
  it('renders the active-epic progress + increments', async () => {
    renderView()
    const epic = await screen.findByTestId('dashboard-epic')
    expect(within(epic).getByText('E007')).toBeInTheDocument()
    expect(within(epic).getByText('Console SPA rebuild')).toBeInTheDocument()
    // the overall % shows
    expect(within(epic).getByText(/62%/)).toBeInTheDocument()
    // three increment segments draw
    expect(within(epic).getAllByTestId('epic-increment')).toHaveLength(3)
  })

  it('renders the "continuous · no epics" line when the project has no epics', async () => {
    const client = fakeClient({
      agentEpics: vi.fn().mockResolvedValue(
        epics({ epic: { mode: 'continuous', epic_count: 0, epics: [], label: 'continuous · no epics' } }),
      ),
    })
    renderView({ client })
    const epic = await screen.findByTestId('dashboard-epic')
    expect(within(epic).getByText(/continuous · no epics/i)).toBeInTheDocument()
  })
})

describe('DashboardView — recent activity + health', () => {
  it('renders a recent-activity strip from /history', async () => {
    renderView()
    const activity = await screen.findByTestId('dashboard-activity')
    expect(within(activity).getByText('cole')).toBeInTheDocument()
    expect(within(activity).getByText(/wrote app\/main\.py/)).toBeInTheDocument()
    expect(within(activity).getByText('ren')).toBeInTheDocument()
  })

  it('renders the Cortex health pill from /cortex/health', async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'healthy', base_url: 'http://localhost:8501', project: 'kaidera-os' }),
    } as Response)
    vi.stubGlobal('fetch', fetchFn)
    try {
      renderView()
      const pill = await screen.findByTestId('dashboard-health')
      expect(within(pill).getByText(/healthy/i)).toBeInTheDocument()
      // it read the CONSOLE JSON health endpoint (not a bare /health)
      expect(fetchFn).toHaveBeenCalledWith(
        expect.stringMatching(/^\/cortex\/health/),
        expect.anything(),
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('DashboardView — continuous worker feed', () => {
  it('renders project-level worker transcripts from run state', async () => {
    renderView()
    const feed = await screen.findByTestId('dashboard-worker-feed')

    await waitFor(() => expect(within(feed).getAllByText('Sample Worker').length).toBeGreaterThan(0))
    expect(within(feed).getByText('I need an airport operations dashboard.')).toBeInTheDocument()
    expect(within(feed).getByText('Identify dashboard intent and missing operational dimensions.')).toBeInTheDocument()
    expect(within(feed).getByText('Loaded Tableau and Snowflake requirement guidance.')).toBeInTheDocument()
    expect(within(feed).getByText('What should the dashboard display and who will use it?')).toBeInTheDocument()
  })

  it('shows an honest empty state when the project has no run-state rows', async () => {
    const client = fakeClient({
      runBoard: vi.fn().mockResolvedValue(runBoard({ active: [], recent: [], active_count: 0, recent_count: 0 })),
    })
    renderView({ client })

    const feed = await screen.findByTestId('dashboard-worker-feed')
    expect(within(feed).getByText(/no worker runs yet/i)).toBeInTheDocument()
  })
})

describe('DashboardView — loading / empty / error', () => {
  it('shows a loading hint before the first payload lands', () => {
    // a client whose epics never resolves → stays loading
    const client = fakeClient({ agentEpics: vi.fn().mockReturnValue(new Promise(() => {})) })
    renderView({ client })
    expect(screen.getByTestId('dashboard-loading')).toBeInTheDocument()
  })

  it('still renders the header + vitals (degraded) when the live reads fail', async () => {
    // Both Cortex-sourced reads (epics + kpis) fail → the counters have no source left.
    const client = fakeClient({
      agentEpics: vi.fn().mockRejectedValue(new Error('500')),
      analyticsKpis: vi.fn().mockRejectedValue(new Error('500')),
    })
    renderView({ client })
    // the header still renders from the project row (which the shell already has)
    const header = await screen.findByTestId('dashboard-header')
    expect(within(header).getByText('Kaidera OS')).toBeInTheDocument()
    // vitals degrade to '—' for the live-sourced counters (never a crash / fabricated 0)
    const vitals = await screen.findByTestId('dashboard-vitals')
    expect(within(vitals).getByText(/active tasks/i).closest('[data-stat]')).toHaveTextContent('—')
    // pending-tasks has NO fallback (epics-only) — it's the cleanest degraded assertion
    expect(within(vitals).getByText(/pending tasks/i).closest('[data-stat]')).toHaveTextContent('—')
    // but agents still shows (it comes from the project row, not a live read)
    expect(within(vitals).getByText('Workers').closest('[data-stat]')).toHaveTextContent('3')
  })

  it('prompts to select a project when none is selected', () => {
    renderView({ project: null, projectRow: null })
    expect(screen.getByText(/select a project/i)).toBeInTheDocument()
  })
})
