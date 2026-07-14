/**
 * useChatSend — the interactive-chat SEND flow for the agent-detail composer.
 *
 * On `send(message)` it POSTs the turn (`api.chat`), then reads the SSE reply with
 * `parseSseStream`:
 *   - `event: run`    → captures the run_id; calls `onRun(run_id)` so the parent can
 *     POINT THE TRANSCRIPT at this run (it then streams in via useRunStateStream —
 *     the SAME /runstate/stream surface autonomous runs use, the durable source).
 *   - `event: delta`  → legacy/degraded direct text frame; normal local chat now
 *     writes live text/thinking/tool/task spans to run-state and the feed follows that.
 *   - `event: result` → a terminal receipt/fallback text frame when direct deltas
 *     did not stream.
 *   - `event: error`  → a clean error string for the composer's error bubble.
 *   - `event: done` / stream-close → re-enables the composer (sending=false).
 *
 * The user's message is `echo`ed immediately (an instant outgoing bubble). State is
 * scoped so a (project, agent) switch resets it.
 *
 * MULTI-TURN CONTEXT (feature-gap step 6, Inc B): the hook mints a STABLE per-
 * conversation session id and sends it on every turn (`api.chat(..., sessionId)`) so the
 * backend threads the conversation's prior turns into the prompt. The hook still holds NO
 * durable transcript — that's the run-state transcript's job; it only tracks the session
 * grouping key + a turn count for the composer's thread indicator.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { writeActiveChatRun } from './activeChatRun'
import { api } from './client'
import { parseSseStream } from './chatStream'

export interface UseChatSendArgs {
  project: string | null
  agent: string | null
  /** Called with the run_id from the `run` frame — the parent pins the transcript at it. */
  onRun?: (runId: string) => void
}

/** Per-file + per-turn client-side attachment limits (mirror the backend caps so the
 * user gets a friendly error before the upload round-trip). 2 MB/file, 5 files, 8 MB. */
export const ATTACH_MAX_FILE_BYTES = 2 * 1024 * 1024
export const ATTACH_MAX_FILES = 5
export const ATTACH_MAX_TURN_BYTES = 8 * 1024 * 1024

export interface ChatSend {
  /**
   * Send one turn. No-op for a blank message (with no files) or a missing project/agent.
   * `files` (OPTIONAL, chat file-attachments — step 6) are uploaded sequentially under a
   * pre-minted client_run_id BEFORE the chat POST, which then carries their ids.
   */
  send: (message: string, files?: File[]) => Promise<void>
  /** True from the POST until the stream closes (drives disabled-while-sending). */
  sending: boolean
  /**
   * Stop the in-flight turn. First asks the backend to cancel the current run, then
   * aborts the upload/chat POST/SSE fetch for immediate UI teardown. Leaves the partial
   * reply as-is and re-enables the composer. No-op when idle.
   */
  stop: () => void
  /** The run_id of the in-flight/last turn (null before the run frame). */
  runId: string | null
  /** The user's last-sent message (the instant echo bubble). */
  echo: string | null
  /** Terminal/direct fallback reply text (delta/result); '' until text arrives. */
  reply: string
  /** A clean error message (error frame / transport failure), else null. */
  error: string | null
  /**
   * The STABLE per-conversation session id (multi-turn chat, feature-gap step 6).
   * Minted once per (project, agent) conversation and sent on every turn so the backend
   * threads the conversation's prior turns into the prompt. Resets on agent change.
   */
  sessionId: string
  /**
   * The number of turns sent in this conversation (multi-turn chat). 0 at the start;
   * increments as turns are sent. Drives the composer's "thread: N turns" indicator
   * (history that the backend threads into context). Resets on agent change.
   */
  turns: number
  /**
   * The prior turns of this conversation restored from run_state/run_span on mount
   * (oldest-first), so a page reload no longer blanks the chat — the operator sees
   * what was said and can continue. Each entry is `{user, reply}`. Empty until the
   * history load completes (or when there is no prior conversation).
   */
  history: { user: string; reply: string }[]
  /**
   * The CURRENT turn's task list (claude-code TodoWrite), as `{done, total}` derived
   * from the latest `tasks` SSE frame. `null` when the turn emitted no task list (a
   * normal one-shot turn) so the UI shows nothing extra. Reset to `null` per send.
   */
  tasks: { done: number; total: number } | null
  /**
   * The number of sub-agents spawned in the current turn (claude-code `Task`), counted
   * from `subagent` SSE frames. 0 when none. Reset to 0 per send.
   */
  subagents: number
}

/** localStorage key for a (project, agent) conversation's session_id — so a page
 * reload resumes the SAME conversation instead of minting a fresh session and
 * losing the visible history (the prior turns are always in run_state/run_span; this
 * just keeps the pointer to them). */
function chatSessionKey(project: string, agent: string): string {
  return `kaidera-os:chat-session:${project}:${agent}`
}

/** Read a persisted session_id for (project, agent), or '' if none. */
function readPersistedSession(project: string, agent: string): string {
  try {
    if (typeof localStorage === 'undefined') return ''
    return localStorage.getItem(chatSessionKey(project, agent)) ?? ''
  } catch {
    return ''
  }
}

/** Persist a session_id for (project, agent) so reload resumes the conversation. */
function writePersistedSession(project: string, agent: string, sessionId: string): void {
  try {
    if (typeof localStorage === 'undefined') return
    localStorage.setItem(chatSessionKey(project, agent), sessionId)
  } catch {
    /* ignore quota / disabled storage */
  }
}

/** Mint a conversation id (uuid4 where available, else a random fallback). */
function newSessionId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch {
    // fall through to the fallback below
  }
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/**
 * Mint a STRICT uuid4 for the per-turn `client_run_id` (shared by the attachment
 * upload(s) + the chat send so the bytes land under the run the turn writes to). Unlike
 * the session id, the backend REQUIRES a genuine uuid4 here (a non-v4 id is rejected and
 * the backend mints its own — which would break the upload↔send sharing), so the fallback
 * is a uuid4-SHAPED string (v4 + variant nibbles set), not the `s-…` session fallback.
 */
function newRunId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch {
    // fall through to the v4-shaped fallback below
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

export function useChatSend({ project, agent, onRun }: UseChatSendArgs): ChatSend {
  const [sending, setSending] = useState(false)
  const [runId, setRunId] = useState<string | null>(null)
  const [echo, setEcho] = useState<string | null>(null)
  const [reply, setReply] = useState('')
  const [error, setError] = useState<string | null>(null)
  // TASKS + SUB-AGENTS indicator (this turn only). `tasks` holds the latest TodoWrite
  // list as {done,total}; `subagents` counts Task spawns. Both reset at the start of
  // each `send` so a new turn starts clean (a one-shot turn with no tasks shows nothing).
  const [tasks, setTasks] = useState<{ done: number; total: number } | null>(null)
  const [subagents, setSubagents] = useState(0)

  // STABLE per-conversation session id + turn count (multi-turn chat, feature-gap step
  // 6), STAMPED with the conversation key they belong to — the SAME derive-by-key
  // pattern useRunStateStream uses (no setState in an effect, no setState/ref-write
  // during render). A (project, agent) switch is handled purely: the exposed `turns`
  // DERIVE from whether the stamped key matches the current conversation (a mismatched
  // conversation surfaces 0 turns, so the thread indicator clears instantly), and the
  // next `send` resumes that scope's persisted session id when present, else mints +
  // stamps a fresh one. This keeps a prior agent's history from ever threading into a
  // different agent's chat. The only writes are in `send` (an event handler, where
  // setState is allowed).
  const convKey = `${project ?? ''} ${agent ?? ''}`
  const [conv, setConv] = useState<{ key: string; sessionId: string; turns: number }>(() => {
    // Resume the persisted conversation for (project, agent) if one exists, so a
    // page reload continues the SAME chat instead of minting a fresh session and
    // blanking the history. Falls back to a new session when none is stored.
    const persisted =
      project && agent ? readPersistedSession(project, agent) : ''
    return {
      key: convKey,
      sessionId: persisted || newSessionId(),
      turns: 0,
    }
  })
  const sameConv = conv.key === convKey
  const persistedScopeSession =
    !sameConv && project && agent ? readPersistedSession(project, agent) : ''
  // Exposed: the stamped session id (committed on the last send) + the turn count for
  // THIS conversation (0 when the key no longer matches — a not-yet-sent agent switch).
  // A scope with no persisted conversation has no session until its first send.
  // Never expose the previous agent's session id while that new session is being
  // minted: history restore and active-run restore both key off this value.
  const sessionId = sameConv ? conv.sessionId : persistedScopeSession
  const turns = sameConv ? conv.turns : 0

  // RESTORE-ON-RELOAD: load the prior turns for the persisted session_id so the
  // operator sees the conversation history and can continue it. Re-runs when the
  // project/agent/session changes. Graceful-degrades to [] on any failure (the
  // composer renders empty and a fresh chat still works). The loaded turns are
  // OLDEST-FIRST from the backend; we keep that order for display.
  const [history, setHistory] = useState<{ user: string; reply: string }[]>([])
  useEffect(() => {
    const proj = project ?? ''
    const ag = agent ?? ''
    const sess = sessionId
    if (!proj || !ag || !sess) {
      queueMicrotask(() => setHistory([]))
      return
    }
    const ctrl = new AbortController()
    let cancelled = false
    api
      .chatHistory(proj, ag, sess, ctrl.signal)
      .then((res) => {
        if (!cancelled) setHistory(res.turns)
      })
      .catch(() => {
        if (!cancelled) setHistory([])
      })
    return () => {
      cancelled = true
      ctrl.abort()
    }
  }, [project, agent, sessionId])

  // Keep onRun in a ref so `send` stays stable + never goes stale mid-stream. The
  // ref is updated in an effect (not during render) — the same off-render pattern
  // useResource uses — so refs are only ever touched outside render.
  const onRunRef = useRef(onRun)
  useEffect(() => {
    onRunRef.current = onRun
  })

  // The AbortController for the in-flight SEND path (upload + chat POST/SSE). Stop
  // explicitly cancels the run first, then aborts this controller for local teardown.
  const sendCtrlRef = useRef<AbortController | null>(null)
  const inFlightRunIdRef = useRef<string | null>(null)

  const stop = useCallback(() => {
    const ctrl = sendCtrlRef.current
    const currentRunId = inFlightRunIdRef.current
    sendCtrlRef.current = null
    inFlightRunIdRef.current = null
    if (currentRunId) {
      void api.cancelRun(currentRunId).catch(() => {
        // Stop remains local-best-effort even if the explicit cancel endpoint is absent/down.
      })
    }
    // Abort the live upload/chat fetch after firing explicit cancel. The partial reply
    // is left as-is; the run-state feed renders the terminal run when the cancel lands.
    ctrl?.abort()
    setSending(false)
  }, [])

  const send = useCallback(
    async (message: string, files?: File[]) => {
      const text = message.trim()
      if (!text || !project || !agent) return

      const turnFiles = files ?? []
      // CHAT FILE-ATTACHMENTS (step 6): client-side guard (a friendly error before the
      // upload round-trip, mirroring the backend caps). On a breach we surface the error
      // and DON'T send — the user can drop a file and retry.
      if (turnFiles.length > ATTACH_MAX_FILES) {
        setError(`Too many attachments (max ${ATTACH_MAX_FILES} per message).`)
        return
      }
      const oversized = turnFiles.find((f) => f.size > ATTACH_MAX_FILE_BYTES)
      if (oversized) {
        setError(`"${oversized.name}" is too large (max 2 MB per file).`)
        return
      }
      const totalBytes = turnFiles.reduce((n, f) => n + f.size, 0)
      if (totalBytes > ATTACH_MAX_TURN_BYTES) {
        setError('Attachments exceed the 8 MB total limit for one message.')
        return
      }

      // Resolve THIS turn's conversation: reuse the stamped session id when the key
      // still matches, else start a FRESH conversation (a (project, agent) switch since
      // the last turn) so the session id is new and a prior agent's history isn't
      // threaded across agents. Stamp + bump the turn count for the new conversation.
      const key = `${project} ${agent}`
      const reuse = conv.key === key
      const persisted = reuse ? '' : readPersistedSession(project, agent)
      const turnSessionId = reuse ? conv.sessionId : persisted || newSessionId()
      setConv({ key, sessionId: turnSessionId, turns: (reuse ? conv.turns : 0) + 1 })
      // Persist the session_id for (project, agent) so a page reload resumes THIS
      // conversation instead of minting a fresh one and blanking the history.
      if (project && agent) writePersistedSession(project, agent, turnSessionId)

      // ALWAYS mint the run id up front (not only for attachments) and PIN the
      // transcript at it IMMEDIATELY — so the pane follows THIS run from first paint,
      // independent of when (or whether) the streamed `run` frame arrives. A slow or
      // proxy-buffered SSE used to leave the pane stuck on the PREVIOUS run (e.g. an old
      // completed run), so the new reply never appeared. The backend adopts this
      // client_run_id as the run_id (run-state lands under it) + any attachments share it.
      const clientRunId = newRunId()
      inFlightRunIdRef.current = clientRunId

      // Reset for the new turn + echo the user's message instantly + PIN the run now.
      setEcho(message)
      setReply('')
      setRunId(clientRunId)
      setError(null)
      // Clear the prior turn's task/sub-agent indicator so this turn starts fresh.
      setTasks(null)
      setSubagents(0)
      setSending(true)
      writeActiveChatRun(project, agent, turnSessionId, clientRunId)
      onRunRef.current?.(clientRunId)

      // Fresh AbortController for THIS turn's upload/stream; `stop` aborts it after
      // firing explicit run cancel. Abort any prior in-flight stream first (defensive —
      // sending gates re-entry, but never leak a controller).
      sendCtrlRef.current?.abort()
      const ctrl = new AbortController()
      sendCtrlRef.current = ctrl

      let assembled = ''
      let gotDelta = false
      let resultText = ''
      try {
        // CHAT FILE-ATTACHMENTS (step 6): each file is uploaded SEQUENTIALLY under the
        // shared client_run_id BEFORE the send, collecting the ids the chat POST carries
        // so the bytes land under the run the turn writes to. A failed upload aborts the
        // turn with a clean error (the file matters — we don't silently send without it).
        const attachmentIds: string[] = []
        for (const file of turnFiles) {
          const up = await api.uploadAttachment(project, agent, clientRunId, file, ctrl.signal)
          attachmentIds.push(up.attachment_id)
        }

        const res = await api.chat(
          project, agent, text, turnSessionId, clientRunId,
          attachmentIds.length > 0 ? attachmentIds : undefined,
          ctrl.signal,
        )
        if (!res.body) throw new Error('chat response had no body to stream')
        for await (const frame of parseSseStream(res.body)) {
          // The `tasks` frame's data is a JSON ARRAY (not an object), so parse the raw
          // data once as `unknown` and only coerce to a record for the object frames.
          let parsed: unknown = undefined
          try {
            parsed = frame.data ? JSON.parse(frame.data) : undefined
          } catch {
            parsed = undefined // a malformed frame is skipped (house law: keep streaming)
          }
          const payload: Record<string, unknown> =
            parsed && typeof parsed === 'object' && !Array.isArray(parsed)
              ? (parsed as Record<string, unknown>)
              : {}
          if (frame.event === 'run') {
            const id = typeof payload.run_id === 'string' ? payload.run_id : null
            if (id) {
              inFlightRunIdRef.current = id
              setRunId(id)
              writeActiveChatRun(project, agent, turnSessionId, id)
              onRunRef.current?.(id)
            }
          } else if (frame.event === 'delta') {
            assembled += typeof payload.text === 'string' ? payload.text : ''
            gotDelta = true
            setReply(assembled)
          } else if (frame.event === 'tasks') {
            // The agent's task list (claude-code TodoWrite). The latest frame WINS — the
            // task list is re-sent whole each time the agent updates it. Defensive: only
            // count items that look like a todo; an unexpected/empty shape → {0,0} (we
            // still show the indicator since the agent did emit a list this turn).
            const items = Array.isArray(parsed) ? parsed : []
            let done = 0
            let total = 0
            for (const it of items) {
              if (it && typeof it === 'object') {
                total += 1
                if ((it as { status?: unknown }).status === 'completed') done += 1
              }
            }
            setTasks({ done, total })
          } else if (frame.event === 'subagent') {
            // A sub-agent spawn (claude-code Task). Count each occurrence for this turn.
            setSubagents((n) => n + 1)
          } else if (frame.event === 'result') {
            resultText = typeof payload.text === 'string' ? payload.text : ''
          } else if (frame.event === 'error') {
            setError(
              typeof payload.message === 'string'
                ? payload.message
                : 'The harness reported an error.',
            )
          }
          // `done` needs no handling — the loop ends when the stream closes.
        }
        // If only a final result frame arrived (no streamed deltas), show it.
        if (!gotDelta && resultText) setReply(resultText)
      } catch (e: unknown) {
        // A user-initiated STOP aborts the fetch → a DOMException AbortError (or the
        // controller's own aborted flag). That's a clean cancel — the partial reply
        // stays and the run is terminal — NOT a failure, so don't surface an error.
        const aborted =
          ctrl.signal.aborted ||
          (e instanceof DOMException && e.name === 'AbortError') ||
          (e instanceof Error && e.name === 'AbortError')
        if (!aborted) {
          setError(e instanceof Error ? e.message : String(e))
        }
      } finally {
        // Clear the ref only if it still points at THIS turn's controller (a `stop`
        // or a newer `send` may have already replaced/cleared it).
        if (sendCtrlRef.current === ctrl) {
          sendCtrlRef.current = null
          inFlightRunIdRef.current = null
        }
        setSending(false)
      }
    },
    [project, agent, conv.key, conv.sessionId, conv.turns],
  )

  return { send, stop, sending, runId, echo, reply, error, sessionId, turns, history, tasks, subagents }
}
