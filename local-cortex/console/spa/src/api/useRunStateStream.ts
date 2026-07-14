/**
 * useRunStateStream — the thin SSE client for the live run transcript.
 *
 * Subscribes to `GET /runstate/stream?project=&agent=&run=` (the console's
 * RunState SSOT push, Milestone 1 T8). Each `event: runstate` frame carries a
 * fresh read-model (`RunStateFrame`) — the selected run's structured fields +
 * the pre-rendered transcript partial. The backend RE-READS the same model the
 * REST first-paint uses on every wake, so the push and the initial fetch cannot
 * disagree (ratified design decision #5). This hook just surfaces the newest
 * frame + the connection status; the view renders from it.
 *
 * Lifecycle: opens an EventSource scoped to (project, agent[, run]); reconnects
 * (EventSource's own backoff) until unmounted or the args change. A null project
 * or agent means "no subscription" (the dashboard / unselected state).
 */

import { useEffect, useState } from 'react'
import type { RunStateFrame } from './types'

export type SseStatus = 'idle' | 'connecting' | 'open' | 'error'

export interface RunStateStream {
  /** The most recent frame, or null before the first one arrives. */
  frame: RunStateFrame | null
  status: SseStatus
}

export interface UseRunStateStreamArgs {
  project: string | null
  agent: string | null
  /** Optional pinned run id (a past run the operator clicked). */
  run?: string | null
  /** Set false to suspend the subscription without unmounting. Default true. */
  enabled?: boolean
}

function buildUrl(project: string, agent: string, run?: string | null): string {
  const q = new URLSearchParams({ project, agent })
  if (run) q.set('run', run)
  return `/runstate/stream?${q.toString()}`
}

export function useRunStateStream({
  project,
  agent,
  run = null,
  enabled = true,
}: UseRunStateStreamArgs): RunStateStream {
  // The subscription target as a single key. State is STAMPED with the key it
  // belongs to, so a target switch resets the surfaced values by DERIVATION (the
  // stale frame/status simply no longer match the live key) — no synchronous
  // setState inside the effect, which keeps the effect a pure subscription.
  const active = enabled && !!project && !!agent
  const key = active ? buildUrl(project as string, agent as string, run) : ''

  const [state, setState] = useState<{
    key: string
    frame: RunStateFrame | null
    status: SseStatus
  }>({ key: '', frame: null, status: 'idle' })

  useEffect(() => {
    if (!active) return // not subscribed → derived idle/null below

    // Until the first event arrives, the derived fallback (bottom of the hook)
    // reports 'connecting' for this key — so we set NO state synchronously here;
    // the listeners below stamp the live key as events fire.
    const es = new EventSource(key)

    // A firing listener always belongs to THIS effect's `key` (old listeners are
    // removed on a key change), so each adopts the live key — keeping a frame
    // from a previous target's last event from ever leaking in is the cleanup's
    // job, not a stale-key guard here.
    const onOpen = () =>
      setState((s) => ({ key, frame: s.key === key ? s.frame : null, status: 'open' }))

    const onMessage = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data) as RunStateFrame
        setState({ key, frame: data, status: 'open' })
      } catch {
        // A malformed frame is skipped — keep the stream alive (house law).
      }
    }

    const onError = () =>
      setState((s) => ({
        key,
        frame: s.key === key ? s.frame : null,
        status: s.key === key && s.status === 'open' ? 'connecting' : 'error',
      }))

    es.addEventListener('open', onOpen)
    es.addEventListener('runstate', onMessage as EventListener)
    es.addEventListener('error', onError)

    return () => {
      es.removeEventListener('open', onOpen)
      es.removeEventListener('runstate', onMessage as EventListener)
      es.removeEventListener('error', onError)
      es.close()
    }
  }, [active, key])

  // Surface state only when it belongs to the CURRENT target; otherwise the
  // derived defaults (no frame; connecting if subscribing, idle if not).
  if (state.key === key && key) {
    return { frame: state.frame, status: state.status }
  }
  return { frame: null, status: active ? 'connecting' : 'idle' }
}
