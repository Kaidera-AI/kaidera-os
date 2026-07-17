import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

type ProjectRailMockProps = {
  projects: { project_key: string; display_name?: string | null }[]
  selected: string | null
}

type ProjectShellMockProps = {
  project: string | null
}

vi.mock('./features/ProjectRail', () => ({
  ProjectRail: ({ projects, selected }: ProjectRailMockProps) => (
    <section data-testid="project-rail" data-selected={selected ?? ''}>
      {projects.map((project) => (
        <button
          key={project.project_key}
          type="button"
          aria-current={selected === project.project_key ? 'true' : undefined}
        >
          {project.display_name ?? project.project_key}
        </button>
      ))}
    </section>
  ),
}))

vi.mock('./features/AgentsColumn', () => ({
  AgentsColumn: ({ project }: ProjectShellMockProps) => (
    <section data-testid="agents-column" data-project={project ?? ''} />
  ),
}))

vi.mock('./features/MainArea', () => ({
  MainArea: ({ project }: ProjectShellMockProps) => (
    <main data-testid="main-area" data-project={project ?? ''} />
  ),
}))

vi.mock('./features/WorkspaceColumn', () => ({
  WorkspaceColumn: ({ project }: ProjectShellMockProps) => (
    <aside data-testid="workspace-column" data-project={project ?? ''} />
  ),
}))

vi.mock('./features/OnboardingView', () => ({
  OnboardingView: () => <main data-testid="onboarding-view" />,
}))

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
}

function requestPath(input: RequestInfo | URL): string {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
  if (!/^https?:\/\//.test(raw)) return raw
  const url = new URL(raw)
  return `${url.pathname}${url.search}`
}

describe('App project selection', () => {
  const requests: string[] = []
  const unhandled: string[] = []
  let projectRows: Array<Record<string, unknown>>

  beforeEach(() => {
    requests.length = 0
    unhandled.length = 0
    projectRows = [
      {
        project_key: 'kaidera-os',
        display_name: 'Kaidera OS',
        status: 'active',
        agent_count: 1,
        repo_root: '/repo/kaidera-os',
      },
    ]
    window.history.replaceState(null, '', '/')
    window.location.hash = '#/stale-project/ren'

    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const path = requestPath(input)
        requests.push(path)

        if (path === '/projects') {
          return Promise.resolve(jsonResponse(projectRows))
        }
        if (path === '/console/version') return Promise.resolve(jsonResponse({ version: 'test' }))
        if (path === '/console/update-status') {
          return Promise.resolve(
            jsonResponse({
              current_version: 'test',
              check_ok: true,
              source: 'test',
              repo: '',
              update_command: '',
            }),
          )
        }
        if (path === '/whoami') {
          return Promise.resolve(jsonResponse({ name: 'Test', email: 'test@example.com', is_admin: false }))
        }
        if (path === '/agents/kaidera-os') {
          return Promise.resolve(
            jsonResponse({
              project: 'kaidera-os',
              interactive: [],
              autonomous: [],
              orchestrator: null,
              lead: 'ren',
            }),
          )
        }
        if (path === '/agents/kaidera-os/epics') {
          return Promise.resolve(
            jsonResponse({
              project: 'kaidera-os',
              epic: { mode: 'continuous', epics: [], epic_count: 0, label: 'continuous' },
              metrics: {
                active_tasks: 0,
                pending_tasks: 0,
                pending_handoffs: 0,
                events_24h: 0,
              },
            }),
          )
        }
        if (path === '/runs/kaidera-os') {
          return Promise.resolve(
            jsonResponse({ project: 'kaidera-os', active: [], active_count: 0, recent: [], recent_count: 0 }),
          )
        }
        if (path === '/dispatch/kaidera-os/board') {
          return Promise.resolve(
            jsonResponse({
              project: 'kaidera-os',
              rows: [],
              dispatch_count: 0,
              dispatch_proposed_count: 0,
              dispatch_unassigned_count: 0,
              autonomous_on: false,
              propose_mode_on: false,
              awaiting_approval_ids: [],
            }),
          )
        }
        if (path === '/dispatch/kaidera-os/activity') {
          return Promise.resolve(
            jsonResponse({
              project: 'kaidera-os',
              activity: [],
              activity_count: 0,
              waves: [],
              waves_any: false,
              loop_running: false,
              inflight: 0,
              cap: 0,
              no_orch: false,
            }),
          )
        }
        if (path === '/analytics/kaidera-os/usage') return Promise.resolve(jsonResponse({ project: 'kaidera-os' }))
        if (path === '/analytics/kaidera-os/kpis') return Promise.resolve(jsonResponse({ project: 'kaidera-os' }))
        if (path === '/settings/kaidera-os/flags') {
          return Promise.resolve(jsonResponse({ project: 'kaidera-os', autonomous: false, propose_mode: false }))
        }
        if (path === '/skills/kaidera-os') return Promise.resolve(jsonResponse({ skills: [] }))
        if (/^\/settings\/(?:_system|kaidera-os)\/app$/.test(path)) {
          return Promise.resolve(
            jsonResponse({
              project: path.includes('_system') ? '_system' : 'kaidera-os',
              settings: { cortex_default_project: 'kaidera-os' },
              store_connected: true,
            }),
          )
        }
        if (/^\/settings\/(?:_system|kaidera-os)\/system-schema$/.test(path)) {
          return Promise.resolve(jsonResponse({ project: 'kaidera-os', groups: [], store_connected: true }))
        }
        unhandled.push(path)
        return Promise.resolve(jsonResponse({ error: 'unhandled' }, { status: 404 }))
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('recovers from a stale hash project when the renamed project is available', async () => {
    render(<App />)

    await waitFor(() => {
      expect(screen.getByTestId('project-rail')).toHaveAttribute('data-selected', 'kaidera-os')
    })
    await waitFor(() => expect(requests).toContain('/agents/kaidera-os'))

    expect(window.location.hash).toMatch(/^#\/kaidera-os(?:\/|$)/)
    expect(requests.filter((path) => path.includes('stale-project'))).toEqual([])
    expect(unhandled).toEqual([])
  })

  it('shows onboarding instead of an empty dashboard on a fresh install', async () => {
    projectRows = []

    render(<App />)

    expect(await screen.findByTestId('onboarding-view')).toBeInTheDocument()
    expect(screen.queryByTestId('main-area')).not.toBeInTheDocument()
    expect(requests.some((path) => path.startsWith('/agents/'))).toBe(false)
    expect(unhandled).toEqual([])
  })

  it('replaces an unknown worker slug with the registered project lead', async () => {
    window.history.replaceState(null, '', '#/kaidera-os/oryx')

    render(<App />)

    await waitFor(() => expect(window.location.hash).toBe('#/kaidera-os/ren'))
    expect(screen.getByTestId('main-area')).toHaveAttribute('data-project', 'kaidera-os')
    expect(unhandled).toEqual([])
  })
})
