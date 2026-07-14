import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ExplainView } from './ExplainView'
import type { ExplainClient } from './ExplainView'
import type { ExplainListItem, ExplainStartResult, RunTranscript } from '../api'
// Raw source of the three explain files (Vite `?raw`) for the security guard test — the
// model-generated HTML must NEVER reach the SPA DOM via a raw-HTML prop / innerHTML write.
import explainViewSrc from './ExplainView.tsx?raw'
import explainGallerySrc from './ExplainGallery.tsx?raw'
import explainFrameSrc from './ExplainFrame.tsx?raw'

const HTML_DOC = '<!DOCTYPE html><html><head><title>Explains main.py</title></head><body>hi</body></html>'

function runTranscript(over: Partial<RunTranscript> = {}): RunTranscript {
  return {
    run_id: 'run-1',
    project: 'kaidera-os',
    agent: 'console',
    agent_display: 'console',
    handoff_id: null,
    handoff_short: null,
    model: null,
    harness: null,
    status: 'ok',
    running: false,
    started_ts: null,
    updated_ts: null,
    started_ago: '',
    updated_ago: '',
    status_label: 'completed',
    error: null,
    ended_ts: null,
    ended_ago: '',
    segments: [{ kind: 'output', text: HTML_DOC }],
    body: HTML_DOC,
    truncated: false,
    ...over,
  }
}

function fakeClient(over: Partial<ExplainClient> = {}): ExplainClient {
  return {
    postExplain: vi
      .fn<(...a: unknown[]) => Promise<ExplainStartResult>>()
      .mockResolvedValue({ run_id: 'run-1', accepted: true, error: null }),
    getExplainList: vi.fn<(...a: unknown[]) => Promise<ExplainListItem[]>>().mockResolvedValue([]),
    run: vi.fn<(...a: unknown[]) => Promise<RunTranscript>>().mockResolvedValue(runTranscript()),
    ...over,
  }
}

/** The rendered explainer iframe (sandboxed) — found by its accessible title. */
function explainerIframe(): HTMLIFrameElement | null {
  return (
    (screen.queryByTitle('Code explainer') as HTMLIFrameElement | null) ??
    (document.querySelector('iframe') as HTMLIFrameElement | null)
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

/** Open the collapsed "Advanced" disclosure that holds the kind picker + per-kind inputs. */
async function openAdvanced(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /advanced/i }))
}

describe('ExplainView — target picker', () => {
  it('opens project-bound: the kind picker + drill-down inputs are hidden until Advanced', () => {
    render(<ExplainView project="kaidera-os" client={fakeClient()} />)
    // The default flow is the one-click project explainer — no kind select, no path field.
    expect(screen.queryByLabelText('Explain target kind')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('File path')).not.toBeInTheDocument()
    // The primary button leads with the project explainer.
    expect(screen.getByRole('button', { name: /generate project explainer/i })).toBeInTheDocument()
  })

  it('renders the kind selector with the project-level target plus code targets (under Advanced)', async () => {
    const user = userEvent.setup()
    render(<ExplainView project="kaidera-os" client={fakeClient()} />)
    await openAdvanced(user)
    const select = screen.getByLabelText('Explain target kind')
    const opts = within(select).getAllByRole('option').map((o) => (o as HTMLOptionElement).value)
    expect(opts).toEqual(['project', 'file', 'blast', 'dir', 'diff'])
  })

  it('shows the project, path, fn_name, and git_rev inputs by target mode (under Advanced)', async () => {
    const user = userEvent.setup()
    render(<ExplainView project="kaidera-os" client={fakeClient()} />)
    await openAdvanced(user)
    const select = screen.getByLabelText('Explain target kind')

    // file → path
    await user.selectOptions(select, 'file')
    expect(screen.getByLabelText('File path')).toBeInTheDocument()
    expect(screen.queryByLabelText('Function name')).not.toBeInTheDocument()

    // blast → fn_name (path gone)
    await user.selectOptions(select, 'blast')
    expect(screen.getByLabelText('Function name')).toBeInTheDocument()
    expect(screen.queryByLabelText('File path')).not.toBeInTheDocument()

    // dir → directory path
    await user.selectOptions(select, 'dir')
    expect(screen.getByLabelText('Directory path')).toBeInTheDocument()

    // diff → git revision (optional)
    await user.selectOptions(select, 'diff')
    expect(screen.getByLabelText(/git revision/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('Function name')).not.toBeInTheDocument()

    // project → no manual path; it uses the configured repo_root
    await user.selectOptions(select, 'project')
    expect(screen.getByText(/configured repo root/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/file path/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/directory path/i)).not.toBeInTheDocument()
  })
})

describe('ExplainView — generate + stream + render', () => {
  it('Advanced Generate POSTs the kind+path to postExplain', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<ExplainView project="kaidera-os" client={client} />)

    await openAdvanced(user)
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'file')
    await user.type(screen.getByLabelText('File path'), 'app/main.py')
    await user.click(screen.getByRole('button', { name: /generate explainer/i }))

    await waitFor(() => expect(client.postExplain).toHaveBeenCalled())
    expect(client.postExplain).toHaveBeenCalledWith('kaidera-os', { kind: 'file', path: 'app/main.py' })
  })

  it('project-level Generate (default, no typing) posts EXACTLY the project target', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<ExplainView project="kaidera-os" client={client} />)

    // Default mode is 'project' — one click, no kind selection, no typing.
    await user.click(screen.getByRole('button', { name: /generate project explainer/i }))

    await waitFor(() => expect(client.postExplain).toHaveBeenCalled())
    expect(client.postExplain).toHaveBeenCalledWith('kaidera-os', { kind: 'project' })
    // No stale path/fn/harness/model leaks into the body (backend resolves the lead's routing).
    const mock = vi.mocked(client.postExplain)
    const body = mock.mock.calls[0][1] as unknown as Record<string, unknown>
    expect(Object.keys(body)).toEqual(['kind'])
    expect(body).not.toHaveProperty('path')
    expect(body).not.toHaveProperty('fn_name')
    expect(body).not.toHaveProperty('harness')
    expect(body).not.toHaveProperty('model')
  })

  it('does not send harness/model from a stale Advanced override on a project run', async () => {
    const user = userEvent.setup()
    const client = fakeClient()
    render(<ExplainView project="kaidera-os" client={client} />)

    // Operator opens Advanced, picks file, fills a harness/model override, then switches
    // back to the project target — the project run must NOT carry the stale override.
    await openAdvanced(user)
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'file')
    await user.type(screen.getByLabelText('Harness override'), 'codex')
    await user.type(screen.getByLabelText('Model override'), 'gpt-x')
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'project')
    await user.click(screen.getByRole('button', { name: /generate project explainer/i }))

    await waitFor(() => expect(client.postExplain).toHaveBeenCalled())
    expect(client.postExplain).toHaveBeenCalledWith('kaidera-os', { kind: 'project' })
  })

  it('blocks Generate until the required field is filled (advanced file kind)', async () => {
    const user = userEvent.setup()
    render(<ExplainView project="kaidera-os" client={fakeClient()} />)
    // Default project mode → primary button is enabled (no required field).
    expect(screen.getByRole('button', { name: /generate project explainer/i })).not.toBeDisabled()
    // file kind with empty path → disabled
    await openAdvanced(user)
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'file')
    expect(screen.getByRole('button', { name: /generate explainer/i })).toBeDisabled()
  })

  it('streams progress while the run is non-terminal (transcript, not iframe yet)', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      run: vi
        .fn<(...a: unknown[]) => Promise<RunTranscript>>()
        .mockResolvedValue(
          runTranscript({
            status: 'running',
            running: true,
            status_label: 'running',
            segments: [{ kind: 'output', text: '<!DOCTYPE html>' }],
          }),
        ),
    })
    render(<ExplainView project="kaidera-os" client={client} />)
    await openAdvanced(user)
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'file')
    await user.type(screen.getByLabelText('File path'), 'app/main.py')
    await user.click(screen.getByRole('button', { name: /generate explainer/i }))

    // The run is followed (progress strip), and the document iframe is NOT shown yet.
    await waitFor(() => expect(client.run).toHaveBeenCalledWith('run-1', expect.anything()))
    expect(explainerIframe()).toBeNull()
  })

  it('on terminal OK renders the FULL HTML in a sandboxed iframe (srcDoc + sandbox)', async () => {
    const user = userEvent.setup()
    const client = fakeClient() // run resolves to an `ok` transcript with the full HTML
    render(<ExplainView project="kaidera-os" client={client} />)
    await openAdvanced(user)
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'file')
    await user.type(screen.getByLabelText('File path'), 'app/main.py')
    await user.click(screen.getByRole('button', { name: /generate explainer/i }))

    const iframe = await waitFor(() => {
      const el = explainerIframe()
      expect(el).not.toBeNull()
      return el as HTMLIFrameElement
    })
    // SECURITY: srcDoc carries the FULL document; sandbox is allow-scripts WITHOUT
    // allow-same-origin; no src attribute.
    expect(iframe.getAttribute('srcdoc')).toBe(HTML_DOC)
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts')
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-same-origin')
    expect(iframe.hasAttribute('src')).toBe(false)
    expect(iframe.getAttribute('referrerpolicy')).toBe('no-referrer')
    const exportLink = screen.getByRole('link', { name: /export/i })
    expect(exportLink).toHaveAttribute('href', '/explain/kaidera-os/export/run-1')
    expect(exportLink).toHaveAttribute('download')
  })

  it('surfaces a clean error state on a terminal error run', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      run: vi
        .fn<(...a: unknown[]) => Promise<RunTranscript>>()
        .mockResolvedValue(
          runTranscript({ status: 'error', running: false, status_label: 'errored', error: 'generation was empty', segments: [] }),
        ),
    })
    render(<ExplainView project="kaidera-os" client={client} />)
    await openAdvanced(user)
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'file')
    await user.type(screen.getByLabelText('File path'), 'app/main.py')
    await user.click(screen.getByRole('button', { name: /generate explainer/i }))

    await waitFor(() => expect(screen.getByText(/could not be generated/i)).toBeInTheDocument())
    expect(screen.getByText('generation was empty')).toBeInTheDocument()
    expect(explainerIframe()).toBeNull()
  })

  it('surfaces a clean error when the host harness-service rejects (accepted=false)', async () => {
    const user = userEvent.setup()
    const client = fakeClient({
      postExplain: vi
        .fn<(...a: unknown[]) => Promise<ExplainStartResult>>()
        .mockResolvedValue({ run_id: 'run-1', accepted: false, error: 'host harness-service returned 503' }),
    })
    render(<ExplainView project="kaidera-os" client={client} />)
    await openAdvanced(user)
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'file')
    await user.type(screen.getByLabelText('File path'), 'app/main.py')
    await user.click(screen.getByRole('button', { name: /generate explainer/i }))

    await waitFor(() => expect(screen.getByText(/could not start the explainer/i)).toBeInTheDocument())
    // The run is NOT followed when the host rejected.
    expect(client.run).not.toHaveBeenCalled()
  })

  it('renders nothing-to-do hint when no project is selected', () => {
    render(<ExplainView project={null} client={fakeClient()} />)
    expect(screen.getByText(/select a project/i)).toBeInTheDocument()
  })
})

describe('ExplainView — compose with Graph (initialTarget pre-fill)', () => {
  it('seeds a project-level target handed over from the Dashboard (Advanced stays collapsed)', () => {
    render(
      <ExplainView
        project="kaidera-os"
        client={fakeClient()}
        initialTarget={{ kind: 'project', value: '', nonce: 1 }}
      />,
    )
    // A project seed is the primary flow — Advanced stays collapsed (no kind select shown).
    expect(screen.queryByLabelText('Explain target kind')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /generate project explainer/i })).not.toBeDisabled()
  })

  it('seeds the picker to a file target handed over from the Graph view (auto-expands Advanced)', () => {
    render(
      <ExplainView
        project="kaidera-os"
        client={fakeClient()}
        initialTarget={{ kind: 'file', value: 'app/cortex_client.py', nonce: 1 }}
      />,
    )
    // A file sub-target auto-expands Advanced so the seeded path input is visible…
    const select = screen.getByLabelText('Explain target kind') as HTMLSelectElement
    expect(select.value).toBe('file')
    // …and the path is pre-filled with the node's path.
    const path = screen.getByLabelText('File path') as HTMLInputElement
    expect(path.value).toBe('app/cortex_client.py')
  })

  it('seeds a blast target (function node) into the fn_name field (auto-expands Advanced)', () => {
    render(
      <ExplainView
        project="kaidera-os"
        client={fakeClient()}
        initialTarget={{ kind: 'blast', value: 'explain_one', nonce: 1 }}
      />,
    )
    const fn = screen.getByLabelText('Function name') as HTMLInputElement
    expect(fn.value).toBe('explain_one')
  })

  it('re-seeds when the SAME node is handed over again (nonce bump)', async () => {
    const { rerender } = render(
      <ExplainView
        project="kaidera-os"
        client={fakeClient()}
        initialTarget={{ kind: 'file', value: 'a.py', nonce: 1 }}
      />,
    )
    const user = userEvent.setup()
    // The user edits the path away…
    const path = screen.getByLabelText('File path') as HTMLInputElement
    await user.clear(path)
    await user.type(path, 'edited.py')
    expect((screen.getByLabelText('File path') as HTMLInputElement).value).toBe('edited.py')
    // …then re-hands the same node (a new nonce) → the picker re-seeds.
    rerender(
      <ExplainView
        project="kaidera-os"
        client={fakeClient()}
        initialTarget={{ kind: 'file', value: 'a.py', nonce: 2 }}
      />,
    )
    expect((screen.getByLabelText('File path') as HTMLInputElement).value).toBe('a.py')
  })
})

describe('ExplainView — security guard (no raw-HTML injection)', () => {
  it('ExplainView + ExplainGallery + ExplainFrame source never use the raw-HTML React prop', () => {
    // The model-generated HTML must ONLY reach a sandboxed iframe — never the SPA DOM.
    // Assert the unsafe React escape hatch + innerHTML writes are ABSENT from all three
    // explain source files (the token is assembled so this guard file itself stays clean).
    const banned = 'dangerously' + 'SetInnerHTML'
    for (const src of [explainViewSrc, explainGallerySrc, explainFrameSrc]) {
      expect(src).not.toContain(banned)
      expect(src).not.toMatch(/\.innerHTML\s*=/)
    }
  })

  it('the rendered iframe uses srcDoc, never a src URL', async () => {
    const user = userEvent.setup()
    render(<ExplainView project="kaidera-os" client={fakeClient()} />)
    await openAdvanced(user)
    await user.selectOptions(screen.getByLabelText('Explain target kind'), 'file')
    await user.type(screen.getByLabelText('File path'), 'app/main.py')
    await user.click(screen.getByRole('button', { name: /generate explainer/i }))
    const iframe = await waitFor(() => {
      const el = explainerIframe()
      expect(el).not.toBeNull()
      return el as HTMLIFrameElement
    })
    expect(iframe.hasAttribute('srcdoc')).toBe(true)
    expect(iframe.hasAttribute('src')).toBe(false)
  })
})
