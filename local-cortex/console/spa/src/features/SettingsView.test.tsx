import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import type { AppSettings, Project, SystemSchema } from '../api'
import { SettingsView, type SettingsWriteClient } from './SettingsView'

const appSettings: AppSettings = {
  project: 'kaidera-os',
  settings: { default_harness: 'claude-code', max_runs: 4 },
  store_connected: true,
}

const systemSchema: SystemSchema = {
  project: 'kaidera-os',
  store_connected: true,
  groups: [
    {
      key: 'cortex',
      label: 'Cortex connection',
      fields: [
        {
          key: 'cortex_base_url',
          label: 'Cortex base URL',
          type: 'text',
          group: 'cortex',
          help: 'Loopback URL',
          placeholder: 'http://localhost:8501',
          value: 'http://localhost:8501',
        },
        {
          key: 'cortex_admin_token',
          label: 'Cortex admin token',
          type: 'secret',
          group: 'cortex',
          help: 'Optional',
          placeholder: 'set',
          value: '',
          is_set: true,
        },
      ],
    },
  ],
}

const project: Project = {
  project_key: 'kaidera-os',
  display_name: 'Kaidera OS',
  status: 'active',
  repo_root: '/workspace/kaidera-os',
}

function client(): SettingsWriteClient {
  return {
    setAppSetting: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      settings: {},
      store_connected: true,
      ok: true,
    }),
    setAppSettings: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      settings: {},
      store_connected: true,
      ok: true,
    }),
    setWorkspace: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      project_key: 'kaidera-os',
      ok: true,
      repo_root: '/workspace/new',
      previous_repo_root: '/workspace/kaidera-os',
      error: null,
    }),
  }
}

function renderView(writeClient = client()) {
  const onSaved = vi.fn()
  render(
    <SettingsView
      project="kaidera-os"
      appSettings={appSettings}
      systemSchema={systemSchema}
      projectRow={project}
      projects={[project]}
      loading={false}
      error={null}
      client={writeClient}
      onSaved={onSaved}
    />,
  )
  return { writeClient, onSaved }
}

describe('SettingsView community boundary', () => {
  it('shows only community settings sections', () => {
    renderView()
    for (const name of ['System', 'Workspace', 'Extensions', 'Cortex']) {
      expect(screen.getByRole('tab', { name })).toBeInTheDocument()
    }
    for (const name of ['Providers', 'License', 'Billing']) {
      expect(screen.queryByRole('tab', { name })).not.toBeInTheDocument()
    }
  })

  it('saves changed system values and never renders a stored secret', async () => {
    const user = userEvent.setup()
    const { writeClient, onSaved } = renderView()

    const baseUrl = screen.getByLabelText('Cortex base URL')
    await user.clear(baseUrl)
    await user.type(baseUrl, 'http://localhost:8600')
    expect(screen.getByLabelText(/Cortex admin token \(masked\)/i)).toHaveValue('set')
    expect(document.body.textContent).not.toContain('community-secret')
    await user.click(screen.getByRole('button', { name: /Save settings/ }))

    await waitFor(() =>
      expect(writeClient.setAppSettings).toHaveBeenCalledWith('kaidera-os', {
        cortex_base_url: 'http://localhost:8600',
      }),
    )
    expect(onSaved).toHaveBeenCalled()
  })

  it('updates the selected project workspace', async () => {
    const user = userEvent.setup()
    const { writeClient } = renderView()

    await user.click(screen.getByRole('tab', { name: 'Workspace' }))
    const row = screen.getByTestId('ws-row-kaidera-os')
    const input = within(row).getByRole('textbox')
    await user.clear(input)
    await user.type(input, '/workspace/new')
    await user.click(within(row).getByRole('button', { name: 'Save folder' }))

    await waitFor(() =>
      expect(writeClient.setWorkspace).toHaveBeenCalledWith('kaidera-os', {
        repo_root: '/workspace/new',
      }),
    )
  })
})
