/**
 * RunTranscriptView — renders ONE run's live transcript (the main-area body).
 *
 * It prefers the SSE-pushed `RunStateFrame.selected` (the live, always-fresh read
 * model) and falls back to the REST first-paint transcript when no frame has
 * arrived yet. Both are the SAME backend view-model (decision #5), so they can't
 * disagree; the SSE simply keeps it live with no poll. Segments are seg-typed
 * (RunSpan.kind) so output / tool / error blocks can be styled distinctly.
 */

import { StatusDot } from '../components/glass'
import { cx, statusKind } from '../components/ui'
import { useStickToBottom } from '../components/useStickToBottom'
import type { RunSegment, RunTranscript } from '../api'

interface RunTranscriptViewProps {
  transcript: RunTranscript | null
  /** True while the SSE channel is open (drives the "live" affordance). */
  live: boolean
  emptyHint?: string
}

const SEG_STYLE: Record<string, string> = {
  output: 'text-ink-300',
  tool: 'text-mint-300/90',
  error: 'text-run-errored',
  thinking: 'text-ink-500 italic',
  attachment: 'text-ink-300',
}

function segClass(kind: string): string {
  return SEG_STYLE[kind] ?? SEG_STYLE.output
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

function Segment({ seg }: { seg: RunSegment }) {
  if (!seg.text) return null
  // The `input` span (multi-turn chat, feature-gap step 6) is the USER's message — a
  // turn of the conversation persisted on the run, NOT agent output. Render it as a
  // distinct right-aligned user bubble (mirroring the composer's echo) so the
  // transcript reads as a dialogue, never as the agent's own text. `data-seg-kind`
  // tags the kind for styling + tests.
  if (seg.kind === 'input') {
    return (
      <div data-seg-kind="input" className="my-1 flex justify-end">
        <span className="max-w-[80%] whitespace-pre-wrap break-words rounded-2xl rounded-br-sm bg-mint-500/15 px-3 py-1.5 not-italic text-ink-100 ring-1 ring-mint-400/20">
          {seg.text}
        </span>
      </div>
    )
  }
  // The `attachment` span (chat file-attachments, feature-gap step 6) is a file the user
  // attached to a turn — render it as a right-aligned chip (the filename), alongside the
  // `input` bubble, so the transcript shows what was attached. `data-seg-kind` tags it.
  if (seg.kind === 'attachment') {
    return (
      <div data-seg-kind="attachment" className="my-1 flex justify-end">
        <span className="inline-flex items-center gap-1.5 rounded-lg border border-mint-400/20 bg-base-800/60 px-2 py-1 text-[11px] not-italic text-ink-300">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className="h-3 w-3 shrink-0 text-ink-400"
            aria-hidden="true"
          >
            <path d="M21.4 11.05 12.2 20.2a5 5 0 0 1-7.1-7.1l9.2-9.2a3.33 3.33 0 0 1 4.7 4.7l-9.2 9.2a1.67 1.67 0 0 1-2.35-2.35l8.5-8.5" />
          </svg>
          {seg.text}
        </span>
      </div>
    )
  }
  return (
    <span
      data-seg-kind={seg.kind}
      className={cx('whitespace-pre-wrap break-words', segClass(seg.kind))}
    >
      {seg.text}
    </span>
  )
}

export function RunTranscriptView({ transcript, live, emptyHint }: RunTranscriptViewProps) {
  const segCount = transcript?.segments?.length ?? 0
  const bodyLen = transcript?.body?.length ?? 0

  // Pin to the tail (latest message) ROBUSTLY: re-pins on first paint / run switch /
  // refresh, follows streaming while the user is at the bottom, and never yanks one
  // who scrolled up to read history. Re-applies as content grows so a refresh can't
  // strand the view at the top (the bug this replaced). Shared hook — the logic used
  // to be duplicated per view and kept regressing.
  const bodyRef = useStickToBottom<HTMLDivElement>(transcript?.run_id ?? null, [
    transcript,
    segCount,
    bodyLen,
  ])

  if (!transcript) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <p className="text-sm text-ink-500">{emptyHint ?? 'No run selected.'}</p>
      </div>
    )
  }

  const kind = statusKind(transcript.status_label ?? transcript.status)

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Run header strip. */}
      <div className="flex items-center gap-3 border-b border-glass-line px-5 py-3">
        <StatusDot status={kind} pulse={transcript.running} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-ink-100">
              {transcript.status_label}
            </span>
            {transcript.handoff_short && (
              <span
                className="font-mono text-[11px] text-ink-400"
                title={transcript.handoff_id ?? undefined}
              >
                handoff {transcript.handoff_short}
              </span>
            )}
            {live && transcript.running && (
              <span className="rounded-full bg-mint-500/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-mint-300">
                live
              </span>
            )}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[11px] text-ink-500">
            {transcript.harness && <span>{transcript.harness}</span>}
            {transcript.model && <span>· {transcript.model}</span>}
            {transcript.started_ago && <span>· started {transcript.started_ago} ago</span>}
            {transcript.ended_ago && <span>· ended {transcript.ended_ago} ago</span>}
          </div>
        </div>
        <span className="shrink-0 font-mono text-[10px] text-ink-500" title={transcript.run_id}>
          {transcript.run_id.slice(0, 8)}
        </span>
      </div>

      {/* Error banner (if the run errored). */}
      {transcript.error && (
        <div className="border-b border-run-errored/20 bg-run-errored/10 px-5 py-2 text-xs text-run-errored">
          {transcript.error}
        </div>
      )}

      {/* The transcript body. */}
      <div
        ref={bodyRef}
        data-testid="run-transcript-body"
        className="min-h-0 flex-1 overflow-y-auto px-5 py-4 font-mono text-[12.5px] leading-relaxed"
      >
        {segCount === 0 ? (
          <p className="text-ink-500">
            {transcript.running ? 'Waiting for output…' : 'No transcript output.'}
          </p>
        ) : (
          coalesceSegments(transcript.segments).map((seg, i) => <Segment key={i} seg={seg} />)
        )}
      </div>
    </div>
  )
}
