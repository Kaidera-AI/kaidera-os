/**
 * parseSseStream — a tiny `text/event-stream` reader for the chat POST reply.
 *
 * The interactive-chat route (`POST /agents/{p}/{a}/chat`) streams SSE over a POST
 * body, so a browser `EventSource` (GET-only) can't read it — the legacy composer
 * used `fetch()` + a `ReadableStream` reader and hand-parsed frames. This is that
 * parser, factored out and unit-tested: GIVEN the response body stream it
 * async-yields `{event, data}` frames (frames are blocks separated by a blank line;
 * each block has `event:` / `data:` lines, `data:` lines concatenated). It is robust
 * to frames split across chunk boundaries (the trailing partial is buffered) and
 * flushes a final frame with no closing blank line. The DURABLE reply still arrives
 * via `/runstate/stream` (useRunStateStream); these frames carry the `run` id (to
 * pin the transcript) + the local-mode delta/result/error/done signals.
 */

export interface SseFrame {
  event: string
  data: string
}

/** Parse one already-split SSE block into {event, data} (data lines concatenated). */
function parseBlock(block: string): SseFrame | null {
  if (!block.trim()) return null
  let event = 'message'
  let data = ''
  for (const raw of block.split('\n')) {
    const line = raw.replace(/\r$/, '')
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  return { event, data }
}

export async function* parseSseStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SseFrame> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop() ?? '' // keep the trailing partial frame
      for (const part of parts) {
        const frame = parseBlock(part)
        if (frame) yield frame
      }
    }
    // Flush any trailing frame that had no closing blank line.
    const tail = parseBlock(buf)
    if (tail) yield tail
  } finally {
    reader.releaseLock()
  }
}
