import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SkillsView } from './SkillsView'
import type { SkillsClient } from './SkillsView'
import type { SkillBindResult, SkillInstallResult, SkillRow } from '../api'

function skill(over: Partial<SkillRow> = {}): SkillRow {
  return {
    id: 's1',
    project: '*',
    skill_slug: 'web-reader',
    name: 'web-reader',
    description: "Read a website's content from the command line using curl.",
    scope: 'global',
    version: '1',
    status: 'active',
    body_ref: '.agents/skills/web-reader/SKILL.md',
    ...over,
  }
}

function fakeClient(over: Partial<SkillsClient> = {}): SkillsClient {
  return {
    installSkill: vi
      .fn<(...a: unknown[]) => Promise<SkillInstallResult>>()
      .mockResolvedValue({ ok: true, error: null, skills: [skill()] }),
    bindSkill: vi
      .fn<(...a: unknown[]) => Promise<SkillBindResult>>()
      .mockResolvedValue({ ok: true, slug: 'web-reader', subject: 'qa', error: null }),
    ...over,
  }
}

/** Default props so each test overrides only what it cares about. */
function props(over: Partial<Parameters<typeof SkillsView>[0]> = {}) {
  return {
    project: 'kaidera-os',
    skills: [skill()] as SkillRow[] | null,
    loading: false,
    error: null as Error | null,
    client: fakeClient(),
    onChanged: vi.fn(),
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SkillsView — catalogue render', () => {
  it('renders a skill row — slug, scope, version, description', () => {
    render(<SkillsView {...props()} />)
    expect(screen.getByText('web-reader')).toBeInTheDocument()
    // scope chip (lowercase) + version chip — "global" also appears as a scope <option>, so
    // assert at least one occurrence rather than uniqueness.
    expect(screen.getAllByText('global').length).toBeGreaterThan(0)
    expect(screen.getByText('v1')).toBeInTheDocument()
    expect(screen.getByText(/Read a website's content/)).toBeInTheDocument()
  })

  it('shows the catalogue counts (total / global / scoped)', () => {
    render(
      <SkillsView
        {...props({
          skills: [
            skill(),
            skill({ skill_slug: 'deploy-helper', scope: 'project', project: 'kaidera-os' }),
          ],
        })}
      />,
    )
    // "Skills" is both the heading and a StatPill label — assert the heading explicitly.
    expect(screen.getByRole('heading', { name: 'Skills' })).toBeInTheDocument()
    // the StatPill labels (the other rows have a global + a project skill → 1 global, 1 scoped)
    expect(screen.getByText('Global')).toBeInTheDocument()
    expect(screen.getByText('Scoped')).toBeInTheDocument()
  })

  it('shows an empty state for a connected-but-empty catalogue', () => {
    render(<SkillsView {...props({ skills: [] })} />)
    expect(screen.getByTestId('skills-empty')).toBeInTheDocument()
    expect(screen.getByText(/no skills installed yet/i)).toBeInTheDocument()
  })

  it('shows a loading hint before any catalogue arrives', () => {
    render(<SkillsView {...props({ skills: null, loading: true })} />)
    expect(screen.getByTestId('skills-loading')).toBeInTheDocument()
  })

  it('shows an error hint (stale-backend 404) when the catalogue fails to load', () => {
    render(<SkillsView {...props({ skills: null, error: new Error('404') })} />)
    expect(screen.getByTestId('skills-error')).toBeInTheDocument()
  })

  it('shows a no-project hint when no project is selected', () => {
    render(<SkillsView {...props({ project: null })} />)
    expect(screen.getByText(/select a project to manage its skills/i)).toBeInTheDocument()
  })
})

describe('SkillsView — install from GitHub', () => {
  it('typing a URL and clicking Install calls installSkill and refetches on success', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    const onChanged = vi.fn()
    render(<SkillsView {...props({ client, onChanged })} />)

    await user.type(
      screen.getByLabelText('Skill GitHub URL'),
      'https://github.com/org/skill-repo',
    )
    await user.click(screen.getByRole('button', { name: /^install$/i }))

    expect(client.installSkill).toHaveBeenCalledWith('kaidera-os', {
      url: 'https://github.com/org/skill-repo',
    })
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('passes the chosen scope through to installSkill', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<SkillsView {...props({ client })} />)

    await user.type(screen.getByLabelText('Skill GitHub URL'), 'https://github.com/org/s')
    await user.selectOptions(screen.getByLabelText('Skill scope'), 'project')
    await user.click(screen.getByRole('button', { name: /^install$/i }))

    expect(client.installSkill).toHaveBeenCalledWith('kaidera-os', {
      url: 'https://github.com/org/s',
      scope: 'project',
    })
  })

  it('does not call installSkill for a blank URL (the button is disabled)', () => {
    const client = fakeClient()
    render(<SkillsView {...props({ client })} />)
    expect(screen.getByRole('button', { name: /^install$/i })).toBeDisabled()
    expect(client.installSkill).not.toHaveBeenCalled()
  })

  it('surfaces a friendly install error (ok=false) without crashing', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      installSkill: vi
        .fn<(...a: unknown[]) => Promise<SkillInstallResult>>()
        .mockResolvedValue({ ok: false, error: 'The skill installer failed: bad repo', skills: [] }),
    })
    render(<SkillsView {...props({ client })} />)

    await user.type(screen.getByLabelText('Skill GitHub URL'), 'https://github.com/org/bad')
    await user.click(screen.getByRole('button', { name: /^install$/i }))

    expect(await screen.findByText(/the skill installer failed/i)).toBeInTheDocument()
  })

  it('surfaces a thrown install error (transport) without crashing', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      installSkill: vi
        .fn<(...a: unknown[]) => Promise<SkillInstallResult>>()
        .mockRejectedValue(new Error('install route 500')),
    })
    render(<SkillsView {...props({ client })} />)

    await user.type(screen.getByLabelText('Skill GitHub URL'), 'https://github.com/org/x')
    await user.click(screen.getByRole('button', { name: /^install$/i }))

    expect(await screen.findByText(/install route 500/i)).toBeInTheDocument()
  })
})

describe('SkillsView — assign to an agent/role', () => {
  it('opens the assign control and binds the skill to a role', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    const onChanged = vi.fn()
    render(<SkillsView {...props({ client, onChanged })} />)

    await user.click(screen.getByRole('button', { name: /assign to/i }))
    await user.type(screen.getByLabelText('role name'), 'qa')
    await user.click(screen.getByRole('button', { name: /^assign$/i }))

    expect(client.bindSkill).toHaveBeenCalledWith('kaidera-os', 'web-reader', {
      subject: 'qa',
      subject_kind: 'role',
    })
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('binds to a single agent when the agent kind is chosen', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<SkillsView {...props({ client })} />)

    await user.click(screen.getByRole('button', { name: /assign to/i }))
    await user.click(screen.getByRole('radio', { name: 'agent' }))
    await user.type(screen.getByLabelText('agent name'), 'ren')
    await user.click(screen.getByRole('button', { name: /^assign$/i }))

    expect(client.bindSkill).toHaveBeenCalledWith('kaidera-os', 'web-reader', {
      subject: 'ren',
      subject_kind: 'agent',
    })
  })

  it('surfaces a friendly bind error (ok=false) without crashing', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      bindSkill: vi
        .fn<(...a: unknown[]) => Promise<SkillBindResult>>()
        .mockResolvedValue({ ok: false, slug: 'web-reader', subject: 'qa', error: 'not authorised' }),
    })
    render(<SkillsView {...props({ client })} />)

    await user.click(screen.getByRole('button', { name: /assign to/i }))
    await user.type(screen.getByLabelText('role name'), 'qa')
    await user.click(screen.getByRole('button', { name: /^assign$/i }))

    expect(await screen.findByText(/not authorised/i)).toBeInTheDocument()
  })

  it('does not bind for a blank subject (the Assign button is disabled)', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<SkillsView {...props({ client })} />)
    await user.click(screen.getByRole('button', { name: /assign to/i }))
    expect(screen.getByRole('button', { name: /^assign$/i })).toBeDisabled()
    expect(client.bindSkill).not.toHaveBeenCalled()
  })
})
