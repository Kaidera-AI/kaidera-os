import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type {
  AppSettings,
  Project,
  ProvidersCatalog,
  ProvidersConfig,
  SystemSchema,
} from '../api'
import { SettingsView, type SettingsWriteClient } from './SettingsView'

function appSettings(): AppSettings {
  return { project: 'kaidera-os', settings: {}, store_connected: true }
}

function systemSchema(): SystemSchema {
  return {
    project: 'kaidera-os',
    store_connected: true,
    groups: [
      {
        key: 'harness',
        label: 'Harness',
        fields: [
          {
            key: 'harness_default',
            label: 'Default harness',
            type: 'select',
            group: 'harness',
            help: 'Harness used by new workers.',
            placeholder: '',
            value: 'kaidera',
            options: ['kaidera', 'codex', 'pi'],
          },
          {
            key: 'harness_autostart',
            label: 'Auto-start autonomy',
            type: 'bool',
            group: 'harness',
            help: '',
            placeholder: '',
            value: false,
          },
        ],
      },
    ],
  }
}

function catalog(): ProvidersCatalog {
  return {
    project: 'kaidera-os',
    providers: [
      {
        name: 'kaidera-manifold',
        models: [
          {
            model: 'openai/gpt-5.4',
            type: 'chat',
            reasoning_tiers: ['none', 'low', 'medium', 'high', 'xhigh'],
            input_price_per_mtok: 2.5,
            output_price_per_mtok: 15,
            context_window: 400000,
            source: 'live',
            freshness: 'live',
          },
        ],
      },
    ],
  }
}

function providerConfig(): ProvidersConfig {
  return {
    project: 'kaidera-os',
    store_connected: true,
    providers: [
      {
        name: 'kaidera-manifold',
        label: 'Kaidera AI Manifold',
        key_is_set: true,
        is_custom: false,
        testable: true,
        provider_ref: 'kaidera_manifold_api_key',
        key_field: 'kaidera_manifold_api_key',
        base_url: 'https://api.kaidera.ai/v1',
        project_id: 'project-1',
      },
    ],
  }
}

function project(): Project {
  return {
    project_key: 'kaidera-os',
    display_name: 'Kaidera OS',
    status: 'active',
    repo_root: '/work/kaidera-os',
  }
}

function fakeClient(overrides: Partial<SettingsWriteClient> = {}): SettingsWriteClient {
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
    providerKeyTest: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      ok: true,
      detail: 'Manifold connection is healthy.',
      status: 'ok',
      label: 'Kaidera AI Manifold',
    }),
    setWorkspace: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      project_key: 'kaidera-os',
      ok: true,
      repo_root: '/work/new',
      previous_repo_root: '/work/kaidera-os',
      error: null,
    }),
    listProjectPacks: vi.fn().mockResolvedValue({ ok: true, packs: [], error: null }),
    setProjectPackExtension: vi.fn().mockResolvedValue({ ok: true, pack: null, error: null }),
    cortexConfig: vi.fn().mockResolvedValue({
      ok: true,
      config: {
        embedding_provider: 'kaidera-manifold',
        embedding_model: 'openai/text-embedding-3-large',
        embedding_dims: 3072,
        rerank_enabled: true,
        rerank_provider: 'kaidera-manifold',
        rerank_model: 'cohere/rerank-v3.5',
      },
      error: null,
    }),
    setCortexConfig: vi.fn().mockResolvedValue({
      ok: true,
      config: {},
      error: null,
    }),
    cortexEmbeddingBacklog: vi.fn().mockResolvedValue({
      ok: true,
      project: 'kaidera-os',
      backlog: {},
      coverage: {},
      error: null,
    }),
    cortexEmbeddingBackfill: vi.fn().mockResolvedValue({
      ok: true,
      project: 'kaidera-os',
      result: {},
      error: null,
    }),
    runstateRestartStatus: vi.fn().mockResolvedValue({
      ok: true,
      project: 'kaidera-os',
      store: 'ok',
      active: [],
      counts: { active: 0, restart_survivable: 0, request_lived: 0, needs_reconcile: 0 },
      error: null,
    }),
    ...overrides,
  }
}

function renderView(overrides: Partial<Parameters<typeof SettingsView>[0]> = {}) {
  const client = overrides.client ?? fakeClient()
  const onSaved = overrides.onSaved ?? vi.fn()
  render(
    <SettingsView
      project="kaidera-os"
      appSettings={appSettings()}
      systemSchema={systemSchema()}
      providers={catalog()}
      providersConfig={providerConfig()}
      projectRow={project()}
      projects={[project()]}
      loading={false}
      error={null}
      client={client}
      onSaved={onSaved}
      {...overrides}
    />,
  )
  return { client, onSaved }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('SettingsView public edition', () => {
  it('renders only the public settings sections', () => {
    renderView()
    for (const label of [
      'System',
      'Providers',
      'Workspace',
      'Extensions',
      'Cortex',
      'Open-source license',
    ]) {
      expect(screen.getByRole('tab', { name: label })).toBeInTheDocument()
    }
    expect(screen.queryByRole('tab', { name: 'Billing' })).not.toBeInTheDocument()
  })

  it('saves only changed system values', async () => {
    const user = userEvent.setup()
    const { client, onSaved } = renderView()

    await user.selectOptions(screen.getByLabelText('Default harness'), 'codex')
    await user.click(screen.getByRole('button', { name: 'Save settings' }))

    await waitFor(() =>
      expect(client.setAppSettings).toHaveBeenCalledWith('kaidera-os', {
        harness_default: 'codex',
      }),
    )
    expect(onSaved).toHaveBeenCalled()
  })

  it('saves and tests the managed Manifold connection', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await user.click(screen.getByRole('tab', { name: 'Providers' }))

    await user.type(screen.getByLabelText('Inference key'), 'mfld-secret')
    await user.click(screen.getByRole('button', { name: 'Save connection' }))

    await waitFor(() =>
      expect(client.setAppSettings).toHaveBeenCalledWith('kaidera-os', {
        kaidera_manifold_api_key: 'mfld-secret',
      }),
    )

    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    await waitFor(() =>
      expect(client.providerKeyTest).toHaveBeenCalledWith('kaidera-os', {
        provider: 'kaidera_manifold_api_key',
        use_stored: true,
      }),
    )
    expect(await screen.findByText('Manifold connection is healthy.')).toBeInTheDocument()
  })

  it('renders live model effort levels from the catalog', async () => {
    const user = userEvent.setup()
    renderView()
    await user.click(screen.getByRole('tab', { name: 'Providers' }))

    expect(screen.getByText('openai/gpt-5.4')).toBeInTheDocument()
    expect(screen.getByText('none, low, medium, high, xhigh')).toBeInTheDocument()
    expect(screen.getByText('400,000')).toBeInTheDocument()
  })

  it('shows an informational AGPL notice without activation controls', async () => {
    const user = userEvent.setup()
    renderView()
    await user.click(screen.getByRole('tab', { name: 'Open-source license' }))

    expect(screen.getByText('GNU AGPLv3 open-source edition')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'sales@kaidera.ai' })).toHaveAttribute(
      'href',
      'mailto:sales@kaidera.ai',
    )
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /activate/i })).not.toBeInTheDocument()
  })

  it('updates a project workspace', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await user.click(screen.getByRole('tab', { name: 'Workspace' }))

    const input = screen.getByLabelText('Repo root')
    await user.clear(input)
    await user.type(input, '/work/new')
    await user.click(screen.getByRole('button', { name: 'Save folder' }))

    await waitFor(() =>
      expect(client.setWorkspace).toHaveBeenCalledWith('kaidera-os', {
        repo_root: '/work/new',
      }),
    )
  })

  it('loads Cortex health and keeps the component name stable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          status: 'ok',
          base_url: 'http://127.0.0.1:8501',
          surface_version: '6',
          event_backend: 'postgres',
        }),
      } as Response),
    )
    const user = userEvent.setup()
    renderView()
    await user.click(screen.getByRole('tab', { name: 'Cortex' }))

    expect(await screen.findByText('Cortex connection')).toBeInTheDocument()
    expect(await screen.findByText('http://127.0.0.1:8501')).toBeInTheDocument()
    expect(screen.getByText('Cortex retrieval models')).toBeInTheDocument()
  })

  it('gates project-specific tabs when no project is selected', async () => {
    const user = userEvent.setup()
    renderView({ project: null, projectRow: null, projects: [] })
    await user.click(screen.getByRole('tab', { name: 'Workspace' }))
    expect(screen.getByText('Select or create a project to edit its workspace.')).toBeInTheDocument()
  })
})
