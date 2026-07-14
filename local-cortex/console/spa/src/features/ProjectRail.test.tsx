import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProjectRail } from './ProjectRail'
import type { Project } from '../api'

function project(over: Partial<Project> = {}): Project {
  return {
    project_key: 'demo',
    display_name: 'Demo',
    status: 'active',
    agent_count: 3,
    repo_root: '/abs/path/to/demo',
    ...over,
  }
}

const noop = () => {}

describe('ProjectRail', () => {
  it('renders the agent count per row and NO project-hex chip', () => {
    render(
      <ProjectRail projects={[project({ project_hex: '5872' })]} selected="demo" onSelect={noop} loading={false} error={null} />,
    )
    // the project hex is GONE from the new identity model — no `:<hex>` chip renders,
    // even if the (now-dead) field is supplied.
    expect(screen.queryByText(/:5872/)).not.toBeInTheDocument()
    expect(screen.queryByText(/:\?\?\?\?/)).not.toBeInTheDocument()
    // the agent count is still rendered (pluralised).
    expect(screen.getByText(/3 workers/)).toBeInTheDocument()
  })

  it('singularises the agent-count label for a one-agent project', () => {
    render(
      <ProjectRail
        projects={[project({ agent_count: 1 })]}
        selected="demo"
        onSelect={noop}
        loading={false}
        error={null}
      />,
    )
    expect(screen.getByText(/1 worker\b/)).toBeInTheDocument()
  })

  it('exposes the repo_root as a title tooltip on the row', () => {
    render(
      <ProjectRail projects={[project()]} selected="demo" onSelect={noop} loading={false} error={null} />,
    )
    const row = screen.getByRole('button', { name: /demo/i })
    expect(row).toHaveAttribute('title', expect.stringContaining('/abs/path/to/demo'))
  })

  it('renders an orange pending-handoffs badge when the project has pending work', () => {
    render(
      <ProjectRail
        projects={[project()]}
        selected="demo"
        onSelect={noop}
        loading={false}
        error={null}
        attention={{ demo: { pending: 4 } }}
      />,
    )
    const badge = screen.getByTitle(/4 pending handoffs/i)
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveTextContent('4')
  })

  it('omits the pending badge when there is no pending work (zero or unknown)', () => {
    const { rerender } = render(
      <ProjectRail
        projects={[project()]}
        selected="demo"
        onSelect={noop}
        loading={false}
        error={null}
        attention={{ demo: { pending: 0 } }}
      />,
    )
    expect(screen.queryByTitle(/pending handoffs/i)).not.toBeInTheDocument()

    // a null (unknown — e.g. /state unreachable) also shows no badge (never a fabricated 0).
    rerender(
      <ProjectRail
        projects={[project()]}
        selected="demo"
        onSelect={noop}
        loading={false}
        error={null}
        attention={{ demo: { pending: null } }}
      />,
    )
    expect(screen.queryByTitle(/pending handoffs/i)).not.toBeInTheDocument()
  })

  it('caps the pending badge label at 999+', () => {
    render(
      <ProjectRail
        projects={[project()]}
        selected="demo"
        onSelect={noop}
        loading={false}
        error={null}
        attention={{ demo: { pending: 1500 } }}
      />,
    )
    expect(screen.getByTitle(/1500 pending handoffs/i)).toHaveTextContent('999+')
  })

  it('selects a project on click', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(
      <ProjectRail
        projects={[project(), project({ project_key: 'other', display_name: 'Other' })]}
        selected="demo"
        onSelect={onSelect}
        loading={false}
        error={null}
      />,
    )
    await user.click(screen.getByRole('button', { name: /other/i }))
    expect(onSelect).toHaveBeenCalledWith('other')
  })

  it('marks the selected project as current', () => {
    render(
      <ProjectRail
        projects={[project(), project({ project_key: 'other', display_name: 'Other' })]}
        selected="other"
        onSelect={noop}
        loading={false}
        error={null}
      />,
    )
    const current = screen.getByRole('button', { name: /other/i })
    expect(current).toHaveAttribute('aria-current', 'true')
  })

  it('falls back to the project key for a missing display name and never renders a hex chip', () => {
    render(
      <ProjectRail
        projects={[project({ display_name: null })]}
        selected="demo"
        onSelect={noop}
        loading={false}
        error={null}
      />,
    )
    const row = screen.getByRole('button', { name: /demo/i })
    // the key stands in for the missing display name …
    expect(within(row).getByText('demo')).toBeInTheDocument()
    // … and there is no project-hex chip at all (the new identity model dropped it).
    expect(within(row).queryByText(/:null/)).not.toBeInTheDocument()
    expect(within(row).queryByText(/:\?\?\?\?/)).not.toBeInTheDocument()
  })

  it('shows a loading hint before projects arrive', () => {
    render(<ProjectRail projects={[]} selected={null} onSelect={noop} loading error={null} />)
    expect(screen.getByText(/loading projects/i)).toBeInTheDocument()
  })

  it('shows an error hint when the projects route fails', () => {
    render(
      <ProjectRail projects={[]} selected={null} onSelect={noop} loading={false} error={new Error('404')} />,
    )
    expect(screen.getByText(/couldn’t load/i)).toBeInTheDocument()
  })

  it('shows an empty state when there are no active projects', () => {
    render(<ProjectRail projects={[]} selected={null} onSelect={noop} loading={false} error={null} />)
    expect(screen.getByText(/no active projects/i)).toBeInTheDocument()
  })

  // -- "+ Add project" affordance (feature-gap #81) --------------------------
  it('shows "+ Add" only with a registration client + opens the add-project modal', async () => {
    const user = userEvent.setup()
    const registrationClient = { registerProject: vi.fn() }
    render(
      <ProjectRail
        projects={[project()]}
        selected={null}
        onSelect={noop}
        loading={false}
        error={null}
        registrationClient={registrationClient}
        onProjectRegistered={vi.fn()}
      />,
    )
    await user.click(screen.getByRole('button', { name: /^add$/i }))
    expect(await screen.findByRole('dialog', { name: /add project/i })).toBeInTheDocument()
    expect(screen.getByLabelText('Project key')).toBeInTheDocument()
  })

  it('hides "+ Add" without a registration client', () => {
    render(<ProjectRail projects={[project()]} selected={null} onSelect={noop} loading={false} error={null} />)
    expect(screen.queryByRole('button', { name: /^add$/i })).not.toBeInTheDocument()
  })
})
