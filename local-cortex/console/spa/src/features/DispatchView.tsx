/**
 * DispatchView — a PROJECT-LEVEL main-area view: the dispatch BOARD, now INTERACTIVE.
 *
 * The open/pending handoff queue for the project (NOT a single agent), each row a
 * waiting handoff with its summary, from→to, priority, and the rule-based proposed
 * agent. Reached via the main-area switcher (Agent · Dispatch · Analytics · Settings)
 * — it does NOT live in a column, so it never repeats the agents/metrics the 2nd
 * column owns.
 *
 * Read data: GET /dispatch/{project}/board (DispatchBoard) + GET /dispatch/{project}/
 * activity (DispatchActivity — orchestrator ring + the wave plan). The backend sorts the
 * rows urgent-first; this view is presentation over that order PLUS the write controls
 * the legacy HTML console has and the SPA was missing:
 *
 *   1. Approve & Run per proposed row — the PROPOSE-MODE human trigger. Drives the
 *      parent's useDispatchRun controller (so the streamed run survives a board
 *      refetch): it POSTs /dispatch/{p}/run, captures the run_id from the SSE `run`
 *      frame, and streams the harness reply into THIS row's inline output panel.
 *      Disabled while running; error/done surfaced.
 *   2. Propose-mode approval queue — the awaiting_approval_ids handoffs as a queue
 *      with a one-click Approve → approveHandoff. Refetch on success.
 *   3. Activity feed + wave strip — orchestrator ring buffer + the E007 per-epic wave plan.
 *
 * Writes go through the injected `client` (the `api` object satisfies it; tests pass a
 * fake). Project dispatch is intentionally read-only here; the single project-level
 * switch lives on the Dashboard. On a successful write the view calls `onChanged` (the
 * shell's board+activity refetch) — REFETCH-ON-SUCCESS, the simplest correct sync.
 * Graceful-degrade rides through everywhere — a stale-backend 404 / down store yields
 * a hint, never a crash.
 */

import { useState } from 'react'
import { GlassPanel, GlassCard, StatPill, StatusDot } from '../components/glass'
import { cx, formatRelative } from '../components/ui'
import type {
  DispatchActivity,
  DispatchActivityItem,
  DispatchBoard,
  DispatchRow,
  DispatchRunController,
  DispatchWave,
} from '../api'

/**
 * The WRITE surface the view drives. The concrete `api` object satisfies this
 * structurally (so the shell passes `api`); tests pass a fake that records calls.
 *   - setFlags        → the project autonomy kill-switch.
 *   - approveHandoff  → the propose-mode approval-queue Approve.
 * (Approve & Run is the streaming `useDispatchRun` controller, passed separately so
 * the in-flight run state lives in the parent and survives board refetches.)
 */
export interface DispatchClient {
  approveHandoff: (project: string, handoffId: string) => Promise<void>
}

interface DispatchViewProps {
  project: string | null
  board: DispatchBoard | null
  loading: boolean
  error: Error | null
  /** Orchestrator activity feed + the wave plan (GET …/activity). Null while loading. */
  activity: DispatchActivity | null
  /** The write client (the `api` object) — autonomy toggle + approve gate. */
  client: DispatchClient
  /** The Approve & Run SSE controller (owned by the parent; survives board refetch). */
  runCtl: DispatchRunController
  /** Called after any successful write — the shell refetches the board + activity. */
  onChanged: () => void
}

// Priority → a chip tone. Urgent/high read hot, the rest cool. Unknown → neutral.
const PRIORITY_CHIP: Record<string, string> = {
  urgent: 'bg-run-errored/15 text-run-errored',
  high: 'bg-run-queued/15 text-run-queued',
  medium: 'bg-mint-500/12 text-mint-300',
  low: 'bg-base-700/60 text-ink-400',
  normal: 'bg-base-700/60 text-ink-400',
}

function priorityChip(priority: string | undefined): string {
  return PRIORITY_CHIP[(priority ?? 'normal').toLowerCase()] ?? PRIORITY_CHIP.normal
}

function resolutionStatus(row: DispatchRow): string {
  return String(row.resolution?.status ?? row.resolution_status ?? '').toLowerCase()
}

function resolutionReason(row: DispatchRow): string {
  return String(row.resolution?.reason ?? row.resolution_reason ?? '').trim()
}

function resolutionLabel(row: DispatchRow): string {
  const status = resolutionStatus(row)
  if (status === 'blocked') return 'blocked'
  if (status === 'unresolved') return 'unresolved'
  return 'unassigned'
}

function resolutionChipClass(row: DispatchRow): string {
  const status = resolutionStatus(row)
  if (status === 'blocked') return 'bg-run-queued/15 text-run-queued'
  if (status === 'unresolved') return 'bg-run-errored/12 text-run-errored/90'
  return 'bg-base-700/50 text-ink-500'
}

const BTN_RUN =
  'inline-flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-semibold ' +
  'transition-colors bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/30 hover:bg-mint-500/25 ' +
  'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-mint-500/15'

/** The read-only mode banner. The single project-level switch lives on Dashboard. */
function ModeBanner({
  autonomousOn,
  proposeOn,
  cap,
  inflight,
}: {
  autonomousOn: boolean
  proposeOn: boolean
  cap: number
  inflight: number
}) {
  return (
    <GlassCard
      className={cx(
        'space-y-2 px-4 py-3',
        autonomousOn && 'border-mint-400/40 bg-mint-500/[0.06]',
      )}
    >
      <div className="flex items-start gap-3">
        <StatusDot status={autonomousOn ? 'running' : 'idle'} pulse={autonomousOn} className="mt-0.5" />
        <p className="min-w-0 flex-1 text-[11px] leading-relaxed text-ink-300">
          {autonomousOn ? (
            <>
              <b className="text-mint-200">Project dispatch ON</b> · the orchestrator auto-runs the target
              agent on each <i>new</i> handoff — <i>on your subscription</i> (max {cap} at once,{' '}
              {inflight} running). Manual <b>Approve&nbsp;&amp;&nbsp;Run</b> still works. Change this
              project-level switch from the <b>Dashboard</b>.
            </>
          ) : (
            <>
              <b className="text-ink-100">{proposeOn ? 'Propose mode' : 'Manual mode'}</b> ·{' '}
              {proposeOn
                ? 'nothing runs without your approval. '
                : 'new handoffs wait until an operator starts them. '}
              The orchestrator only <i>proposes</i> which agent takes each handoff while{' '}
              <b>Project dispatch is OFF</b>. Turn it on from the <b>Dashboard</b>.
            </>
          )}
        </p>
        <span className="shrink-0 rounded-md border border-glass-line bg-base-900/45 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-ink-400">
          dashboard control
        </span>
      </div>
    </GlassCard>
  )
}

/** The inline per-row run output panel — surfaces the streamed Approve & Run reply. */
function RunOutput({
  running,
  output,
  runId,
  error,
  done,
}: {
  running: boolean
  output: string
  runId: string | null
  error: string | null
  done: boolean
}) {
  // Nothing to show until a run starts (running / has a run id / has output or error).
  if (!running && !runId && !output && !error) return null
  return (
    <div
      className={cx(
        'mt-2 overflow-hidden rounded-lg border',
        error ? 'border-run-errored/40 bg-run-errored/[0.06]' : 'border-glass-line bg-base-900/40',
      )}
      data-run-output
    >
      <div className="flex items-center gap-2 border-b border-glass-line px-3 py-1.5">
        <StatusDot
          status={error ? 'errored' : running ? 'running' : 'completed'}
          pulse={running}
        />
        <span className="text-[10px] font-medium uppercase tracking-wide text-ink-400">
          {error ? 'Run error' : running ? 'Running…' : done ? 'Done' : 'Run'}
        </span>
        {runId && (
          <code className="ml-auto font-mono text-[10px] text-ink-500" title={`run ${runId}`}>
            run {runId.slice(0, 8)}
          </code>
        )}
      </div>
      <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap px-3 py-2 text-[11px] leading-relaxed text-ink-200">
        {error || output || (running ? '…' : '')}
      </pre>
    </div>
  )
}

/** One dispatch row — the read-only handoff card PLUS its Approve & Run action + output. */
function DispatchCard({
  row,
  runCtl,
}: {
  row: DispatchRow
  runCtl: DispatchRunController
}) {
  const proposed = row.proposed ?? null
  const reason = resolutionReason(row)
  const priority = row.priority ?? 'normal'
  const runnable = !!(proposed && proposed.name)
  const rs = runCtl.stateFor(row.id)

  function onRun() {
    if (!runnable || rs.running) return
    runCtl.run({
      agentName: proposed!.name as string,
      summary: row.summary ?? '',
      handoffId: row.id,
      handoffCompound: row.compound ?? row.id,
    })
  }

  return (
    <GlassCard className="px-4 py-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          {/* Priority + identity line. */}
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cx(
                'rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide',
                priorityChip(priority),
              )}
            >
              {priority}
            </span>
            <span className="truncate text-[11px] text-ink-400">
              <span className="text-ink-300">{row.from_agent ?? '—'}</span>
              <span className="px-1 text-ink-500">→</span>
              <span className="text-ink-300">{row.to_target ?? '—'}</span>
            </span>
            {row.created_at && (
              <span className="ml-auto shrink-0 text-[10px] text-ink-500">
                {formatRelative(row.created_at)}
              </span>
            )}
          </div>

          {/* The handoff summary. */}
          <p
            className="mt-1.5 text-sm leading-snug text-ink-100"
            title={row.summary_full ?? row.summary}
          >
            {row.summary ?? '(no summary)'}
          </p>

          {/* Footer: the compound id + the rule-based proposal. */}
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
            <span className="font-mono text-ink-500" title={row.compound ?? row.id}>
              {(row.compound ?? row.id).slice(0, 20)}
            </span>
            {proposed && proposed.name ? (
              <span className="inline-flex items-center gap-1.5 rounded-md bg-mint-500/10 px-2 py-0.5 text-mint-300">
                <span className="text-[9px] font-semibold uppercase tracking-wide text-mint-300/70">
                  proposed
                </span>
                <span className="font-medium text-mint-200">
                  {proposed.display_name ?? proposed.name}
                </span>
                {proposed.harness_label && proposed.harness_label !== '—' && (
                  <span className="text-mint-300/70">· {proposed.harness_label}</span>
                )}
                {proposed.model && <span className="text-mint-300/60">· {proposed.model}</span>}
              </span>
            ) : (
              <span
                className={cx(
                  'rounded-md px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide',
                  resolutionChipClass(row),
                )}
                title={reason || undefined}
              >
                {resolutionLabel(row)}
              </span>
            )}
          </div>

          {!runnable && reason && (
            <p className="mt-1.5 rounded-md bg-base-900/35 px-2 py-1.5 text-[11px] leading-relaxed text-ink-400">
              {reason}
            </p>
          )}
        </div>

        {/* right: Approve & Run (the ONLY thing that fires a dispatch). */}
        <button
          type="button"
          className={BTN_RUN}
          disabled={!runnable || rs.running}
          onClick={onRun}
          title={
            runnable
              ? `Spawn ${proposed!.display_name ?? proposed!.name}'s harness on this handoff`
              : reason || 'No proposed agent — nothing to run'
          }
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-3 w-3" aria-hidden="true">
            <path d="M5 3l14 9-14 9V3z" />
          </svg>
          {rs.running ? 'Running…' : 'Approve & Run'}
        </button>
      </div>

      {/* per-row streamed run output (hidden until Approve & Run streams a reply). */}
      <RunOutput
        running={rs.running}
        output={rs.output}
        runId={rs.runId}
        error={rs.error}
        done={rs.done}
      />
    </GlassCard>
  )
}

/** One parked-handoff card in the approval queue — a one-click Approve. */
function ApprovalCard({
  row,
  saving,
  onApprove,
}: {
  row: DispatchRow
  saving: boolean
  onApprove: () => void
}) {
  const proposed = row.proposed ?? null
  return (
    <GlassCard className="flex items-center gap-3 px-4 py-2.5">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-ink-100" title={row.summary_full ?? row.summary}>
          {row.summary ?? '(no summary)'}
        </p>
        <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[10px] text-ink-500">
          <code className="font-mono" title={row.compound ?? row.id}>
            {(row.compound ?? row.id).slice(0, 20)}
          </code>
          <span>·</span>
          {proposed && proposed.name ? (
            <span>
              → <b className="text-ink-300">{proposed.display_name ?? proposed.name}</b>
            </span>
          ) : (
            <span className="uppercase tracking-wide">unassigned</span>
          )}
        </div>
      </div>
      <button
        type="button"
        className={BTN_RUN}
        disabled={saving}
        onClick={onApprove}
        title="Approve — let Dispatch spawn this handoff on the next sweep"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-3 w-3" aria-hidden="true">
          <path d="M5 13l4 4L19 7" />
        </svg>
        Approve
      </button>
    </GlassCard>
  )
}

/** One activity-feed row (orchestrator ring buffer). */
function ActivityRow({ item }: { item: DispatchActivityItem }) {
  const levelTone =
    item.level === 'error'
      ? 'text-run-errored/90'
      : item.level === 'warn'
        ? 'text-run-queued'
        : item.level === 'success'
          ? 'text-mint-300'
          : 'text-ink-300'
  return (
    <li className="flex items-start gap-2 px-3 py-1.5 text-[11px]">
      <span
        className={cx(
          'shrink-0 rounded bg-base-700/60 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide',
          levelTone,
        )}
      >
        {item.kind.replace(/_/g, ' ')}
      </span>
      <span className="min-w-0 flex-1 text-ink-300">{item.text}</span>
      {item.handoff_short && (
        <code className="shrink-0 font-mono text-[10px] text-ink-500">{item.handoff_short}</code>
      )}
      {item.ago && <span className="shrink-0 text-[10px] text-ink-600">{item.ago}</span>}
    </li>
  )
}

/** The wave-plan strip (E007 Phase 1.5) — per-epic active wave + running/waiting. */
function WaveStrip({ waves }: { waves: DispatchWave[] }) {
  return (
    <div className="flex flex-wrap gap-2 px-3 py-2" role="status" aria-label="dependency wave plan">
      {waves.map((w) => {
        const done = w.active_wave === null
        return (
          <span
            key={w.epic}
            className={cx(
              'inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px]',
              done ? 'bg-base-700/50 text-ink-400' : 'bg-mint-500/12 text-mint-300',
            )}
            title={
              done
                ? `Epic ${w.epic} — all waves complete`
                : `Epic ${w.epic} — wave ${w.active_wave}: ${w.running} running, ${w.waiting} waiting`
            }
          >
            <b>{w.epic}</b>
            {done ? (
              <span>all waves complete</span>
            ) : (
              <span>
                wave <b>{w.active_wave}</b> · {w.running} running · {w.waiting} waiting
              </span>
            )}
          </span>
        )
      })}
    </div>
  )
}

/** The dispatch-activity panel — the wave strip + orchestrator ring buffer. */
function ActivityPanel({ activity }: { activity: DispatchActivity | null }) {
  const items = activity?.activity ?? []
  const waves = activity?.waves ?? []
  const loopRunning = activity?.loop_running ?? false
  const noOrch = activity?.no_orch ?? false

  return (
    <GlassCard
      className="overflow-hidden p-0"
      role="region"
      aria-label="Orchestrator dispatch activity"
    >
      <div className="flex items-center gap-2 border-b border-glass-line px-3 py-2">
        <StatusDot status={loopRunning ? 'running' : 'idle'} pulse={loopRunning} />
        <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-400">
          Orchestrator · dispatch activity
        </span>
        {activity && (
          <span className="ml-auto text-[10px] text-ink-500">
            {activity.inflight}/{activity.cap} running
          </span>
        )}
      </div>

      {waves.length > 0 && (
        <div className="border-b border-glass-line">
          <WaveStrip waves={waves} />
        </div>
      )}

      {items.length > 0 ? (
        <ul className="divide-y divide-glass-line">
          {items.map((a, i) => (
            <ActivityRow key={`${a.kind}-${i}`} item={a} />
          ))}
        </ul>
      ) : (
        <p className="px-3 py-3 text-[11px] leading-relaxed text-ink-500">
          {noOrch
            ? 'The autonomous orchestrator isn’t running in this build — nothing to show.'
            : loopRunning
              ? 'The orchestrator is watching this project’s handoff feed. Picked-up / running / completed events appear here as they happen.'
              : 'Nothing here — project dispatch is idle. Turn project dispatch on from the Dashboard to let the orchestrator auto-run agents on new handoffs.'}
        </p>
      )}
    </GlassCard>
  )
}

export function DispatchView({
  project,
  board,
  loading,
  error,
  activity,
  client,
  runCtl,
  onChanged,
}: DispatchViewProps) {
  const rows = board?.rows ?? []
  const total = board?.dispatch_count ?? rows.length
  const proposed = board?.dispatch_proposed_count ?? 0
  const unassigned = board?.dispatch_unassigned_count ?? 0
  const autonomousOn = board?.autonomous_on ?? false
  const proposeOn = board?.propose_mode_on ?? false
  const awaitingIds = new Set(board?.awaiting_approval_ids ?? [])
  const awaiting = awaitingIds.size

  // Approval-queue write state (per-row in-flight + a shared error line).
  const [approving, setApproving] = useState<string | null>(null)
  const [approveError, setApproveError] = useState<string | null>(null)

  async function approve(handoffId: string) {
    if (!project) return
    setApproving(handoffId)
    setApproveError(null)
    try {
      await client.approveHandoff(project, handoffId)
      onChanged()
    } catch (e) {
      setApproveError(e instanceof Error ? e.message : String(e))
    } finally {
      setApproving(null)
    }
  }

  const awaitingRows = rows.filter((r) => awaitingIds.has(r.id))

  return (
    <GlassPanel className="min-w-0 flex-1">
      {/* Header — board counts + the project autonomy/propose-mode flags. */}
      <header className="border-b border-glass-line px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-base font-semibold text-ink-100">Dispatch board</h1>
          <div className="flex shrink-0 items-center gap-2">
            <span
              className={cx(
                'inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] font-medium uppercase tracking-wide',
                autonomousOn ? 'bg-mint-500/15 text-mint-300' : 'bg-base-700/50 text-ink-500',
              )}
              title="Project dispatch for this project"
            >
              <StatusDot status={autonomousOn ? 'running' : 'idle'} pulse={autonomousOn} />
              project dispatch {autonomousOn ? 'on' : 'off'}
            </span>
            <span
              className={cx(
                'inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] font-medium uppercase tracking-wide',
                proposeOn ? 'bg-run-queued/15 text-run-queued' : 'bg-base-700/50 text-ink-500',
              )}
              title="Propose-mode (training-wheels approval gate)"
            >
              propose {proposeOn ? 'on' : 'off'}
            </span>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <StatPill label="Waiting" value={total} tone={total > 0 ? 'mint' : 'muted'} />
          <StatPill label="Proposed" value={proposed} tone={proposed > 0 ? 'default' : 'muted'} />
          <StatPill
            label="Unassigned"
            value={unassigned}
            tone={unassigned > 0 ? 'default' : 'muted'}
            title="Open handoffs with no rule-based proposal"
          />
          <StatPill
            label="Awaiting"
            value={awaiting}
            tone={awaiting > 0 ? 'default' : 'muted'}
            title="Handoffs parked for approval (propose-mode)"
          />
        </div>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {loading && !board && (
          <p className="px-1 py-2 text-xs text-ink-500">Loading the dispatch board…</p>
        )}

        {error && !board && (
          <p className="px-1 py-2 text-xs leading-relaxed text-run-errored/80">
            Couldn’t load the dispatch board for{' '}
            <span className="font-mono">{project}</span>. The backend{' '}
            <code className="font-mono">/dispatch/{'{project}'}/board</code> route may
            not be live in this build.
          </p>
        )}

        {/* The autonomy mode banner + master kill-switch (only with a loaded board). */}
        {board && (
          <ModeBanner
            autonomousOn={autonomousOn}
            proposeOn={proposeOn}
            cap={activity?.cap ?? 3}
            inflight={activity?.inflight ?? 0}
          />
        )}

        {/* Propose-mode approval queue (only when something is awaiting). */}
        {awaitingRows.length > 0 && (
          <section
            className="space-y-2 rounded-xl border border-run-queued/30 bg-run-queued/[0.04] p-3"
            role="region"
            aria-label="Handoffs awaiting approval"
          >
            <div className="flex items-center gap-2 px-1 text-[11px] font-medium text-run-queued">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-3.5 w-3.5" aria-hidden="true">
                <path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
              </svg>
              <span>
                <b>{awaitingRows.length}</b> handoff{awaitingRows.length === 1 ? '' : 's'} awaiting
                your approval
              </span>
              <span className="text-ink-500">· propose-mode gate is ON</span>
            </div>
            {approveError && (
              <p className="rounded-md bg-run-errored/12 px-3 py-2 text-[11px] leading-relaxed text-run-errored/90">
                Couldn’t approve — {approveError}
              </p>
            )}
            <div className="space-y-2">
              {awaitingRows.map((r) => (
                <ApprovalCard
                  key={r.id}
                  row={r}
                  saving={approving === r.id}
                  onApprove={() => approve(r.id)}
                />
              ))}
            </div>
          </section>
        )}

        {/* The queue. */}
        {board && rows.length === 0 && (
          <div className="flex items-center justify-center p-10">
            <p className="text-sm text-ink-500">No open handoffs waiting for dispatch.</p>
          </div>
        )}

        {rows.length > 0 && (
          <div className="space-y-2">
            {rows.map((row) => (
              <DispatchCard key={row.id} row={row} runCtl={runCtl} />
            ))}
          </div>
        )}

        {/* Orchestrator activity feed + the wave-plan strip. */}
        {board && <ActivityPanel activity={activity} />}
      </div>
    </GlassPanel>
  )
}
