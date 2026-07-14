import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useRunStateStream } from './useRunStateStream'
import type { RunStateFrame } from './types'

/** A minimal controllable EventSource stand-in for jsdom. */
class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  closed = false
  private listeners: Record<string, ((ev: unknown) => void)[]> = {}

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
  addEventListener(type: string, cb: (ev: unknown) => void) {
    ;(this.listeners[type] ||= []).push(cb)
  }
  removeEventListener(type: string, cb: (ev: unknown) => void) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((c) => c !== cb)
  }
  close() {
    this.closed = true
  }
  emit(type: string, ev: unknown) {
    for (const cb of this.listeners[type] ?? []) cb(ev)
  }
}

afterEach(() => {
  FakeEventSource.instances = []
  vi.unstubAllGlobals()
})

describe('useRunStateStream', () => {
  it('opens a scoped EventSource and surfaces parsed runstate frames', async () => {
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)

    const { result } = renderHook(() =>
      useRunStateStream({ project: 'kaidera-os', agent: 'ren', run: 'run-9' }),
    )

    // URL carries project/agent/run.
    const es = FakeEventSource.instances[0]
    expect(es).toBeDefined()
    expect(es.url).toContain('project=kaidera-os')
    expect(es.url).toContain('agent=ren')
    expect(es.url).toContain('run=run-9')
    expect(result.current.status).toBe('connecting')

    const frame: RunStateFrame = {
      project: 'kaidera-os',
      agent: 'ren',
      wake_run_id: 'run-9',
      running: 1,
      count: 2,
      selected_id: 'run-9',
      selected: null,
      html: '<div/>',
    }

    act(() => {
      es.emit('open', {})
      es.emit('runstate', { data: JSON.stringify(frame) })
    })

    await waitFor(() => expect(result.current.status).toBe('open'))
    expect(result.current.frame).toEqual(frame)
  })

  it('does not subscribe when project or agent is null', () => {
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
    const { result } = renderHook(() => useRunStateStream({ project: null, agent: null }))
    expect(FakeEventSource.instances).toHaveLength(0)
    expect(result.current.status).toBe('idle')
  })

  it('closes the EventSource on unmount', () => {
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
    const { unmount } = renderHook(() =>
      useRunStateStream({ project: 'kaidera-os', agent: 'ren' }),
    )
    const es = FakeEventSource.instances[0]
    expect(es.closed).toBe(false)
    unmount()
    expect(es.closed).toBe(true)
  })
})
