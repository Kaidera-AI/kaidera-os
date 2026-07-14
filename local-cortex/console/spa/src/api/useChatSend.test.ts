import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useChatSend } from './useChatSend'
import { readActiveChatRun } from './activeChatRun'
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

function mockChat(chunks: string[]) {
  const resp = { ok: true, status: 200, body: sseStream(chunks) } as unknown as Response
  return vi.spyOn(api, 'chat').mockResolvedValue(resp)
}

afterEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('useChatSend', () => {
  it('posts the message and captures the run_id from the run frame (onRun)', async () => {
    const chatSpy = mockChat([
      'event: run\ndata: {"run_id":"run-abc"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const onRun = vi.fn()
    const { result } = renderHook(() =>
      useChatSend({ project: 'kaidera-os', agent: 'ren', onRun }),
    )

    await act(async () => {
      await result.current.send('hello there')
    })

    // The POST carries (project, agent, message, sessionId, clientRunId, attachmentIds,
    // signal). The clientRunId is now ALWAYS minted up front (not only for attachments) so
    // the pane can pin to this run IMMEDIATELY; with no attachments the attachmentIds stay
    // undefined; the AbortSignal is the Stop/cancel handle threaded to the fetch.
    expect(chatSpy).toHaveBeenCalledWith(
      'kaidera-os', 'ren', 'hello there', expect.any(String), expect.any(String), undefined,
      expect.any(AbortSignal),
    )
    const sentRunId = chatSpy.mock.calls[0][4] as string
    // The run is pinned the INSTANT we send (onRun fires with the pre-minted id the POST
    // carries + the backend adopts) AND again when the `run` frame echoes — so the pane
    // follows the turn from first paint, regardless of the streamed frame's timing.
    expect(onRun).toHaveBeenCalledWith(sentRunId)
    expect(onRun).toHaveBeenCalledWith('run-abc')
    expect(result.current.runId).toBe('run-abc')
    // After done, the composer is re-enabled.
    await waitFor(() => expect(result.current.sending).toBe(false))
  })

  it('persists the pre-minted client run id for reconnect restore', async () => {
    const chatSpy = mockChat(['event: done\ndata: {}\n\n'])
    const { result } = renderHook(() => useChatSend({ project: 'demo-project', agent: 'ren' }))

    await act(async () => {
      await result.current.send('restore me')
    })

    const sessionId = chatSpy.mock.calls[0][3] as string
    const clientRunId = chatSpy.mock.calls[0][4] as string
    expect(readActiveChatRun('demo-project', 'ren', sessionId)).toBe(clientRunId)
  })

  it('echoes the sent message immediately and assembles streamed delta/result text', async () => {
    mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: delta\ndata: {"text":"Think"}\n\n',
      'event: delta\ndata: {"text":"ing."}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))

    await act(async () => {
      await result.current.send('do the thing')
    })

    // The user's message is echoed back (so it shows instantly, before the reply).
    expect(result.current.echo).toBe('do the thing')
    // Local-mode delta frames are assembled into the live reply text.
    expect(result.current.reply).toBe('Thinking.')
    expect(result.current.error).toBeNull()
  })

  it('prefers the result-frame text when no deltas streamed', async () => {
    mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: result\ndata: {"text":"All done."}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('hi')
    })
    expect(result.current.reply).toBe('All done.')
  })

  it('surfaces an error frame as the error state (clean error bubble)', async () => {
    mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: error\ndata: {"message":"model not available","category":"model_unavailable"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('hi')
    })
    expect(result.current.error).toBe('model not available')
    // The stream still closed → composer re-enabled.
    expect(result.current.sending).toBe(false)
  })

  it('is disabled-while-sending: sending flips true during the post and false after close', async () => {
    // A controllable stream so we can observe the in-flight sending state.
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => {
      release = r
    })
    const enc = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        controller.enqueue(enc.encode('event: run\ndata: {"run_id":"r1"}\n\n'))
        await gate // park until released
        controller.enqueue(enc.encode('event: done\ndata: {}\n\n'))
        controller.close()
      },
    })
    vi.spyOn(api, 'chat').mockResolvedValue({ ok: true, status: 200, body: stream } as unknown as Response)

    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))

    let sendPromise!: Promise<void>
    await act(async () => {
      sendPromise = result.current.send('hi')
      // Allow the run frame + the park to take effect.
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.sending).toBe(true))

    await act(async () => {
      release()
      await sendPromise
    })
    expect(result.current.sending).toBe(false)
  })

  it('stop() cancels the run, aborts the in-flight stream, re-enables, and surfaces no error', async () => {
    // A stream that parks until the turn's AbortSignal fires, then rejects the reader
    // with an AbortError — exactly how a real aborted fetch body behaves.
    let capturedSignal: AbortSignal | undefined
    const events: string[] = []
    const cancelSpy = vi.spyOn(api, 'cancelRun').mockImplementation(async (runId) => {
      events.push(`cancel:${runId}`)
      return { run_id: runId, cancelled: true }
    })
    const enc = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(enc.encode('event: run\ndata: {"run_id":"r1"}\n\n'))
        return new Promise<void>((_resolve, reject) => {
          // Park until aborted; on abort, fail the read like a torn-down fetch body.
          const sig = capturedSignal
          if (sig?.aborted) {
            reject(new DOMException('The user aborted a request.', 'AbortError'))
            return
          }
          sig?.addEventListener('abort', () => {
            events.push('abort')
            reject(new DOMException('The user aborted a request.', 'AbortError'))
          })
        })
      },
    })
    vi.spyOn(api, 'chat').mockImplementation((...args) => {
      capturedSignal = args[6] as AbortSignal | undefined
      return Promise.resolve({ ok: true, status: 200, body: stream } as unknown as Response)
    })

    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))

    let sendPromise!: Promise<void>
    await act(async () => {
      sendPromise = result.current.send('long running')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.sending).toBe(true))
    // The send received a real AbortSignal.
    expect(capturedSignal).toBeInstanceOf(AbortSignal)
    expect(capturedSignal?.aborted).toBe(false)

    // Stop → aborts the signal, flips sending off immediately.
    await act(async () => {
      result.current.stop()
      await sendPromise // the reader rejection is caught + suppressed (clean cancel)
    })

    expect(capturedSignal?.aborted).toBe(true)
    expect(cancelSpy).toHaveBeenCalledTimes(1)
    expect(events[0]).toMatch(/^cancel:/)
    expect(events[1]).toBe('abort')
    expect(result.current.sending).toBe(false)
    // An aborted stream is a clean stop — NOT an error toast.
    expect(result.current.error).toBeNull()
  })

  it('stop() before the run frame cancels the pre-minted clientRunId', async () => {
    let capturedSignal: AbortSignal | undefined
    const cancelSpy = vi.spyOn(api, 'cancelRun').mockResolvedValue({
      run_id: 'unused',
      cancelled: true,
    })
    const chatSpy = vi.spyOn(api, 'chat').mockImplementation((...args) => {
      capturedSignal = args[6] as AbortSignal | undefined
      return new Promise<Response>((_resolve, reject) => {
        capturedSignal?.addEventListener('abort', () => {
          reject(new DOMException('The user aborted a request.', 'AbortError'))
        })
      })
    })

    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))

    let sendPromise!: Promise<void>
    await act(async () => {
      sendPromise = result.current.send('long running')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.sending).toBe(true))

    const preMintedClientRunId = chatSpy.mock.calls[0][4] as string
    expect(result.current.runId).toBe(preMintedClientRunId)

    await act(async () => {
      result.current.stop()
      await sendPromise
    })

    expect(cancelSpy).toHaveBeenCalledWith(preMintedClientRunId)
    expect(capturedSignal?.aborted).toBe(true)
    expect(result.current.error).toBeNull()
  })

  it('stop() is a no-op when idle (no error, not sending)', () => {
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    act(() => {
      result.current.stop()
    })
    expect(result.current.sending).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('does not send a blank/whitespace message', async () => {
    const chatSpy = mockChat(['event: done\ndata: {}\n\n'])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('   ')
    })
    expect(chatSpy).not.toHaveBeenCalled()
  })

  it('uploads each file under one client_run_id, then sends with the ids (step 6)', async () => {
    const chatSpy = mockChat([
      'event: run\ndata: {"run_id":"run-1"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const uploadSpy = vi
      .spyOn(api, 'uploadAttachment')
      .mockImplementation(async (_p, _a, _rid, file) => ({
        attachment_id: `id-${file.name}`,
        filename: file.name,
        size_bytes: file.size,
      }))

    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    const f1 = new File(['aaa'], 'a.txt', { type: 'text/plain' })
    const f2 = new File(['bbb'], 'b.txt', { type: 'text/plain' })

    await act(async () => {
      await result.current.send('review these', [f1, f2])
    })

    // Both files were uploaded under the SAME client_run_id (the 3rd upload arg).
    expect(uploadSpy).toHaveBeenCalledTimes(2)
    const rid1 = uploadSpy.mock.calls[0][2]
    const rid2 = uploadSpy.mock.calls[1][2]
    const uploadSignal = uploadSpy.mock.calls[0][4]
    expect(rid1).toBe(rid2)
    expect(typeof rid1).toBe('string')
    expect(uploadSignal).toBeInstanceOf(AbortSignal)

    // The chat POST carries the SAME client_run_id (arg 5) + the collected ids (arg 6).
    const call = chatSpy.mock.calls[0]
    expect(call[4]).toBe(rid1)
    expect(call[5]).toEqual(['id-a.txt', 'id-b.txt'])
    expect(call[6]).toBe(uploadSignal)
  })

  it('rejects too many attachments with a friendly error (no send)', async () => {
    const chatSpy = mockChat(['event: done\ndata: {}\n\n'])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    const files = Array.from({ length: 6 }, (_, i) => new File(['x'], `f${i}.txt`))

    await act(async () => {
      await result.current.send('too many', files)
    })
    expect(chatSpy).not.toHaveBeenCalled()
    expect(result.current.error).toMatch(/too many/i)
  })

  it('rejects an oversized file with a friendly error (no send)', async () => {
    const chatSpy = mockChat(['event: done\ndata: {}\n\n'])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    // A 3 MB file (over the 2 MB/file cap).
    const big = new File([new Uint8Array(3 * 1024 * 1024)], 'big.bin')

    await act(async () => {
      await result.current.send('here', [big])
    })
    expect(chatSpy).not.toHaveBeenCalled()
    expect(result.current.error).toMatch(/too large/i)
  })

  it('surfaces a network/transport failure as an error and re-enables', async () => {
    vi.spyOn(api, 'chat').mockRejectedValue(new Error('Failed to fetch'))
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('hi')
    })
    expect(result.current.error).toMatch(/Failed to fetch/)
    expect(result.current.sending).toBe(false)
  })

  // ── multi-turn chat (feature-gap step 6, Inc B): a STABLE session id ──────────

  it('mints a stable session id and reuses it across turns in the same conversation', async () => {
    const chatSpy = mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))

    await act(async () => {
      await result.current.send('first')
    })
    await act(async () => {
      await result.current.send('second')
    })

    // A uuid-shaped session id is passed on BOTH turns, and it is the SAME id.
    const first = chatSpy.mock.calls[0]
    const second = chatSpy.mock.calls[1]
    const sid1 = first[3] as string
    const sid2 = second[3] as string
    expect(typeof sid1).toBe('string')
    expect(sid1.length).toBeGreaterThan(8)
    expect(sid2).toBe(sid1) // stable within a conversation (so the backend threads it)
    // The hook also exposes it.
    expect(result.current.sessionId).toBe(sid1)
  })

  it('resets the session id when the agent changes (a new conversation)', async () => {
    const chatSpy = mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result, rerender } = renderHook(
      ({ agent }) => useChatSend({ project: 'kaidera-os', agent }),
      { initialProps: { agent: 'ren' } },
    )

    await act(async () => {
      await result.current.send('to ren')
    })
    const sidRen = chatSpy.mock.calls[0][3] as string

    // Switch to a different agent → a NEW conversation → a fresh session id.
    rerender({ agent: 'kai' })
    await act(async () => {
      await result.current.send('to kai')
    })
    const sidKai = chatSpy.mock.calls[1][3] as string

    expect(sidKai).not.toBe(sidRen)
  })

  it('does not expose the previous agent session while a fresh scope has no conversation', async () => {
    const historySpy = vi.spyOn(api, 'chatHistory').mockResolvedValue({
      turns: [],
    })
    mockChat(['event: done\ndata: {}\n\n'])
    const { result, rerender } = renderHook(
      ({ agent }) => useChatSend({ project: 'kaidera-os', agent }),
      { initialProps: { agent: 'ren' } },
    )

    await act(async () => {
      await result.current.send('to ren')
    })
    const renSession = result.current.sessionId

    rerender({ agent: 'kai' })

    expect(result.current.sessionId).toBe('')
    expect(
      historySpy.mock.calls.some(
        ([project, agent, session]) =>
          project === 'kaidera-os' && agent === 'kai' && session === renSession,
      ),
    ).toBe(false)
  })

  it('tracks the TodoWrite task list → {done,total} from the latest tasks frame', async () => {
    mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      // First snapshot: 0 of 2 done.
      'event: tasks\ndata: [{"content":"a","status":"in_progress"},{"content":"b","status":"pending"}]\n\n',
      // Updated snapshot (latest wins): 1 of 2 done.
      'event: tasks\ndata: [{"content":"a","status":"completed"},{"content":"b","status":"pending"}]\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('plan it')
    })
    expect(result.current.tasks).toEqual({ done: 1, total: 2 })
  })

  it('counts each subagent (Task) frame for the current turn', async () => {
    mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: subagent\ndata: {"label":"Investigate"}\n\n',
      'event: subagent\ndata: {"label":"Refactor"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('go')
    })
    expect(result.current.subagents).toBe(2)
  })

  it('reports null tasks + 0 subagents for a normal turn (nothing extra to show)', async () => {
    mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      'event: delta\ndata: {"text":"hi"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('hello')
    })
    expect(result.current.tasks).toBeNull()
    expect(result.current.subagents).toBe(0)
  })

  it('does not break the stream on a malformed tasks payload (degrades to {0,0})', async () => {
    mockChat([
      'event: run\ndata: {"run_id":"r1"}\n\n',
      // Not an array, and a junk item — must not throw; reply still streams.
      'event: tasks\ndata: {"oops":true}\n\n',
      'event: tasks\ndata: [1, null, {"content":"ok","status":"completed"}]\n\n',
      'event: delta\ndata: {"text":"still here"}\n\n',
      'event: done\ndata: {}\n\n',
    ])
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('go')
    })
    // The array frame wins; only the one object item counts → 1 of 1.
    expect(result.current.tasks).toEqual({ done: 1, total: 1 })
    // The stream kept going past the bad frame.
    expect(result.current.reply).toBe('still here')
    expect(result.current.error).toBeNull()
  })

  it('resets tasks/subagents at the start of a new send', async () => {
    const chatSpy = vi.spyOn(api, 'chat')
    chatSpy.mockResolvedValueOnce({
      ok: true, status: 200,
      body: sseStream([
        'event: run\ndata: {"run_id":"r1"}\n\n',
        'event: tasks\ndata: [{"content":"a","status":"completed"}]\n\n',
        'event: subagent\ndata: {"label":"x"}\n\n',
        'event: done\ndata: {}\n\n',
      ]),
    } as unknown as Response)
    const { result } = renderHook(() => useChatSend({ project: 'kaidera-os', agent: 'ren' }))
    await act(async () => {
      await result.current.send('first')
    })
    expect(result.current.tasks).toEqual({ done: 1, total: 1 })
    expect(result.current.subagents).toBe(1)

    // A second turn with NO task/subagent frames clears the prior indicator.
    chatSpy.mockResolvedValueOnce({
      ok: true, status: 200,
      body: sseStream([
        'event: run\ndata: {"run_id":"r2"}\n\n',
        'event: delta\ndata: {"text":"ok"}\n\n',
        'event: done\ndata: {}\n\n',
      ]),
    } as unknown as Response)
    await act(async () => {
      await result.current.send('second')
    })
    expect(result.current.tasks).toBeNull()
    expect(result.current.subagents).toBe(0)
  })
})
