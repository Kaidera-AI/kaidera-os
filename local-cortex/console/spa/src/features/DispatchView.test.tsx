import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DispatchView } from './DispatchView'
import type { DispatchClient } from './DispatchView'
import type {
  DispatchActivity,
  DispatchBoard,
  DispatchRow,
  DispatchRunController,
  DispatchRunState,
} from '../api'

function row(over: Partial<DispatchRow> = {}): DispatchRow {
  return {
    id: 'h1',
    compound: 'h1:5872',
    summary: 'fix the boot cache',
    summary_full: 'fix the boot cache fully',
    from_agent: 'ren',
    to_target: 'kai',
    priority: 'high',
    proposed: {
      name: 'kai',
      display_name: 'Kai',
      harness: 'claude-code',
      harness_label: 'Claude Code',
      model: 'claude-opus-4-8[1m]',
    },
    created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    ...over,
  }
}

function board(over: Partial<DispatchBoard> = {}): DispatchBoard {
  return {
    project: 'kaidera-os',
    rows: [row()],
    dispatch_count: 1,
    dispatch_proposed_count: 1,
    dispatch_unassigned_count: 0,
    autonomous_on: false,
    propose_mode_on: false,
    awaiting_approval_ids: [],
    ...over,
  }
}

const IDLE: DispatchRunState = { running: false, output: '', runId: null, error: null, done: false }

/** A fake run-controller whose per-row state the test can drive. */
function fakeRunCtl(over: Partial<DispatchRunController> = {}): DispatchRunController & {
  run: ReturnType<typeof vi.fn>
} {
  const run = vi.fn().mockResolvedValue(undefined)
  return {
    run,
    stateFor: () => IDLE,
    ...over,
  } as DispatchRunController & { run: ReturnType<typeof vi.fn> }
}

function fakeClient(over: Partial<DispatchClient> = {}): DispatchClient {
  return {
    approveHandoff: vi.fn().mockResolvedValue(undefined),
    ...over,
  }
}

function activity(over: Partial<DispatchActivity> = {}): DispatchActivity {
  return {
    project: 'kaidera-os',
    activity: [],
    activity_count: 0,
    waves: [],
    waves_any: false,
    loop_running: false,
    inflight: 0,
    cap: 3,
    no_orch: false,
    ...over,
  }
}

/** Default props so each test overrides only what it cares about. */
function props(over: Partial<Parameters<typeof DispatchView>[0]> = {}) {
  return {
    project: 'kaidera-os',
    board: board(),
    loading: false,
    error: null,
    activity: activity(),
    client: fakeClient(),
    runCtl: fakeRunCtl(),
    onChanged: vi.fn(),
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('DispatchView — read-only board (unchanged)', () => {
  it('renders a row — summary, from→to, priority, and the proposed agent', () => {
    render(<DispatchView {...props()} />)
    expect(screen.getByText('fix the boot cache')).toBeInTheDocument()
    expect(screen.getByText('ren')).toBeInTheDocument()
    // 'kai' appears in to_target + the proposal; assert at least one.
    expect(screen.getAllByText('kai').length).toBeGreaterThan(0)
    expect(screen.getByText('high')).toBeInTheDocument()
    expect(screen.getByText('proposed')).toBeInTheDocument()
    expect(screen.getByText('Kai')).toBeInTheDocument()
  })

  it('shows the board counts', () => {
    render(
      <DispatchView
        {...props({
          board: board({
            dispatch_count: 3,
            dispatch_proposed_count: 2,
            dispatch_unassigned_count: 1,
            awaiting_approval_ids: [],
          }),
        })}
      />,
    )
    expect(screen.getByText('Waiting')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('marks an unassigned row when there is no proposal', () => {
    render(
      <DispatchView
        {...props({
          board: board({ rows: [row({ proposed: null })], dispatch_proposed_count: 0, dispatch_unassigned_count: 1 }),
        })}
      />,
    )
    expect(screen.getByText('unassigned')).toBeInTheDocument()
    expect(screen.queryByText('proposed')).not.toBeInTheDocument()
  })

  it('shows the dispatch resolution reason for blocked or unresolved rows', () => {
    render(
      <DispatchView
        {...props({
          board: board({
            rows: [
              row({
                proposed: null,
                resolution: {
                  status: 'blocked',
                  reason_code: 'human_target',
                  reason: 'Role cto targets a human/operator.',
                  target_type: 'role',
                  target: 'cto',
                },
              }),
            ],
            dispatch_proposed_count: 0,
            dispatch_unassigned_count: 1,
          }),
        })}
      />,
    )
    expect(screen.getByText('blocked')).toBeInTheDocument()
    expect(screen.getByText(/targets a human\/operator/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /approve & run/i })).toBeDisabled()
  })

  it('shows an empty state for a connected-but-empty board', () => {
    render(<DispatchView {...props({ board: board({ rows: [], dispatch_count: 0 }) })} />)
    expect(screen.getByText(/no open handoffs/i)).toBeInTheDocument()
  })

  it('shows a loading hint before any board arrives', () => {
    render(<DispatchView {...props({ board: null, loading: true })} />)
    expect(screen.getByText(/loading the dispatch board/i)).toBeInTheDocument()
  })

  it('shows an error hint (stale-backend 404) when the board fails to load', () => {
    render(<DispatchView {...props({ board: null, error: new Error('404') })} />)
    expect(screen.getByText(/couldn’t load the dispatch board/i)).toBeInTheDocument()
  })
})

describe('DispatchView — project dispatch status is read-only here', () => {
  it('renders project dispatch status and points users to the Dashboard control', () => {
    render(<DispatchView {...props({ board: board({ autonomous_on: true }) })} />)
    expect(screen.getAllByText(/project dispatch on/i).length).toBeGreaterThan(0)
    expect(screen.getByText(/dashboard control/i)).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: /project dispatch/i })).not.toBeInTheDocument()
  })
})

describe('DispatchView — Approve & Run per handoff', () => {
  it('renders an Approve & Run button per proposed row and runs it on click', async () => {
    const user = userEvent.setup()
    const runCtl = fakeRunCtl()
    render(<DispatchView {...props({ runCtl })} />)

    const btn = screen.getByRole('button', { name: /approve & run/i })
    await user.click(btn)

    expect(runCtl.run).toHaveBeenCalledWith({
      agentName: 'kai',
      summary: 'fix the boot cache',
      handoffId: 'h1',
      handoffCompound: 'h1:5872',
    })
  })

  it('disables Approve & Run for an unassigned row (nothing to run)', () => {
    render(
      <DispatchView
        {...props({ board: board({ rows: [row({ proposed: null })] }) })}
      />,
    )
    const btn = screen.getByRole('button', { name: /approve & run/i })
    expect(btn).toBeDisabled()
  })

  it('disables the button + shows a spinner label while that row is running', () => {
    const runCtl = fakeRunCtl({
      stateFor: (hid) =>
        hid === 'h1' ? { ...IDLE, running: true, runId: 'run-1' } : IDLE,
    })
    render(<DispatchView {...props({ runCtl })} />)
    const btn = screen.getByRole('button', { name: /running/i })
    expect(btn).toBeDisabled()
  })

  it('surfaces the streamed run output (run_id + assembled text) in a per-row panel', () => {
    const runCtl = fakeRunCtl({
      stateFor: (hid) =>
        hid === 'h1'
          ? { running: false, output: 'Working on it…', runId: 'run-xyz', error: null, done: true }
          : IDLE,
    })
    render(<DispatchView {...props({ runCtl })} />)
    expect(screen.getByText('Working on it…')).toBeInTheDocument()
    // the run id is surfaced so the operator can follow the live run.
    expect(screen.getByText(/run-xyz/)).toBeInTheDocument()
  })

  it('surfaces a per-row run error', () => {
    const runCtl = fakeRunCtl({
      stateFor: (hid) =>
        hid === 'h1'
          ? { running: false, output: '', runId: null, error: 'could not claim handoff', done: true }
          : IDLE,
    })
    render(<DispatchView {...props({ runCtl })} />)
    expect(screen.getByText(/could not claim handoff/i)).toBeInTheDocument()
  })
})

describe('DispatchView — propose-mode approval queue', () => {
  it('renders the awaiting-approval queue with an Approve per parked row', () => {
    render(
      <DispatchView
        {...props({
          board: board({
            rows: [row({ id: 'h9', compound: 'h9:5872', summary: 'parked work' })],
            propose_mode_on: true,
            awaiting_approval_ids: ['h9'],
            dispatch_count: 1,
          }),
        })}
      />,
    )
    const queue = screen.getByRole('region', { name: /awaiting approval/i })
    expect(within(queue).getByText('parked work')).toBeInTheDocument()
    expect(within(queue).getByRole('button', { name: /approve/i })).toBeInTheDocument()
  })

  it('clicking Approve posts approveHandoff and refetches', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    const onChanged = vi.fn()
    render(
      <DispatchView
        {...props({
          board: board({
            rows: [row({ id: 'h9', compound: 'h9:5872', summary: 'parked work' })],
            propose_mode_on: true,
            awaiting_approval_ids: ['h9'],
          }),
          client,
          onChanged,
        })}
      />,
    )
    const queue = screen.getByRole('region', { name: /awaiting approval/i })
    await user.click(within(queue).getByRole('button', { name: /approve/i }))

    expect(client.approveHandoff).toHaveBeenCalledWith('kaidera-os', 'h9')
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('does not render the approval queue when nothing is awaiting', () => {
    render(<DispatchView {...props()} />)
    expect(screen.queryByRole('region', { name: /awaiting approval/i })).not.toBeInTheDocument()
  })
})

describe('DispatchView — activity feed + wave strip', () => {
  it('renders the activity feed rows (orchestrator ring buffer)', () => {
    render(
      <DispatchView
        {...props({
          activity: activity({
            activity_count: 1,
            activity: [
              {
                kind: 'dispatched',
                level: 'success',
                text: 'ran kai on h1',
                agent: 'kai',
                handoff_short: 'h1abc',
                ago: '2m',
              },
            ],
            loop_running: true,
            inflight: 1,
          }),
        })}
      />,
    )
    const feed = screen.getByRole('region', { name: /dispatch activity/i })
    expect(within(feed).getByText('ran kai on h1')).toBeInTheDocument()
  })

  it('renders the wave-plan strip when a plan exists', () => {
    render(
      <DispatchView
        {...props({
          activity: activity({
            waves_any: true,
            waves: [{ epic: 'E007', active_wave: 2, running: 1, waiting: 3 }],
          }),
        })}
      />,
    )
    expect(screen.getByText('E007')).toBeInTheDocument()
    expect(screen.getByText(/wave/i)).toBeInTheDocument()
  })

  it('shows an idle activity hint when the feed is empty', () => {
    render(<DispatchView {...props({ activity: activity() })} />)
    const feed = screen.getByRole('region', { name: /dispatch activity/i })
    expect(within(feed).getByText(/nothing here|idle|watching/i)).toBeInTheDocument()
  })
})
