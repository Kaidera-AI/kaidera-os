/**
 * PlanBlocks — the rich visual-plan block renderers.
 *
 * A plan `.mdx` expresses structured blocks as fenced code with a YAML body and a language
 * tag the renderer intercepts (the same mechanism as ```mermaid):
 *
 *   ```data-model … ```      → a multi-entity ERD: collapsible cards, FK hover-ring +
 *                              click-to-scroll, diff chips (forked lean from agent-native, MIT)
 *   ```api-endpoint … ```    → a method/path card with collapsible request/response
 *   ```annotated-code … ```  → real code with line-anchored margin notes (hover to highlight)
 *   ```file-tree … ```       → an indented file map with notes
 *
 * Bodies are YAML so they read well in the raw `.mdx` and round-trip cleanly. Each block
 * fails soft: a parse error renders the raw body in an amber box, never throws. This is the
 * lean, self-contained path: NO npm renderer dep (the data-model design is forked + stripped
 * from @agent-native/core but reimplemented dependency-free), so the bloat never ships.
 */
import { useMemo, useRef, useState } from 'react'
import yaml from 'js-yaml'

// js-yaml v4 `load` uses DEFAULT_SCHEMA, which is safe — it does NOT execute code or
// construct arbitrary types (the unsafe loaders were removed in v4). Block bodies are
// plain data (maps/lists/scalars), so this is the right, safe parser.
/** Parse a YAML block body; returns [value, error]. Never throws. */
function parseYaml<T>(body: string): [T | null, string | null] {
  try {
    return [yaml.load(body) as T, null]
  } catch (e) {
    return [null, e instanceof Error ? e.message : String(e)]
  }
}

function BlockError({ kind, error, body }: { kind: string; error: string; body: string }) {
  return (
    <pre className="my-3 overflow-auto rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200">
      {kind} parse error: {error}
      {'\n\n'}
      {body}
    </pre>
  )
}

/* ---------------------------------------------------------------- data-model
 *
 * Multi-entity ERD: forked + stripped from @agent-native/core (MIT, BuilderIO/
 * agent-native) `DataModelBlock` — we own a lean, dependency-free Read renderer
 * (their version pulls @tabler/icons-react + an Edit surface we don't need). Kept:
 * collapsible entity cards, foreign-key hover-ring + click-to-scroll, and diff
 * chips (added/modified/removed/renamed) for a living design doc. Dropped: the
 * tabler icons (→ inline glyphs/emoji) and all editing.
 */

type Change = 'added' | 'modified' | 'removed' | 'renamed'
interface DMField {
  name: string
  type?: string
  pk?: boolean
  fk?: string // "Entity.field" | "Entity"
  nullable?: boolean
  default?: string
  note?: string
  change?: Change
  was?: string // prior value when change === 'modified'
}
interface DMEntity { id?: string; name: string; note?: string; change?: Change; fields: DMField[] }
interface DMRelation { from: string; to: string; kind?: '1-1' | '1-n' | 'n-n'; label?: string }
interface DataModelData { entities: DMEntity[]; relations?: DMRelation[] }

const CHANGE_BADGE: Record<Change, string> = {
  added: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300',
  modified: 'bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300',
  removed: 'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300',
  renamed: 'bg-violet-100 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300',
}
const CHANGE_NAME_INK: Record<Change, string> = {
  added: 'text-emerald-700 dark:text-emerald-300',
  modified: 'text-blue-700 dark:text-blue-300',
  removed: 'text-red-600 line-through dark:text-red-300',
  renamed: 'text-violet-700 dark:text-violet-300',
}

/** Stable key for an entity (id when given, else its name). Relations/FKs ref this. */
const entKey = (e: DMEntity) => e.id || e.name
/** Split an FK string "Entity.field" → {entity, field}. */
function parseFk(fk: string): { entity: string; field?: string } {
  const i = fk.indexOf('.')
  return i === -1 ? { entity: fk.trim() } : { entity: fk.slice(0, i).trim(), field: fk.slice(i + 1).trim() || undefined }
}
/** Resolve an entity by id OR case-insensitive name. */
function resolveEntity(entities: DMEntity[], ref: string): DMEntity | undefined {
  const n = ref.trim()
  return entities.find((e) => entKey(e) === n) ?? entities.find((e) => e.name.toLowerCase() === n.toLowerCase())
}
const relationGlyph = (k?: string) => (k === '1-1' ? '1:1' : k === 'n-n' ? 'n:n' : '1:n')
/** Explicit relations, or infer simple 1-n relations from fk fields when omitted. */
function effectiveRelations(data: DataModelData): DMRelation[] {
  if (data.relations?.length) return data.relations
  const out: DMRelation[] = []
  for (const e of data.entities) {
    for (const f of e.fields) {
      if (!f.fk) continue
      const target = resolveEntity(data.entities, parseFk(f.fk).entity)
      if (target) out.push({ from: entKey(target), to: entKey(e), kind: '1-n', label: f.name })
    }
  }
  return out
}

function ChangeChip({ change }: { change: Change }) {
  const label = change[0].toUpperCase() + change.slice(1)
  return (
    <span className={'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase leading-none tracking-wide ' + CHANGE_BADGE[change]}>
      {label}
    </span>
  )
}

export function DataModelBlock({ body }: { body: string }) {
  const [data, err] = parseYaml<DataModelData>(body)
  const entities = data?.entities ?? []
  const relations = useMemo(() => (data ? effectiveRelations(data) : []), [data])
  // Default: all expanded for a small model (≤2), else only the first.
  const [expanded, setExpanded] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {}
    const all = entities.length <= 2
    entities.forEach((e, i) => (init[entKey(e)] = all || i === 0))
    return init
  })
  const [highlight, setHighlight] = useState<string | null>(null)
  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({})

  const toggle = (k: string) => setExpanded((c) => ({ ...c, [k]: !c[k] }))
  // Hover an FK → ring the referenced card; click → also expand + scroll to it.
  const focus = (k: string | null, scroll: boolean) => {
    setHighlight(k)
    if (k && scroll) {
      setExpanded((c) => ({ ...c, [k]: true }))
      cardRefs.current[k]?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }

  if (err || !data) return <BlockError kind="data-model" error={err ?? 'empty'} body={body} />
  return (
    <div className="my-4 flex flex-col gap-3">
      {entities.map((entity) => {
        const k = entKey(entity)
        const open = expanded[k] ?? false
        const ringed = highlight === k
        return (
          <div
            key={k}
            ref={(n) => { cardRefs.current[k] = n }}
            className={
              'overflow-hidden rounded-xl border transition-shadow ' +
              (ringed ? 'border-blue-400 ring-2 ring-blue-400/60' : 'border-neutral-300 dark:border-neutral-700')
            }
          >
            <button
              type="button"
              aria-expanded={open}
              onClick={() => toggle(k)}
              className="flex w-full items-center gap-2 bg-neutral-50 px-4 py-2.5 text-left transition-colors hover:bg-neutral-100 dark:bg-neutral-900 dark:hover:bg-neutral-800"
            >
              <span className={'shrink-0 text-neutral-400 transition-transform ' + (open ? 'rotate-90' : '')}>▸</span>
              <span className="shrink-0">🗄️</span>
              <span className={'min-w-0 truncate font-mono text-sm font-semibold ' + (entity.change ? CHANGE_NAME_INK[entity.change] : 'text-neutral-800 dark:text-neutral-100')}>
                {entity.name}
              </span>
              {entity.change && <ChangeChip change={entity.change} />}
              <span className="ml-auto shrink-0 rounded-full bg-neutral-200 px-2 py-0.5 text-[11px] font-medium text-neutral-500 dark:bg-neutral-700 dark:text-neutral-300">
                {entity.fields.length} {entity.fields.length === 1 ? 'field' : 'fields'}
              </span>
            </button>
            {open && (
              <div className="border-t border-neutral-200 dark:border-neutral-800">
                {entity.note && <p className="px-4 pt-2 text-xs italic text-neutral-500">{entity.note}</p>}
                <table className="w-full border-collapse text-sm">
                  <tbody>
                    {entity.fields.map((field, i) => {
                      const fkTarget = field.fk ? resolveEntity(entities, parseFk(field.fk).entity) : undefined
                      const fkKey = fkTarget ? entKey(fkTarget) : undefined
                      return (
                        <tr
                          key={`${field.name}-${i}`}
                          className={
                            'border-t border-neutral-100 align-top first:border-t-0 dark:border-neutral-800/70 ' +
                            (field.fk ? 'cursor-pointer hover:bg-blue-500/5' : '')
                          }
                          onMouseEnter={fkKey ? () => focus(fkKey, false) : undefined}
                          onMouseLeave={fkKey ? () => focus(null, false) : undefined}
                          onClick={fkKey ? () => focus(fkKey, true) : undefined}
                        >
                          <td className="w-px whitespace-nowrap py-1.5 pl-4 pr-2">
                            <span className="flex items-center gap-1.5">
                              {field.pk && <span title="Primary key" className="text-[11px]">🔑</span>}
                              {field.fk && <span title="Foreign key" className="text-[11px]">🔗</span>}
                              <span className={'font-mono text-xs ' + (field.pk ? 'font-semibold ' : '') + (field.change ? CHANGE_NAME_INK[field.change] : 'text-neutral-800 dark:text-neutral-200')}>
                                {field.name}
                              </span>
                            </span>
                          </td>
                          <td className="py-1.5 pr-2">
                            <span className="flex flex-wrap items-center gap-1.5">
                              {field.change === 'modified' && field.was && (
                                <>
                                  <span className="inline-block rounded bg-neutral-200 px-1.5 py-0.5 font-mono text-[11px] text-neutral-500 line-through dark:bg-neutral-700">{field.was}</span>
                                  <span className="shrink-0 text-neutral-400">→</span>
                                </>
                              )}
                              {field.type && (
                                <span className={'inline-block rounded bg-neutral-200 px-1.5 py-0.5 font-mono text-[11px] text-neutral-500 dark:bg-neutral-700 ' + (field.change === 'removed' ? 'line-through' : '')}>
                                  {field.type}
                                </span>
                              )}
                            </span>
                          </td>
                          <td className="py-1.5 pr-4 text-right">
                            <span className="flex flex-wrap items-center justify-end gap-1">
                              {field.change && <ChangeChip change={field.change} />}
                              {field.pk && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-600 dark:bg-amber-500/15 dark:text-amber-300">PK</span>}
                              {field.fk && (
                                <span className="inline-flex items-center gap-1 rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-semibold text-blue-700 dark:bg-blue-500/15 dark:text-blue-300">
                                  FK
                                  <span className="font-mono font-normal opacity-90">
                                    {fkTarget ? `${fkTarget.name}${parseFk(field.fk).field ? `.${parseFk(field.fk).field}` : ''}` : field.fk}
                                  </span>
                                </span>
                              )}
                              {field.nullable && <span className="rounded bg-neutral-200 px-1.5 py-0.5 text-[10px] font-medium text-neutral-500 dark:bg-neutral-700">nullable</span>}
                              {field.default != null && field.default !== '' && (
                                <span className="rounded bg-neutral-200 px-1.5 py-0.5 font-mono text-[10px] text-neutral-500 dark:bg-neutral-700">= {field.default}</span>
                              )}
                            </span>
                            {field.note && <div className="mt-0.5 text-[11px] italic text-neutral-400">{field.note}</div>}
                          </td>
                        </tr>
                      )
                    })}
                    {entity.fields.length === 0 && (
                      <tr><td className="px-4 py-2 text-xs text-neutral-500">No fields yet.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )
      })}
      {relations.length > 0 && (
        <div className="mt-1">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-neutral-400">Relations</div>
          <div className="mt-2 flex flex-col gap-1.5">
            {relations.map((r, i) => {
              const to = resolveEntity(entities, r.to)
              const from = resolveEntity(entities, r.from)
              const toKey = to ? entKey(to) : undefined
              return (
                <button
                  key={`${r.from}-${r.to}-${i}`}
                  type="button"
                  className="flex w-fit items-center gap-2 rounded-md px-2 py-1 text-sm transition-colors hover:bg-neutral-100 dark:hover:bg-neutral-800"
                  onMouseEnter={toKey ? () => focus(toKey, false) : undefined}
                  onMouseLeave={toKey ? () => focus(null, false) : undefined}
                  onClick={toKey ? () => focus(toKey, true) : undefined}
                >
                  <span className="font-mono text-xs font-semibold text-neutral-700 dark:text-neutral-200">{from?.name ?? r.from}</span>
                  <span className="flex items-center gap-1 rounded bg-blue-100 px-1.5 py-0.5 font-mono text-[10px] font-bold text-blue-700 dark:bg-blue-500/15 dark:text-blue-300">
                    {relationGlyph(r.kind)} →
                  </span>
                  <span className="font-mono text-xs font-semibold text-neutral-700 dark:text-neutral-200">{to?.name ?? r.to}</span>
                  {r.label && <span className="text-xs text-neutral-500">· {r.label}</span>}
                  {(!from || !to) && <span className="text-[10px] text-amber-600 dark:text-amber-300">(unresolved)</span>}
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------- api-endpoint */

interface ApiEndpointData {
  method: string
  path: string
  purpose?: string
  request?: string
  response?: string
}

const METHOD_COLOR: Record<string, string> = {
  GET: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200',
  POST: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200',
  PUT: 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200',
  PATCH: 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200',
  DELETE: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
}

export function ApiEndpointBlock({ body }: { body: string }) {
  const [data, err] = parseYaml<ApiEndpointData>(body)
  const [open, setOpen] = useState(false)
  if (err || !data) return <BlockError kind="api-endpoint" error={err ?? 'empty'} body={body} />
  const method = (data.method || 'GET').toUpperCase()
  const hasDetail = Boolean(data.request || data.response)
  return (
    <div className="my-3 rounded-lg border border-neutral-300 dark:border-neutral-700">
      <button
        onClick={() => hasDetail && setOpen((o) => !o)}
        className={'flex w-full items-center gap-3 px-3 py-2 text-left ' + (hasDetail ? 'cursor-pointer hover:bg-neutral-50 dark:hover:bg-neutral-900' : 'cursor-default')}
      >
        <span className={'rounded px-2 py-0.5 font-mono text-xs font-bold ' + (METHOD_COLOR[method] ?? METHOD_COLOR.GET)}>{method}</span>
        <code className="flex-1 font-mono text-sm text-neutral-800 dark:text-neutral-100">{data.path}</code>
        {data.purpose && <span className="hidden truncate text-xs text-neutral-500 sm:block">{data.purpose}</span>}
        {hasDetail && <span className="text-neutral-400">{open ? '▾' : '▸'}</span>}
      </button>
      {open && hasDetail && (
        <div className="border-t border-neutral-200 px-3 py-2 dark:border-neutral-800">
          {data.request && (
            <div className="mb-1 text-xs">
              <span className="font-semibold text-neutral-400">request </span>
              <code className="text-neutral-700 dark:text-neutral-300">{data.request}</code>
            </div>
          )}
          {data.response && (
            <div className="text-xs">
              <span className="font-semibold text-neutral-400">response </span>
              <code className="text-neutral-700 dark:text-neutral-300">{data.response}</code>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------ annotated-code */

interface AnnotatedCodeData {
  file?: string
  lang?: string
  code: string
  notes?: { lines: string | number; label?: string; note: string }[]
}

/** Expand a `lines` field ("2" | "2-4" | 2) into a set of 1-based line numbers. */
function lineSet(spec: string | number): Set<number> {
  const s = new Set<number>()
  for (const part of String(spec).split(',')) {
    const m = /^\s*(\d+)\s*(?:-\s*(\d+))?\s*$/.exec(part)
    if (!m) continue
    const a = Number(m[1])
    const b = m[2] ? Number(m[2]) : a
    for (let i = a; i <= b; i++) s.add(i)
  }
  return s
}

export function AnnotatedCodeBlock({ body }: { body: string }) {
  const [data, err] = parseYaml<AnnotatedCodeData>(body)
  const [hover, setHover] = useState<number | null>(null)
  const lines = useMemo(() => (data?.code ?? '').replace(/\n$/, '').split('\n'), [data])
  // Which source lines each note covers, and the set highlighted by the hovered note.
  const noteLineSets = useMemo(() => (data?.notes ?? []).map((n) => lineSet(n.lines)), [data])
  const highlit = hover === null ? null : noteLineSets[hover]
  if (err || !data) return <BlockError kind="annotated-code" error={err ?? 'empty'} body={body} />
  return (
    <div className="my-4 overflow-hidden rounded-lg border border-neutral-300 dark:border-neutral-700">
      {data.file && (
        <div className="flex items-center gap-2 border-b border-neutral-200 bg-neutral-50 px-3 py-1.5 dark:border-neutral-800 dark:bg-neutral-900">
          <span className="text-sm">📝</span>
          <code className="text-xs text-neutral-600 dark:text-neutral-400">{data.file}</code>
        </div>
      )}
      <pre className="overflow-auto bg-neutral-950 p-0 text-xs leading-relaxed text-neutral-100">
        <code className="block">
          {lines.map((ln, i) => {
            const n = i + 1
            const on = highlit?.has(n)
            return (
              <div key={i} className={'flex px-3 ' + (on ? 'bg-blue-500/25' : '')}>
                <span className="mr-3 w-6 shrink-0 select-none text-right text-neutral-600">{n}</span>
                <span className="whitespace-pre">{ln || ' '}</span>
              </div>
            )
          })}
        </code>
      </pre>
      {data.notes && data.notes.length > 0 && (
        <ol className="divide-y divide-neutral-100 dark:divide-neutral-800">
          {data.notes.map((nt, i) => (
            <li
              key={i}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              className="flex cursor-default gap-3 px-3 py-1.5 text-xs hover:bg-blue-50 dark:hover:bg-blue-950"
            >
              <span className="shrink-0 font-mono text-blue-600 dark:text-blue-400">L{String(nt.lines)}</span>
              <span className="text-neutral-700 dark:text-neutral-300">
                {nt.label && <span className="font-semibold">{nt.label}: </span>}
                {nt.note}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- file-tree */

interface FileTreeData {
  root?: string
  items: { path: string; note?: string }[]
}

export function FileTreeBlock({ body }: { body: string }) {
  const [data, err] = parseYaml<FileTreeData>(body)
  if (err || !data) return <BlockError kind="file-tree" error={err ?? 'empty'} body={body} />
  return (
    <div className="my-4 rounded-lg border border-neutral-300 font-mono text-xs dark:border-neutral-700">
      {data.root && (
        <div className="border-b border-neutral-200 bg-neutral-50 px-3 py-1.5 text-neutral-600 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-400">
          🌳 {data.root}/
        </div>
      )}
      <div className="px-3 py-2">
        {(data.items ?? []).map((it, i) => {
          // Indent by the slash depth of the path so nested entries read as a tree.
          const depth = (it.path.match(/\//g) || []).length
          return (
            <div key={i} className="flex py-0.5" style={{ paddingLeft: `${depth * 1.25}rem` }}>
              <span className="text-neutral-800 dark:text-neutral-200">{it.path}</span>
              {it.note && <span className="ml-3 text-neutral-400">{it.note}</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}
