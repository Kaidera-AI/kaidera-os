/**
 * The REAL cytoscape mounter — the single seam where the node-edge canvas is built.
 *
 * Isolated in its own module so `GraphView` (and its tests) never import cytoscape: the
 * component takes a `GraphMounter` (this is the production default), and tests inject a fake
 * that captures the elements instead of mounting a (jsdom-impossible) WebGL/canvas graph.
 *
 * Ports the legacy `_graph.html` cytoscape init verbatim-in-spirit: a force-directed (cose)
 * layout, nodes coloured by `kind` via the node-kind palette (search hits drawn larger),
 * relationship edges, pan/zoom, and click-to-focus that highlights a node's 1-hop
 * neighbourhood (dimming the rest). The palette + interaction match the prototype so the SPA
 * graph echoes the original UX. Cytoscape is BUNDLED (npm), NO CDN.
 */

import cytoscape from 'cytoscape'
import type { Core, ElementDefinition, NodeSingular } from 'cytoscape'

/** The cytoscape-element shape the SPA builds from the backend nodes/edges. */
export type GraphElement = ElementDefinition

/** What a mount call hands back so the component can drive the canvas (fit/focus/zoom). */
export interface GraphMountHandle {
  destroy: () => void
  fit: () => void
  /** Focus a node by id + highlight its 1-hop neighbours (dim the rest). */
  focusNode: (id: string) => void
  relayout: () => void
  /** Zoom by a factor about the canvas centre (>1 in, <1 out). */
  zoomBy: (factor: number) => void
}

/** The options a mounter receives. `onNodeTap` reports a canvas node-tap up to the component. */
export interface GraphMountOptions {
  elements: GraphElement[]
  /** Called with a node id when the user taps a node on the canvas (drives the inspector). */
  onNodeTap?: (id: string) => void
  /** Called when the user taps the empty background (clears the inspector). */
  onBackgroundTap?: () => void
}

/** The injectable seam: build a graph in `container` and return a control handle. */
export type GraphMounter = (
  container: HTMLElement,
  options: GraphMountOptions,
) => GraphMountHandle

// ---- node-kind palette (mirrors the legacy _graph.html KIND map + the dark base) ----
const KIND: Record<string, { fill: string; line: string }> = {
  code: { fill: '#1b2a38', line: '#5b8fd6' },
  mem: { fill: '#13241f', line: '#43e0b6' },
  work: { fill: '#2a2415', line: '#e8c45a' },
}
const ACCENT = '#43e0b6'
const EDGE_INK = 'rgba(122,196,176,0.35)'

function kindOf(n: NodeSingular): { fill: string; line: string } {
  return KIND[n.data('kind') as string] || KIND.mem
}

/**
 * The production cytoscape mounter. Builds the graph, wires pan/zoom + tap-to-focus, and
 * returns a handle the component uses for the toolbar (fit/relayout/zoom) + inspector focus.
 */
export const cytoscapeMount: GraphMounter = (container, { elements, onNodeTap, onBackgroundTap }) => {
  const cy: Core = cytoscape({
    container,
    elements,
    minZoom: 0.15,
    maxZoom: 3.5,
    boxSelectionEnabled: false,
    style: [
      {
        selector: 'node',
        style: {
          'background-color': (n: NodeSingular) => kindOf(n).fill,
          'border-color': (n: NodeSingular) => kindOf(n).line,
          'border-width': (n: NodeSingular) => (n.data('hit') ? 2 : 1.4),
          width: (n: NodeSingular) => (n.data('hit') ? 26 : 16),
          height: (n: NodeSingular) => (n.data('hit') ? 26 : 16),
          label: 'data(label)',
          'font-size': (n: NodeSingular) => (n.data('hit') ? 9 : 8),
          'font-family': 'monospace',
          color: '#b6c6c4',
          'text-valign': 'bottom',
          'text-margin-y': 3,
          'text-max-width': '110px',
          'text-wrap': 'ellipsis',
          'text-background-color': '#0a1117',
          'text-background-opacity': 0.7,
          'text-background-padding': '1.5px',
          'min-zoomed-font-size': 7,
          'transition-property': 'border-width, background-color, opacity',
          'transition-duration': 120,
        },
      },
      {
        selector: 'edge',
        style: {
          width: 1.2,
          'line-color': EDGE_INK,
          'curve-style': 'bezier',
          opacity: 0.6,
          'target-arrow-shape': 'triangle',
          'target-arrow-color': EDGE_INK,
          'arrow-scale': 0.6,
        },
      },
      { selector: 'node.faded', style: { opacity: 0.16 } },
      { selector: 'edge.faded', style: { opacity: 0.06 } },
      {
        selector: 'node.sel',
        style: { 'border-color': ACCENT, 'border-width': 3.5 },
      },
      { selector: 'node.nbr', style: { 'border-color': ACCENT, 'border-width': 2.4 } },
      {
        selector: 'edge.hot',
        style: { 'line-color': ACCENT, 'target-arrow-color': ACCENT, width: 2, opacity: 1 },
      },
    ],
    layout: {
      name: 'cose',
      animate: false,
      fit: true,
      padding: 36,
      nodeRepulsion: () => 7000,
      idealEdgeLength: () => 70,
      nodeOverlap: 12,
      gravity: 0.6,
      numIter: 900,
      componentSpacing: 90,
      randomize: true,
    },
  })

  function clearHi() {
    cy.elements().removeClass('sel nbr hot faded')
  }

  function focusNode(id: string) {
    const node = cy.getElementById(id)
    if (!node || node.empty()) return
    clearHi()
    const nbrs = node.neighborhood('node')
    const nbrEdges = node.connectedEdges()
    cy.elements().addClass('faded')
    node.removeClass('faded').addClass('sel')
    nbrs.removeClass('faded').addClass('nbr')
    nbrEdges.removeClass('faded').addClass('hot')
    cy.animate({ center: { eles: node }, zoom: Math.max(cy.zoom(), 0.9) }, { duration: 220 })
  }

  cy.on('tap', 'node', (evt) => {
    const id = (evt.target as NodeSingular).id()
    focusNode(id)
    onNodeTap?.(id)
  })
  cy.on('tap', (evt) => {
    if (evt.target === cy) {
      clearHi()
      onBackgroundTap?.()
    }
  })

  // A deferred fit once the container has its final size (post-layout).
  window.setTimeout(() => {
    try {
      cy.resize()
      cy.fit(undefined, 36)
    } catch {
      /* noop */
    }
  }, 60)

  return {
    destroy: () => {
      try {
        cy.destroy()
      } catch {
        /* noop */
      }
    },
    fit: () => {
      try {
        cy.animate({ fit: { eles: cy.elements(), padding: 36 } }, { duration: 220 })
      } catch {
        /* noop */
      }
    },
    focusNode,
    relayout: () => {
      cy.layout({
        name: 'cose',
        animate: false,
        fit: true,
        padding: 36,
        nodeRepulsion: () => 7000,
        idealEdgeLength: () => 70,
        nodeOverlap: 12,
        gravity: 0.6,
        numIter: 900,
        componentSpacing: 90,
        randomize: true,
      }).run()
    },
    zoomBy: (factor) => {
      try {
        cy.zoom({
          level: cy.zoom() * factor,
          renderedPosition: { x: container.clientWidth / 2, y: container.clientHeight / 2 },
        })
      } catch {
        /* noop */
      }
    },
  }
}
