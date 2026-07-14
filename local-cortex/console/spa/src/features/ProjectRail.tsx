/**
 * ProjectRail — the LEFT column. PROJECTS ONLY (the CTO's canonical-layout law).
 *
 * It lists the projects and is the project SELECTOR. It MUST NOT list agents,
 * agent names, or settings — those live in exactly one other place each (the
 * agents column owns agents; settings is its own concern). This column's single
 * job is "which project am I looking at".
 *
 * Each row carries the project's metadata: the AGENT COUNT, a PENDING-HANDOFFS badge
 * (orange when > 0), and the `repo_root` as a title tooltip. The pending count comes
 * from the `attention` map the shell supplies (per-project pending handoffs); a null/0
 * count shows NO badge (never a fabricated 0).
 */

import { useState } from 'react'
import { BrandLockup } from '../components/Logo'
import { AppVersionBadge } from '../components/AppVersionBadge'
import { ProfileMenu } from '../components/ProfileMenu'
import { GlassPanel } from '../components/glass'
import { cx } from '../components/ui'
import { AddProjectModal, type AddProjectClient } from './RegistrationForms'
import type { Project, UpdateJob, UpdateStatus } from '../api'

/** Per-project "needs attention" datum (the pending-handoffs count the badge renders).
 * `pending` is null when unknown (e.g. /state unreachable for that project). */
export interface RailAttention {
  pending: number | null
}

/** localStorage key remembering the collapsed/expanded state of the rail across sessions. */
const RAIL_COLLAPSED_KEY = 'kaidera-os:rail-collapsed'

interface ProjectRailProps {
  projects: Project[]
  selected: string | null
  onSelect: (projectKey: string) => void
  loading: boolean
  error: Error | null
  /** {project_key → {pending}} — drives the per-row pending-handoffs badge. Optional: an absent
   * entry (or a null `pending`) renders no badge. Today the shell fills the SELECTED project's
   * pending from its dispatch board; other rows show no badge until a cross-project source lands. */
  attention?: Record<string, RailAttention>
  /** Optional registration client (feature-gap #81) — enables the "+ Add project" affordance +
   * its modal (registerProject). When absent, no add button. */
  registrationClient?: AddProjectClient
  /** Called after a successful project register — the shell refetches the project list. */
  onProjectRegistered?: () => void
  /** The console build stamp (e.g. "0.1.72"), rendered as a dedicated version line directly
   * under the brand/name box in the header. Null/undefined renders a stable "v..." placeholder. */
  version?: string | null
  updateStatus?: UpdateStatus | null
  updateJob?: UpdateJob | null
  canManageUpdates?: boolean
  onApplyUpdate?: () => Promise<unknown> | unknown
  /** Show an account view in the main area (Profile/Users) — threaded into the bottom ProfileMenu. */
  onNavigateView?: (view: 'profile' | 'users') => void
}

function projectLabel(p: Project): string {
  return p.display_name?.trim() || p.project_key
}

/** The badge label for a pending count (capped at 999+ so a huge queue doesn't blow the chip). */
function pendingLabel(n: number): string {
  return n < 1000 ? String(n) : '999+'
}

function ProjectRow({
  project,
  active,
  pending,
  onSelect,
  collapsed,
}: {
  project: Project
  active: boolean
  pending: number | null
  onSelect: (key: string) => void
  collapsed?: boolean
}) {
  const label = projectLabel(project)
  const count = typeof project.agent_count === 'number' ? project.agent_count : null
  const showBadge = pending != null && pending > 0
  // The repo_root is the tooltip (the legacy rail's `title="{{ p.repo_root }}"`); fall back to
  // the project key so the row always has a meaningful hover.
  const tooltip = project.repo_root?.trim() || label

  // Collapsed rail: an icon-only dot row (tooltip carries the name) that still selects the project.
  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => onSelect(project.project_key)}
        aria-current={active ? 'true' : undefined}
        title={`${label}${showBadge ? ` — ${pending} pending` : ''}`}
        className={cx(
          'group relative flex w-full items-center justify-center rounded-lg py-2 transition-colors',
          active ? 'bg-base-800/70 ring-1 ring-mint-400/40' : 'hover:bg-base-800/40',
        )}
      >
        <span
          className={cx(
            'h-2 w-2 rounded-full transition-colors',
            active ? 'bg-mint-400 shadow-[0_0_8px_rgba(67,224,182,0.7)]' : 'bg-ink-500 group-hover:bg-ink-400',
          )}
        />
        {showBadge && (
          <span className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-run-queued" />
        )}
      </button>
    )
  }

  return (
    <button
      type="button"
      onClick={() => onSelect(project.project_key)}
      aria-current={active ? 'true' : undefined}
      title={tooltip}
      className={cx(
        'group flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left transition-colors',
        active ? 'bg-base-800/70 ring-1 ring-mint-400/40' : 'hover:bg-base-800/40',
      )}
    >
      <span
        className={cx(
          'mt-1 h-1.5 w-1.5 shrink-0 self-start rounded-full transition-colors',
          active
            ? 'bg-mint-400 shadow-[0_0_8px_rgba(67,224,182,0.7)]'
            : 'bg-ink-500 group-hover:bg-ink-400',
        )}
      />

      <span className="min-w-0 flex-1">
        <span
          className={cx(
            'block truncate text-sm font-medium',
            active ? 'text-ink-100' : 'text-ink-300',
          )}
        >
          {label}
        </span>
        {/* The meta line: N agents. */}
        {count != null && (
          <span className="mt-0.5 flex items-center gap-1.5 text-[10px] text-ink-500">
            <span>
              {count} worker{count === 1 ? '' : 's'}
            </span>
          </span>
        )}
      </span>

      {/* The pending-handoffs badge — orange (the warn tone) when the project has pending work. */}
      {showBadge && (
        <span
          title={`${pending} pending handoffs`}
          className="shrink-0 rounded-full bg-run-queued/15 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-run-queued"
        >
          {pendingLabel(pending)}
        </span>
      )}
    </button>
  )
}

export function ProjectRail({
  projects,
  selected,
  onSelect,
  loading,
  error,
  attention,
  registrationClient,
  onProjectRegistered,
  version,
  updateStatus,
  updateJob,
  canManageUpdates,
  onApplyUpdate,
  onNavigateView,
}: ProjectRailProps) {
  const [addOpen, setAddOpen] = useState(false)
  // Collapsible rail (CTO request) — narrow icon-strip mode, remembered across sessions.
  const [collapsed, setCollapsed] = useState(
    () => typeof localStorage !== 'undefined' && localStorage.getItem(RAIL_COLLAPSED_KEY) === '1',
  )
  const toggleCollapsed = () =>
    setCollapsed((c) => {
      const next = !c
      try {
        localStorage.setItem(RAIL_COLLAPSED_KEY, next ? '1' : '0')
      } catch {
        /* private mode / quota — non-fatal, the toggle still works for this session */
      }
      return next
    })

  const ToggleButton = (
    <button
      type="button"
      onClick={toggleCollapsed}
      title={collapsed ? 'Expand projects rail' : 'Collapse projects rail'}
      aria-label={collapsed ? 'Expand projects rail' : 'Collapse projects rail'}
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-ink-400 transition-colors hover:bg-base-800/60 hover:text-ink-200"
    >
      <span aria-hidden="true" className="text-sm leading-none">{collapsed ? '»' : '«'}</span>
    </button>
  )

  return (
    <GlassPanel
      as="aside"
      className={cx(
        'shrink-0 transition-[width] max-lg:order-2 max-lg:h-[22rem] max-lg:w-full',
        collapsed ? 'w-14' : 'w-60',
      )}
    >
      <header
        className={cx(
          'border-b border-glass-line',
          collapsed ? 'flex flex-col items-center gap-2 px-2 py-3' : 'px-4 py-4',
        )}
      >
        {collapsed ? (
          ToggleButton
        ) : (
          <>
            <div className="flex items-start justify-between">
              <BrandLockup />
              {ToggleButton}
            </div>
            {/* Dedicated build-stamp slot directly under the name box (replaces the old buried
                bottom-left fixed badge that overlapped the live console text). */}
            <AppVersionBadge
              version={version}
              updateStatus={updateStatus}
              updateJob={updateJob}
              canManageUpdates={canManageUpdates}
              onApplyUpdate={onApplyUpdate}
            />
          </>
        )}
      </header>

      {!collapsed && (
        <div className="flex items-center justify-between px-4 pb-2 pt-4">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-500">
            Projects
          </h2>
          {/* "+ Add project" — opens the register modal (feature-gap #81). */}
          {registrationClient && (
            <button
              type="button"
              onClick={() => setAddOpen(true)}
              title="Register a new project"
              className="flex items-center gap-0.5 rounded-md px-1.5 py-0.5 text-[10px] font-semibold text-mint-300 transition-colors hover:bg-base-800/60 hover:text-mint-200"
            >
              <span aria-hidden="true" className="text-[12px] leading-none">+</span>
              Add
            </button>
          )}
        </div>
      )}

      <nav className={cx('flex-1 space-y-1 overflow-y-auto pb-3', collapsed ? 'px-1.5' : 'px-2')}>
        {/* Text status lines only in expanded mode (no room when collapsed). */}
        {!collapsed && loading && projects.length === 0 && (
          <p className="px-3 py-2 text-xs text-ink-500">Loading projects…</p>
        )}

        {!collapsed && error && projects.length === 0 && (
          <p className="px-3 py-2 text-xs leading-relaxed text-run-errored/80">
            Couldn’t load <code className="font-mono">/projects</code>. The backend
            JSON route may not be live in this build.
          </p>
        )}

        {!collapsed && !loading && !error && projects.length === 0 && (
          <p className="px-3 py-2 text-xs text-ink-500">No active projects.</p>
        )}

        {projects.map((p) => (
          <ProjectRow
            key={p.project_key}
            project={p}
            active={p.project_key === selected}
            pending={attention?.[p.project_key]?.pending ?? null}
            onSelect={onSelect}
            collapsed={collapsed}
          />
        ))}
      </nav>

      {/* Bottom-of-rail account control — hidden when collapsed (expand to reach Profile/Logout). */}
      {!collapsed && <ProfileMenu onNavigateView={onNavigateView} />}

      {/* The "+ Add project" register modal (feature-gap #81). */}
      {registrationClient && (
        <AddProjectModal
          open={addOpen}
          onClose={() => setAddOpen(false)}
          client={registrationClient}
          onDone={() => onProjectRegistered?.()}
        />
      )}
    </GlassPanel>
  )
}
