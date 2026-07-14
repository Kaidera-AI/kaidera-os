import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HistoryView } from './HistoryView'
import type { HistoryClient } from './HistoryView'
import type { HistoryPayload } from '../api'

// A representative shaped payload: a tool event, a message event, a reasoning event +
// two recent decisions + a roster agent count of 3.
function payload(over: Partial<HistoryPayload> = {}): HistoryPayload {
  return {
    events: [
      {
        ts: '2026-06-07T12:00:30Z',
        ts_ago: '2m',
        agent: 'ada',
        role: 'assistant',
        kind: 'tool',
        kind_label: 'action',
        summary: 'ran exec_command · pytest -q',
      },
      {
        ts: '2026-06-07T12:00:20Z',
        ts_ago: '3m',
        agent: 'ivy',
        role: 'assistant',
        kind: 'say',
        kind_label: 'message',
        summary: 'shipped the history endpoint and verified the tests',
      },
      {
        ts: '2026-06-07T12:00:10Z',
        ts_ago: '4m',
        agent: 'ada',
        role: 'assistant',
        kind: 'think',
        kind_label: 'reasoning',
        summary: 'reasoned about the next step',
      },
    ],
    decisions: [
      {
        ts: '2026-06-07T11:00:00Z',
        ts_ago: '1h',
        agent: 'ada',
        summary: 'decided to graceful-degrade the history endpoint to empty lists',
        source: 'decisions',
        category: 'architecture',
      },
      {
        ts: '',
        ts_ago: '',
        agent: '',
        summary: 'lesson: always summarise the noisy /history content before rendering',
        source: 'lessons',
        category: 'ux',
      },
    ],
    agent_count: 3,
    ...over,
  }
}

function fakeClient(over: Partial<HistoryClient> = {}): HistoryClient {
  return {
    history: vi.fn<(...a: unknown[]) => Promise<HistoryPayload>>().mockResolvedValue(payload()),
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('HistoryView — load + render', () => {
  it('fetches the timeline for the project on mount and renders the events', async () => {
    const client = fakeClient()
    // pollMs=0 disables the interval so the test only sees the initial fetch.
    render(<HistoryView project="kaidera-os" client={client} pollMs={0} />)

    await waitFor(() => expect(client.history).toHaveBeenCalledWith(
      'kaidera-os',
      undefined,
      expect.anything(),
      { includeDecisions: true },
    ))

    const timeline = await screen.findByTestId('history-timeline')
    // Each event renders its agent + its summarised line + a relative time.
    expect(within(timeline).getByText('ran exec_command · pytest -q')).toBeInTheDocument()
    expect(within(timeline).getByText('shipped the history endpoint and verified the tests')).toBeInTheDocument()
    expect(within(timeline).getByText('reasoned about the next step')).toBeInTheDocument()
    // The agent name is shown on a row (ada appears on two rows).
    expect(within(timeline).getAllByText('ada').length).toBeGreaterThanOrEqual(1)
    // A relative-age label (server-computed) is rendered.
    expect(within(timeline).getByText('2m')).toBeInTheDocument()
  })

  it('renders the recent-decisions side panel from the search feed', async () => {
    render(<HistoryView project="kaidera-os" client={fakeClient()} pollMs={0} />)
    const panel = await screen.findByTestId('history-decisions')
    expect(within(panel).getByText(/graceful-degrade the history endpoint/)).toBeInTheDocument()
    expect(within(panel).getByText(/always summarise the noisy/)).toBeInTheDocument()
    // The source layer chips surface (decisions / lessons).
    expect(within(panel).getByText('decisions')).toBeInTheDocument()
    expect(within(panel).getByText('lessons')).toBeInTheDocument()
  })

  it('shows the agent count + event count in the header', async () => {
    render(<HistoryView project="kaidera-os" client={fakeClient()} pollMs={0} />)
    const counts = await screen.findByTestId('history-counts')
    // 3 events + 3 agents (the roster count).
    expect(counts.textContent).toContain('3')
    expect(counts.textContent).toMatch(/agents/)
    expect(counts.textContent).toMatch(/events/)
  })

  it('forwards no limit by default (the backend bounds the window itself)', async () => {
    const client = fakeClient()
    render(<HistoryView project="kaidera-os" client={client} pollMs={0} />)
    await waitFor(() => expect(client.history).toHaveBeenCalled())
    // The component leaves `limit` undefined — the backend caps the rendered timeline.
    expect(client.history).toHaveBeenCalledWith(
      'kaidera-os',
      undefined,
      expect.anything(),
      { includeDecisions: true },
    )
  })
})

describe('HistoryView — refresh', () => {
  it('the Refresh button re-fetches the timeline', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<HistoryView project="kaidera-os" client={client} pollMs={0} />)
    await waitFor(() => expect(client.history).toHaveBeenCalledTimes(1))

    await user.click(screen.getByRole('button', { name: /refresh the activity timeline/i }))
    await waitFor(() => expect(client.history).toHaveBeenCalledTimes(2))
  })
})

describe('HistoryView — states', () => {
  it('shows a no-project hint when no project is selected', () => {
    render(<HistoryView project={null} client={fakeClient()} pollMs={0} />)
    expect(screen.getByText(/select a project/i)).toBeInTheDocument()
  })

  it('shows an empty state when there are no events', async () => {
    const client = fakeClient({
      history: vi
        .fn<(...a: unknown[]) => Promise<HistoryPayload>>()
        .mockResolvedValue({ events: [], decisions: [], agent_count: 0 }),
    })
    render(<HistoryView project="kaidera-os" client={client} pollMs={0} />)
    expect(await screen.findByTestId('history-empty')).toBeInTheDocument()
  })

  it('shows an error state when the fetch fails (and degrades, not crashes)', async () => {
    const client = fakeClient({
      history: vi
        .fn<(...a: unknown[]) => Promise<HistoryPayload>>()
        .mockRejectedValue(new Error('history down')),
    })
    render(<HistoryView project="kaidera-os" client={client} pollMs={0} />)
    expect(await screen.findByTestId('history-error')).toBeInTheDocument()
  })

  it('retry after an error re-fetches', async () => {
    const user = userEvent.setup()
    const history = vi
      .fn<(...a: unknown[]) => Promise<HistoryPayload>>()
      .mockRejectedValueOnce(new Error('history down'))
      .mockResolvedValue(payload())
    render(<HistoryView project="kaidera-os" client={{ history }} pollMs={0} />)
    const err = await screen.findByTestId('history-error')
    await user.click(within(err).getByRole('button', { name: /retry/i }))
    // After the retry the timeline renders (the second call resolves).
    expect(await screen.findByTestId('history-timeline')).toBeInTheDocument()
    await waitFor(() =>
      expect(within(screen.getByTestId('history-timeline')).getByText(/shipped the history endpoint/)).toBeInTheDocument(),
    )
  })
})

describe('HistoryView — project switch', () => {
  it('refetches when the project changes', async () => {
    const client = fakeClient()
    const { rerender } = render(<HistoryView project="kaidera-os" client={client} pollMs={0} />)
    await waitFor(() => expect(client.history).toHaveBeenCalledWith(
      'kaidera-os',
      undefined,
      expect.anything(),
      { includeDecisions: true },
    ))

    rerender(<HistoryView project="other" client={client} pollMs={0} />)
    await waitFor(() => expect(client.history).toHaveBeenCalledWith(
      'other',
      undefined,
      expect.anything(),
      { includeDecisions: true },
    ))
  })
})
