/**
 * ExplainFrame — the SANDBOXED renderer for a generated explainer document.
 *
 * SECURITY (load-bearing — docs/sdk/modules/explain.md §5): the explainer is
 * MODEL-GENERATED HTML and is treated as UNTRUSTED. It is rendered ONLY inside a
 * sandboxed `<iframe srcDoc=… sandbox="allow-scripts" referrerPolicy="no-referrer">`:
 *
 *   - `srcDoc` (NOT `src=`) — the document is inlined; the iframe never points at a URL.
 *   - `sandbox="allow-scripts"` WITHOUT `allow-same-origin` — the document's inline
 *     scripts (Mermaid / Chart.js init) run, but in a UNIQUE OPAQUE origin: it CANNOT
 *     read the console's origin, cookies, localStorage, or reach into the parent DOM.
 *     (Per the HTML spec, `allow-scripts` + `allow-same-origin` together would let the
 *     frame remove its own sandbox — so the two are DELIBERATELY never combined here.)
 *   - `referrerPolicy="no-referrer"` — the CDN fetches (Mermaid/Chart.js) leak no URL.
 *
 * The HTML is NEVER injected into the SPA's own DOM via React's raw-HTML escape hatch or
 * an `innerHTML` write, and the iframe is never pointed at a URL (`src=`). This component
 * is the single rendering seam, so the isolation model lives in exactly one place;
 * ExplainView + ExplainGallery only ever hand HTML to it.
 */

interface ExplainFrameProps {
  /** The full self-contained HTML document to render (sandboxed). */
  html: string
  /** Accessible title for the iframe (defaults to a generic label). */
  title?: string
  className?: string
}

export function ExplainFrame({ html, title = 'Code explainer', className }: ExplainFrameProps) {
  return (
    <iframe
      title={title}
      srcDoc={html}
      // allow-scripts WITHOUT allow-same-origin → scripts run in an opaque origin, isolated
      // from the console's origin/cookies/DOM. Do NOT add allow-same-origin (see the file
      // header) — it would defeat the sandbox.
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      className={className ?? 'h-full w-full rounded-xl border border-glass-line bg-white'}
    />
  )
}
