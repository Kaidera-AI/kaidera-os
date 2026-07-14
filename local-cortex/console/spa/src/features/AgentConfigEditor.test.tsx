/**
 * AgentConfigEditor — the per-agent config editor, RELOCATED from Settings→Configure
 * into the agent-detail middle pane (the CTO's "settings in the middle pane of the
 * agent" directive). It edits ONE agent: role preset · role · harness/model/reasoning
 * when model-backed, with the registry value as a hint + an
 * override indicator on fields that differ. Save → POST .../config, then refetch the
 * agent's config-view.
 *
 * These are the step-1 Configure assertions, MOVED here (the deliberate change): the
 * editor renders the selected agent's effective config, harness change repopulates the
 * model/reasoning options client-side, save posts the full override + refetches, the
 * registry hint + override dot render, and a role-preset save fires the roster-regroup
 * refresh (onSaved). Fakes the client (the existing SPA test style).
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AgentConfigEditor } from './AgentConfigEditor'
import type { AgentConfigEditorClient } from './AgentConfigEditor'
import type { AgentConfigCatalog, AgentConfigView, AgentConfigWriteResult, PromoteResult } from '../api'

// -- fixtures (mirrors the step-1 Configure catalog + config-view shapes) ------

function catalog(): AgentConfigCatalog {
  return {
    project: 'kaidera-os',
    harnesses: [
      { value: 'claude-code', label: 'Claude Code', model_source: 'claude-catalog', lane: 'subscription' },
      { value: 'codex', label: 'Codex', model_source: 'codex-catalog', lane: 'subscription' },
      { value: 'pi', label: 'pi', model_source: 'pi-catalog', lane: 'subscription' },
      { value: 'kaidera', label: 'kaidera', model_source: 'catalog', lane: 'api' },
    ],
    models_by_harness: {
      'claude-code': [
        { value: 'opus', label: 'Opus 4.8', reasoning_levels: ['low', 'medium', 'high', 'xhigh', 'max'] },
        { value: 'claude-opus-4-8[1m]', label: 'Opus 4.8 (1M context)' },
        { value: 'sonnet', label: 'Sonnet 4.7', reasoning_levels: ['low', 'medium', 'high', 'xhigh', 'max'] },
      ],
      codex: [
        {
          value: 'gpt-5.6-sol',
          label: 'GPT-5.6-Sol',
          reasoning_levels: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
        },
        {
          value: 'gpt-5.5',
          label: 'GPT-5.5',
          reasoning_levels: ['low', 'medium', 'high', 'xhigh'],
        },
      ],
      pi: [
        { value: 'gpt-5.5', label: 'GPT-5.5', provider: 'openai-codex', reasoning_levels: ['off', 'low', 'high'] },
        { value: 'gpt-5.3-codex-spark', label: 'GPT-5.3 Codex Spark', provider: 'openai-codex', reasoning_levels: ['off', 'low', 'high'] },
        {
          value: 'fireworks/accounts/fireworks/models/kimi-k2p6',
          label: 'Kimi K2.6',
          provider: 'fireworks',
          reasoning_levels: ['off', 'low', 'high'],
        },
      ],
      'kaidera': [
        {
          value: 'anthropic/claude-opus',
          label: 'Claude Opus',
          provider: 'anthropic',
          reasoning_levels: ['low', 'medium', 'high', 'max', 'xhigh'],
        },
        {
          value: 'openrouter/openai/gpt-5.5',
          label: 'GPT-5.5',
          provider: 'openrouter',
          reasoning_levels: ['low', 'medium', 'high'],
        },
        // a known NON-reasoning model — the dropdown must hide for it.
        {
          value: 'fireworks/kimi-k2',
          label: 'Kimi K2 (base)',
          provider: 'fireworks',
          reasoning_levels: [],
        },
      ],
    },
    // B3: per-model reasoning for the kaidera catalog lane (the SELECTED model's
    // own levels). A non-reasoning model is absent ⇒ the SPA hides the dropdown.
    reasoning_by_model: {
      'codex:gpt-5.5': [
        { value: 'low', label: 'low' },
        { value: 'medium', label: 'medium' },
        { value: 'high', label: 'high' },
        { value: 'xhigh', label: 'xhigh' },
      ],
      'pi:gpt-5.5': [
        { value: 'off', label: 'off' },
        { value: 'low', label: 'low' },
        { value: 'high', label: 'high' },
      ],
      'kaidera:anthropic/claude-opus': [
        { value: 'low', label: 'low' },
        { value: 'medium', label: 'medium' },
        { value: 'high', label: 'high' },
        { value: 'max', label: 'max' },
        { value: 'xhigh', label: 'xhigh' },
      ],
      'kaidera:openrouter/openai/gpt-5.5': [
        { value: 'low', label: 'low' },
        { value: 'medium', label: 'medium' },
        { value: 'high', label: 'high' },
      ],
    },
    reasoning_by_harness: {
      'claude-code': [
        { value: 'low', label: 'low' },
        { value: 'medium', label: 'medium' },
        { value: 'high', label: 'high' },
        { value: 'xhigh', label: 'xhigh' },
        { value: 'max', label: 'max' },
      ],
      codex: [
        { value: 'low', label: 'low' },
        { value: 'medium', label: 'medium' },
        { value: 'high', label: 'high' },
        { value: 'xhigh', label: 'xhigh' },
        { value: 'max', label: 'max' },
        { value: 'ultra', label: 'ultra' },
      ],
      pi: [
        { value: 'off', label: 'off' },
        { value: 'low', label: 'low' },
        { value: 'high', label: 'high' },
      ],
      'kaidera': [
        { value: 'low', label: 'low' },
        { value: 'high', label: 'high' },
      ],
    },
    default_harness: 'claude-code',
    default_model: 'claude-opus-4-8[1m]',
  }
}

/** Ren's resolved config-view — a model/reasoning/designation/role override over the registry. */
function configView(over: Partial<AgentConfigView> = {}): AgentConfigView {
  return {
    name: 'ren',
    display_name: 'Ren',
    role: 'CPO / lead',
    designation: 'interactive',
    reg_designation: 'interactive',
    harness: 'claude-code',
    harness_label: 'Claude Code',
    model: 'claude-opus-4-8[1m]',
    reasoning: 'high',
    reg_harness: 'claude-code',
    reg_harness_label: 'Claude Code',
    reg_model: 'opus',
    reg_reasoning: null,
    reg_role: 'CPO / lead',
    auto_dispatch: false,
    ov_harness: false,
    ov_model: true,
    ov_reasoning: true,
    ov_designation: true,
    ov_role: true,
    ov_auto_dispatch: false,
    has_override: true,
    ...over,
  }
}

function fakeClient(over: Partial<AgentConfigEditorClient> = {}): AgentConfigEditorClient {
  return {
    configCatalog: vi.fn(async () => catalog()),
    agentConfigView: vi.fn(async () => configView()),
    setAgentConfig: vi.fn(
      async (): Promise<AgentConfigWriteResult> => ({
        project: 'kaidera-os',
        agent: 'ren',
        override: {},
        designation: 'interactive',
        ok: true,
      }),
    ),
    promoteAgent: vi.fn(async (): Promise<PromoteResult> => ({ ok: true, error: null })),
    setAppSettings: vi.fn(async () => ({
      project: 'kaidera-os',
      settings: {},
      store_connected: true,
      ok: true,
    })),
    ...over,
  }
}

function renderEditor(props: Partial<Parameters<typeof AgentConfigEditor>[0]> = {}) {
  const client = props.client ?? fakeClient()
  const onSaved = props.onSaved ?? vi.fn()
  render(
    <AgentConfigEditor
      project="kaidera-os"
      agent="ren"
      client={client}
      onSaved={onSaved}
      {...props}
    />,
  )
  return { client, onSaved }
}

describe('AgentConfigEditor — the relocated per-agent config editor', () => {
  it('renders the selected agent’s role preset, harness/model/reasoning, and role with current values', async () => {
    renderEditor()

    const rolePreset = (await screen.findByLabelText(/role preset/i)) as HTMLSelectElement
    expect(rolePreset.value).toBe('interactive-lead')
    const harnessSel = (await screen.findByLabelText(/harness/i)) as HTMLSelectElement
    expect(harnessSel.value).toBe('claude-code')
    const modelSel = screen.getByLabelText(/^model$/i) as HTMLSelectElement
    expect(modelSel.value).toBe('claude-opus-4-8[1m]')
    const reasonSel = screen.getByLabelText(/reasoning/i) as HTMLSelectElement
    expect(reasonSel.value).toBe('high')
    expect(screen.queryByLabelText(/designation/i)).not.toBeInTheDocument()
    expect(screen.getByLabelText(/^role$/i)).toHaveValue('CPO / lead')
  })

  it('shows the registry hint + an override indicator on fields that differ', async () => {
    renderEditor()
    await screen.findByLabelText(/harness/i)

    // the registry value is shown as a hint (Ren's registry model is "opus")
    expect(screen.getAllByText(/registry:/i).length).toBeGreaterThan(0)
    expect(screen.getByText('opus')).toBeInTheDocument()
    // an override indicator + per-field override dots
    expect(screen.getByText(/^override$/i)).toBeInTheDocument()
    expect(screen.getAllByTestId('override-dot').length).toBeGreaterThan(0)
  })

  it('surfaces a subtle hint when the stored model was invalid for the harness (feature #99)', async () => {
    // The config-view reports it coerced an impossible stored model to the harness
    // default; the editor lands the dropdown on the COERCED (valid) model + shows the
    // "model was invalid for harness" hint with the original value.
    const client = fakeClient({
      agentConfigView: vi.fn(async () =>
        configView({
          model: 'claude-opus-4-8[1m]', // the coerced (valid) effective model
          model_coerced: true,
          model_invalid_original: 'gemini-3.1-pro-preview',
        }),
      ),
    })
    renderEditor({ client })

    const modelSel = (await screen.findByLabelText(/^model$/i)) as HTMLSelectElement
    // the dropdown lands on the coerced (valid) model, NEVER the impossible one
    expect(modelSel.value).toBe('claude-opus-4-8[1m]')
    // the subtle hint names the original invalid model
    expect(screen.getByText(/invalid for/i)).toBeInTheDocument()
    expect(screen.getByText(/gemini-3\.1-pro-preview/)).toBeInTheDocument()
  })

  it('shows NO coerced hint when the stored model is valid for the harness', async () => {
    renderEditor() // the default configView has no model_coerced flag
    await screen.findByLabelText(/^model$/i)
    expect(screen.queryByText(/invalid for/i)).not.toBeInTheDocument()
  })

  it('populates the model options harness-aware (claude-code set first, not pi-only)', async () => {
    renderEditor()
    const modelSel = (await screen.findByLabelText(/^model$/i)) as HTMLSelectElement
    const values = Array.from(modelSel.options).map((o) => o.value)
    expect(values).toContain('opus')
    expect(values).toContain('claude-opus-4-8[1m]')
    expect(values).toContain('sonnet')
    expect(values).not.toContain('gpt-5.3-codex-spark')
  })

  it('repopulates model + reasoning options CLIENT-SIDE when the harness changes', async () => {
    const user = userEvent.setup()
    renderEditor()
    const harnessSel = (await screen.findByLabelText(/harness/i)) as HTMLSelectElement

    await user.selectOptions(harnessSel, 'pi')

    const modelSel = screen.getByLabelText(/^model$/i) as HTMLSelectElement
    const modelValues = Array.from(modelSel.options).map((o) => o.value)
    expect(modelValues).toContain('gpt-5.5')
    expect(modelValues).toContain('gpt-5.3-codex-spark')
    expect(modelValues).toContain('fireworks/accounts/fireworks/models/kimi-k2p6')
    expect(modelValues).not.toContain('opus')

    const reasonSel = screen.getByLabelText(/reasoning/i) as HTMLSelectElement
    const reasonValues = Array.from(reasonSel.options).map((o) => o.value)
    expect(reasonValues).toContain('off') // pi's level set
    expect(reasonValues).not.toContain('max') // claude-code-only level gone
  })

  it('uses the selected Codex model’s discovered effort ladder', async () => {
    const user = userEvent.setup()
    renderEditor()
    await user.selectOptions(await screen.findByLabelText(/harness/i), 'codex')

    const modelSel = screen.getByLabelText(/^model$/i) as HTMLSelectElement
    expect(Array.from(modelSel.options).map((o) => o.value)).toContain('gpt-5.6-sol')
    expect(Array.from((screen.getByLabelText(/reasoning/i) as HTMLSelectElement).options).map((o) => o.value)).toContain(
      'ultra',
    )

    await user.selectOptions(modelSel, 'gpt-5.5')
    const efforts = Array.from((screen.getByLabelText(/reasoning/i) as HTMLSelectElement).options).map(
      (o) => o.value,
    )
    expect(efforts).toContain('xhigh')
    expect(efforts).not.toContain('off') // PI's same model id must not overwrite Codex.
    expect(efforts).not.toContain('max')
    expect(efforts).not.toContain('ultra')
  })

  it('groups catalog-lane (kaidera) models by provider into optgroups', async () => {
    const user = userEvent.setup()
    renderEditor()
    const harnessSel = (await screen.findByLabelText(/harness/i)) as HTMLSelectElement

    await user.selectOptions(harnessSel, 'kaidera')

    const modelSel = screen.getByLabelText(/^model$/i) as HTMLSelectElement
    const labels = Array.from(modelSel.querySelectorAll('optgroup')).map((g) => g.label)
    expect(labels).toContain('anthropic')
    expect(labels).toContain('openrouter')
  })

  it('groups PI dynamic models by the provider PI can see', async () => {
    const user = userEvent.setup()
    renderEditor()
    const harnessSel = (await screen.findByLabelText(/harness/i)) as HTMLSelectElement

    await user.selectOptions(harnessSel, 'pi')

    const modelSel = screen.getByLabelText(/^model$/i) as HTMLSelectElement
    const labels = Array.from(modelSel.querySelectorAll('optgroup')).map((g) => g.label)
    expect(labels).toContain('openai-codex')
    expect(labels).toContain('fireworks')
  })

  // B3: the kaidera reasoning dropdown shows the SELECTED MODEL's own levels.
  it('shows the selected kaidera model’s OWN reasoning levels (not the generic harness set)', async () => {
    const user = userEvent.setup()
    renderEditor()
    const harnessSel = (await screen.findByLabelText(/harness/i)) as HTMLSelectElement
    await user.selectOptions(harnessSel, 'kaidera')
    // kaidera defaults to the first model (anthropic/claude-opus → max/xhigh ladder).
    await user.selectOptions(screen.getByLabelText(/^model$/i), 'anthropic/claude-opus')

    const reasonSel = screen.getByLabelText(/reasoning/i) as HTMLSelectElement
    const values = Array.from(reasonSel.options).map((o) => o.value)
    expect(values).toEqual(['low', 'medium', 'high', 'max', 'xhigh'])
  })

  it('SWAPS the reasoning options when the kaidera model changes', async () => {
    const user = userEvent.setup()
    renderEditor()
    const harnessSel = (await screen.findByLabelText(/harness/i)) as HTMLSelectElement
    await user.selectOptions(harnessSel, 'kaidera')

    await user.selectOptions(screen.getByLabelText(/^model$/i), 'anthropic/claude-opus')
    let values = Array.from(
      (screen.getByLabelText(/reasoning/i) as HTMLSelectElement).options,
    ).map((o) => o.value)
    expect(values).toContain('max')

    // switch to the openrouter gpt-5.5 model → its ladder has NO max.
    await user.selectOptions(screen.getByLabelText(/^model$/i), 'openrouter/openai/gpt-5.5')
    values = Array.from(
      (screen.getByLabelText(/reasoning/i) as HTMLSelectElement).options,
    ).map((o) => o.value)
    expect(values).toEqual(['low', 'medium', 'high'])
    expect(values).not.toContain('max')
  })

  it('HIDES the reasoning dropdown for a non-reasoning kaidera model', async () => {
    const user = userEvent.setup()
    renderEditor()
    const harnessSel = (await screen.findByLabelText(/harness/i)) as HTMLSelectElement
    await user.selectOptions(harnessSel, 'kaidera')

    await user.selectOptions(screen.getByLabelText(/^model$/i), 'fireworks/kimi-k2')
    // the <select> is gone; a "doesn’t support reasoning" note is shown instead.
    expect(screen.queryByLabelText(/reasoning/i)).toBeNull()
    expect(screen.getByTestId('reasoning-not-supported')).toBeInTheDocument()
  })

  it('saving POSTs the full override payload for the selected agent, then refetches its config-view', async () => {
    const user = userEvent.setup()
    const { client, onSaved } = renderEditor()
    await screen.findByLabelText(/harness/i)

    await user.selectOptions(screen.getByLabelText(/harness/i), 'pi')
    await user.selectOptions(screen.getByLabelText(/^model$/i), 'gpt-5.5')
    await user.selectOptions(screen.getByLabelText(/reasoning/i), 'high')
    await user.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(client.setAgentConfig).toHaveBeenCalledWith(
        'kaidera-os',
        'ren',
        expect.objectContaining({ harness: 'pi', model: 'gpt-5.5', reasoning: 'high' }),
      ),
    )
    // refetch-on-success: the agent's config-view is re-fetched + onSaved fires (shell refresh)
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    await waitFor(() =>
      // (the FIRST call is the initial load; a later call is the refetch)
      expect((client.agentConfigView as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(1),
    )
  })

  it('adds an operator Claude Code model to the harness catalog and selects it', async () => {
    const user = userEvent.setup()
    const { client } = renderEditor()

    const custom = await screen.findByLabelText(/add claude model/i)
    await user.type(custom, 'claude-fable-5')
    await user.click(screen.getByRole('button', { name: /add & select/i }))

    await waitFor(() =>
      expect(client.setAppSettings).toHaveBeenCalledWith('kaidera-os', {
        harness_model_overrides: {
          'claude-code': [{ value: 'claude-fable-5', label: 'claude-fable-5' }],
        },
      }),
    )
    await waitFor(() =>
      expect(screen.getByLabelText(/^model$/i)).toHaveValue('claude-fable-5'),
    )
    expect(screen.getByRole('option', { name: 'claude-fable-5' })).toBeInTheDocument()
  })

  it('saving a role-preset type change still fires the roster-regroup refresh (onSaved)', async () => {
    const user = userEvent.setup()
    const { client, onSaved } = renderEditor()
    await screen.findByLabelText(/role preset/i)

    await user.selectOptions(screen.getByLabelText(/role preset/i), 'ai-worker')
    await user.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(client.setAgentConfig).toHaveBeenCalledWith(
        'kaidera-os',
        'ren',
        expect.objectContaining({ designation: 'autonomous' }),
      ),
    )
    // onSaved is the shell's regroup-the-agents-column refresh (same as it did from Settings)
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  it('shows an agent-level auto-run eligibility switch and saves the override', async () => {
    const user = userEvent.setup()
    const { client } = renderEditor()
    const toggle = await screen.findByRole('switch', { name: /allow auto-run when assigned/i })
    expect(toggle).toHaveAttribute('aria-checked', 'false')

    await user.click(toggle)
    await user.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(client.setAgentConfig).toHaveBeenCalledWith(
        'kaidera-os',
        'ren',
        expect.objectContaining({ auto_dispatch: 'true' }),
      ),
    )
  })

  it('role preset Orchestrator sets deterministic role/config and disables queued work', async () => {
    const user = userEvent.setup()
    const { client } = renderEditor()
    await screen.findByLabelText(/role preset/i)

    await user.selectOptions(screen.getByLabelText(/role preset/i), 'orchestrator')

    expect(screen.getByLabelText(/^role$/i)).toHaveValue('orchestrator')
    expect(screen.queryByLabelText(/designation/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/^model$/i)).not.toBeInTheDocument()
    expect(screen.getByRole('switch', { name: /allow auto-run when assigned/i })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: /save/i }))
    await waitFor(() =>
      expect(client.setAgentConfig).toHaveBeenCalledWith(
        'kaidera-os',
        'ren',
        expect.objectContaining({
          role: 'orchestrator',
          designation: 'deterministic',
          harness: '',
          model: '',
          reasoning: '',
          auto_dispatch: 'false',
        }),
      ),
    )
  })

  it('role preset PM AI Agent keeps the worker model-backed but non-interactive', async () => {
    const user = userEvent.setup()
    renderEditor()
    await screen.findByLabelText(/role preset/i)

    await user.selectOptions(screen.getByLabelText(/role preset/i), 'pm')

    expect(screen.getByLabelText(/^role$/i)).toHaveValue('pm')
    expect(screen.queryByLabelText(/designation/i)).not.toBeInTheDocument()
    expect(screen.getByLabelText(/^model$/i)).toBeInTheDocument()
  })

  it('deterministic role preset hides AI config and clears AI-only overrides on save', async () => {
    const user = userEvent.setup()
    const { client } = renderEditor()
    await screen.findByLabelText(/role preset/i)

    await user.selectOptions(screen.getByLabelText(/role preset/i), 'deterministic-worker')

    expect(screen.queryByLabelText(/^model$/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/harness/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/reasoning/i)).not.toBeInTheDocument()
    expect(screen.getByText(/no LLM\/model is attached/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(client.setAgentConfig).toHaveBeenCalledWith(
        'kaidera-os',
        'ren',
        expect.objectContaining({
          designation: 'deterministic',
          harness: '',
          model: '',
          reasoning: '',
        }),
      ),
    )
  })

  it('shows a save confirmation on success', async () => {
    const user = userEvent.setup()
    renderEditor()
    await screen.findByLabelText(/^model$/i)

    await user.selectOptions(screen.getByLabelText(/^model$/i), 'sonnet')
    await user.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(screen.getByText(/saved/i)).toBeInTheDocument())
  })

  it('surfaces a save error without crashing', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      setAgentConfig: vi.fn().mockRejectedValue(new Error('500 on /config')),
    })
    renderEditor({ client })
    await screen.findByLabelText(/^model$/i)

    await user.selectOptions(screen.getByLabelText(/^model$/i), 'sonnet')
    await user.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(screen.getByText(/couldn’t save/i)).toBeInTheDocument())
  })

  it('surfaces a soft ok:false save response without showing a false success', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      setAgentConfig: vi.fn().mockResolvedValue({
        project: 'kaidera-os',
        agent: 'ren',
        override: {},
        designation: 'interactive',
        ok: false,
        error: 'Only one deterministic orchestrator is allowed per project.',
      }),
    })
    renderEditor({ client })
    await screen.findByLabelText(/^model$/i)

    await user.selectOptions(screen.getByLabelText(/^model$/i), 'sonnet')
    await user.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() => expect(screen.getByText(/only one deterministic orchestrator/i)).toBeInTheDocument())
    expect(screen.queryByText(/saved/i)).not.toBeInTheDocument()
  })

  it('degrades to a hint when the catalog/config-view fail to load (never crashes)', async () => {
    const client = fakeClient({
      configCatalog: vi.fn().mockRejectedValue(new Error('404')),
      agentConfigView: vi.fn().mockRejectedValue(new Error('404')),
    })
    renderEditor({ client })
    expect(await screen.findByText(/couldn’t load/i)).toBeInTheDocument()
  })

  it('renders nothing actionable when no agent is selected', () => {
    const client = fakeClient()
    render(<AgentConfigEditor project="kaidera-os" agent={null} client={client} onSaved={vi.fn()} />)
    // no fetch fired, no controls
    expect(client.configCatalog).not.toHaveBeenCalled()
    expect(screen.queryByLabelText(/harness/i)).not.toBeInTheDocument()
  })
})

describe('AgentConfigEditor — explicit Promote to registry (feature-gap #81)', () => {
  it('Save is console-local: it posts the override and does NOT promote to the registry', async () => {
    const user = userEvent.setup()
    const { client } = renderEditor()
    await screen.findByLabelText(/^model$/i)

    await user.selectOptions(screen.getByLabelText(/^model$/i), 'sonnet')
    await user.click(screen.getByRole('button', { name: /save config/i }))

    await waitFor(() => expect(client.setAgentConfig).toHaveBeenCalled())
    // Save must NEVER trigger a registry promote (the boundary is the whole point).
    expect(client.promoteAgent).not.toHaveBeenCalled()
  })

  it('the Promote button calls promoteAgent and shows a promoted ✓ result', async () => {
    const user = userEvent.setup()
    const { client } = renderEditor()
    await screen.findByLabelText(/harness/i)

    await user.click(screen.getByRole('button', { name: /promote to registry/i }))

    await waitFor(() => expect(client.promoteAgent).toHaveBeenCalledWith('kaidera-os', 'ren'))
    expect(await screen.findByTestId('promote-result')).toHaveTextContent(/promoted/i)
    // Promoting must not have posted a config save (it's a distinct gesture).
    expect(client.setAgentConfig).not.toHaveBeenCalled()
  })

  it('a degraded promote shows the failure with the returned error (the local config is untouched)', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      promoteAgent: vi.fn(
        async (): Promise<PromoteResult> => ({ ok: false, error: 'Cortex is unreachable' }),
      ),
    })
    renderEditor({ client })
    await screen.findByLabelText(/harness/i)

    await user.click(screen.getByRole('button', { name: /promote to registry/i }))

    const result = await screen.findByTestId('promote-result')
    expect(result).toHaveTextContent(/registry sync failed/i)
    expect(result).toHaveTextContent(/Cortex is unreachable/i)
  })

  it('surfaces a thrown promote error without crashing', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      promoteAgent: vi.fn().mockRejectedValue(new Error('500 on /promote')),
    })
    renderEditor({ client })
    await screen.findByLabelText(/harness/i)

    await user.click(screen.getByRole('button', { name: /promote to registry/i }))
    expect(await screen.findByTestId('promote-result')).toHaveTextContent(/500 on \/promote/i)
  })

  it('no Deregister action without a registration client', async () => {
    renderEditor()
    await screen.findByLabelText(/harness/i)
    expect(screen.queryByRole('button', { name: /deregister/i })).not.toBeInTheDocument()
  })

  it('opens the deregister confirm + calls deregisterAgent + onRemoved on confirm', async () => {
    const user = userEvent.setup()
    const registrationClient = {
      deregisterAgent: vi.fn().mockResolvedValue({ ok: true, removed: true, agent: 'ren', error: null }),
    }
    const onRemoved = vi.fn()
    renderEditor({ registrationClient, onRemoved })
    await screen.findByLabelText(/harness/i)

    // open the confirm (the editor footer button)
    await user.click(screen.getByRole('button', { name: /deregister/i }))
    // confirm INSIDE the dialog (scope to avoid also matching the footer button)
    const dialog = await screen.findByRole('dialog', { name: /deregister worker/i })
    await user.click(within(dialog).getByRole('button', { name: /^deregister$/i }))

    await waitFor(() => expect(registrationClient.deregisterAgent).toHaveBeenCalledWith('kaidera-os', 'ren'))
    expect(onRemoved).toHaveBeenCalledTimes(1)
  })
})
