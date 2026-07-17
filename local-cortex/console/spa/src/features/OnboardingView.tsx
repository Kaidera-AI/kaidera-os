import { useState } from 'react'

import { AddProjectModal, type AddProjectClient } from './RegistrationForms'

interface OnboardingViewProps {
  registrationClient: AddProjectClient
  onProjectCreated: () => void
}

function StepHeader({
  n,
  title,
  locked,
}: {
  n: number
  title: string
  locked?: boolean
}) {
  return (
    <div className="flex items-center gap-3">
      <div
        className={
          'flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ' +
          (locked ? 'bg-base-700/60 text-ink-600' : 'bg-accent/20 text-accent')
        }
      >
        {n}
      </div>
      <h2 className={'text-sm font-semibold ' + (locked ? 'text-ink-500' : 'text-ink-100')}>
        {title}
      </h2>
    </div>
  )
}

export function OnboardingView({
  registrationClient,
  onProjectCreated,
}: OnboardingViewProps) {
  const [addOpen, setAddOpen] = useState(false)

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-6 py-8">
      <div className="mx-auto max-w-2xl space-y-6">
        <header className="space-y-1">
          <h1 className="text-lg font-semibold text-ink-100">Get started</h1>
          <p className="text-xs leading-relaxed text-ink-500">
            Create a project to begin working with your lead.
          </p>
        </header>

        <section className="space-y-3 rounded-xl border border-glass-line bg-base-900/40 p-4">
          <StepHeader n={1} title="Create your first project" />
          <p className="text-[11px] leading-relaxed text-ink-500">
            Set the project scope, workspace folder, and lead worker.
          </p>
          <button
            type="button"
            onClick={() => setAddOpen(true)}
            className="rounded-lg bg-accent/20 px-3 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/30"
          >
            Create project
          </button>
        </section>

        <section className="space-y-2 rounded-xl border border-glass-line bg-base-900/20 p-4 opacity-70">
          <StepHeader n={2} title="Meet your lead worker" locked />
          <p className="text-[11px] leading-relaxed text-ink-500">
            Your project opens on its lead worker after creation.
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
