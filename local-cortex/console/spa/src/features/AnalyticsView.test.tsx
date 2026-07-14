import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AnalyticsView } from './AnalyticsView'
import type {
  AnalyticsKpis,
  UsageAgentRow,
  UsageBreakdown,
  UsageModelRow,
  UsageProviderGroup,
} from '../api'

function kpis(over: Partial<AnalyticsKpis> = {}): AnalyticsKpis {
  return {
    project: 'kaidera-os',
    events_24h: 21,
    active_tasks: 5,
    pending_handoffs: 3,
    decisions_recent: 12,
    window_days: 7,
    tokens_recent: 1_240_000,
    tokens_recent_h: '1.24M',
    ...over,
  }
}

function agentRow(over: Partial<UsageAgentRow> = {}): UsageAgentRow {
  return {
    agent: 'ren',
    display: 'Ren',
    model: 'claude-opus-4-8[1m]',
    model_known: true,
    provider: 'Anthropic',
    tokens: 94_592,
    tokens_h: '94.6k',
    input: 80_000,
    output: 14_592,
    priced: true,
    price_in_h: '—',
    price_out_h: '—',
    cost: 1.23,
    cost_h: '$1.23',
    cost_na_reason: null,
    ...over,
  }
}

function usage(over: Partial<UsageBreakdown> = {}): UsageBreakdown {
  return {
    project: 'kaidera-os',
    store_connected: true,
    total_runs: 5,
    total_tokens: 1_240_000,
    total_tokens_h: '1.24M',
    by_model_bars: [{ label: 'claude-opus-4-8[1m]', value: 1_240_000, value_h: '1.24M', pct: 100 }],
    by_model_table: [],
    model_count: 1,
    by_provider: [],
    by_provider_bars: [{ label: 'Anthropic', value: 1_240_000, value_h: '1.24M', pct: 100 }],
    provider_count: 1,
    rows: [agentRow()],
    agent_count: 1,
    agents_with_usage: 1,
    cost_rows: [agentRow()],
    project_cost: 1.23,
    project_cost_h: '$1.23',
    priced_agent_count: 1,
    ...over,
  }
}

describe('AnalyticsView', () => {
  it('renders the project rollup stat cards (tokens + est. cost)', () => {
    render(<AnalyticsView project="kaidera-os" usage={usage()} loading={false} error={null} />)
    // "Total tokens" now appears in BOTH the header rollup AND the cost hero side stat.
    expect(screen.getAllByText('Total tokens').length).toBeGreaterThan(0)
    expect(screen.getAllByText('1.24M').length).toBeGreaterThan(0)
    expect(screen.getByText('Est. cost')).toBeInTheDocument()
    expect(screen.getAllByText('$1.23').length).toBeGreaterThan(0)
  })

  it('renders the by-model + by-provider bar breakdowns and the per-agent table', () => {
    render(<AnalyticsView project="kaidera-os" usage={usage()} loading={false} error={null} />)
    expect(screen.getByText('Usage by model')).toBeInTheDocument()
    expect(screen.getByText('Usage by provider')).toBeInTheDocument()
    expect(screen.getByText('Usage by worker')).toBeInTheDocument()
    // "Ren" now appears in both the usage-by-agent table AND the cost-by-agent table.
    expect(screen.getAllByText('Ren').length).toBeGreaterThan(0)
  })

  it('shows a "not connected" state when the store is offline', () => {
    render(
      <AnalyticsView
        project="kaidera-os"
        usage={usage({ store_connected: false, total_runs: 0 })}
        loading={false}
        error={null}
      />,
    )
    expect(screen.getByText('store offline')).toBeInTheDocument()
    expect(screen.getByText(/usage store isn’t connected/i)).toBeInTheDocument()
  })

  it('shows a "no usage yet" empty state when connected but empty', () => {
    render(
      <AnalyticsView
        project="kaidera-os"
        usage={usage({ total_runs: 0, total_tokens: 0, total_tokens_h: null, rows: [], by_model_bars: [], by_provider_bars: [] })}
        loading={false}
        error={null}
      />,
    )
    expect(screen.getByText(/no usage recorded yet/i)).toBeInTheDocument()
  })

  it('shows a loading hint before any usage arrives', () => {
    render(<AnalyticsView project="kaidera-os" usage={null} loading error={null} />)
    expect(screen.getByText(/loading usage/i)).toBeInTheDocument()
  })

  it('shows an error hint (stale-backend 404) when usage fails to load', () => {
    render(
      <AnalyticsView project="kaidera-os" usage={null} loading={false} error={new Error('404')} />,
    )
    expect(screen.getByText(/couldn’t load usage/i)).toBeInTheDocument()
  })

  it('surfaces the by-model table + by-provider groups in an expandable section', async () => {
    const user = userEvent.setup()
    const modelRows: UsageModelRow[] = [
      { model: 'claude-opus-4-8[1m]', provider: 'Anthropic', tokens: 1_240_000, tokens_h: '1.24M' },
    ]
    const providerGroups: UsageProviderGroup[] = [
      {
        provider: 'anthropic',
        label: 'Anthropic',
        tokens: 1_240_000,
        tokens_h: '1.24M',
        models: [{ model: 'claude-opus-4-8[1m]', tokens: 1_240_000, tokens_h: '1.24M' }],
      },
    ]
    render(
      <AnalyticsView
        project="kaidera-os"
        usage={usage({ by_model_table: modelRows, by_provider: providerGroups })}
        loading={false}
        error={null}
      />,
    )
    // the detail is collapsed by default (a disclosure the operator expands)
    const toggle = screen.getByRole('button', { name: /model .* provider breakdown/i })
    expect(toggle).toBeInTheDocument()

    await user.click(toggle)
    // once expanded, the model table + the provider group totals are visible
    const region = screen.getByRole('region', { name: /model .* provider breakdown/i })
    // the model id appears in BOTH cuts (the by-model table + the provider group)
    expect(within(region).getAllByText('claude-opus-4-8[1m]').length).toBeGreaterThan(0)
    expect(within(region).getAllByText('Anthropic').length).toBeGreaterThan(0)
    expect(within(region).getAllByText('1.24M').length).toBeGreaterThan(0)
  })

  it('omits the breakdown disclosure when there is no model/provider data', () => {
    render(
      <AnalyticsView
        project="kaidera-os"
        usage={usage({ by_model_table: [], by_provider: [] })}
        loading={false}
        error={null}
      />,
    )
    expect(screen.queryByRole('button', { name: /model .* provider breakdown/i })).not.toBeInTheDocument()
  })

  // ---- the headline KPI strip (events/24h · active tasks · decisions · recent tokens) ----

  it('renders the headline KPI strip from the kpis payload', () => {
    render(
      <AnalyticsView project="kaidera-os" usage={usage()} kpis={kpis()} loading={false} error={null} />,
    )
    const strip = screen.getByTestId('analytics-kpis')
    // the four KPI labels …
    expect(within(strip).getByText(/events · 24h/i)).toBeInTheDocument()
    expect(within(strip).getByText(/active tasks/i)).toBeInTheDocument()
    expect(within(strip).getByText(/decisions · 7d/i)).toBeInTheDocument()
    expect(within(strip).getByText(/tokens · recent/i)).toBeInTheDocument()
    // … and their values.
    expect(within(strip).getByText('21')).toBeInTheDocument()
    expect(within(strip).getByText('5')).toBeInTheDocument()
    expect(within(strip).getByText('12')).toBeInTheDocument()
    expect(within(strip).getByText('1.24M')).toBeInTheDocument()
  })

  it('shows n/a for KPI counters that are null (degraded Cortex), never fabricated zeros', () => {
    render(
      <AnalyticsView
        project="kaidera-os"
        usage={usage()}
        kpis={kpis({ events_24h: null, active_tasks: null, decisions_recent: null, tokens_recent: 0, tokens_recent_h: null })}
        loading={false}
        error={null}
      />,
    )
    const strip = screen.getByTestId('analytics-kpis')
    // null counters degrade to n/a (not 0).
    expect(within(strip).getAllByText(/n\/a/i).length).toBeGreaterThan(0)
  })

  it('omits the KPI strip entirely when no kpis payload is supplied', () => {
    render(<AnalyticsView project="kaidera-os" usage={usage()} loading={false} error={null} />)
    expect(screen.queryByTestId('analytics-kpis')).not.toBeInTheDocument()
  })

  // ---- the elevated cost hero ----

  it('renders the cost hero with the project est-cost, total tokens, and priced-agent count', () => {
    render(
      <AnalyticsView project="kaidera-os" usage={usage()} kpis={kpis()} loading={false} error={null} />,
    )
    const hero = screen.getByTestId('analytics-cost-hero')
    // the prominent project est-cost …
    expect(within(hero).getByText('$1.23')).toBeInTheDocument()
    // … the total tokens + the priced-agent count summary.
    expect(within(hero).getByText('1.24M')).toBeInTheDocument()
    expect(within(hero).getByText(/1 agent/i)).toBeInTheDocument()
  })

  it('shows an n/a cost hero when the project has no priced usage', () => {
    render(
      <AnalyticsView
        project="kaidera-os"
        usage={usage({ project_cost: null, project_cost_h: 'n/a', priced_agent_count: 0 })}
        kpis={kpis()}
        loading={false}
        error={null}
      />,
    )
    const hero = screen.getByTestId('analytics-cost-hero')
    expect(within(hero).getByText('n/a')).toBeInTheDocument()
  })

  // ---- the per-agent cost table's project-total footer ----

  it('renders the per-agent cost table with a project-total footer row', () => {
    render(
      <AnalyticsView project="kaidera-os" usage={usage()} kpis={kpis()} loading={false} error={null} />,
    )
    const footer = screen.getByTestId('analytics-cost-total')
    expect(within(footer).getByText(/project total/i)).toBeInTheDocument()
    // the footer carries the summed project cost.
    expect(within(footer).getByText('$1.23')).toBeInTheDocument()
  })
})
