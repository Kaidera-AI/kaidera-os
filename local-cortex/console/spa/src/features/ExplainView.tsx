/**
 * ExplainView — the "Explain" main-area tab: a visual code explainer.
 *
 * Turn a project/code TARGET (the whole project, a file, a function's blast radius, a
 * directory, or a git diff) into a SELF-CONTAINED visual HTML document (Mermaid diagrams
 * + prose), generated host-side and persisted to Cortex L5. This view is the user-facing
 * surface: a target PICKER, a Generate trigger, LIVE progress while it streams, and the
 * finished document rendered in a SANDBOXED iframe (see ExplainFrame — the
 * model-generated HTML is treated as untrusted).
 *
 * FLOW (the backend contract — docs/sdk/modules/explain.md):
 *   1. Generate → `client.postExplain(project, {kind, path?, fn_name?, git_rev?, …})` →
 *      `{run_id}` (the repo is resolved SERVER-SIDE from the project; the user never types it).
 *      Project is a first-class backend target (`{kind: 'project'}`) with no path.
 *   2. Follow the run by id (`useExplainRun` polls `client.run(run_id)`) — the run's `output`
 *      spans carry the full HTML AS IT STREAMS, so progress is the transcript growing.
 *   3. On terminal `ok`, the FULL HTML (concat of the run's output spans) renders in the
 *      sandboxed iframe. On `error`, a clean message.
 *
 * It COMPOSES with (does not replace) the planned graph view — this is one more main-area
 * tab alongside Agent · Dispatch · Analytics · Settings · Graph.
 *
 * The gallery (ExplainGallery) lives beside the picker; its "View" hands a past explainer's
 * full HTML up here (`onView`) to render in the SAME sandboxed iframe.
 */

import { useCallback, useState } from 'react'
import { GlassPanel } from '../components/glass'
import { cx } from '../components/ui'
import { ExplainFrame } from './ExplainFrame'
import { ExplainGallery } from './ExplainGallery'
import { RunTranscriptView } from './RunTranscriptView'
import { explainExportUrl, useExplainRun } from '../api'
import type { ExplainKind, ExplainListItem, ExplainRequest, ExplainRunReader, ExplainStartResult } from '../api'

/** The slice of the api client ExplainView (+ its gallery) needs — so tests fake one object. */
export interface ExplainClient {
  postExplain: (project: string, body: ExplainRequest, signal?: AbortSignal) => Promise<ExplainStartResult>
  getExplainList: (project: string, signal?: AbortSignal) => Promise<ExplainListItem[]>
  run: ExplainRunReader
}

/**
 * A cross-tab pre-fill target (compose-with-Graph): a `file` path or a `blast` function name
 * the Graph view's "Explain this" action hands over. `nonce` makes each hand-off distinct so
 * re-clicking the SAME node re-seeds the picker (a value-only change wouldn't re-fire).
 */
export interface ExplainInitialTarget {
  kind: 'project' | 'file' | 'blast'
  value: string
  nonce: number
}

interface ExplainViewProps {
  project: string | null
  client: ExplainClient
  /** Optional pre-fill from the Graph view's "Explain this <node>" action. */
  initialTarget?: ExplainInitialTarget | null
}

type ExplainMode = ExplainKind

const KINDS: { id: ExplainMode; label: string; hint: string }[] = [
  { id: 'project', label: 'Project', hint: 'A full-system overview of the project repo' },
  { id: 'file', label: 'File', hint: 'A single source file' },
  { id: 'blast', label: 'Blast radius', hint: "A function + everything it touches" },
  { id: 'dir', label: 'Directory', hint: 'A package / folder of code' },
  { id: 'diff', label: 'Git diff', hint: 'What changed since a revision' },
]

/** The per-mode required input field — drives the conditional form + the validation. */
function requiredField(mode: ExplainMode): 'path' | 'fn_name' | null {
  if (mode === 'file' || mode === 'dir') return 'path'
  if (mode === 'blast') return 'fn_name'
  return null // diff: git_rev is OPTIONAL (defaults to diff HEAD server-side)
}

export function ExplainView({ project, client, initialTarget }: ExplainViewProps) {
  const [mode, setMode] = useState<ExplainMode>('project')
  const [path, setPath] = useState('')
  const [fnName, setFnName] = useState('')
  const [gitRev, setGitRev] = useState('')
  const [harness, setHarness] = useState('')
  const [model, setModel] = useState('')
  // The Kind picker + per-kind inputs + harness/model override all live behind a single
  // collapsed "Advanced" disclosure (default closed) so the primary flow is one click.
  const [showAdvanced, setShowAdvanced] = useState(false)

  // The in-flight generation's run id (null = nothing generating yet). A NEW Generate
  // resets it; useExplainRun follows whichever id is set.
  const [runId, setRunId] = useState<string | null>(null)
  // A start-time error (postExplain failed / host rejected) — distinct from a run `error`.
  const [startError, setStartError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  // A gallery "View" override: a past explainer's full HTML to show in the SAME iframe
  // (takes precedence over the live run's document until the next Generate).
  const [galleryHtml, setGalleryHtml] = useState<string | null>(null)
  const [galleryCaption, setGalleryCaption] = useState<string>('')
  const [galleryRunId, setGalleryRunId] = useState<string | null>(null)

  // Compose-with-Graph: when the Graph view hands over an "Explain this <node>" target, seed
  // the picker (kind + the per-kind input). Done with the React "adjust state during render"
  // pattern (tracking the last-applied nonce) rather than an effect — re-clicking the same
  // node bumps the nonce and re-seeds, and there's no set-state-in-effect cascade. The user
  // can still edit before hitting Generate (we never auto-submit).
  const [seededNonce, setSeededNonce] = useState<number | null>(null)
  if (initialTarget && initialTarget.nonce !== seededNonce) {
    setSeededNonce(initialTarget.nonce)
    if (initialTarget.kind === 'project') {
      setMode('project')
      // A whole-project seed is the primary flow — keep Advanced collapsed.
    } else if (initialTarget.kind === 'file') {
      setMode('file')
      setPath(initialTarget.value)
      setShowAdvanced(true) // auto-expand so the seeded path input is visible
    } else {
      setMode('blast')
      setFnName(initialTarget.value)
      setShowAdvanced(true) // auto-expand so the seeded fn_name input is visible
    }
    setStartError(null)
  }

  const runState = useExplainRun({ runId, getRun: client.run })

  const reqField = requiredField(mode)
  const canGenerate =
    !!project &&
    !starting &&
    (reqField === 'path'
      ? path.trim().length > 0
      : reqField === 'fn_name'
        ? fnName.trim().length > 0
        : true) // diff needs nothing

  const onGenerate = useCallback(async () => {
    if (!project || starting) return
    // Per-kind required-input guard (a friendly message before the round-trip).
    if (reqField === 'path' && !path.trim()) {
      setStartError('Enter a file or directory path to explain.')
      return
    }
    if (reqField === 'fn_name' && !fnName.trim()) {
      setStartError('Enter a function name for the blast-radius explainer.')
      return
    }
    // The default project run posts ONLY { kind: 'project' } — no stale path/fn/git_rev and
    // (critically) no harness/model, so the backend resolves the project lead's selected
    // routing instead of a stale body model overriding it. Drill-down (advanced) kinds attach
    // their per-kind input + any explicit harness/model override.
    const body: ExplainRequest = { kind: mode }
    if (mode !== 'project') {
      if (path.trim()) body.path = path.trim()
      if (fnName.trim()) body.fn_name = fnName.trim()
      if (gitRev.trim()) body.git_rev = gitRev.trim()
      if (harness.trim()) body.harness = harness.trim()
      if (model.trim()) body.model = model.trim()
    }

    setStarting(true)
    setStartError(null)
    setGalleryHtml(null) // a fresh generation supersedes a gallery preview
    setGalleryRunId(null)
    setRunId(null) // reset the follower before the new run lands
    try {
      const res = await client.postExplain(project, body)
      if (!res.accepted) {
        // The host harness-service rejected / was unreachable (200 accepted=false). The run
        // is marked errored server-side; surface a clean message instead of following it.
        setStartError(
          res.error
            ? `Could not start the explainer: ${res.error}`
            : 'Could not start the explainer (the host generator is unavailable).',
        )
      } else {
        setRunId(res.run_id)
      }
    } catch (e: unknown) {
      setStartError(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }, [project, starting, reqField, path, fnName, gitRev, harness, model, mode, client])

  // The gallery hands a past explainer's full HTML here → render it in the same iframe.
  const onViewPast = useCallback((html: string, caption: string, pastRunId: string) => {
    setGalleryHtml(html)
    setGalleryCaption(caption)
    setGalleryRunId(pastRunId)
    setRunId(null) // stop following any live run; the gallery view takes the stage
    setStartError(null)
  }, [])

  // What the render stage shows, in priority order: a gallery preview > the finished live
  // document > live progress > the empty hint.
  const showGallery = galleryHtml !== null
  const showDocument = !showGallery && runState.phase === 'ok' && runState.html.length > 0
  const showProgress = !showGallery && !showDocument && runId !== null && runState.phase !== 'error'
  const showRunError = !showGallery && runState.phase === 'error'

  if (!project) {
    return (
      <GlassPanel className="flex-1">
        <div className="flex h-full items-center justify-center p-10">
          <p className="text-sm text-ink-500">Select a project to generate a code explainer.</p>
        </div>
      </GlassPanel>
    )
  }

  return (
    <div className="explain-layout flex min-h-0 min-w-0 flex-1 gap-3">
      {/* LEFT: the target picker + the gallery. */}
      <GlassPanel as="aside" className="explain-sidebar w-80 shrink-0">
        <header className="border-b border-glass-line px-4 py-3">
          <h2 className="text-sm font-semibold text-ink-100">Explain code</h2>
          <p className="mt-0.5 text-[11px] text-ink-500">
            Generate a whole-project visual explainer — diagrams + prose — or drill into a
            file, function, directory, or diff under Advanced.
          </p>
        </header>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
          {/* PRIMARY flow — a one-click whole-project explainer (no typing). */}
          <div className="rounded-lg border border-glass-line bg-base-800/30 px-3 py-2">
            <p className="text-[11px] text-ink-400">
              One click generates a whole-project visual explainer — no path needed.
            </p>
          </div>

          {/* ADVANCED disclosure — the Kind picker, per-kind inputs, and harness/model
              override all live here (collapsed by default so the primary flow is one click). */}
          <div>
            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              className="text-[11px] font-medium text-ink-400 hover:text-ink-200"
              aria-expanded={showAdvanced}
            >
              {showAdvanced ? '▾' : '▸'} Advanced / drill into a file, function, or diff
            </button>
            {showAdvanced && (
              <div className="mt-2 space-y-4">
                {/* Kind selector. */}
                <div>
                  <label
                    htmlFor="explain-kind"
                    className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-ink-400"
                  >
                    Target
                  </label>
                  <select
                    id="explain-kind"
                    value={mode}
                    onChange={(e) => {
                      setMode(e.target.value as ExplainMode)
                      setStartError(null)
                    }}
                    aria-label="Explain target kind"
                    className="glass-soft w-full rounded-lg bg-base-800/40 px-2.5 py-1.5 text-[13px] text-ink-100 focus:outline-none focus:ring-1 focus:ring-mint-400/40"
                  >
                    {KINDS.map((k) => (
                      <option key={k.id} value={k.id}>
                        {k.label}
                      </option>
                    ))}
                  </select>
                  <p className="mt-1 text-[11px] text-ink-500">
                    {KINDS.find((k) => k.id === mode)?.hint}
                  </p>
                </div>

                {/* Conditional input — file/dir → path, blast → fn_name, diff → git_rev. */}
                {mode === 'project' && (
                  <div className="rounded-lg border border-glass-line bg-base-800/30 px-3 py-2">
                    <p className="text-[11px] text-ink-400">
                      Explains the project from its configured repo root.
                    </p>
                  </div>
                )}
                {(mode === 'file' || mode === 'dir') && (
                  <Field
                    id="explain-path"
                    label={mode === 'file' ? 'File path' : 'Directory path'}
                    placeholder={mode === 'file' ? 'app/main.py' : 'app/explain'}
                    value={path}
                    onChange={setPath}
                    help="Relative to the project's repo root."
                  />
                )}
                {mode === 'blast' && (
                  <Field
                    id="explain-fn"
                    label="Function name"
                    placeholder="explain_one"
                    value={fnName}
                    onChange={setFnName}
                    help="The blast radius is computed from the code graph."
                  />
                )}
                {mode === 'diff' && (
                  <Field
                    id="explain-rev"
                    label="Git revision (optional)"
                    placeholder="HEAD~5  ·  a1b2c3d  ·  main"
                    value={gitRev}
                    onChange={setGitRev}
                    help="Diffs <rev>..HEAD. Leave blank to explain the working diff against HEAD."
                  />
                )}

                {/* Optional harness/model override (repo is never asked). */}
                <div>
                  <Field
                    id="explain-harness"
                    label="Harness override"
                    placeholder="(project default)"
                    value={harness}
                    onChange={setHarness}
                  />
                  <div className="mt-3">
                    <Field
                      id="explain-model"
                      label="Model override"
                      placeholder="(project default)"
                      value={model}
                      onChange={setModel}
                    />
                  </div>
                </div>
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={onGenerate}
            disabled={!canGenerate}
            className={cx(
              'mt-1 inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold transition-colors',
              canGenerate
                ? 'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/40 hover:bg-mint-500/25'
                : 'cursor-not-allowed bg-base-700/50 text-ink-500',
            )}
          >
            {starting || runState.polling
              ? 'Generating…'
              : mode === 'project'
                ? 'Generate Project Explainer'
                : 'Generate explainer'}
          </button>

          {startError && (
            <div className="rounded-lg border border-run-errored/25 bg-run-errored/10 px-3 py-2 text-[11px] text-run-errored">
              {startError}
            </div>
          )}

          {/* The gallery of past explainers (re-render any in the same sandboxed iframe). */}
          <div className="mt-2 border-t border-glass-line pt-3">
            <ExplainGallery project={project} client={client} onView={onViewPast} />
          </div>
        </div>
      </GlassPanel>

      {/* RIGHT: the render stage — sandboxed document, live progress, or empty hint. */}
      <GlassPanel className="explain-stage min-w-0 flex-1">
        {showGallery ? (
          <div className="flex h-full min-h-0 flex-col">
            <div className="flex items-center gap-2 border-b border-glass-line px-5 py-3">
              <span className="truncate text-sm font-semibold text-ink-100" title={galleryCaption}>
                {galleryCaption || 'Saved explainer'}
              </span>
              {galleryRunId && (
                <a
                  href={explainExportUrl(project, galleryRunId)}
                  download
                  className="ml-auto rounded-md px-2 py-1 text-[11px] font-medium text-ink-300 hover:bg-base-700/60 hover:text-ink-100"
                  title="Export explainer archive"
                >
                  ↓ Export
                </a>
              )}
              <span className={cx('rounded-full bg-base-700/60 px-2 py-0.5 text-[10px] uppercase tracking-wide text-ink-400', !galleryRunId && 'ml-auto')}>
                from gallery
              </span>
            </div>
            <div className="min-h-0 flex-1 p-3">
              <ExplainFrame html={galleryHtml as string} title={galleryCaption || 'Saved explainer'} />
            </div>
          </div>
        ) : showDocument ? (
          <div className="flex h-full min-h-0 flex-col">
            <div className="flex items-center gap-2 border-b border-glass-line px-5 py-3">
              <span className="text-sm font-semibold text-ink-100">
                {runState.transcript?.status_label === 'completed' ? 'Explainer ready' : 'Explainer'}
              </span>
              {runId && (
                <a
                  href={explainExportUrl(project, runId)}
                  download
                  className="ml-auto rounded-md px-2 py-1 text-[11px] font-medium text-ink-300 hover:bg-base-700/60 hover:text-ink-100"
                  title="Export explainer archive"
                >
                  ↓ Export
                </a>
              )}
              <span className={cx('rounded-full bg-mint-500/15 px-2 py-0.5 text-[10px] uppercase tracking-wide text-mint-300', !runId && 'ml-auto')}>
                sandboxed
              </span>
            </div>
            <div className="min-h-0 flex-1 p-3">
              <ExplainFrame html={runState.html} title="Code explainer" />
            </div>
          </div>
        ) : showRunError ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-10 text-center">
            <p className="text-sm font-medium text-run-errored">The explainer could not be generated.</p>
            <p className="max-w-md text-xs text-ink-500">{runState.error}</p>
          </div>
        ) : showProgress ? (
          <RunTranscriptView
            transcript={runState.transcript}
            live={runState.polling}
            emptyHint="Starting the explainer…"
          />
        ) : (
          <div className="flex h-full items-center justify-center p-10 text-center">
            <p className="max-w-sm text-sm text-ink-500">
              Click <span className="text-ink-300">Generate Project Explainer</span>, or open
              <span className="text-ink-300"> Advanced</span> to drill into a file or function —
              the explainer streams in here, then renders as an interactive document.
            </p>
          </div>
        )}
      </GlassPanel>
    </div>
  )
}

/** A small labelled text input (the picker's fields). */
function Field({
  id,
  label,
  placeholder,
  value,
  onChange,
  help,
}: {
  id: string
  label: string
  placeholder?: string
  value: string
  onChange: (v: string) => void
  help?: string
}) {
  return (
    <div>
      <label
        htmlFor={id}
        className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-ink-400"
      >
        {label}
      </label>
      <input
        id={id}
        type="text"
        spellCheck={false}
        autoComplete="off"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="glass-soft w-full rounded-lg bg-base-800/40 px-2.5 py-1.5 font-mono text-[12.5px] text-ink-100 placeholder:text-ink-600 focus:outline-none focus:ring-1 focus:ring-mint-400/40"
      />
      {help && <p className="mt-1 text-[11px] text-ink-500">{help}</p>}
    </div>
  )
}
