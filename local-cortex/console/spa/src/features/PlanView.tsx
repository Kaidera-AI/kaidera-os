/**
 * PlanView — the Visual Plan tab.
 *
 * Left: the project's `.mdx` plans (under `docs/plans/`), newest-first, grouped by slug.
 * Right: the selected plan rendered as MDX (MdxPlanRenderer, lazy-loaded so react-markdown
 * + mermaid stay out of the initial bundle).
 *
 * v1 is a READ surface — plans are authored by an agent (the `visual-plan` skill) or a
 * human writing files; this tab reviews them. The bootstrap action creates a Cortex
 * handoff for the project lead to author the first plan.
 */
import { Suspense, lazy, useCallback, useEffect, useState } from 'react'
import type { PlanBootstrapRequest, PlanBootstrapResult, PlanFile, PlanListItem } from '../api/types'

const MdxPlanRenderer = lazy(() => import('./MdxPlanRenderer'))

/** The slice of the api client PlanView needs (structural — `api` satisfies it). */
export interface PlanClient {
  getPlanList: (project: string, signal?: AbortSignal) => Promise<PlanListItem[]>
  getPlanFile: (project: string, path: string, signal?: AbortSignal) => Promise<PlanFile>
  bootstrapPlan?: (
    project: string,
    body: PlanBootstrapRequest,
    signal?: AbortSignal,
  ) => Promise<PlanBootstrapResult>
}

export function PlanView({ project, client }: { project: string; client: PlanClient }) {
  const [plans, setPlans] = useState<PlanListItem[] | null>(null)
  const [listErr, setListErr] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [doc, setDoc] = useState<PlanFile | null>(null)
  const [docErr, setDocErr] = useState<string | null>(null)
  const [loadingDoc, setLoadingDoc] = useState(false)
  const [bootstrapTitle, setBootstrapTitle] = useState('')
  const [bootstrapObjective, setBootstrapObjective] = useState('')
  const [bootstrapping, setBootstrapping] = useState(false)
  const [bootstrapNotice, setBootstrapNotice] = useState<string | null>(null)
  const [bootstrapErr, setBootstrapErr] = useState<string | null>(null)

  const refresh = useCallback(() => {
    setListErr(null)
    client
      .getPlanList(project)
      .then((ps) => {
        setPlans(ps)
        // Auto-select the newest plan-kind doc on first load if nothing chosen.
        setSelected((cur) => cur ?? ps.find((p) => p.kind === 'plan')?.path ?? ps[0]?.path ?? null)
      })
      .catch((e) => setListErr(e instanceof Error ? e.message : String(e)))
  }, [client, project])

  const requestBootstrap = useCallback(() => {
    if (!client.bootstrapPlan || bootstrapping) return
    setBootstrapErr(null)
    setBootstrapNotice(null)
    setBootstrapping(true)
    const payload: PlanBootstrapRequest = {
      title: bootstrapTitle.trim() || `${project} project plan`,
      objective: bootstrapObjective.trim() || undefined,
    }
    client
      .bootstrapPlan(project, payload)
      .then((res) => {
        if (!res.ok) {
          throw new Error(res.error || 'Plan bootstrap failed')
        }
        setBootstrapNotice(
          `Created handoff for ${res.lead || 'the project lead'} to write ${res.path || 'the plan'}.`,
        )
        refresh()
      })
      .catch((e) => setBootstrapErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBootstrapping(false))
  }, [bootstrapObjective, bootstrapTitle, bootstrapping, client, project, refresh])

  // Reset + reload when the project changes.
  useEffect(() => {
    queueMicrotask(() => {
      setPlans(null)
      setSelected(null)
      setDoc(null)
      setBootstrapTitle('')
      setBootstrapObjective('')
      setBootstrapNotice(null)
      setBootstrapErr(null)
      refresh()
    })
  }, [refresh])

  // Load the selected plan's MDX.
  useEffect(() => {
    if (!selected) {
      queueMicrotask(() => setDoc(null))
      return
    }
    const ctrl = new AbortController()
    queueMicrotask(() => {
      if (!ctrl.signal.aborted) {
        setLoadingDoc(true)
        setDocErr(null)
      }
    })
    client
      .getPlanFile(project, selected, ctrl.signal)
      .then((f) => setDoc(f))
      .catch((e) => {
        if (!ctrl.signal.aborted) setDocErr(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoadingDoc(false)
      })
    return () => ctrl.abort()
  }, [client, project, selected])

  return (
    <div className="flex h-full min-h-0">
      {/* Sidebar: the plan index */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-neutral-200 dark:border-neutral-800">
        <div className="flex items-center justify-between px-3 py-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">Plans</h2>
          <button
            onClick={refresh}
            className="rounded px-2 py-0.5 text-xs text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
            title="Reload the plan list"
          >
            Refresh
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-2 pb-3">
          {listErr && <p className="px-1 py-2 text-xs text-red-600">{listErr}</p>}
          {plans && plans.length === 0 && (
            <div
              className="mx-1 space-y-2 rounded-lg border border-dashed border-neutral-300 p-3 text-xs text-neutral-500 dark:border-neutral-700 dark:text-neutral-400"
              data-testid="plan-bootstrap"
            >
              <p>
                No plans yet. Plans live under <code>docs/plans/&lt;slug&gt;/plan.mdx</code>.
              </p>
              {client.bootstrapPlan ? (
                <>
                  <label className="block">
                    <span className="mb-1 block font-medium text-neutral-600 dark:text-neutral-300">
                      Plan title
                    </span>
                    <input
                      value={bootstrapTitle}
                      onChange={(e) => setBootstrapTitle(e.currentTarget.value)}
                      placeholder={`${project} project plan`}
                      className="w-full rounded border border-neutral-300 bg-white px-2 py-1 text-xs text-neutral-800 dark:border-neutral-700 dark:bg-neutral-950 dark:text-neutral-100"
                    />
                  </label>
                  <label className="block">
                    <span className="mb-1 block font-medium text-neutral-600 dark:text-neutral-300">
                      Objective
                    </span>
                    <textarea
                      value={bootstrapObjective}
                      onChange={(e) => setBootstrapObjective(e.currentTarget.value)}
                      placeholder="Ask the lead to create the initial plan, roadmap, acceptance criteria, and next handoffs."
                      rows={4}
                      className="w-full rounded border border-neutral-300 bg-white px-2 py-1 text-xs text-neutral-800 dark:border-neutral-700 dark:bg-neutral-950 dark:text-neutral-100"
                    />
                  </label>
                  <button
                    type="button"
                    onClick={requestBootstrap}
                    disabled={bootstrapping}
                    className="rounded bg-neutral-900 px-2 py-1 text-xs font-medium text-white disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-950"
                  >
                    {bootstrapping ? 'Creating handoff…' : 'Ask lead to create plan'}
                  </button>
                  {bootstrapNotice && <p className="text-emerald-700">{bootstrapNotice}</p>}
                  {bootstrapErr && <p className="text-red-600">{bootstrapErr}</p>}
                </>
              ) : (
                <p>Author one with the visual-plan skill, then refresh this tab.</p>
              )}
            </div>
          )}
          {plans?.map((p) => (
            <button
              key={p.path}
              onClick={() => setSelected(p.path)}
              className={
                'mb-0.5 block w-full truncate rounded px-2 py-1 text-left text-sm ' +
                (selected === p.path
                  ? 'bg-blue-50 font-medium text-blue-700 dark:bg-blue-950 dark:text-blue-300'
                  : 'text-neutral-700 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800')
              }
              title={p.path}
            >
              <span className="text-neutral-400">{p.kind !== 'plan' ? `${p.kind} · ` : ''}</span>
              {p.path}
            </button>
          ))}
        </div>
      </aside>

      {/* Content: the rendered plan */}
      <section className="min-h-0 flex-1 overflow-auto">
        {!selected && <p className="p-6 text-sm text-neutral-400">Select a plan to review.</p>}
        {docErr && <p className="p-6 text-sm text-red-600">{docErr}</p>}
        {loadingDoc && !doc && <p className="p-6 text-sm text-neutral-400">Loading…</p>}
        {doc && (
          <Suspense fallback={<p className="p-6 text-sm text-neutral-400">Rendering…</p>}>
            <MdxPlanRenderer text={doc.text} />
          </Suspense>
        )}
      </section>
    </div>
  )
}
