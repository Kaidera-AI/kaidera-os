/**
 * MdxPlanRenderer — renders a visual-plan `.mdx` document as React.
 *
 * v1 scope: the MDX is rendered as GFM markdown (headings, lists, tables, blockquote
 * callouts, links) with two enrichments:
 *   - ```mermaid fenced blocks → a rendered SVG diagram (mermaid, dynamically imported
 *     only when a diagram is present, so the heavy lib stays out of the initial bundle).
 *   - other fenced code → highlighted-ish monospace blocks (react-markdown default).
 *
 * SECURITY: react-markdown does NOT render raw/embedded HTML (no rehype-raw), so
 * model-authored HTML in the MDX is inert — there is no XSS surface from plan content.
 * Mermaid runs with securityLevel:'strict' (it sanitizes its own SVG).
 *
 * NOT in v1: the agent-native JSX blocks (`<DesignBoard>/<Artboard>/<Screen>`) and the
 * roughjs sketch wireframe canvas. Those are a later increment; raw-HTML wireframes in a
 * plan render as inert text until then. Authors should use markdown + ```mermaid for v1.
 */
import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { PLAN_BLOCKS } from './PlanBlockRegistry'

/** Extract the fence language from a code element's className (supports hyphens, e.g. data-model). */
function fenceLang(className?: string): string | undefined {
  return /language-([\w-]+)/.exec(className || '')?.[1]
}

// Rendered-SVG cache, keyed by theme+code. Mermaid re-runs are async (a "rendering…"
// frame then the SVG), so when react-markdown recreates the code component on each parent
// re-render the diagram FLICKERED. Seeding state from this cache makes a re-mount paint the
// finished SVG synchronously — no flash. Keyed by theme so a light/dark toggle re-renders.
const svgCache = new Map<string, string>()
const cacheKey = (theme: string, code: string) => theme + ':' + hash(code)

/** Is the app in dark mode? Default is `<html class="dark">`; light is `html.light`. */
function currentTheme(): 'dark' | 'light' {
  if (typeof document === 'undefined') return 'dark'
  return document.documentElement.classList.contains('light') ? 'light' : 'dark'
}

/** A single mermaid diagram. Imports mermaid lazily, renders to SVG, shows the source on error. */
function MermaidDiagram({ code }: { code: string }) {
  const theme = currentTheme()
  // Seed from cache → a re-mount shows the finished SVG immediately (no flicker).
  const [svg, setSvg] = useState<string | null>(() => svgCache.get(cacheKey(theme, code)) ?? null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    const key = cacheKey(theme, code)
    const cached = svgCache.get(key)
    let alive = true
    if (cached) {
      queueMicrotask(() => {
        if (alive) setSvg(cached)
      })
      return () => {
        alive = false
      }
    }
    ;(async () => {
      try {
        const mermaid = (await import('mermaid')).default
        // 'dark'/'default' are COLOURFUL palettes (the old 'neutral' was greyscale).
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: theme === 'light' ? 'default' : 'dark',
        })
        // Unique id per render; mermaid throws on a syntactically bad diagram.
        const id = 'mmd-' + Math.abs(hash(code)).toString(36)
        const { svg } = await mermaid.render(id, code)
        svgCache.set(key, svg)
        if (alive) setSvg(svg)
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      alive = false
    }
  }, [code, theme])

  if (err) {
    return (
      <pre className="my-3 overflow-auto rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900">
        mermaid error: {err}
        {'\n\n'}
        {code}
      </pre>
    )
  }
  if (!svg) {
    return <div className="my-3 text-xs text-neutral-400">rendering diagram…</div>
  }
  // svg is mermaid-generated under securityLevel:'strict'.
  return <div className="my-3 flex justify-center overflow-auto" dangerouslySetInnerHTML={{ __html: svg }} />
}

/** Tiny stable string hash for a deterministic mermaid element id (avoids collisions). */
function hash(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0
  return h
}

export default function MdxPlanRenderer({ text }: { text: string }) {
  // Strip YAML frontmatter (--- … ---) so it doesn't render as a horizontal-rule + text.
  const body = stripFrontmatter(text)
  return (
    <div className="plan-doc max-w-none px-6 py-5">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Unwrap the default <pre> for our custom fenced blocks (mermaid + plan blocks)
          // so the block component isn't nested inside <pre> styling; keep <pre> for real code.
          pre(props) {
            const child = (props as { children?: unknown }).children as
              | { props?: { className?: string } }
              | undefined
            const lang = fenceLang(child?.props?.className)
            if (lang === 'mermaid' || (lang && lang in PLAN_BLOCKS)) return <>{props.children}</>
            return <pre {...(props as object)} />
          },
          code(props) {
            const { className, children } = props as { className?: string; children?: unknown }
            const lang = fenceLang(className)
            const value = String(children ?? '').replace(/\n$/, '')
            if (lang === 'mermaid') return <MermaidDiagram code={value} />
            if (lang && lang in PLAN_BLOCKS) {
              const Block = PLAN_BLOCKS[lang]
              return <Block body={value} />
            }
            return <code className={className}>{children as never}</code>
          },
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  )
}

/** Remove a leading YAML frontmatter fence if present. */
function stripFrontmatter(text: string): string {
  if (!text.startsWith('---')) return text
  const end = text.indexOf('\n---', 3)
  if (end === -1) return text
  const after = text.indexOf('\n', end + 1)
  return after === -1 ? '' : text.slice(after + 1)
}
