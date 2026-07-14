import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useExplainRun } from './useExplainRun'
import type { RunTranscript } from './types'

function run(over: Partial<RunTranscript> = {}): RunTranscript {
  return {
    run_id: 'run-1',
    project: 'kaidera-os',
    agent: 'console',
    agent_display: 'console',
    handoff_id: null,
    handoff_short: null,
    model: null,
    harness: null,
    status: 'running',
    running: true,
    started_ts: null,
    updated_ts: null,
    started_ago: '',
    updated_ago: '',
    status_label: 'running',
    error: null,
    ended_ts: null,
    ended_ago: '',
    segments: [],
    body: '',
    truncated: false,
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useExplainRun', () => {
  it('parks idle with no transcript when runId is null', () => {
    const getRun = vi.fn()
    const { result } = renderHook(() => useExplainRun({ runId: null, getRun }))
    expect(result.current.phase).toBe('idle')
    expect(result.current.transcript).toBeNull()
    expect(result.current.polling).toBe(false)
    expect(getRun).not.toHaveBeenCalled()
  })

  it('follows the run and surfaces RUNNING progress (segments stream in)', async () => {
    const getRun = vi.fn().mockResolvedValue(
      run({ status: 'running', running: true, segments: [{ kind: 'output', text: '<!DOCTYPE html>' }] }),
    )
    const { result } = renderHook(() => useExplainRun({ runId: 'run-1', getRun, pollMs: 50 }))

    await waitFor(() => expect(result.current.phase).toBe('running'))
    expect(getRun).toHaveBeenCalledWith('run-1', expect.anything())
    expect(result.current.polling).toBe(true)
    // The full HTML is only published on a terminal ok — not while running.
    expect(result.current.html).toBe('')
    expect(result.current.transcript?.segments).toHaveLength(1)
  })

  it('on terminal OK publishes the full HTML (concat of output spans) and stops polling', async () => {
    const getRun = vi.fn().mockResolvedValue(
      run({
        status: 'ok',
        running: false,
        status_label: 'completed',
        segments: [
          { kind: 'input', text: 'Explain file: app/main.py' },
          { kind: 'output', text: '<!DOCTYPE html><html>' },
          { kind: 'output', text: '<body>ok</body></html>' },
        ],
      }),
    )
    const { result } = renderHook(() => useExplainRun({ runId: 'run-1', getRun, pollMs: 30 }))

    await waitFor(() => expect(result.current.phase).toBe('ok'))
    expect(result.current.html).toBe('<!DOCTYPE html><html><body>ok</body></html>')
    expect(result.current.error).toBeNull()
    expect(result.current.polling).toBe(false)

    // Polling stops at terminal: after a terminal status, the call count settles (no
    // unbounded growth). Capture, wait past several poll intervals, assert no new calls.
    const settledCalls = getRun.mock.calls.length
    await new Promise((r) => setTimeout(r, 120))
    expect(getRun.mock.calls.length).toBe(settledCalls)
  })

  it('on terminal ERROR surfaces the error message and no html', async () => {
    const getRun = vi.fn().mockResolvedValue(
      run({ status: 'error', running: false, status_label: 'errored', error: 'generation was empty' }),
    )
    const { result } = renderHook(() => useExplainRun({ runId: 'run-1', getRun, pollMs: 30 }))

    await waitFor(() => expect(result.current.phase).toBe('error'))
    expect(result.current.error).toBe('generation was empty')
    expect(result.current.html).toBe('')
    expect(result.current.polling).toBe(false)
  })
})
