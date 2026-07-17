/**
 * AgentConfigEditor — the per-agent config editor, now HOMED in the agent-detail
 * middle pane (the CTO's "settings in the middle pane of the agent" directive). It
 * edits ONE agent — the one selected in the agents column — echoing the prototype's
 * inline header selects (`app/templates/_agent_detail_config.html`):
 *
 *   role preset · role · harness/model/reasoning (when model-backed) · queued work
 *
 * Each agent's EFFECTIVE config is its registry value overlaid with any console-local
 * override; a ● marks an overridden field and the "registry: …" hint shows the
 * source-of-truth value. The model list FOLLOWS the harness (repopulated client-side
 * from the config-catalog's `models_by_harness` on a harness change — no round-trip;
 * the kaidera/pi catalog lanes group by provider into <optgroup>s). Save →
 * `POST /settings/{p}/agents/{a}/config`, then refetch the agent's config-view (so the
 * panel lands on the authoritative post-save effective state) and call `onSaved` (the
 * shell's refresh — which regroups the agents column when a designation changed).
 *
 * Save is CONSOLE-LOCAL by design (feature-gap #81, the CTO's reversed decision): it does
 * NOT touch the Cortex registry (the registry stays authoritative). A separate, explicit
 * "Promote to registry" button → `POST /settings/{p}/agents/{a}/promote` pushes the agent's
 * current effective config INTO the registry on demand (the deliberate commit gesture).
 *
 * MOVED, NOT DUPLICATED: this is the exact step-1 Configure logic, lifted out of
 * SettingsView and re-homed here; Settings no longer carries any per-agent config.
 */

import { useCallback, useEffect, useState } from 'react'
import { GlassCard } from '../components/glass'
import { cx } from '../components/ui'
import { DeregisterAgentModal, type DeregisterClient } from './RegistrationForms'
import type {
  AgentConfigCatalog,
  AgentConfigView,
  AppSettingsWriteResult,
  AgentConfigWriteResult,
  AgentOverridePatch,
  ModelOption,
  PromoteResult,
  ReasoningOption,
} from '../api'

/**
 * The data surface the editor drives: the full harness→model+reasoning catalog, the
 * SELECTED agent's resolved config-view, and the override save. The concrete `api`
 * object satisfies this structurally (AgentDetail adapts `api.agentDetail` →
 * `agentConfigView`); tests pass a fake that records calls.
 */
export interface AgentConfigEditorClient {
  configCatalog: (project: string, signal?: AbortSignal) => Promise<AgentConfigCatalog>
  /** The selected agent's resolved config-view (the effective + registry + override-flag fields). */
  agentConfigView: (project: string, agent: string, signal?: AbortSignal) => Promise<AgentConfigView>
  /** Save the agent's CONSOLE-LOCAL override (does not touch the registry). */
  setAgentConfig: (
    project: string,
    agent: string,
    override: AgentOverridePatch,
  ) => Promise<AgentConfigWriteResult>
  /** EXPLICIT "Promote to registry" (feature-gap #81) — push the agent's current effective
   * config into the Cortex registry on demand. Distinct from Save (which stays console-local). */
  promoteAgent: (project: string, agent: string) => Promise<PromoteResult>
  /** Optional app-settings writer used for operator-added harness model catalog rows. */
  setAppSettings?: (
    project: string,
    settings: Record<string, unknown>,
  ) => Promise<AppSettingsWriteResult>
}

interface AgentConfigEditorProps {
  project: string | null
  agent: string | null
  client: AgentConfigEditorClient
  /** Called after a successful save — the shell refetches catalogs (regroups the roster on a designation change). */
  onSaved: () => void
  /** Optional deregister client (feature-gap #81) — enables the "Deregister agent" action +
   * its confirm modal. When absent, no remove action. */
  registrationClient?: DeregisterClient
  /** Called after a successful deregister — the shell refetches the roster + the modal owner can close. */
  onRemoved?: () => void
}

const FIELD_CLASS =
  'glass-soft w-full rounded-md border border-glass-line bg-base-900/40 px-2.5 py-1.5 text-xs ' +
  'text-ink-100 outline-none transition-colors placeholder:text-ink-600 ' +
  'focus:border-mint-400/50 focus:ring-1 focus:ring-mint-400/30 disabled:opacity-50'

const BTN_CLASS =
  'shrink-0 rounded-md px-2.5 py-1.5 text-[11px] font-semibold transition-colors ' +
  'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/30 hover:bg-mint-500/25 ' +
  'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-mint-500/15'

// The explicit "Promote to registry" action (feature-gap #81): glass-styled + neutral,
// deliberately DISTINCT from the mint Save so the commit-to-source-of-truth gesture
// reads as a separate action, not an accent of Save.
const PROMOTE_BTN_CLASS =
  'glass-soft shrink-0 rounded-md border border-glass-line px-2.5 py-1.5 text-[11px] ' +
  'font-semibold text-ink-200 transition-colors hover:border-mint-400/30 hover:text-ink-100 ' +
  'disabled:cursor-not-allowed disabled:opacity-40'

const LABEL_CLASS =
  'flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-ink-500'

const HARNESS_MODEL_OVERRIDES_KEY = 'harness_model_overrides'

/** A small "console override" dot — marks a field whose effective value ≠ registry. */
function OverrideDot() {
  return (
    <span
      data-testid="override-dot"
      title="console-local override (differs from the registry value)"
      className="inline-block h-1.5 w-1.5 rounded-full bg-mint-400"
    />
  )
}

/** The "registry: <value>" hint under a field. */
function RegistryHint({ value, suffix }: { value: string | null | undefined; suffix?: string }) {
  return (
    <span className="block text-[10px] text-ink-600">
      registry: <code className="font-mono text-ink-500">{value || '—'}</code>
      {suffix ? <span className="text-ink-600"> · {suffix}</span> : null}
    </span>
  )
}

/**
 * B3: the reasoning options for a SPECIFIC kaidera catalog model, or null when
 * the model isn't a known catalog model (caller falls back to the per-harness
 * list). An empty array means the model is a known NON-reasoner (hide the
 * dropdown). Prefers the catalog's `reasoning_by_model` map, falling back to the
 * model option's own `reasoning_levels` (and normalizing the `['supported']`
 * toggle placeholder to a single "on" option).
 */
function reasoningForModel(
  catalog: AgentConfigCatalog,
  harness: string,
  model: string,
): ReasoningOption[] | null {
  if (!model) return null
  const byModel =
    catalog.reasoning_by_model?.[`${harness}:${model}`] ??
    catalog.reasoning_by_model?.[model] // compatibility with pre-v0.1.226 payloads
  if (byModel) return byModel
  const opt = (catalog.models_by_harness[harness] ?? []).find((m) => m.value === model)
  if (!opt || opt.reasoning_levels === undefined) return null
  const levels = opt.reasoning_levels
  if (levels.length === 1 && levels[0] === 'supported') return [{ value: 'on', label: 'on' }]
  return levels.map((lvl) => ({ value: lvl, label: lvl }))
}

/** The model <select> options for a harness — flat (fixed lanes) or grouped by provider (catalog lanes). */
function ModelOptions({ models, current }: { models: ModelOption[]; current: string }) {
  const hasProviders = models.some((m) => m.provider)
  const ensureCurrent =
    current && !models.some((m) => m.value === current) ? (
      <option value={current}>{current}</option>
    ) : null

  if (!hasProviders) {
    return (
      <>
        {models.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label}
          </option>
        ))}
        {ensureCurrent}
        {models.length === 0 && !current && (
          <option value="">— no models advertised by this harness —</option>
        )}
      </>
    )
  }

  const order: string[] = []
  const byProvider: Record<string, ModelOption[]> = {}
  for (const m of models) {
    const p = m.provider || 'other'
    if (!(p in byProvider)) {
      byProvider[p] = []
      order.push(p)
    }
    byProvider[p].push(m)
  }
  return (
    <>
      {order.map((p) => (
        <optgroup key={p} label={p}>
          {byProvider[p].map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </optgroup>
      ))}
      {ensureCurrent}
    </>
  )
}

interface CardForm {
  harness: string
  model: string
  reasoning: string
  designation: string
  role: string
  auto_dispatch: string
}

const ROLE_PRESETS = [
  {
    value: 'registry',
    label: 'Registry default',
    role: '',
    designation: '',
    auto_dispatch: '',
    help: 'Use the role and worker type already stored in the project registry.',
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
  {
    value: 'custom',
    label: 'Custom role',
    help: 'Keep the current worker type and edit the free-text role below.',
  },
] as const

function rolePresetFor(form: CardForm): string {
  const role = form.role.trim().toLowerCase()
  if (!form.role && !form.designation) return 'registry'
  if (role === 'orchestrator' && form.designation === 'deterministic') return 'orchestrator'
  if (form.designation === 'deterministic') return 'deterministic-worker'
  if ((role === 'pm' || role === 'project manager' || role === 'project-manager') && form.designation === 'autonomous') return 'pm'
  if (form.designation === 'autonomous') return 'ai-worker'
  if (
    form.designation === 'interactive' &&
    (role === 'lead' || role === 'cpo' || role === 'cmo' || role.includes('lead') || role.includes('cpo') || role.includes('cmo'))
  ) return 'interactive-lead'
  return 'custom'
}

/** Seed the editable form from an agent's resolved config-view (effective values). */
function seedForm(cv: AgentConfigView): CardForm {
  return {
    harness: (cv.harness as string) || '',
    model: (cv.model as string) || '',
    reasoning: (cv.reasoning as string) || '',
    designation: cv.ov_designation ? (cv.designation as string) || '' : '',
    role: cv.ov_role ? (cv.role as string) || '' : '',
    auto_dispatch: cv.ov_auto_dispatch ? ((cv.auto_dispatch as boolean) ? 'true' : 'false') : '',
  }
}

export function AgentConfigEditor({
  project,
  agent,
  client,
  onSaved,
  registrationClient,
  onRemoved,
}: AgentConfigEditorProps) {
  const [catalog, setCatalog] = useState<AgentConfigCatalog | null>(null)
  const [cv, setCv] = useState<AgentConfigView | null>(null)
  const [form, setForm] = useState<CardForm | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [customModel, setCustomModel] = useState('')
  const [customModelError, setCustomModelError] = useState<string | null>(null)
  const [addingCustomModel, setAddingCustomModel] = useState(false)
  // EXPLICIT "Promote to registry" (feature-gap #81): the in-flight flag + the last
  // result (ok → "promoted ✓"; !ok → "registry sync failed: <error>"). Independent of
  // Save — promoting never saves and saving never promotes.
  const [promoting, setPromoting] = useState(false)
  const [promoteResult, setPromoteResult] = useState<PromoteResult | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)
  // M5 — UI-only provider FILTER for the model dropdown (catalog lanes): pick a configured
  // provider to narrow the model list. "" = all providers (the grouped view). Reset on a
  // harness/agent switch (the provider set changes with the harness).
  const [providerFilter, setProviderFilter] = useState('')

  // A project/agent switch CLEARS the loaded config synchronously DURING render (the
  // "adjust state during render" idiom the rest of the app uses) — no setState-in-effect
  // cascade — so the stale agent's config never flashes and the effect below refetches.
  const scopeKey = `${project ?? ''}/${agent ?? ''}`
  const [loadedScope, setLoadedScope] = useState<string | null>(null)
  if (scopeKey !== loadedScope) {
    setLoadedScope(scopeKey)
    setCatalog(null)
    setCv(null)
    setForm(null)
    setLoadError(null)
    setSaved(false)
    setSaveError(null)
    setCustomModel('')
    setCustomModelError(null)
    setAddingCustomModel(false)
    setPromoteResult(null)
    setProviderFilter('')
  }

  const fetchView = useCallback(
    async (signal?: AbortSignal) => {
      if (!project || !agent) return
      const view = await client.agentConfigView(project, agent, signal)
      setCv(view)
      setForm(seedForm(view))
    },
    [client, project, agent],
  )

  // Load the catalog + the selected agent's config-view (refetched on a project/agent
  // switch via the dep array). Every setState here is inside an async callback (a
  // then/catch), never synchronously in the effect body.
  useEffect(() => {
    if (!project || !agent) return
    const ctrl = new AbortController()
    let alive = true
    Promise.all([
      client.configCatalog(project, ctrl.signal),
      client.agentConfigView(project, agent, ctrl.signal),
    ])
      .then(([cat, view]) => {
        if (!alive) return
        setCatalog(cat)
        setCv(view)
        setForm(seedForm(view))
      })
      .catch((e: unknown) => {
        if (!alive) return
        if (e instanceof DOMException && e.name === 'AbortError') return
        setLoadError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
      ctrl.abort()
    }
  }, [project, agent, client])

  if (!project || !agent) return null

  if (loadError && !catalog) {
    return (
      <div className="border-b border-glass-line px-5 py-3" data-agent-config-editor>
        <p className="text-[11px] leading-relaxed text-run-errored/80">
          Couldn’t load the config catalog for <span className="font-mono">{agent}</span>. The
          backend <code className="font-mono">/agents/{'{project}'}/config-catalog</code> route may
          not be live in this build.
        </p>
      </div>
    )
  }

  if (!catalog || !cv || !form) {
    return (
      <div className="border-b border-glass-line px-5 py-3" data-agent-config-editor>
        <p className="text-[11px] text-ink-500">Loading config…</p>
      </div>
    )
  }

  const f: CardForm = form
  // Deterministic agents carry NO LLM — hide harness/model/reasoning (mirrors
  // app.domain.designation.is_ai_worker). The Role preset stays editable so the tier
  // can be switched. Reads the LIVE form value so toggling the dropdown updates at once.
  const isDeterministic = f.designation === 'deterministic'
  const harnessModels = catalog.models_by_harness[f.harness] ?? []
  // Every dynamic harness shows the SELECTED MODEL's discovered effort ladder.
  // A model that does not reason has no options; an undiscovered/fallback model
  // uses the harness-wide outage fallback.
  const harnessSpec = catalog.harnesses.find((h) => h.value === f.harness)
  const isPerModelReasoningLane = ['catalog', 'claude-catalog', 'codex-catalog', 'pi-catalog'].includes(
    harnessSpec?.model_source ?? '',
  )
  const canAddCustomModel = f.harness === 'claude-code'
  const customModelsByHarness = catalog.custom_models_by_harness ?? {}
  const modelReasoning = isPerModelReasoningLane
    ? reasoningForModel(catalog, f.harness, f.model)
    : null
  const harnessReasoning = modelReasoning ?? (catalog.reasoning_by_harness[f.harness] ?? [])
  // Hide the reasoning control entirely when the kaidera catalog lane's selected
  // model is a known non-reasoning model (an empty per-model option set).
  const reasoningHidden =
    isPerModelReasoningLane && modelReasoning !== null && modelReasoning.length === 0
  // M5 — the configured providers THIS harness's models come from (catalog lanes carry a
  // per-model provider; fixed lanes like claude-code don't). The filter narrows the model
  // dropdown to one provider; the models are already configured-only (M1 picker filter).
  const modelProviders = Array.from(
    new Set(harnessModels.map((m) => m.provider).filter((p): p is string => Boolean(p))),
  )
  const showProviderFilter = modelProviders.length > 0
  const filteredModels =
    providerFilter && showProviderFilter
      ? harnessModels.filter((m) => m.provider === providerFilter)
      : harnessModels
  const autoDispatchOverride = f.auto_dispatch !== ''
  const autoDispatchOn = autoDispatchOverride
    ? f.auto_dispatch === 'true'
    : Boolean(cv.auto_dispatch)
  const rolePreset = rolePresetFor(f)
  const activePreset = ROLE_PRESETS.find((preset) => preset.value === rolePreset)

  function setHarness(next: string) {
    setSaved(false)
    setCustomModel('')
    setCustomModelError(null)
    setProviderFilter('')  // the provider set changes with the harness — reset the filter
    setForm((prev) => {
      const cur = prev ?? f
      const models = catalog!.models_by_harness[next] ?? []
      const model = models.some((m) => m.value === cur.model) ? cur.model : models[0]?.value ?? ''
      // B3: reasoning options follow the (effective) model on a catalog lane, else
      // the per-harness list. Re-validate the current reasoning against them.
      const reasoning = reasoningOptionsFor(next, model)
      const reason = reasoning.some((r) => r.value === cur.reasoning)
        ? cur.reasoning
        : reasoning[0]?.value ?? ''
      return { ...cur, harness: next, model, reasoning: reason }
    })
  }

  // B3: the effective reasoning options for a (harness, model) — per-model on the
  // kaidera catalog lane, else the fixed per-harness list. Used to re-validate the
  // selected reasoning level whenever the harness OR model changes.
  function reasoningOptionsFor(harness: string, model: string): ReasoningOption[] {
    const spec = catalog!.harnesses.find((h) => h.value === harness)
    if (['catalog', 'claude-catalog', 'codex-catalog', 'pi-catalog'].includes(spec?.model_source ?? '')) {
      const perModel = reasoningForModel(catalog!, harness, model)
      if (perModel !== null) return perModel
    }
    return catalog!.reasoning_by_harness[harness] ?? []
  }

  function setField<K extends keyof CardForm>(key: K, value: CardForm[K]) {
    setSaved(false)
    setForm((prev) => {
      const cur = prev ?? f
      const nextForm = { ...cur, [key]: value }
      // B3: changing the MODEL re-derives the reasoning options (the new model may
      // have a different ladder, or none). Keep the current level if still valid,
      // else snap to the new model's first level (or "" when it doesn't reason).
      if (key === 'model') {
        const reasoning = reasoningOptionsFor(nextForm.harness, nextForm.model)
        if (!reasoning.some((r) => r.value === nextForm.reasoning)) {
          nextForm.reasoning = reasoning[0]?.value ?? ''
        }
      }
      if (key === 'designation' && value === 'deterministic') {
        nextForm.auto_dispatch = 'false'
      }
      return nextForm
    })
  }

  function applyRolePreset(value: string) {
    setSaved(false)
    const preset = ROLE_PRESETS.find((p) => p.value === value)
    if (!preset || !('designation' in preset)) return
    setForm((prev) => {
      const cur = prev ?? f
      return {
        ...cur,
        role: preset.role || cur.role,
        designation: preset.designation,
        auto_dispatch: preset.auto_dispatch,
      }
    })
  }

  function setProvider(next: string) {
    setSaved(false)
    setProviderFilter(next)
    // if the current model isn't in the newly-filtered set, jump to that provider's first model
    const filtered = next ? harnessModels.filter((m) => m.provider === next) : harnessModels
    if (!filtered.some((m) => m.value === f.model)) {
      setField('model', filtered[0]?.value ?? '')
    }
  }

  function addCustomHarnessModel() {
    if (!project || !catalog) return
    const value = customModel.trim()
    setSaved(false)
    setCustomModelError(null)

    if (!canAddCustomModel) {
      setCustomModelError('Custom model entries are only enabled for Claude Code.')
      return
    }
    if (!client.setAppSettings) {
      setCustomModelError('This build cannot persist harness model catalog changes.')
      return
    }
    if (!value) {
      setCustomModelError('Enter a Claude Code model alias or full model id.')
      return
    }
    if (value.length > 200 || /[\r\n]/.test(value)) {
      setCustomModelError('Model id must be a single line under 200 characters.')
      return
    }

    if (harnessModels.some((m) => m.value === value)) {
      setField('model', value)
      setCustomModel('')
      return
    }

    const currentForHarness = customModelsByHarness[f.harness] ?? []
    const nextForHarness = [...currentForHarness, { value, label: value }]
    const nextCustom = {
      ...customModelsByHarness,
      [f.harness]: nextForHarness,
    }

    setAddingCustomModel(true)
    client
      .setAppSettings(project, { [HARNESS_MODEL_OVERRIDES_KEY]: nextCustom })
      .then((res) => {
        if (res?.ok === false) {
          throw new Error('settings store unavailable')
        }
        setCatalog((prev) => {
          if (!prev) return prev
          const mergedModels = [...(prev.models_by_harness[f.harness] ?? []), { value, label: value }]
          return {
            ...prev,
            models_by_harness: {
              ...prev.models_by_harness,
              [f.harness]: mergedModels,
            },
            custom_models_by_harness: {
              ...(prev.custom_models_by_harness ?? {}),
              [f.harness]: nextForHarness,
            },
          }
        })
        setForm((prev) => (prev ? { ...prev, model: value } : prev))
        setCustomModel('')
      })
      .catch((e: unknown) => {
        setCustomModelError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setAddingCustomModel(false))
  }

  function submit() {
    if (!project || !agent || !cv) return
    setSaving(true)
    setSaved(false)
    setSaveError(null)
    // CONSOLE-LOCAL save (feature-gap #81): persists the override only — it does NOT
    // promote to the registry. Promotion is the separate explicit button below.
    //
    // PERSIST ONLY EXPLICIT SELECTIONS (user rule: "a selected model is saved, not
    // overridden by the default — the default is only the out-of-the-box redist
    // setting"). For harness/model/reasoning: a value that merely MIRRORS the effective
    // registry/default (NOT an existing override, and unchanged this session) is sent as
    // "" (cleared) so the agent keeps FOLLOWING the default dynamically instead of being
    // PINNED to it. An existing override (cv.ov_*) or a value the user CHANGED is sent
    // as-is. This stops a designation/role edit from silently baking the default model.
    const deterministicOut = f.designation === 'deterministic'
    const harnessOut = deterministicOut
      ? ''
      : cv.ov_harness
        ? f.harness
        : f.harness !== ((cv.harness as string) || '')
          ? f.harness
          : ''
    const modelOut = deterministicOut
      ? ''
      : cv.ov_model
        ? f.model
        : f.model !== ((cv.model as string) || '')
          ? f.model
          : ''
    const reasoningOut = deterministicOut
      ? ''
      : cv.ov_reasoning
        ? f.reasoning
        : f.reasoning !== ((cv.reasoning as string) || '')
          ? f.reasoning
          : ''
    const autoDispatchOut = deterministicOut ? 'false' : f.auto_dispatch
    client
      .setAgentConfig(project, agent, {
        harness: harnessOut,
        model: modelOut,
        reasoning: reasoningOut,
        designation: f.designation,
        role: f.role,
        auto_dispatch: autoDispatchOut,
      })
      .then(async (res) => {
        if (res?.ok === false) {
          throw new Error(res.error || 'settings store unavailable')
        }
        // refetch-on-success: land on the authoritative post-save effective config…
        await fetchView().catch(() => {})
        // …and ask the shell to refresh (regroups the agents column on a designation change).
        onSaved()
        setSaved(true)
      })
      .catch((e: unknown) => {
        setSaveError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setSaving(false))
  }

  function promote() {
    if (!project || !agent) return
    setPromoting(true)
    setPromoteResult(null)
    client
      .promoteAgent(project, agent)
      .then((res) => {
        setPromoteResult(res ?? { ok: false, error: 'no response from the promote call.' })
      })
      .catch((e: unknown) => {
        setPromoteResult({ ok: false, error: e instanceof Error ? e.message : String(e) })
      })
      .finally(() => setPromoting(false))
  }

  const designationLabel =
    (cv.designation as string) === 'interactive'
      ? 'Interactive · Lead'
      : (cv.designation as string) === 'deterministic'
        ? 'Deterministic'
        : 'Non-interactive · AI worker'

  return (
    <div className="border-b border-glass-line px-5 py-3" data-agent-config-editor>
      <GlassCard className="space-y-3 p-4">
        <header className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
            Configuration
          </span>
          <span
            className={cx(
              'rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide',
              (cv.designation as string) === 'interactive'
                ? 'bg-mint-500/15 text-mint-300'
                : 'bg-base-700/60 text-ink-400',
            )}
          >
            {designationLabel}
          </span>
          {cv.has_override ? (
            <span
              title="This agent has a console-local override layered over its registry value"
              className="rounded bg-mint-500/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-mint-300"
            >
              override
            </span>
          ) : null}
          <span className="w-full text-[10px] leading-relaxed text-ink-600 sm:ml-auto sm:w-auto sm:text-right">
            role preset · role · model · auto-run eligibility
          </span>
        </header>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {isDeterministic ? (
            <div className="rounded-lg border border-glass-line bg-base-800/40 px-3 py-2 text-[11px] leading-relaxed text-ink-500 sm:col-span-2">
              Deterministic agent — no LLM/model is attached. It runs its packaged code on a
              schedule or trigger. Switch the role preset below to make it an AI worker.
            </div>
          ) : (
            <>
          <div className="space-y-1">
            <label htmlFor={`cfg-harness-${agent}`} className={LABEL_CLASS}>
              Harness {cv.ov_harness ? <OverrideDot /> : null}
            </label>
            <select
              id={`cfg-harness-${agent}`}
              className={FIELD_CLASS}
              value={form.harness}
              disabled={saving}
              onChange={(e) => setHarness(e.target.value)}
            >
              {catalog.harnesses.map((h) => (
                <option key={h.value} value={h.value}>
                  {h.label}
                </option>
              ))}
              {form.harness && !catalog.harnesses.some((h) => h.value === form.harness) && (
                <option value={form.harness}>{form.harness}</option>
              )}
            </select>
            <RegistryHint value={(cv.reg_harness_label as string) || (cv.reg_harness as string)} />
          </div>

          <div className="space-y-1">
            <label htmlFor={`cfg-model-${agent}`} className={LABEL_CLASS}>
              Model {cv.ov_model ? <OverrideDot /> : null}
            </label>
            {showProviderFilter && (
              <select
                aria-label="Filter models by provider"
                data-testid="model-provider-filter"
                className={cx(FIELD_CLASS, 'mb-1')}
                value={providerFilter}
                disabled={saving}
                onChange={(e) => setProvider(e.target.value)}
              >
                <option value="">All providers</option>
                {modelProviders.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            )}
            <select
              id={`cfg-model-${agent}`}
              className={FIELD_CLASS}
              value={form.model}
              disabled={saving}
              onChange={(e) => setField('model', e.target.value)}
            >
              <ModelOptions models={filteredModels} current={form.model} />
            </select>
            {canAddCustomModel ? (
              <div className="mt-2 rounded-lg border border-glass-line bg-base-900/30 p-2">
                <label htmlFor={`cfg-custom-model-${agent}`} className={LABEL_CLASS}>
                  Add Claude model
                </label>
                <div className="mt-1 flex gap-2">
                  <input
                    id={`cfg-custom-model-${agent}`}
                    className={FIELD_CLASS}
                    value={customModel}
                    disabled={saving || addingCustomModel}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="fable or claude-fable-5"
                    onChange={(e) => {
                      setCustomModel(e.target.value)
                      setCustomModelError(null)
                    }}
                  />
                  <button
                    type="button"
                    className={PROMOTE_BTN_CLASS}
                    disabled={saving || addingCustomModel || !customModel.trim()}
                    onClick={addCustomHarnessModel}
                  >
                    {addingCustomModel ? 'Adding…' : 'Add & select'}
                  </button>
                </div>
                <p className="mt-1 text-[10px] leading-relaxed text-ink-600">
                  Use this when Claude Code exposes a new alias or full model id before
                  Kaidera OS ships a new default catalog.
                </p>
                {customModelError ? (
                  <p className="mt-1 text-[10px] text-run-errored">{customModelError}</p>
                ) : null}
              </div>
            ) : null}
            <RegistryHint value={cv.reg_model as string | null} />
            {/* VALIDITY (feature #99): the stored model was impossible for the harness
                — the backend coerced it to the harness default; surface a subtle hint so
                the operator sees why the shown model differs from what was stored. */}
            {cv.model_coerced ? (
              <span
                data-testid="model-coerced-hint"
                className="block text-[10px] text-run-queued"
                title="The stored model couldn’t run on this harness, so it was reset to the harness default. Pick a model and Save to make it permanent."
              >
                model{' '}
                <code className="font-mono">{(cv.model_invalid_original as string) || '—'}</code>{' '}
                was invalid for this harness — using its default
              </span>
            ) : null}
          </div>

          <div className="space-y-1">
            <label htmlFor={`cfg-reasoning-${agent}`} className={LABEL_CLASS}>
              Reasoning {cv.ov_reasoning ? <OverrideDot /> : null}
            </label>
            {reasoningHidden ? (
              // B3: the selected kaidera model is a known NON-reasoning model — no
              // reasoning to configure. Show a quiet note instead of an empty <select>.
              <p
                data-testid="reasoning-not-supported"
                className="text-[11px] text-ink-600"
              >
                This model doesn’t support reasoning.
              </p>
            ) : (
              <select
                id={`cfg-reasoning-${agent}`}
                className={FIELD_CLASS}
                value={form.reasoning}
                disabled={saving}
                onChange={(e) => setField('reasoning', e.target.value)}
              >
                {harnessReasoning.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
                {form.reasoning && !harnessReasoning.some((r) => r.value === form.reasoning) && (
                  <option value={form.reasoning}>{form.reasoning}</option>
                )}
              </select>
            )}
            <RegistryHint value={cv.reg_reasoning as string | null} />
          </div>
            </>
          )}

          <div className="space-y-1">
            <label htmlFor={`cfg-role-preset-${agent}`} className={LABEL_CLASS}>
              Role preset
            </label>
            <select
              id={`cfg-role-preset-${agent}`}
              className={FIELD_CLASS}
              value={rolePreset}
              disabled={saving}
              onChange={(e) => applyRolePreset(e.target.value)}
            >
              {ROLE_PRESETS.map((preset) => (
                <option key={preset.value || 'custom'} value={preset.value}>
                  {preset.label}
                </option>
              ))}
            </select>
            <span className="block text-[10px] text-ink-600">
              {activePreset?.help ?? 'Choose how this worker should behave.'}
            </span>
            <RegistryHint
              value={cv.reg_designation as string | null}
              suffix="stored worker type"
            />
          </div>
        </div>

        <div className="space-y-1">
          <label htmlFor={`cfg-role-${agent}`} className={LABEL_CLASS}>
            Role {cv.ov_role ? <OverrideDot /> : null}
          </label>
          <input
            id={`cfg-role-${agent}`}
            className={FIELD_CLASS}
            placeholder={(cv.reg_role as string) || 'registry role'}
            value={form.role}
            disabled={saving}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setField('role', e.target.value)}
          />
          <RegistryHint value={cv.reg_role as string | null} suffix="blank uses the registry role" />
        </div>

        <div className="rounded-lg border border-glass-line bg-base-900/35 px-3 py-2">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <div className={LABEL_CLASS}>
                Allow auto-run when assigned {cv.ov_auto_dispatch ? <OverrideDot /> : null}
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-ink-500">
                When the global engine and project dispatch are on, handoffs assigned to this
                worker can start automatically. Project dispatch is controlled from the Dashboard.
              </p>
              <span className="mt-1 block text-[10px] text-ink-600">
                effective: <code className="font-mono text-ink-500">{autoDispatchOn ? 'enabled' : 'disabled'}</code>
                {autoDispatchOverride ? ' · console-local override' : ' · registry default'}
              </span>
            </div>
            <div className="flex shrink-0 flex-col items-end gap-1">
              <button
                type="button"
                role="switch"
                aria-label="Allow auto-run when assigned"
                aria-checked={autoDispatchOn}
                disabled={saving || isDeterministic}
                onClick={() => setField('auto_dispatch', autoDispatchOn ? 'false' : 'true')}
                className={cx(
                  'relative inline-flex h-5 w-9 items-center rounded-full transition-colors',
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
              {autoDispatchOverride && !isDeterministic && (
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => setField('auto_dispatch', '')}
                  className="text-[10px] font-medium text-ink-500 hover:text-ink-200 disabled:opacity-40"
                >
                  use default
                </button>
              )}
            </div>
          </div>
          {isDeterministic && (
            <p className="mt-2 text-[10px] text-ink-600">
              Deterministic orchestrators coordinate dispatch; automatic handoff execution is disabled.
            </p>
          )}
        </div>

        {saveError && (
          <p className="rounded-md bg-run-errored/12 px-3 py-2 text-[11px] leading-relaxed text-run-errored/90">
            Couldn’t save — {saveError}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={BTN_CLASS} disabled={saving} onClick={submit}>
            {saving ? 'Saving…' : 'Save config'}
          </button>
          {saved && <span className="text-[11px] font-medium text-mint-300">Saved ✓</span>}

          {/* EXPLICIT "Promote to registry" (feature-gap #81): a glass-styled button,
              distinct from the mint Save — it commits the agent's current effective config
              INTO the Cortex registry on demand (Save stays console-local). */}
          <button
            type="button"
            onClick={promote}
            disabled={promoting}
            title="Push this agent's current effective config into the Cortex registry (the source of truth). Save stays console-local; this is the deliberate commit."
            className={PROMOTE_BTN_CLASS}
          >
            {promoting ? 'Promoting…' : 'Promote to registry'}
          </button>
          {promoteResult &&
            (promoteResult.ok ? (
              <span
                data-testid="promote-result"
                title="This agent's config was pushed into the Cortex registry."
                className="text-[11px] font-medium text-mint-300"
              >
                promoted ✓
              </span>
            ) : (
              <span
                data-testid="promote-result"
                title="The registry promote didn't land — the console-local config still applies."
                className="text-[11px] font-medium text-run-queued"
              >
                registry sync failed{promoteResult.error ? `: ${promoteResult.error}` : ''}
              </span>
            ))}

          {/* Deregister (remove) the agent — opens a confirm modal (feature-gap #81). */}
          {registrationClient && project && agent && (
            <button
              type="button"
              onClick={() => setRemoveOpen(true)}
              disabled={saving || promoting}
              title="Remove this agent from the project roster (history preserved)"
              className="ml-auto rounded-md px-2.5 py-1.5 text-[11px] font-medium text-run-errored/80 transition-colors hover:bg-run-errored/12 hover:text-run-errored disabled:opacity-40"
            >
              Deregister
            </button>
          )}
        </div>
      </GlassCard>

      {/* The deregister confirm modal (feature-gap #81). */}
      {registrationClient && project && agent && (
        <DeregisterAgentModal
          open={removeOpen}
          onClose={() => setRemoveOpen(false)}
          project={project}
          agent={agent}
          client={registrationClient}
          onDone={() => onRemoved?.()}
        />
      )}
    </div>
  )
}
