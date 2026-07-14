/**
 * HistoryView — the "History" main-area tab: the cross-agent activity timeline (feature-gap
 * #80, the second piece; the Graph view shipped first).
 *
 * A reverse-chronological timeline of the project's recent activity (who · what · when) over
 * REAL Cortex data: the noisy `/history` tool-call JSON is summarised SERVER-SIDE (the ported
 * `_summarize_history_row`) into a clean readable line per row, tagged by kind (say = a
 * message, tool = an action, think = a reasoning step). A recent-DECISIONS side panel (from
 * Cortex `/search`) sits alongside, and the header carries the roster agent count. It is a
 * read-only activity feed — the rich span-tree trace replay is a later increment.
 *
 * It COMPOSES with (does not replace) the other main-area tabs — one more tab alongside
 * Agent · Dispatch · Analytics · Settings · Explain · Graph · History.
 *
 * DATA: `client.history(project, limit?)` → `{events, decisions, agent_count}` (the backend's
 * shaped, summarised, bounded payload). A gentle poll keeps it live (matching the SPA's
 * snapshot-catalog cadence); a manual Refresh re-fetches now. Glass-morphism throughout;
 * empty / loading / error states. The relative-age label (`ts_ago`) is computed server-side
 * (same formatter the run rail uses), so the timeline renders identical age labels.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { GlassPanel } from '../components/glass'
import { cx } from '../components/ui'
import type { HistoryEvent, HistoryDecision, HistoryPayload } from '../api'

/** A gentle poll cadence for the timeline (matches the SPA's snapshot-catalog refresh — the
 * live transcript is pushed by SSE elsewhere; this surface is a periodic snapshot). */
const HISTORY_POLL_MS = 12000

/** The slice of the api client HistoryView needs — so tests fake one object. */
export interface HistoryClient {
  history: (
    project: string,
    limit?: number,
    signal?: AbortSignal,
    opts?: { includeDecisions?: boolean },
  ) => Promise<HistoryPayload>
}

interface HistoryViewProps {
  project: string | null
  client: HistoryClient
  /** Override the poll cadence (tests pass 0 / undefined to disable the interval). */
  pollMs?: number
}

type LoadState = 'loading' | 'ready' | 'error'

export function HistoryView({ project, client, pollMs = HISTORY_POLL_MS }: HistoryViewProps) {
  const [data, setData] = useState<HistoryPayload | null>(null)
  const [state, setState] = useState<LoadState>('loading')

  // The active fetch's abort controller (so a project switch / re-fetch cancels the prior).
  const acRef = useRef<AbortController | null>(null)
  // Which project the current view belongs to — drives the "reset on project switch" using
  // the React "adjust state during render" pattern (the same one MainArea/GraphView use), so
  // the project-change resets happen in render, NOT synchronously inside an effect.
  const [viewProject, setViewProject] = useState<string | null>(project)
  if (project !== viewProject) {
    setViewProject(project)
    setData(null)
    setState('loading')
  }

  // Keep the latest project + client in refs so the fetch closure is STABLE (empty-deps
  // useCallback) — the same shape useResource/GraphView use, which keeps the
  // set-state-in-effect analysis happy (the effect calls an opaque stable callback).
  const projectRef = useRef(project)
  const clientRef = useRef(client)
  useEffect(() => {
    projectRef.current = project
    clientRef.current = client
  })

  // The single fetch path. Always lands the surface on a truthful state (loading →
  // ready/error). The caller owns the AbortSignal so a project switch / poll tick can cancel
  // the prior in-flight fetch. Stable identity (refs above).
  const run = useCallback((signal: AbortSignal) => {
    const proj = projectRef.current
    const c = clientRef.current
    if (!proj) return
    c.history(proj, undefined, signal, { includeDecisions: true })
      .then((payload) => {
        if (signal.aborted) return
        setData(payload)
        setState('ready')
      })
      .catch((e: unknown) => {
        if (signal.aborted || (e instanceof DOMException && e.name === 'AbortError')) return
        setState('error')
      })
  }, [])

  // Fire a fetch, replacing any in-flight one (its controller is aborted first).
  const load = useCallback(() => {
    acRef.current?.abort()
    const ac = new AbortController()
    acRef.current = ac
    run(ac.signal)
  }, [run])

  // (Re)load whenever the project changes + on a gentle poll. The synchronous resets happen
  // in render (above); this effect kicks off the async fetch via the stable `run` callback,
  // arms the interval, and aborts the in-flight fetch on unmount / project change.
  useEffect(() => {
    if (!project) return
    const ac = new AbortController()
    acRef.current = ac
    run(ac.signal)
    let timer: ReturnType<typeof setInterval> | null = null
    if (pollMs && pollMs > 0) {
      timer = setInterval(() => {
        const tick = new AbortController()
        acRef.current = tick
        run(tick.signal)
      }, pollMs)
    }
    return () => {
      ac.abort()
      if (timer) clearInterval(timer)
    }
  }, [project, run, pollMs])

  if (!project) {
    return (
      <GlassPanel className="flex-1">
        <div className="flex h-full items-center justify-center p-10">
          <p className="text-sm text-ink-500">Select a project to view its activity history.</p>
        </div>
      </GlassPanel>
    )
  }

  const events = data?.events ?? []
  const decisions = data?.decisions ?? []
  const agentCount = data?.agent_count ?? 0

  return (
    <GlassPanel className="min-w-0 flex-1">
      <div className="flex h-full min-h-0 flex-col">
        {/* ---------- header: title + counts + refresh ---------- */}
        <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-glass-line px-5 py-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-ink-100">Activity history</h2>
            <p className="text-[11px] text-ink-500">
              Cross-agent timeline · who · what · when
            </p>
          </div>
          <div className="ml-auto flex items-center gap-3">
            <div data-testid="history-counts" className="flex items-center gap-3 text-[11px] text-ink-400">
              <span>
                <b className="tabular-nums text-ink-100">{events.length}</b> events
              </span>
              <span className="text-ink-600">·</span>
              <span>
                <b className="tabular-nums text-ink-100">{agentCount}</b>{' '}
                {agentCount === 1 ? 'agent' : 'agents'}
              </span>
              <span
                className="inline-flex items-center gap-1.5 text-ink-500"
                title="Live, polled from Cortex /history"
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-mint-400" /> live
              </span>
            </div>
            <button
              type="button"
              onClick={load}
              aria-label="Refresh the activity timeline"
              className="glass-soft inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-medium text-ink-300 hover:bg-base-800/70 hover:text-ink-100"
            >
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                <path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" />
              </svg>
              Refresh
            </button>
          </div>
        </header>

        {/* ---------- two-pane: timeline (left) + recent decisions (right) ---------- */}
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-0 lg:grid-cols-[1fr_20rem]">
          {/* ===== TIMELINE ===== */}
          <section
            data-testid="history-timeline"
            className="min-h-0 overflow-y-auto px-5 py-4 lg:border-r lg:border-glass-line"
          >
            {state === 'loading' && !data && (
              <div data-testid="history-loading" className="flex h-full items-center justify-center">
                <p className="text-sm text-ink-400">Loading the activity timeline…</p>
              </div>
            )}

            {state === 'error' && (
              <div data-testid="history-error" className="flex h-full flex-col items-center justify-center text-center">
                <p className="text-sm font-medium text-run-errored">The activity history could not be loaded.</p>
                <p className="mt-1 max-w-md text-xs text-ink-500">
                  Cortex is unreachable, or the project has no history yet. Try again.
                </p>
                <button
                  type="button"
                  onClick={load}
                  className="mt-3 rounded-lg bg-base-700/60 px-3 py-1.5 text-xs font-medium text-ink-200 hover:bg-base-700"
                >
                  Retry
                </button>
              </div>
            )}

            {state === 'ready' && events.length === 0 && (
              <div data-testid="history-empty" className="flex h-full flex-col items-center justify-center text-center">
                <p className="text-sm font-medium text-ink-300">No recent activity</p>
                <p className="mt-1 max-w-md text-xs text-ink-500">
                  Nothing in the live <code className="text-ink-400">/history</code> window for this
                  project yet. Activity appears here as agents log decisions and run tools.
                </p>
              </div>
            )}

            {events.length > 0 && (
              <ol className="flex flex-col gap-1.5">
                {events.map((ev, i) => (
                  <TimelineRow key={`${ev.ts}-${i}`} ev={ev} />
                ))}
              </ol>
            )}
          </section>

          {/* ===== RECENT DECISIONS RAIL ===== */}
          <aside
            data-testid="history-decisions"
            className="hidden min-h-0 flex-col overflow-hidden lg:flex"
          >
            <div className="flex shrink-0 items-center gap-2 border-b border-glass-line px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
                <path d="M9 11l3 3L22 4" />
                <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
              </svg>
              Recent decisions
              <span className="ml-auto rounded-full bg-base-800/70 px-1.5 py-0.5 text-[10px] tabular-nums text-ink-400">
                {decisions.length}
              </span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
              {decisions.length > 0 ? (
                <ul className="flex flex-col gap-2">
                  {decisions.map((d, i) => (
                    <DecisionRow key={`${d.ts}-${i}`} d={d} />
                  ))}
                </ul>
              ) : (
                <p className="text-[11px] leading-relaxed text-ink-500">
                  No recent decisions or lessons surfaced from <code className="text-ink-400">/search</code> for
                  this project.
                </p>
              )}
            </div>
          </aside>
        </div>
      </div>
    </GlassPanel>
  )
}

// ---------------------------------------------------------------------------
//  small presentational bits
// ---------------------------------------------------------------------------

/** One timeline row: a kind dot · agent · summary · relative time. */
function TimelineRow({ ev }: { ev: HistoryEvent }) {
  return (
    <li
      data-seg-kind={ev.kind}
      className="group flex items-start gap-2.5 rounded-lg px-2 py-1.5 hover:bg-base-800/40"
    >
      <span className={cx('mt-1.5 h-2 w-2 shrink-0 rounded-full', kindDotClass(ev.kind))} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="shrink-0 text-[12px] font-semibold text-ink-200">{ev.agent}</span>
          <span className="shrink-0 text-[10px] uppercase tracking-wide text-ink-600">{ev.kind_label}</span>
          {ev.ts_ago && (
            <span className="ml-auto shrink-0 text-[10px] tabular-nums text-ink-600" title={ev.ts}>
              {ev.ts_ago}
            </span>
          )}
        </div>
        <p className="mt-0.5 break-words text-[12.5px] leading-snug text-ink-300">{ev.summary}</p>
      </div>
    </li>
  )
}

/** One recent-decision row: a source chip · the readable text · category. */
function DecisionRow({ d }: { d: HistoryDecision }) {
  return (
    <li className="glass-soft rounded-lg px-2.5 py-2">
      <div className="flex items-center gap-1.5">
        <span className="rounded bg-mint-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-mint-300">
          {d.source}
        </span>
        {d.category && (
          <span className="text-[9px] uppercase tracking-wide text-ink-600">{d.category}</span>
        )}
        {d.ts_ago && (
          <span className="ml-auto text-[9px] tabular-nums text-ink-600" title={d.ts}>{d.ts_ago}</span>
        )}
      </div>
      <p className="mt-1 break-words text-[11.5px] leading-snug text-ink-300">{d.summary}</p>
    </li>
  )
}

/** The kind → dot-colour mapping (say = a message, tool = an action, think = a reasoning step). */
function kindDotClass(kind: string): string {
  if (kind === 'tool') return 'bg-run-queued'
  if (kind === 'think') return 'bg-mint-400'
  return 'bg-run-completed'
}
