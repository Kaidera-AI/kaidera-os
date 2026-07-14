/**
 * useDispatchRun — the "Approve & Run" SSE flow for the Dispatch board, keyed PER ROW.
 *
 * The propose-mode invariant: a dispatch runs ONLY when the operator clicks Approve &
 * Run on a proposed handoff. On `run(row)` this POSTs to `/dispatch/{p}/run?agent_name=…`
 * (the work `summary` + the handoff `id`/`compound` in the body) and reads the SSE reply
 * with `parseSseStream` — the SAME parser + frame contract the chat composer uses
 * (`run` / `delta` / `result` / `error` / `done`):
 *   - `event: run`    → captures the run_id; calls `onRun(handoffId, runId)` so the
 *     caller can POINT a transcript at this run (it streams via /runstate/stream — the
 *     same durable surface autonomous runs use).
 *   - `event: delta`  → assembles the live reply text into THIS row's output panel.
 *   - `event: result` → the final assembled text when no deltas streamed.
 *   - `event: error`  → a clean error string for the row (a failed claim / harness error).
 *   - `event: done` / stream-close → clears the row's running flag.
 *
 * State is a map keyed by handoff id, so each row tracks its own running / output /
 * run_id / error / done independently (the board lists many handoffs). A row already
 * running ignores a repeat click (no double-spawn). The project resets the map.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './client'
import { parseSseStream } from './chatStream'

/** The work to dispatch for one row (the proposed agent + the handoff). */
export interface DispatchRunArgs {
  agentName: string
  summary: string
  handoffId: string
  handoffCompound: string
}

/** One row's live run state (a clean default for a row that never ran). */
export interface DispatchRunState {
  running: boolean
  /** The assembled reply text (delta/result); '' until text arrives. */
  output: string
  /** The run_id from the `run` frame (null before it / for a never-run row). */
  runId: string | null
  /** A clean error message (error frame / transport failure), else null. */
  error: string | null
  /** True once the stream closed (done frame / close) for this row's last run. */
  done: boolean
}

const IDLE: DispatchRunState = { running: false, output: '', runId: null, error: null, done: false }

export interface UseDispatchRunArgs {
  project: string | null
  /** Called with (handoffId, runId) when the run frame lands — pin a transcript at it. */
  onRun?: (handoffId: string, runId: string) => void
}

export interface DispatchRunController {
  /** Run one proposed dispatch (Approve & Run). No-op while that row is already running. */
  run: (args: DispatchRunArgs) => Promise<void>
  /** This row's live run state (a clean default for a row that hasn't run). */
  stateFor: (handoffId: string) => DispatchRunState
}

export function useDispatchRun({ project, onRun }: UseDispatchRunArgs): DispatchRunController {
  const [byRow, setByRow] = useState<Record<string, DispatchRunState>>({})

  // Reset per-row state when the project changes (the board is project-scoped). Done
  // in render via the key-scoped pattern the rest of the app uses.
  const [scopedTo, setScopedTo] = useState<string | null>(project)
  if (project !== scopedTo) {
    setScopedTo(project)
    setByRow({})
  }

  // Keep onRun in a ref so `run` stays stable + never goes stale mid-stream (the same
  // off-render ref pattern useChatSend/useResource use).
  const onRunRef = useRef(onRun)
  useEffect(() => {
    onRunRef.current = onRun
  })

  // Track in-flight rows synchronously (guards a double-click before state flushes).
  const inflight = useRef<Set<string>>(new Set())

  const patch = useCallback((hid: string, next: Partial<DispatchRunState>) => {
    setByRow((prev) => ({ ...prev, [hid]: { ...(prev[hid] ?? IDLE), ...next } }))
  }, [])

  const run = useCallback(
    async (args: DispatchRunArgs) => {
      const { agentName, summary, handoffId, handoffCompound } = args
      if (!project || !handoffId) return
      if (inflight.current.has(handoffId)) return // already running — ignore the click
      inflight.current.add(handoffId)

      // Reset this row for the new run.
      patch(handoffId, { running: true, output: '', runId: null, error: null, done: false })

      let assembled = ''
      let gotDelta = false
      let resultText = ''
      try {
        const res = await api.dispatchRun(project, agentName, {
          summary,
          handoff_id: handoffId,
          handoff_compound: handoffCompound,
        })
        if (!res.body) throw new Error('dispatch run had no body to stream')
        for await (const frame of parseSseStream(res.body)) {
          let payload: Record<string, unknown> = {}
          try {
            payload = frame.data ? (JSON.parse(frame.data) as Record<string, unknown>) : {}
          } catch {
            payload = {} // a malformed frame is skipped (house law: keep streaming)
          }
          if (frame.event === 'run') {
            const id = typeof payload.run_id === 'string' ? payload.run_id : null
            if (id) {
              patch(handoffId, { runId: id })
              onRunRef.current?.(handoffId, id)
            }
          } else if (frame.event === 'delta') {
            assembled += typeof payload.text === 'string' ? payload.text : ''
            gotDelta = true
            patch(handoffId, { output: assembled })
          } else if (frame.event === 'result') {
            resultText = typeof payload.text === 'string' ? payload.text : ''
          } else if (frame.event === 'error') {
            patch(handoffId, {
              error:
                typeof payload.message === 'string'
                  ? payload.message
                  : 'The harness reported an error.',
            })
          }
          // `done` needs no handling — the loop ends when the stream closes.
        }
        if (!gotDelta && resultText) patch(handoffId, { output: resultText })
      } catch (e: unknown) {
        patch(handoffId, { error: e instanceof Error ? e.message : String(e) })
      } finally {
        inflight.current.delete(handoffId)
        patch(handoffId, { running: false, done: true })
      }
    },
    [project, patch],
  )

  const stateFor = useCallback(
    (handoffId: string): DispatchRunState => byRow[handoffId] ?? IDLE,
    [byRow],
  )

  return { run, stateFor }
}
