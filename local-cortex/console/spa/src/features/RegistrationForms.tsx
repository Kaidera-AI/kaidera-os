/**
 * RegistrationForms — the in-console add-agent / add-project modal forms (feature-gap #81).
 *
 * Two `GlassModal`-based forms backing the registration affordances:
 *   * AddAgentModal   — "+ Add worker" (top of the AgentsColumn): name + role preset/role
 *                       + harness/model/reasoning (harness-aware, from the config catalog) +
 *                       writer_scope → POST /agents/{project}/register.
 *   * AddProjectModal — "+ Add project" (ProjectRail): project_key + display_name + repo_root
 *                       (absolute) → POST /projects/register.
 *
 * Both follow the SPA house pattern: a single submit that calls the client, surfaces the
 * backend's friendly `{ok, error}` (a degraded write — caller-not-a-writer / admin-token-
 * missing — shows the human error, never a token), and on SUCCESS calls `onDone` (the shell's
 * refetch — the new agent/project appears) + closes. Reuses GlassModal + the glass field/btn
 * styles for visual consistency.
 */

import { useCallback, useEffect, useState } from 'react'
import { GlassModal } from '../components/glass'
import { cx } from '../components/ui'
import type {
  AgentConfigCatalog,
  DeregisterAgentResult,
  ProjectPackListResult,
  ProjectPackOption,
  RegisterAgentPayload,
  RegisterAgentResult,
  RegisterProjectPayload,
  RegisterProjectResult,
} from '../api'

const FIELD_CLASS =
  'glass-soft w-full rounded-md border border-glass-line bg-base-900/40 px-2.5 py-1.5 text-xs ' +
  'text-ink-100 outline-none transition-colors placeholder:text-ink-600 ' +
  'focus:border-mint-400/50 focus:ring-1 focus:ring-mint-400/30 disabled:opacity-50'

const LABEL_CLASS =
  'mb-1 block text-[10px] font-semibold uppercase tracking-wide text-ink-500'

const PRIMARY_BTN =
  'shrink-0 rounded-md px-3 py-1.5 text-[11px] font-semibold transition-colors ' +
  'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/30 hover:bg-mint-500/25 ' +
  'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-mint-500/15'

const GHOST_BTN =
  'shrink-0 rounded-md px-3 py-1.5 text-[11px] font-medium text-ink-400 ' +
  'transition-colors hover:bg-base-800/60 hover:text-ink-100'

/** A small inline error banner (the friendly, non-leaky backend message). */
function ErrorNote({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="rounded-md bg-run-errored/12 px-3 py-2 text-[11px] leading-relaxed text-run-errored/90"
    >
      {message}
    </p>
  )
}

// ===========================================================================
//  AddAgentModal
// ===========================================================================

export interface AddAgentClient {
  configCatalog: (project: string, signal?: AbortSignal) => Promise<AgentConfigCatalog>
  registerAgent: (
    project: string,
    body: RegisterAgentPayload,
    signal?: AbortSignal,
  ) => Promise<RegisterAgentResult>
}

interface AddAgentModalProps {
  open: boolean
  onClose: () => void
  project: string
  client: AddAgentClient
  /** Called after a successful register — the shell refetches the roster (the new agent appears). */
  onDone: () => void
}

interface AgentForm {
  name: string
  role: string
  harness: string
  model: string
  reasoning: string
  designation: string
  auto_dispatch: string
  writer_scope: string
}

const EMPTY_AGENT: AgentForm = {
  name: '',
  role: '',
  harness: '',
  model: '',
  reasoning: '',
  designation: '',
  auto_dispatch: '',
  writer_scope: 'work',
}

const ROLE_PRESETS = [
  {
    value: '',
    label: 'Custom role',
    help: 'Use the free-text role and let the registry infer the worker type.',
  },
  {
    value: 'interactive-lead',
    label: 'Interactive lead',
    role: 'lead',
    designation: 'interactive',
    auto_dispatch: 'true',
    help: 'Chat-facing lead that can also auto-run handoffs assigned to it.',
  },
  {
    value: 'pm',
    label: 'PM AI Agent',
    role: 'pm',
    designation: 'autonomous',
    auto_dispatch: 'false',
    help: 'Model-backed, non-interactive PM support worker.',
  },
  {
    value: 'ai-worker',
    label: 'AI worker',
    role: '',
    designation: 'autonomous',
    auto_dispatch: 'false',
    help: 'Model-backed worker that runs assigned tasks without direct chat.',
  },
  {
    value: 'orchestrator',
    label: 'Orchestrator',
    role: 'orchestrator',
    designation: 'deterministic',
    auto_dispatch: 'false',
    help: 'Deterministic dispatch coordinator; no model and no chat.',
  },
  {
    value: 'deterministic-worker',
    label: 'Deterministic worker',
    role: '',
    designation: 'deterministic',
    auto_dispatch: 'false',
    help: 'Packaged code/scheduled worker with no model and no chat.',
  },
] as const

function rolePresetFor(form: AgentForm): string {
  const role = form.role.trim().toLowerCase()
  if (role === 'orchestrator' && form.designation === 'deterministic') return 'orchestrator'
  if (form.designation === 'deterministic') return 'deterministic-worker'
  if ((role === 'pm' || role === 'project manager' || role === 'project-manager') && form.designation === 'autonomous') return 'pm'
  if (form.designation === 'autonomous') return 'ai-worker'
  if (
    form.designation === 'interactive' &&
    (role === 'lead' || role === 'cpo' || role === 'cmo' || role.includes('lead') || role.includes('cpo') || role.includes('cmo'))
  ) return 'interactive-lead'
  return ''
}

export function AddAgentModal({ open, onClose, project, client, onDone }: AddAgentModalProps) {
  const [catalog, setCatalog] = useState<AgentConfigCatalog | null>(null)
  const [form, setForm] = useState<AgentForm>(EMPTY_AGENT)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Reset to a FRESH form each time the modal opens — done DURING render via an
  // open-transition sentinel (the codebase's "adjust state during render" idiom,
  // NOT setState-in-an-effect), so a re-open never shows the last submission.
  const [wasOpen, setWasOpen] = useState(false)
  if (open && !wasOpen) {
    setWasOpen(true)
    setForm(EMPTY_AGENT)
    setError(null)
    setSubmitting(false)
  } else if (!open && wasOpen) {
    setWasOpen(false)
  }

  // Load the config catalog while open so the harness/model/reasoning dropdowns
  // mirror the Configure editor. Async-only effect (every setState is inside the
  // promise callback, never synchronously in the effect body).
  useEffect(() => {
    if (!open) return
    const ctrl = new AbortController()
    let alive = true
    client
      .configCatalog(project, ctrl.signal)
      .then((cat) => {
        if (!alive) return
        setCatalog(cat)
        // seed harness/model from the catalog defaults so the dropdowns aren't blank.
        setForm((prev) => ({
          ...prev,
          harness: prev.harness || cat.default_harness || '',
          model: prev.model || cat.default_model || '',
        }))
      })
      .catch(() => {
        /* a missing catalog just leaves the dropdowns minimal — name+role still submit */
      })
    return () => {
      alive = false
      ctrl.abort()
    }
  }, [open, project, client])

  const harnessModels = catalog?.models_by_harness[form.harness] ?? []
  const harnessReasoning = catalog?.reasoning_by_harness[form.harness] ?? []
  const isDeterministic = form.designation === 'deterministic'
  const autoDispatchOn = form.auto_dispatch === 'true'
  const activeRolePreset = ROLE_PRESETS.find((preset) => preset.value === rolePresetFor(form))

  const setHarness = useCallback(
    (next: string) => {
      setError(null)
      setForm((prev) => {
        const models = catalog?.models_by_harness[next] ?? []
        const reasoning = catalog?.reasoning_by_harness[next] ?? []
        const model = models.some((m) => m.value === prev.model) ? prev.model : models[0]?.value ?? ''
        const reason = reasoning.some((r) => r.value === prev.reasoning)
          ? prev.reasoning
          : reasoning[0]?.value ?? ''
        return { ...prev, harness: next, model, reasoning: reason }
      })
    },
    [catalog],
  )

  function set<K extends keyof AgentForm>(key: K, value: AgentForm[K]) {
    setError(null)
    setForm((prev) => {
      const next = { ...prev, [key]: value }
      if (key === 'designation' && value === 'deterministic') {
        next.auto_dispatch = 'false'
      }
      return next
    })
  }

  function applyRolePreset(value: string) {
    setError(null)
    const preset = ROLE_PRESETS.find((p) => p.value === value)
    if (!preset || !('role' in preset)) return
    setForm((prev) => ({
      ...prev,
      role: preset.role || prev.role,
      designation: preset.designation,
      auto_dispatch: preset.auto_dispatch,
    }))
  }

  function submit() {
    const name = form.name.trim()
    const role = form.role.trim()
    if (!name) {
      setError('A worker name is required.')
      return
    }
    if (!role) {
      setError('A role is required.')
      return
    }
    setSubmitting(true)
    setError(null)
    const body: RegisterAgentPayload = { name, role }
    if (!isDeterministic) {
      if (form.harness) body.harness = form.harness
      if (form.model) body.model = form.model
      if (form.reasoning) body.reasoning = form.reasoning
    }
    if (form.designation) body.designation = form.designation
    if (form.auto_dispatch) body.auto_dispatch = form.auto_dispatch
    if (form.writer_scope) body.writer_scope = form.writer_scope
    client
      .registerAgent(project, body)
      .then((res) => {
        if (res.ok) {
          onDone()
          onClose()
        } else {
          setError(res.error || "Couldn't add the agent.")
        }
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSubmitting(false))
  }

  return (
    <GlassModal open={open} onClose={onClose} title="Add worker">
      <form
        className="space-y-3 p-5"
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <p className="text-[11px] leading-relaxed text-ink-400">
          Register a new worker on <span className="font-mono text-ink-300">{project}</span>. It
          lands in the project roster (the console writer authorises the write).
        </p>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="add-agent-name" className={LABEL_CLASS}>
              Name
            </label>
            <input
              id="add-agent-name"
              className={FIELD_CLASS}
              value={form.name}
              disabled={submitting}
              autoComplete="off"
              spellCheck={false}
              placeholder="e.g. quinn"
              onChange={(e) => set('name', e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="add-agent-role-preset" className={LABEL_CLASS}>
              Role preset
            </label>
            <select
              id="add-agent-role-preset"
              className={FIELD_CLASS}
              value={rolePresetFor(form)}
              disabled={submitting}
              onChange={(e) => applyRolePreset(e.target.value)}
            >
              {ROLE_PRESETS.map((preset) => (
                <option key={preset.value || 'custom'} value={preset.value}>
                  {preset.label}
                </option>
              ))}
            </select>
            <p className="mt-1 text-[10px] leading-relaxed text-ink-600">
              {activeRolePreset?.help ?? 'Choose how this worker should behave.'}
            </p>
          </div>
          <div>
            <label htmlFor="add-agent-role" className={LABEL_CLASS}>
              Role
            </label>
            <input
              id="add-agent-role"
              className={FIELD_CLASS}
              value={form.role}
              disabled={submitting}
              autoComplete="off"
              spellCheck={false}
              placeholder="e.g. full-stack-developer"
              onChange={(e) => set('role', e.target.value)}
            />
          </div>

          {isDeterministic ? (
            <div className="rounded-lg border border-glass-line bg-base-800/40 px-3 py-2 text-[11px] leading-relaxed text-ink-500 sm:col-span-2">
              Deterministic workers do not use harness/model/reasoning. Use this for the
              orchestrator or packaged code agents.
            </div>
          ) : (
            <>
          <div>
            <label htmlFor="add-agent-harness" className={LABEL_CLASS}>
              Harness
            </label>
            <select
              id="add-agent-harness"
              className={FIELD_CLASS}
              value={form.harness}
              disabled={submitting}
              onChange={(e) => setHarness(e.target.value)}
            >
              <option value="">— default —</option>
              {(catalog?.harnesses ?? []).map((h) => (
                <option key={h.value} value={h.value}>
                  {h.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="add-agent-model" className={LABEL_CLASS}>
              Model
            </label>
            <select
              id="add-agent-model"
              className={FIELD_CLASS}
              value={form.model}
              disabled={submitting}
              onChange={(e) => set('model', e.target.value)}
            >
              <option value="">— default —</option>
              {harnessModels.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
              {form.model && !harnessModels.some((m) => m.value === form.model) && (
                <option value={form.model}>{form.model}</option>
              )}
            </select>
          </div>

          <div>
            <label htmlFor="add-agent-reasoning" className={LABEL_CLASS}>
              Reasoning
            </label>
            <select
              id="add-agent-reasoning"
              className={FIELD_CLASS}
              value={form.reasoning}
              disabled={submitting}
              onChange={(e) => set('reasoning', e.target.value)}
            >
              <option value="">— default —</option>
              {harnessReasoning.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>
            </>
          )}
        </div>

        <div className="rounded-lg border border-glass-line bg-base-900/35 px-3 py-2">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <div className={LABEL_CLASS}>Allow auto-run when assigned</div>
              <p className="mt-1 text-[11px] leading-relaxed text-ink-500">
                When the global engine and project dispatch are on, handoffs assigned to this
                worker can start automatically. Project dispatch is controlled from the Dashboard.
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-label="Allow auto-run when assigned"
              aria-checked={autoDispatchOn}
              disabled={submitting || isDeterministic}
              onClick={() => set('auto_dispatch', autoDispatchOn ? 'false' : 'true')}
              className={cx(
                'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors',
                'disabled:cursor-not-allowed disabled:opacity-45',
                autoDispatchOn ? 'bg-mint-500/70' : 'bg-base-700/70',
              )}
            >
              <span
                className={cx(
                  'inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform',
                  autoDispatchOn ? 'translate-x-[18px]' : 'translate-x-[3px]',
                )}
              />
            </button>
          </div>
        </div>

        <div>
          <label htmlFor="add-agent-scope" className={LABEL_CLASS}>
            Writer scope
          </label>
          <select
            id="add-agent-scope"
            className={cx(FIELD_CLASS, 'sm:w-1/2')}
            value={form.writer_scope}
            disabled={submitting}
            onChange={(e) => set('writer_scope', e.target.value)}
          >
            <option value="work">work (decisions / lessons / handoffs)</option>
            <option value="full">full (everything)</option>
            <option value="none">none (read-only)</option>
          </select>
        </div>

        {error && <ErrorNote message={error} />}

        <div className="flex items-center justify-end gap-2 pt-1">
          <button type="button" className={GHOST_BTN} disabled={submitting} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={PRIMARY_BTN} disabled={submitting}>
            {submitting ? 'Adding…' : 'Add worker'}
          </button>
        </div>
      </form>
    </GlassModal>
  )
}

// ===========================================================================
//  AddProjectModal
// ===========================================================================

export interface AddProjectClient {
  listProjectPacks?: (repoRoot: string, signal?: AbortSignal) => Promise<ProjectPackListResult>
  registerProject: (
    body: RegisterProjectPayload,
    signal?: AbortSignal,
  ) => Promise<RegisterProjectResult>
}

interface AddProjectModalProps {
  open: boolean
  onClose: () => void
  client: AddProjectClient
  /** Called after a successful register — the shell refetches the project rail. */
  onDone: () => void
}

interface ProjectForm {
  project_key: string
  display_name: string
  description: string
  repo_root: string
  lead_name: string
  project_pack_key: string
}

const EMPTY_PROJECT: ProjectForm = {
  project_key: '',
  display_name: '',
  description: '',
  repo_root: '',
  lead_name: '',
  project_pack_key: '',
}

export function AddProjectModal({ open, onClose, client, onDone }: AddProjectModalProps) {
  const [form, setForm] = useState<ProjectForm>(EMPTY_PROJECT)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [packs, setPacks] = useState<ProjectPackOption[]>([])
  const [packsLoading, setPacksLoading] = useState(false)
  const [packsError, setPacksError] = useState<string | null>(null)

  // Fresh form on each open — DURING render via an open-transition sentinel (the
  // "adjust state during render" idiom, not setState-in-an-effect).
  const [wasOpen, setWasOpen] = useState(false)
  if (open && !wasOpen) {
    setWasOpen(true)
    setForm(EMPTY_PROJECT)
    setError(null)
    setPacks([])
    setPacksError(null)
    setPacksLoading(false)
    setSubmitting(false)
  } else if (!open && wasOpen) {
    setWasOpen(false)
  }

  function set<K extends keyof ProjectForm>(key: K, value: ProjectForm[K]) {
    setError(null)
    setForm((prev) => {
      const next = { ...prev, [key]: value }
      if (key === 'repo_root') next.project_pack_key = ''
      return next
    })
    if (key === 'repo_root') {
      setPacks([])
      setPacksError(null)
    }
  }

  function selectPack(key: string) {
    const pack = packs.find((p) => p.key === key)
    setError(null)
    setForm((prev) => ({
      ...prev,
      project_pack_key: key,
      project_key: prev.project_key.trim() ? prev.project_key : pack?.default_project_key || prev.project_key,
    }))
  }

  function scanPacks() {
    if (!client.listProjectPacks) return
    const root = form.repo_root.trim()
    if (!root) {
      setPacks([])
      setPacksError('Enter an absolute project folder before scanning for packs.')
      return
    }
    if (!root.startsWith('/')) {
      setPacks([])
      setPacksError('The project folder (repo_root) must be an absolute path.')
      return
    }
    setPacksLoading(true)
    setPacksError(null)
    client
      .listProjectPacks(root)
      .then((res) => {
        if (!res.ok) {
          setPacks([])
          setPacksError(res.error || "Couldn't read installed project packs.")
          return
        }
        setPacks(res.packs || [])
        setForm((prev) => {
          if (!prev.project_pack_key || (res.packs || []).some((p) => p.key === prev.project_pack_key)) {
            return prev
          }
          return { ...prev, project_pack_key: '' }
        })
      })
      .catch((e: unknown) => {
        setPacks([])
        setPacksError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setPacksLoading(false))
  }

  function submit() {
    const key = form.project_key.trim()
    const root = form.repo_root.trim()
    if (!key) {
      setError('A project key is required.')
      return
    }
    if (root && !root.startsWith('/')) {
      setError('The project folder (repo_root) must be an absolute path.')
      return
    }
    setSubmitting(true)
    setError(null)
    const body: RegisterProjectPayload = { project_key: key }
    if (form.display_name.trim()) body.display_name = form.display_name.trim()
    if (form.description.trim()) body.description = form.description.trim()
    if (root) body.repo_root = root
    if (form.lead_name.trim()) body.lead_name = form.lead_name.trim()
    if (form.project_pack_key.trim()) body.project_pack_key = form.project_pack_key.trim()
    client
      .registerProject(body)
      .then((res) => {
        if (res.ok) {
          onDone()
          onClose()
        } else {
          setError(res.error || "Couldn't add the project.")
        }
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSubmitting(false))
  }

  return (
    <GlassModal open={open} onClose={onClose} title="Add project">
      <form
        className="space-y-3 p-5"
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <p className="text-[11px] leading-relaxed text-ink-400">
          Register a new project + its first AI worker. Give it a name, a one-line scope, and a
          workspace folder; we seed a lead worker (named below) you can chat with right away.
        </p>

        <div>
          <label htmlFor="add-project-key" className={LABEL_CLASS}>
            Project key
          </label>
          <input
            id="add-project-key"
            className={FIELD_CLASS}
            value={form.project_key}
            disabled={submitting}
            autoComplete="off"
            spellCheck={false}
            placeholder="e.g. acme-app"
            onChange={(e) => set('project_key', e.target.value)}
          />
        </div>

        <div>
          <label htmlFor="add-project-name" className={LABEL_CLASS}>
            Display name <span className="text-ink-600">(optional)</span>
          </label>
          <input
            id="add-project-name"
            className={FIELD_CLASS}
            value={form.display_name}
            disabled={submitting}
            autoComplete="off"
            placeholder="e.g. Acme App"
            onChange={(e) => set('display_name', e.target.value)}
          />
        </div>

        <div>
          <label htmlFor="add-project-description" className={LABEL_CLASS}>
            Description / scope <span className="text-ink-600">(what this project is for)</span>
          </label>
          <textarea
            id="add-project-description"
            className={cx(FIELD_CLASS, 'min-h-[60px] resize-y')}
            value={form.description}
            disabled={submitting}
            placeholder="e.g. Marketing automation for ACME — plan, create + publish content; track what's approved + shipped."
            onChange={(e) => set('description', e.target.value)}
          />
          <p className="mt-1 text-[10px] leading-relaxed text-ink-600">
            This becomes your lead worker&rsquo;s starting brief — its role, skills, and the team it
            builds are shaped from this and your first conversation.
          </p>
        </div>

        <div>
          <label htmlFor="add-project-root" className={LABEL_CLASS}>
            Project folder <span className="text-ink-600">(absolute path)</span>
          </label>
          <input
            id="add-project-root"
            className={FIELD_CLASS}
            value={form.repo_root}
            disabled={submitting}
            autoComplete="off"
            spellCheck={false}
            placeholder="~/code/acme-app"
            onChange={(e) => set('repo_root', e.target.value)}
          />
        </div>

        {client.listProjectPacks && (
          <div className="rounded-lg border border-glass-line bg-base-900/25 p-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <label htmlFor="add-project-pack" className={LABEL_CLASS}>
                  Project pack <span className="text-ink-600">(optional)</span>
                </label>
                <p className="text-[10px] leading-relaxed text-ink-600">
                  Scans this folder&rsquo;s .kaidera-os/project-packs directory. Selected pack seed
                  files are ingested into this new project only.
                </p>
              </div>
              <button
                type="button"
                className={GHOST_BTN}
                disabled={submitting || packsLoading}
                onClick={scanPacks}
              >
                {packsLoading ? 'Scanning…' : 'Scan packs'}
              </button>
            </div>

            <select
              id="add-project-pack"
              className={cx(FIELD_CLASS, 'mt-2')}
              value={form.project_pack_key}
              disabled={submitting || packsLoading || packs.length === 0}
              onChange={(e) => selectPack(e.target.value)}
            >
              <option value="">No pack</option>
              {packs.map((pack) => (
                <option key={pack.key} value={pack.key}>
                  {pack.name} ({pack.version}) · {pack.seed_count} seed file
                  {pack.seed_count === 1 ? '' : 's'}
                </option>
              ))}
            </select>
            {packs.length > 0 && (
              <p className="mt-1 text-[10px] leading-relaxed text-ink-600">
                {packs.length} installed pack{packs.length === 1 ? '' : 's'} found. Selecting a
                pack keeps its knowledge scoped to this project.
              </p>
            )}
            {packsError && <p className="mt-2 text-[10px] text-run-errored/90">{packsError}</p>}
          </div>
        )}

        <div>
          <label htmlFor="add-project-lead" className={LABEL_CLASS}>
            First lead worker&rsquo;s name <span className="text-ink-600">(optional)</span>
          </label>
          <input
            id="add-project-lead"
            className={FIELD_CLASS}
            value={form.lead_name}
            disabled={submitting}
            autoComplete="off"
            spellCheck={false}
            placeholder="e.g. your first AI worker's name (rename anytime)"
            onChange={(e) => set('lead_name', e.target.value)}
          />
        </div>

        {error && <ErrorNote message={error} />}

        <div className="flex items-center justify-end gap-2 pt-1">
          <button type="button" className={GHOST_BTN} disabled={submitting} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={PRIMARY_BTN} disabled={submitting}>
            {submitting ? 'Adding…' : 'Add project'}
          </button>
        </div>
      </form>
    </GlassModal>
  )
}

// ===========================================================================
//  Deregister confirm — a tiny confirm modal for the remove-agent action
// ===========================================================================

export interface DeregisterClient {
  deregisterAgent: (
    project: string,
    agent: string,
    signal?: AbortSignal,
  ) => Promise<DeregisterAgentResult>
}

interface DeregisterAgentModalProps {
  open: boolean
  onClose: () => void
  project: string
  agent: string
  client: DeregisterClient
  /** Called after a successful deregister — the shell refetches the roster. */
  onDone: () => void
}

export function DeregisterAgentModal({
  open,
  onClose,
  project,
  agent,
  client,
  onDone,
}: DeregisterAgentModalProps) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Clear any prior error/submitting on each open — DURING render via an
  // open-transition sentinel (not setState-in-an-effect).
  const [wasOpen, setWasOpen] = useState(false)
  if (open && !wasOpen) {
    setWasOpen(true)
    setError(null)
    setSubmitting(false)
  } else if (!open && wasOpen) {
    setWasOpen(false)
  }

  function confirm() {
    setSubmitting(true)
    setError(null)
    client
      .deregisterAgent(project, agent)
      .then((res) => {
        if (res.ok) {
          onDone()
          onClose()
        } else {
          setError(res.error || "Couldn't deregister the agent.")
        }
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSubmitting(false))
  }

  return (
    <GlassModal open={open} onClose={onClose} title="Deregister worker">
      <div className="space-y-3 p-5">
        <p className="text-[12px] leading-relaxed text-ink-300">
          Remove <span className="font-semibold text-ink-100">{agent}</span> from the{' '}
          <span className="font-mono text-ink-400">{project}</span> roster? It stops counting toward
          the project's active roster and writer set.
        </p>
        <p className="text-[11px] leading-relaxed text-ink-500">
          History is preserved — the agent's decisions, lessons, and handoffs stay intact, and you
          can re-add the agent later.
        </p>

        {error && <ErrorNote message={error} />}

        <div className="flex items-center justify-end gap-2 pt-1">
          <button type="button" className={GHOST_BTN} disabled={submitting} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="shrink-0 rounded-md bg-run-errored/15 px-3 py-1.5 text-[11px] font-semibold text-run-errored ring-1 ring-run-errored/30 transition-colors hover:bg-run-errored/25 disabled:cursor-not-allowed disabled:opacity-40"
            disabled={submitting}
            onClick={confirm}
          >
            {submitting ? 'Removing…' : 'Deregister'}
          </button>
        </div>
      </div>
    </GlassModal>
  )
}
