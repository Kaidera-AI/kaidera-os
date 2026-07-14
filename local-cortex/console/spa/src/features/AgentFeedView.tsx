/**
 * AgentFeedView - a continuous, agent-scoped chat/thinking feed.
 *
 * The run rail is still useful for selecting or following a run, but the main body
 * should read like one conversation stream across the agent's recent runs. This view
 * hydrates each run header from the existing run transcript endpoint and overlays the
 * live SSE transcript for the currently selected run when present.
 */

import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { StatusDot } from '../components/glass'
import { cx, statusKind } from '../components/ui'
import { useStickToBottom } from '../components/useStickToBottom'
import type { RunRow, RunSegment, RunTranscript } from '../api'

export interface AgentFeedClient {
  run: (runId: string, signal?: AbortSignal) => Promise<RunTranscript>
}

interface AgentFeedViewProps {
  runs: RunRow[]
  liveTranscript: RunTranscript | null
  selectedRunId: string | null
  live: boolean
  emptyHint?: string
  client?: AgentFeedClient
}

interface FeedState {
  scope: string
  key: string
  transcripts: RunTranscript[]
  failed: number
  error: Error | null
  loading: boolean
}

type LoadResult =
  | { ok: true; data: RunTranscript }
  | { ok: false; aborted: boolean; error: Error | null }

type FailedLoadResult = { ok: false; aborted: boolean; error: Error | null }

const feedClient: AgentFeedClient = {
  run: (runId, signal) => api.run(runId, signal),
}

const RUN_RETRY_DELAY_MS = 150
const RUN_LOAD_RETRIES = 2

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer)
      reject(new DOMException('The operation was aborted.', 'AbortError'))
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    if (signal.aborted) {
      onAbort()
      return
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

async function loadRun(
  client: AgentFeedClient,
  runId: string,
  signal: AbortSignal,
): Promise<RunTranscript> {
  // A run listed by the board should already have a durable detail row. Keep a
  // short bounded retry for transactional propagation without coupling hydration
  // to volatile board objects.
  let lastError: unknown = new Error('run could not load')
  for (let attempt = 0; attempt <= RUN_LOAD_RETRIES; attempt += 1) {
    try {
      return await client.run(runId, signal)
    } catch (err) {
      lastError = err
      if (signal.aborted || attempt === RUN_LOAD_RETRIES) throw err
      await wait(RUN_RETRY_DELAY_MS * (attempt + 1), signal)
    }
  }
  throw lastError
}

const SEGMENT_STYLE: Record<string, string> = {
  input: 'text-ink-100',
  output: 'text-ink-300',
  thinking: 'text-ink-500 italic',
  tool: 'text-mint-300/90',
  error: 'text-run-errored',
  attachment: 'text-ink-300',
}

function asError(value: unknown): Error {
  return value instanceof Error ? value : new Error(String(value))
}

function segmentLabel(seg: RunSegment, transcript: RunTranscript): string {
  if (seg.kind === 'input') return 'you'
  if (seg.kind === 'thinking') return 'thinking'
  if (seg.kind === 'tool') return 'tool'
  if (seg.kind === 'error') return 'error'
  if (seg.kind === 'attachment') return 'file'
  return transcript.agent_display ?? transcript.agent ?? 'agent'
}

function segmentClass(kind: string): string {
  return SEGMENT_STYLE[kind] ?? SEGMENT_STYLE.output
}

function shouldMergeSegment(kind: string): boolean {
  return kind === 'output' || kind === 'thinking'
}

function coalesceSegments(segments: RunSegment[]): RunSegment[] {
  const merged: RunSegment[] = []
  for (const seg of segments) {
    const last = merged[merged.length - 1]
    if (last && last.kind === seg.kind && shouldMergeSegment(seg.kind)) {
      last.text += seg.text
    } else {
      merged.push({ ...seg })
    }
  }
  return merged
}

function FeedSegment({ seg, transcript }: { seg: RunSegment; transcript: RunTranscript }) {
  if (!seg.text) return null
  return (
    <div
      data-feed-line
      data-seg-kind={seg.kind}
      className="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-3 py-1"
    >
      <span className="select-none pt-[1px] text-right font-mono text-[10px] uppercase tracking-wide text-ink-500">
        {segmentLabel(seg, transcript)}
      </span>
      <span className={cx('whitespace-pre-wrap break-words', segmentClass(seg.kind))}>
        {seg.text}
      </span>
    </div>
  )
}

function RunMarker({
  transcript,
  row,
  active,
  live,
}: {
  transcript: RunTranscript
  row: RunRow | null
  active: boolean
  live: boolean
}) {
  const kind = statusKind(transcript.status_label ?? transcript.status)
  const agent = transcript.agent_display ?? row?.agent_display ?? transcript.agent ?? 'agent'
  const harness = transcript.harness ?? row?.harness
  const model = transcript.model ?? row?.model
  return (
    <div
      data-feed-run={transcript.run_id}
      className={cx(
        'mt-4 flex items-center gap-2 border-t border-glass-line/70 pt-3 first:mt-0 first:border-t-0 first:pt-0',
        active ? 'text-ink-200' : 'text-ink-500',
      )}
    >
      <StatusDot status={kind} pulse={transcript.running} />
      <span className="truncate text-[10px] font-semibold uppercase tracking-wide">
        {agent}
      </span>
      <span className="font-mono text-[10px]" title={transcript.run_id}>
        {transcript.run_id.slice(0, 8)}
      </span>
      {transcript.handoff_short && (
        <span className="font-mono text-[10px]">handoff {transcript.handoff_short}</span>
      )}
      {harness && <span className="hidden text-[10px] sm:inline">{harness}</span>}
      {model && <span className="hidden text-[10px] sm:inline">{model}</span>}
      {transcript.started_ago && (
        <span className="ml-auto shrink-0 text-[10px]">started {transcript.started_ago} ago</span>
      )}
      {live && transcript.running && (
        <span className="rounded-full bg-mint-500/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-mint-300">
          live
        </span>
      )}
    </div>
  )
}

function uniqueRunIds(rows: RunRow[], liveRunId: string | null): string[] {
  const ids: string[] = []
  const seen = new Set<string>()
  for (const row of [...rows].reverse()) {
    if (seen.has(row.run_id)) continue
    seen.add(row.run_id)
    ids.push(row.run_id)
  }
  if (liveRunId && !seen.has(liveRunId)) ids.push(liveRunId)
  return ids
}

export function AgentFeedView({
  runs,
  liveTranscript,
  selectedRunId,
  live,
  emptyHint,
  client = feedClient,
}: AgentFeedViewProps) {
  const liveRunId = liveTranscript?.run_id ?? null
  const feedIds = useMemo(() => uniqueRunIds(runs, liveRunId), [runs, liveRunId])
  const feedKey = feedIds.join('|')
  const scopeKey = `${runs[0]?.project ?? liveTranscript?.project ?? ''}/${runs[0]?.agent ?? liveTranscript?.agent ?? ''}`
  const rowsById = useMemo(() => new Map(runs.map((row) => [row.run_id, row])), [runs])
  // The list endpoint can expose a queued row before its durable detail record is
  // readable. This key changes only when a run enters/leaves the hydratable set,
  // not on heartbeat polls or other row-object churn.
  const hydrationKey = feedIds
    .filter((runId) => rowsById.get(runId)?.status !== 'queued')
    .join('|')
  const [state, setState] = useState<FeedState>({
    scope: '',
    key: '',
    transcripts: [],
    failed: 0,
    error: null,
    loading: false,
  })

  useEffect(() => {
    if (feedIds.length === 0) return

    const ctrl = new AbortController()
    const prior = state.scope === scopeKey ? state.transcripts : []
    const loadedIds = new Set(prior.map((tx) => tx.run_id))
    // The SSE transcript is the authoritative in-memory copy while a chat is
    // streaming. Do not probe its client-minted id before the backend row exists;
    // when the stream hands off, `liveRunId` changes and this effect hydrates the
    // durable copy exactly once.
    const missingIds = feedIds.filter(
      (runId) =>
        runId !== liveRunId &&
        rowsById.get(runId)?.status !== 'queued' &&
        !loadedIds.has(runId),
    )

    if (missingIds.length === 0) {
      return () => ctrl.abort()
    }

    Promise.all<LoadResult>(
      missingIds.map(async (runId) => {
        try {
          return {
            ok: true,
            data: await loadRun(client, runId, ctrl.signal),
          }
        } catch (err) {
          return {
            ok: false,
            aborted: ctrl.signal.aborted,
            error: ctrl.signal.aborted ? null : asError(err),
          }
        }
      }),
    ).then((results) => {
      if (ctrl.signal.aborted) return
      const transcripts = results
        .filter((result): result is { ok: true; data: RunTranscript } => result.ok)
        .map((result) => result.data)
      const failures = results.filter(
        (result): result is FailedLoadResult => !result.ok && !result.aborted,
      )
      const firstError = failures.find((result) => result.error)?.error ?? null
      setState((current) => {
        const merged = new Map(
          (current.scope === scopeKey ? current.transcripts : prior).map((tx) => [
            tx.run_id,
            tx,
          ]),
        )
        for (const transcript of transcripts) merged.set(transcript.run_id, transcript)
        return {
          scope: scopeKey,
          key: feedKey,
          transcripts: feedIds
            .map((runId) => merged.get(runId))
            .filter((tx): tx is RunTranscript => Boolean(tx)),
          failed: failures.length,
          error: firstError,
          loading: false,
        }
      })
    })

    return () => ctrl.abort()
    // `feedKey` is the identity of the requested set. State is deliberately not a
    // dependency: adding one run should fetch that run, not recursively re-fetch all
    // transcripts after each cache write.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, hydrationKey, liveRunId, scopeKey])

  // STALE-WHILE-REVALIDATE: keep the last-loaded transcripts even when `feedKey` changes
  // (a board poll adding the just-finished run, a new turn, a liveRunId change). The old
  // code reset this to [] on ANY feedIds churn, which BLANKED every run's content until the
  // re-hydration landed — that is the chat reply "showing then disappearing on a refresh".
  // Runs from a different agent simply aren't in the new `feedIds` below, so they're
  // filtered out regardless; keeping the stale set only ever preserves the CURRENT agent's
  // runs while their fresh copy loads.
  const scopedState = state.scope === scopeKey ? state : null
  const loaded = scopedState?.transcripts ?? []
  const loadedIds = new Set(loaded.map((tx) => tx.run_id))
  const allLoaded = feedIds.every((runId) => loadedIds.has(runId))
  const loading =
    feedIds.length > 0 &&
    !allLoaded &&
    (scopedState?.key === feedKey ? scopedState.loading : true)
  const failed = allLoaded || scopedState?.key !== feedKey ? 0 : scopedState.failed
  const error = allLoaded || scopedState?.key !== feedKey ? null : scopedState.error
  const transcriptsById = new Map(loaded.map((tx) => [tx.run_id, tx]))
  if (liveTranscript) transcriptsById.set(liveTranscript.run_id, liveTranscript)
  const transcripts = feedIds
    .map((runId) => transcriptsById.get(runId))
    .filter((tx): tx is RunTranscript => Boolean(tx))

  const segCount = transcripts.reduce((total, tx) => total + (tx.segments?.length ?? 0), 0)
  const anyRunning = transcripts.some((tx) => tx.running)

  // Pin the feed to the tail (latest messages) ROBUSTLY — re-pins on first paint /
  // feed change / refresh, follows streaming while at the bottom, never yanks a user
  // who scrolled up, and re-applies as the feed hydrates so a refresh can't strand
  // the view at the top. Shared hook with the chat transcript (this logic kept
  // regressing when each view had its own copy). `feedKey` is the reset key.
  const bodyRef = useStickToBottom<HTMLDivElement>(feedKey, [
    loading,
    segCount,
    transcripts.length,
  ])

  if (feedIds.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <p className="text-sm text-ink-500">
          {emptyHint ?? 'This agent has no chat history yet.'}
        </p>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-3 border-b border-glass-line px-5 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-ink-100">Continuous feed</span>
            {live && (
              <span className="rounded-full bg-mint-500/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-mint-300">
                live
              </span>
            )}
          </div>
          <div className="mt-0.5 text-[11px] text-ink-500">
            {feedIds.length} {feedIds.length === 1 ? 'run' : 'runs'} stitched into one stream
          </div>
        </div>
        {loading && <span className="text-[11px] text-ink-500">Loading feed...</span>}
      </div>

      {failed > 0 && (
        <div className="border-b border-run-errored/20 bg-run-errored/10 px-5 py-2 text-xs text-run-errored">
          {transcripts.length > 0
            ? 'Some earlier runs could not load.'
            : `Feed could not load${error?.message ? `: ${error.message}` : '.'}`}
        </div>
      )}

      <div
        ref={bodyRef}
        data-testid="agent-continuous-feed"
        aria-label="Continuous chat and thinking feed"
        className="min-h-0 flex-1 overflow-y-auto px-5 py-4 font-mono text-[12.5px] leading-relaxed"
      >
        {transcripts.length === 0 && loading ? (
          <p className="text-ink-500">Loading feed...</p>
        ) : segCount === 0 ? (
          <p className="text-ink-500">
            {anyRunning ? 'Waiting for output...' : 'No chat history yet.'}
          </p>
        ) : (
          transcripts.map((tx) => (
            <section key={tx.run_id} className="contents">
              <RunMarker
                transcript={tx}
                row={rowsById.get(tx.run_id) ?? null}
                active={selectedRunId === tx.run_id}
                live={live}
              />
              {tx.error && (
                <div data-feed-line data-seg-kind="error" className="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-3 py-1">
                  <span className="select-none pt-[1px] text-right font-mono text-[10px] uppercase tracking-wide text-ink-500">
                    error
                  </span>
                  <span className="whitespace-pre-wrap break-words text-run-errored">
                    {tx.error}
                  </span>
                </div>
              )}
              {coalesceSegments(tx.segments).map((seg, idx) => (
                <FeedSegment key={`${tx.run_id}-${idx}`} seg={seg} transcript={tx} />
              ))}
            </section>
          ))
        )}
      </div>
    </div>
  )
}
