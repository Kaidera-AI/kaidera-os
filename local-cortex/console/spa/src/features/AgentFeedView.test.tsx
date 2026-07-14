import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { AgentFeedView, type AgentFeedClient } from './AgentFeedView'
import type { RunRow, RunSegment, RunTranscript } from '../api'

function row(runId: string, updatedAgo: string): RunRow {
  return {
    run_id: runId,
    project: 'kaidera-os',
    agent: 'ren',
    agent_display: 'Ren',
    handoff_id: null,
    handoff_short: null,
    model: 'opus',
    harness: 'claude-code',
    status: 'completed',
    running: false,
    started_ts: null,
    updated_ts: null,
    started_ago: updatedAgo,
    updated_ago: updatedAgo,
    status_label: 'completed',
  }
}

function tx(runId: string, segments: RunSegment[], over: Partial<RunTranscript> = {}): RunTranscript {
  return {
    ...row(runId, '1m'),
    error: null,
    ended_ts: null,
    ended_ago: '',
    segments,
    body: segments.map((seg) => seg.text).join(''),
    truncated: false,
    ...over,
  }
}

function client(transcripts: Record<string, RunTranscript>): AgentFeedClient {
  return {
    run: vi.fn(async (runId: string) => transcripts[runId]),
  }
}

describe('AgentFeedView', () => {
  it('pins the first hydrated feed view to the latest messages', async () => {
    const scrollHeightDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    const clientHeightDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
    const scrollTopDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTop')
    const scrollTops = new WeakMap<HTMLElement, number>()

    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get() {
        return this.getAttribute('data-testid') === 'agent-continuous-feed' ? 2400 : 0
      },
    })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
      configurable: true,
      get() {
        return this.getAttribute('data-testid') === 'agent-continuous-feed' ? 480 : 0
      },
    })
    Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
      configurable: true,
      get() {
        return scrollTops.get(this) ?? 0
      },
      set(value: number) {
        scrollTops.set(this, value)
      },
    })

    try {
      const fake = client({
        'run-1': tx('run-1', [
          { kind: 'input', text: 'older question' },
          { kind: 'output', text: 'latest answer' },
        ]),
      })

      render(
        <AgentFeedView
          runs={[row('run-1', 'now')]}
          liveTranscript={null}
          selectedRunId={null}
          live={false}
          client={fake}
        />,
      )

      await screen.findByText('latest answer')
      const feed = screen.getByTestId('agent-continuous-feed')
      await waitFor(() => expect(feed.scrollTop).toBe(2400))
    } finally {
      if (scrollHeightDesc) {
        Object.defineProperty(HTMLElement.prototype, 'scrollHeight', scrollHeightDesc)
      } else {
        Reflect.deleteProperty(HTMLElement.prototype, 'scrollHeight')
      }
      if (clientHeightDesc) {
        Object.defineProperty(HTMLElement.prototype, 'clientHeight', clientHeightDesc)
      } else {
        Reflect.deleteProperty(HTMLElement.prototype, 'clientHeight')
      }
      if (scrollTopDesc) {
        Object.defineProperty(HTMLElement.prototype, 'scrollTop', scrollTopDesc)
      } else {
        Reflect.deleteProperty(HTMLElement.prototype, 'scrollTop')
      }
    }
  })

  it('stitches recent runs into one chronological text feed with thinking/tool spans', async () => {
    const fake = client({
      'run-1': tx('run-1', [
        { kind: 'input', text: 'first question' },
        { kind: 'thinking', text: 'thinking about the first answer' },
        { kind: 'output', text: 'first answer' },
      ]),
      'run-2': tx('run-2', [
        { kind: 'input', text: 'second question' },
        { kind: 'tool', text: 'shell(ls)' },
        { kind: 'output', text: 'second answer' },
      ]),
    })

    render(
      <AgentFeedView
        runs={[row('run-2', 'now'), row('run-1', '1m')]}
        liveTranscript={null}
        selectedRunId={null}
        live={false}
        client={fake}
      />,
    )

    await screen.findByText('first question')
    const feed = screen.getByTestId('agent-continuous-feed')
    const text = feed.textContent ?? ''
    expect(text.indexOf('first question')).toBeLessThan(text.indexOf('second question'))
    expect(within(feed).getByText('thinking about the first answer')).toBeInTheDocument()
    expect(within(feed).getByText('shell(ls)')).toBeInTheDocument()
    expect(within(feed).getAllByText('Ren').length).toBeGreaterThan(0)
  })

  it('renders token-sized output spans as one assistant line', async () => {
    const fake = client({
      'run-1': tx('run-1', [
        { kind: 'input', text: 'hi Kai,' },
        { kind: 'output', text: 'Hi' },
        { kind: 'output', text: '!' },
        { kind: 'output', text: ' How' },
        { kind: 'output', text: ' can' },
        { kind: 'output', text: ' I' },
        { kind: 'output', text: ' help' },
        { kind: 'output', text: '?' },
      ]),
    })

    render(
      <AgentFeedView
        runs={[row('run-1', 'now')]}
        liveTranscript={null}
        selectedRunId={null}
        live={false}
        client={fake}
      />,
    )

    const feed = await screen.findByTestId('agent-continuous-feed')
    await waitFor(() => expect(feed).toHaveTextContent('Hi! How can I help?'))
    const outputLines = feed.querySelectorAll('[data-feed-line][data-seg-kind="output"]')
    expect(outputLines).toHaveLength(1)
    expect(outputLines[0]).toHaveTextContent('Hi! How can I help?')
  })

  it('uses the live SSE transcript until the durable run is ready to hydrate', async () => {
    const fake = client({
      'run-2': tx('run-2', [{ kind: 'output', text: 'durable output' }]),
    })
    const liveTranscript = tx(
      'run-2',
      [
        { kind: 'input', text: 'what changed?' },
        { kind: 'output', text: 'streaming now' },
      ],
      { running: true, status: 'running', status_label: 'running' },
    )

    const view = render(
      <AgentFeedView
        runs={[row('run-2', 'now')]}
        liveTranscript={liveTranscript}
        selectedRunId="run-2"
        live
        client={fake}
      />,
    )

    expect(await screen.findByText('streaming now')).toBeInTheDocument()
    expect(screen.queryByText('durable output')).not.toBeInTheDocument()
    expect(fake.run).not.toHaveBeenCalled()

    view.rerender(
      <AgentFeedView
        runs={[row('run-2', 'now')]}
        liveTranscript={null}
        selectedRunId="run-2"
        live={false}
        client={fake}
      />,
    )

    expect(await screen.findByText('durable output')).toBeInTheDocument()
    expect(fake.run).toHaveBeenCalledTimes(1)
  })

  it('hydrates only a newly appended run instead of reloading the whole feed', async () => {
    const fake = client({
      'run-1': tx('run-1', [{ kind: 'output', text: 'first answer' }]),
      'run-2': tx('run-2', [{ kind: 'output', text: 'second answer' }]),
    })
    const view = render(
      <AgentFeedView
        runs={[row('run-1', '1m')]}
        liveTranscript={null}
        selectedRunId="run-1"
        live={false}
        client={fake}
      />,
    )

    await screen.findByText('first answer')
    expect(fake.run).toHaveBeenCalledTimes(1)

    view.rerender(
      <AgentFeedView
        runs={[row('run-2', 'now'), row('run-1', '1m')]}
        liveTranscript={null}
        selectedRunId="run-2"
        live={false}
        client={fake}
      />,
    )

    await screen.findByText('second answer')
    const calls = vi.mocked(fake.run).mock.calls.map(([runId]) => runId)
    expect(calls).toEqual(['run-1', 'run-2'])
  })

  it('waits for a queued run to become durable before hydrating it', async () => {
    const fake = client({
      'run-1': tx('run-1', [{ kind: 'output', text: 'durable answer' }]),
    })
    const queued = {
      ...row('run-1', 'now'),
      status: 'queued',
      status_label: 'queued',
      running: true,
    }
    const view = render(
      <AgentFeedView
        runs={[queued]}
        liveTranscript={null}
        selectedRunId="run-1"
        live
        client={fake}
      />,
    )

    await waitFor(() => expect(fake.run).not.toHaveBeenCalled())

    view.rerender(
      <AgentFeedView
        runs={[{ ...queued, status: 'running', status_label: 'running' }]}
        liveTranscript={null}
        selectedRunId="run-1"
        live
        client={fake}
      />,
    )

    expect(await screen.findByText('durable answer')).toBeInTheDocument()
    expect(fake.run).toHaveBeenCalledTimes(1)
  })

  it('renders the no-run empty state', () => {
    render(
      <AgentFeedView
        runs={[]}
        liveTranscript={null}
        selectedRunId={null}
        live={false}
        emptyHint="No runs yet."
        client={client({})}
      />,
    )

    expect(screen.getByText('No runs yet.')).toBeInTheDocument()
  })

  it('renders loading and error states without splitting the pane into per-run cards', async () => {
    const broken: AgentFeedClient = {
      run: vi.fn(async () => {
        throw new Error('run store offline')
      }),
    }

    render(
      <AgentFeedView
        runs={[row('run-err', 'now')]}
        liveTranscript={null}
        selectedRunId={null}
        live={false}
        client={broken}
      />,
    )

    expect(screen.getAllByText('Loading feed...').length).toBeGreaterThan(0)
    expect(await screen.findByText(/Feed could not load: run store offline/i)).toBeInTheDocument()
    expect(screen.getByTestId('agent-continuous-feed').querySelectorAll('section')).toHaveLength(0)
  })
})
