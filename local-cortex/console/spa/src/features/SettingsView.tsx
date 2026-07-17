/**
 * SettingsView — the PROJECT-LEVEL / GLOBAL canonical settings surface, in TABS.
 *
 * The single home for PROJECT settings (the no-repeat rule keeps it out of every
 * column). A glass sub-nav mirrors the legacy console's tab shell:
 *
 *   System · Workspace · Extensions · Cortex
 *
 * NOTE: per-agent configuration (harness / model / reasoning / designation / role) is
 * NO LONGER here — it MOVED into the agent-detail middle pane (`AgentConfigEditor`,
 * rendered by `AgentDetail`), per the CTO's "settings in the middle pane of the agent"
 * directive. You select an agent and edit its config inline, right there. Settings
 * retains only the PROJECT/global config below (no per-agent duplication).
 *
 *   • System — a TYPED form driven by GET /settings/{p}/system-schema of the
 *     Cortex connection, harness paths/flags, and app
 *     preferences): text / number inputs, bool → toggle switch, SECRET → a masked
 *     field with a Replace/Hide affordance (the stored secret is NEVER rendered),
 *     readonly → static. Save POSTs ONLY changed keys to POST .../app. The raw
 *     App-settings key→value editor is folded in below (still editable).
 *   • Workspace — per-project repo_root editor → POST .../workspace; shows
 *     `previous → new` on success, the error string otherwise.
 *   • Extensions — installed project-pack modules, pack-local enable/disable helper, and
 *     restart-required visibility.
 *   • Cortex — the connection + the live /cortex/health read-out (the console JSON
 *     health endpoint) + the selected project's registry row (folder). A
 *     compact 6-layer reference.
 * Project autonomy controls live on the project Dashboard where the project name
 * and working folder are visible. The global engine autostart remains in System.
 *
 * Read data flows in as props (the shell fetches + polls them); writes go through the
 * injected `client`. On a successful write the view calls `onSaved` (the shell's
 * refetch) — REFETCH-ON-SUCCESS, the simplest correct sync. Graceful-degrade rides
 * through everywhere — a down store / a stale-backend 404 yields a hint, never a crash.
 */

import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { GlassPanel, GlassCard, StatusDot } from '../components/glass'
import { cx } from '../components/ui'
import type {
  AppSettings,
  AppSettingsWriteResult,
  CortexEmbeddingBackfillRequest,
  CortexEmbeddingBackfillResult,
  CortexEmbeddingBacklogResult,
  CortexConfigResult,
  CortexPlatformConfig,
  Project,
  ProjectPackExtension,
  ProjectPackExtensionResult,
  ProjectPackListResult,
  ProjectPackOption,
  ProjectPackPortal,
  RunStateRestartStatus,
  SystemField,
  SystemSchema,
  WorkspaceResult,
} from '../api'

/**
 * The WRITE surface the view drives. The concrete `api` object satisfies this
 * structurally (so the shell passes `api`); tests pass a fake that records calls.
 */
export interface SettingsWriteClient {
  setAppSetting: (project: string, key: string, value: unknown) => Promise<AppSettingsWriteResult>
  // -- step 3b writes --------------------------------------------------------
  /** Upsert a batch of app/system settings (the typed System form's "save changed keys"). */
  setAppSettings?: (project: string, settings: Record<string, unknown>) => Promise<AppSettingsWriteResult>
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


/** The settings sections (the sub-nav order, mirroring the legacy tab shell). */
type SettingsTab =
  | 'system'
  | 'workspace'
  | 'extensions'
  | 'cortex'

const TABS: { id: SettingsTab; label: string }[] = [
  { id: 'system', label: 'System' },
  { id: 'workspace', label: 'Workspace' },
  { id: 'extensions', label: 'Extensions' },
  { id: 'cortex', label: 'Cortex' },
]

interface SettingsViewProps {
  project: string | null
  appSettings: AppSettings | null
  /** The typed System schema (GET …/system-schema). Null while loading / on a stale backend. */
  systemSchema: SystemSchema | null
  /** The selected project's registry row (for the Cortex tab — repo_root / status). */
  projectRow: Project | null
  /** ALL active projects (the Workspace tab is a MULTI-project repo-root editor — every active
   * project's repo_root, each editable). Reuses the /projects rows the shell already holds; when
   * absent the Workspace tab degrades to the single selected project (back-compat). */
  projects?: Project[]
  loading: boolean
  error: Error | null
  client: SettingsWriteClient
  /** Called after any successful write — the shell refetches the settings resources. */
  onSaved: () => void
}

/** Render a settings value compactly for the editable input (strings/numbers inline; objects as JSON). */
function valueToInput(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

/**
 * Resolve the choosable options for a `select` System field. STATIC options come from
 * the schema (`field.options`, e.g. the harnesses). A DYNAMIC `options_source` is
 * resolved HERE from live SPA data: "projects" → the registered-project keys, so the
 * default-project dropdown is never a hardcoded phantom (a project key that doesn't
 * exist" papercut). An unknown / empty source → [] (the field degrades to a text input).
 */
type SelectOption = { value: string; label: string }

function resolveFieldOptions(field: SystemField, projects?: Project[]): SelectOption[] {
  if (field.options_source === 'projects') {
    return (projects ?? [])
      .filter((p) => Boolean(p.project_key))
      .map((p) => ({ value: p.project_key, label: p.display_name || p.project_key }))
  }
  return (field.options ?? []).map((value) => ({ value, label: value }))
}

const FIELD_CLASS =
  'glass-soft w-full rounded-md border border-glass-line bg-base-900/40 px-2.5 py-1.5 text-xs ' +
  'text-ink-100 outline-none transition-colors placeholder:text-ink-600 ' +
  'focus:border-mint-400/50 focus:ring-1 focus:ring-mint-400/30 disabled:opacity-50'

const BTN_CLASS =
  'shrink-0 rounded-md px-2.5 py-1.5 text-[11px] font-semibold transition-colors ' +
  'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/30 hover:bg-mint-500/25 ' +
  'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-mint-500/15'

const BTN_GHOST =
  'shrink-0 rounded-md px-2.5 py-1.5 text-[11px] font-semibold transition-colors ' +
  'bg-base-700/50 text-ink-300 ring-1 ring-glass-line hover:bg-base-700/80 hover:text-ink-100 ' +
  'disabled:cursor-not-allowed disabled:opacity-40'

const SECTION_LABEL =
  'px-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500'

/** A toggle-switch (the bool-field + flag affordance). */
function Toggle({
  on,
  disabled,
  onToggle,
  label,
}: {
  on: boolean
  disabled: boolean
  onToggle: (next: boolean) => void
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onToggle(!on)}
      className={cx(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-50',
        on ? 'bg-mint-500/70' : 'bg-base-700/70',
      )}
    >
      <span
        className={cx(
          'inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform',
          on ? 'translate-x-[18px]' : 'translate-x-[3px]',
        )}
      />
    </button>
  )
}

/** One editable app-setting row: the key, an editable value, and a save (enabled when changed). */
function AppSettingRow({
  settingKey,
  initial,
  disabled,
  onSave,
}: {
  settingKey: string
  initial: string
  disabled: boolean
  onSave: (key: string, value: string) => void
}) {
  const [value, setValue] = useState(initial)
  const dirty = value !== initial
  return (
    <div className="flex items-center gap-3 px-4 py-2.5" data-setting-row>
      <label
        className="w-44 shrink-0 truncate font-mono text-[11px] text-ink-400"
        title={settingKey}
        htmlFor={`setting-${settingKey}`}
      >
        {settingKey}
      </label>
      <input
        id={`setting-${settingKey}`}
        className={FIELD_CLASS}
        value={value}
        disabled={disabled}
        onChange={(e) => setValue(e.target.value)}
      />
      <button
        type="button"
        className={BTN_CLASS}
        disabled={disabled || !dirty}
        onClick={() => onSave(settingKey, value)}
      >
        Save
      </button>
    </div>
  )
}

// ===========================================================================
//  Shared field label class (used by the System and Workspace tabs).
//  NOTE: the per-agent Configure experience that used to live HERE has MOVED into the
//  agent-detail middle pane (`AgentConfigEditor`) — Settings carries no per-agent
//  config anymore (the no-repeat / "settings in the agent pane" directive).
// ===========================================================================

const LABEL_CLASS =
  'flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-ink-500'

// ===========================================================================
//  System tab — the typed form, secret masking, and raw App-settings editor.
// ===========================================================================

/**
 * One typed System field. The VALUE is local component state; `onChange(key,value)`
 * bubbles a CHANGED value up to the form's dirty-tracker (so Save can post only
 * changed keys). A secret renders masked with a Replace/Hide affordance and NEVER
 * places the stored secret in the DOM.
 */
function SystemFieldRow({
  field,
  disabled,
  options,
  registerChange,
}: {
  field: SystemField
  disabled: boolean
  /** Resolved choosable options for a `select` field (static or dynamic); ignored otherwise. */
  options?: SelectOption[]
  /** Report a field's current value (or `undefined` to mark it unchanged/cleared). */
  registerChange: (key: string, value: unknown | undefined) => void
}) {
  const id = `sys-${field.key}`

  // text / number / select — seed from the stored value; a change registers the new value.
  const [text, setText] = useState<string>(
    field.type === 'number' || field.type === 'text' || field.type === 'readonly' || field.type === 'select'
      ? valueToInput(field.value)
      : '',
  )
  // bool — seed from the stored boolean.
  const [bool, setBool] = useState<boolean>(Boolean(field.value))
  // secret — `revealed` switches the masked display for an EMPTY editable input
  // (the stored secret is never shown); `secretText` is the freshly-typed value.
  const [revealed, setRevealed] = useState(false)
  const [secretText, setSecretText] = useState('')

  const help = field.help ? (
    <span className="block text-[10px] leading-snug text-ink-600">{field.help}</span>
  ) : null

  const keyLabel = (
    <label
      htmlFor={id}
      className="flex items-center gap-1.5 text-[11px] font-medium text-ink-300"
    >
      {field.type === 'readonly' && (
        <span aria-hidden="true" title="read-only" className="text-ink-600">
          🔒
        </span>
      )}
      {field.label}
    </label>
  )

  if (field.type === 'bool') {
    return (
      <div className="flex items-start gap-3 px-4 py-2.5" data-system-field={field.key}>
        <div className="min-w-0 flex-1">
          {keyLabel}
          {help}
        </div>
        <Toggle
          on={bool}
          disabled={disabled}
          label={field.label}
          onToggle={(next) => {
            setBool(next)
            registerChange(field.key, next)
          }}
        />
      </div>
    )
  }

  if (field.type === 'readonly') {
    return (
      <div className="space-y-1 px-4 py-2.5" data-system-field={field.key}>
        {keyLabel}
        <input
          id={id}
          className={cx(FIELD_CLASS, 'cursor-default opacity-80')}
          value={text}
          readOnly
        />
        {help}
      </div>
    )
  }

  if (field.type === 'secret') {
    // The masked, NON-editable display when not revealing; an EMPTY editable input
    // (placeholder "enter a key…") once Replace is clicked. The stored secret value
    // is NEVER rendered — only the masked placeholder.
    const masked = field.is_set ? field.placeholder || '•••• set' : ''
    return (
      <div className="space-y-1 px-4 py-2.5" data-system-field={field.key}>
        {keyLabel}
        <div className="flex items-center gap-2">
          {revealed ? (
            <input
              id={id}
              className={FIELD_CLASS}
              type="text"
              value={secretText}
              placeholder="enter a key…"
              disabled={disabled}
              autoComplete="off"
              spellCheck={false}
              data-revealed="1"
              onChange={(e) => {
                const v = e.target.value
                setSecretText(v)
                // A non-empty typed value is the change; an empty input → unchanged
                // (keep the stored secret) so we DON'T send it.
                registerChange(field.key, v.trim() ? v : undefined)
              }}
            />
          ) : (
            <input
              id={id}
              className={cx(FIELD_CLASS, 'masked cursor-default text-ink-400')}
              type="text"
              value={masked}
              placeholder={field.is_set ? '' : 'no key set'}
              readOnly
              data-revealed="0"
              aria-label={`${field.label} (masked)`}
            />
          )}
          <button
            type="button"
            className={BTN_GHOST}
            disabled={disabled}
            aria-label={revealed ? `Hide ${field.label}` : `Replace ${field.label}`}
            onClick={() => {
              if (revealed) {
                // Hide → re-mask + drop the typed value (back to "unchanged").
                setRevealed(false)
                setSecretText('')
                registerChange(field.key, undefined)
              } else {
                setRevealed(true)
              }
            }}
          >
            {revealed ? 'Hide' : 'Replace'}
          </button>
        </div>
        {help}
      </div>
    )
  }

  if (field.type === 'select' && (options?.length ?? 0) > 0) {
    // A dropdown of the allowed values — static (the harnesses) or dynamic (the
    // registered projects). The stored value is pre-selected; if it isn't among the
    // options (e.g. a since-removed project), it's shown as a leading option so the
    // operator still sees what's set and can switch. An empty options list (a dynamic
    // source with no data yet) falls through to the plain text input below.
    const opts = options ?? []
    const stored = valueToInput(field.value)
    const withStored = stored && !opts.some((option) => option.value === stored)
      ? [{ value: stored, label: stored }, ...opts]
      : opts
    return (
      <div className="space-y-1 px-4 py-2.5" data-system-field={field.key}>
        {keyLabel}
        <select
          id={id}
          className={FIELD_CLASS}
          value={text}
          disabled={disabled}
          onChange={(e) => {
            const v = e.target.value
            setText(v)
            registerChange(field.key, v)
          }}
        >
          {!stored && <option value="">— select —</option>}
          {withStored.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        {help}
      </div>
    )
  }

  // text / number (also the graceful fallback for a `select` with no options yet)
  return (
    <div className="space-y-1 px-4 py-2.5" data-system-field={field.key}>
      {keyLabel}
      <input
        id={id}
        className={FIELD_CLASS}
        type={field.type === 'number' ? 'number' : 'text'}
        value={text}
        placeholder={field.placeholder || ''}
        disabled={disabled}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => {
          const v = e.target.value
          setText(v)
          registerChange(field.key, field.type === 'number' && v !== '' ? Number(v) : v)
        }}
      />
      {help}
    </div>
  )
}


/** The System tab — typed form (save only changed keys) plus the raw editor. */
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
  /** Active projects — the dynamic source for the default-project `select` dropdown. */
  projects?: Project[]
  client: SettingsWriteClient
  onSaved: () => void
}) {
  // The set of CHANGED keys → values (only these are POSTed). A secret left blank /
  // a bool/text reverted is removed from the map (so "untouched" never gets sent).
  const [changed, setChanged] = useState<Record<string, unknown>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // A bump key forces the typed inputs to remount (Cancel / Reload-from-store).
  const [resetNonce, setResetNonce] = useState(0)

  // Re-seed (clear pending changes) whenever a NEW schema object lands.
  const [seededFrom, setSeededFrom] = useState<SystemSchema | null>(schema)
  if (schema !== seededFrom) {
    setSeededFrom(schema)
    setChanged({})
    setSaved(false)
  }

  const registerChange = useCallback((key: string, value: unknown | undefined) => {
    setSaved(false)
    setChanged((prev) => {
      const next = { ...prev }
      if (value === undefined) delete next[key]
      else next[key] = value
      return next
    })
  }, [])

  const changedKeys = Object.keys(changed)
  const dirty = changedKeys.length > 0

  async function save() {
    if (!dirty) return
    setSaving(true)
    setError(null)
    try {
      // POST ONLY the changed keys. Prefer a batch upsert; fall back to per-key.
      if (client.setAppSettings) {
        await client.setAppSettings(project, changed)
      } else {
        for (const k of changedKeys) {
          await client.setAppSetting(project, k, changed[k])
        }
      }
      setChanged({})
      setSaved(true)
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function resetForm() {
    setChanged({})
    setSaved(false)
    setError(null)
    setResetNonce((n) => n + 1)
    onSaved() // reload-from-store: ask the shell to refetch the schema
  }

  const groups = schema?.groups ?? []
  const connected = schema?.store_connected ?? appSettings?.store_connected ?? false

  // The raw App-settings keys (folded below the typed form so they stay editable).
  const settingsMap = appSettings?.settings ?? {}
  const rawKeys = Object.keys(settingsMap).sort()

  return (
    <div className="space-y-5">
      <div className="px-1">
        <p className="text-[11px] leading-relaxed text-ink-500">
          Console configuration — <b>Cortex connection</b>, harness paths/flags, and app
          preferences. Secrets live only in the gitignored local store and are <b>never rendered
          back</b>: a stored key shows as <code className="font-mono">•••• set</code>; use{' '}
          <b>Replace</b> to enter a new one. <b>Save</b> writes only the fields you changed.
        </p>
      </div>

      {error && (
        <p className="rounded-md bg-run-errored/12 px-3 py-2 text-xs leading-relaxed text-run-errored/90">
          Couldn’t save — {error}
        </p>
      )}

      {!schema ? (
        <p className="px-1 py-2 text-xs leading-relaxed text-ink-500">
          {appSettings
            ? 'The typed System schema isn’t available in this build — use the raw editor below.'
            : 'Loading the System form…'}
        </p>
      ) : groups.length === 0 ? (
        <p className="px-1 py-2 text-xs text-ink-500">
          The System schema is empty{!connected ? ' (the settings store is offline)' : ''}.
        </p>
      ) : (
        <div className="space-y-3" key={resetNonce}>
          {groups.map((g) => (
            <GlassCard key={g.key} className="overflow-hidden p-0" data-system-group={g.key}>
              <div className="border-b border-glass-line px-4 py-2.5">
                <h3 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-400">
                  {g.label}
                </h3>
              </div>
              <div className="divide-y divide-glass-line">
                {g.fields.map((f) => (
                  <SystemFieldRow
                    key={f.key}
                    field={f}
                    disabled={saving}
                    options={resolveFieldOptions(f, projects)}
                    registerChange={registerChange}
                  />
                ))}
              </div>
            </GlassCard>
          ))}

          {/* footer — Save (changed keys only), Cancel + Reload-from-store. */}
          <div className="flex items-center gap-3 px-1">
            <button type="button" className={BTN_CLASS} disabled={saving || !dirty} onClick={save}>
              {saving ? 'Saving…' : `Save settings${dirty ? ` (${changedKeys.length})` : ''}`}
            </button>
            <button
              type="button"
              className={BTN_GHOST}
              disabled={saving || !dirty}
              onClick={resetForm}
            >
              Cancel
            </button>
            <button type="button" className={BTN_GHOST} disabled={saving} onClick={resetForm}>
              Reload from store
            </button>
            {saved && <span className="text-[11px] font-medium text-mint-300">Saved ✓</span>}
          </div>
        </div>
      )}

      {/* The raw App-settings key→value editor, folded in (still fully editable). */}
      {appSettings && rawKeys.length > 0 && (
        <section className="space-y-2">
          <div className="flex items-baseline justify-between px-1">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-500">
              App settings (raw)
            </h3>
            {!connected && <span className="text-[10px] text-run-errored/70">store offline</span>}
          </div>
          <GlassCard className="overflow-hidden p-0">
            <div className="divide-y divide-glass-line">
              {rawKeys.map((key) => (
                <AppSettingRow
                  key={key}
                  settingKey={key}
                  initial={valueToInput(settingsMap[key])}
                  disabled={saving}
                  onSave={(k, value) => {
                    client
                      .setAppSetting(project, k, value)
                      .then(() => onSaved())
                      .catch((e: unknown) =>
                        setError(e instanceof Error ? e.message : String(e)),
                      )
                  }}
                />
              ))}
            </div>
          </GlassCard>
        </section>
      )}
    </div>
  )
}

// ===========================================================================
//  Workspace tab — the MULTI-project repo_root editor.
//
//  This is the workspace-wide editor: it lists EVERY active project's repo_root, each
//  in its own editable row that saves to ITS OWN project (`POST /settings/{p}/workspace`).
//  The CTO's question ("is showing only the selected project's repo-root right?") resolved
//  to NO — a multi-project workspace editor should let you set the working folder for any
//  registered project from one place. It reuses the /projects rows the shell already holds
//  (no extra fetch); when that list is absent it degrades to the single selected project.
// ===========================================================================

/** One project's repo_root editor row — its own value/dirty/save state, scoped to ITS project
 * key. Saving POSTs to that project (never the globally-selected one), so a non-selected row
 * edits the right registry entry. Shows `previous → new` on success, the error otherwise. */
function WorkspaceProjectRow({
  row,
  client,
  selected,
}: {
  row: Project
  client: SettingsWriteClient
  /** True for the globally-selected project (a subtle "current" marker). */
  selected: boolean
}) {
  const projectKey = row.project_key
  const storedRoot = (row.repo_root as string | null) ?? ''
  const name = (row.display_name as string | null) || projectKey
  const inputId = `ws-repo-root-${projectKey}`

  const [value, setValue] = useState<string>(storedRoot)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<WorkspaceResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Re-seed when a fresh stored root lands for THIS project (a refetch after a save elsewhere).
  const [seededFrom, setSeededFrom] = useState<string>(storedRoot)
  if (storedRoot !== seededFrom) {
    setSeededFrom(storedRoot)
    setValue(storedRoot)
  }

  async function save() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      // POST to THIS row's project key — the multi-project edit (not the selected project).
      const res = await client.setWorkspace(projectKey, { repo_root: value.trim() })
      setResult(res)
      if (!res.ok) setError(res.error || 'couldn’t set the project folder.')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const dirty = value.trim() !== storedRoot.trim()

  return (
    <GlassCard className="space-y-3 p-4" data-testid={`ws-row-${projectKey}`} data-ws-project-row={projectKey}>
      <div className="flex items-baseline gap-2">
        <span className="truncate text-sm font-medium text-ink-100" title={name}>
          {name}
        </span>
        <code className="shrink-0 font-mono text-[10px] text-ink-500">{projectKey}</code>
        {selected && (
          <span className="ml-auto shrink-0 rounded bg-mint-500/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-mint-300">
            current
          </span>
        )}
      </div>
      <div className="space-y-1">
        <label htmlFor={inputId} className={LABEL_CLASS}>
          Repo root
        </label>
        <input
          id={inputId}
          className={FIELD_CLASS}
          value={value}
          placeholder="/absolute/path/to/the/project"
          disabled={busy}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setValue(e.target.value)}
        />
        <span className="block text-[10px] text-ink-600">
          current: <code className="font-mono text-ink-500">{storedRoot || '—'}</code>
        </span>
      </div>
      <div className="flex items-center gap-3">
        <button type="button" className={BTN_CLASS} disabled={busy || !dirty} onClick={save}>
          {busy ? 'Saving…' : 'Save folder'}
        </button>
        {result?.ok && (
          <span className="text-[11px] font-medium text-mint-300" data-testid="ws-success">
            <code className="font-mono">{result.previous_repo_root || '—'}</code>
            {' → '}
            <code className="font-mono">{result.repo_root}</code>
          </span>
        )}
      </div>
      {error && (
        <p className="rounded-md bg-run-errored/12 px-3 py-2 text-xs leading-relaxed text-run-errored/90">
          {error}
        </p>
      )}
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
  // The multi-project list, de-duped by project_key. Prefer the full active-projects list; fall
  // back to the single selected row (back-compat / a degraded shell). Each row is keyed by its
  // project_key so a refetch re-seeds the right input.
  const rows: Project[] = (() => {
    const list = projects && projects.length > 0 ? projects : projectRow ? [projectRow] : []
    const seen = new Set<string>()
    return list.filter((p) => {
      const k = p?.project_key
      if (!k || seen.has(k)) return false
      seen.add(k)
      return true
    })
  })()

  return (
    <div className="space-y-3">
      <p className="px-1 text-[11px] leading-relaxed text-ink-500">
        Each registered project’s canonical working folder (<code className="font-mono">repo_root</code>)
        — the absolute path the harness runs that project’s agents in. This is the workspace-wide
        editor: set the folder for ANY active project here. Saving a row PATCHes the Cortex registry
        for THAT project (the admin token is sent server-side and never exposed).
      </p>
      {rows.length === 0 ? (
        <p className="px-1 py-2 text-xs text-ink-500">No active projects to configure.</p>
      ) : (
        rows.map((row) => (
          <WorkspaceProjectRow
            key={row.project_key}
            row={row}
            client={client}
            selected={row.project_key === project}
          />
        ))
      )}
    </div>
  )
}

// ===========================================================================
//  Extensions tab — installed project-pack modules and restart-required state.
// ===========================================================================

function extensionStatusLabel(ext: ProjectPackExtension): string {
  switch (ext.status) {
    case 'loaded':
      return 'Loaded'
    case 'enabled_restart_required':
      return 'Enabled, restart required'
    case 'loaded_disable_restart_required':
      return 'Disabled, restart required'
    case 'disabled':
      return 'Disabled'
    default:
      return ext.status || 'Unknown'
  }
}

function portalStatusLabel(portal: ProjectPackPortal): string {
  switch (portal.status) {
    case 'ready':
      return 'Ready'
    case 'missing_frontend':
      return 'Missing frontend'
    case 'frontend_not_installed':
      return 'Frontend not installed'
    case 'metadata_only':
      return 'Metadata only'
    default:
      return portal.status || 'Unknown'
  }
}

function PortalRow({ portal }: { portal: ProjectPackPortal }) {
  const ready = portal.status === 'ready' || portal.status === 'metadata_only'
  return (
    <div className="space-y-2 rounded-md border border-glass-line bg-base-950/25 p-3">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-xs text-ink-100">{portal.route_prefix}</code>
            <span
              className={cx(
                'rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide',
                ready ? 'bg-mint-500/15 text-mint-300' : 'bg-run-errored/12 text-run-errored',
              )}
            >
              {portalStatusLabel(portal)}
            </span>
          </div>
          <p className="mt-1 text-[10px] leading-relaxed text-ink-600">
            {portal.key} · agent <code className="font-mono">{portal.agent || 'unset'}</code> ·{' '}
            {portal.auth || 'auth unset'} · {portal.stream_contract || 'stream unset'}
          </p>
          {portal.frontend_path && (
            <p className="mt-1 text-[10px] leading-relaxed text-ink-600">
              Frontend: <code className="font-mono">{portal.frontend_path}</code>{' '}
              {portal.frontend_exists ? 'is installed' : 'is not installed'}
            </p>
          )}
          {portal.runtime_contract && (
            <div className="mt-2 rounded border border-glass-line bg-base-950/40 p-2 text-[10px] leading-relaxed text-ink-500">
              <div className="font-semibold uppercase tracking-wide text-ink-400">Canonical stream replay</div>
              <div>
                Chat POST: <code className="font-mono text-ink-300">{portal.runtime_contract.chat_endpoint_template}</code>
              </div>
              <div>
                Run SSE: <code className="font-mono text-ink-300">{portal.runtime_contract.stream_endpoint_template}</code>
              </div>
              <div>
                Run detail: <code className="font-mono text-ink-300">{portal.runtime_contract.run_endpoint_template}</code>
              </div>
            </div>
          )}
          {portal.description && (
            <p className="mt-1 text-[10px] leading-relaxed text-ink-600">{portal.description}</p>
          )}
        </div>
      </div>
    </div>
  )
}

function ExtensionRow({
  repoRoot,
  pack,
  ext,
  client,
  onUpdated,
}: {
  repoRoot: string
  pack: ProjectPackOption
  ext: ProjectPackExtension
  client: SettingsWriteClient
  onUpdated: (pack: ProjectPackOption) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nextEnabled = !ext.enabled
  async function toggle() {
    if (!client.setProjectPackExtension) return
    setBusy(true)
    setError(null)
    try {
      const res = await client.setProjectPackExtension({
        repo_root: repoRoot,
        pack_key: pack.key,
        module: ext.module,
        enabled: nextEnabled,
      })
      if (res.ok && res.pack) onUpdated(res.pack)
      else setError(res.error || 'couldn’t update the extension helper.')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2 rounded-md border border-glass-line bg-base-950/25 p-3">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-xs text-ink-100">{ext.module}</code>
            {ext.required && (
              <span className="rounded bg-run-queued/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-run-queued">
                required
              </span>
            )}
            <span
              className={cx(
                'rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide',
                ext.loaded
                  ? 'bg-mint-500/15 text-mint-300'
                  : ext.restart_required
                    ? 'bg-run-queued/15 text-run-queued'
                    : 'bg-base-700/60 text-ink-500',
              )}
            >
              {extensionStatusLabel(ext)}
            </span>
          </div>
          {ext.description && (
            <p className="mt-1 text-[10px] leading-relaxed text-ink-600">{ext.description}</p>
          )}
        </div>
        <button
          type="button"
          className={ext.enabled ? BTN_GHOST : BTN_CLASS}
          disabled={busy || !client.setProjectPackExtension}
          onClick={toggle}
          aria-label={`${ext.enabled ? 'Disable' : 'Enable'} ${ext.module}`}
        >
          {busy ? 'Saving…' : ext.enabled ? 'Disable' : 'Enable'}
        </button>
      </div>
      {error && <p className="text-[10px] leading-relaxed text-run-errored/90">{error}</p>}
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
  const repoRoot = String(projectRow?.repo_root || '').trim()
  const [packs, setPacks] = useState<ProjectPackOption[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    if (!repoRoot || !client.listProjectPacks) return
    setLoading(true)
    setError(null)
    client
      .listProjectPacks(repoRoot)
      .then((res) => {
        if (res.ok) {
          setPacks(res.packs || [])
        } else {
          setPacks([])
          setError(res.error || 'couldn’t read installed project packs.')
        }
      })
      .catch((e: unknown) => {
        setPacks([])
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setLoading(false))
  }, [client, repoRoot])

  useEffect(() => {
    queueMicrotask(load)
  }, [load])

  const updatePack = useCallback((pack: ProjectPackOption) => {
    setPacks((prev) => (prev ?? []).map((p) => (p.key === pack.key ? pack : p)))
  }, [])

  if (!repoRoot) {
    return (
      <p className="px-1 py-2 text-xs leading-relaxed text-ink-500">
        Set this project&rsquo;s repo root in Workspace before managing installed project packs.
      </p>
    )
  }
  if (!client.listProjectPacks) {
    return (
      <p className="px-1 py-2 text-xs leading-relaxed text-ink-500">
        This console backend does not expose project-pack discovery yet.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3 px-1">
        <div>
          <h2 className={SECTION_LABEL}>Project-pack extensions</h2>
          <p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-ink-500">
            Installed packs live under <code className="font-mono">.kaidera-os/project-packs</code>{' '}
            inside <code className="font-mono">{repoRoot}</code>. Enable/disable writes the
            pack-local helper file; loading changes require a console restart with the updated
            extension environment.
          </p>
        </div>
        <button type="button" className={BTN_GHOST} disabled={loading} onClick={load}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <p className="rounded-md bg-run-errored/12 px-3 py-2 text-xs leading-relaxed text-run-errored/90">
          {error}
        </p>
      )}

      {loading && !packs ? (
        <p className="px-1 py-2 text-xs text-ink-500">Loading installed packs…</p>
      ) : packs && packs.length === 0 ? (
        <p className="px-1 py-2 text-xs text-ink-500">No project packs installed in this folder.</p>
      ) : (
        (packs ?? []).map((pack) => {
          const extensions = pack.extensions ?? []
          const portals = pack.portals ?? []
          return (
            <GlassCard key={pack.key} className="space-y-3 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-ink-100">{pack.name}</h3>
                  <p className="mt-1 text-[10px] text-ink-600">
                    <code className="font-mono">{pack.key}</code> · v{pack.version} · {pack.seed_count}{' '}
                    seed file{pack.seed_count === 1 ? '' : 's'}
                  </p>
                </div>
                {pack.restart_required && (
                  <span className="rounded bg-run-queued/15 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-run-queued">
                    Restart required
                  </span>
                )}
              </div>
              {extensions.length === 0 ? (
                <p className="text-[11px] text-ink-500">This pack declares no console extensions.</p>
              ) : (
                <div className="space-y-2">
                  {extensions.map((ext) => (
                    <ExtensionRow
                      key={ext.module}
                      repoRoot={repoRoot}
                      pack={pack}
                      ext={ext}
                      client={client}
                      onUpdated={updatePack}
                    />
                  ))}
                </div>
              )}
              <div className="border-t border-glass-line pt-3">
                <h4 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-ink-500">
                  Package portals
                </h4>
                {portals.length === 0 ? (
                  <p className="mt-2 text-[11px] text-ink-500">This pack declares no thin portals.</p>
                ) : (
                  <div className="mt-2 space-y-2">
                    {portals.map((portal) => (
                      <PortalRow key={portal.key} portal={portal} />
                    ))}
                  </div>
                )}
              </div>
              {pack.restart_required && (
                <p className="text-[10px] leading-relaxed text-ink-600">
                  The helper value and the loaded process differ. Restart the console after applying
                  the updated <code className="font-mono">{pack.extension_env || 'KAIDERA_OS_EXTENSION_MODULES'}</code>{' '}
                  value.
                </p>
              )}
            </GlassCard>
          )
        })
      )}
    </div>
  )
}

// ===========================================================================
//  Cortex tab — connection, ingestion model config, health, and registry row.
//  Reuses the project registry row and gracefully degrades when the Cortex admin
//  config or health probes are not available.
// ===========================================================================

/** The local-cortex 6-layer reference (from local-cortex/ARCHITECTURE.md). */
const CORTEX_LAYERS: { id: string; name: string; status: 'KEEP' | 'NEW' | 'OPTIMIZE'; what: string }[] = [
  { id: 'L6', name: 'Boot Context', status: 'KEEP', what: 'cortex-boot — identity + facts + recent history' },
  { id: 'L5', name: 'Multimodal Artifacts', status: 'NEW', what: 'typed modality + captions + provenance' },
  { id: 'L4', name: 'Knowledge Graph', status: 'NEW', what: 'entities + relationships (dual-level retrieval)' },
  { id: 'L3', name: 'Code Graph', status: 'OPTIMIZE', what: 'better-code-review-graph via DuckDB + SQLite' },
  { id: 'L2', name: 'Vector Embeddings', status: 'KEEP', what: 'pgvector 768-d over durable text' },
  { id: 'L1', name: 'Verbatim Storage', status: 'KEEP', what: 'decisions · lessons · handoffs · messages · runs' },
]

const DEFAULT_CORTEX_CONFIG: CortexPlatformConfig = {
  embedding_provider: 'openrouter',
  embedding_model: 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
  embedding_dims: 768,
  rerank_enabled: true,
  rerank_provider: 'nvidia',
  rerank_model: 'nv-rerank-qa-mistral-4b:1',
  embed_input_max_chars: 500,
  rerank_input_max_chars: 500,
  embed_timeout_ms: 15000,
  rerank_timeout_ms: 15000,
}

const INGESTION_PROVIDERS = ['openrouter', 'nvidia', 'openai', 'cohere']

const EMBEDDING_MODEL_SUGGESTIONS = [
  'nvidia/llama-nemotron-embed-vl-1b-v2:free',
  'openai/text-embedding-3-small',
  'google/gemini-embedding-001',
  'nvidia/llama-nemotron-embed-1b-v2',
  'nvidia/llama-nemotron-embed-vl-1b-v2',
]

const RERANK_MODEL_SUGGESTIONS = [
  'nv-rerank-qa-mistral-4b:1',
  'nvidia/llama-nemotron-rerank-1b-v2',
  'nvidia/nv-rerankqa-mistral-4b-v3',
]

interface CortexHealth {
  status?: string
  surface_version?: string | null
  event_backend?: string | null
  embed_provider?: string | null
  embed_model?: string | null
  embed_dims?: number | null
  rerank_enabled?: boolean | null
  rerank_provider?: string | null
  rerank_model?: string | null
  rls_enforced?: boolean | null
  /** The Cortex API base URL the console talks to (folded in by /cortex/health). */
  base_url?: string | null
  /** The project the read-out names (echoed by /cortex/health). */
  project?: string | null
  error?: string
  [k: string]: unknown
}

/** A compact label : value read-out row (the Cortex-tab cards). */
function CortexKV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-4 py-2">
      <span className="text-[11px] text-ink-500">{k}</span>
      <span className="min-w-0 truncate text-right font-mono text-[11px] text-ink-200">{v}</span>
    </div>
  )
}

function normalizeCortexConfig(config?: CortexPlatformConfig | null): CortexPlatformConfig {
  return { ...DEFAULT_CORTEX_CONFIG, ...(config ?? {}) }
}

function asPositiveInt(value: unknown, fallback: number): number {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : fallback
}

function CortexFormField({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="space-y-1.5">
      <span className="block text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-500">
        {label}
      </span>
      {children}
      {hint && <span className="block text-[10px] leading-relaxed text-ink-500">{hint}</span>}
    </label>
  )
}

function CortexIngestionSettings({
  client,
  onSaved,
}: {
  client: SettingsWriteClient
  onSaved: () => void
}) {
  const [form, setForm] = useState<CortexPlatformConfig>(DEFAULT_CORTEX_CONFIG)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    let alive = true
    queueMicrotask(() => {
      if (!alive) return
      setLoading(true)
      setError(null)
    })
    if (!client.cortexConfig) {
      queueMicrotask(() => {
        if (!alive) return
        setLoading(false)
        setError('The console backend does not expose Cortex config in this build.')
      })
      return () => {
        alive = false
        ctrl.abort()
      }
    }
    client
      .cortexConfig(ctrl.signal)
      .then((result) => {
        if (!alive) return
        setForm(normalizeCortexConfig(result.config))
        if (!result.ok && result.error) setError(result.error)
      })
      .catch((e) => {
        if (!alive) return
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
      ctrl.abort()
    }
  }, [client])

  const patch = (next: Partial<CortexPlatformConfig>) => {
    setSaved(null)
    setForm((cur) => ({ ...cur, ...next }))
  }

  async function save() {
    if (!client.setCortexConfig) {
      setError('The console backend does not expose Cortex config writes in this build.')
      return
    }
    setSaving(true)
    setError(null)
    setSaved(null)
    const payload: Partial<CortexPlatformConfig> = {
      embedding_provider: String(form.embedding_provider || '').trim(),
      embedding_model: String(form.embedding_model || '').trim(),
      embedding_dims: asPositiveInt(form.embedding_dims, 768),
      rerank_enabled: Boolean(form.rerank_enabled),
      rerank_provider: String(form.rerank_provider || '').trim(),
      rerank_model: String(form.rerank_model || '').trim(),
      embed_input_max_chars: asPositiveInt(form.embed_input_max_chars, 500),
      rerank_input_max_chars: asPositiveInt(form.rerank_input_max_chars, 500),
      embed_timeout_ms: asPositiveInt(form.embed_timeout_ms, 15000),
      rerank_timeout_ms: asPositiveInt(form.rerank_timeout_ms, 15000),
    }
    try {
      const result = await client.setCortexConfig(payload)
      setForm(normalizeCortexConfig(result.config))
      if (!result.ok) {
        setError(result.error || 'Cortex config write did not succeed.')
        return
      }
      setSaved('Saved. New ingestion calls will use this config after the short API cache expires.')
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <GlassCard className="space-y-4 p-4" data-cortex-ingestion-settings>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-ink-100">Ingestion models</h3>
          <p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-ink-500">
            API-owned defaults for Cortex embeddings, rerank, and vector search. Cortex owns service
            credentials; this row only selects its configured connector and model.
          </p>
        </div>
        <button type="button" className={BTN_CLASS} disabled={saving || loading} onClick={save}>
          {saving ? 'Saving…' : 'Save models'}
        </button>
      </div>

      <div className="rounded-lg border border-run-queued/25 bg-run-queued/10 px-3 py-2 text-[11px] leading-relaxed text-run-queued">
        Keep embedding dimensions at <b>768</b> unless this is a planned vector migration. Changing
        provider, model, or dimensions creates a new vector space; existing embeddings are no longer
        comparable and must be rebuilt. NVIDIA/OpenRouter embedding models can default higher, so
        Kaidera OS requests the configured dimension and rejects mismatched vectors.
      </div>

      {error && (
        <p className="rounded-md bg-run-errored/12 px-3 py-2 text-xs leading-relaxed text-run-errored/90">
          {error}
        </p>
      )}
      {saved && (
        <p className="rounded-md bg-mint-500/10 px-3 py-2 text-xs leading-relaxed text-mint-300">
          {saved}
        </p>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        <CortexFormField label="Embedding connector" hint="Select a connector configured in Cortex.">
          <select
            className={FIELD_CLASS}
            value={String(form.embedding_provider || '')}
            disabled={loading || saving}
            onChange={(e) => patch({ embedding_provider: e.target.value })}
          >
            {INGESTION_PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </CortexFormField>

        <CortexFormField label="Embedding dimensions" hint="Matches the current pgvector schema/index. Default: 768.">
          <input
            className={FIELD_CLASS}
            type="number"
            min={1}
            value={String(form.embedding_dims ?? 768)}
            disabled={loading || saving}
            onChange={(e) => patch({ embedding_dims: Number(e.target.value) })}
          />
        </CortexFormField>

        <CortexFormField label="Embedding model" hint="Use the free OpenRouter NVIDIA model unless you are deliberately migrating.">
          <input
            className={FIELD_CLASS}
            list="cortex-embedding-models"
            value={String(form.embedding_model || '')}
            disabled={loading || saving}
            onChange={(e) => patch({ embedding_model: e.target.value })}
          />
          <datalist id="cortex-embedding-models">
            {EMBEDDING_MODEL_SUGGESTIONS.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </CortexFormField>

        <CortexFormField label="Rerank enabled" hint="Rerank improves top-N ordering after vector search.">
          <div className="flex h-[34px] items-center gap-3">
            <Toggle
              on={Boolean(form.rerank_enabled)}
              disabled={loading || saving}
              onToggle={(next) => patch({ rerank_enabled: next })}
              label="Rerank enabled"
            />
            <span className="text-xs text-ink-300">{form.rerank_enabled ? 'enabled' : 'disabled'}</span>
          </div>
        </CortexFormField>

        <CortexFormField label="Rerank connector" hint="Select a connector configured in Cortex.">
          <select
            className={FIELD_CLASS}
            value={String(form.rerank_provider || '')}
            disabled={loading || saving || !form.rerank_enabled}
            onChange={(e) => patch({ rerank_provider: e.target.value })}
          >
            {INGESTION_PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </CortexFormField>

        <CortexFormField label="Rerank model" hint="NVIDIA requires nv-rerank-qa-mistral-4b:1 for this endpoint.">
          <input
            className={FIELD_CLASS}
            list="cortex-rerank-models"
            value={String(form.rerank_model || '')}
            disabled={loading || saving || !form.rerank_enabled}
            onChange={(e) => patch({ rerank_model: e.target.value })}
          />
          <datalist id="cortex-rerank-models">
            {RERANK_MODEL_SUGGESTIONS.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </CortexFormField>

        <CortexFormField label="Embedding input chars" hint="Bounded per chunk before provider call.">
          <input
            className={FIELD_CLASS}
            type="number"
            min={10}
            value={String(form.embed_input_max_chars ?? 500)}
            disabled={loading || saving}
            onChange={(e) => patch({ embed_input_max_chars: Number(e.target.value) })}
          />
        </CortexFormField>

        <CortexFormField label="Rerank input chars" hint="Bounded per passage before provider call.">
          <input
            className={FIELD_CLASS}
            type="number"
            min={10}
            value={String(form.rerank_input_max_chars ?? 500)}
            disabled={loading || saving}
            onChange={(e) => patch({ rerank_input_max_chars: Number(e.target.value) })}
          />
        </CortexFormField>
      </div>
    </GlassCard>
  )
}

function embeddingBacklogTotal(data: CortexEmbeddingBacklogResult | null): number {
  if (!data) return 0
  if (typeof data.backlog?.total === 'number') return data.backlog.total
  return Object.values(data.coverage ?? {}).reduce((sum, row) => sum + Number(row.backlog || 0), 0)
}

const EMBEDDING_BACKLOG_TTL_MS = 120_000
const embeddingBacklogCache = new Map<string, { expires: number; data: CortexEmbeddingBacklogResult }>()

function resultLine(result: CortexEmbeddingBackfillResult | null): string | null {
  if (!result) return null
  if (!result.ok) return result.error || 'Embedding backfill did not start.'
  const body = result.result || {}
  if (typeof body.message === 'string') return body.message
  if (typeof body.job_id === 'string') return `embedding backfill queued: ${body.job_id}`
  const processed = body.processed ?? body.embedded ?? body.updated
  if (processed !== undefined) return `embedding backfill processed ${String(processed)} row(s)`
  return 'Embedding backfill completed.'
}

function CortexEmbeddingBackfillPanel({
  project,
  client,
  onChanged,
}: {
  project: string
  client: SettingsWriteClient
  onChanged: () => void
}) {
  const [data, setData] = useState<CortexEmbeddingBacklogResult | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [table, setTable] = useState('all')
  const [limit, setLimit] = useState(100)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<CortexEmbeddingBackfillResult | null>(null)

  const load = useCallback((signal?: AbortSignal, opts?: { force?: boolean }) => {
    if (!client.cortexEmbeddingBacklog) {
      setLoading(false)
      setLoaded(true)
      setError('The console backend does not expose embedding backlog reads in this build.')
      return Promise.resolve()
    }
    const cached = embeddingBacklogCache.get(project)
    if (!opts?.force && cached && cached.expires > Date.now()) {
      setData(cached.data)
      setLoaded(true)
      setError(cached.data.ok ? null : cached.data.error || null)
      return Promise.resolve()
    }
    setLoading(true)
    setError(null)
    return client
      .cortexEmbeddingBacklog(project, signal)
      .then((next) => {
        setData(next)
        setLoaded(true)
        embeddingBacklogCache.set(project, {
          data: next,
          expires: Date.now() + EMBEDDING_BACKLOG_TTL_MS,
        })
        if (!next.ok && next.error) setError(next.error)
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        setLoaded(true)
        setLoading(false)
      })
  }, [client, project])

  async function runBackfill(dryRun: boolean) {
    if (!client.cortexEmbeddingBackfill) {
      setError('The console backend does not expose embedding backfill writes in this build.')
      return
    }
    setRunning(true)
    setError(null)
    setResult(null)
    const request: CortexEmbeddingBackfillRequest = {
      table,
      limit: asPositiveInt(limit, 100),
      chunk_size: Math.min(asPositiveInt(limit, 100), 100),
      dry_run: dryRun,
      async_job: !dryRun,
    }
    try {
      const next = await client.cortexEmbeddingBackfill(project, request)
      setResult(next)
      if (!next.ok && next.error) setError(next.error)
      await load(undefined, { force: true })
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  const coverage = Object.entries(data?.coverage ?? {}).sort(([a], [b]) => a.localeCompare(b))
  const total = embeddingBacklogTotal(data)
  const tables = ['all', ...coverage.map(([name]) => name)]
  const line = resultLine(result)

  return (
    <GlassCard className="space-y-4 p-4" data-cortex-embedding-backfill>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-ink-100">Embedding coverage</h3>
          <p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-ink-500">
            Project-scoped vector backlog and rebuild controls. Use dry-run before rebuilding after
            changing embedding provider, model, or dimensions.
          </p>
        </div>
        <button type="button" className={BTN_CLASS} disabled={loading || running} onClick={() => load(undefined, { force: true })}>
          {loaded ? 'Refresh coverage' : 'Load coverage'}
        </button>
      </div>

      <div className="grid gap-3 md:grid-cols-[1fr_8rem_12rem]">
        <CortexFormField label="Table" hint="Use all unless you are repairing one memory class.">
          <select
            className={FIELD_CLASS}
            value={table}
            disabled={loading || running}
            onChange={(e) => setTable(e.target.value)}
          >
            {tables.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </CortexFormField>
        <CortexFormField label="Limit" hint="Rows per request.">
          <input
            className={FIELD_CLASS}
            type="number"
            min={1}
            max={500}
            value={String(limit)}
            disabled={loading || running}
            onChange={(e) => setLimit(asPositiveInt(e.target.value, 100))}
          />
        </CortexFormField>
        <div className="flex items-end gap-2">
          <button
            type="button"
            className={BTN_CLASS}
            disabled={!loaded || loading || running}
            onClick={() => runBackfill(true)}
          >
            Dry run
          </button>
          <button
            type="button"
            className={BTN_CLASS}
            disabled={!loaded || loading || running || total === 0}
            onClick={() => runBackfill(false)}
          >
            Start backfill
          </button>
        </div>
      </div>

      <div className="rounded-lg border border-glass-line bg-base-900/25">
        <div className="flex items-center justify-between border-b border-glass-line px-3 py-2">
          <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-500">
            Vector backlog
          </span>
          <span className="font-mono text-[11px] text-ink-300">
            {loading ? 'loading...' : loaded ? `${total} pending` : 'not loaded'}
          </span>
        </div>
        {!loaded ? (
          <p className="px-3 py-3 text-[11px] text-ink-500">
            Coverage is loaded on demand to avoid expensive shared Cortex scans during Settings startup.
          </p>
        ) : coverage.length === 0 ? (
          <p className="px-3 py-3 text-[11px] text-ink-500">
            {loading ? 'Loading embedding coverage...' : 'No coverage rows returned.'}
          </p>
        ) : (
          <div className="divide-y divide-glass-line">
            {coverage.map(([name, row]) => (
              <div key={name} className="grid grid-cols-[1fr_5rem_5rem_5rem] gap-2 px-3 py-2 text-[11px]">
                <span className="font-mono text-ink-200">{name}</span>
                <span className="text-right text-ink-400">{row.embedded}/{row.total}</span>
                <span className="text-right text-mint-300">{row.pct}%</span>
                <span className={cx('text-right', row.backlog > 0 ? 'text-run-queued' : 'text-ink-500')}>
                  {row.backlog} back
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {error && (
        <p className="rounded-md bg-run-errored/12 px-3 py-2 text-xs leading-relaxed text-run-errored/90">
          {error}
        </p>
      )}
      {line && (
        <p className="rounded-md bg-mint-500/10 px-3 py-2 text-xs leading-relaxed text-mint-300">
          {line}
        </p>
      )}
    </GlassCard>
  )
}

function lifecycleLabel(value: string): string {
  switch (value) {
    case 'restart_survivable':
      return 'Restart-survivable'
    case 'live_request':
      return 'Live request'
    case 'needs_reconcile':
      return 'Needs reconcile'
    case 'legacy_request_lived':
      return 'Legacy request-lived'
    default:
      return value || 'Unknown'
  }
}

function RunstateRestartPanel({
  project,
  client,
}: {
  project: string
  client: SettingsWriteClient
}) {
  const [data, setData] = useState<RunStateRestartStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    if (!client.runstateRestartStatus) return
    setLoading(true)
    setError(null)
    client
      .runstateRestartStatus(project)
      .then((res) => {
        setData(res)
        if (!res.ok) setError(res.error || 'run-state restart status is degraded.')
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [client, project])

  useEffect(() => {
    queueMicrotask(load)
  }, [load])

  if (!client.runstateRestartStatus) return null

  const counts = data?.counts
  return (
    <GlassCard className="space-y-3 p-4" data-runstate-restart-health>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-ink-100">Run-state restart health</h3>
          <p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-ink-500">
            Active durable runs by lifecycle. Detached workers survive console restarts;
            request-lived chat/approve runs are reconciled on startup when their owning console
            PID is gone.
          </p>
        </div>
        <button type="button" className={BTN_GHOST} disabled={loading} onClick={load}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <p className="rounded-md bg-run-errored/12 px-3 py-2 text-xs leading-relaxed text-run-errored/90">
          {error}
        </p>
      )}

      {counts ? (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          <CortexKV k="Active" v={<code>{counts.active}</code>} />
          <CortexKV k="Survivable" v={<code>{counts.restart_survivable}</code>} />
          <CortexKV k="Request-lived" v={<code>{counts.request_lived}</code>} />
          <CortexKV k="Needs reconcile" v={<code>{counts.needs_reconcile}</code>} />
        </div>
      ) : (
        <p className="text-xs text-ink-500">Loading restart health…</p>
      )}

      {data && data.active.length > 0 && (
        <div className="space-y-1">
          {data.active.slice(0, 5).map((run) => (
            <div
              key={run.run_id || `${run.agent}-${run.lifecycle}`}
              className="flex flex-wrap items-center gap-2 rounded-md border border-glass-line bg-base-950/25 px-3 py-2 text-[11px]"
            >
              <code className="font-mono text-ink-300">{run.run_id || 'unknown-run'}</code>
              <span className="text-ink-500">{run.agent || 'unknown-agent'}</span>
              <span
                className={cx(
                  'rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide',
                  run.restart_survivable
                    ? 'bg-mint-500/15 text-mint-300'
                    : run.needs_reconcile
                      ? 'bg-run-errored/12 text-run-errored/90'
                      : 'bg-base-700/60 text-ink-500',
                )}
              >
                {lifecycleLabel(run.lifecycle)}
              </span>
              <span className="ml-auto font-mono text-[10px] text-ink-600">
                {run.lease_owner || 'no-lease'} · pid {run.pid ?? '—'}
              </span>
            </div>
          ))}
        </div>
      )}
    </GlassCard>
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
  // The live health read-out — the CONSOLE JSON health endpoint `/cortex/health`
  // (the fix: the console now exposes a JSON health surface; the SPA used to read a
  // bare `/health` that 404'd, so it always showed "unreachable"). It returns the
  // live Cortex `get_health()` (status + surface fields) folded with connection info
  // (base_url + project). `probed` flips true only AFTER the async fetch settles, so
  // the read-out shows "checking…" until then.
  const [health, setHealth] = useState<CortexHealth | null>(null)
  const [probed, setProbed] = useState(false)

  useEffect(() => {
    const ctrl = new AbortController()
    let alive = true
    fetch(`/cortex/health?project=${encodeURIComponent(project)}`, {
      headers: { Accept: 'application/json' },
      signal: ctrl.signal,
    })
      .then(async (res) => (res.ok ? ((await res.json()) as CortexHealth) : null))
      .catch(() => null)
      .then((h) => {
        if (!alive) return
        setHealth(h)
        setProbed(true)
      })
    return () => {
      alive = false
      ctrl.abort()
    }
  }, [project])

  // Prefer the base_url the console reports (the actual Cortex API base it talks to);
  // fall back to the browser origin only if the payload didn't carry one.
  const baseUrl =
    (typeof health?.base_url === 'string' && health.base_url) ||
    (typeof window !== 'undefined' ? window.location.origin : '—')
  const repoRoot = (projectRow?.repo_root as string | null) || null
  const status = (health?.status || (probed ? 'unreachable' : 'checking')).toLowerCase()
  const statusOk = ['ok', 'healthy', 'up'].includes(status)

  return (
    <div className="space-y-4">
      <p className="px-1 text-[11px] leading-relaxed text-ink-500">
        The Cortex service this console reads. Connection, live health, ingestion model settings,
        and the <b>6-layer</b> Cortex architecture are shown below.
      </p>

      <CortexIngestionSettings client={client} onSaved={onSaved} />
      <CortexEmbeddingBackfillPanel project={project} client={client} onChanged={onSaved} />
      <RunstateRestartPanel project={project} client={client} />

      {/* connection + registry */}
      <GlassCard className="overflow-hidden p-0" data-cortex-connection>
        <div className="border-b border-glass-line px-4 py-2.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-400">
          Connection
        </div>
        <div className="divide-y divide-glass-line">
          <CortexKV k="Console base URL" v={<code>{baseUrl}</code>} />
          <CortexKV k="Project" v={<code>{project}</code>} />
          <CortexKV k="Repo root" v={<code title={repoRoot || ''}>{repoRoot || '—'}</code>} />
          <CortexKV k="Status" v={<code>{(projectRow?.status as string | null) || '—'}</code>} />
        </div>
      </GlassCard>

      {/* live health */}
      <GlassCard className="overflow-hidden p-0" data-cortex-health>
        <div className="flex items-center gap-2 border-b border-glass-line px-4 py-2.5">
          <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-400">
            Live health
          </span>
          <span
            className={cx(
              'ml-auto inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[10px] font-medium',
              statusOk
                ? 'bg-mint-500/15 text-mint-300'
                : status === 'checking'
                  ? 'bg-base-700/60 text-ink-400'
                  : 'bg-run-errored/15 text-run-errored/90',
            )}
          >
            <StatusDot status={statusOk ? 'running' : status === 'checking' ? 'queued' : 'errored'} />
            {health?.status || (probed ? 'unreachable' : 'checking…')}
          </span>
        </div>
        {probed && !health ? (
          <p className="px-4 py-3 text-[11px] leading-relaxed text-ink-500">
            Couldn’t read the console health endpoint (<code className="font-mono">/cortex/health</code>)
            in this build — it may not be live yet. Connection + registry details are shown above.
          </p>
        ) : (
          <div className="divide-y divide-glass-line">
            <CortexKV k="Surface version" v={<code>{health?.surface_version || '—'}</code>} />
            <CortexKV k="Event backend" v={<code>{health?.event_backend || '—'}</code>} />
            <CortexKV
              k="Embedding"
              v={
                <code>
                  {health?.embed_provider || '—'} / {health?.embed_model || '—'} (
                  {health?.embed_dims || '—'}d)
                </code>
              }
            />
            <CortexKV
              k="Rerank"
              v={
                health?.rerank_enabled === false ? (
                  <span className="text-ink-400">disabled</span>
                ) : (
                  <code>
                    {health?.rerank_provider || '—'} / {health?.rerank_model || '—'}
                  </code>
                )
              }
            />
            <CortexKV
              k="RLS enforced"
              v={
                health?.rls_enforced === undefined || health?.rls_enforced === null ? (
                  <code>—</code>
                ) : (
                  <span className={health.rls_enforced ? 'text-mint-300' : 'text-ink-400'}>
                    {health.rls_enforced ? 'yes' : 'no'}
                  </span>
                )
              }
            />
            {health?.error && (
              <div className="px-4 py-2 text-[11px] text-run-errored/80">
                couldn’t reach Cortex: <code className="font-mono">{String(health.error)}</code>
              </div>
            )}
          </div>
        )}
      </GlassCard>

      {/* 6-layer reference */}
      <GlassCard className="overflow-hidden p-0" data-cortex-layers>
        <div className="border-b border-glass-line px-4 py-2.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-400">
          6-layer Cortex
        </div>
        <ol className="divide-y divide-glass-line">
          {CORTEX_LAYERS.map((L) => (
            <li key={L.id} className="flex items-center gap-3 px-4 py-2">
              <span className="w-7 shrink-0 font-mono text-[11px] text-ink-500">{L.id}</span>
              <span className="min-w-0 flex-1">
                <span className="block text-[12px] font-medium text-ink-100">{L.name}</span>
                <span className="block truncate text-[10px] text-ink-500">{L.what}</span>
              </span>
              <span
                className={cx(
                  'shrink-0 rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide',
                  L.status === 'KEEP'
                    ? 'bg-base-700/60 text-ink-400'
                    : L.status === 'NEW'
                      ? 'bg-mint-500/15 text-mint-300'
                      : 'bg-run-queued/15 text-run-queued',
                )}
              >
                {L.status}
              </span>
            </li>
          ))}
        </ol>
      </GlassCard>
    </div>
  )
}

// ===========================================================================
//  The SettingsView shell — the sub-nav + the active tab.
// ===========================================================================

export function SettingsView({
  project,
  appSettings,
  systemSchema,
  projectRow,
  projects,
  loading,
  error,
  client,
  onSaved,
}: SettingsViewProps) {
  const [tab, setTab] = useState<SettingsTab>('system')

  // A project switch resets to the System tab (the canonical landing section).
  const [tabProject, setTabProject] = useState<string | null>(project)
  if (project !== tabProject) {
    setTabProject(project)
    setTab('system')
  }

  return (
    <GlassPanel className="min-w-0 flex-1">
      <header className="border-b border-glass-line px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-base font-semibold text-ink-100">Settings</h1>
        </div>
        <p className="mt-1 text-[11px] text-ink-500">
          {project ? (
            <>
              Settings for <span className="font-mono text-ink-400">{project}</span>. System is
              <span className="text-ink-400"> global</span> (every project); Workspace,
              Extensions &amp; Cortex are per-project. Project autonomy controls live on the Dashboard.
            </>
          ) : (
            <>
              <span className="text-ink-400">Global configuration</span> — applies to every project.
              System is universal; Workspace, Extensions &amp; Cortex need a project.
            </>
          )}
        </p>
        {/* the glass sub-nav — System · Workspace · Extensions · Cortex */}
        <nav
          role="tablist"
          aria-label="Settings sections"
          className="glass-soft mt-3 inline-flex flex-wrap items-center gap-0.5 rounded-xl p-1"
        >
          {TABS.map((t) => {
            const active = t.id === tab
            return (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => setTab(t.id)}
                className={cx(
                  'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                  active
                    ? 'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/40'
                    : 'text-ink-400 hover:bg-base-800/50 hover:text-ink-200',
                )}
              >
                {t.label}
              </button>
            )
          })}
        </nav>
      </header>

      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        {loading && !appSettings && !systemSchema && (
          <p className="px-1 py-2 text-xs text-ink-500">Loading settings…</p>
        )}

        {error && !appSettings && !systemSchema && (
          <p className="px-1 py-2 text-xs leading-relaxed text-run-errored/80">
            Couldn’t load settings for <span className="font-mono">{project}</span>. The backend{' '}
            <code className="font-mono">/settings/{'{project}'}/…</code> routes may not be live in
            this build.
          </p>
        )}

        {/* System is global and renders without a selected project. */}
        {tab === 'system' && (
          <SystemTab
            project={project ?? ''}
            schema={systemSchema}
            appSettings={appSettings}
            projects={projects}
            client={client}
            onSaved={onSaved}
          />
        )}

        {tab === 'workspace' &&
          (!project ? (
            <p className="px-1 py-2 text-xs text-ink-500">Select or create a project to edit its workspace.</p>
          ) : (
            <WorkspaceTab
              project={project}
              projectRow={projectRow}
              projects={projects}
              client={client}
            />
          ))}

        {tab === 'extensions' &&
          (!project ? (
            <p className="px-1 py-2 text-xs text-ink-500">Select or create a project to manage extensions.</p>
          ) : (
            <ExtensionsTab projectRow={projectRow} client={client} />
          ))}

        {tab === 'cortex' &&
          (!project ? (
            <p className="px-1 py-2 text-xs text-ink-500">Select or create a project to view its Cortex registry.</p>
          ) : (
            <CortexTab project={project} projectRow={projectRow} client={client} onSaved={onSaved} />
          ))}

      </div>
    </GlassPanel>
  )
}
