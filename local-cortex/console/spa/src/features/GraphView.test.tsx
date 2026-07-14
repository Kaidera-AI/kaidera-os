import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GraphView } from './GraphView'
import type { GraphClient, GraphMounter, GraphMountHandle } from './GraphView'
import type { GraphPayload } from '../api'

// A representative bounded payload: 2 code nodes, 1 mem, 1 work, 3 edges + stats.
function payload(over: Partial<GraphPayload> = {}): GraphPayload {
  return {
    nodes: [
      { id: 'app/main.py', label: 'app/main.py', full: 'app/main.py', kind: 'code', etype: 'file', desc: 'the app', hit: 1 },
      { id: 'orchestrator.py', label: 'orchestrator.py', full: 'orchestrator.py', kind: 'code', etype: 'file', desc: '', hit: 0 },
      { id: 'dispatch flow', label: 'dispatch flow', full: 'dispatch flow', kind: 'mem', etype: 'concept', desc: 'the flow', hit: 1 },
      { id: 'abcd:5872', label: 'abcd:5872', full: 'abcd:5872', kind: 'work', etype: 'handoff', desc: '', hit: 1 },
    ],
    edges: [
      { id: 'e0', source: 'app/main.py', target: 'dispatch flow', label: 'defines' },
      { id: 'e1', source: 'app/main.py', target: 'orchestrator.py', label: 'imports' },
      { id: 'e2', source: 'dispatch flow', target: 'abcd:5872', label: 'tracked_by' },
    ],
    stats: {
      own_nodes: 5868,
      own_edges: 44000,
      total_nodes: 7068,
      total_edges: 53000,
      repo_count: 2,
      repos: [
        { name: 'kaidera-os', nodes: 5868, edges: 44000, is_own: true },
        { name: 'kaidera', nodes: 1200, edges: 9000, is_own: false },
      ],
      shown_nodes: 4,
      shown_edges: 3,
      total_shown_nodes: 5868,
      kind_counts: { code: 2, mem: 1, work: 1 },
      entity_count: 42,
      relationship_count: 99,
      source_counts: { decisions: 10, lessons: 2, knowledge: 30, work_products: 1 },
      backlog: { decisions: 3, lessons: 0, knowledge: 1, work_products: 0 },
      layers: [
        { id: 'L1', name: 'Operational memory', status: 'observed', count: 42, detail: 'memory rows' },
        { id: 'L2', name: 'Vector retrieval', status: 'configured', detail: 'openrouter · embed' },
        { id: 'L3', name: 'Code graph', status: 'ready', count: 5868, edges: 44000 },
        { id: 'L4', name: 'Entity graph', status: 'ready', count: 42, edges: 99, backlog: 4 },
        { id: 'L5', name: 'Work products', status: 'observed', count: 1 },
        { id: 'L6', name: 'Runtime boot', status: 'configured', detail: 'project registry' },
      ],
      node_cap: 140,
      capped: false,
    },
    ...over,
  }
}

/**
 * A FAKE cytoscape mounter: captures the elements it was handed (so we assert the data
 * wiring without a real canvas), and exposes a controllable focus callback so the test can
 * drive node-focus. Returns a no-op handle. Cytoscape itself never mounts in jsdom.
 */
function fakeMounter() {
  const calls: { elements: unknown[]; nodeCount: number }[] = []
  let onNodeFocus: ((id: string) => void) | null = null
  const mount: GraphMounter = (_container, { elements, onNodeTap }) => {
    const nodes = (elements as { data: { source?: string } }[]).filter((e) => !e.data.source)
    calls.push({ elements, nodeCount: nodes.length })
    onNodeFocus = onNodeTap ?? null
    const handle: GraphMountHandle = {
      destroy: vi.fn(),
      fit: vi.fn(),
      focusNode: vi.fn(),
      relayout: vi.fn(),
      zoomBy: vi.fn(),
    }
    return handle
  }
  return {
    mount,
    calls,
    tapNode: (id: string) => onNodeFocus?.(id),
  }
}

function fakeClient(over: Partial<GraphClient> = {}): GraphClient {
  return {
    graph: vi.fn<(...a: unknown[]) => Promise<GraphPayload>>().mockResolvedValue(payload()),
    graphSearch: vi.fn<(...a: unknown[]) => Promise<GraphPayload>>().mockResolvedValue(payload()),
    graphMemory: vi.fn<(...a: unknown[]) => Promise<GraphPayload>>().mockResolvedValue(payload({
      stats: { ...payload().stats, mode: 'memory' },
    })),
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('GraphView — load + render', () => {
  it('fetches the seed graph for the project on mount and hands the elements to cytoscape', async () => {
    const m = fakeMounter()
    const client = fakeClient()
    render(<GraphView project="kaidera-os" client={client} mountGraph={m.mount} />)

    await waitFor(() => expect(client.graph).toHaveBeenCalledWith('kaidera-os', expect.anything()))
    // Cytoscape sets its mount element to position:relative. Explicit dimensions keep
    // the real canvas from collapsing to 0px after that inline-style mutation.
    expect(screen.getByTestId('graph-canvas')).toHaveClass('h-full', 'w-full')
    // The shaped nodes+edges were handed to the (fake) cytoscape mounter as elements.
    await waitFor(() => expect(m.calls.length).toBeGreaterThan(0))
    expect(m.calls[0].nodeCount).toBe(4) // 4 nodes in the payload
    // The elements carry the cytoscape data shape (a node's id + kind, an edge's source/target).
    const els = m.calls[0].elements as { data: Record<string, unknown> }[]
    const mainNode = els.find((e) => e.data.id === 'app/main.py')
    expect(mainNode?.data.kind).toBe('code')
    const edge = els.find((e) => e.data.source === 'app/main.py' && e.data.target === 'orchestrator.py')
    expect(edge?.data.label).toBe('imports')
  })

  it('shows the stats header with own/total/rendered counts', async () => {
    render(<GraphView project="kaidera-os" client={fakeClient()} mountGraph={fakeMounter().mount} />)
    // own nodes (5,868), total nodes (7,068), and the rendered "showing N" count all surface
    // (formatted). The own-nodes value also appears as the "of 5,868" rendered total, so we
    // assert the header simply CONTAINS each — getAllByText (>=1) avoids the duplicate trap.
    const header = await screen.findByTestId('graph-stats')
    expect(within(header).getAllByText(/5,868/).length).toBeGreaterThanOrEqual(1) // own nodes
    expect(within(header).getByText(/7,068/)).toBeInTheDocument() // total nodes
    // rendered count ("showing N") — 4 nodes — and the per-repo "Repos" count (2).
    expect(header.textContent).toMatch(/Showing/)
    expect(header.textContent).toContain('4')
  })

  it('renders the node-kind legend with the per-kind counts (colour mapping applied)', async () => {
    const m = fakeMounter()
    render(<GraphView project="kaidera-os" client={fakeClient()} mountGraph={m.mount} />)
    await waitFor(() => expect(m.calls.length).toBeGreaterThan(0))
    // The legend reflects kind_counts: code 2, mem 1, work 1.
    const legend = screen.getByTestId('graph-legend')
    expect(within(legend).getByTestId('legend-code')).toHaveTextContent('2')
    expect(within(legend).getByTestId('legend-mem')).toHaveTextContent('1')
    expect(within(legend).getByTestId('legend-work')).toHaveTextContent('1')
    // The mounter received a per-kind colour map so nodes are coloured by kind (the SPA
    // owns the palette, not the backend).
    const opts = m.calls[0] as unknown as { elements: unknown[] }
    expect(opts.elements.length).toBe(7) // 4 nodes + 3 edges
  })

  it('renders the Cortex L1-L6 layer status strip', async () => {
    render(<GraphView project="kaidera-os" client={fakeClient()} mountGraph={fakeMounter().mount} />)
    const layers = await screen.findByTestId('graph-layers')
    expect(within(layers).getByText('L1')).toBeInTheDocument()
    expect(within(layers).getByText('L6')).toBeInTheDocument()
    expect(within(layers).getByText('Entity graph')).toBeInTheDocument()
    expect(layers.textContent).toContain('4 backlog')
  })

  it('can switch to the project memory graph', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<GraphView project="kaidera-os" client={client} mountGraph={fakeMounter().mount} />)
    await waitFor(() => expect(client.graph).toHaveBeenCalled())

    await user.click(screen.getByRole('button', { name: /memory graph/i }))

    await waitFor(() => expect(client.graphMemory).toHaveBeenCalledWith('kaidera-os', expect.anything()))
    expect(await screen.findByText(/Project memory graph/i)).toBeInTheDocument()
    expect(await screen.findByTestId('graph-memory-filters')).toBeInTheDocument()
  })

  it('filters noisy project memory nodes without refetching', async () => {
    const user = userEvent.setup()
    const m = fakeMounter()
    const memory = payload({
      nodes: [
        { id: 'Marlow', label: 'Marlow', full: 'Marlow', kind: 'work', etype: 'agent', desc: '', hit: 1, source_count: 3 },
        { id: 'marketing', label: 'marketing', full: 'marketing', kind: 'mem', etype: 'project', desc: '', hit: 1, source_count: 5 },
        { id: 'runbook.md', label: 'runbook.md', full: 'runbook.md', kind: 'code', etype: 'file', desc: '', hit: 1, source_count: 1 },
        { id: 'Publishing cadence', label: 'Publishing cadence', full: 'Publishing cadence', kind: 'mem', etype: 'concept', desc: '', hit: 1, source_count: 2 },
      ],
      edges: [
        { id: 'e0', source: 'Marlow', target: 'Publishing cadence', label: 'owns' },
        { id: 'e1', source: 'runbook.md', target: 'Publishing cadence', label: 'documents' },
      ],
      stats: { ...payload().stats, mode: 'memory', shown_nodes: 4, shown_edges: 2 },
    })
    const client = fakeClient({
      graphMemory: vi.fn<(...a: unknown[]) => Promise<GraphPayload>>().mockResolvedValue(memory),
    })
    render(<GraphView project="kaidera-os" client={client} mountGraph={m.mount} />)
    await waitFor(() => expect(client.graph).toHaveBeenCalled())

    await user.click(screen.getByRole('button', { name: /memory graph/i }))
    await waitFor(() => expect(m.calls[m.calls.length - 1].nodeCount).toBe(4))
    const filters = await screen.findByTestId('graph-memory-filters')

    await user.click(within(filters).getByLabelText(/hide technical refs/i))

    await waitFor(() => expect(m.calls[m.calls.length - 1].nodeCount).toBe(3))
    expect(client.graphMemory).toHaveBeenCalledTimes(1)
  })
})

describe('GraphView — search re-centre', () => {
  it('search box calls graphSearch with the term and re-mounts the result', async () => {
    const user = userEvent.setup()
    const m = fakeMounter()
    const searched = payload({
      nodes: [{ id: 'cortex_client.py', label: 'cortex_client.py', full: 'cortex_client.py', kind: 'code', etype: 'file', desc: '', hit: 1 }],
      edges: [],
      stats: { ...payload().stats, shown_nodes: 1, shown_edges: 0, kind_counts: { code: 1, mem: 0, work: 0 } },
    })
    const client = fakeClient({
      graphSearch: vi.fn<(...a: unknown[]) => Promise<GraphPayload>>().mockResolvedValue(searched),
    })
    render(<GraphView project="kaidera-os" client={client} mountGraph={m.mount} />)
    await waitFor(() => expect(client.graph).toHaveBeenCalled())

    const box = screen.getByRole('searchbox', { name: /search the/i })
    await user.type(box, 'cortex client')
    await user.click(screen.getByRole('button', { name: /^search$/i }))

    await waitFor(() => expect(client.graphSearch).toHaveBeenCalledWith('kaidera-os', 'cortex client', expect.anything()))
    // The re-centred graph was re-handed to the canvas (1 node now).
    await waitFor(() => expect(m.calls[m.calls.length - 1].nodeCount).toBe(1))
  })
})

describe('GraphView — node focus (1-hop)', () => {
  it('tapping a node calls the mount handle focusNode + populates the inspector', async () => {
    const m = fakeMounter()
    render(<GraphView project="kaidera-os" client={fakeClient()} mountGraph={m.mount} />)
    await waitFor(() => expect(m.calls.length).toBeGreaterThan(0))

    // Simulate cytoscape reporting a node tap (the canvas can't be driven in jsdom).
    m.tapNode('app/main.py')

    // The inspector opens with the focused node's identity.
    const insp = await screen.findByTestId('graph-inspector')
    expect(within(insp).getByText('app/main.py')).toBeInTheDocument()
  })
})

describe('GraphView — compose with Explain', () => {
  it('offers "Explain this file" on a file node and fires onExplainTarget', async () => {
    const user = userEvent.setup()
    const m = fakeMounter()
    const onExplainTarget = vi.fn()
    render(
      <GraphView project="kaidera-os" client={fakeClient()} mountGraph={m.mount} onExplainTarget={onExplainTarget} />,
    )
    await waitFor(() => expect(m.calls.length).toBeGreaterThan(0))
    // Focus the file node (etype 'file') → the inspector offers "Explain this file".
    m.tapNode('app/main.py')
    const btn = await screen.findByRole('button', { name: /explain this file/i })
    await user.click(btn)
    expect(onExplainTarget).toHaveBeenCalledWith({ kind: 'file', value: 'app/main.py' })
  })

  it('does NOT offer Explain on a non-code node (a handoff)', async () => {
    const m = fakeMounter()
    render(
      <GraphView project="kaidera-os" client={fakeClient()} mountGraph={m.mount} onExplainTarget={vi.fn()} />,
    )
    await waitFor(() => expect(m.calls.length).toBeGreaterThan(0))
    m.tapNode('abcd:5872') // a handoff (work) node — no Explain target
    await screen.findByTestId('graph-inspector')
    expect(screen.queryByRole('button', { name: /explain this/i })).not.toBeInTheDocument()
  })

  it('omits the Explain action entirely when no onExplainTarget is wired', async () => {
    const m = fakeMounter()
    render(<GraphView project="kaidera-os" client={fakeClient()} mountGraph={m.mount} />)
    await waitFor(() => expect(m.calls.length).toBeGreaterThan(0))
    m.tapNode('app/main.py')
    await screen.findByTestId('graph-inspector')
    expect(screen.queryByRole('button', { name: /explain this/i })).not.toBeInTheDocument()
  })
})

describe('GraphView — states', () => {
  it('shows a no-project hint when no project is selected', () => {
    render(<GraphView project={null} client={fakeClient()} mountGraph={fakeMounter().mount} />)
    expect(screen.getByText(/select a project/i)).toBeInTheDocument()
  })

  it('shows an empty state when the graph comes back with no nodes', async () => {
    const empty = payload({
      nodes: [],
      edges: [],
      stats: { ...payload().stats, shown_nodes: 0, shown_edges: 0, kind_counts: { code: 0, mem: 0, work: 0 } },
    })
    const client = fakeClient({
      graph: vi.fn<(...a: unknown[]) => Promise<GraphPayload>>().mockResolvedValue(empty),
    })
    render(<GraphView project="kaidera-os" client={client} mountGraph={fakeMounter().mount} />)
    expect(await screen.findByTestId('graph-empty')).toBeInTheDocument()
  })

  it('shows an error state when the fetch fails', async () => {
    const client = fakeClient({
      graph: vi.fn<(...a: unknown[]) => Promise<GraphPayload>>().mockRejectedValue(new Error('graph down')),
    })
    render(<GraphView project="kaidera-os" client={client} mountGraph={fakeMounter().mount} />)
    expect(await screen.findByTestId('graph-error')).toBeInTheDocument()
  })

  it('shows the capped nudge when the bounded view clipped at the node cap', async () => {
    const capped = payload({
      stats: { ...payload().stats, shown_nodes: 140, capped: true, total_shown_nodes: 5868 },
    })
    const client = fakeClient({
      graph: vi.fn<(...a: unknown[]) => Promise<GraphPayload>>().mockResolvedValue(capped),
    })
    render(<GraphView project="kaidera-os" client={client} mountGraph={fakeMounter().mount} />)
    const header = await screen.findByTestId('graph-stats')
    expect(within(header).getByText(/capped|search to explore/i)).toBeInTheDocument()
  })
})
