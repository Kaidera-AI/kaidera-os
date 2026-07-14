/**
 * AnalyticsView — a PROJECT-LEVEL main-area view: usage + est. cost.
 *
 * The token/cost breakdown for the project (NOT a single agent): a project rollup
 * (total tokens, est. cost, agents-with-usage, runs) as glass stat cards, a usage-
 * by-model bar breakdown, a model×provider grouping, and a per-agent token + cost
 * table. Reached via the main-area switcher — it does NOT live in a column, so it
 * never repeats the agents/metrics the 2nd column owns; this is the deep usage cut.
 *
 * Data: GET /analytics/{project}/usage (UsageBreakdown — AnalyticsService.
 * shape_usage_cost). The backend already sorts the bars (desc) + the cost rows
 * (cost desc); this view is pure presentation.
 *
 * Graceful-degrade: `store_connected === false` → a "usage store not connected"
 * note; connected-but-empty (total_runs === 0) → a "no usage recorded yet" empty
 * state; a stale backend (the route 404s) → the error hint. Never a crash.
 */

import { useState } from 'react'
import { GlassPanel, GlassCard, StatPill } from '../components/glass'
import { cx } from '../components/ui'
import type {
  AnalyticsKpis,
  UsageBar,
  UsageBreakdown,
  UsageAgentRow,
  UsageModelRow,
  UsageProviderGroup,
} from '../api'

interface AnalyticsViewProps {
  project: string | null
  usage: UsageBreakdown | null
  /** The slim headline KPI strip (events/24h · active tasks · decisions · recent tokens), from
   * GET /analytics/{p}/kpis. Optional/null — the strip simply hides until it loads. */
  kpis?: AnalyticsKpis | null
  loading: boolean
  error: Error | null
}

/** A labelled proportional bar (label · value, a mint track filled to `pct`). */
function BarRow({ bar }: { bar: UsageBar }) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-xs text-ink-300" title={bar.label}>
          {bar.label}
        </span>
        <span className="shrink-0 text-[11px] tabular-nums text-ink-400">{bar.value_h}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-base-700/60">
        <div
          className="h-full rounded-full bg-mint-400/70"
          style={{ width: `${Math.max(2, Math.min(100, bar.pct))}%` }}
        />
      </div>
    </div>
  )
}

function BarBreakdown({ title, bars }: { title: string; bars: UsageBar[] }) {
  if (bars.length === 0) return null
  return (
    <GlassCard className="p-4">
      <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
        {title}
      </h3>
      <div className="space-y-2.5">
        {bars.map((b) => (
          <BarRow key={b.label} bar={b} />
        ))}
      </div>
    </GlassCard>
  )
}

/** The per-agent token + est-cost table (the model-usage-per-agent + cost cut). */
function AgentUsageTable({ rows }: { rows: UsageAgentRow[] }) {
  const withUsage = rows.filter((r) => (r.tokens ?? 0) > 0 || r.cost !== null)
  if (withUsage.length === 0) return null
  return (
    <GlassCard className="overflow-hidden p-0">
      <h3 className="border-b border-glass-line px-4 py-3 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
        Usage by worker
      </h3>
      <div className="divide-y divide-glass-line">
        {withUsage.map((r) => (
          <div
            key={r.agent}
            className="flex items-center gap-3 px-4 py-2.5"
          >
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-ink-100">{r.display}</div>
              <div className="truncate text-[11px] text-ink-500">
                {r.model_known ? r.model : 'model n/a'}
                {r.provider ? ` · ${r.provider}` : ''}
              </div>
            </div>
            <div className="shrink-0 text-right">
              <div className="text-xs tabular-nums text-ink-200">{r.tokens_h ?? '—'}</div>
              <div className="text-[10px] uppercase tracking-wide text-ink-500">tokens</div>
            </div>
            <div className="w-20 shrink-0 text-right">
              <div
                className={cx(
                  'text-xs tabular-nums',
                  r.priced ? 'text-mint-300' : 'text-ink-500',
                )}
                title={r.cost_na_reason ?? undefined}
              >
                {r.cost_h}
              </div>
              <div className="text-[10px] uppercase tracking-wide text-ink-500">est. cost</div>
            </div>
          </div>
        ))}
      </div>
    </GlassCard>
  )
}

/** One headline KPI tile (value over a label). A null value renders 'n/a' (never a fabricated
 * 0 — a degraded Cortex counter is honestly absent). */
function KpiTile({ label, value }: { label: string; value: string | number | null }) {
  return (
    <div className="glass-soft flex min-w-0 flex-col items-start gap-0.5 rounded-lg px-2.5 py-2">
      <span
        className={cx(
          'text-base font-semibold leading-none tabular-nums',
          value != null ? 'text-ink-100' : 'text-ink-500',
        )}
      >
        {value != null ? value : 'n/a'}
      </span>
      <span className="text-[10px] font-medium uppercase tracking-wider text-ink-500">{label}</span>
    </div>
  )
}

/**
 * The slim headline KPI strip — the legacy `_analytics.html` `an-kpi-row`: Events/24h ·
 * Active tasks · Decisions · recent Tokens. Sits above the cost hero + breakdowns. Null
 * counters degrade to 'n/a'.
 */
function KpiStrip({ kpis }: { kpis: AnalyticsKpis }) {
  return (
    <div data-testid="analytics-kpis" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      <KpiTile label="Events · 24h" value={kpis.events_24h} />
      <KpiTile label="Active tasks" value={kpis.active_tasks} />
      <KpiTile label={`Decisions · ${kpis.window_days}d`} value={kpis.decisions_recent} />
      <KpiTile label="Tokens · recent" value={kpis.tokens_recent_h ?? (kpis.tokens_recent ? String(kpis.tokens_recent) : null)} />
    </div>
  )
}

/**
 * The COST HERO — the elevated, prominent project est-cost panel (the legacy `an-cost-hero`).
 * Foregrounds the project's estimated API cost (big), with the total tokens · agents-with-usage ·
 * models/providers as side stats. The cost is a counterfactual metered estimate (the team is on
 * subscriptions) — labelled `est.`.
 */
function CostHero({ usage }: { usage: UsageBreakdown }) {
  const priced = usage.project_cost != null
  return (
    <GlassCard data-testid="analytics-cost-hero" className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-ink-500">
            Est. API cost · this project
            <span
              title="The team is on subscriptions, so this is what the usage WOULD cost on metered API tokens — not a real charge."
              className="rounded bg-base-700/50 px-1 py-0.5 text-[9px] font-semibold text-ink-400"
            >
              est.
            </span>
          </div>
          <div
            className={cx(
              'mt-1 text-3xl font-semibold tabular-nums',
              priced ? 'text-mint-300' : 'text-ink-500',
            )}
          >
            {usage.project_cost_h}
          </div>
          <div className="mt-1 text-[11px] text-ink-500">
            {priced
              ? `summed across ${usage.priced_agent_count} agent${usage.priced_agent_count === 1 ? '' : 's'} with priced usage · ${usage.total_tokens_h ?? '0'} tokens`
              : usage.store_connected
                ? 'no usage recorded yet — agent runs will populate this'
                : 'usage store not connected — cost is recorded once the store is up'}
          </div>
        </div>
        <div className="flex shrink-0 gap-4">
          <div className="text-right">
            <div className="text-sm font-semibold tabular-nums text-ink-200">
              {usage.total_tokens_h ?? 'n/a'}
            </div>
            <div className="text-[9px] uppercase tracking-wide text-ink-500">Total tokens</div>
          </div>
          <div className="text-right">
            <div className="text-sm font-semibold tabular-nums text-ink-200">
              {usage.agents_with_usage}
              <span className="text-ink-500">/{usage.agent_count}</span>
            </div>
            <div className="text-[9px] uppercase tracking-wide text-ink-500">Agents w/ usage</div>
          </div>
          <div className="text-right">
            <div className="text-sm font-semibold tabular-nums text-ink-200">
              {usage.model_count}
              <span className="text-ink-500"> · {usage.provider_count}</span>
            </div>
            <div className="text-[9px] uppercase tracking-wide text-ink-500">Models · prov</div>
          </div>
        </div>
      </div>
    </GlassCard>
  )
}

/**
 * The est. API cost BY AGENT table + a project-TOTAL footer row (the legacy `an-tbl-cost` with
 * its `tfoot`). Agents are cost-desc (the backend sorts `cost_rows`); an unpriced row shows
 * 'n/a'. The footer sums to the project est. cost.
 */
function CostByAgentTable({ usage }: { usage: UsageBreakdown }) {
  if (usage.cost_rows.length === 0) return null
  return (
    <GlassCard className="overflow-hidden p-0">
      <div className="flex items-center gap-2 border-b border-glass-line px-4 py-3">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
          Est. API cost by worker
        </h3>
        <span className="rounded bg-base-700/50 px-1 py-0.5 text-[9px] font-semibold text-ink-400">
          est.
        </span>
        <span className="ml-auto text-[10px] tabular-nums text-ink-500">
          {usage.priced_agent_count} priced
        </span>
      </div>
      <div className="divide-y divide-glass-line">
        {usage.cost_rows.map((r) => (
          <div
            key={r.agent}
            className={cx('flex items-center gap-3 px-4 py-2', r.cost == null && 'opacity-60')}
          >
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm text-ink-100">{r.display}</div>
              <div className="truncate text-[10px] text-ink-500">
                {r.model_known ? r.model : '—'}
              </div>
            </div>
            <div className="shrink-0 text-right text-xs tabular-nums text-ink-300">
              {r.tokens != null ? r.tokens_h : <span className="text-ink-500">n/a</span>}
            </div>
            <div className="w-20 shrink-0 text-right">
              {r.cost != null ? (
                <span className="text-xs font-semibold tabular-nums text-mint-300">{r.cost_h}</span>
              ) : (
                <span className="text-xs tabular-nums text-ink-500" title={r.cost_na_reason ?? undefined}>
                  n/a
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
      {/* Project-total footer (the legacy tfoot). */}
      <div
        data-testid="analytics-cost-total"
        className="flex items-center gap-3 border-t border-glass-line bg-base-800/40 px-4 py-2.5"
      >
        <span className="flex-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
          Project total · est.
        </span>
        <span
          className={cx(
            'text-sm font-semibold tabular-nums',
            usage.project_cost != null ? 'text-mint-300' : 'text-ink-500',
          )}
        >
          {usage.project_cost_h}
        </span>
      </div>
    </GlassCard>
  )
}

/**
 * An expandable detail cut: the exact per-model token table + the per-provider
 * groups (model×provider) the backend already shapes (`by_model_table` /
 * `by_provider`). Collapsed by default — the bars above are the at-a-glance read;
 * this is the drill-down. A controlled disclosure (button + region) so the
 * accessible name is stable.
 */
function ModelProviderBreakdown({
  models,
  providers,
}: {
  models: UsageModelRow[]
  providers: UsageProviderGroup[]
}) {
  const [open, setOpen] = useState(false)
  if (models.length === 0 && providers.length === 0) return null
  const LABEL = 'Model & provider breakdown'
  return (
    <GlassCard className="overflow-hidden p-0">
      <button
        type="button"
        aria-expanded={open}
        aria-label={LABEL}
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-2 px-4 py-3 text-left transition-colors hover:bg-base-800/40"
      >
        <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
          {LABEL}
        </span>
        <span className="text-[10px] tabular-nums text-ink-500">
          {open ? '▾ hide' : `▸ ${models.length} models · ${providers.length} providers`}
        </span>
      </button>

      {open && (
        <div role="region" aria-label={LABEL} className="space-y-4 border-t border-glass-line p-4">
          {/* The exact per-model token table. */}
          {models.length > 0 && (
            <div className="space-y-1.5">
              <h4 className="text-[10px] font-semibold uppercase tracking-wide text-ink-500">
                By model
              </h4>
              <div className="divide-y divide-glass-line">
                {models.map((m) => (
                  <div key={`${m.provider}:${m.model}`} className="flex items-center gap-3 py-1.5">
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-200" title={m.model}>
                      {m.model}
                    </span>
                    <span className="shrink-0 text-[10px] uppercase tracking-wide text-ink-500">
                      {m.provider}
                    </span>
                    <span className="w-16 shrink-0 text-right text-xs tabular-nums text-ink-300">
                      {m.tokens_h}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* The per-provider groups (model×provider). */}
          {providers.length > 0 && (
            <div className="space-y-2">
              <h4 className="text-[10px] font-semibold uppercase tracking-wide text-ink-500">
                By provider
              </h4>
              {providers.map((p) => (
                <div key={p.provider} className="space-y-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-xs font-medium text-ink-200">{p.label}</span>
                    <span className="text-[11px] tabular-nums text-mint-300">{p.tokens_h}</span>
                  </div>
                  <div className="ml-3 divide-y divide-glass-line/60">
                    {p.models.map((m) => (
                      <div key={m.model} className="flex items-center gap-3 py-1">
                        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-ink-400" title={m.model}>
                          {m.model}
                        </span>
                        <span className="w-16 shrink-0 text-right text-[11px] tabular-nums text-ink-400">
                          {m.tokens_h}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </GlassCard>
  )
}

export function AnalyticsView({ project, usage, kpis, loading, error }: AnalyticsViewProps) {
  const connected = usage?.store_connected ?? false
  const totalRuns = usage?.total_runs ?? 0
  const hasUsage = !!usage && connected && totalRuns > 0

  return (
    <GlassPanel className="min-w-0 flex-1">
      <header className="border-b border-glass-line px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-base font-semibold text-ink-100">Analytics · usage</h1>
          {usage && !connected && (
            <span className="rounded-md bg-run-errored/12 px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-run-errored/80">
              store offline
            </span>
          )}
        </div>
        {/* The project usage rollup as glass stat cards. */}
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <StatPill
            label="Total tokens"
            value={usage?.total_tokens_h ?? (totalRuns ? '0' : '—')}
            tone={hasUsage ? 'mint' : 'muted'}
          />
          <StatPill
            label="Est. cost"
            value={usage?.project_cost_h ?? 'n/a'}
            tone={usage?.project_cost != null ? 'mint' : 'muted'}
          />
          <StatPill
            label="Agents used"
            value={usage?.agents_with_usage ?? 0}
            tone={(usage?.agents_with_usage ?? 0) > 0 ? 'default' : 'muted'}
            title="Agents with recorded usage"
          />
          <StatPill
            label="Runs"
            value={totalRuns}
            tone={totalRuns > 0 ? 'default' : 'muted'}
            title="Total runs recorded"
          />
        </div>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {loading && !usage && (
          <p className="px-1 py-2 text-xs text-ink-500">Loading usage…</p>
        )}

        {error && !usage && (
          <p className="px-1 py-2 text-xs leading-relaxed text-run-errored/80">
            Couldn’t load usage for <span className="font-mono">{project}</span>. The
            backend <code className="font-mono">/analytics/{'{project}'}/usage</code>{' '}
            route may not be live in this build.
          </p>
        )}

        {/* The slim headline KPI strip — always at the top once it lands (it reads Cortex, not the
            usage store, so it stands even when the usage store is offline/empty). */}
        {kpis && <KpiStrip kpis={kpis} />}

        {/* The elevated cost hero — rendered whenever a usage payload exists (it shows the n/a
            states itself when the store is offline / empty). */}
        {usage && <CostHero usage={usage} />}

        {usage && !connected && (
          <div className="flex items-center justify-center p-8 text-center">
            <p className="max-w-sm text-sm leading-relaxed text-ink-500">
              The usage store isn’t connected, so no token/cost data is available for
              this project.
            </p>
          </div>
        )}

        {usage && connected && !hasUsage && (
          <div className="flex items-center justify-center p-8 text-center">
            <p className="text-sm text-ink-500">No usage recorded yet for this project.</p>
          </div>
        )}

        {hasUsage && (
          <>
            <BarBreakdown title="Usage by model" bars={usage.by_model_bars} />
            <BarBreakdown title="Usage by provider" bars={usage.by_provider_bars} />
            <ModelProviderBreakdown models={usage.by_model_table} providers={usage.by_provider} />
            <AgentUsageTable rows={usage.rows} />
            <CostByAgentTable usage={usage} />
          </>
        )}
      </div>
    </GlassPanel>
  )
}
