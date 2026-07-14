import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AgentsColumn } from './AgentsColumn'
import type {
  AgentEpicsPayload,
  AgentView,
  AgentsCatalog,
  DispatchBoard,
  RunBoard,
} from '../api'

function agent(over: Partial<AgentView> = {}): AgentView {
  return {
    name: 'ada',
    display_name: 'Ada',
    initials: 'AD',
    role: 'full-stack',
    model: 'claude-opus-4-8[1m]',
    model_label: 'Opus',
    harness: 'claude-code',
    harness_label: 'Claude Code',
    thinking: 'high',
    writer_scope: 'project',
    capabilities: ['python', 'react', 'qa'],
    row_sub: 'Claude Code · Opus · high reasoning',
    is_test: false,
    interactive: false,
    designation_override: false,
    cpo_tag: false,
    ...over,
  }
}

function catalog(over: Partial<AgentsCatalog> = {}): AgentsCatalog {
  return {
    project: 'demo',
    interactive: [agent({ name: 'lead', display_name: 'Lead', interactive: true, cpo_tag: true })],
    autonomous: [agent()],
    orchestrator: 'Orchestrator',
    lead: 'lead',
    ...over,
  }
}

function runBoard(over: Partial<RunBoard> = {}): RunBoard {
  return { project: 'demo', active: [], active_count: 0, recent: [], recent_count: 0, ...over }
}

function dispatch(over: Partial<DispatchBoard> = {}): DispatchBoard {
  return {
    project: 'demo',
    rows: [],
    dispatch_count: 0,
    dispatch_proposed_count: 0,
    dispatch_unassigned_count: 0,
    autonomous_on: true,
    propose_mode_on: false,
    awaiting_approval_ids: [],
    ...over,
  }
}

function epics(over: Partial<AgentEpicsPayload> = {}): AgentEpicsPayload {
  return {
    project: 'demo',
    epic: {
      mode: 'epics',
      epic_count: 1,
      epics: [
        {
          epic_id: 'E007',
          title: 'Console parity',
          status: 'build',
          overall_pct: 62,
          increment_count: 3,
          is_active: true,
          increments: [
            { num: 1, label: 'Inc1', title: 'History', pct: 100, status: 'done', kind: 'done' },
            { num: 2, label: 'Inc2', title: 'Graph', pct: 40, status: 'in_progress', kind: 'prog' },
            { num: 3, label: 'Inc3', title: 'Polish', pct: 0, status: 'todo', kind: 'todo' },
          ],
        },
      ],
    },
    metrics: { active_tasks: 4, pending_tasks: 1, pending_handoffs: 2, events_24h: 17 },
    ...over,
  }
}

const baseProps = {
  project: 'demo',
  selectedAgent: null as string | null,
  onSelectAgent: () => {},
  loading: false,
  error: null as Error | null,
}

describe('AgentsColumn', () => {
  it('renders a MINIMALIST worker row: name + role, no harness/model/caps clutter', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={null}
      />,
    )
    // The name + the role (from config) are shown …
    expect(screen.getAllByText('Ada').length).toBeGreaterThan(0)
    expect(screen.getAllByText('full-stack').length).toBeGreaterThan(0)
    // … and the old busy bits are GONE from the row (they live in the main window now).
    expect(screen.queryByText('react')).not.toBeInTheDocument()
    expect(screen.queryByTitle(/writer scope/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Claude Code · Opus/)).not.toBeInTheDocument()
  })

  it('renders the metrics row (active/pending tasks · handoffs · events-24h) from the epics payload', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={epics()}
      />,
    )
    // the four metric tiles by label …
    expect(screen.getByText(/active tasks/i)).toBeInTheDocument()
    expect(screen.getByText(/pending tasks/i)).toBeInTheDocument()
    expect(screen.getByText(/pending handoffs/i)).toBeInTheDocument()
    expect(screen.getByText(/events/i)).toBeInTheDocument()
    // … and their values from the payload (active_tasks 4, pending_tasks 1, handoffs 2, events 17).
    expect(screen.getByText('17')).toBeInTheDocument()
  })

  it('fires the details affordance when the metrics drill-in is clicked', async () => {
    const user = userEvent.setup()
    const onShowMetricsDetails = vi.fn()
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={epics()}
        onShowMetricsDetails={onShowMetricsDetails}
      />,
    )
    await user.click(screen.getByRole('button', { name: /details/i }))
    expect(onShowMetricsDetails).toHaveBeenCalled()
  })

  it('renders the Active-Epic widget — epic id, overall %, and per-increment mini-bars', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={epics()}
      />,
    )
    expect(screen.getByText(/active epic/i)).toBeInTheDocument()
    expect(screen.getByText('E007')).toBeInTheDocument()
    expect(screen.getByText('62%')).toBeInTheDocument()
    // an overall progressbar + the three increment segments are present.
    const bars = screen.getAllByRole('progressbar')
    expect(bars.length).toBeGreaterThan(0)
    // the active epic exposes its increments by their per-increment title tooltip.
    expect(screen.getByTitle(/Inc2.*Graph/i)).toBeInTheDocument()
  })

  it('shows the continuous · no epics line when the project has no epics', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={epics({
          epic: { mode: 'continuous', epic_count: 0, epics: [], label: 'continuous · no epics' },
        })}
      />,
    )
    expect(screen.getByText(/continuous · no epics/i)).toBeInTheDocument()
    // no epic card / progressbar in continuous mode.
    expect(screen.queryByText('E007')).not.toBeInTheDocument()
  })

  it('renders em-dash metrics when the epics payload is absent (degraded)', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={null}
      />,
    )
    // the metrics block still renders its labels (the values degrade to '—').
    expect(screen.getByText(/active tasks/i)).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('keeps the existing roster groups + the rollup header pills', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard({ active_count: 2 })}
        dispatch={dispatch({ dispatch_count: 3 })}
        epics={epics()}
      />,
    )
    expect(screen.getByText('Interactive')).toBeInTheDocument()
    expect(screen.getAllByText('AI Workers').length).toBeGreaterThan(0)
    // the header rollup pills still show agents / pending / runs ("Agents" appears as both the
    // column header AND the rollup pill label, so match all).
    expect(screen.getAllByText('Workers').length).toBeGreaterThan(0)
    expect(screen.getByText('Lead')).toBeInTheDocument()
  })

  it('shows a loading hint before the roster arrives', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={null}
        runBoard={null}
        dispatch={null}
        epics={null}
        loading
      />,
    )
    expect(screen.getByText(/loading roster/i)).toBeInTheDocument()
  })

  it('shows an empty-roster note when the catalog has no agents', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog({ interactive: [], autonomous: [], lead: null })}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={null}
      />,
    )
    expect(screen.getByText(/no workers in this project/i)).toBeInTheDocument()
  })

  // -- "+ Add worker" affordance (feature-gap #81) ----------------------------
  it('shows "+ Add worker" only with a registration client + opens the modal', async () => {
    const user = userEvent.setup()
    const registrationClient = {
      configCatalog: vi.fn().mockResolvedValue({
        project: 'demo',
        harnesses: [],
        models_by_harness: {},
        reasoning_by_harness: {},
        default_harness: '',
        default_model: '',
      }),
      registerAgent: vi.fn(),
    }
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={null}
        registrationClient={registrationClient}
        onAgentRegistered={vi.fn()}
      />,
    )
    const addBtn = screen.getByRole('button', { name: /add worker/i })
    await user.click(addBtn)
    // the modal opens (its title + the Name field render)
    expect(await screen.findByRole('dialog', { name: /add worker/i })).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toBeInTheDocument()
  })

  it('hides "+ Add worker" without a registration client', () => {
    render(
      <AgentsColumn
        {...baseProps}
        catalog={catalog()}
        runBoard={runBoard()}
        dispatch={dispatch()}
        epics={null}
      />,
    )
    expect(screen.queryByRole('button', { name: /add worker/i })).not.toBeInTheDocument()
  })
})
