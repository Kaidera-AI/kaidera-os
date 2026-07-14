/**
 * ExplainGallery — the list of PAST explainers, with a "View" per item.
 *
 * Reads `GET /explain/{project}/list`, now enumerated SERVER-SIDE from run_state
 * (`lease_owner='explain'` runs), NOT Cortex content search — which can't reliably
 * prefix-enumerate artifacts (the live-testing bug this fixed). So each item carries a
 * FIRST-CLASS `run_id` (+ the target/caption/created_at/status from the run's metadata).
 * "View" RE-RENDERS the full document from that explainer's RUN spans
 * (`GET /runs/run/{run_id}` → concat of the `output` spans) and hands the full HTML up to
 * ExplainView (`onView`), which shows it in the SAME sandboxed iframe.
 *
 * The HTML itself is NEVER touched here beyond passing the string up — the render + the
 * isolation live in ExplainFrame (the single sandboxed seam). Empty / down store → a
 * clean empty state (the list degrades to [] server-side).
 */

import { useCallback, useState } from 'react'
import { explainExportUrl, explainHtmlFromRun, useResource } from '../api'
import { cx } from '../components/ui'
import type { ExplainListItem } from '../api'
import type { ExplainClient } from './ExplainView'

interface ExplainGalleryProps {
  project: string
  client: ExplainClient
  /** Called with a past explainer's FULL HTML + its caption when "View" is clicked. */
  onView: (html: string, caption: string, runId: string) => void
}

/** A compact 'how long ago' label for an ISO timestamp ('' when absent/unparseable). */
function relAge(ts: string | null | undefined): string {
  if (!ts) return ''
  const then = Date.parse(ts)
  if (Number.isNaN(then)) return ''
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 5) return 'now'
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

/** The friendly status chip word + tone for a run status. Null/ok → no chip (the common case). */
function statusChip(status: string | null | undefined): { label: string; cls: string } | null {
  const s = (status || '').toLowerCase()
  if (s === 'error') return { label: 'errored', cls: 'text-run-errored' }
  if (s === 'recovered') return { label: 'recovered', cls: 'text-mint-300' }
  if (s === 'running' || s === 'queued') return { label: s === 'queued' ? 'queued' : 'generating', cls: 'text-mint-300' }
  return null // ok / unknown → no chip
}

export function ExplainGallery({ project, client, onView }: ExplainGalleryProps) {
  const list = useResource<ExplainListItem[]>(
    (signal) => client.getExplainList(project, signal),
    [project],
    { pollMs: 20000 },
  )
  // The run_id currently being fetched for a "View" (disables that row's button).
  const [loadingId, setLoadingId] = useState<string | null>(null)
  const [viewError, setViewError] = useState<string | null>(null)

  const onViewItem = useCallback(
    async (item: ExplainListItem) => {
      if (!item.run_id) return
      setLoadingId(item.run_id)
      setViewError(null)
      try {
        const run = await client.run(item.run_id)
        const html = explainHtmlFromRun(run)
        if (!html) {
          setViewError('That explainer has no rendered document yet.')
          return
        }
        onView(html, item.caption || 'Saved explainer', item.run_id)
      } catch (e: unknown) {
        setViewError(e instanceof Error ? e.message : String(e))
      } finally {
        setLoadingId(null)
      }
    },
    [client, onView],
  )

  const items = list.data ?? []

  return (
    <div>
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-ink-400">
        Saved explainers
      </h3>

      {viewError && (
        <div className="mb-2 rounded-lg border border-run-errored/25 bg-run-errored/10 px-2.5 py-1.5 text-[11px] text-run-errored">
          {viewError}
        </div>
      )}

      {items.length === 0 ? (
        <p className="text-[11px] text-ink-500">
          {list.loading ? 'Loading…' : 'No explainers yet — generate one above.'}
        </p>
      ) : (
        <ul className="space-y-1.5" aria-label="Saved explainers">
          {items.map((item, i) => (
            <li
              key={item.artifact_id ?? item.source_file ?? i}
              className="glass-soft flex items-start gap-2 rounded-lg px-2.5 py-2"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-[12px] text-ink-200" title={item.caption}>
                  {item.caption || 'Untitled explainer'}
                </p>
                <p className="flex items-center gap-1.5 truncate text-[10px] text-ink-500">
                  {(item.target_path || item.run_id) && (
                    <span className="truncate font-mono">
                      {item.target_kind ? `${item.target_kind} · ` : ''}
                      {item.target_path ?? item.run_id?.slice(0, 8)}
                    </span>
                  )}
                  {relAge(item.created_at) && (
                    <span className="shrink-0 text-ink-600">· {relAge(item.created_at)}</span>
                  )}
                  {statusChip(item.status) && (
                    <span className={cx('shrink-0 font-medium', statusChip(item.status)!.cls)}>
                      · {statusChip(item.status)!.label}
                    </span>
                  )}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-0.5">
                <button
                  type="button"
                  disabled={!item.run_id || loadingId === item.run_id}
                  onClick={() => onViewItem(item)}
                  className={cx(
                    'rounded-md px-2 py-1 text-[11px] font-medium transition-colors',
                    item.run_id
                      ? 'text-mint-300 hover:bg-base-800/60'
                      : 'cursor-not-allowed text-ink-600',
                  )}
                  title={item.run_id ? 'Render this explainer' : 'No run to render from'}
                >
                  {item.run_id && loadingId === item.run_id ? 'Loading…' : 'View'}
                </button>
                {item.run_id && (
                  <a
                    href={explainExportUrl(project, item.run_id)}
                    download
                    aria-label={`Export ${item.caption || 'explainer'} archive`}
                    title="Export explainer archive"
                    className="rounded-md px-2 py-1 text-[11px] font-medium text-ink-400 transition-colors hover:bg-base-800/60 hover:text-ink-200"
                  >
                    ↓ Export
                  </a>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
