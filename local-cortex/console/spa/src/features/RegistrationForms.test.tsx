import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AddAgentModal, AddProjectModal, DeregisterAgentModal } from './RegistrationForms'
import type {
  AgentConfigCatalog,
  RegisterAgentResult,
  RegisterProjectResult,
  DeregisterAgentResult,
} from '../api'

function catalog(): AgentConfigCatalog {
  return {
    project: 'kaidera-os',
    harnesses: [
      { value: 'claude-code', label: 'Claude Code', model_source: 'fixed', lane: null, lane_label: null },
      { value: 'pi', label: 'Pi', model_source: 'catalog', lane: null, lane_label: null },
    ],
    models_by_harness: {
      'claude-code': [{ value: 'opus', label: 'Opus' }],
      pi: [{ value: 'gpt-5', label: 'GPT-5', provider: 'openai' }],
    },
    reasoning_by_harness: { 'claude-code': [{ value: 'high', label: 'high' }], pi: [] },
    default_harness: 'claude-code',
    default_model: 'opus',
  }
}

// ===========================================================================
//  AddAgentModal
// ===========================================================================

describe('AddAgentModal', () => {
  it('submits the new-agent payload + calls onDone then closes on success', async () => {
    const user = userEvent.setup()
    const ok: RegisterAgentResult = { ok: true, agent: 'quinn', role: 'qa', error: null }
    const client = {
      configCatalog: vi.fn().mockResolvedValue(catalog()),
      registerAgent: vi.fn().mockResolvedValue(ok),
    }
    const onDone = vi.fn()
    const onClose = vi.fn()

    render(
      <AddAgentModal open project="kaidera-os" client={client} onDone={onDone} onClose={onClose} />,
    )

    // the catalog loads the harness/model dropdowns
    await waitFor(() => expect(client.configCatalog).toHaveBeenCalledWith('kaidera-os', expect.anything()))

    await user.type(screen.getByLabelText('Name'), 'quinn')
    await user.type(screen.getByLabelText('Role'), 'qa')
    await user.click(screen.getByRole('button', { name: /add worker/i }))

    await waitFor(() => expect(client.registerAgent).toHaveBeenCalledTimes(1))
    const [proj, body] = client.registerAgent.mock.calls[0]
    expect(proj).toBe('kaidera-os')
    expect(body.name).toBe('quinn')
    expect(body.role).toBe('qa')
    // the seeded harness/model defaults ride along
    expect(body.harness).toBe('claude-code')
    expect(body.model).toBe('opus')
    expect(body.writer_scope).toBe('work')
    // success → refetch + close
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('validates name + role before calling the client', async () => {
    const user = userEvent.setup()
    const client = {
      configCatalog: vi.fn().mockResolvedValue(catalog()),
      registerAgent: vi.fn(),
    }
    render(
      <AddAgentModal open project="kaidera-os" client={client} onDone={vi.fn()} onClose={vi.fn()} />,
    )
    await waitFor(() => expect(client.configCatalog).toHaveBeenCalled())

    await user.click(screen.getByRole('button', { name: /add worker/i }))
    expect(await screen.findByText(/worker name is required/i)).toBeInTheDocument()
    expect(client.registerAgent).not.toHaveBeenCalled()
  })

  it('surfaces a friendly degraded-write error (caller not a registered writer)', async () => {
    const user = userEvent.setup()
    const bad: RegisterAgentResult = {
      ok: false,
      agent: 'q',
      role: 'qa',
      error: 'The console writer may not be authorised to add agents on this project.',
    }
    const client = {
      configCatalog: vi.fn().mockResolvedValue(catalog()),
      registerAgent: vi.fn().mockResolvedValue(bad),
    }
    const onDone = vi.fn()
    const onClose = vi.fn()
    render(
      <AddAgentModal open project="kaidera-os" client={client} onDone={onDone} onClose={onClose} />,
    )
    await waitFor(() => expect(client.configCatalog).toHaveBeenCalled())

    await user.type(screen.getByLabelText('Name'), 'q')
    await user.type(screen.getByLabelText('Role'), 'qa')
    await user.click(screen.getByRole('button', { name: /add worker/i }))

    expect(await screen.findByText(/not be authorised/i)).toBeInTheDocument()
    // a failed write does NOT close or refetch
    expect(onDone).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('repopulates the model list when the harness changes', async () => {
    const user = userEvent.setup()
    const client = {
      configCatalog: vi.fn().mockResolvedValue(catalog()),
      registerAgent: vi.fn().mockResolvedValue({ ok: true, agent: 'x', role: 'r', error: null }),
    }
    render(
      <AddAgentModal open project="kaidera-os" client={client} onDone={vi.fn()} onClose={vi.fn()} />,
    )
    await waitFor(() => expect(client.configCatalog).toHaveBeenCalled())

    // switch to pi → its model (gpt-5) appears as an option
    await user.selectOptions(screen.getByLabelText('Harness'), 'pi')
    expect(screen.getByRole('option', { name: 'GPT-5' })).toBeInTheDocument()
  })

  it('role preset Orchestrator registers a deterministic worker without AI config', async () => {
    const user = userEvent.setup()
    const client = {
      configCatalog: vi.fn().mockResolvedValue(catalog()),
      registerAgent: vi.fn().mockResolvedValue({ ok: true, agent: 'orchestrator', role: 'orchestrator', error: null }),
    }
    render(
      <AddAgentModal open project="kaidera-os" client={client} onDone={vi.fn()} onClose={vi.fn()} />,
    )
    await waitFor(() => expect(client.configCatalog).toHaveBeenCalled())

    await user.type(screen.getByLabelText('Name'), 'orchestrator')
    await user.selectOptions(screen.getByLabelText(/role preset/i), 'orchestrator')

    expect(screen.getByLabelText('Role')).toHaveValue('orchestrator')
    expect(screen.queryByLabelText('Designation')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Harness')).not.toBeInTheDocument()
    expect(screen.getByRole('switch', { name: /allow auto-run when assigned/i })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: /add worker/i }))
    await waitFor(() => expect(client.registerAgent).toHaveBeenCalledTimes(1))
    const [, body] = client.registerAgent.mock.calls[0]
    expect(body).toEqual({
      name: 'orchestrator',
      role: 'orchestrator',
      designation: 'deterministic',
      auto_dispatch: 'false',
      writer_scope: 'work',
    })
  })

  it('role preset PM AI Agent registers as non-interactive model-backed worker', async () => {
    const user = userEvent.setup()
    const client = {
      configCatalog: vi.fn().mockResolvedValue(catalog()),
      registerAgent: vi.fn().mockResolvedValue({ ok: true, agent: 'morgan', role: 'pm', error: null }),
    }
    render(
      <AddAgentModal open project="kaidera-os" client={client} onDone={vi.fn()} onClose={vi.fn()} />,
    )
    await waitFor(() => expect(client.configCatalog).toHaveBeenCalled())

    await user.type(screen.getByLabelText('Name'), 'morgan')
    await user.selectOptions(screen.getByLabelText(/role preset/i), 'pm')
    await user.click(screen.getByRole('button', { name: /add worker/i }))

    await waitFor(() => expect(client.registerAgent).toHaveBeenCalledTimes(1))
    const [, body] = client.registerAgent.mock.calls[0]
    expect(body).toMatchObject({
      name: 'morgan',
      role: 'pm',
      designation: 'autonomous',
      auto_dispatch: 'false',
      harness: 'claude-code',
      model: 'opus',
      writer_scope: 'work',
    })
  })

  it('renders nothing when closed', () => {
    const client = { configCatalog: vi.fn(), registerAgent: vi.fn() }
    render(
      <AddAgentModal open={false} project="kaidera-os" client={client} onDone={vi.fn()} onClose={vi.fn()} />,
    )
    expect(screen.queryByText('Add worker')).not.toBeInTheDocument()
    expect(client.configCatalog).not.toHaveBeenCalled()
  })
})

// ===========================================================================
//  AddProjectModal
// ===========================================================================

describe('AddProjectModal', () => {
  it('submits the new-project payload + calls onDone then closes on success', async () => {
    const user = userEvent.setup()
    const ok: RegisterProjectResult = { ok: true, project_key: 'acme', error: null }
    const client = { registerProject: vi.fn().mockResolvedValue(ok) }
    const onDone = vi.fn()
    const onClose = vi.fn()

    render(<AddProjectModal open client={client} onDone={onDone} onClose={onClose} />)

    await user.type(screen.getByLabelText('Project key'), 'acme')
    await user.type(screen.getByLabelText(/Display name/i), 'Acme')
    await user.type(screen.getByLabelText(/Project folder/i), '/abs/acme')
    await user.click(screen.getByRole('button', { name: /add project/i }))

    await waitFor(() => expect(client.registerProject).toHaveBeenCalledTimes(1))
    const [body] = client.registerProject.mock.calls[0]
    expect(body).toEqual({ project_key: 'acme', display_name: 'Acme', repo_root: '/abs/acme' })
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('scans installed project packs and submits the selected pack key', async () => {
    const user = userEvent.setup()
    const ok: RegisterProjectResult = {
      ok: true,
      project_key: 'customer-project',
      project_pack: {
        key: 'basic-project-pack',
        name: 'Basic Project Pack',
        seed_files: ['cortex-seed/README.md'],
        seed_count: 1,
        ingested: 1,
        errors: [],
      },
      error: null,
    }
    const client = {
      listProjectPacks: vi.fn().mockResolvedValue({
        ok: true,
        packs: [
          {
            key: 'basic-project-pack',
            name: 'Basic Project Pack',
            version: '0.1.0',
            description: 'Generic starter pack.',
            default_project_key: 'customer-project',
            seed_files: ['cortex-seed/README.md'],
            seed_count: 1,
            extension_modules: [],
          },
        ],
        error: null,
      }),
      registerProject: vi.fn().mockResolvedValue(ok),
    }

    render(<AddProjectModal open client={client} onDone={vi.fn()} onClose={vi.fn()} />)

    await user.type(screen.getByLabelText(/Project folder/i), '/abs/customer')
    await user.click(screen.getByRole('button', { name: /scan packs/i }))
    await waitFor(() => expect(client.listProjectPacks).toHaveBeenCalledWith('/abs/customer'))
    await screen.findByRole('option', { name: /Basic Project Pack/i })
    await user.selectOptions(screen.getByLabelText(/Project pack/i), 'basic-project-pack')
    expect(screen.getByLabelText('Project key')).toHaveValue('customer-project')
    await user.click(screen.getByRole('button', { name: /add project/i }))

    await waitFor(() => expect(client.registerProject).toHaveBeenCalledTimes(1))
    const [body] = client.registerProject.mock.calls[0]
    expect(body).toEqual({
      project_key: 'customer-project',
      repo_root: '/abs/customer',
      project_pack_key: 'basic-project-pack',
    })
  })

  it('rejects a non-absolute project folder before calling the client', async () => {
    const user = userEvent.setup()
    const client = { registerProject: vi.fn() }
    render(<AddProjectModal open client={client} onDone={vi.fn()} onClose={vi.fn()} />)

    await user.type(screen.getByLabelText('Project key'), 'acme')
    await user.type(screen.getByLabelText(/Project folder/i), 'relative/path')
    await user.click(screen.getByRole('button', { name: /add project/i }))

    expect(await screen.findByText(/must be an absolute path/i)).toBeInTheDocument()
    expect(client.registerProject).not.toHaveBeenCalled()
  })

  it('requires a project key', async () => {
    const user = userEvent.setup()
    const client = { registerProject: vi.fn() }
    render(<AddProjectModal open client={client} onDone={vi.fn()} onClose={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /add project/i }))
    expect(await screen.findByText(/project key is required/i)).toBeInTheDocument()
    expect(client.registerProject).not.toHaveBeenCalled()
  })

  it('surfaces a friendly degraded-write error (admin token missing)', async () => {
    const user = userEvent.setup()
    const bad: RegisterProjectResult = {
      ok: false,
      project_key: 'acme',
      error: 'Adding a project needs the Cortex admin token configured.',
    }
    const client = { registerProject: vi.fn().mockResolvedValue(bad) }
    render(<AddProjectModal open client={client} onDone={vi.fn()} onClose={vi.fn()} />)

    await user.type(screen.getByLabelText('Project key'), 'acme')
    await user.click(screen.getByRole('button', { name: /add project/i }))
    expect(await screen.findByText(/admin token/i)).toBeInTheDocument()
  })
})

// ===========================================================================
//  DeregisterAgentModal
// ===========================================================================

describe('DeregisterAgentModal', () => {
  it('confirms + calls deregister, then onDone + close on success', async () => {
    const user = userEvent.setup()
    const ok: DeregisterAgentResult = { ok: true, removed: true, agent: 'gone', error: null }
    const client = { deregisterAgent: vi.fn().mockResolvedValue(ok) }
    const onDone = vi.fn()
    const onClose = vi.fn()

    render(
      <DeregisterAgentModal
        open
        project="kaidera-os"
        agent="gone"
        client={client}
        onDone={onDone}
        onClose={onClose}
      />,
    )

    await user.click(screen.getByRole('button', { name: /^deregister$/i }))
    await waitFor(() => expect(client.deregisterAgent).toHaveBeenCalledWith('kaidera-os', 'gone'))
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('surfaces a friendly error (admin token) on a failed remove + stays open', async () => {
    const user = userEvent.setup()
    const bad: DeregisterAgentResult = {
      ok: false,
      removed: false,
      agent: 'gone',
      error: 'Removing an agent needs the Cortex admin token configured.',
    }
    const client = { deregisterAgent: vi.fn().mockResolvedValue(bad) }
    const onDone = vi.fn()
    const onClose = vi.fn()
    render(
      <DeregisterAgentModal
        open
        project="kaidera-os"
        agent="gone"
        client={client}
        onDone={onDone}
        onClose={onClose}
      />,
    )
    await user.click(screen.getByRole('button', { name: /^deregister$/i }))
    expect(await screen.findByText(/admin token/i)).toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })
})
