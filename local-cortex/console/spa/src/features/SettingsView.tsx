import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { GlassCard, GlassPanel, StatusDot } from '../components/glass'
import { cx } from '../components/ui'
import type {
  AppSettings,
  AppSettingsWriteResult,
  CortexEmbeddingBackfillRequest,
  CortexEmbeddingBackfillResult,
  CortexEmbeddingBacklogResult,
  CortexConfigResult,
  CortexPlatformConfig,
  KeyTestResult,
  Project,
  ProjectPackExtensionResult,
  ProjectPackListResult,
  ProviderConfigRow,
  ProvidersCatalog,
  ProvidersConfig,
  RunStateRestartStatus,
  SystemField,
  SystemSchema,
  WorkspaceResult,
} from '../api'

export interface SettingsWriteClient {
  setAppSetting: (project: string, key: string, value: unknown) => Promise<AppSettingsWriteResult>
  setAppSettings?: (
    project: string,
    settings: Record<string, unknown>,
  ) => Promise<AppSettingsWriteResult>
  providerKeyTest: (
    project: string,
    body: { provider: string; key?: string; use_stored?: boolean },
  ) => Promise<KeyTestResult>
  setWorkspace: (
    project: string,
    body: { repo_root: string; project_key?: string },
  ) => Promise<WorkspaceResult>
  listProjectPacks?: (repoRoot: string, signal?: AbortSignal) => Promise<ProjectPackListResult>
  setProjectPackExtension?: (
    body: { repo_root: string; pack_key: string; module: string; enabled: boolean },
    signal?: AbortSignal,
  ) => Promise<ProjectPackExtensionResult>
  cortexConfig?: (signal?: AbortSignal) => Promise<CortexConfigResult>
  setCortexConfig?: (
    config: Partial<CortexPlatformConfig>,
    signal?: AbortSignal,
  ) => Promise<CortexConfigResult>
  cortexEmbeddingBacklog?: (
    project: string,
    signal?: AbortSignal,
  ) => Promise<CortexEmbeddingBacklogResult>
  cortexEmbeddingBackfill?: (
    project: string,
    request: CortexEmbeddingBackfillRequest,
    signal?: AbortSignal,
  ) => Promise<CortexEmbeddingBackfillResult>
  runstateRestartStatus?: (
    project: string,
    signal?: AbortSignal,
  ) => Promise<RunStateRestartStatus>
}

interface SettingsViewProps {
  project: string | null
  appSettings: AppSettings | null
  systemSchema: SystemSchema | null
  providers: ProvidersCatalog | null
  providersConfig: ProvidersConfig | null
  projectRow: Project | null
  projects?: Project[]
  loading: boolean
  error: Error | null
  client: SettingsWriteClient
  onSaved: () => void
}

type SettingsTab = 'system' | 'providers' | 'workspace' | 'extensions' | 'cortex' | 'license'

const TABS: Array<{ id: SettingsTab; label: string }> = [
  { id: 'system', label: 'System' },
  { id: 'providers', label: 'Providers' },
  { id: 'workspace', label: 'Workspace' },
  { id: 'extensions', label: 'Extensions' },
  { id: 'cortex', label: 'Cortex' },
  { id: 'license', label: 'Open-source license' },
]

const FIELD =
  'w-full rounded-md border border-glass-line bg-base-950/55 px-3 py-2 text-sm text-ink-100 outline-none transition-colors placeholder:text-ink-600 focus:border-mint-400/60 disabled:cursor-not-allowed disabled:opacity-55'
const BUTTON =
  'rounded-md border border-mint-400/35 bg-mint-500/12 px-3 py-2 text-xs font-semibold text-mint-200 transition-colors hover:bg-mint-500/20 disabled:cursor-not-allowed disabled:opacity-45'
const GHOST =
  'rounded-md border border-glass-line bg-base-900/35 px-3 py-2 text-xs font-medium text-ink-300 transition-colors hover:bg-base-800/60 disabled:cursor-not-allowed disabled:opacity-45'
const LABEL = 'block text-[11px] font-semibold uppercase tracking-wide text-ink-400'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function valuesEqual(left: unknown, right: unknown): boolean {
  if (typeof left === 'number' || typeof right === 'number') {
    return Number(left) === Number(right)
  }
  return left === right
}

function fieldSeed(field: SystemField): unknown {
  if (field.type === 'secret') return ''
  if (field.type === 'bool') return Boolean(field.value)
  if (field.type === 'number') return Number(field.value ?? 0)
  return String(field.value ?? '')
}

function fieldOptions(field: SystemField, projects?: Project[]): string[] {
  const options = [...(field.options ?? [])]
  if (field.options_source === 'projects') {
    options.push(...(projects ?? []).map((row) => row.project_key))
  }
  return [...new Set(options.filter(Boolean))]
}

function Notice({
  tone = 'neutral',
  children,
}: {
  tone?: 'neutral' | 'good' | 'bad'
  children: ReactNode
}) {
  return (
    <p
      className={cx(
        'rounded-md border px-3 py-2 text-xs leading-relaxed',
        tone === 'good' && 'border-mint-400/25 bg-mint-500/8 text-mint-300',
        tone === 'bad' && 'border-run-errored/25 bg-run-errored/8 text-run-errored/90',
        tone === 'neutral' && 'border-glass-line bg-base-900/30 text-ink-400',
      )}
    >
      {children}
    </p>
  )
}

function SectionTitle({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="text-sm font-semibold text-ink-100">{title}</h2>
      {detail && <span className="text-[10px] text-ink-500">{detail}</span>}
    </div>
  )
}

function SystemFieldControl({
  field,
  inputId,
  value,
  options,
  disabled,
  onChange,
}: {
  field: SystemField
  inputId: string
  value: unknown
  options: string[]
  disabled: boolean
  onChange: (value: unknown) => void
}) {
  if (field.type === 'readonly') {
    return <code className="block break-all text-xs text-ink-300">{String(value || 'not set')}</code>
  }

  if (field.type === 'bool') {
    const enabled = value === true
    return (
      <label className="inline-flex min-h-9 items-center gap-2 text-xs text-ink-300">
        <input
          id={inputId}
          type="checkbox"
          checked={enabled}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        {enabled ? 'Enabled' : 'Disabled'}
      </label>
    )
  }

  if (field.type === 'select' && options.length > 0) {
    return (
      <select
        id={inputId}
        className={FIELD}
        value={String(value ?? '')}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Default</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    )
  }

  return (
    <input
      id={inputId}
      className={FIELD}
      type={field.type === 'secret' ? 'password' : field.type === 'number' ? 'number' : 'text'}
      value={String(value ?? '')}
      placeholder={
        field.type === 'secret' && field.is_set ? 'Stored securely; enter a replacement' : field.placeholder
      }
      disabled={disabled}
      autoComplete={field.type === 'secret' ? 'new-password' : 'off'}
      spellCheck={false}
      onChange={(event) =>
        onChange(field.type === 'number' ? Number(event.target.value) : event.target.value)
      }
    />
  )
}

function SystemTab({
  project,
  schema,
  appSettings,
  projects,
  client,
  onSaved,
}: {
  project: string
  schema: SystemSchema | null
  appSettings: AppSettings | null
  projects?: Project[]
  client: SettingsWriteClient
  onSaved: () => void
}) {
  const initialValues = () => {
    const next: Record<string, unknown> = {}
    for (const group of schema?.groups ?? []) {
      for (const field of group.fields) next[field.key] = fieldSeed(field)
    }
    return next
  }
  const [draft, setDraft] = useState<Record<string, unknown>>(initialValues)
  const [baseline] = useState<Record<string, unknown>>(initialValues)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fields = useMemo(
    () => (schema?.groups ?? []).flatMap((group) => group.fields),
    [schema],
  )
  const changes = useMemo(() => {
    const next: Record<string, unknown> = {}
    for (const field of fields) {
      const value = draft[field.key]
      if (field.type === 'secret') {
        if (String(value ?? '').trim()) next[field.key] = String(value).trim()
      } else if (!valuesEqual(value, baseline[field.key])) {
        next[field.key] = value
      }
    }
    return next
  }, [baseline, draft, fields])
  const changedKeys = Object.keys(changes)

  async function save() {
    if (changedKeys.length === 0) return
    setSaving(true)
    setError(null)
    try {
      if (client.setAppSettings) {
        await client.setAppSettings(project, changes)
      } else {
        for (const key of changedKeys) await client.setAppSetting(project, key, changes[key])
      }
      setSaved(true)
      onSaved()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setSaving(false)
    }
  }

  if (!schema && !appSettings) {
    return <p className="text-xs text-ink-500">Loading system settings...</p>
  }

  return (
    <div className="space-y-4">
      {(schema?.groups ?? []).map((group) => (
        <GlassCard key={group.key} className="overflow-hidden p-0" data-system-group={group.key}>
          <div className="border-b border-glass-line px-4 py-3">
            <h3 className="text-xs font-semibold text-ink-200">{group.label}</h3>
          </div>
          <div className="divide-y divide-glass-line">
            {group.fields.map((field) => (
              <div
                key={field.key}
                className="grid gap-2 px-4 py-3 md:grid-cols-[minmax(10rem,0.7fr)_minmax(14rem,1.3fr)] md:items-center"
              >
                <div>
                  <label htmlFor={'setting-' + field.key} className={LABEL}>
                    {field.label}
                  </label>
                  {field.help && <p className="mt-1 text-[10px] leading-relaxed text-ink-600">{field.help}</p>}
                </div>
                <div>
                  <SystemFieldControl
                    field={field}
                    inputId={'setting-' + field.key}
                    value={draft[field.key]}
                    options={fieldOptions(field, projects)}
                    disabled={saving}
                    onChange={(value) => {
                      setSaved(false)
                      setDraft((current) => ({ ...current, [field.key]: value }))
                    }}
                  />
                </div>
              </div>
            ))}
          </div>
        </GlassCard>
      ))}

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={BUTTON} disabled={saving || changedKeys.length === 0} onClick={save}>
          {saving ? 'Saving...' : 'Save settings'}
        </button>
        <button
          type="button"
          className={GHOST}
          disabled={saving || changedKeys.length === 0}
          onClick={() => {
            setDraft(baseline)
            setError(null)
            setSaved(false)
          }}
        >
          Discard changes
        </button>
        {saved && <span className="text-xs text-mint-300">Saved</span>}
      </div>
      {error && <Notice tone="bad">{error}</Notice>}
      {schema && schema.groups.length === 0 && (
        <Notice>The settings schema is empty{schema.store_connected === false ? ' because the store is offline' : ''}.</Notice>
      )}
    </div>
  )
}

function formatPrice(value: number | null): string {
  return value === null ? '-' : '$' + value.toFixed(2)
}

function formatContext(value: number | null): string {
  return value === null ? '-' : value.toLocaleString()
}

function ManifoldModels({ catalog }: { catalog: ProvidersCatalog | null }) {
  const groups = (catalog?.providers ?? []).filter(
    (group) => group.name.toLowerCase() === 'kaidera-manifold',
  )
  const models = groups.flatMap((group) => group.models)

  return (
    <section className="space-y-3">
      <SectionTitle
        title="Available models"
        detail={catalog ? String(models.length) + ' discovered' : 'Loading...'}
      />
      {!catalog ? (
        <p className="text-xs text-ink-500">Loading the Manifold catalog...</p>
      ) : models.length === 0 ? (
        <Notice>No models are available. Save and test the Manifold connection, then refresh.</Notice>
      ) : (
        <div className="overflow-x-auto rounded-md border border-glass-line">
          <table className="min-w-[720px] w-full border-collapse text-left text-xs">
            <thead className="bg-base-900/60 text-[10px] uppercase text-ink-500">
              <tr>
                <th className="px-3 py-2 font-semibold">Model</th>
                <th className="px-3 py-2 font-semibold">Type</th>
                <th className="px-3 py-2 font-semibold">Effort levels</th>
                <th className="px-3 py-2 text-right font-semibold">Input / 1M</th>
                <th className="px-3 py-2 text-right font-semibold">Output / 1M</th>
                <th className="px-3 py-2 text-right font-semibold">Context</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-glass-line">
              {models.map((model) => (
                <tr key={model.model} className="bg-base-950/20">
                  <td className="max-w-[18rem] break-all px-3 py-2 font-mono text-ink-200">{model.model}</td>
                  <td className="px-3 py-2 text-ink-400">{model.type}</td>
                  <td className="px-3 py-2 text-ink-300">
                    {model.reasoning_tiers.length > 0 ? model.reasoning_tiers.join(', ') : 'Provider default'}
                  </td>
                  <td className="px-3 py-2 text-right text-ink-400">{formatPrice(model.input_price_per_mtok)}</td>
                  <td className="px-3 py-2 text-right text-ink-400">{formatPrice(model.output_price_per_mtok)}</td>
                  <td className="px-3 py-2 text-right text-ink-400">{formatContext(model.context_window)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function ProvidersTab({
  project,
  catalog,
  config,
  client,
  onSaved,
}: {
  project: string
  catalog: ProvidersCatalog | null
  config: ProvidersConfig | null
  client: SettingsWriteClient
  onSaved: () => void
}) {
  const row: ProviderConfigRow | null =
    (config?.providers ?? []).find((item) => item.name === 'kaidera-manifold') ?? null
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState(row?.base_url ?? 'https://api.kaidera.ai/v1')
  const [projectId, setProjectId] = useState(row?.project_id ?? '')
  const [busy, setBusy] = useState<'save' | 'test' | null>(null)
  const [result, setResult] = useState<KeyTestResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const dirty =
    Boolean(apiKey.trim()) ||
    baseUrl.trim() !== (row?.base_url ?? 'https://api.kaidera.ai/v1') ||
    projectId.trim() !== (row?.project_id ?? '')

  const saveConfig = useCallback(async (): Promise<boolean> => {
    const changes: Record<string, unknown> = {}
    if (apiKey.trim()) changes.kaidera_manifold_api_key = apiKey.trim()
    if (baseUrl.trim() && baseUrl.trim() !== row?.base_url) {
      changes.kaidera_manifold_base_url = baseUrl.trim()
    }
    if (projectId.trim() && projectId.trim() !== row?.project_id) {
      changes.kaidera_manifold_project_id = projectId.trim()
    }
    if (Object.keys(changes).length === 0) return true

    if (client.setAppSettings) {
      await client.setAppSettings(project, changes)
    } else {
      for (const [key, value] of Object.entries(changes)) {
        await client.setAppSetting(project, key, value)
      }
    }
    setApiKey('')
    onSaved()
    return true
  }, [apiKey, baseUrl, client, onSaved, project, projectId, row?.base_url, row?.project_id])

  async function save() {
    setBusy('save')
    setError(null)
    setResult(null)
    try {
      await saveConfig()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  async function test() {
    setBusy('test')
    setError(null)
    setResult(null)
    try {
      await saveConfig()
      const next = await client.providerKeyTest(project, {
        provider: row?.provider_ref ?? 'kaidera_manifold_api_key',
        use_stored: true,
      })
      setResult(next)
      if (!next.ok) setError(next.detail)
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-6">
      <GlassCard className="space-y-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SectionTitle title="Kaidera AI Manifold" />
          <span className="inline-flex items-center gap-2 text-xs text-ink-400">
            <StatusDot status={row?.key_is_set ? 'running' : 'queued'} />
            {row?.key_is_set ? 'Configured' : 'Configuration required'}
          </span>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <label className="space-y-1.5">
            <span className={LABEL}>Inference key</span>
            <input
              className={FIELD}
              type="password"
              value={apiKey}
              placeholder={row?.key_is_set ? 'Stored securely; enter a replacement' : 'mfld-...'}
              autoComplete="new-password"
              onChange={(event) => setApiKey(event.target.value)}
            />
          </label>
          <label className="space-y-1.5">
            <span className={LABEL}>Project ID</span>
            <input
              className={FIELD}
              value={projectId}
              placeholder="Manifold project ID"
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setProjectId(event.target.value)}
            />
          </label>
          <label className="space-y-1.5 md:col-span-2">
            <span className={LABEL}>Base URL</span>
            <input
              className={FIELD}
              type="url"
              value={baseUrl}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setBaseUrl(event.target.value)}
            />
          </label>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={BUTTON} disabled={busy !== null || !dirty} onClick={save}>
            {busy === 'save' ? 'Saving...' : 'Save connection'}
          </button>
          <button
            type="button"
            className={GHOST}
            disabled={busy !== null || (!row?.key_is_set && !apiKey.trim()) || !projectId.trim()}
            onClick={test}
          >
            {busy === 'test' ? 'Testing...' : 'Test connection'}
          </button>
        </div>
        {result?.ok && <Notice tone="good">{result.detail}</Notice>}
        {error && <Notice tone="bad">{error}</Notice>}
        {config?.store_connected === false && <Notice tone="bad">The local settings store is unavailable.</Notice>}
      </GlassCard>
      <ManifoldModels catalog={catalog} />
    </div>
  )
}

function WorkspaceRow({
  row,
  selected,
  client,
}: {
  row: Project
  selected: boolean
  client: SettingsWriteClient
}) {
  const stored = String(row.repo_root ?? '')
  const [value, setValue] = useState(stored)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<WorkspaceResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    setBusy(true)
    setResult(null)
    setError(null)
    try {
      const next = await client.setWorkspace(row.project_key, { repo_root: value.trim() })
      setResult(next)
      if (!next.ok) setError(next.error || 'The workspace was not updated.')
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <GlassCard className="space-y-3 p-4" data-ws-project-row={row.project_key}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-ink-100">{row.display_name || row.project_key}</span>
        <code className="text-[10px] text-ink-500">{row.project_key}</code>
        {selected && <span className="ml-auto text-[10px] font-semibold uppercase text-mint-300">Current</span>}
      </div>
      <label className="space-y-1.5">
        <span className={LABEL}>Repo root</span>
        <input
          className={FIELD}
          value={value}
          placeholder="/absolute/path/to/project"
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setValue(event.target.value)}
        />
      </label>
      <button type="button" className={BUTTON} disabled={busy || value.trim() === stored.trim()} onClick={save}>
        {busy ? 'Saving...' : 'Save folder'}
      </button>
      {result?.ok && (
        <Notice tone="good">
          <code>{result.previous_repo_root || 'not set'}</code>
          {' to '}
          <code>{result.repo_root}</code>
        </Notice>
      )}
      {error && <Notice tone="bad">{error}</Notice>}
    </GlassCard>
  )
}

function WorkspaceTab({
  project,
  projectRow,
  projects,
  client,
}: {
  project: string
  projectRow: Project | null
  projects?: Project[]
  client: SettingsWriteClient
}) {
  const rows = useMemo(() => {
    const source = projects?.length ? projects : projectRow ? [projectRow] : []
    return source.filter(
      (row, index) => source.findIndex((candidate) => candidate.project_key === row.project_key) === index,
    )
  }, [projectRow, projects])

  if (rows.length === 0) return <Notice>No registered projects are available.</Notice>
  return (
    <div className="space-y-3">
      {rows.map((row) => (
        <WorkspaceRow
          key={row.project_key + ':' + String(row.repo_root ?? '')}
          row={row}
          selected={row.project_key === project}
          client={client}
        />
      ))}
    </div>
  )
}

function ExtensionsTab({
  projectRow,
  client,
}: {
  projectRow: Project | null
  client: SettingsWriteClient
}) {
  const repoRoot = String(projectRow?.repo_root ?? '')
  const [data, setData] = useState<ProjectPackListResult | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (!repoRoot || !client.listProjectPacks) return
      try {
        const next = await client.listProjectPacks(repoRoot, signal)
        setData(next)
        setError(next.ok ? null : next.error)
      } catch (caught) {
        if (!(caught instanceof DOMException && caught.name === 'AbortError')) setError(message(caught))
      }
    },
    [client, repoRoot],
  )

  useEffect(() => {
    const controller = new AbortController()
    queueMicrotask(() => void load(controller.signal))
    return () => controller.abort()
  }, [load])

  async function toggle(packKey: string, module: string, enabled: boolean) {
    if (!client.setProjectPackExtension) return
    setBusy(packKey + ':' + module)
    setError(null)
    try {
      const next = await client.setProjectPackExtension({
        repo_root: repoRoot,
        pack_key: packKey,
        module,
        enabled,
      })
      if (!next.ok) setError(next.error || 'The extension was not updated.')
      await load()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  if (!repoRoot) return <Notice>Set this project&apos;s repo root before managing extensions.</Notice>
  if (!client.listProjectPacks) return <Notice>Project-pack management is unavailable in this build.</Notice>

  return (
    <div className="space-y-3">
      {error && <Notice tone="bad">{error}</Notice>}
      {!data ? (
        <p className="text-xs text-ink-500">Loading project packs...</p>
      ) : data.packs.length === 0 ? (
        <Notice>No project packs are installed in this workspace.</Notice>
      ) : (
        data.packs.map((pack) => (
          <GlassCard key={pack.key} className="overflow-hidden p-0">
            <div className="flex flex-wrap items-center gap-2 border-b border-glass-line px-4 py-3">
              <h3 className="text-sm font-semibold text-ink-100">{pack.name}</h3>
              <code className="text-[10px] text-ink-500">{pack.version}</code>
              {pack.restart_required && (
                <span className="ml-auto text-[10px] font-semibold uppercase text-run-queued">Restart required</span>
              )}
            </div>
            <div className="divide-y divide-glass-line">
              {(pack.extensions ?? []).map((extension) => {
                const id = pack.key + ':' + extension.module
                return (
                  <label key={extension.module} className="flex items-center gap-3 px-4 py-3">
                    <input
                      type="checkbox"
                      checked={extension.enabled}
                      disabled={extension.required || busy === id || !client.setProjectPackExtension}
                      onChange={(event) => void toggle(pack.key, extension.module, event.target.checked)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block text-xs font-medium text-ink-200">{extension.module}</span>
                      {extension.description && (
                        <span className="block text-[10px] leading-relaxed text-ink-500">{extension.description}</span>
                      )}
                    </span>
                    <span className="text-[10px] uppercase text-ink-500">{extension.status.replaceAll('_', ' ')}</span>
                  </label>
                )
              })}
            </div>
          </GlassCard>
        ))
      )}
    </div>
  )
}

interface CortexHealth {
  status?: string
  base_url?: string
  surface_version?: string
  event_backend?: string
  embed_provider?: string
  embed_model?: string
  embed_dims?: number
  rerank_enabled?: boolean
  rerank_provider?: string
  rerank_model?: string
  rls_enforced?: boolean
  error?: string
}

function KV({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="grid gap-1 px-4 py-2.5 text-xs sm:grid-cols-[10rem_minmax(0,1fr)]">
      <span className="text-ink-500">{label}</span>
      <span className="min-w-0 break-all text-ink-200">{value}</span>
    </div>
  )
}

function CortexTab({
  project,
  projectRow,
  client,
  onSaved,
}: {
  project: string
  projectRow: Project | null
  client: SettingsWriteClient
  onSaved: () => void
}) {
  const [health, setHealth] = useState<CortexHealth | null>(null)
  const [healthDone, setHealthDone] = useState(false)
  const [config, setConfig] = useState<CortexPlatformConfig | null>(null)
  const [backlog, setBacklog] = useState<CortexEmbeddingBacklogResult | null>(null)
  const [restart, setRestart] = useState<RunStateRestartStatus | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetch('/cortex/health?project=' + encodeURIComponent(project), {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })
      .then(async (response) => (response.ok ? ((await response.json()) as CortexHealth) : null))
      .catch(() => null)
      .then((next) => {
        if (!controller.signal.aborted) {
          setHealth(next)
          setHealthDone(true)
        }
      })
    if (client.cortexConfig) {
      client
        .cortexConfig(controller.signal)
        .then((next) => {
          if (!controller.signal.aborted) {
            setConfig(next.config)
            if (!next.ok) setError(next.error)
          }
        })
        .catch((caught) => {
          if (!controller.signal.aborted) setError(message(caught))
        })
    }
    if (client.runstateRestartStatus) {
      client
        .runstateRestartStatus(project, controller.signal)
        .then((next) => {
          if (!controller.signal.aborted) setRestart(next)
        })
        .catch(() => undefined)
    }
    return () => controller.abort()
  }, [client, project])

  async function saveConfig() {
    if (!client.setCortexConfig || !config) return
    setBusy('config')
    setError(null)
    try {
      const next = await client.setCortexConfig({
        embedding_provider: 'kaidera-manifold',
        embedding_model: String(config.embedding_model ?? ''),
        embedding_dims: Number(config.embedding_dims ?? 0),
        rerank_enabled: Boolean(config.rerank_enabled),
        rerank_provider: 'kaidera-manifold',
        rerank_model: String(config.rerank_model ?? ''),
      })
      setConfig(next.config)
      if (!next.ok) setError(next.error)
      else onSaved()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  async function loadBacklog() {
    if (!client.cortexEmbeddingBacklog) return
    setBusy('backlog')
    setError(null)
    try {
      const next = await client.cortexEmbeddingBacklog(project)
      setBacklog(next)
      if (!next.ok) setError(next.error)
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  async function backfill(dryRun: boolean) {
    if (!client.cortexEmbeddingBackfill) return
    setBusy(dryRun ? 'dry-run' : 'backfill')
    setError(null)
    try {
      const next: CortexEmbeddingBackfillResult = await client.cortexEmbeddingBackfill(project, {
        table: 'all',
        limit: 100,
        chunk_size: 100,
        dry_run: dryRun,
        async_job: !dryRun,
      })
      if (!next.ok) setError(next.error)
      await loadBacklog()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  const status = String(health?.status ?? (healthDone ? 'unreachable' : 'checking')).toLowerCase()
  const healthy = ['ok', 'healthy', 'up'].includes(status)
  const backlogTotal = Object.values(backlog?.coverage ?? {}).reduce(
    (total, row) => total + Number(row.backlog || 0),
    0,
  )

  return (
    <div className="space-y-4">
      {error && <Notice tone="bad">{error}</Notice>}
      <GlassCard className="overflow-hidden p-0">
        <div className="flex items-center gap-2 border-b border-glass-line px-4 py-3">
          <SectionTitle title="Cortex connection" />
          <span className="ml-auto inline-flex items-center gap-2 text-xs text-ink-400">
            <StatusDot status={healthy ? 'running' : healthDone ? 'errored' : 'queued'} />
            {status}
          </span>
        </div>
        <div className="divide-y divide-glass-line">
          <KV label="Project" value={<code>{project}</code>} />
          <KV label="Repo root" value={<code>{String(projectRow?.repo_root ?? 'not set')}</code>} />
          <KV label="Base URL" value={<code>{health?.base_url || 'not reported'}</code>} />
          <KV label="Surface version" value={<code>{health?.surface_version || 'not reported'}</code>} />
          <KV label="Event backend" value={<code>{health?.event_backend || 'not reported'}</code>} />
        </div>
      </GlassCard>

      {config && (
        <GlassCard className="space-y-4 p-4">
          <SectionTitle title="Cortex retrieval models" detail="Routed through Manifold" />
          <div className="grid gap-3 md:grid-cols-3">
            <label className="space-y-1.5 md:col-span-2">
              <span className={LABEL}>Embedding model</span>
              <input
                className={FIELD}
                value={String(config.embedding_model ?? '')}
                onChange={(event) => setConfig({ ...config, embedding_model: event.target.value })}
              />
            </label>
            <label className="space-y-1.5">
              <span className={LABEL}>Dimensions</span>
              <input
                className={FIELD}
                type="number"
                min={1}
                value={String(config.embedding_dims ?? '')}
                onChange={(event) => setConfig({ ...config, embedding_dims: Number(event.target.value) })}
              />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className={LABEL}>Rerank model</span>
              <input
                className={FIELD}
                value={String(config.rerank_model ?? '')}
                disabled={!config.rerank_enabled}
                onChange={(event) => setConfig({ ...config, rerank_model: event.target.value })}
              />
            </label>
            <label className="flex items-end gap-2 pb-2 text-xs text-ink-300">
              <input
                type="checkbox"
                checked={Boolean(config.rerank_enabled)}
                onChange={(event) => setConfig({ ...config, rerank_enabled: event.target.checked })}
              />
              Reranking enabled
            </label>
          </div>
          <button type="button" className={BUTTON} disabled={busy !== null} onClick={saveConfig}>
            {busy === 'config' ? 'Saving...' : 'Save Cortex models'}
          </button>
        </GlassCard>
      )}

      <GlassCard className="space-y-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SectionTitle
            title="Embedding coverage"
            detail={backlog ? String(backlogTotal) + ' pending' : 'Not loaded'}
          />
          <button type="button" className={GHOST} disabled={busy !== null} onClick={loadBacklog}>
            {busy === 'backlog' ? 'Loading...' : 'Refresh'}
          </button>
        </div>
        {backlog && (
          <div className="overflow-hidden rounded-md border border-glass-line">
            {Object.entries(backlog.coverage).map(([table, row]) => (
              <div key={table} className="grid grid-cols-[1fr_auto_auto] gap-3 border-b border-glass-line px-3 py-2 text-xs last:border-0">
                <code className="text-ink-300">{table}</code>
                <span className="text-ink-500">{row.embedded}/{row.total}</span>
                <span className={row.backlog > 0 ? 'text-run-queued' : 'text-mint-300'}>{row.backlog} pending</span>
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <button type="button" className={GHOST} disabled={!backlog || busy !== null} onClick={() => void backfill(true)}>
            {busy === 'dry-run' ? 'Checking...' : 'Dry run'}
          </button>
          <button
            type="button"
            className={BUTTON}
            disabled={!backlog || backlogTotal === 0 || busy !== null}
            onClick={() => void backfill(false)}
          >
            {busy === 'backfill' ? 'Starting...' : 'Start backfill'}
          </button>
        </div>
      </GlassCard>

      {restart && (
        <GlassCard className="space-y-3 p-4">
          <SectionTitle title="Autonomy run-state" detail={restart.store} />
          <div className="grid grid-cols-2 gap-2 text-center md:grid-cols-4">
            {[
              ['Active', restart.counts.active],
              ['Restart-survivable', restart.counts.restart_survivable],
              ['Request-lived', restart.counts.request_lived],
              ['Needs reconcile', restart.counts.needs_reconcile],
            ].map(([label, value]) => (
              <div key={String(label)} className="rounded-md border border-glass-line bg-base-950/25 px-2 py-3">
                <div className="font-mono text-base text-ink-100">{value}</div>
                <div className="mt-1 text-[9px] uppercase text-ink-500">{label}</div>
              </div>
            ))}
          </div>
        </GlassCard>
      )}
    </div>
  )
}

function LicenseTab() {
  return (
    <div className="max-w-3xl space-y-4">
      <GlassCard className="space-y-4 p-4">
        <SectionTitle title="GNU AGPLv3 open-source edition" />
        <p className="text-sm leading-relaxed text-ink-300">
          Kaidera OS is provided under the GNU Affero General Public License version 3,
          without warranty or liability. See the repository license for the complete terms.
        </p>
        <p className="text-sm leading-relaxed text-ink-400">
          Commercial licensing, enterprise support, and managed deployment are available from{' '}
          <a className="text-mint-300 hover:underline" href="mailto:sales@kaidera.ai">
            sales@kaidera.ai
          </a>
          .
        </p>
      </GlassCard>
    </div>
  )
}

function SettingsViewBody({
  project,
  appSettings,
  systemSchema,
  providers,
  providersConfig,
  projectRow,
  projects,
  loading,
  error,
  client,
  onSaved,
}: SettingsViewProps) {
  const [tab, setTab] = useState<SettingsTab>('system')

  return (
    <GlassPanel className="min-w-0 flex-1">
      <header className="border-b border-glass-line px-5 py-4">
        <h1 className="text-base font-semibold text-ink-100">Settings</h1>
        <p className="mt-1 text-[11px] text-ink-500">
          {project ? (
            <>
              Project <span className="font-mono text-ink-400">{project}</span>
            </>
          ) : (
            'Global configuration'
          )}
        </p>
        <nav
          role="tablist"
          aria-label="Settings sections"
          className="mt-3 flex max-w-full flex-wrap gap-1 border-b border-glass-line pb-2"
        >
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={tab === item.id}
              onClick={() => setTab(item.id)}
              className={cx(
                'shrink-0 rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                tab === item.id
                  ? 'bg-mint-500/15 text-mint-200'
                  : 'text-ink-400 hover:bg-base-800/50 hover:text-ink-200',
              )}
            >
              {item.label}
            </button>
          ))}
        </nav>
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        {loading && !appSettings && !systemSchema && <p className="text-xs text-ink-500">Loading settings...</p>}
        {error && !appSettings && !systemSchema && <Notice tone="bad">Could not load settings: {error.message}</Notice>}

        {tab === 'system' && (
          <SystemTab
            key={JSON.stringify(
              (systemSchema?.groups ?? []).map((group) =>
                group.fields.map((field) => [field.key, field.value, field.is_set]),
              ),
            )}
            project={project ?? ''}
            schema={systemSchema}
            appSettings={appSettings}
            projects={projects}
            client={client}
            onSaved={onSaved}
          />
        )}
        {tab === 'providers' && (
          <ProvidersTab
            key={JSON.stringify(
              (providersConfig?.providers ?? []).map((row) => [
                row.name,
                row.key_is_set,
                row.base_url,
                row.project_id,
              ]),
            )}
            project={project ?? ''}
            catalog={providers}
            config={providersConfig}
            client={client}
            onSaved={onSaved}
          />
        )}
        {tab === 'workspace' &&
          (project ? (
            <WorkspaceTab
              project={project}
              projectRow={projectRow}
              projects={projects}
              client={client}
            />
          ) : (
            <Notice>Select or create a project to edit its workspace.</Notice>
          ))}
        {tab === 'extensions' &&
          (project ? (
            <ExtensionsTab projectRow={projectRow} client={client} />
          ) : (
            <Notice>Select or create a project to manage extensions.</Notice>
          ))}
        {tab === 'cortex' &&
          (project ? (
            <CortexTab project={project} projectRow={projectRow} client={client} onSaved={onSaved} />
          ) : (
            <Notice>Select or create a project to inspect Cortex.</Notice>
          ))}
        {tab === 'license' && <LicenseTab />}
      </div>
    </GlassPanel>
  )
}

export function SettingsView(props: SettingsViewProps) {
  return <SettingsViewBody key={props.project ?? '_global'} {...props} />
}
