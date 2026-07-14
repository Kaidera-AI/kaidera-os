/**
 * SkillsView — a PROJECT-LEVEL main-area view: the skills CATALOGUE, INTERACTIVE.
 *
 * The installed skills for the project (the global skills + this project's own), each a row
 * with its slug · scope · version · description. Reached via the main-area switcher (… ·
 * History · Graph · Explain · Skills · Settings) — it does NOT live in a column, so it never
 * repeats the agents/metrics the 2nd column owns. Mirrors the DispatchView shape: a read
 * `Resource` (the catalogue) PLUS a write `client` for the two actions the SPA was missing:
 *
 *   1. Install from GitHub — a URL input + an optional scope select + an Install button →
 *      client.installSkill({url, scope?}). The backend shells out to `cortex-skill install`
 *      (clone + SKILL.md parse + register) and returns the refreshed catalogue; on success
 *      the view calls onChanged (the shell's resource refetch) so the new skill appears.
 *   2. Assign per skill — a per-row "Assign to…" control (an agent/role kind toggle + a
 *      subject input + Assign) → client.bindSkill(slug, {subject, subject_kind}). Refetch on
 *      success (a bind doesn't change the catalogue rows, but we refresh for consistency).
 *
 * Writes go through the injected `client` (the `api` object satisfies it structurally; tests
 * pass a fake). On a successful write the view calls `onChanged` (the shell's skills refetch)
 * — REFETCH-ON-SUCCESS, the simplest correct sync. Graceful-degrade rides through everywhere
 * — a stale-backend 404 / down Cortex yields a hint, never a crash; an `ok=false` install/bind
 * result surfaces its friendly `error` inline.
 */

import { useState } from 'react'
import { GlassPanel, GlassCard, StatPill } from '../components/glass'
import { cx } from '../components/ui'
import type { SkillBindResult, SkillInstallResult, SkillRow } from '../api'

/**
 * The WRITE surface the view drives. The concrete `api` object satisfies this structurally
 * (so the shell passes `api`); tests pass a fake that records calls.
 *   - installSkill → install from a GitHub URL (the backend shells out to `cortex-skill`).
 *   - bindSkill    → assign a skill to an agent/role.
 */
export interface SkillsClient {
  installSkill: (
    project: string,
    body: { url: string; scope?: string },
  ) => Promise<SkillInstallResult>
  bindSkill: (
    project: string,
    slug: string,
    body: { subject: string; subject_kind?: string },
  ) => Promise<SkillBindResult>
}

interface SkillsViewProps {
  project: string | null
  /** The skills catalogue (global + this project's). Null while loading. */
  skills: SkillRow[] | null
  loading: boolean
  error: Error | null
  /** The write client (the `api` object) — install + bind. */
  client: SkillsClient
  /** Called after any successful write — the shell refetches the catalogue. */
  onChanged: () => void
}

// Scope → a chip tone. Global reads "reaches everyone" (mint); project/agent are scoped (cool).
const SCOPE_CHIP: Record<string, string> = {
  global: 'bg-mint-500/15 text-mint-300',
  project: 'bg-run-queued/15 text-run-queued',
  agent: 'bg-base-700/60 text-ink-300',
}

function scopeChip(scope: string | undefined): string {
  return SCOPE_CHIP[(scope ?? '').toLowerCase()] ?? 'bg-base-700/60 text-ink-400'
}

const BTN =
  'inline-flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-semibold ' +
  'transition-colors bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/30 hover:bg-mint-500/25 ' +
  'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-mint-500/15'

const INPUT =
  'min-w-0 flex-1 rounded-md border border-glass-line bg-base-900/50 px-2.5 py-1.5 text-[12px] ' +
  'text-ink-100 placeholder:text-ink-600 focus:border-mint-400/40 focus:outline-none'

/** The "Install from GitHub" bar — a URL input + a scope select + the Install button. */
function InstallBar({
  disabled,
  onInstall,
}: {
  disabled: boolean
  onInstall: (url: string, scope: string) => void
}) {
  const [url, setUrl] = useState('')
  const [scope, setScope] = useState('')
  const trimmed = url.trim()

  function submit() {
    if (!trimmed || disabled) return
    onInstall(trimmed, scope)
    setUrl('')
  }

  return (
    <GlassCard className="space-y-2 px-4 py-3">
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-400">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-3.5 w-3.5" aria-hidden="true">
          <path d="M12 5v14M5 12h14" />
        </svg>
        Install a skill
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="url"
          inputMode="url"
          aria-label="Skill GitHub URL"
          placeholder="https://github.com/org/skill-repo  (or a local path)"
          className={INPUT}
          value={url}
          disabled={disabled}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
          }}
        />
        <label className="sr-only" htmlFor="skill-install-scope">
          Skill scope
        </label>
        <select
          id="skill-install-scope"
          aria-label="Skill scope"
          className="shrink-0 rounded-md border border-glass-line bg-base-900/50 px-2 py-1.5 text-[12px] text-ink-200 focus:border-mint-400/40 focus:outline-none"
          value={scope}
          disabled={disabled}
          onChange={(e) => setScope(e.target.value)}
        >
          <option value="">scope: auto</option>
          <option value="global">global</option>
          <option value="project">project</option>
          <option value="agent">agent</option>
        </select>
        <button
          type="button"
          className={BTN}
          disabled={!trimmed || disabled}
          onClick={submit}
          title="Clone + register the skill from this URL"
        >
          {disabled ? 'Installing…' : 'Install'}
        </button>
      </div>
      <p className="text-[10px] leading-relaxed text-ink-600">
        Clones the repo, parses its <code className="text-ink-500">SKILL.md</code>, and registers
        it. Global skills reach every agent at boot; project/agent skills must be assigned below.
      </p>
    </GlassCard>
  )
}

/** One skill card — the catalogue row (slug · scope · version · description) + an Assign control. */
function SkillCard({
  row,
  project,
  client,
  onChanged,
}: {
  row: SkillRow
  project: string
  client: SkillsClient
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState<'role' | 'agent'>('role')
  const [subject, setSubject] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [doneMsg, setDoneMsg] = useState<string | null>(null)
  const status = (row.status ?? 'active').toLowerCase()
  const inactive = status !== 'active'
  const trimmed = subject.trim()

  async function assign() {
    if (!trimmed || saving) return
    setSaving(true)
    setError(null)
    setDoneMsg(null)
    try {
      const res = await client.bindSkill(project, row.skill_slug, {
        subject: trimmed,
        subject_kind: kind,
      })
      if (res.ok) {
        setDoneMsg(`Assigned to ${kind} “${trimmed}”.`)
        setSubject('')
        onChanged()
      } else {
        setError(res.error ?? 'Could not assign the skill.')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <GlassCard className="px-4 py-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          {/* slug + scope + version line */}
          <div className="flex flex-wrap items-center gap-2">
            <code className="truncate font-mono text-[13px] font-semibold text-ink-100">
              {row.skill_slug}
            </code>
            <span
              className={cx(
                'rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide',
                scopeChip(row.scope),
              )}
            >
              {row.scope || 'scope?'}
            </span>
            {row.version && (
              <span className="rounded bg-base-700/50 px-1.5 py-0.5 text-[10px] font-medium text-ink-400">
                v{row.version}
              </span>
            )}
            {inactive && (
              <span className="rounded bg-run-errored/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-run-errored/90">
                {status}
              </span>
            )}
          </div>

          {/* description */}
          <p className="mt-1.5 text-[12.5px] leading-snug text-ink-300">
            {row.description || row.name || <span className="text-ink-600">(no description)</span>}
          </p>
        </div>

        {/* Assign toggle (opens the inline assign control) */}
        <button
          type="button"
          className={BTN}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          title="Assign this skill to an agent or role"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-3 w-3" aria-hidden="true">
            <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 7a4 4 0 1 0 0 0M19 8v6M22 11h-6" />
          </svg>
          Assign to…
        </button>
      </div>

      {/* inline assign control (kind toggle + subject input + Assign) */}
      {open && (
        <div className="mt-2 space-y-2 rounded-lg border border-glass-line bg-base-900/40 px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <div role="radiogroup" aria-label="Assign target kind" className="inline-flex overflow-hidden rounded-md ring-1 ring-glass-line">
              {(['role', 'agent'] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  role="radio"
                  aria-checked={kind === k}
                  onClick={() => setKind(k)}
                  className={cx(
                    'px-2.5 py-1.5 text-[11px] font-medium capitalize transition-colors',
                    kind === k ? 'bg-mint-500/20 text-mint-200' : 'text-ink-400 hover:bg-base-800/60',
                  )}
                >
                  {k}
                </button>
              ))}
            </div>
            <input
              type="text"
              aria-label={`${kind} name`}
              placeholder={kind === 'agent' ? 'agent name (e.g. lead)' : 'role (e.g. qa)'}
              className={INPUT}
              value={subject}
              disabled={saving}
              onChange={(e) => setSubject(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') assign()
              }}
            />
            <button
              type="button"
              className={BTN}
              disabled={!trimmed || saving}
              onClick={assign}
              title={`Deliver ${row.skill_slug} to this ${kind}`}
            >
              {saving ? 'Assigning…' : 'Assign'}
            </button>
          </div>
          {error && (
            <p className="rounded-md bg-run-errored/12 px-2.5 py-1.5 text-[11px] leading-relaxed text-run-errored/90">
              {error}
            </p>
          )}
          {doneMsg && (
            <p className="rounded-md bg-mint-500/10 px-2.5 py-1.5 text-[11px] leading-relaxed text-mint-300">
              {doneMsg}
            </p>
          )}
        </div>
      )}
    </GlassCard>
  )
}

export function SkillsView({ project, skills, loading, error, client, onChanged }: SkillsViewProps) {
  const rows = skills ?? []
  const globalCount = rows.filter((r) => (r.scope ?? '').toLowerCase() === 'global').length
  const projectCount = rows.length - globalCount

  // Install write state (a shared in-flight + error/result line for the install bar).
  const [installing, setInstalling] = useState(false)
  const [installError, setInstallError] = useState<string | null>(null)
  const [installDone, setInstallDone] = useState<string | null>(null)

  async function install(url: string, scope: string) {
    if (!project) return
    setInstalling(true)
    setInstallError(null)
    setInstallDone(null)
    try {
      const res = await client.installSkill(project, scope ? { url, scope } : { url })
      if (res.ok) {
        setInstallDone('Skill installed.')
        onChanged()
      } else {
        setInstallError(res.error ?? 'Could not install the skill.')
      }
    } catch (e) {
      setInstallError(e instanceof Error ? e.message : String(e))
    } finally {
      setInstalling(false)
    }
  }

  if (!project) {
    return (
      <GlassPanel className="flex-1">
        <div className="flex h-full items-center justify-center p-10">
          <p className="text-sm text-ink-500">Select a project to manage its skills.</p>
        </div>
      </GlassPanel>
    )
  }

  return (
    <GlassPanel className="min-w-0 flex-1">
      {/* Header — title + the catalogue counts. */}
      <header className="border-b border-glass-line px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-base font-semibold text-ink-100">Skills</h1>
            <p className="text-[11px] text-ink-500">
              Browse · install from GitHub · assign to an agent or role
            </p>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-3 gap-2">
          <StatPill label="Skills" value={rows.length} tone={rows.length > 0 ? 'mint' : 'muted'} />
          <StatPill label="Global" value={globalCount} tone={globalCount > 0 ? 'default' : 'muted'} title="Reach every agent at boot" />
          <StatPill label="Scoped" value={projectCount} tone={projectCount > 0 ? 'default' : 'muted'} title="Project/agent skills — assign to deliver" />
        </div>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {/* Install bar (always available with a selected project). */}
        <InstallBar disabled={installing} onInstall={install} />
        {installError && (
          <p className="rounded-md bg-run-errored/12 px-3 py-2 text-[11px] leading-relaxed text-run-errored/90">
            Couldn’t install — {installError}
          </p>
        )}
        {installDone && (
          <p className="rounded-md bg-mint-500/10 px-3 py-2 text-[11px] leading-relaxed text-mint-300">
            {installDone}
          </p>
        )}

        {loading && !skills && (
          <p data-testid="skills-loading" className="px-1 py-2 text-xs text-ink-500">
            Loading the skills catalogue…
          </p>
        )}

        {error && !skills && (
          <p data-testid="skills-error" className="px-1 py-2 text-xs leading-relaxed text-run-errored/80">
            Couldn’t load the skills for <span className="font-mono">{project}</span>. The backend{' '}
            <code className="font-mono">/skills/{'{project}'}</code> route may not be live in this build.
          </p>
        )}

        {/* The catalogue. */}
        {skills && rows.length === 0 && (
          <div data-testid="skills-empty" className="flex items-center justify-center p-10">
            <p className="text-sm text-ink-500">
              No skills installed yet. Install one from a GitHub URL above.
            </p>
          </div>
        )}

        {rows.length > 0 && (
          <div className="space-y-2">
            {rows.map((row) => (
              <SkillCard
                key={row.skill_slug}
                row={row}
                project={project}
                client={client}
                onChanged={onChanged}
              />
            ))}
          </div>
        )}
      </div>
    </GlassPanel>
  )
}
