import { describe, expect, it } from 'vitest'
import { parseSseStream } from './chatStream'

/** Build a ReadableStream that emits the given string chunks (as UTF-8 bytes). */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
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

async function collect(stream: ReadableStream<Uint8Array>) {
  const out: { event: string; data: string }[] = []
  for await (const frame of parseSseStream(stream)) out.push(frame)
  return out
}

describe('parseSseStream', () => {
  it('parses event/data frames separated by a blank line', async () => {
    const frames = await collect(
      streamOf([
        'event: run\ndata: {"run_id":"r1"}\n\n',
        'event: delta\ndata: {"text":"hi"}\n\n',
        'event: done\ndata: {}\n\n',
      ]),
    )
    expect(frames).toEqual([
      { event: 'run', data: '{"run_id":"r1"}' },
      { event: 'delta', data: '{"text":"hi"}' },
      { event: 'done', data: '{}' },
    ])
  })

  it('reassembles a frame split across chunk boundaries', async () => {
    const frames = await collect(
      streamOf(['event: del', 'ta\ndata: {"text":"par', 'tial"}\n\n']),
    )
    expect(frames).toEqual([{ event: 'delta', data: '{"text":"partial"}' }])
  })

  it('defaults the event name to "message" when only a data line is present', async () => {
    const frames = await collect(streamOf(['data: {"text":"x"}\n\n']))
    expect(frames).toEqual([{ event: 'message', data: '{"text":"x"}' }])
  })

  it('flushes a trailing frame with no final blank line', async () => {
    const frames = await collect(streamOf(['event: done\ndata: {}']))
    expect(frames).toEqual([{ event: 'done', data: '{}' }])
  })

  it('ignores blank padding between frames', async () => {
    const frames = await collect(
      streamOf(['event: delta\ndata: {"text":"a"}\n\n\n\nevent: delta\ndata: {"text":"b"}\n\n']),
    )
    expect(frames.map((f) => f.data)).toEqual(['{"text":"a"}', '{"text":"b"}'])
  })
})
