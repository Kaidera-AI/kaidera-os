import { useState } from 'react'

import type { ProvidersConfig } from '../api/types'
import { AddProjectModal, type AddProjectClient } from './RegistrationForms'
import { ConfiguredProvidersPanel, type SettingsWriteClient } from './SettingsView'

/**
 * The first-run STARTING POINT. Shown (by App) whenever there are zero projects, so a fresh
 * install opens on a single visible linear path instead of an empty dashboard:
 *
 *   ① Connect access  — add a provider API key (UNIVERSAL: applies to every project; no project
 *      needed, the keyless `_system` scope). Reuses the real ConfiguredProvidersPanel.
 *   ② Create your first project — name + workspace folder; seeds the "lead" worker.
 *   ③ Meet & name your lead — chat with the lead worker, give it a real name (in the agent pane
 *      after create), and it helps build the rest.
 *
 * Prop-driven, no business logic of its own — it composes the existing provider panel + project
 * modal + the shell's refetch/selection callbacks, so it stays portable (the Marketing app + the
 * platform reuse the same cold-start).
 */
interface OnboardingViewProps {
  /** The global (`_system`) provider config — drives Step 1's "key set" status. */
  providersConfig: ProvidersConfig | null
  /** Writes provider keys (Step 1 panel) — the same client SettingsView uses. */
  settingsClient: SettingsWriteClient
  /** Registers the project (Step 2 modal). `api` satisfies this. */
  registrationClient: AddProjectClient
  /** Called after a provider key is saved — the shell refetches providersConfig so Step 1 flips
   *  to ✓ and Step 2 unlocks (CRITICAL: without this the wizard never advances). */
  onSettingsSaved: () => void
  /** Called after a project is registered — the shell refetches projects (which auto-selects the
   *  new one + dismisses this view, landing the operator on the seeded "lead" worker). */
  onProjectCreated: () => void
}

function StepHeader({ n, title, done, locked }: { n: number; title: string; done?: boolean; locked?: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <div
        className={
          'flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ' +
          (done
            ? 'bg-run-ok/20 text-run-ok'
            : locked
              ? 'bg-base-700/60 text-ink-600'
              : 'bg-accent/20 text-accent')
        }
      >
        {done ? '✓' : n}
      </div>
      <h2 className={'text-sm font-semibold ' + (locked ? 'text-ink-500' : 'text-ink-100')}>{title}</h2>
    </div>
  )
}

export function OnboardingView({
  providersConfig,
  settingsClient,
  registrationClient,
  onSettingsSaved,
  onProjectCreated,
}: OnboardingViewProps) {
  const [addOpen, setAddOpen] = useState(false)
  const hasKey = (providersConfig?.providers ?? []).some((p) => p.key_is_set)

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-6 py-8">
      <div className="mx-auto max-w-2xl space-y-6">
        <header className="space-y-1">
          <h1 className="text-lg font-semibold text-ink-100">Get started</h1>
          <p className="text-xs leading-relaxed text-ink-500">
            Three steps to a working AI-worker team. Your access is configured <em>once</em> and
            applies to every project you create.
          </p>
        </header>

        {/* ① Connect access — universal provider key (no project needed) */}
        <section className="space-y-3 rounded-xl border border-glass-line bg-base-900/40 p-4">
          <StepHeader n={1} title="Connect access" done={hasKey} />
          <p className="text-[11px] leading-relaxed text-ink-500">
            Add at least one provider API key (e.g. Fireworks or Ollama Cloud) — the URL is already
            built in. This is <span className="text-ink-400">universal</span>: it applies to every
            project. Paste your key, Save, then <span className="text-ink-400">Test</span> to
            confirm it works.
          </p>
          <ConfiguredProvidersPanel
            project=""
            config={providersConfig}
            client={settingsClient}
            onSaved={onSettingsSaved}
          />
        </section>

        {/* ② Create your first project */}
        <section
          className={
            'space-y-3 rounded-xl border border-glass-line p-4 ' +
            (hasKey ? 'bg-base-900/40' : 'bg-base-900/20 opacity-60')
          }
        >
          <StepHeader n={2} title="Create your first project" locked={!hasKey} />
          <p className="text-[11px] leading-relaxed text-ink-500">
            Give it a name, a one-line <span className="text-ink-400">scope</span>, and a workspace
            folder — and name your first AI worker. We seed that lead with a persona built from your
            scope, ready to chat.
          </p>
          <button
            type="button"
            disabled={!hasKey}
            onClick={() => setAddOpen(true)}
            className={
              'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ' +
              (hasKey
                ? 'bg-accent/20 text-accent hover:bg-accent/30'
                : 'cursor-not-allowed bg-base-700/40 text-ink-600')
            }
          >
            {hasKey ? 'Create your first project' : 'Add a provider key first ↑'}
          </button>
        </section>

        {/* ③ Meet & name your lead (happens in the agent pane after the project is created) */}
        <section className="space-y-2 rounded-xl border border-glass-line bg-base-900/20 p-4 opacity-70">
          <StepHeader n={3} title="Meet your lead worker" locked />
          <p className="text-[11px] leading-relaxed text-ink-500">
            Once your project exists you&rsquo;ll land on your named lead worker. Chat with it — from
            your project scope and that conversation it shapes its own role and skills, and helps you
            build out the rest of the team.
          </p>
        </section>
      </div>

      <AddProjectModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        client={registrationClient}
        onDone={() => {
          setAddOpen(false)
          onProjectCreated()
        }}
      />
    </div>
  )
}
