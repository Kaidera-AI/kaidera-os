/**
 * DashboardView — the project-level LANDING surface (task #97; "we lost the dashboard").
 *
 * When a project is selected (and no agent is picked), the main area shows THIS: a
 * project overview / vitals at-a-glance, restoring what the legacy console had
 * (`_project_detail.html` + the `_fleet_*` cards) and the SPA rebuild dropped. It is
 * the FIRST main-area tab + the default view on project-select.
 *
 * It COMPOSES from EXISTING endpoints (no new backend) — the same JSON the other tabs
 * already read:
 *   - the project HEADER (display name · status · repo_root) from the project
 *     registry row the shell already holds (GET /projects).
 *   - the VITALS stat cards (agents · pending handoffs · active/pending tasks ·
 *     events/24h · est. tokens) from GET /agents/{p}/epics (metrics block) +
 *     GET /analytics/{p}/kpis + GET /dispatch/{p}/board.
 *   - the ACTIVE-EPIC widget (per-epic progress + per-increment mini-bars, or the
 *     'continuous · no epics' line) from the epics payload.
 *   - a recent-ACTIVITY strip from GET /history/{p} (the summarised cross-agent feed).
 *   - the Cortex HEALTH pill from the console JSON GET /cortex/health.
 *
 * Graceful-degrade is the house law: a counter whose source read failed renders '—'
 * (never a fabricated 0); the header still renders from the project row even if every
 * live read is down; loading / empty states throughout. Glass-morphism.
 *
 * (Notes for #102: the project-level Explain — a full-system explanation — lands ON /
 * NEAR this Dashboard, since this is the project's landing/overview surface.)
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { GlassPanel, GlassCard, StatusDot } from '../components/glass'
import { cx, statusKind } from '../components/ui'
import type {
  AgentEpicsPayload,
  AnalyticsKpis,
  AutomationDeleteResult,
  AutomationFeedersExportResult,
  AutomationFeedersImportPayload,
  AutomationFeedersImportResult,
  DispatchBoard,
  FlagsPatch,
  FlagsWriteResult,
  EpicView,
  HistoryEvent,
  HistoryPayload,
  PlanningBeatStatus,
  PlanningBeatWritePayload,
  Project,
  RunBoard,
  RunRow,
  RunSegment,
  RunTranscript,
  ScheduledJob,
  ScheduledJobsResult,
  ScheduledJobRunNowResult,
  ScheduledJobWritePayload,
  ScheduledJobWriteResult,
} from '../api'

/** A gentle poll cadence for the overview (matches the SPA's snapshot-catalog refresh —
 * the live transcript is pushed by SSE elsewhere; this surface is a periodic snapshot). */
const DASHBOARD_POLL_MS = 12000
/** How many recent-activity rows the strip shows (the deep timeline lives in the History tab). */
const ACTIVITY_LIMIT = 8
/** How many recent/active run transcripts the project worker feed hydrates. */
const WORKER_FEED_RUN_LIMIT = 6

/**
 * The slice of the api client the Dashboard reads — so tests fake ONE object. Every
 * method already exists on the concrete `api` (the Dashboard adds no new endpoint), so
 * the shell passes `api` directly and it satisfies this structurally.
 */
export interface DashboardClient {
  agentEpics: (project: string, signal?: AbortSignal) => Promise<AgentEpicsPayload>
  analyticsKpis: (project: string, signal?: AbortSignal) => Promise<AnalyticsKpis>
  dispatchBoard: (project: string, signal?: AbortSignal) => Promise<DispatchBoard>
  setFlags?: (project: string, patch: FlagsPatch) => Promise<FlagsWriteResult>
  history: (project: string, limit?: number, signal?: AbortSignal) => Promise<HistoryPayload>
  runBoard: (project: string, signal?: AbortSignal) => Promise<RunBoard>
  run: (runId: string, signal?: AbortSignal) => Promise<RunTranscript>
  scheduledJobs?: (project: string, signal?: AbortSignal) => Promise<ScheduledJobsResult>
  saveScheduledJob?: (
    project: string,
    body: ScheduledJobWritePayload,
    signal?: AbortSignal,
  ) => Promise<ScheduledJobWriteResult>
  runScheduledJobNow?: (
    project: string,
    jobId: string,
    signal?: AbortSignal,
  ) => Promise<ScheduledJobRunNowResult>
  deleteScheduledJob?: (
    project: string,
    jobId: string,
    signal?: AbortSignal,
  ) => Promise<AutomationDeleteResult>
  planningBeat?: (project: string, signal?: AbortSignal) => Promise<PlanningBeatStatus>
  savePlanningBeat?: (
    project: string,
    body: PlanningBeatWritePayload,
    signal?: AbortSignal,
  ) => Promise<ScheduledJobWriteResult>
  exportAutomationFeeders?: (
    project: string,
    signal?: AbortSignal,
  ) => Promise<AutomationFeedersExportResult>
  importAutomationFeeders?: (
    project: string,
    body: AutomationFeedersImportPayload,
    signal?: AbortSignal,
  ) => Promise<AutomationFeedersImportResult>
}

interface DashboardViewProps {
  project: string | null
  /** The selected project's registry row (display name / repo_root / status / agent_count).
   * Reused from the shell's already-fetched /projects list — no extra call. */
  projectRow: Project | null
  client: DashboardClient
  /** Opens Explain pre-seeded to the project-level/full-system target (#102). */
  onExplainProject?: () => void
  /** Override the poll cadence (tests pass 0 to disable the interval). */
  pollMs?: number
}

/** The composed overview payload (each piece graceful-degrades to null on its own read fail). */
interface Overview {
  epics: AgentEpicsPayload | null
  kpis: AnalyticsKpis | null
  board: DispatchBoard | null
  history: HistoryPayload | null
  runBoard: RunBoard | null
}

const EMPTY_OVERVIEW: Overview = {
  epics: null,
  kpis: null,
  board: null,
  history: null,
  runBoard: null,
}

/** Format a counter that may be null/absent — '—' when unknown (NEVER a fabricated 0). */
function num(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : String(v)
}

export function DashboardView({
  project,
  projectRow,
  client,
  onExplainProject,
  pollMs = DASHBOARD_POLL_MS,
}: DashboardViewProps) {
  const [data, setData] = useState<Overview>(EMPTY_OVERVIEW)
  // `loaded` flips true after the FIRST settled fetch cycle (so the loading hint clears even
  // when some sources degrade). The header/vitals render from the project row regardless.
  const [loaded, setLoaded] = useState(false)
  const [savingFlag, setSavingFlag] = useState<keyof FlagsPatch | null>(null)
  const [flagError, setFlagError] = useState<string | null>(null)

  // Reset on a project switch via the React "adjust state during render" pattern (the same one
  // MainArea / HistoryView use) — no synchronous setState inside an effect.
  const [viewProject, setViewProject] = useState<string | null>(project)
  if (project !== viewProject) {
    setViewProject(project)
    setData(EMPTY_OVERVIEW)
    setLoaded(false)
  }

  // Keep the latest project + client in refs so the fetch closure stays STABLE (empty-deps
  // useCallback) — mirrors HistoryView / useResource, which keeps the hooks lint happy.
  const projectRef = useRef(project)
  const clientRef = useRef(client)
  useEffect(() => {
    projectRef.current = project
    clientRef.current = client
  })

  // The single composing fetch. Each source is awaited INDEPENDENTLY (Promise.allSettled) so
  // one down read degrades only its own slice — the rest of the overview still lands. The
  // caller owns the AbortSignal (a project switch / poll tick cancels the prior in-flight set).
  const run = useCallback((signal: AbortSignal): Promise<void> => {
    const proj = projectRef.current
    const c = clientRef.current
    if (!proj) return Promise.resolve()
    return Promise.allSettled([
      c.agentEpics(proj, signal),
      c.analyticsKpis(proj, signal),
      c.dispatchBoard(proj, signal),
      c.history(proj, ACTIVITY_LIMIT, signal),
      c.runBoard(proj, signal),
    ]).then((results) => {
      if (signal.aborted) return
      const [epics, kpis, board, history, runBoard] = results
      setData({
        epics: epics.status === 'fulfilled' ? epics.value : null,
        kpis: kpis.status === 'fulfilled' ? kpis.value : null,
        board: board.status === 'fulfilled' ? board.value : null,
        history: history.status === 'fulfilled' ? history.value : null,
        runBoard: runBoard.status === 'fulfilled' ? runBoard.value : null,
      })
      setLoaded(true)
    })
  }, [])

  // (Re)load on project change + a gentle poll. The synchronous resets happen in render
  // (above); this effect kicks the async fetch via the stable `run` callback, arms the
  // interval, and aborts any in-flight fetch on unmount / project change.
  useEffect(() => {
    if (!project) return
    const controllers = new Set<AbortController>()
    const start = () => {
      const ac = new AbortController()
      controllers.add(ac)
      run(ac.signal).finally(() => controllers.delete(ac))
    }
    start()
    let timer: ReturnType<typeof setInterval> | null = null
    if (pollMs && pollMs > 0) {
      timer = setInterval(start, pollMs)
    }
    return () => {
      controllers.forEach((ac) => ac.abort())
      if (timer) clearInterval(timer)
    }
  }, [project, run, pollMs])

  if (!project) {
    return (
      <GlassPanel className="flex-1">
        <div className="flex h-full items-center justify-center p-10">
          <p className="text-sm text-ink-500">Select a project to see its dashboard.</p>
        </div>
      </GlassPanel>
    )
  }

  const name = (projectRow?.display_name as string | null) || project
  const status = (projectRow?.status as string | null) || null
  const repoRoot = (projectRow?.repo_root as string | null) || null
  const agentCount = (projectRow?.agent_count as number | null) ?? null

  const metrics = data.epics?.metrics ?? null
  const board = data.board ?? null
  const kpis = data.kpis ?? null

  // pending handoffs: prefer the metrics block, fall back to the dispatch board's open count.
  const pendingHandoffs = metrics?.pending_handoffs ?? board?.dispatch_count ?? null
  // events/24h: the metrics block, else the KPI strip (both read Cortex /state).
  const events24h = metrics?.events_24h ?? kpis?.events_24h ?? null
  const activeTasks = metrics?.active_tasks ?? kpis?.active_tasks ?? null
  const pendingTasks = metrics?.pending_tasks ?? null
  // est. recent tokens — from the KPI strip (the App-DB project rollup).
  const tokens = kpis?.tokens_recent_h ?? (kpis?.tokens_recent ? String(kpis.tokens_recent) : null)

  async function toggleProjectFlag(patch: FlagsPatch) {
    if (!project || !client.setFlags) return
    const key = Object.keys(patch)[0] as keyof FlagsPatch | undefined
    setSavingFlag(key ?? null)
    setFlagError(null)
    try {
      const next = await client.setFlags(project, patch)
      setData((prev) => ({
        ...prev,
        board: prev.board
          ? {
              ...prev.board,
              autonomous_on: next.autonomous,
              propose_mode_on: next.propose_mode,
            }
          : prev.board,
      }))
    } catch (e) {
      setFlagError(e instanceof Error ? e.message : String(e))
    } finally {
      setSavingFlag(null)
    }
  }

  return (
    <GlassPanel className="min-w-0 flex-1">
      <div className="flex h-full min-h-0 flex-col">
        {/* ---------- HEADER: name · status · repo_root + health pill ---------- */}
        <header
          data-testid="dashboard-header"
          className="shrink-0 border-b border-glass-line px-5 py-4"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-baseline gap-2">
                <h1 className="truncate text-lg font-semibold text-ink-100">{name}</h1>
                {status && (
                  <span className="shrink-0 rounded bg-base-700/60 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-ink-400">
                    {status}
                  </span>
                )}
              </div>
              <div className="mt-1 flex items-center gap-2 text-[11px] text-ink-500">
                <code className="font-mono text-ink-400">{project}</code>
                {repoRoot && (
                  <>
                    <span aria-hidden="true">·</span>
                    <span className="truncate font-mono" title="canonical working folder (repo_root)">
                      {repoRoot}
                    </span>
                  </>
                )}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {onExplainProject && (
                <button
                  type="button"
                  onClick={onExplainProject}
                  className="glass-soft rounded-lg px-3 py-1.5 text-[11px] font-semibold text-mint-200 transition-colors hover:border-mint-400/30 hover:bg-mint-500/10"
                >
                  Explain project
                </button>
              )}
              <CortexHealthPill project={project} />
            </div>
          </div>

          <ProjectAutonomyControls
            board={board}
            canWrite={!!client.setFlags}
            saving={savingFlag}
            error={flagError}
            onToggle={toggleProjectFlag}
          />

          {/* ---------- VITALS stat cards ---------- */}
          <div
            data-testid="dashboard-vitals"
            className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6"
          >
            <StatCard label="Workers" value={num(agentCount)} tone="default" />
            <StatCard label="Pending handoffs" value={num(pendingHandoffs)} tone={(pendingHandoffs ?? 0) > 0 ? 'mint' : 'muted'} />
            <StatCard label="Active tasks" value={num(activeTasks)} tone={(activeTasks ?? 0) > 0 ? 'default' : 'muted'} />
            <StatCard label="Pending tasks" value={num(pendingTasks)} tone="muted" />
            <StatCard label="Events · 24h" value={num(events24h)} tone="muted" />
            <StatCard label="Tokens · recent" value={tokens ?? '—'} tone={tokens ? 'mint' : 'muted'} />
          </div>
        </header>

        {/* ---------- BODY: active-epic widget + recent activity ---------- */}
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {!loaded && (
            <p data-testid="dashboard-loading" className="px-1 py-2 text-xs text-ink-500">
              Loading the project overview…
            </p>
          )}

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_22rem]">
            {/* ===== ACTIVE EPIC ===== */}
            <ActiveEpicWidget epics={data.epics} />

            {/* ===== RECENT ACTIVITY ===== */}
            <RecentActivityStrip history={data.history} />

            {/* ===== PROJECT AUTOMATION FEEDERS ===== */}
            <AutomationFeedersCard project={project} client={client} />

            {/* ===== CONTINUOUS WORKER FEED ===== */}
            <ProjectWorkerFeed runBoard={data.runBoard} client={client} />
          </div>
        </div>
      </div>
    </GlassPanel>
  )
}

// ---------------------------------------------------------------------------
//  small presentational bits
// ---------------------------------------------------------------------------

/** Project-level autonomy policy. The global engine toggle lives in Settings -> System;
 * this project-scoped strip belongs on the project landing dashboard. */
function ProjectAutonomyControls({
  board,
  canWrite,
  saving,
  error,
  onToggle,
}: {
  board: DispatchBoard | null
  canWrite: boolean
  saving: keyof FlagsPatch | null
  error: string | null
  onToggle: (patch: FlagsPatch) => void
}) {
  const autonomousOn = board?.autonomous_on ?? false
  const proposeOn = board?.propose_mode_on ?? false
  const disabled = !board || !canWrite || saving !== null

  return (
    <section
      data-testid="dashboard-autonomy"
      className="mt-3 rounded-xl border border-glass-line bg-base-950/35 p-3"
      aria-label="Project autonomy controls"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
            Project autonomy
          </div>
          <p className="mt-1 max-w-2xl text-[11px] leading-relaxed text-ink-500">
            These controls apply only to this project. The global autonomy engine is controlled
            from <span className="text-ink-300">Settings {'->'} System</span>.
          </p>
        </div>
        {error && (
          <p className="max-w-md rounded-md bg-run-errored/12 px-3 py-2 text-[11px] leading-relaxed text-run-errored/90">
            Couldn’t save — {error}
          </p>
        )}
      </div>

      <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">
        <ProjectFlagCard
          label="Project dispatch"
          hint="Let Kaidera OS auto-run resolved handoffs for this project."
          on={autonomousOn}
          saving={saving === 'autonomous'}
          disabled={disabled}
          onToggle={(next) => onToggle({ autonomous: next })}
        />
        <ProjectFlagCard
          label="Propose mode"
          hint="Park proposed runs for approval before launch."
          on={proposeOn}
          saving={saving === 'propose_mode'}
          disabled={disabled}
          onToggle={(next) => onToggle({ propose_mode: next })}
        />
      </div>
    </section>
  )
}

function ProjectFlagCard({
  label,
  hint,
  on,
  saving,
  disabled,
  onToggle,
}: {
  label: string
  hint: string
  on: boolean
  saving: boolean
  disabled: boolean
  onToggle: (next: boolean) => void
}) {
  return (
    <div className="glass-soft flex items-center gap-3 rounded-lg px-3 py-2.5">
      <StatusDot status={on ? 'running' : 'idle'} pulse={on} />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-ink-100">{label}</div>
        <div className="truncate text-[11px] text-ink-500">{hint}</div>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={label}
        disabled={disabled}
        onClick={() => onToggle(!on)}
        className={cx(
          'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors',
          'disabled:cursor-not-allowed disabled:opacity-50',
          on ? 'bg-mint-500/70' : 'bg-base-700/70',
        )}
      >
        <span
          className={cx(
            'inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform',
            on ? 'translate-x-[18px]' : 'translate-x-[3px]',
          )}
        />
      </button>
      {saving && <span className="text-[10px] text-ink-500">saving…</span>}
    </div>
  )
}

type StatTone = 'default' | 'mint' | 'muted'
const STAT_TONE: Record<StatTone, string> = {
  default: 'text-ink-100',
  mint: 'text-mint-300',
  muted: 'text-ink-400',
}

/** One vitals stat card — a big value over a label. A '—' value is the honest "unknown". */
function StatCard({ label, value, tone }: { label: string; value: string; tone: StatTone }) {
  return (
    <div
      data-stat
      className="glass-soft flex min-w-0 flex-col items-start gap-0.5 rounded-lg px-3 py-2.5"
    >
      <span className={cx('text-xl font-semibold leading-none tabular-nums', STAT_TONE[tone])}>
        {value}
      </span>
      <span className="text-[10px] font-medium uppercase tracking-wider text-ink-500">{label}</span>
    </div>
  )
}

/** The Active-Epic widget — the headline active epic's progress + per-increment mini-bars,
 * or the 'continuous · no epics' line. Reuses the epics payload the col-2 widget reads. */
function ActiveEpicWidget({ epics }: { epics: AgentEpicsPayload | null }) {
  const section = epics?.epic ?? null
  const list = section?.epics ?? []
  const mode = section?.mode ?? 'continuous'

  return (
    <GlassCard data-testid="dashboard-epic" className="overflow-hidden p-0">
      <div className="flex items-center gap-2 border-b border-glass-line px-4 py-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
          Active epic
        </h2>
        {mode === 'epics' && section && (
          <span className="ml-auto rounded-full bg-base-800/70 px-1.5 py-0.5 text-[10px] tabular-nums text-ink-400">
            {section.epic_count} epic{section.epic_count === 1 ? '' : 's'}
          </span>
        )}
      </div>

      {mode !== 'epics' || list.length === 0 ? (
        <div className="px-4 py-5 text-center">
          <p className="text-sm font-medium text-ink-300">
            {section?.label || 'continuous · no epics'}
          </p>
          <p className="mt-1 text-[11px] text-ink-500">
            This project runs a continuous backlog — there’s no active epic to track right now.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-glass-line">
          {list.map((e) => (
            <EpicRow key={e.epic_id} epic={e} />
          ))}
        </div>
      )}
    </GlassCard>
  )
}

/** One epic row: id · title · overall % + a compact per-increment bar (done/prog/todo segments). */
function EpicRow({ epic }: { epic: EpicView }) {
  return (
    <div className="px-4 py-3">
      <div className="flex items-baseline gap-2">
        <span className="shrink-0 font-mono text-[12px] font-semibold text-mint-300">
          {epic.epic_id}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm text-ink-100" title={epic.title}>
          {epic.title || epic.epic_id}
        </span>
        <span className="shrink-0 text-[11px] font-semibold tabular-nums text-ink-300">
          {epic.overall_pct}%
        </span>
      </div>
      {/* the overall progress track */}
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-base-700/60">
        <div
          className="h-full rounded-full bg-mint-400/70"
          style={{ width: `${Math.max(2, Math.min(100, epic.overall_pct))}%` }}
        />
      </div>
      {/* the per-increment mini-segments */}
      {epic.increments.length > 0 && (
        <div className="mt-2 flex gap-1" aria-label={`${epic.increment_count} increments`}>
          {epic.increments.map((inc, i) => (
            <span
              key={inc.num ?? i}
              data-testid="epic-increment"
              title={`${inc.label}: ${inc.title} — ${inc.pct}%`}
              className={cx(
                'h-1.5 flex-1 rounded-full',
                inc.kind === 'done'
                  ? 'bg-run-completed'
                  : inc.kind === 'prog'
                    ? 'bg-mint-400/70'
                    : 'bg-base-700/70',
              )}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** The recent-activity strip — a short cross-agent feed from /history (the deep timeline is the
 * History tab). Each row: a kind dot · agent · summary · relative age. */
function RecentActivityStrip({ history }: { history: HistoryPayload | null }) {
  const events = history?.events ?? []
  return (
    <GlassCard data-testid="dashboard-activity" className="overflow-hidden p-0">
      <div className="flex items-center gap-2 border-b border-glass-line px-4 py-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
          Recent activity
        </h2>
        <span
          className="ml-auto inline-flex items-center gap-1.5 text-[10px] text-ink-500"
          title="Live, polled from Cortex /history"
        >
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-mint-400" /> live
        </span>
      </div>
      {events.length === 0 ? (
        <p className="px-4 py-5 text-center text-[11px] leading-relaxed text-ink-500">
          No recent activity in the live <code className="text-ink-400">/history</code> window yet.
          It appears here as agents log decisions and run tools.
        </p>
      ) : (
        <ol className="divide-y divide-glass-line">
          {events.map((ev, i) => (
            <ActivityRow key={`${ev.ts}-${i}`} ev={ev} />
          ))}
        </ol>
      )}
    </GlassCard>
  )
}

/** One recent-activity row. */
function ActivityRow({ ev }: { ev: HistoryEvent }) {
  return (
    <li data-seg-kind={ev.kind} className="flex items-start gap-2.5 px-4 py-2">
      <span
        className={cx('mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full', kindDotClass(ev.kind))}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="shrink-0 text-[12px] font-semibold text-ink-200">{ev.agent}</span>
          {ev.ts_ago && (
            <span className="ml-auto shrink-0 text-[10px] tabular-nums text-ink-600" title={ev.ts}>
              {ev.ts_ago}
            </span>
          )}
        </div>
        <p className="mt-0.5 break-words text-[12px] leading-snug text-ink-300">{ev.summary}</p>
      </div>
    </li>
  )
}

/** The kind → dot-colour mapping (say = a message, tool = an action, think = a reasoning step). */
function kindDotClass(kind: string): string {
  if (kind === 'tool') return 'bg-run-queued'
  if (kind === 'think') return 'bg-mint-400'
  return 'bg-run-completed'
}

// ---------------------------------------------------------------------------
//  Automation feeders — project-owned schedules.
//  These emit ordinary Cortex handoffs; the Dispatch loop still owns execution.
// ---------------------------------------------------------------------------

interface AutomationState {
  jobs: ScheduledJob[]
  connected: boolean
  loading: boolean
  error: string | null
  notice: string | null
}

const EMPTY_AUTOMATION: AutomationState = {
  jobs: [],
  connected: false,
  loading: false,
  error: null,
  notice: null,
}

const inputClass =
  'w-full rounded-lg border border-glass-line bg-base-950/55 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-600 outline-none transition-colors focus:border-mint-400/40'

function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

function dtLabel(value: string | null | undefined): string {
  if (!value) return 'not scheduled'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString()
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function scheduleSummary(job: ScheduledJob): string {
  const schedule = job.schedule || {}
  const kind = asText(schedule.kind || 'once')
  if (kind === 'interval') {
    const seconds = Number(schedule.every_seconds || 0)
    if (seconds >= 3600 && seconds % 3600 === 0) return `every ${seconds / 3600}h`
    if (seconds >= 60 && seconds % 60 === 0) return `every ${seconds / 60}m`
    return `every ${seconds || '?'}s`
  }
  if (kind === 'daily') {
    return `daily at ${asText(schedule.time || '??:??')} ${asText(schedule.timezone || 'UTC')}`
  }
  return kind
}

function jobTarget(job: ScheduledJob): string {
  const payload = job.payload || {}
  const from = asText(payload.from_agent || 'unknown')
  const target = asText(payload.to_agent || payload.to_role || 'target')
  return `${from} -> ${target}`
}

function AutomationFeedersCard({
  project,
  client,
}: {
  project: string
  client: DashboardClient
}) {
  const [state, setState] = useState<AutomationState>(EMPTY_AUTOMATION)
  const [jobName, setJobName] = useState('Lead planning heartbeat')
  const [jobKind, setJobKind] = useState<'interval' | 'daily'>('interval')
  const [jobEveryMinutes, setJobEveryMinutes] = useState('60')
  const [jobDailyTime, setJobDailyTime] = useState('09:00')
  const [jobTimezone, setJobTimezone] = useState(localTimezone())
  const [jobFromAgent, setJobFromAgent] = useState('')
  const [jobToRole, setJobToRole] = useState('lead')
  const [jobSummary, setJobSummary] = useState(
    'Review scheduled project work, update the plan, and create handoffs for the team.',
  )
  const [action, setAction] = useState<string | null>(null)
  const [editingJobId, setEditingJobId] = useState<string | null>(null)
  const [importText, setImportText] = useState('')

  const canRead = !!client.scheduledJobs
  const canWrite = !!client.saveScheduledJob
  const canMigrate = !!(client.exportAutomationFeeders && client.importAutomationFeeders)
  const planningJob = state.jobs.find((job) => job.id === 'pm-planning-beat')

  const loadAutomation = useCallback(
    async (signal?: AbortSignal) => {
      if (!canRead || !client.scheduledJobs) {
        setState({
          ...EMPTY_AUTOMATION,
          error: 'Automation feeder APIs are not available in this build.',
        })
        return
      }
      setState((prev) => ({ ...prev, loading: true, error: null }))
      try {
        const jobs = await client.scheduledJobs(project, signal)
        if (signal?.aborted) return
        setState((prev) => ({
          jobs: jobs.jobs ?? [],
          connected: Boolean(jobs.connected),
          loading: false,
          error: null,
          notice: prev.notice,
        }))
      } catch {
        if (signal?.aborted) return
        setState((prev) => ({
          ...prev,
          loading: false,
          error: 'Automation schedules could not be loaded.',
        }))
      }
    },
    [canRead, client, project],
  )

  useEffect(() => {
    const ctrl = new AbortController()
    void loadAutomation(ctrl.signal)
    return () => ctrl.abort()
  }, [loadAutomation])

  async function saveJob(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!client.saveScheduledJob) return
    const fromAgent = jobFromAgent.trim().toLowerCase()
    const summary = jobSummary.trim()
    if (!fromAgent || !summary) {
      setState((prev) => ({ ...prev, error: 'Scheduled jobs require from agent and summary.' }))
      return
    }
    const minutes = Math.max(1, Number.parseInt(jobEveryMinutes || '60', 10) || 60)
    const schedule =
      jobKind === 'daily'
        ? { kind: 'daily', time: jobDailyTime || '09:00', timezone: jobTimezone || 'UTC' }
        : { kind: 'interval', every_seconds: minutes * 60 }
    setAction('save-job')
    setState((prev) => ({ ...prev, error: null, notice: null }))
    try {
      const result = await client.saveScheduledJob(project, {
        id: editingJobId ?? undefined,
        name: jobName.trim() || summary.slice(0, 48),
        enabled: true,
        schedule,
        payload: {
          from_agent: fromAgent,
          to_role: jobToRole.trim().toLowerCase() || 'lead',
          priority: 'medium',
          summary,
          context: 'Created from Kaidera OS Dashboard automation feeders.',
        },
      })
      if (!result.ok) throw new Error(result.error || 'scheduled job could not be saved')
      setState((prev) => ({ ...prev, notice: `Saved schedule ${result.job?.name || jobName}.` }))
      setEditingJobId(null)
      await loadAutomation()
    } catch (err) {
      setState((prev) => ({ ...prev, error: err instanceof Error ? err.message : String(err) }))
    } finally {
      setAction(null)
    }
  }

  function editJob(job: ScheduledJob) {
    const schedule = job.schedule || {}
    const payload = job.payload || {}
    setEditingJobId(job.id)
    setJobName(job.name || job.id)
    setJobKind(asText(schedule.kind || 'interval') === 'daily' ? 'daily' : 'interval')
    const seconds = Number(schedule.every_seconds || 3600)
    setJobEveryMinutes(String(Math.max(1, Math.round(seconds / 60))))
    setJobDailyTime(asText(schedule.time || '09:00'))
    setJobTimezone(asText(schedule.timezone || localTimezone()))
    setJobFromAgent(asText(payload.from_agent || ''))
    setJobToRole(asText(payload.to_role || 'lead'))
    setJobSummary(asText(payload.summary || ''))
    setState((prev) => ({ ...prev, notice: `Editing schedule ${job.name}.`, error: null }))
  }

  async function deleteJob(job: ScheduledJob) {
    if (!client.deleteScheduledJob) return
    setAction(`delete-job:${job.id}`)
    setState((prev) => ({ ...prev, error: null, notice: null }))
    try {
      const result = await client.deleteScheduledJob(project, job.id)
      if (!result.ok) throw new Error(result.error || 'scheduled job could not be deleted')
      setState((prev) => ({ ...prev, notice: `Deleted schedule ${job.name}.` }))
      if (editingJobId === job.id) setEditingJobId(null)
      await loadAutomation()
    } catch (err) {
      setState((prev) => ({ ...prev, error: err instanceof Error ? err.message : String(err) }))
    } finally {
      setAction(null)
    }
  }

  async function markJobDue(job: ScheduledJob) {
    if (!client.runScheduledJobNow) return
    setAction(`job:${job.id}`)
    setState((prev) => ({ ...prev, error: null, notice: null }))
    try {
      const result = await client.runScheduledJobNow(project, job.id)
      if (!result.ok) throw new Error(result.error || 'scheduled job could not be marked due')
      setState((prev) => ({
        ...prev,
        notice: `${job.name} is due now; the next orchestrator sweep will emit the handoff.`,
      }))
      await loadAutomation()
    } catch (err) {
      setState((prev) => ({ ...prev, error: err instanceof Error ? err.message : String(err) }))
    } finally {
      setAction(null)
    }
  }

  async function savePlanningBeat() {
    if (!client.savePlanningBeat) return
    setAction('save-planning-beat')
    setState((prev) => ({ ...prev, error: null, notice: null }))
    try {
      const result = await client.savePlanningBeat(project, {
        enabled: true,
        every_minutes: 240,
      })
      if (!result.ok) throw new Error(result.error || 'PM planning beat could not be saved')
      setState((prev) => ({
        ...prev,
        notice: `Saved PM planning beat ${result.job?.name || 'PM planning beat'}.`,
      }))
      await loadAutomation()
    } catch (err) {
      setState((prev) => ({ ...prev, error: err instanceof Error ? err.message : String(err) }))
    } finally {
      setAction(null)
    }
  }

  async function exportFeeders() {
    setAction('export-feeders')
    setState((prev) => ({ ...prev, error: null, notice: null }))
    try {
      const payload = client.exportAutomationFeeders
        ? await client.exportAutomationFeeders(project)
        : {
            project,
            version: 1,
            connected: state.connected,
            scheduled_jobs: state.jobs,
          }
      setImportText(JSON.stringify(payload, null, 2))
      setState((prev) => ({ ...prev, notice: 'Automation feeder definitions exported below.' }))
    } catch (err) {
      setState((prev) => ({ ...prev, error: err instanceof Error ? err.message : String(err) }))
    } finally {
      setAction(null)
    }
  }

  async function importFeeders() {
    if (!client.importAutomationFeeders) return
    let parsed: unknown
    try {
      parsed = JSON.parse(importText)
    } catch {
      setState((prev) => ({ ...prev, error: 'Import JSON is invalid.' }))
      return
    }
    setAction('import-feeders')
    setState((prev) => ({ ...prev, error: null, notice: null }))
    try {
      const result = await client.importAutomationFeeders(project, parsed as AutomationFeedersImportPayload)
      if (!result.ok) {
        const first = result.errors?.[0]?.error || result.error || 'automation feeder import failed'
        throw new Error(first)
      }
      setState((prev) => ({
        ...prev,
        notice: `Imported ${result.imported.scheduled_jobs} schedules.`,
      }))
      await loadAutomation()
    } catch (err) {
      setState((prev) => ({ ...prev, error: err instanceof Error ? err.message : String(err) }))
    } finally {
      setAction(null)
    }
  }

  return (
    <GlassCard data-testid="dashboard-automation" className="overflow-hidden p-0 lg:col-span-2">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-glass-line px-4 py-3">
        <div>
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
            Automation feeders
          </h2>
          <p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-ink-500">
            Lead-owned schedules emit normal Cortex handoffs. Project dispatch, propose mode, and
            agent auto-dispatch still control execution.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void exportFeeders()}
            disabled={action === 'export-feeders'}
            className="rounded-md border border-glass-line px-2.5 py-1.5 text-[11px] font-semibold text-ink-300 hover:bg-base-800/70 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {action === 'export-feeders' ? 'Exporting...' : 'Export JSON'}
          </button>
          <button
            type="button"
            onClick={() => void savePlanningBeat()}
            disabled={!client.savePlanningBeat || action === 'save-planning-beat'}
            className="rounded-md border border-mint-400/35 bg-mint-500/10 px-2.5 py-1.5 text-[11px] font-semibold text-mint-200 hover:bg-mint-500/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {action === 'save-planning-beat'
              ? 'Saving PM beat...'
              : planningJob
                ? 'Refresh PM beat'
                : 'Create PM beat'}
          </button>
          <button
            type="button"
            onClick={() => void importFeeders()}
            disabled={!canMigrate || !importText.trim() || action === 'import-feeders'}
            className="rounded-md border border-glass-line px-2.5 py-1.5 text-[11px] font-semibold text-ink-300 hover:bg-base-800/70 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {action === 'import-feeders' ? 'Importing...' : 'Import JSON'}
          </button>
          <span className="inline-flex items-center gap-1.5 rounded-md bg-base-800/60 px-2 py-1 text-[10px] text-ink-400">
            <StatusDot status={state.connected ? 'running' : state.loading ? 'queued' : 'idle'} />
            {state.loading ? 'loading' : state.connected ? 'connected' : 'not connected'}
          </span>
        </div>
      </div>

      {(state.error || state.notice) && (
        <div
          className={cx(
            'border-b px-4 py-2 text-[11px]',
            state.error
              ? 'border-run-errored/20 bg-run-errored/10 text-run-errored'
              : 'border-mint-400/15 bg-mint-500/10 text-mint-300',
          )}
        >
          {state.error || state.notice}
        </div>
      )}

      <div className="border-b border-glass-line px-4 py-3">
        <label className="text-[11px] text-ink-500">
          Import / export definitions
          <textarea
            className={`${inputClass} mt-1 min-h-24 resize-y font-mono text-[11px]`}
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
            placeholder='{"scheduled_jobs":[]}'
          />
        </label>
        <p className="mt-1 text-[10px] text-ink-600">
          Definition-only JSON. This does not import run history or Cortex memory.
        </p>
      </div>

      <div className="px-4 py-4">
        <section>
          <div className="mb-2 flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-ink-100">Scheduled handoffs</h3>
            <span className="text-[10px] text-ink-500">{state.jobs.length} saved</span>
          </div>
          <form onSubmit={saveJob} className="grid grid-cols-1 gap-2 rounded-xl border border-glass-line bg-base-950/30 p-3">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              <label className="text-[11px] text-ink-500">
                Name
                <input className={inputClass} value={jobName} onChange={(e) => setJobName(e.target.value)} />
              </label>
              <label className="text-[11px] text-ink-500">
                From agent
                <input
                  className={inputClass}
                  placeholder="marlow"
                  value={jobFromAgent}
                  onChange={(e) => setJobFromAgent(e.target.value)}
                />
              </label>
            </div>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
              <label className="text-[11px] text-ink-500">
                Schedule
                <select className={inputClass} value={jobKind} onChange={(e) => setJobKind(e.target.value as 'interval' | 'daily')}>
                  <option value="interval">Interval</option>
                  <option value="daily">Daily</option>
                </select>
              </label>
              {jobKind === 'interval' ? (
                <label className="text-[11px] text-ink-500 md:col-span-2">
                  Every minutes
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    value={jobEveryMinutes}
                    onChange={(e) => setJobEveryMinutes(e.target.value)}
                  />
                </label>
              ) : (
                <>
                  <label className="text-[11px] text-ink-500">
                    Time
                    <input className={inputClass} type="time" value={jobDailyTime} onChange={(e) => setJobDailyTime(e.target.value)} />
                  </label>
                  <label className="text-[11px] text-ink-500">
                    Timezone
                    <input className={inputClass} value={jobTimezone} onChange={(e) => setJobTimezone(e.target.value)} />
                  </label>
                </>
              )}
            </div>
            <label className="text-[11px] text-ink-500">
              Target role
              <input className={inputClass} value={jobToRole} onChange={(e) => setJobToRole(e.target.value)} />
            </label>
            <label className="text-[11px] text-ink-500">
              Handoff summary
              <textarea
                className={`${inputClass} min-h-20 resize-y`}
                value={jobSummary}
                onChange={(e) => setJobSummary(e.target.value)}
              />
            </label>
            <button
              type="submit"
              disabled={!canWrite || action === 'save-job'}
              className="justify-self-start rounded-lg bg-mint-500/20 px-3 py-2 text-xs font-semibold text-mint-200 transition-colors hover:bg-mint-500/30 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {action === 'save-job' ? 'Saving...' : editingJobId ? 'Update schedule' : 'Save schedule'}
            </button>
            {editingJobId && (
              <button
                type="button"
                onClick={() => setEditingJobId(null)}
                className="justify-self-start rounded-lg border border-glass-line px-3 py-2 text-xs font-semibold text-ink-300 hover:bg-base-800/70"
              >
                Cancel edit
              </button>
            )}
          </form>

          <div className="mt-3 divide-y divide-glass-line rounded-xl border border-glass-line bg-base-950/20">
            {state.jobs.length === 0 ? (
              <p className="px-3 py-3 text-[11px] text-ink-500">No scheduled jobs yet.</p>
            ) : (
              state.jobs.map((job) => (
                <div key={job.id} className="flex flex-wrap items-center gap-3 px-3 py-2.5">
                  <StatusDot status={job.enabled ? 'running' : 'idle'} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-ink-100">{job.name}</div>
                    <div className="truncate text-[11px] text-ink-500">
                      {scheduleSummary(job)} · {jobTarget(job)} · next {dtLabel(job.next_run_at)}
                    </div>
                    {job.last_error && (
                      <div className="truncate text-[11px] text-run-errored">{job.last_error}</div>
                    )}
                  </div>
                  <button
                    type="button"
                    disabled={!client.runScheduledJobNow || action === `job:${job.id}`}
                    onClick={() => void markJobDue(job)}
                    className="rounded-md border border-glass-line px-2.5 py-1.5 text-[11px] font-semibold text-ink-300 hover:bg-base-800/70 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {action === `job:${job.id}` ? 'Queuing...' : 'Run next sweep'}
                  </button>
                  <button
                    type="button"
                    onClick={() => editJob(job)}
                    className="rounded-md border border-glass-line px-2.5 py-1.5 text-[11px] font-semibold text-ink-300 hover:bg-base-800/70"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    disabled={!client.deleteScheduledJob || action === `delete-job:${job.id}`}
                    onClick={() => void deleteJob(job)}
                    className="rounded-md border border-run-errored/30 px-2.5 py-1.5 text-[11px] font-semibold text-run-errored hover:bg-run-errored/10 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {action === `delete-job:${job.id}` ? 'Deleting...' : 'Delete'}
                  </button>
                </div>
              ))
            )}
          </div>
        </section>
      </div>
    </GlassCard>
  )
}

// ---------------------------------------------------------------------------
//  Project worker feed — a bounded project-level transcript stitch from the
//  RunState SSOT. This is intentionally read-only and project-scoped: no memory
//  lookup, no project-specific code, no hidden local files.
// ---------------------------------------------------------------------------

interface ProjectFeedState {
  key: string
  transcripts: RunTranscript[]
  loading: boolean
  failed: number
}

type TranscriptLoad =
  | { ok: true; data: RunTranscript }
  | { ok: false; aborted: boolean }

const FEED_SEGMENT_STYLE: Record<string, string> = {
  input: 'text-ink-100',
  output: 'text-ink-300',
  thinking: 'text-ink-500 italic',
  think: 'text-ink-500 italic',
  tool: 'text-mint-300/90',
  error: 'text-run-errored',
  attachment: 'text-ink-300',
}

function projectFeedRows(board: RunBoard | null): RunRow[] {
  if (!board) return []
  const byId = new Map<string, RunRow>()
  for (const row of [...(board.recent ?? []), ...(board.active ?? [])]) {
    if (row?.run_id) byId.set(row.run_id, row)
  }
  return [...byId.values()]
    .sort((a, b) => String(a.started_ts ?? '').localeCompare(String(b.started_ts ?? '')))
    .slice(-WORKER_FEED_RUN_LIMIT)
}

function feedSegmentLabel(seg: RunSegment, transcript: RunTranscript): string {
  if (seg.kind === 'input') return 'you'
  if (seg.kind === 'thinking' || seg.kind === 'think') return 'thinking'
  if (seg.kind === 'tool') return 'tool'
  if (seg.kind === 'error') return 'error'
  if (seg.kind === 'attachment') return 'file'
  return transcript.agent_display ?? transcript.agent ?? 'worker'
}

function feedSegmentClass(kind: string): string {
  return FEED_SEGMENT_STYLE[kind] ?? FEED_SEGMENT_STYLE.output
}

function shouldMergeFeedSegment(kind: string): boolean {
  return kind === 'output' || kind === 'thinking' || kind === 'think'
}

function coalesceFeedSegments(segments: RunSegment[]): RunSegment[] {
  const merged: RunSegment[] = []
  for (const seg of segments) {
    const last = merged[merged.length - 1]
    if (last && last.kind === seg.kind && shouldMergeFeedSegment(seg.kind)) {
      last.text += seg.text
    } else {
      merged.push({ ...seg })
    }
  }
  return merged
}

function ProjectWorkerFeed({
  runBoard,
  client,
}: {
  runBoard: RunBoard | null
  client: DashboardClient
}) {
  const rows = useMemo(() => projectFeedRows(runBoard), [runBoard])
  const feedKey = rows.map((row) => row.run_id).join('|')
  const rowsById = new Map(rows.map((row) => [row.run_id, row]))
  const [state, setState] = useState<ProjectFeedState>({
    key: '',
    transcripts: [],
    loading: false,
    failed: 0,
  })

  useEffect(() => {
    if (rows.length === 0) {
      queueMicrotask(() => {
        setState({ key: feedKey, transcripts: [], loading: false, failed: 0 })
      })
      return
    }

    const ctrl = new AbortController()
    queueMicrotask(() => {
      if (ctrl.signal.aborted) return
      setState((prev) => ({
        key: feedKey,
        transcripts: prev.key === feedKey ? prev.transcripts : [],
        loading: true,
        failed: 0,
      }))
    })

    Promise.all<TranscriptLoad>(
      rows.map(async (row) => {
        try {
          return { ok: true, data: await client.run(row.run_id, ctrl.signal) }
        } catch {
          return { ok: false, aborted: ctrl.signal.aborted }
        }
      }),
    ).then((results) => {
      if (ctrl.signal.aborted) return
      const transcripts = results
        .filter((result): result is { ok: true; data: RunTranscript } => result.ok)
        .sort((a, b) => String(a.data.started_ts ?? '').localeCompare(String(b.data.started_ts ?? '')))
        .map((result) => result.data)
      setState({
        key: feedKey,
        transcripts,
        loading: false,
        failed: results.filter((result) => !result.ok && !result.aborted).length,
      })
    })

    return () => ctrl.abort()
  }, [client, feedKey, rows])

  const loading = state.key === feedKey ? state.loading : rows.length > 0
  const failed = state.key === feedKey ? state.failed : 0
  const transcripts = state.key === feedKey ? state.transcripts : []
  const anyRunning = rows.some((row) => row.running)

  return (
    <GlassCard data-testid="dashboard-worker-feed" className="overflow-hidden p-0 lg:col-span-2">
      <div className="flex items-center gap-2 border-b border-glass-line px-4 py-3">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
          Continuous worker feed
        </h2>
        <span className="ml-auto inline-flex items-center gap-1.5 text-[10px] text-ink-500">
          {anyRunning && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-mint-400" />}
          {rows.length} {rows.length === 1 ? 'run' : 'runs'}
        </span>
      </div>

      {failed > 0 && (
        <div className="border-b border-run-errored/20 bg-run-errored/10 px-4 py-2 text-[11px] text-run-errored">
          {failed} run transcript{failed === 1 ? '' : 's'} could not load.
        </div>
      )}

      <div className="max-h-[28rem] overflow-y-auto px-4 py-3 font-mono text-[12px] leading-relaxed">
        {!runBoard ? (
          <p className="text-ink-500">Run-state feed is unavailable for this project.</p>
        ) : rows.length === 0 ? (
          <p className="text-ink-500">
            No worker runs yet. Chat turns, handoff dispatches, tools, thinking, and final
            outputs appear here as run-state records land.
          </p>
        ) : loading && transcripts.length === 0 ? (
          <p className="text-ink-500">Loading worker feed...</p>
        ) : transcripts.length === 0 ? (
          <p className="text-ink-500">
            {anyRunning ? 'Waiting for worker output...' : 'No transcript output yet.'}
          </p>
        ) : (
          transcripts.map((tx) => (
            <section key={tx.run_id} className="block border-t border-glass-line/70 py-3 first:border-t-0 first:pt-0">
              <DashboardRunMarker transcript={tx} row={rowsById.get(tx.run_id) ?? null} />
              {tx.error && (
                <DashboardFeedLine
                  label="error"
                  kind="error"
                  text={tx.error}
                />
              )}
              {coalesceFeedSegments(tx.segments).map((seg, idx) => (
                <DashboardFeedLine
                  key={`${tx.run_id}-${idx}`}
                  label={feedSegmentLabel(seg, tx)}
                  kind={seg.kind}
                  text={seg.text}
                />
              ))}
            </section>
          ))
        )}
      </div>
    </GlassCard>
  )
}

function DashboardRunMarker({
  transcript,
  row,
}: {
  transcript: RunTranscript
  row: RunRow | null
}) {
  const agent = transcript.agent_display ?? row?.agent_display ?? transcript.agent ?? 'worker'
  const model = transcript.model ?? row?.model
  return (
    <div className="mb-2 flex items-center gap-2 text-ink-500">
      <StatusDot status={statusKind(transcript.running ? 'running' : transcript.status_label ?? transcript.status)} pulse={transcript.running} />
      <span className="truncate text-[10px] font-semibold uppercase tracking-wide text-ink-300">
        {agent}
      </span>
      <span className="font-mono text-[10px]" title={transcript.run_id}>
        {transcript.run_id.slice(0, 8)}
      </span>
      {transcript.handoff_short && (
        <span className="font-mono text-[10px]">handoff {transcript.handoff_short}</span>
      )}
      {model && <span className="hidden text-[10px] sm:inline">{model}</span>}
      {transcript.started_ago && (
        <span className="ml-auto shrink-0 text-[10px]">started {transcript.started_ago} ago</span>
      )}
    </div>
  )
}

function DashboardFeedLine({
  label,
  kind,
  text,
}: {
  label: string
  kind: string
  text: string
}) {
  if (!text) return null
  return (
    <div
      data-feed-line
      data-seg-kind={kind}
      className="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-3 py-1"
    >
      <span className="select-none pt-[1px] text-right font-mono text-[10px] uppercase tracking-wide text-ink-500">
        {label}
      </span>
      <span className={cx('whitespace-pre-wrap break-words', feedSegmentClass(kind))}>
        {text}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
//  Cortex health pill — reads the console JSON /cortex/health (same endpoint the
//  Settings → Cortex tab uses). Best-effort: "checking…" until the probe settles,
//  then the live status (or a graceful "unreachable" — never a crash).
// ---------------------------------------------------------------------------

interface HealthRead {
  status?: string
  error?: string
  [k: string]: unknown
}

function CortexHealthPill({ project }: { project: string }) {
  const [health, setHealth] = useState<HealthRead | null>(null)
  const [probed, setProbed] = useState(false)
  const [viewProject, setViewProject] = useState(project)
  if (project !== viewProject) {
    setViewProject(project)
    setHealth(null)
    setProbed(false)
  }

  useEffect(() => {
    const ctrl = new AbortController()
    let alive = true
    fetch(`/cortex/health?project=${encodeURIComponent(project)}`, {
      headers: { Accept: 'application/json' },
      signal: ctrl.signal,
    })
      .then(async (res) => (res.ok ? ((await res.json()) as HealthRead) : null))
      .catch(() => null)
      .then((h) => {
        if (!alive) return
        setHealth(h)
        setProbed(true)
      })
    return () => {
      alive = false
      ctrl.abort()
    }
  }, [project])

  const status = (health?.status || (probed ? 'unreachable' : 'checking')).toLowerCase()
  const ok = ['ok', 'healthy', 'up'].includes(status)
  const label = health?.status || (probed ? 'unreachable' : 'checking…')

  return (
    <span
      data-testid="dashboard-health"
      title={health?.error ? `Cortex: ${String(health.error)}` : 'Local Cortex health'}
      className={cx(
        'inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-[10px] font-medium',
        ok
          ? 'bg-mint-500/15 text-mint-300'
          : status === 'checking'
            ? 'bg-base-700/60 text-ink-400'
            : 'bg-run-errored/15 text-run-errored/90',
      )}
    >
      <StatusDot status={ok ? 'running' : status === 'checking' ? 'queued' : 'errored'} />
      Cortex · {label}
    </span>
  )
}
