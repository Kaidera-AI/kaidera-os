import { describe, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { RunTranscriptView } from './RunTranscriptView'
import type { RunTranscript } from '../api'

/** A minimal RunTranscript with the given segments. */
function tx(segments: { kind: string; text: string }[]): RunTranscript {
  return {
    run_id: 'run-abcdef12',
    project: 'kaidera-os',
    agent: 'ren',
    agent_display: 'Ren',
    handoff_id: null,
    handoff_short: null,
    harness: 'claude-code',
    model: 'opus',
    status: 'ok',
    status_label: 'Completed',
    running: false,
    started_ts: null,
    started_ago: '',
    updated_ts: null,
    updated_ago: '',
    error: null,
    ended_ts: null,
    ended_ago: '',
    segments,
    body: segments.map((s) => s.text).join(''),
    truncated: false,
  } as unknown as RunTranscript
}

describe('RunTranscriptView — multi-turn input span (feature-gap step 6, Inc B)', () => {
  it('pins the first rendered transcript to the latest segment', async () => {
    const scrollHeightDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    const clientHeightDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
    const scrollTopDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTop')
    const scrollTops = new WeakMap<HTMLElement, number>()

    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get() {
        return this.getAttribute('data-testid') === 'run-transcript-body' ? 1800 : 0
      },
    })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
      configurable: true,
      get() {
        return this.getAttribute('data-testid') === 'run-transcript-body' ? 420 : 0
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
      render(
        <RunTranscriptView
          transcript={tx([
            { kind: 'input', text: 'first' },
            { kind: 'output', text: 'latest' },
          ])}
          live={false}
        />,
      )

      const body = screen.getByTestId('run-transcript-body')
      await waitFor(() => expect(body.scrollTop).toBe(1800))
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

  it("renders an `input` segment as a distinct user-message bubble (data-seg-kind='input')", () => {
    render(
      <RunTranscriptView
        transcript={tx([
          { kind: 'input', text: 'what is my name?' },
          { kind: 'output', text: 'You are Amad.' },
        ])}
        live={false}
      />,
    )
    // The user message renders.
    const userEl = screen.getByText('what is my name?')
    expect(userEl).toBeInTheDocument()
    // It is tagged as an input segment (a distinct user bubble, NOT agent output).
    const seg = userEl.closest('[data-seg-kind]')
    expect(seg).not.toBeNull()
    expect(seg?.getAttribute('data-seg-kind')).toBe('input')

    // The assistant reply renders as a (different) output segment.
    const replyEl = screen.getByText('You are Amad.')
    expect(replyEl.closest('[data-seg-kind]')?.getAttribute('data-seg-kind')).toBe('output')
  })

  it('renders output/tool/thinking segments as before (no regression)', () => {
    render(
      <RunTranscriptView
        transcript={tx([
          { kind: 'thinking', text: 'pondering' },
          { kind: 'tool', text: 'shell(ls)' },
          { kind: 'output', text: 'done' },
        ])}
        live={false}
      />,
    )
    expect(screen.getByText('pondering')).toBeInTheDocument()
    expect(screen.getByText('shell(ls)')).toBeInTheDocument()
    expect(screen.getByText('done')).toBeInTheDocument()
  })

  it('renders token-sized output spans as one readable assistant segment', () => {
    const { container } = render(
      <RunTranscriptView
        transcript={tx([
          { kind: 'input', text: 'hi Kai,' },
          { kind: 'output', text: 'Hi' },
          { kind: 'output', text: '!' },
          { kind: 'output', text: ' How' },
          { kind: 'output', text: ' can' },
          { kind: 'output', text: ' I' },
          { kind: 'output', text: ' help' },
          { kind: 'output', text: '?' },
        ])}
        live={false}
      />,
    )

    const outputs = container.querySelectorAll('[data-seg-kind="output"]')
    expect(outputs).toHaveLength(1)
    expect(outputs[0]).toHaveTextContent('Hi! How can I help?')
  })

  it("renders an `attachment` segment as a chip (data-seg-kind='attachment') — step 6 Inc A", () => {
    render(
      <RunTranscriptView
        transcript={tx([
          { kind: 'input', text: 'review this' },
          { kind: 'attachment', text: 'spec.txt' },
          { kind: 'output', text: 'looks good' },
        ])}
        live={false}
      />,
    )
    const chip = screen.getByText('spec.txt')
    expect(chip).toBeInTheDocument()
    expect(chip.closest('[data-seg-kind]')?.getAttribute('data-seg-kind')).toBe('attachment')
  })
})
