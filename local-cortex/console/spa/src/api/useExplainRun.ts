/**
 * useExplainRun — follow ONE explain generation by run id, surfacing live progress +
 * the final full HTML.
 *
 * The Explain capability STARTS a host-side generation (`POST /explain` → a run_id), then
 * the run's `output` spans carry the self-contained HTML document AS IT STREAMS. The
 * SPA follows that run via the existing runs READ surface (`GET /runs/run/{run_id}`):
 * this hook POLLS that endpoint on a light interval until the run reaches a TERMINAL
 * status (`ok` | `error`), then stops.
 *
 * Why poll the run (not the `/runstate/stream` SSE the chat composer uses): the SSE is
 * AGENT-scoped (`agent_runs_view` filters recent runs to a given agent and only honours a
 * pinned run_id when it belongs to that agent), and the `POST /explain` response does NOT
 * surface the (server-resolved) console writer agent the run was opened under — so the SPA
 * can't address the explain run on that SSE by run_id alone. Polling the run BY ID needs
 * no agent, gives the same streaming progress (the segments grow each tick) + the terminal
 * status, and reads the FULL HTML straight from the run's spans (docs/sdk/modules/
 * explain.md §8). Once terminal we stop polling (the document is complete).
 *
 * GRACEFUL-DEGRADE: a 404 while the run row hasn't been created yet (or the store is down)
 * surfaces as `polling` with no transcript — the next tick retries; it never throws into
 * the view. A null run id parks idle.
 */

import { useMemo, useState } from 'react'
import { explainHtmlFromRun } from './client'
import { useResource } from './useResource'
import type { RunTranscript } from './types'

/** The terminal phase of an explain run (mirrors the run-state statuses). */
export type ExplainPhase = 'idle' | 'running' | 'ok' | 'error'

export interface ExplainRunState {
  /** The run's hydrated transcript (segments stream in as it generates), or null. */
  transcript: RunTranscript | null
  /** idle (no run) · running (generating / not yet terminal) · ok · error. */
  phase: ExplainPhase
  /** The FULL generated HTML (concat of the run's `output` spans) — '' until present. */
  html: string
  /** The error message on a terminal `error`, else null. */
  error: string | null
  /** True while the hook is actively polling the run (stops at a terminal status). */
  polling: boolean
}

/** The just-the-run-read slice of the api client this hook needs (so tests fake one fn). */
export type ExplainRunReader = (runId: string, signal?: AbortSignal) => Promise<RunTranscript>

export interface UseExplainRunArgs {
  /** The run id to follow (from `postExplain`), or null to park idle. */
  runId: string | null
  /** The run reader (`api.run`). */
  getRun: ExplainRunReader
  /** Poll cadence in ms while the run is non-terminal. Default 1200. */
  pollMs?: number
}

/** Map a run's status to the explain phase. A terminal run stops the poll. */
function phaseOf(run: RunTranscript | null): ExplainPhase {
  if (!run) return 'idle'
  const s = (run.status || '').toLowerCase()
  if (s === 'ok') return 'ok'
  if (s === 'error') return 'error'
  return 'running'
}

export function useExplainRun({ runId, getRun, pollMs = 1200 }: UseExplainRunArgs): ExplainRunState {
  // A terminal latch SCOPED to the current run id: once THIS run reports `ok`/`error` we
  // stop polling (the document is complete). Stored as the run id that went terminal, so a
  // NEW generation (different runId) re-arms automatically with no effect — the latch only
  // matches when it equals the run we're currently following (the derive-by-key pattern the
  // rest of the SPA uses; the only write is in render-time state, guarded to fire once).
  const [terminalFor, setTerminalFor] = useState<string | null>(null)
  const settled = !!runId && terminalFor === runId
  const effectivePoll = runId && !settled ? pollMs : undefined

  const res = useResource<RunTranscript>(
    runId ? (signal) => getRun(runId, signal) : null,
    [runId],
    { pollMs: effectivePoll },
  )

  const transcript = res.data
  const phase = runId ? phaseOf(transcript) : 'idle'
  const terminal = phase === 'ok' || phase === 'error'
  // Latch the terminal run id during render (allowed — it's a state-update-during-render
  // that converges in one extra render, the same pattern MainArea/useChatSend use). This
  // flips `effectivePoll` to undefined on the next render, which clears useResource's timer.
  if (terminal && runId && terminalFor !== runId) {
    setTerminalFor(runId)
  }

  return useMemo(
    () => ({
      transcript,
      phase,
      html: phase === 'ok' ? explainHtmlFromRun(transcript) : '',
      error: phase === 'error' ? (transcript?.error ?? 'The explainer generation failed.') : null,
      polling: !!runId && !terminal,
    }),
    [transcript, phase, runId, terminal],
  )
}
