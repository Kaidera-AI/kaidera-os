/**
 * WorkspaceColumn — the RIGHT column. The project's working-folder file tree.
 *
 * A read-only, lazily-loaded tree of the project's `repo_root`: each folder fetches its
 * children on first expand (`GET /workspace/{project}/filetree?path=`); the secure walk
 * (rejects `..`/symlink escapes) lives server-side in `ws.list_dir`. Collapsible to a thin
 * strip via a header toggle, remembered across sessions. Files are display-only here.
 */
import { useCallback, useEffect, useState } from 'react'
import { GlassModal, GlassPanel } from '../components/glass'
import { cx } from '../components/ui'
import type { WorkspaceEntry, WorkspaceFile } from '../api/types'

/** The slice of the api client this column needs (structural — `api` satisfies it). */
export interface WorkspaceClient {
  getWorkspaceTree: (
    project: string,
    path?: string,
    signal?: AbortSignal,
  ) => Promise<{ path: string; entries: WorkspaceEntry[]; error?: string }>
  getWorkspaceFile: (project: string, path: string, signal?: AbortSignal) => Promise<WorkspaceFile>
  saveWorkspaceFile: (
    project: string,
    path: string,
    content: string,
    signal?: AbortSignal,
  ) => Promise<{ ok: boolean; path: string; error?: string }>
}

const WS_COLLAPSED_KEY = 'kaidera-os:workspace-collapsed'

/** Render one directory's rows; folders recurse via the loaded-dirs map. */
function TreeRows({
  dir,
  depth,
  loaded,
  expanded,
  loading,
  onToggle,
  onOpenFile,
}: {
  dir: string
  depth: number
  loaded: Record<string, WorkspaceEntry[]>
  expanded: Set<string>
  loading: Set<string>
  onToggle: (path: string) => void
  onOpenFile: (path: string) => void
}) {
  const entries = loaded[dir]
  if (!entries) return null
  return (
    <>
      {entries.map((e) => {
        const isOpen = e.is_dir && expanded.has(e.path)
        return (
          <div key={e.path}>
            <button
              type="button"
              onClick={() => (e.is_dir ? onToggle(e.path) : onOpenFile(e.path))}
              title={e.path}
              className={cx(
                'flex w-full items-center gap-1 rounded px-1.5 py-1 text-left text-xs transition-colors hover:bg-base-800/50',
                e.is_dir ? 'text-ink-200' : 'text-ink-400',
              )}
              style={{ paddingLeft: `${depth * 0.85 + 0.4}rem` }}
            >
              <span aria-hidden="true" className="w-3 shrink-0 text-center text-ink-500">
                {e.is_dir ? (isOpen ? '▾' : '▸') : ''}
              </span>
              <span aria-hidden="true" className="shrink-0">{e.is_dir ? '📁' : '📄'}</span>
              <span className="truncate">{e.name}</span>
              {e.is_dir && loading.has(e.path) && (
                <span className="ml-auto shrink-0 text-[10px] text-ink-500">…</span>
              )}
            </button>
            {isOpen && (
              <TreeRows
                dir={e.path}
                depth={depth + 1}
                loaded={loaded}
                expanded={expanded}
                loading={loading}
                onToggle={onToggle}
                onOpenFile={onOpenFile}
              />
            )}
          </div>
        )
      })}
    </>
  )
}

/** A file viewer + inline editor (modal). Reads on open; Edit toggles a textarea; Save writes back. */
function FileViewerModal({
  project,
  path,
  client,
  onClose,
}: {
  project: string
  path: string
  client: WorkspaceClient
  onClose: () => void
}) {
  const [file, setFile] = useState<WorkspaceFile | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const ctrl = new AbortController()
    queueMicrotask(() => {
      if (!ctrl.signal.aborted) {
        setFile(null)
        setErr(null)
        setEditing(false)
      }
    })
    client
      .getWorkspaceFile(project, path, ctrl.signal)
      .then((f) => {
        if (f.error) setErr(f.error)
        setFile(f)
        setDraft(f.content ?? '')
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) setErr(e instanceof Error ? e.message : String(e))
      })
    return () => ctrl.abort()
  }, [client, project, path])

  const save = () => {
    setSaving(true)
    setErr(null)
    client
      .saveWorkspaceFile(project, path, draft)
      .then((r) => {
        if (r.error) setErr(r.error)
        else {
          setEditing(false)
          setFile((f) => (f ? { ...f, content: draft } : f))
        }
      })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setSaving(false))
  }

  const editable = file != null && !file.binary
  return (
    <GlassModal open onClose={onClose} title={path}>
      <div className="flex max-h-[70vh] min-h-[12rem] flex-col gap-2">
        {err && <p className="text-xs text-run-errored/80">{err}</p>}
        {!file && !err && <p className="text-xs text-ink-500">Loading…</p>}
        {file?.binary && <p className="text-xs text-ink-500">Binary file ({file.size} bytes) — not shown.</p>}
        {file?.truncated && (
          <p className="text-[10px] text-ink-500">Showing the first part of a large file (read-only).</p>
        )}
        {editable && (editing ? (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className="min-h-[40vh] flex-1 resize-none rounded border border-glass-line bg-base-950/60 p-3 font-mono text-xs text-ink-200 focus:outline-none focus:ring-1 focus:ring-mint-400/40"
          />
        ) : (
          <pre className="flex-1 overflow-auto rounded border border-glass-line bg-base-950/60 p-3 font-mono text-xs text-ink-200">
            {file?.content ?? ''}
          </pre>
        ))}
        <div className="flex shrink-0 items-center justify-end gap-2">
          {editable && !editing && (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="rounded-md bg-base-800/60 px-3 py-1 text-xs text-ink-200 hover:bg-base-800"
            >
              Edit
            </button>
          )}
          {editable && editing && (
            <>
              <button
                type="button"
                onClick={() => {
                  setEditing(false)
                  setDraft(file?.content ?? '')
                }}
                className="rounded-md px-3 py-1 text-xs text-ink-400 hover:text-ink-200"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={save}
                disabled={saving || file?.truncated}
                title={file?.truncated ? 'Large/truncated files are read-only here' : undefined}
                className="rounded-md bg-mint-500/15 px-3 py-1 text-xs font-semibold text-mint-200 ring-1 ring-mint-400/40 hover:bg-mint-500/25 disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </>
          )}
        </div>
      </div>
    </GlassModal>
  )
}

export function WorkspaceColumn({ project, client }: { project: string | null; client: WorkspaceClient }) {
  const [loaded, setLoaded] = useState<Record<string, WorkspaceEntry[]>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [openFile, setOpenFile] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState(
    () => typeof localStorage !== 'undefined' && localStorage.getItem(WS_COLLAPSED_KEY) === '1',
  )

  const toggleCollapsed = () =>
    setCollapsed((c) => {
      const next = !c
      try {
        localStorage.setItem(WS_COLLAPSED_KEY, next ? '1' : '0')
      } catch {
        /* private mode — non-fatal */
      }
      return next
    })

  // Fetch one directory's entries (idempotent — caches into `loaded`).
  const fetchDir = useCallback(
    (path: string) => {
      if (!project) return
      setLoading((s) => new Set(s).add(path))
      client
        .getWorkspaceTree(project, path)
        .then((res) => {
          if (res.error) setError(res.error)
          setLoaded((m) => ({ ...m, [path]: res.entries ?? [] }))
        })
        .catch((e) => setError(e instanceof Error ? e.message : String(e)))
        .finally(() =>
          setLoading((s) => {
            const n = new Set(s)
            n.delete(path)
            return n
          }),
        )
    },
    [client, project],
  )

  // Reset + load the root when the project changes.
  useEffect(() => {
    queueMicrotask(() => {
      setLoaded({})
      setExpanded(new Set())
      setError(null)
      setOpenFile(null)
      if (project) fetchDir('')
    })
  }, [project, fetchDir])

  const onToggle = (path: string) => {
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(path)) {
        n.delete(path)
      } else {
        n.add(path)
        if (!loaded[path]) fetchDir(path)
      }
      return n
    })
  }

  if (collapsed) {
    return (
      <GlassPanel as="aside" className="flex w-9 shrink-0 flex-col items-center py-3 max-lg:order-4 max-lg:h-12 max-lg:w-full max-lg:flex-row max-lg:px-3 max-lg:py-2">
        <button
          type="button"
          onClick={toggleCollapsed}
          title="Expand workspace files"
          aria-label="Expand workspace files"
          className="flex h-6 w-6 items-center justify-center rounded-md text-ink-400 transition-colors hover:bg-base-800/60 hover:text-ink-200"
        >
          <span aria-hidden="true">📁</span>
        </button>
      </GlassPanel>
    )
  }

  return (
    <GlassPanel as="aside" className="flex w-64 shrink-0 flex-col max-lg:order-4 max-lg:h-[28rem] max-lg:w-full">
      <header className="flex items-center justify-between border-b border-glass-line px-3 py-2.5">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-500">
          Workspace
        </h2>
        <button
          type="button"
          onClick={toggleCollapsed}
          title="Collapse workspace files"
          aria-label="Collapse workspace files"
          className="flex h-6 w-6 items-center justify-center rounded-md text-ink-400 transition-colors hover:bg-base-800/60 hover:text-ink-200"
        >
          <span aria-hidden="true" className="text-sm leading-none">»</span>
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto p-1.5">
        {!project && <p className="px-2 py-2 text-xs text-ink-500">Select a project.</p>}
        {project && error && !loaded[''] && (
          <p
            className="px-2 py-2 text-xs leading-relaxed text-ink-500"
            title={error}
          >
            Workspace files aren’t available for this project here. (The native console reads the
            project folder directly; a containerized console needs it mounted.)
          </p>
        )}
        {project && !error && !loaded[''] && (
          <p className="px-2 py-2 text-xs text-ink-500">Loading files…</p>
        )}
        {project && loaded[''] && loaded[''].length === 0 && (
          <p className="px-2 py-2 text-xs text-ink-500">Empty working folder.</p>
        )}
        <TreeRows
          dir=""
          depth={0}
          loaded={loaded}
          expanded={expanded}
          loading={loading}
          onToggle={onToggle}
          onOpenFile={setOpenFile}
        />
      </div>

      {/* File viewer / editor — opens when a file row is clicked. */}
      {openFile && project && (
        <FileViewerModal
          project={project}
          path={openFile}
          client={client}
          onClose={() => setOpenFile(null)}
        />
      )}
    </GlassPanel>
  )
}
