import { useState } from 'react'
import type { UpdateHealthCheck, UpdateJob, UpdateStatus } from '../api'

interface AppVersionBadgeProps {
  version?: string | null
  updateStatus?: UpdateStatus | null
  updateJob?: UpdateJob | null
  canManageUpdates?: boolean
  onApplyUpdate?: () => Promise<unknown> | unknown
}

function healthTone(check: UpdateHealthCheck): string {
  const status = (check.status || '').toLowerCase()
  if (status === 'ok') return 'text-mint-200'
  if (status === 'failed') return 'text-run-errored'
  if (status === 'warn') return 'text-run-queued'
  return 'text-ink-400'
}

function guidanceList(title: string, items?: string[]) {
  const clean = (items ?? []).filter(Boolean)
  if (clean.length === 0) return null
  return (
    <section className="space-y-1">
      <h4 className="text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-500">{title}</h4>
      <ul className="list-disc space-y-1 pl-4 text-ink-300">
        {clean.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </section>
  )
}

/**
 * The console build stamp — a small, quiet version chip pinned to the BOTTOM-RIGHT
 * corner of the viewport. The live console/feed sits bottom-left, so the right corner
 * is free space where the stamp reads cleanly and never overlaps the streaming text.
 * pointer-events-none so it can never intercept a click.
 */
export function AppVersionBadge({
  version,
  updateStatus,
  updateJob,
  canManageUpdates = true,
  onApplyUpdate,
}: AppVersionBadgeProps) {
  const normalized = (version ?? '').trim()
  const label = normalized ? `v${normalized}` : 'v...'
  const [starting, setStarting] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const updateAvailable = updateStatus?.update_available === true
  const checkUnavailable = updateStatus && updateStatus.check_ok === false
  const jobState = (updateJob?.status || '').toLowerCase()
  const jobRunning = starting || jobState === 'starting' || jobState === 'running'
  const jobFinished = jobState === 'succeeded' || jobState === 'failed' || jobState === 'unknown'
  const healthChecks = updateJob?.health_checks ?? []
  const healthFailed = healthChecks.some((check) => (check.status || '').toLowerCase() !== 'ok')
  const latest = updateStatus?.latest_version ? `v${updateStatus.latest_version}` : 'latest'
  const adminRequired = updateStatus?.admin_required !== false
  const nonAdminUpdate = updateAvailable && adminRequired && !canManageUpdates
  const statusLabel = updateAvailable
    ? jobRunning
      ? `Update is running. The console may restart.`
      : nonAdminUpdate
        ? `Update available: ${latest}. Admin required to apply.`
      : `Update available: ${latest}. Run ${updateStatus?.update_command || './update.sh'}`
    : checkUnavailable
      ? `Update check unavailable: ${updateStatus?.error || 'unknown error'}`
      : normalized
        ? `Console version ${label}`
        : 'Console version loading'
  const canApply = updateAvailable && canManageUpdates && !!onApplyUpdate && !jobRunning
  const showDetails = Boolean(updateStatus || updateJob || applyError)

  const apply = async () => {
    if (!canApply) return
    setStarting(true)
    setApplyError(null)
    try {
      await onApplyUpdate?.()
    } catch (error) {
      setApplyError(error instanceof Error ? error.message : String(error))
    } finally {
      setStarting(false)
    }
  }

  return (
    <div
      aria-label={statusLabel}
      data-testid="app-version-badge"
      className="fixed bottom-2 right-3 z-40 flex select-none pointer-events-none items-center gap-1.5 rounded-md bg-base-900/70 px-2 py-1 text-[10px] leading-none text-ink-500 backdrop-blur-sm"
    >
      {detailsOpen && (
        <div className="pointer-events-auto absolute bottom-full right-0 mb-2 max-h-[70vh] w-[min(28rem,calc(100vw-1.5rem))] overflow-y-auto rounded-xl border border-glass-line bg-base-950/95 p-3 text-left text-xs leading-normal text-ink-300 shadow-2xl backdrop-blur">
          <div className="mb-2 flex items-start justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-ink-100">Kaidera OS update</h3>
              <p className="text-[11px] text-ink-500">
                {updateStatus?.release_name || updateStatus?.latest_tag || latest}
              </p>
            </div>
            <button
              type="button"
              onClick={() => setDetailsOpen(false)}
              className="rounded px-1.5 py-0.5 text-[10px] uppercase tracking-[0.14em] text-ink-500 hover:bg-base-800 hover:text-ink-200"
            >
              Close
            </button>
          </div>

          <div className="mb-3 grid grid-cols-2 gap-2 rounded-lg border border-glass-line bg-base-900/60 p-2 text-[11px]">
            <div>
              <span className="block uppercase tracking-[0.14em] text-ink-600">Current</span>
              <span className="font-mono text-ink-200">{label}</span>
            </div>
            <div>
              <span className="block uppercase tracking-[0.14em] text-ink-600">Latest</span>
              <span className="font-mono text-ink-200">{latest}</span>
            </div>
            <div className="col-span-2">
              <span className="block uppercase tracking-[0.14em] text-ink-600">Source</span>
              <span className="font-mono text-ink-300">{updateStatus?.repo || 'unknown'}</span>
            </div>
          </div>

          {nonAdminUpdate && (
            <p className="mb-3 rounded-lg border border-amber-300/20 bg-amber-400/10 p-2 text-amber-100">
              Update is available, but an admin must apply it.
            </p>
          )}

          {updateStatus?.release_notes && (
            <section className="mb-3 space-y-1">
              <h4 className="text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-500">Release notes</h4>
              <p className="max-h-32 overflow-y-auto whitespace-pre-wrap rounded-lg bg-base-900/60 p-2 text-ink-300">
                {updateStatus.release_notes}
              </p>
            </section>
          )}

          <div className="space-y-3">
            {guidanceList('Impact', updateStatus?.impact)}

            {(jobFinished || jobRunning) && (
              <section className="space-y-1">
                <h4 className="text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-500">Job status</h4>
                <p className="rounded-lg bg-base-900/60 p-2">
                  <span className="font-semibold text-ink-100">{updateJob?.status || 'unknown'}</span>
                  {updateJob?.return_code != null && (
                    <span className="ml-2 text-ink-500">rc {updateJob.return_code}</span>
                  )}
                  {updateJob?.log_path && (
                    <span className="mt-1 block break-all font-mono text-[10px] text-ink-500">
                      {updateJob.log_path}
                    </span>
                  )}
                  {updateJob?.error && (
                    <span className="mt-1 block text-run-errored">{updateJob.error}</span>
                  )}
                </p>
              </section>
            )}

            {healthChecks.length > 0 && (
              <section className="space-y-1">
                <h4 className="text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-500">Post-update health</h4>
                <ul className="space-y-1 rounded-lg bg-base-900/60 p-2">
                  {healthChecks.map((check) => (
                    <li key={`${check.name}:${check.checked_at || ''}`} className="flex gap-2">
                      <span className={healthTone(check)}>{(check.status || 'unknown').toUpperCase()}</span>
                      <span className="min-w-0 flex-1">
                        <span className="font-medium text-ink-200">{check.name}</span>
                        {check.detail && <span className="block truncate text-ink-500">{check.detail}</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {healthChecks.length === 0 && guidanceList('Post-update checks', updateStatus?.post_update_checks)}
            {guidanceList('Backup', updateStatus?.backup_guidance)}
            {guidanceList('Rollback', updateStatus?.rollback_guidance)}
          </div>

          {updateStatus?.release_url && (
            <a
              href={updateStatus.release_url}
              target="_blank"
              rel="noreferrer"
              className="mt-3 inline-flex rounded-md border border-glass-line px-2 py-1 text-[11px] text-mint-200 hover:bg-mint-400/10"
            >
              Open release
            </a>
          )}
        </div>
      )}
      <span className="font-medium uppercase tracking-[0.16em] text-ink-600">Version</span>
      <span className="font-mono tabular-nums text-ink-400">{label}</span>
      {updateAvailable && (
        <>
          <span className="rounded bg-amber-400/15 px-1.5 py-0.5 font-semibold uppercase tracking-[0.14em] text-amber-200">
            {jobRunning ? 'Updating' : `Update ${latest}`}
          </span>
          {nonAdminUpdate && (
            <span className="rounded bg-base-700/70 px-1.5 py-0.5 uppercase tracking-[0.14em] text-ink-500">
              Admin required
            </span>
          )}
          {canManageUpdates && onApplyUpdate && (
            <button
              type="button"
              onClick={apply}
              disabled={!canApply}
              className="pointer-events-auto rounded bg-amber-300 px-1.5 py-0.5 font-semibold uppercase tracking-[0.12em] text-base-950 transition-colors hover:bg-amber-200 disabled:cursor-not-allowed disabled:bg-base-700 disabled:text-ink-500"
            >
              {jobRunning ? 'Running' : 'Apply'}
            </button>
          )}
        </>
      )}
      {jobFinished && (
        <span className={healthFailed ? 'rounded bg-run-errored/15 px-1.5 py-0.5 text-run-errored' : 'rounded bg-mint-400/10 px-1.5 py-0.5 text-mint-200'}>
          {healthFailed ? 'Needs check' : 'Healthy'}
        </span>
      )}
      {!updateAvailable && checkUnavailable && (
        <span className="rounded bg-base-700/70 px-1.5 py-0.5 uppercase tracking-[0.14em] text-ink-500">
          Check unavailable
        </span>
      )}
      {applyError && (
        <span className="rounded bg-run-errored/15 px-1.5 py-0.5 text-run-errored">
          Apply failed
        </span>
      )}
      {showDetails && (
        <button
          type="button"
          onClick={() => setDetailsOpen((open) => !open)}
          className="pointer-events-auto rounded border border-glass-line px-1.5 py-0.5 font-semibold uppercase tracking-[0.12em] text-ink-400 transition-colors hover:bg-base-800 hover:text-ink-100"
        >
          Details
        </button>
      )}
    </div>
  )
}
