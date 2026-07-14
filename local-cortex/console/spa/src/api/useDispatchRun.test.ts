import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useDispatchRun } from './useDispatchRun'
import { api } from './client'

/** A ReadableStream emitting the given SSE string chunks (UTF-8). */
function sseStream(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  let i = 0
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(enc.encode(chunks[i]))
        i += 1
      } else {
        controller.close()
      }
    },
  })
}

function mockRun(chunks: string[]) {
  const resp = { ok: true, status: 200, body: sseStream(chunks) } as unknown as Response
  return vi.spyOn(api, 'dispatchRun').mockResolvedValue(resp)
}

const ROW = { agentName: 'kai', summary: 'do the work', handoffId: 'h1', handoffCompound: 'h1:5872' }

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useDispatchRun', () => {
  it('POSTs to /dispatch/{p}/run with agent + handoff fields and captures the run_id', async () => {
    const spy = mockRun([
      'event: run\ndata: {"run_id":"run-xyz"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const onRun = vi.fn()
    const { result } = renderHook(() => useDispatchRun({ project: 'kaidera-os', onRun }))

    await act(async () => {
      await result.current.run(ROW)
    })

    expect(spy).toHaveBeenCalledWith('kaidera-os', 'kai', {
      summary: 'do the work',
      handoff_id: 'h1',
      handoff_compound: 'h1:5872',
    })
    // The run_id from the run frame is surfaced (callback + per-row state) so the
    // operator can point the transcript at the live run.
    expect(onRun).toHaveBeenCalledWith('h1', 'run-xyz')
    expect(result.current.stateFor('h1').runId).toBe('run-xyz')
  })

  it('assembles streamed delta text into the per-row output', async () => {
    mockRun([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: delta\ndata: {"text":"Work"}\n\n',
      'event: delta\ndata: {"text":"ing…"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useDispatchRun({ project: 'kaidera-os' }))

    await act(async () => {
      await result.current.run(ROW)
    })

    expect(result.current.stateFor('h1').output).toBe('Working…')
    expect(result.current.stateFor('h1').error).toBeNull()
    expect(result.current.stateFor('h1').done).toBe(true)
  })

  it('prefers the result-frame text when no deltas streamed', async () => {
    mockRun([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: result\ndata: {"text":"All done."}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useDispatchRun({ project: 'kaidera-os' }))
    await act(async () => {
      await result.current.run(ROW)
    })
    expect(result.current.stateFor('h1').output).toBe('All done.')
  })

  it('is disabled-while-running: running flips true during the post, false after close', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => {
      release = r
    })
    const enc = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        controller.enqueue(enc.encode('event: run\ndata: {"run_id":"r1"}\n\n'))
        await gate
        controller.enqueue(enc.encode('event: done\ndata: {}\n\n'))
        controller.close()
      },
    })
    vi.spyOn(api, 'dispatchRun').mockResolvedValue({ ok: true, status: 200, body: stream } as unknown as Response)

    const { result } = renderHook(() => useDispatchRun({ project: 'kaidera-os' }))

    let runPromise!: Promise<void>
    await act(async () => {
      runPromise = result.current.run(ROW)
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.stateFor('h1').running).toBe(true))

    await act(async () => {
      release()
      await runPromise
    })
    expect(result.current.stateFor('h1').running).toBe(false)
  })

  it('surfaces an error frame as the per-row error (and stops running)', async () => {
    mockRun([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: error\ndata: {"message":"could not claim handoff","category":"claim_failed"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useDispatchRun({ project: 'kaidera-os' }))
    await act(async () => {
      await result.current.run(ROW)
    })
    expect(result.current.stateFor('h1').error).toBe('could not claim handoff')
    expect(result.current.stateFor('h1').running).toBe(false)
  })

  it('surfaces a transport failure as the per-row error and re-enables', async () => {
    vi.spyOn(api, 'dispatchRun').mockRejectedValue(new Error('Failed to fetch'))
    const { result } = renderHook(() => useDispatchRun({ project: 'kaidera-os' }))
    await act(async () => {
      await result.current.run(ROW)
    })
    expect(result.current.stateFor('h1').error).toMatch(/Failed to fetch/)
    expect(result.current.stateFor('h1').running).toBe(false)
  })

  it('does not run twice concurrently for the same row', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => {
      release = r
    })
    const enc = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        controller.enqueue(enc.encode('event: run\ndata: {"run_id":"r1"}\n\n'))
        await gate
        controller.close()
      },
    })
    const spy = vi
      .spyOn(api, 'dispatchRun')
      .mockResolvedValue({ ok: true, status: 200, body: stream } as unknown as Response)

    const { result } = renderHook(() => useDispatchRun({ project: 'kaidera-os' }))
    let p1!: Promise<void>
    await act(async () => {
      p1 = result.current.run(ROW)
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.stateFor('h1').running).toBe(true))
    // A second click while running is a no-op (guarded).
    await act(async () => {
      await result.current.run(ROW)
    })
    expect(spy).toHaveBeenCalledTimes(1)
    await act(async () => {
      release()
      await p1
    })
  })

  it('idle rows report a clean default state', () => {
    const { result } = renderHook(() => useDispatchRun({ project: 'kaidera-os' }))
    const s = result.current.stateFor('never-run')
    expect(s.running).toBe(false)
    expect(s.output).toBe('')
    expect(s.error).toBeNull()
    expect(s.runId).toBeNull()
    expect(s.done).toBe(false)
  })
})
