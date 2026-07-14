import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SettingsView } from './SettingsView'
import type {
  AppSettings,
  LicenseStatus,
  Project,
  ProvidersCatalog,
  ProvidersConfig,
  SystemSchema,
} from '../api'
import type { SettingsWriteClient } from './SettingsView'

function appSettings(over: Partial<AppSettings> = {}): AppSettings {
  return {
    project: 'kaidera-os',
    // NON-provider settings only — the provider keys are filtered OUT of the raw
    // editor server-side (they have a canonical home in the Providers tab now).
    settings: { default_harness: 'claude-code', max_runs: 4 },
    store_connected: true,
    ...over,
  }
}

function licenseStatus(over: Partial<LicenseStatus> = {}): LicenseStatus {
  return {
    project: 'kaidera-os',
    edition: 'public',
    required: true,
    valid: false,
    reason: 'free tier (no license)',
    customer: null,
    expires: null,
    features: [],
    in_grace: false,
    hard_gate: {
      enabled: false,
      required: true,
      allowed: true,
      surface: 'app',
      state: 'soft',
      reason: 'hard gate disabled',
      token_present: false,
      revoked: false,
      in_grace: false,
      allowed_surfaces: ['auth', 'backup', 'export', 'health', 'license', 'support'],
    },
    all_harnesses: false,
    harnesses: ['kaidera'],
    advanced: { manifold_access: false },
    limits: { projects: 1, teams: 1, workers: 4, users: 1 },
    ...over,
  }
}

/**
 * A typed System schema — Cortex-connection + harness + app preferences (the
 * NON-provider settings). The provider API keys are NO LONGER in System; we keep a
 * generic (non-provider) readonly + a bool + text/number to exercise the typed form.
 */
function systemSchema(over: Partial<SystemSchema> = {}): SystemSchema {
  return {
    project: 'kaidera-os',
    store_connected: true,
    groups: [
      {
        key: 'cortex',
        label: 'Cortex connection',
        fields: [
          { key: 'cortex_base_url', label: 'Cortex base URL', type: 'text', group: 'cortex', help: 'the loopback URL', placeholder: 'http://localhost:8501', value: 'http://localhost:8501' },
          { key: 'max_runs', label: 'Max runs', type: 'number', group: 'cortex', help: '', placeholder: '', value: 4 },
          { key: 'autonomy_default', label: 'Autonomy default', type: 'bool', group: 'cortex', help: 'default autonomy', placeholder: '', value: false },
          { key: 'surface_version', label: 'Surface version', type: 'readonly', group: 'cortex', help: '', placeholder: '', value: 'v2.5' },
        ],
      },
    ],
    ...over,
  }
}

/** A providers catalog (the Models section) with two providers (one priced, one not). */
function providersCatalog(over: Partial<ProvidersCatalog> = {}): ProvidersCatalog {
  return {
    project: 'kaidera-os',
    providers: [
      {
        name: 'anthropic',
        models: [
          {
            model: 'claude-opus-4-8',
            type: 'chat',
            reasoning_tiers: ['low', 'high', 'max'],
            input_price_per_mtok: 5,
            output_price_per_mtok: 25,
            context_window: 1000000,
            source: 'live',
            freshness: 'live',
          },
        ],
      },
      {
        name: 'openrouter',
        models: [
          {
            model: 'meta/llama-3',
            type: 'chat',
            reasoning_tiers: [],
            input_price_per_mtok: null,
            output_price_per_mtok: null,
            context_window: null,
            source: 'supplement',
            freshness: 'supplement',
          },
        ],
      },
    ],
    ...over,
  }
}

/** The configured-providers control view: one built-in with a key, one without, one custom. */
function providersConfig(over: Partial<ProvidersConfig> = {}): ProvidersConfig {
  return {
    project: 'kaidera-os',
    store_connected: true,
    providers: [
      { name: 'anthropic', label: 'Anthropic', key_is_set: true, is_custom: false, testable: true, provider_ref: 'anthropic_api_key', key_field: 'anthropic_api_key' },
      { name: 'openai', label: 'OpenAI', key_is_set: false, is_custom: false, testable: true, provider_ref: 'openai_api_key', key_field: 'openai_api_key' },
      { name: 'Together AI', label: 'Together AI', key_is_set: true, is_custom: true, testable: true, provider_ref: 'custom:together-ai', base_url: 'https://api.together.xyz/v1' },
    ],
    ...over,
  }
}

function projectRow(over: Partial<Project> = {}): Project {
  return {
    project_key: 'kaidera-os',
    display_name: 'Kaidera OS',
    status: 'active',
    repo_root: '/Users/amad/DevVault/kaidera-os',
    ...over,
  }
}

/** The full active-projects list (the Workspace tab is a MULTI-project editor — every active
 * project's repo_root, each editable). Reuses the /projects rows the shell already holds. */
function projectsList(): Project[] {
  return [
    projectRow(),
    { project_key: 'paul', display_name: 'Paul', status: 'active', repo_root: '/Users/amad/DevVault/paul' },
    { project_key: 'asw-connect', display_name: 'ASW Connect', status: 'active', repo_root: '/Users/amad/DevVault/asw-connect' },
  ]
}

/** A fake write client that records calls + resolves with an authoritative-ish echo. */
function fakeClient(over: Partial<SettingsWriteClient> = {}): SettingsWriteClient {
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
    addCustomProvider: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      ok: true,
      added: 'Together AI',
      error: null,
      custom_providers: [
        { id: 'together-ai', name: 'Together AI', base_url: 'https://api.together.xyz/v1', has_key: true, key_display: '•••• set' },
      ],
    }),
    deleteCustomProvider: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      ok: true,
      removed: true,
      error: null,
      custom_providers: [],
    }),
    providerKeyTest: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      ok: true,
      detail: 'reached the provider — 12 models',
      status: 'ok',
      label: 'Anthropic',
    }),
    setWorkspace: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      project_key: 'kaidera-os',
      ok: true,
      repo_root: '/new/path',
      previous_repo_root: '/old/path',
      error: null,
    }),
    cortexConfig: vi.fn().mockResolvedValue({
      ok: true,
      error: null,
      config: {
        embedding_provider: 'openrouter',
        embedding_model: 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
        embedding_dims: 768,
        rerank_enabled: true,
        rerank_provider: 'nvidia',
        rerank_model: 'nv-rerank-qa-mistral-4b:1',
        embed_input_max_chars: 500,
        rerank_input_max_chars: 500,
        embed_timeout_ms: 15000,
        rerank_timeout_ms: 15000,
      },
    }),
    setCortexConfig: vi.fn().mockResolvedValue({
      ok: true,
      error: null,
      config: {
        embedding_provider: 'openrouter',
        embedding_model: 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
        embedding_dims: 768,
        rerank_enabled: true,
        rerank_provider: 'nvidia',
        rerank_model: 'nv-rerank-qa-mistral-4b:1',
      },
    }),
    cortexEmbeddingBacklog: vi.fn().mockResolvedValue({
      ok: true,
      project: 'kaidera-os',
      backlog: { decisions: 1, lessons: 0, knowledge: 2, messages: 0, work_products: 0, total: 3 },
      coverage: {
        decisions: { total: 5, embedded: 4, backlog: 1, skipped: 0, pct: 80 },
        knowledge: { total: 4, embedded: 2, backlog: 2, skipped: 0, pct: 50 },
      },
      error: null,
    }),
    cortexEmbeddingBackfill: vi.fn().mockResolvedValue({
      ok: true,
      project: 'kaidera-os',
      result: {
        project: 'kaidera-os',
        table: 'all',
        dry_run: true,
        processed: 3,
      },
      error: null,
    }),
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
          extension_modules: ['basic_project_pack.example_worker'],
          extensions: [
            {
              module: 'basic_project_pack.example_worker',
              required: false,
              description: 'Optional worker.',
              enabled: true,
              loaded: false,
              status: 'enabled_restart_required',
              restart_required: true,
            },
          ],
          extensions_enabled: ['basic_project_pack.example_worker'],
          extension_env: 'KAIDERA_OS_EXTENSION_MODULES',
          portals: [
            {
              key: 'operator-chat',
              type: 'thin-web',
              agent: 'lead',
              route_prefix: '/portal/operator-chat',
              auth: 'kaidera-os-auth',
              stream_contract: 'runstate-sse',
              runtime_contract: {
                contract: 'runstate-sse',
                chat_endpoint_template: '/agents/{project}/lead/chat',
                stream_endpoint_template: '/runstate/stream?project={project}&run={run_id}',
                run_endpoint_template: '/runs/run/{run_id}',
                chat_events: ['run', 'error', 'done'],
                stream_events: ['runstate'],
                rules: ['Open run-state stream by run id.'],
              },
              frontend_path: 'portal/index.html',
              frontend_exists: true,
              required: false,
              status: 'ready',
            },
          ],
          restart_required: true,
        },
      ],
      error: null,
    }),
    setProjectPackExtension: vi.fn().mockResolvedValue({
      ok: true,
      pack: {
        key: 'basic-project-pack',
        name: 'Basic Project Pack',
        version: '0.1.0',
        seed_files: ['cortex-seed/README.md'],
        seed_count: 1,
        extension_modules: ['basic_project_pack.example_worker'],
        extensions: [
          {
            module: 'basic_project_pack.example_worker',
            required: false,
            description: 'Optional worker.',
            enabled: false,
            loaded: false,
            status: 'disabled',
            restart_required: false,
          },
        ],
        portals: [
          {
            key: 'operator-chat',
            type: 'thin-web',
            agent: 'lead',
            route_prefix: '/portal/operator-chat',
            auth: 'kaidera-os-auth',
            stream_contract: 'runstate-sse',
            runtime_contract: {
              contract: 'runstate-sse',
              chat_endpoint_template: '/agents/{project}/lead/chat',
              stream_endpoint_template: '/runstate/stream?project={project}&run={run_id}',
              run_endpoint_template: '/runs/run/{run_id}',
              chat_events: ['run', 'error', 'done'],
              stream_events: ['runstate'],
              rules: ['Open run-state stream by run id.'],
            },
            frontend_path: 'portal/index.html',
            frontend_exists: true,
            required: false,
            status: 'ready',
          },
        ],
        restart_required: false,
      },
      error: null,
    }),
    runstateRestartStatus: vi.fn().mockResolvedValue({
      ok: true,
      project: 'kaidera-os',
      store: 'ok',
      current_pid: 123,
      active: [
        {
          run_id: 'worker-1',
          project: 'kaidera-os',
          agent: 'builder',
          handoff_id: 'h1',
          status: 'running',
          lease_owner: 'worker',
          pid: 777,
          lifecycle: 'restart_survivable',
          restart_survivable: true,
          needs_reconcile: false,
        },
      ],
      counts: { active: 1, restart_survivable: 1, request_lived: 0, needs_reconcile: 0 },
      error: null,
    }),
    license: vi.fn().mockResolvedValue(licenseStatus()),
    licenseLogin: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      action: 'login',
      ok: true,
      status_code: 200,
      error: null,
      stored: true,
      grant_valid: true,
      install_id: 'install-1',
      machine_fp: 'fp',
      revoked: false,
      latest_release: null,
      customer: 'Acme',
      org_id: 'org_1',
      expires_at: '2026-07-04T00:00:00Z',
      scopes: ['license:read'],
      manifold_enabled: true,
      manifold_key_stored: true,
    }),
    licenseActivate: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      action: 'activate',
      ok: true,
      status_code: 200,
      error: null,
      stored: true,
      grant_valid: true,
      install_id: 'install-1',
      machine_fp: 'fp',
      revoked: false,
      latest_release: null,
      customer: 'Acme',
    }),
    licenseHeartbeat: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      action: 'heartbeat',
      ok: false,
      status_code: null,
      error: 'no valid license grant to heartbeat',
      stored: false,
      grant_valid: false,
      install_id: null,
      machine_fp: null,
      revoked: false,
      latest_release: null,
      customer: null,
    }),
    licenseRestore: vi.fn().mockResolvedValue({
      project: 'kaidera-os',
      action: 'restore',
      ok: true,
      status_code: 200,
      error: null,
      stored: true,
      grant_valid: true,
      install_id: 'install-1',
      machine_fp: 'fp',
      revoked: false,
      latest_release: null,
      customer: 'Acme',
    }),
    ...over,
  }
}

function renderView(props: Partial<Parameters<typeof SettingsView>[0]> = {}) {
  const client = props.client ?? fakeClient()
  const onSaved = props.onSaved ?? vi.fn()
  render(
    <SettingsView
      project="kaidera-os"
      appSettings={appSettings()}
      systemSchema={systemSchema()}
      providers={providersCatalog()}
      providersConfig={providersConfig()}
      projectRow={projectRow()}
      projects={projectsList()}
      loading={false}
      error={null}
      client={client}
      onSaved={onSaved}
      {...props}
    />,
  )
  return { client, onSaved }
}

/** Click into a settings sub-tab. */
async function openTab(user: ReturnType<typeof userEvent.setup>, name: RegExp | string) {
  await user.click(screen.getByRole('tab', { name }))
}

describe('SettingsView — shell (the tabbed sub-nav)', () => {
  it('renders the project settings tabs and lands on System', () => {
    renderView()
    for (const t of ['System', 'Providers', 'Workspace', 'Extensions', 'Cortex']) {
      expect(screen.getByRole('tab', { name: t })).toBeInTheDocument()
    }
    expect(screen.queryByRole('tab', { name: 'Flags' })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'System' })).toHaveAttribute('aria-selected', 'true')
  })

  it('NO LONGER renders a per-agent Configure tab/section (it moved to the agent pane)', () => {
    renderView()
    expect(screen.queryByRole('tab', { name: 'Configure' })).not.toBeInTheDocument()
    expect(document.querySelector('[data-agent-card]')).toBeNull()
    expect(screen.queryByTestId('override-dot')).not.toBeInTheDocument()
  })

  it('shows a loading hint before anything arrives', () => {
    renderView({ appSettings: null, systemSchema: null, providers: null, loading: true })
    expect(screen.getByText(/loading settings/i)).toBeInTheDocument()
  })

  it('shows an error hint (stale-backend 404) when settings fail to load', () => {
    renderView({ appSettings: null, systemSchema: null, providers: null, error: new Error('404') })
    expect(screen.getByText(/couldn’t load settings/i)).toBeInTheDocument()
  })
})

describe('SettingsView — License tab', () => {
  it('activates online with Kaidera AI console credentials and refreshes posture', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'License')

    expect(screen.queryByRole('link', { name: 'Kaidera AI' })).not.toBeInTheDocument()
    await screen.findByLabelText(/kaidera ai email/i)
    await user.type(screen.getByLabelText(/kaidera ai email/i), 'ops@example.com')
    await user.type(screen.getByLabelText(/kaidera ai password/i), 'secret')
    await user.type(screen.getByLabelText(/kaidera ai mfa code/i), '123456')
    await user.click(screen.getByRole('button', { name: /log in & activate/i }))

    await waitFor(() =>
      expect(client.licenseLogin).toHaveBeenCalledWith('kaidera-os', {
        email: 'ops@example.com',
        password: 'secret',
        mfa_code: '123456',
      }),
    )
    await waitFor(() => expect(screen.getByText(/login complete/i)).toBeInTheDocument())
    expect(client.license).toHaveBeenCalled()
  })

  it('refreshes the online license grant as a soft action', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'License')

    await screen.findByRole('button', { name: /refresh now/i })
    await user.click(screen.getByRole('button', { name: /refresh now/i }))

    await waitFor(() => expect(client.licenseHeartbeat).toHaveBeenCalledWith('kaidera-os'))
    expect(await screen.findByText(/refresh failed/i)).toBeInTheDocument()
  })

  it('restores the platform license session', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'License')

    await screen.findByRole('button', { name: /^restore$/i })
    await user.click(screen.getByRole('button', { name: /^restore$/i }))

    await waitFor(() => expect(client.licenseRestore).toHaveBeenCalledWith('kaidera-os'))
    expect(await screen.findByText(/restore complete/i)).toBeInTheDocument()
  })

  it('shows the default-off hard-gate posture', async () => {
    const user = userEvent.setup()
    renderView()
    await openTab(user, 'License')

    expect(await screen.findByText(/hard gate:/i)).toBeInTheDocument()
    expect(screen.getByText(/hard gate disabled/i)).toBeInTheDocument()
  })
})

// ===========================================================================
//  System tab — typed form, save-changed-keys, the raw editor. The provider keys
//  are NO LONGER here (they moved to the Providers control surface).
// ===========================================================================

describe('SettingsView — System tab (typed form, NON-provider settings)', () => {
  it('renders each schema field by its TYPE (text/number/bool/readonly)', async () => {
    const user = userEvent.setup()
    renderView()
    await openTab(user, 'System')

    expect(screen.getByLabelText('Cortex base URL')).toHaveValue('http://localhost:8501')
    const num = screen.getByLabelText('Max runs') as HTMLInputElement
    expect(num.type).toBe('number')
    expect(num).toHaveValue(4)
    expect(screen.getByRole('switch', { name: /autonomy default/i })).toBeInTheDocument()
    const ro = screen.getByLabelText(/surface version/i) as HTMLInputElement
    expect(ro).toHaveValue('v2.5')
    expect(ro).toHaveAttribute('readonly')
  })

  it('shows every entitled harness and project display names in selects', async () => {
    const user = userEvent.setup()
    renderView({
      systemSchema: systemSchema({
        groups: [
          {
            key: 'defaults',
            label: 'Defaults',
            fields: [
              {
                key: 'cortex_default_project',
                label: 'Default project',
                type: 'select',
                group: 'defaults',
                help: '',
                placeholder: '',
                value: 'kaidera-core',
                options: [],
                options_source: 'projects',
              },
              {
                key: 'harness_default',
                label: 'Default harness',
                type: 'select',
                group: 'defaults',
                help: '',
                placeholder: '',
                value: 'kaidera',
                options: ['claude-code', 'codex', 'kaidera', 'pi'],
              },
            ],
          },
        ],
      }),
      projects: [
        { project_key: 'kaidera-core', display_name: 'Kaidera', status: 'active' },
        projectRow(),
      ],
    })
    await openTab(user, 'System')

    const projectSelect = screen.getByLabelText('Default project')
    expect(projectSelect).toHaveDisplayValue('Kaidera')
    expect(within(projectSelect).queryByRole('option', { name: 'kaidera-core' })).not.toBeInTheDocument()

    const harnessSelect = screen.getByLabelText('Default harness')
    for (const harness of ['claude-code', 'codex', 'kaidera', 'pi']) {
      expect(within(harnessSelect).getByRole('option', { name: harness })).toBeInTheDocument()
    }
  })

  it('does NOT render provider API-key fields nor a custom-providers panel in System', async () => {
    const user = userEvent.setup()
    renderView()
    await openTab(user, 'System')
    // no provider secret fields (they moved to Providers)
    expect(screen.queryByLabelText(/anthropic api key/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/openai api key/i)).not.toBeInTheDocument()
    // the custom-providers panel is no longer in System
    expect(document.querySelector('[data-custom-providers]')).toBeNull()
    // a hint points provider config at the Providers tab (the "Providers tab" is a <b>
    // inside the sentence, so the leading text node carries the recognisable phrase)
    expect(screen.getByText(/provider keys live in the/i)).toBeInTheDocument()
  })

  it('Save POSTs ONLY the changed keys', async () => {
    const user = userEvent.setup()
    const { client, onSaved } = renderView()
    await openTab(user, 'System')

    const base = screen.getByLabelText('Cortex base URL')
    await user.clear(base)
    await user.type(base, 'http://localhost:9000')

    await user.click(screen.getByRole('button', { name: /save settings/i }))

    await waitFor(() => expect(client.setAppSettings).toHaveBeenCalledTimes(1))
    const [, payload] = (client.setAppSettings as ReturnType<typeof vi.fn>).mock.lastCall as [string, Record<string, unknown>]
    expect(payload).toEqual({ cortex_base_url: 'http://localhost:9000' })
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  it('Save is disabled until something changes', async () => {
    const user = userEvent.setup()
    renderView()
    await openTab(user, 'System')
    expect(screen.getByRole('button', { name: /save settings/i })).toBeDisabled()
    await user.clear(screen.getByLabelText('Cortex base URL'))
    await user.type(screen.getByLabelText('Cortex base URL'), 'x')
    expect(screen.getByRole('button', { name: /save settings/i })).toBeEnabled()
  })

  it('folds the raw App-settings editor in (still editable) — and it carries NO provider keys', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'System')

    // the raw key→value rows are present + editable (the per-row save still works)
    const input = screen.getByDisplayValue('claude-code')
    await user.clear(input)
    await user.type(input, 'pi')
    const rawRow = input.closest('[data-setting-row]') as HTMLElement
    await user.click(within(rawRow).getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(client.setAppSetting).toHaveBeenCalledWith('kaidera-os', 'default_harness', 'pi'),
    )
    // no provider secret key leaks into the raw editor
    expect(screen.queryByText('anthropic_api_key')).not.toBeInTheDocument()
  })

  it('degrades to the raw editor when the typed schema is unavailable', async () => {
    const user = userEvent.setup()
    renderView({ systemSchema: null })
    await openTab(user, 'System')
    expect(screen.getByText(/typed System schema isn’t available/i)).toBeInTheDocument()
    expect(screen.getByText('default_harness')).toBeInTheDocument()
  })
})

// ===========================================================================
//  Providers tab — the CONTROL surface: configured providers + status + Test +
//  Add (preconfigured/custom) + the Models catalog.
// ===========================================================================

describe('SettingsView — Providers tab (the control surface)', () => {
  it('lists the configured providers with per-provider key status', async () => {
    const user = userEvent.setup()
    renderView()
    await openTab(user, 'Providers')

    // a configured-providers section with the built-ins + the custom one
    const configured = document.querySelector('[data-providers-config]') as HTMLElement
    expect(configured).toBeTruthy()
    expect(within(configured).getByText('Anthropic')).toBeInTheDocument()
    expect(within(configured).getByText('OpenAI')).toBeInTheDocument()
    expect(within(configured).getByText('Together AI')).toBeInTheDocument()
    // per-provider status: key set vs not set
    const anthRow = within(configured).getByText('Anthropic').closest('[data-provider-config]') as HTMLElement
    expect(within(anthRow).getByText(/key set/i)).toBeInTheDocument()
    const openaiRow = within(configured).getByText('OpenAI').closest('[data-provider-config]') as HTMLElement
    expect(within(openaiRow).getByText(/not set/i)).toBeInTheDocument()
  })

  it('a configured provider has a Test button → POSTs provider-key-test', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'Providers')

    const anthRow = screen.getByText('Anthropic').closest('[data-provider-config]') as HTMLElement
    await user.click(within(anthRow).getByRole('button', { name: /^test$/i }))

    await waitFor(() =>
      expect(client.providerKeyTest).toHaveBeenCalledWith(
        'kaidera-os',
        expect.objectContaining({ provider: 'anthropic_api_key' }),
      ),
    )
    expect(await screen.findByText(/reached the provider/i)).toBeInTheDocument()
  })

  it('adding a key to a PRECONFIGURED provider POSTs the secret to app-settings', async () => {
    const user = userEvent.setup()
    const { client, onSaved } = renderView()
    await openTab(user, 'Providers')

    // the not-set provider (OpenAI) exposes an "Add key" affordance → reveal an input
    const openaiRow = screen.getByText('OpenAI').closest('[data-provider-config]') as HTMLElement
    await user.click(within(openaiRow).getByRole('button', { name: /add key/i }))
    await user.type(within(openaiRow).getByLabelText(/openai key/i), 'sk-openai-new')
    await user.click(within(openaiRow).getByRole('button', { name: /^save$/i }))

    // saved via the canonical secret write (the provider's key_field)
    await waitFor(() =>
      expect(client.setAppSetting).toHaveBeenCalledWith('kaidera-os', 'openai_api_key', 'sk-openai-new'),
    )
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  it('adding Amazon Bedrock credentials saves access key, secret, and region together', async () => {
    const user = userEvent.setup()
    const bedrockConfig = providersConfig({
      providers: [
        { name: 'bedrock', label: 'Amazon Bedrock', key_is_set: false, is_custom: false, testable: true, provider_ref: 'aws_secret_access_key', key_field: 'aws_secret_access_key' },
      ],
    })
    const { client, onSaved } = renderView({ providersConfig: bedrockConfig })
    await openTab(user, 'Providers')

    const bedrockRow = screen.getByText('Amazon Bedrock').closest('[data-provider-config]') as HTMLElement
    await user.click(within(bedrockRow).getByRole('button', { name: /add key/i }))
    await user.type(within(bedrockRow).getByLabelText(/aws access key id/i), 'AKIAEXAMPLE')
    await user.type(within(bedrockRow).getByLabelText(/aws secret access key/i), 'bedrock-secret')
    await user.clear(within(bedrockRow).getByLabelText(/aws region/i))
    await user.type(within(bedrockRow).getByLabelText(/aws region/i), 'eu-west-2')
    await user.click(within(bedrockRow).getByRole('button', { name: /^save$/i }))

    await waitFor(() =>
      expect(client.setAppSettings).toHaveBeenCalledWith('kaidera-os', {
        aws_access_key_id: 'AKIAEXAMPLE',
        aws_secret_access_key: 'bedrock-secret',
        aws_region: 'eu-west-2',
      }),
    )
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(document.body.textContent).not.toContain('bedrock-secret')
  })

  it('adding Kaidera AI Manifold saves the API key and project id together', async () => {
    const user = userEvent.setup()
    const manifoldConfig = providersConfig({
      providers: [
        { name: 'kaidera-manifold', label: 'Kaidera AI Manifold', key_is_set: false, is_custom: false, testable: false, provider_ref: 'kaidera_manifold_api_key', key_field: 'kaidera_manifold_api_key' },
      ],
    })
    const { client, onSaved } = renderView({ providersConfig: manifoldConfig })
    await openTab(user, 'Providers')

    const manifoldRow = screen.getByText('Kaidera AI Manifold').closest('[data-provider-config]') as HTMLElement
    await user.click(within(manifoldRow).getByRole('button', { name: /add key/i }))
    await user.type(within(manifoldRow).getByLabelText(/manifold api key/i), 'mfld_live_v1_secret')
    await user.type(within(manifoldRow).getByLabelText(/manifold company id/i), 'proj-uuid-123')
    await user.click(within(manifoldRow).getByRole('button', { name: /^save$/i }))

    await waitFor(() =>
      expect(client.setAppSettings).toHaveBeenCalledWith('kaidera-os', {
        kaidera_manifold_api_key: 'mfld_live_v1_secret',
        kaidera_manifold_project_id: 'proj-uuid-123',
      }),
    )
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(document.body.textContent).not.toContain('mfld_live_v1_secret')
  })

  it('adds a CUSTOM provider → POST custom-providers (masked, never the raw key)', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'Providers')

    await user.click(screen.getByRole('button', { name: /\+ add (custom )?provider/i }))
    await user.type(screen.getByLabelText('Provider name'), 'Together AI')
    await user.type(screen.getByLabelText('Base URL'), 'https://api.together.xyz/v1')
    await user.type(screen.getByLabelText('API key'), 'tok-secret')
    await user.click(screen.getByRole('button', { name: /^add provider$/i }))

    await waitFor(() =>
      expect(client.addCustomProvider).toHaveBeenCalledWith('kaidera-os', {
        name: 'Together AI',
        base_url: 'https://api.together.xyz/v1',
        api_key: 'tok-secret',
      }),
    )
    expect(document.body.textContent).not.toContain('tok-secret')
  })

  it('shows ONLY configured providers in the Models catalog (M3 — hides the unconfigured)', async () => {
    const user = userEvent.setup()
    renderView()
    await openTab(user, 'Providers')

    // anthropic IS configured (key_is_set) → its model + the priced details render
    expect(screen.getByText('claude-opus-4-8')).toBeInTheDocument()
    expect(screen.getByText('low · high · max')).toBeInTheDocument()
    expect(screen.getByText('$5.00')).toBeInTheDocument()
    expect(screen.getByText('1,000,000')).toBeInTheDocument()
    // openrouter is NOT configured (no key) → its model is HIDDEN, even though the catalog
    // can fetch it keylessly (the operator's "I see openrouter, which isn't configured" fix).
    expect(screen.queryByText('meta/llama-3')).not.toBeInTheDocument()
  })

  it('hides all models when NO provider is configured (the Models section explains why)', async () => {
    const user = userEvent.setup()
    renderView({ providersConfig: providersConfig({ providers: [] }) })
    await openTab(user, 'Providers')
    // no configured provider → no models shown; an empty-state hint stands in instead.
    expect(screen.queryByText('claude-opus-4-8')).not.toBeInTheDocument()
    expect(screen.getByText(/only the providers you/i)).toBeInTheDocument()
  })
})

describe('SettingsView — Workspace tab (MULTI-project repo-root editor)', () => {
  it('lists EVERY active project’s repo_root (each its own editable row), not just the selected one', async () => {
    const user = userEvent.setup()
    renderView()
    await openTab(user, 'Workspace')
    // one row per active project (keyed by project_key)
    const rows = document.querySelectorAll('[data-ws-project-row]')
    expect(rows).toHaveLength(3)
    // each row carries its own project name + key + its repo_root seeded into the input (NO hex chip)
    const ld = screen.getByTestId('ws-row-kaidera-os')
    expect(within(ld).getByText('Kaidera OS')).toBeInTheDocument()
    expect(within(ld).queryByText(/:5872/)).not.toBeInTheDocument()
    expect(within(ld).queryByText(/:\?\?\?\?/)).not.toBeInTheDocument()
    expect(within(ld).getByLabelText(/repo root/i)).toHaveValue('/Users/amad/DevVault/kaidera-os')
    const paul = screen.getByTestId('ws-row-paul')
    expect(within(paul).getByLabelText(/repo root/i)).toHaveValue('/Users/amad/DevVault/paul')
    const asw = screen.getByTestId('ws-row-asw-connect')
    expect(within(asw).getByLabelText(/repo root/i)).toHaveValue('/Users/amad/DevVault/asw-connect')
  })

  it('saving a NON-selected project’s row POSTs to THAT project (not the selected one)', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'Workspace')

    // edit paul's row while kaidera-os is the selected project
    const paul = screen.getByTestId('ws-row-paul')
    const input = within(paul).getByLabelText(/repo root/i)
    await user.clear(input)
    await user.type(input, '/new/paul/path')
    await user.click(within(paul).getByRole('button', { name: /save folder/i }))

    // it POSTs to paul's OWN project — the multi-project fix (not 'kaidera-os')
    await waitFor(() =>
      expect(client.setWorkspace).toHaveBeenCalledWith('paul', { repo_root: '/new/paul/path' }),
    )
  })

  it('editing + saving shows previous → new for that row', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'Workspace')

    const ld = screen.getByTestId('ws-row-kaidera-os')
    const input = within(ld).getByLabelText(/repo root/i)
    await user.clear(input)
    await user.type(input, '/new/path')
    await user.click(within(ld).getByRole('button', { name: /save folder/i }))

    await waitFor(() =>
      expect(client.setWorkspace).toHaveBeenCalledWith('kaidera-os', { repo_root: '/new/path' }),
    )
    const ok = await within(ld).findByTestId('ws-success')
    expect(ok.textContent).toContain('/old/path')
    expect(ok.textContent).toContain('/new/path')
  })

  it('Save is disabled until a row changes (per-row dirty)', async () => {
    const user = userEvent.setup()
    renderView()
    await openTab(user, 'Workspace')
    const paul = screen.getByTestId('ws-row-paul')
    expect(within(paul).getByRole('button', { name: /save folder/i })).toBeDisabled()
    await user.type(within(paul).getByLabelText(/repo root/i), '/x')
    expect(within(paul).getByRole('button', { name: /save folder/i })).toBeEnabled()
  })

  it('falls back to the single selected project when no projects list is provided', async () => {
    const user = userEvent.setup()
    renderView({ projects: undefined })
    await openTab(user, 'Workspace')
    // degrades to the selected project's row only (back-compat)
    expect(document.querySelectorAll('[data-ws-project-row]')).toHaveLength(1)
    expect(screen.getByTestId('ws-row-kaidera-os')).toBeInTheDocument()
  })
})

describe('SettingsView — Extensions tab', () => {
  it('lists installed pack extensions and toggles the pack helper file', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    renderView({ client })

    await openTab(user, 'Extensions')
    expect(await screen.findByText('Project-pack extensions')).toBeInTheDocument()
    expect(client.listProjectPacks).toHaveBeenCalledWith('/Users/amad/DevVault/kaidera-os')
    expect(screen.getByText('Basic Project Pack')).toBeInTheDocument()
    expect(screen.getByText('Enabled, restart required')).toBeInTheDocument()
    expect(screen.getByText('Package portals')).toBeInTheDocument()
    expect(screen.getByText('/portal/operator-chat')).toBeInTheDocument()
    expect(screen.getByText('Ready')).toBeInTheDocument()
    expect(screen.getByText('Canonical stream replay')).toBeInTheDocument()
    expect(screen.getByText('/runstate/stream?project={project}&run={run_id}')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Disable basic_project_pack\.example_worker/i }))

    expect(client.setProjectPackExtension).toHaveBeenCalledWith({
      repo_root: '/Users/amad/DevVault/kaidera-os',
      pack_key: 'basic-project-pack',
      module: 'basic_project_pack.example_worker',
      enabled: false,
    })
    expect(await screen.findByText('Disabled')).toBeInTheDocument()
  })

  it('asks for a repo root before managing extensions', async () => {
    const user = userEvent.setup()
    renderView({ projectRow: projectRow({ repo_root: null }) })
    await openTab(user, 'Extensions')
    expect(await screen.findByText(/Set this project.*repo root/i)).toBeInTheDocument()
  })
})

describe('SettingsView — Cortex tab', () => {
  it('renders the connection + registry info from the project row', async () => {
    const user = userEvent.setup()
    const { client } = renderView()
    await openTab(user, 'Cortex')

    expect(screen.getByText('Connection')).toBeInTheDocument()
    // the project hex is GONE from the new identity model — the connection card no longer shows it.
    expect(screen.queryByText('5872')).not.toBeInTheDocument()
    expect(screen.queryByText('Project hex')).not.toBeInTheDocument()
    expect(screen.getByText('/Users/amad/DevVault/kaidera-os')).toBeInTheDocument()
    expect(await screen.findByText('Ingestion models')).toBeInTheDocument()
    expect(screen.getByDisplayValue('nvidia/llama-nemotron-embed-vl-1b-v2:free')).toBeInTheDocument()
    expect(screen.getByText(/new vector space/i)).toBeInTheDocument()
    expect(await screen.findByText('Embedding coverage')).toBeInTheDocument()
    expect(screen.getByText('not loaded')).toBeInTheDocument()
    expect(screen.getByText(/loaded on demand/i)).toBeInTheDocument()
    expect(client.cortexEmbeddingBacklog).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /Load coverage/i }))
    expect(screen.getByText('3 pending')).toBeInTheDocument()
    expect(screen.getAllByText('knowledge').length).toBeGreaterThan(0)
    expect(await screen.findByText('Run-state restart health')).toBeInTheDocument()
    expect(screen.getByText('Restart-survivable')).toBeInTheDocument()
    expect(screen.getByText('Live health')).toBeInTheDocument()
    expect(screen.getByText('6-layer Cortex')).toBeInTheDocument()
    expect(screen.getByText('Verbatim Storage')).toBeInTheDocument()
  })

  it('saves Cortex ingestion model settings through the admin config proxy', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    const onSaved = vi.fn()
    renderView({ client, onSaved })

    await openTab(user, 'Cortex')
    await screen.findByText('Ingestion models')
    const rerankModel = screen.getByLabelText(/Rerank model/i)
    await user.clear(rerankModel)
    await user.type(rerankModel, 'nv-rerank-qa-mistral-4b:1')
    await user.click(screen.getByRole('button', { name: /Save models/i }))

    expect(client.setCortexConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        embedding_provider: 'openrouter',
        embedding_model: 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
        embedding_dims: 768,
        rerank_enabled: true,
        rerank_provider: 'nvidia',
        rerank_model: 'nv-rerank-qa-mistral-4b:1',
      }),
    )
    expect(await screen.findByText(/New ingestion calls/i)).toBeInTheDocument()
    expect(onSaved).toHaveBeenCalled()
  })

  it('dry-runs a project embedding backfill through the console proxy', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    const onSaved = vi.fn()
    renderView({ client, onSaved })

    await openTab(user, 'Cortex')
    await user.click(await screen.findByRole('button', { name: /Load coverage/i }))
    await screen.findByText('3 pending')
    await user.click(screen.getByRole('button', { name: /Dry run/i }))

    expect(client.cortexEmbeddingBackfill).toHaveBeenCalledWith(
      'kaidera-os',
      expect.objectContaining({
        table: 'all',
        limit: 100,
        dry_run: true,
        async_job: false,
      }),
    )
    expect(await screen.findByText(/processed 3 row/i)).toBeInTheDocument()
    expect(onSaved).toHaveBeenCalled()
  })

  it('renders the live health read-out from the console JSON /cortex/health', async () => {
    const user = userEvent.setup()
    // stub the same-origin console health endpoint that returns the documented fields
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        status: 'healthy',
        surface_version: 'v2.5',
        event_backend: 'postgres',
        rls_enforced: true,
        base_url: 'http://localhost:8501',
        project: 'kaidera-os',
      }),
    } as Response)
    vi.stubGlobal('fetch', fetchFn)
    try {
      renderView()
      await openTab(user, 'Cortex')
      expect(await screen.findByText('v2.5')).toBeInTheDocument()
      expect(screen.getByText('postgres')).toBeInTheDocument()
      // the tab reads the CONSOLE JSON health endpoint (the fix), not the bare /health
      expect(fetchFn).toHaveBeenCalledWith(
        expect.stringMatching(/^\/cortex\/health/),
        expect.anything(),
      )
      // a healthy status renders (no more "unreachable")
      expect(screen.getByText('healthy')).toBeInTheDocument()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('shows a real "unreachable" when the console health reports it (never a 404 crash)', async () => {
    const user = userEvent.setup()
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'unreachable', error: 'connect timed out', base_url: 'http://localhost:8501', project: 'kaidera-os' }),
    } as Response)
    vi.stubGlobal('fetch', fetchFn)
    try {
      renderView()
      await openTab(user, 'Cortex')
      expect(await screen.findByText('unreachable')).toBeInTheDocument()
      expect(screen.getByText(/connect timed out/i)).toBeInTheDocument()
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
