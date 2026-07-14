/**
 * AgentsColumn — the 2nd column. The single CANONICAL home for AGENTS + METRICS.
 *
 * Per the CTO's no-repetition law, this is the ONE place agents live: the roster
 * (Interactive vs AI Workers groups) AND the project rollup metrics (agents
 * count, pending dispatch, active runs). The left column never repeats this.
 *
 * It now mirrors the legacy col-2 (`app/templates/_agents_col.html`): each agent
 * row carries its writer_scope + capability chips; below the roster sits the
 * METRICS block (active tasks / pending tasks / pending handoffs / events-24h with
 * a "details" drill-in affordance) and the ACTIVE-EPIC widget (per-epic progress +
 * per-increment mini-bars, or the 'continuous · no epics' line). The metrics + epic
 * data come from GET /agents/{project}/epics.
 *
 * Data: GET /agents/{project} (the catalog), GET /runs/{project} (active-run count,
 * per-agent running flags), GET /dispatch/{project}/board (pending),
 * GET /agents/{project}/epics (the epic widget + metrics block).
 */

import { useMemo, useState } from 'react'
import { GlassPanel, GlassCard, StatPill, StatusDot } from '../components/glass'
import { cx } from '../components/ui'
import { AddAgentModal, type AddAgentClient } from './RegistrationForms'
import type {
  AgentEpicsPayload,
  AgentView,
  AgentsCatalog,
  DispatchBoard,
  EpicView,
  MetricsBlock,
  RunBoard,
} from '../api'

interface AgentsColumnProps {
  project: string | null
  catalog: AgentsCatalog | null
  runBoard: RunBoard | null
  dispatch: DispatchBoard | null
  /** The Active-Epic widget + the project metrics block (GET /agents/{p}/epics). Null when not
   * yet loaded / a stale-backend 404 — the metrics degrade to '—' and the epic widget hides. */
  epics?: AgentEpicsPayload | null
  selectedAgent: string | null
  onSelectAgent: (agent: string) => void
  /** Optional drill-in for the metrics "details" affordance (the legacy col-2 swapped the
   * center to the project detail). A no-op-safe callback — the button only renders when set. */
  onShowMetricsDetails?: () => void
  /** Optional registration client (feature-gap #81) — enables the "+ Add agent" affordance +
   * its modal (configCatalog for the dropdowns + registerAgent). When absent, no add button. */
  registrationClient?: AddAgentClient
  /** Called after a successful agent register — the shell refetches the roster (new agent appears). */
  onAgentRegistered?: () => void
  loading: boolean
  error: Error | null
}

interface AgentRowProps {
  agent: AgentView
  active: boolean
  running: boolean
  onSelect: (name: string) => void
}

function AgentRow({ agent, active, running, onSelect }: AgentRowProps) {
  return (
    <GlassCard
      interactive
      active={active}
      onClick={() => onSelect(agent.name)}
      className="px-3 py-2.5"
    >
      {/* Minimalist row (CTO): initials · name · role (from config) · green-if-live.
          Everything else (harness/model/caps/writer-scope) lives in the main window. */}
      <div className="flex items-center gap-3">
        <div
          className={cx(
            'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-xs font-semibold',
            active ? 'bg-mint-500/20 text-mint-200' : 'bg-base-700/60 text-ink-300',
          )}
        >
          {agent.initials}
        </div>

        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <span className="truncate text-sm font-semibold text-ink-100">
            {agent.display_name}
          </span>
          {/* The live dot sits right next to the name. */}
          {running && <StatusDot status="running" pulse title="live" />}
          {agent.role && (
            <span
              title={agent.role}
              className="ml-auto shrink-0 max-w-[7rem] truncate rounded bg-mint-500/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-mint-300"
            >
              {agent.role}
            </span>
          )}
        </div>
      </div>
    </GlassCard>
  )
}

function Group({
  title,
  agents,
  runningNames,
  selectedAgent,
  onSelectAgent,
  caption,
}: {
  title: string
  agents: AgentView[]
  runningNames: Set<string>
  selectedAgent: string | null
  onSelectAgent: (name: string) => void
  caption?: string | null
}) {
  if (agents.length === 0) return null
  return (
    <section className="space-y-1.5">
      <div className="flex items-baseline justify-between px-1">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
          {title}
        </h3>
        {caption && <span className="text-[10px] text-ink-500">{caption}</span>}
      </div>
      <div className="space-y-1.5">
        {agents.map((a) => (
          <AgentRow
            key={a.name}
            agent={a}
            active={selectedAgent === a.name}
            running={runningNames.has(a.name.toLowerCase())}
            onSelect={onSelectAgent}
          />
        ))}
      </div>
    </section>
  )
}

/** A single metric tile (value over a label). A null value renders the em-dash degrade. */
function Metric({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="rounded-lg bg-base-800/40 px-2 py-1.5">
      <div className="text-sm font-semibold tabular-nums text-ink-100">
        {value != null ? value : '—'}
      </div>
      <div className="text-[9px] font-medium uppercase tracking-wide text-ink-500">{label}</div>
    </div>
  )
}

/**
 * The METRICS block — the legacy col-2 metrics card: active tasks / pending tasks / pending
 * handoffs / events-24h, with an optional "details" drill-in. A null `metrics` (epics payload
 * not loaded) renders the labels with em-dash values (graceful degrade).
 */
function MetricsBlockView({
  metrics,
  onShowDetails,
}: {
  metrics: MetricsBlock | null
  onShowDetails?: () => void
}) {
  return (
    <section className="space-y-2">
      <div className="flex items-baseline justify-between px-1">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
          Metrics
        </h3>
        {onShowDetails && (
          <button
            type="button"
            onClick={onShowDetails}
            className="text-[10px] font-medium text-mint-400 transition-colors hover:text-mint-300"
            title="Open the project's full handoffs + tasks"
          >
            details ›
          </button>
        )}
      </div>
      <div className="grid grid-cols-2 gap-1.5">
        <Metric label="Active tasks" value={metrics?.active_tasks ?? null} />
        <Metric label="Pending tasks" value={metrics?.pending_tasks ?? null} />
        <Metric label="Pending handoffs" value={metrics?.pending_handoffs ?? null} />
        <Metric label="Events · 24h" value={metrics?.events_24h ?? null} />
      </div>
    </section>
  )
}

/** One epic card — id · title · overall % bar + the per-increment mini-bars. */
function EpicCard({ epic }: { epic: EpicView }) {
  return (
    <GlassCard className={cx('p-2.5', epic.is_active && 'border-mint-400/30')} title={epic.title}>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-[11px] font-semibold text-mint-300">{epic.epic_id}</span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-ink-300">{epic.title}</span>
        <span className="shrink-0 text-[11px] font-semibold tabular-nums text-ink-200">
          {epic.overall_pct}%
        </span>
      </div>
      {/* Overall progress track. */}
      <div
        role="progressbar"
        aria-valuenow={epic.overall_pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${epic.epic_id} overall progress`}
        className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-base-700/60"
      >
        <div
          className={cx(
            'h-full rounded-full',
            epic.overall_pct >= 100 ? 'bg-mint-500' : 'bg-mint-400/70',
          )}
          style={{ width: `${Math.max(2, Math.min(100, epic.overall_pct))}%` }}
        />
      </div>
      {/* Per-increment mini-bars (done = filled, prog = teal, todo = empty). */}
      {epic.increments.length > 0 && (
        <div className="mt-1.5 flex gap-1">
          {epic.increments.map((inc, i) => (
            <span
              key={inc.num ?? i}
              title={`${inc.label} · ${inc.title} — ${inc.status || 'not started'} (${inc.pct}%)`}
              className="h-1 flex-1 overflow-hidden rounded-full bg-base-700/60"
            >
              <span
                className={cx(
                  'block h-full rounded-full',
                  inc.kind === 'done'
                    ? 'bg-mint-500'
                    : inc.kind === 'prog'
                      ? 'bg-mint-400/70'
                      : 'bg-transparent',
                )}
                style={{ width: `${Math.max(0, Math.min(100, inc.pct))}%` }}
              />
            </span>
          ))}
        </div>
      )}
    </GlassCard>
  )
}

/**
 * The ACTIVE-EPIC widget — the legacy col-2 epic section. mode='epics' renders the active-major
 * epic stack (each card: id · title · % bar · increment mini-bars); mode='continuous' (or an
 * absent payload) renders the 'continuous · no epics' line. Never fabricates progress.
 */
function ActiveEpic({ epics }: { epics: AgentEpicsPayload | null }) {
  const section = epics?.epic
  const isEpics = section?.mode === 'epics' && (section.epics?.length ?? 0) > 0
  return (
    <section className="space-y-2">
      <div className="flex items-baseline justify-between px-1">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
          Active Epic
        </h3>
        {isEpics ? (
          <span className="text-[10px] tabular-nums text-ink-500">{section!.epic_count}</span>
        ) : (
          <span className="text-[10px] text-ink-500">continuous</span>
        )}
      </div>
      {isEpics ? (
        <div className="space-y-1.5">
          {section!.epics.map((e) => (
            <EpicCard key={e.epic_id} epic={e} />
          ))}
        </div>
      ) : (
        <div className="flex items-center gap-2 rounded-lg bg-base-800/40 px-3 py-2.5">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-ink-500" aria-hidden="true" />
          <span className="text-[11px] text-ink-400">
            {section?.label || 'continuous · no epics'}
          </span>
        </div>
      )}
    </section>
  )
}

export function AgentsColumn({
  project,
  catalog,
  runBoard,
  dispatch,
  epics,
  selectedAgent,
  onSelectAgent,
  onShowMetricsDetails,
  registrationClient,
  onAgentRegistered,
  loading,
  error,
}: AgentsColumnProps) {
  const [addOpen, setAddOpen] = useState(false)
  // The set of agent names with an active (queued|running) run — drives the
  // per-row running dot, sourced from the run board.
  const runningNames = useMemo(() => {
    const s = new Set<string>()
    for (const r of runBoard?.active ?? []) {
      if (r.running && r.agent) s.add(r.agent.toLowerCase())
    }
    return s
  }, [runBoard])

  const agentCount =
    (catalog?.interactive.length ?? 0) + (catalog?.autonomous.length ?? 0)
  const pending = dispatch?.dispatch_count ?? 0
  const activeRuns = runBoard?.active_count ?? 0

  return (
    <GlassPanel className="flex w-80 shrink-0 flex-col max-lg:order-3 max-lg:h-[34rem] max-lg:w-full">
      <header className="flex items-center justify-between border-b border-glass-line px-4 py-3.5">
        <h2 className="text-sm font-semibold text-ink-100">AI Workers</h2>
        {/* "+ Add worker" — opens the register modal (feature-gap #81). Only when a
            registration client + a selected project are present. */}
        {registrationClient && project && (
          <button
            type="button"
            onClick={() => setAddOpen(true)}
            title="Register a new AI Worker on this project"
            className="flex items-center gap-1 rounded-md bg-mint-500/15 px-2 py-1 text-[10px] font-semibold text-mint-200 ring-1 ring-mint-400/30 transition-colors hover:bg-mint-500/25"
          >
            <span aria-hidden="true" className="text-[12px] leading-none">+</span>
            Add worker
          </button>
        )}
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto p-3">
        {loading && !catalog && (
          <p className="px-1 py-2 text-xs text-ink-500">Loading roster…</p>
        )}

        {error && !catalog && (
          <p className="px-1 py-2 text-xs leading-relaxed text-run-errored/80">
            Couldn’t load the roster for{' '}
            <span className="font-mono">{project}</span>.
          </p>
        )}

        {!project && !loading && (
          <p className="px-1 py-2 text-xs text-ink-500">
            Select a project to see its agents.
          </p>
        )}

        {catalog && (
          <>
            <Group
              title="Interactive"
              agents={catalog.interactive}
              runningNames={runningNames}
              selectedAgent={selectedAgent}
              onSelectAgent={onSelectAgent}
            />
            <Group
              title="AI Workers"
              agents={catalog.autonomous}
              runningNames={runningNames}
              selectedAgent={selectedAgent}
              onSelectAgent={onSelectAgent}
            />
            {agentCount === 0 && (
              <p className="px-1 py-2 text-xs text-ink-500">
                No workers in this project’s roster.
              </p>
            )}
          </>
        )}
      </div>

      {/* Metrics pinned to the BOTTOM (CTO) — the roster fills the top + scrolls, and the
          project rollup + epic sit at the foot, separated by space + a hairline. */}
      {catalog && (
        <div className="shrink-0 space-y-3 border-t border-glass-line p-3">
          <div className="grid grid-cols-3 gap-2">
            <StatPill label="Workers" value={agentCount} tone="mint" />
            <StatPill
              label="Pending"
              value={pending}
              tone={pending > 0 ? 'default' : 'muted'}
              title="Open / pending dispatch handoffs"
            />
            <StatPill
              label="Runs"
              value={activeRuns}
              tone={activeRuns > 0 ? 'default' : 'muted'}
              title="Active (queued/running) runs"
            />
          </div>
          <MetricsBlockView metrics={epics?.metrics ?? null} onShowDetails={onShowMetricsDetails} />
          <ActiveEpic epics={epics ?? null} />
        </div>
      )}

      {/* The "+ Add agent" register modal (feature-gap #81). */}
      {registrationClient && project && (
        <AddAgentModal
          open={addOpen}
          onClose={() => setAddOpen(false)}
          project={project}
          client={registrationClient}
          onDone={() => onAgentRegistered?.()}
        />
      )}
    </GlassPanel>
  )
}
