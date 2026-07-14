/**
 * GraphView — the "Graph" main-area tab: the interactive knowledge/code-graph (feature-gap
 * #80, the marquee surface).
 *
 * The Cortex knowledge graph (L4 entities + L3 code-graph edges) rendered as an actual
 * NODE-EDGE graph (cytoscape, bundled — NO CDN), not a list. Entities are nodes coloured by
 * `kind` (code = L3 file/fn · mem = L4 concept/decision · work = L1 handoff/task/agent),
 * relationships are edges. Pan/zoom; click a node to FOCUS it + highlight its 1-hop
 * neighbours (dimming the rest) and open an inspector; a SEARCH box re-centres the graph on
 * matching entities + their neighbours. A STATS header makes the bound explicit — the
 * backend ships only the search hits + 1-hop neighbours, capped at ~140 nodes, so the user
 * knows it's a bounded view of a big
 * graph.
 *
 * It COMPOSES with (does not replace) the Explain tab — both are codebase-visibility
 * surfaces; this is one more main-area tab alongside Agent · Dispatch · Analytics · Settings
 * · Explain · Graph.
 *
 * DATA: `client.graph(project)` (the seed view) + `client.graphSearch(project, q)` (a
 * re-centre), both → `{nodes, edges, stats}` (the backend's cytoscape-agnostic shape). The
 * canvas itself is built by an injectable `mountGraph` seam (the real `cytoscapeMount` by
 * default) so this component is testable without a (jsdom-impossible) WebGL canvas — tests
 * assert the data wiring + the controls, not pixels.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { GlassPanel } from '../components/glass'
import { cx } from '../components/ui'
import { cytoscapeMount } from './cytoscapeMount'
import type { GraphElement, GraphMountHandle, GraphMounter } from './cytoscapeMount'
import type { GraphNode, GraphPayload } from '../api'

export type { GraphMounter, GraphMountHandle } from './cytoscapeMount'

/** The slice of the api client GraphView needs — so tests fake one object. */
export interface GraphClient {
  graph: (project: string, signal?: AbortSignal) => Promise<GraphPayload>
  graphSearch: (project: string, q: string, signal?: AbortSignal) => Promise<GraphPayload>
  graphMemory?: (project: string, signal?: AbortSignal) => Promise<GraphPayload>
}

interface GraphViewProps {
  project: string | null
  client: GraphClient
  /** The cytoscape mounter (injectable for tests). Defaults to the real bundled mounter. */
  mountGraph?: GraphMounter
  /**
   * Optional compose-with-Explain hook: when set, a file/function node offers an "Explain
   * this" action that switches to the Explain tab pre-filled with the node's target. Wired
   * by MainArea when cheap; otherwise omitted (the action just doesn't render).
   */
  onExplainTarget?: (target: { kind: 'file' | 'blast'; value: string }) => void
}

/** The kinds that map to an Explain target (file → file path, function → blast radius). */
function explainTargetFor(node: GraphNode): { kind: 'file' | 'blast'; value: string } | null {
  const et = (node.etype || '').toLowerCase()
  if (et === 'file' || et === 'module') return { kind: 'file', value: node.full }
  if (et === 'function' || et === 'method' || et === 'class') return { kind: 'blast', value: node.full }
  return null
}

/** Build the cytoscape element array (nodes + edges) from a backend payload. */
function toElements(p: GraphPayload): GraphElement[] {
  const nodes: GraphElement[] = p.nodes.map((n) => ({
    data: {
      id: n.id,
      label: n.label,
      full: n.full,
      kind: n.kind,
      etype: n.etype,
      desc: n.desc,
      hit: n.hit,
      source_count: n.source_count,
    },
  }))
  const edges: GraphElement[] = p.edges.map((e) => ({
    data: { id: e.id, source: e.source, target: e.target, label: e.label },
  }))
  return [...nodes, ...edges]
}

function fmt(n: number | null | undefined): string {
  return typeof n === 'number' ? n.toLocaleString() : '—'
}

type LoadState = 'loading' | 'ready' | 'error'
type GraphMode = 'search' | 'memory'

interface MemoryFilters {
  hideAgents: boolean
  hideProjects: boolean
  hideTechnical: boolean
  minSources: number
}

const DEFAULT_MEMORY_FILTERS: MemoryFilters = {
  hideAgents: false,
  hideProjects: false,
  hideTechnical: false,
  minSources: 0,
}

const TECHNICAL_MEMORY_TYPES = new Set([
  'file',
  'module',
  'function',
  'method',
  'class',
  'callsite',
  'endpoint',
  'table',
  'code',
])

function applyMemoryFilters(payload: GraphPayload | null, filters: MemoryFilters): GraphPayload | null {
  if (!payload || payload.stats.mode !== 'memory') return payload
  const nodes = payload.nodes.filter((node) => {
    const etype = (node.etype || '').toLowerCase()
    if (filters.hideAgents && etype === 'agent') return false
    if (filters.hideProjects && etype === 'project') return false
    if (filters.hideTechnical && TECHNICAL_MEMORY_TYPES.has(etype)) return false
    const sourceCount = typeof node.source_count === 'number' ? node.source_count : 0
    if (filters.minSources > 0 && sourceCount < filters.minSources) return false
    return true
  })
  const kept = new Set(nodes.map((node) => node.id))
  const edges = payload.edges.filter((edge) => kept.has(edge.source) && kept.has(edge.target))
  const kindCounts = { code: 0, mem: 0, work: 0 }
  for (const node of nodes) {
    if (node.kind === 'code') kindCounts.code += 1
    else if (node.kind === 'work') kindCounts.work += 1
    else kindCounts.mem += 1
  }
  return {
    ...payload,
    nodes,
    edges,
    stats: {
      ...payload.stats,
      shown_nodes: nodes.length,
      shown_edges: edges.length,
      kind_counts: kindCounts,
      capped: false,
    },
  }
}

export function GraphView({ project, client, mountGraph = cytoscapeMount, onExplainTarget }: GraphViewProps) {
  const [data, setData] = useState<GraphPayload | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [mode, setMode] = useState<GraphMode>('search')
  const [term, setTerm] = useState('')
  const [draft, setDraft] = useState('')
  const [focused, setFocused] = useState<GraphNode | null>(null)
  const [memoryFilters, setMemoryFilters] = useState<MemoryFilters>(DEFAULT_MEMORY_FILTERS)

  const canvasRef = useRef<HTMLDivElement>(null)
  const handleRef = useRef<GraphMountHandle | null>(null)
  // The active fetch's abort controller (so a project switch / re-search cancels the prior).
  const acRef = useRef<AbortController | null>(null)
  // Which project the current view belongs to — drives the "reset on project switch" using
  // the React "adjust state during render" pattern (the same one MainArea uses), so the
  // project-change resets happen in render, NOT synchronously inside an effect (which would
  // trip the set-state-in-effect rule + cause a cascading render).
  const [viewProject, setViewProject] = useState<string | null>(project)
  if (project !== viewProject) {
    setViewProject(project)
    setDraft('')
    setMode('search')
    setData(null)
    setFocused(null)
    setState('loading')
  }

  const filteredData = useMemo(
    () => applyMemoryFilters(data, memoryFilters),
    [data, memoryFilters],
  )

  // A node lookup for the inspector (id → node).
  const nodesById = useMemo(() => {
    const m = new Map<string, GraphNode>()
    for (const n of filteredData?.nodes ?? []) m.set(n.id, n)
    return m
  }, [filteredData])

  const onNodeTap = useCallback(
    (id: string) => {
      setFocused(nodesById.get(id) ?? null)
    },
    [nodesById],
  )

  // Keep the latest project + client in refs so the fetch closure is STABLE (empty-deps
  // useCallback) — the same shape useResource uses, which keeps the set-state-in-effect
  // analysis happy (the effect calls an opaque stable callback, not an inlined setState path).
  const projectRef = useRef(project)
  const clientRef = useRef(client)
  const modeRef = useRef(mode)
  useEffect(() => {
    projectRef.current = project
    clientRef.current = client
    modeRef.current = mode
  })

  // The single fetch path (seed when q is blank, search otherwise). Always lands the surface
  // on a truthful state (loading → ready/error). The caller owns the AbortSignal so a project
  // switch / re-search can cancel the prior in-flight fetch. Stable identity (refs above).
  const run = useCallback((q: string, signal: AbortSignal) => {
    const proj = projectRef.current
    const c = clientRef.current
    if (!proj) return
    setState('loading')
    const term = q.trim()
    const useMemory = modeRef.current === 'memory'
    const p = useMemory && c.graphMemory
      ? c.graphMemory(proj, signal)
      : term ? c.graphSearch(proj, term, signal) : c.graph(proj, signal)
    p.then((payload) => {
      if (signal.aborted) return
      setData(payload)
      setTerm(term)
      setFocused(null)
      setState('ready')
    }).catch((e: unknown) => {
      if (signal.aborted || (e instanceof DOMException && e.name === 'AbortError')) return
      setState('error')
    })
  }, [])

  // Fire a fetch for `q`, replacing any in-flight one (its controller is aborted first).
  const load = useCallback(
    (q: string) => {
      acRef.current?.abort()
      const ac = new AbortController()
      acRef.current = ac
      run(q, ac.signal)
    },
    [run],
  )

  // (Re)load the seed graph whenever the project changes. The synchronous resets happen in
  // render (above); this effect kicks off the async fetch via the stable `run` callback and
  // aborts the in-flight fetch on unmount / project change.
  useEffect(() => {
    if (!project) return
    const ac = new AbortController()
    acRef.current = ac
    run('', ac.signal)
    return () => ac.abort()
  }, [project, run])

  // (Re)mount the cytoscape canvas whenever the data changes. The mounter is torn down
  // first (project switch / re-search), so there's never a leaked instance.
  useEffect(() => {
    const container = canvasRef.current
    if (!container || !filteredData || filteredData.nodes.length === 0) {
      handleRef.current?.destroy()
      handleRef.current = null
      return
    }
    handleRef.current?.destroy()
    handleRef.current = mountGraph(container, {
      elements: toElements(filteredData),
      onNodeTap,
      onBackgroundTap: () => setFocused(null),
    })
    return () => {
      handleRef.current?.destroy()
      handleRef.current = null
    }
  }, [filteredData, mountGraph, onNodeTap])

  const onSearch = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault()
      modeRef.current = 'search'
      setMode('search')
      load(draft)
    },
    [draft, load],
  )

  const showMemoryGraph = useCallback(() => {
    modeRef.current = 'memory'
    setMode('memory')
    load('')
  }, [load])

  const showSearchGraph = useCallback(() => {
    modeRef.current = 'search'
    setMode('search')
    load(draft)
  }, [draft, load])

  if (!project) {
    return (
      <GlassPanel className="flex-1">
        <div className="flex h-full items-center justify-center p-10">
          <p className="text-sm text-ink-500">Select a project to explore its knowledge graph.</p>
        </div>
      </GlassPanel>
    )
  }

  const stats = filteredData?.stats
  const kc = stats?.kind_counts ?? { code: 0, mem: 0, work: 0 }
  const layers = stats?.layers ?? []
  const explainTarget = focused ? explainTargetFor(focused) : null
  const graphHasHiddenData = Boolean(
    stats
      && filteredData
      && filteredData.nodes.length === 0
      && ((stats.total_shown_nodes ?? 0) > 0 || (stats.entity_count ?? 0) > 0),
  )

  return (
    <GlassPanel className="min-w-0 flex-1">
      <div className="flex h-full min-h-0 flex-col">
        {/* ---------- header: title + search ---------- */}
        <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-glass-line px-5 py-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-ink-100">Knowledge graph</h2>
            <p className="text-[11px] text-ink-500">
              {mode === 'memory'
                ? 'Project memory graph · all current L4 entities, browser-bounded'
                : 'L3 code graph + L4 entities · a bounded neighbourhood'}
            </p>
          </div>
          <div className="flex items-center gap-1 rounded-lg border border-glass-line bg-base-900/35 p-0.5">
            <button
              type="button"
              aria-pressed={mode === 'search'}
              onClick={showSearchGraph}
              className={cx(
                'rounded-md px-2.5 py-1 text-[11px] font-semibold transition-colors',
                mode === 'search' ? 'bg-mint-500/15 text-mint-200' : 'text-ink-500 hover:text-ink-200',
              )}
            >
              Search graph
            </button>
            <button
              type="button"
              aria-pressed={mode === 'memory'}
              onClick={showMemoryGraph}
              className={cx(
                'rounded-md px-2.5 py-1 text-[11px] font-semibold transition-colors',
                mode === 'memory' ? 'bg-mint-500/15 text-mint-200' : 'text-ink-500 hover:text-ink-200',
              )}
            >
              Memory graph
            </button>
          </div>
          <form className="ml-auto flex items-center gap-2" role="search" onSubmit={onSearch}>
            <input
              type="search"
              aria-label="Search the knowledge graph"
              placeholder="Search entities, files, concepts…"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              spellCheck={false}
              autoComplete="off"
              className="glass-soft w-64 rounded-lg bg-base-800/40 px-3 py-1.5 text-[12.5px] text-ink-100 placeholder:text-ink-600 focus:outline-none focus:ring-1 focus:ring-mint-400/40"
            />
            <button
              type="submit"
              className="rounded-lg bg-mint-500/15 px-3 py-1.5 text-xs font-semibold text-mint-200 ring-1 ring-mint-400/40 transition-colors hover:bg-mint-500/25"
            >
              Search
            </button>
          </form>
        </header>

        {/* ---------- stats strip ---------- */}
        <div
          data-testid="graph-stats"
          className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-1.5 border-b border-glass-line px-5 py-2.5 text-[11px]"
        >
          <Stat label={`Nodes · ${project}`} value={fmt(stats?.own_nodes)} />
          <Stat label={`Edges · ${project}`} value={fmt(stats?.own_edges)} />
          <Stat label="Nodes · all repos" value={fmt(stats?.total_nodes)} muted />
          <Stat label="Edges · all repos" value={fmt(stats?.total_edges)} muted />
          <Stat label="Memory entities" value={fmt(stats?.entity_count)} muted />
          <Stat label="Repos" value={fmt(stats?.repo_count)} muted />
          <span className="ml-auto inline-flex items-center gap-1 text-ink-400">
            <span className="text-ink-300">
              Showing <b className="tabular-nums text-ink-100">{fmt(stats?.shown_nodes)}</b>
              {stats?.total_shown_nodes != null && (
                <> of <b className="tabular-nums text-ink-100">{fmt(stats.total_shown_nodes)}</b></>
              )}{' '}
              nodes · <b className="tabular-nums text-ink-100">{fmt(stats?.shown_edges)}</b> edges
            </span>
            {stats?.capped ? (
              <span className="rounded-full bg-run-queued/15 px-2 py-0.5 text-[10px] font-medium text-run-queued">
                capped at {stats.node_cap} — search to explore
              </span>
            ) : (
              <span className="text-ink-500">· bounded neighbourhood</span>
            )}
          </span>
        </div>

        {/* ---------- legend ---------- */}
        <div
          data-testid="graph-legend"
          className="flex shrink-0 items-center gap-4 border-b border-glass-line px-5 py-1.5 text-[10.5px] text-ink-400"
        >
          <LegendSwatch testid="legend-code" colorClass="bg-run-completed" label="Code & files" count={kc.code} tier="L3" />
          <LegendSwatch testid="legend-mem" colorClass="bg-mint-400" label="Memory" count={kc.mem} tier="L4" />
          <LegendSwatch testid="legend-work" colorClass="bg-run-queued" label="Work & handoffs" count={kc.work} tier="L1" />
          <span className="ml-auto inline-flex items-center gap-1.5 text-ink-500">
            <span className="h-px w-5 bg-ink-500/60" /> relationship edge
          </span>
        </div>

        {mode === 'memory' && (
          <div
            data-testid="graph-memory-filters"
            className="flex shrink-0 flex-wrap items-center gap-3 border-b border-glass-line px-5 py-2 text-[10.5px] text-ink-400"
          >
            <span className="font-semibold uppercase tracking-wide text-ink-500">Memory filters</span>
            <Toggle
              label="Hide agents"
              checked={memoryFilters.hideAgents}
              onChange={(checked) => setMemoryFilters((prev) => ({ ...prev, hideAgents: checked }))}
            />
            <Toggle
              label="Hide projects"
              checked={memoryFilters.hideProjects}
              onChange={(checked) => setMemoryFilters((prev) => ({ ...prev, hideProjects: checked }))}
            />
            <Toggle
              label="Hide technical refs"
              checked={memoryFilters.hideTechnical}
              onChange={(checked) => setMemoryFilters((prev) => ({ ...prev, hideTechnical: checked }))}
            />
            <label className="inline-flex items-center gap-1.5">
              Source refs
              <select
                value={memoryFilters.minSources}
                onChange={(e) => setMemoryFilters((prev) => ({ ...prev, minSources: Number(e.target.value) }))}
                className="rounded-md border border-glass-line bg-base-900/70 px-2 py-1 text-[10.5px] text-ink-200 outline-none"
              >
                <option value={0}>all</option>
                <option value={2}>2+</option>
                <option value={3}>3+</option>
              </select>
            </label>
            <button
              type="button"
              onClick={() => setMemoryFilters(DEFAULT_MEMORY_FILTERS)}
              className="ml-auto rounded-md border border-glass-line px-2 py-1 text-[10.5px] font-semibold text-ink-400 hover:bg-base-800/70 hover:text-ink-200"
            >
              Reset filters
            </button>
          </div>
        )}

        {layers.length > 0 && (
          <div
            data-testid="graph-layers"
            className="grid shrink-0 grid-cols-2 gap-1.5 border-b border-glass-line px-5 py-2 text-[10.5px] sm:grid-cols-3 xl:grid-cols-6"
          >
            {layers.map((layer) => (
              <LayerPill key={layer.id} layer={layer} />
            ))}
          </div>
        )}

        {/* ---------- stage: canvas + inspector ---------- */}
        <div className="relative min-h-0 flex-1">
          {/* the cytoscape mount (filled by the mounter) */}
          <div ref={canvasRef} className="h-full w-full" data-testid="graph-canvas" />

          {/* loading overlay */}
          {state === 'loading' && (
            <Overlay testid="graph-loading">
              <p className="text-sm text-ink-400">Loading the graph…</p>
            </Overlay>
          )}

          {/* error overlay */}
          {state === 'error' && (
            <Overlay testid="graph-error">
              <p className="text-sm font-medium text-run-errored">The knowledge graph could not be loaded.</p>
              <p className="mt-1 max-w-md text-xs text-ink-500">
                Cortex graph search is unreachable, or the project has no graph data yet. Try again, or a
                different term.
              </p>
              <button
                type="button"
                onClick={() => load(term)}
                className="mt-3 rounded-lg bg-base-700/60 px-3 py-1.5 text-xs font-medium text-ink-200 hover:bg-base-700"
              >
                Retry
              </button>
            </Overlay>
          )}

          {/* empty overlay */}
          {state === 'ready' && filteredData && filteredData.nodes.length === 0 && (
            <Overlay testid="graph-empty">
              <p className="text-sm font-medium text-ink-300">
                {graphHasHiddenData ? 'Graph data exists, but nothing is renderable yet.' : `No graph for “${term || 'this seed'}”.`}
              </p>
              <p className="mt-1 max-w-md text-xs text-ink-500">
                {graphHasHiddenData
                  ? `Cortex reports ${fmt(stats?.total_shown_nodes ?? stats?.entity_count)} memory/code nodes, but this bounded view returned zero renderable nodes. Reset filters, switch mode, or run graph extraction.`
                  : 'No entities or relationships came back from Cortex graph search. Try a different term, a file path, an entity name, or a handoff id.'}
              </p>
            </Overlay>
          )}

          {/* canvas toolbar (top-left): fit · re-layout · zoom */}
          {state === 'ready' && filteredData && filteredData.nodes.length > 0 && (
            <div className="absolute left-3 top-3 flex items-center gap-1">
              <ToolBtn label="Fit" onClick={() => handleRef.current?.fit()} />
              <ToolBtn label="Re-layout" onClick={() => handleRef.current?.relayout()} />
              <ToolBtn label="+" aria="Zoom in" onClick={() => handleRef.current?.zoomBy(1.3)} />
              <ToolBtn label="−" aria="Zoom out" onClick={() => handleRef.current?.zoomBy(1 / 1.3)} />
            </div>
          )}

          {/* node inspector (right) — populated on node focus */}
          {focused && (
            <aside
              data-testid="graph-inspector"
              className="glass absolute right-3 top-3 bottom-3 flex w-64 flex-col gap-2 overflow-hidden rounded-xl p-3"
            >
              <div className="flex items-start gap-2">
                <span className={cx('mt-1 h-2.5 w-2.5 shrink-0 rounded-full', kindDotClass(focused.kind))} />
                <div className="min-w-0 flex-1">
                  <div className="break-words text-[12.5px] font-semibold text-ink-100">{focused.full}</div>
              <div className="mt-0.5 text-[10px] uppercase tracking-wide text-ink-500">
                {kindLabel(focused.kind)} · {focused.etype}
                {typeof focused.source_count === 'number' && <> · {focused.source_count} source refs</>}
              </div>
                </div>
                <button
                  type="button"
                  aria-label="Close inspector"
                  onClick={() => setFocused(null)}
                  className="shrink-0 rounded-md p-1 text-ink-500 hover:bg-base-800/60 hover:text-ink-200"
                >
                  <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
                    <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
                  </svg>
                </button>
              </div>
              {focused.desc && (
                <p className="overflow-y-auto text-[11px] leading-relaxed text-ink-400">{focused.desc}</p>
              )}
              <div className="mt-auto flex flex-col gap-1.5">
                <button
                  type="button"
                  onClick={() => handleRef.current?.focusNode(focused.id)}
                  className="rounded-lg bg-base-700/60 px-2.5 py-1.5 text-[11px] font-medium text-ink-200 hover:bg-base-700"
                >
                  Re-centre on this node
                </button>
                {onExplainTarget && explainTarget && (
                  <button
                    type="button"
                    onClick={() => onExplainTarget(explainTarget)}
                    className="rounded-lg bg-mint-500/15 px-2.5 py-1.5 text-[11px] font-semibold text-mint-200 ring-1 ring-mint-400/40 hover:bg-mint-500/25"
                  >
                    Explain this {explainTarget.kind === 'file' ? 'file' : 'function'}
                  </button>
                )}
              </div>
            </aside>
          )}
        </div>
      </div>
    </GlassPanel>
  )
}

// ---------------------------------------------------------------------------
//  small presentational bits
// ---------------------------------------------------------------------------

function Stat({ label, value, muted = false }: { label: string; value: string; muted?: boolean }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <b className={cx('tabular-nums', muted ? 'text-ink-300' : 'text-ink-100')}>{value}</b>
      <span className="text-[10px] uppercase tracking-wide text-ink-500">{label}</span>
    </span>
  )
}

function LegendSwatch({
  testid,
  colorClass,
  label,
  count,
  tier,
}: {
  testid: string
  colorClass: string
  label: string
  count: number
  tier: string
}) {
  return (
    <span data-testid={testid} className="inline-flex items-center gap-1.5">
      <span className={cx('h-2.5 w-2.5 rounded-full', colorClass)} />
      {label} <b className="tabular-nums text-ink-200">{count}</b>
      <span className="text-[9px] uppercase tracking-wide text-ink-600">{tier}</span>
    </span>
  )
}

function LayerPill({ layer }: { layer: NonNullable<GraphPayload['stats']['layers']>[number] }) {
  const countBits = [
    typeof layer.count === 'number' ? fmt(layer.count) : null,
    typeof layer.edges === 'number' ? `${fmt(layer.edges)} edges` : null,
    typeof layer.backlog === 'number' && layer.backlog > 0 ? `${fmt(layer.backlog)} backlog` : null,
  ].filter(Boolean)
  return (
    <span
      title={layer.detail ?? undefined}
      className="glass-soft min-w-0 rounded-lg px-2.5 py-1.5"
    >
      <span className="flex items-center gap-1.5">
        <b className="shrink-0 text-[10px] uppercase tracking-wide text-ink-500">{layer.id}</b>
        <span className="min-w-0 truncate font-semibold text-ink-200">{layer.name}</span>
        <span className={cx('ml-auto h-2 w-2 shrink-0 rounded-full', layerStatusClass(layer.status))} />
      </span>
      <span className="mt-0.5 block truncate text-[10px] text-ink-500">
        {layer.status}
        {countBits.length > 0 ? ` · ${countBits.join(' · ')}` : ''}
      </span>
    </span>
  )
}

function layerStatusClass(status: string): string {
  const s = (status || '').toLowerCase()
  if (s === 'ready' || s === 'configured' || s === 'observed') return 'bg-run-completed'
  if (s === 'backlog' || s === 'missing') return 'bg-run-queued'
  if (s === 'empty' || s === 'not observed') return 'bg-ink-600'
  return 'bg-run-running'
}

function Overlay({ testid, children }: { testid: string; children: React.ReactNode }) {
  return (
    <div
      data-testid={testid}
      className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center bg-base-950/40 p-10 text-center"
    >
      <div className="pointer-events-auto">{children}</div>
    </div>
  )
}

function ToolBtn({ label, aria, onClick }: { label: string; aria?: string; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={aria ?? label}
      onClick={onClick}
      className="glass-soft rounded-md px-2 py-1 text-[11px] font-medium text-ink-300 hover:bg-base-800/70 hover:text-ink-100"
    >
      {label}
    </button>
  )
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  return (
    <label className="inline-flex items-center gap-1.5">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-3.5 w-3.5 rounded border-glass-line bg-base-900 text-mint-400"
      />
      {label}
    </label>
  )
}

function kindDotClass(kind: string): string {
  if (kind === 'code') return 'bg-run-completed'
  if (kind === 'work') return 'bg-run-queued'
  return 'bg-mint-400'
}

function kindLabel(kind: string): string {
  if (kind === 'code') return 'Code'
  if (kind === 'work') return 'Work'
  return 'Memory'
}
