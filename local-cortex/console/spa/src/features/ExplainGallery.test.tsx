import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ExplainGallery } from './ExplainGallery'
import type { ExplainClient } from './ExplainView'
import type { ExplainListItem, RunTranscript } from '../api'

const HTML_DOC = '<!DOCTYPE html><html><body>saved doc</body></html>'

function runTranscript(over: Partial<RunTranscript> = {}): RunTranscript {
  return {
    run_id: 'run-7',
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

function item(over: Partial<ExplainListItem> = {}): ExplainListItem {
  return {
    artifact_id: 'a7',
    run_id: 'run-7',
    caption: 'Explains the dispatcher',
    source_file: 'explain/run-7.html',
    modality: 'html',
    ...over,
  }
}

function fakeClient(over: Partial<ExplainClient> = {}): ExplainClient {
  return {
    postExplain: vi.fn(),
    getExplainList: vi.fn<(...a: unknown[]) => Promise<ExplainListItem[]>>().mockResolvedValue([item()]),
    run: vi.fn<(...a: unknown[]) => Promise<RunTranscript>>().mockResolvedValue(runTranscript()),
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ExplainGallery', () => {
  it('lists past explainers (caption) from getExplainList', async () => {
    const onView = vi.fn()
    render(<ExplainGallery project="kaidera-os" client={fakeClient()} onView={onView} />)
    await waitFor(() => expect(screen.getByText('Explains the dispatcher')).toBeInTheDocument())
  })

  it('View fetches the run and hands the full HTML (concat output spans) to onView', async () => {
    const user = userEvent.setup()
    const onView = vi.fn()
    const client = fakeClient()
    render(<ExplainGallery project="kaidera-os" client={client} onView={onView} />)

    const viewBtn = await screen.findByRole('button', { name: 'View' })
    await user.click(viewBtn)

    await waitFor(() => expect(client.run).toHaveBeenCalledWith('run-7'))
    await waitFor(() =>
      expect(onView).toHaveBeenCalledWith(HTML_DOC, 'Explains the dispatcher', 'run-7'),
    )
  })

  it('offers a project-scoped tarball export for each saved explainer', async () => {
    render(<ExplainGallery project="kaidera-os" client={fakeClient()} onView={vi.fn()} />)
    const link = await screen.findByRole('link', { name: /export explains the dispatcher archive/i })
    expect(link).toHaveAttribute('href', '/explain/kaidera-os/export/run-7')
    expect(link).toHaveAttribute('download')
  })

  it('renders a clean empty state when there are no explainers', async () => {
    const onView = vi.fn()
    const client = fakeClient({
      getExplainList: vi.fn<(...a: unknown[]) => Promise<ExplainListItem[]>>().mockResolvedValue([]),
    })
    render(<ExplainGallery project="kaidera-os" client={client} onView={onView} />)
    await waitFor(() => expect(screen.getByText(/no explainers yet/i)).toBeInTheDocument())
  })

  it('disables View for an item with no derivable run_id', async () => {
    const onView = vi.fn()
    const client = fakeClient({
      getExplainList: vi
        .fn<(...a: unknown[]) => Promise<ExplainListItem[]>>()
        .mockResolvedValue([item({ run_id: null, source_file: 'explain/bad', caption: 'broken' })]),
    })
    render(<ExplainGallery project="kaidera-os" client={client} onView={onView} />)
    const btn = await screen.findByRole('button', { name: 'View' })
    expect(btn).toBeDisabled()
  })

  it('shows the target (kind · path) for a run_state-enumerated item', async () => {
    const onView = vi.fn()
    const client = fakeClient({
      getExplainList: vi.fn<(...a: unknown[]) => Promise<ExplainListItem[]>>().mockResolvedValue([
        item({ caption: 'Explains main.py', target_kind: 'file', target_path: 'app/main.py', status: 'ok' }),
      ]),
    })
    render(<ExplainGallery project="kaidera-os" client={client} onView={onView} />)
    // the target kind + path render on the row
    await waitFor(() => expect(screen.getByText(/file · app\/main\.py/)).toBeInTheDocument())
  })

  it('shows a generating chip for a still-running explain run', async () => {
    const onView = vi.fn()
    const client = fakeClient({
      getExplainList: vi.fn<(...a: unknown[]) => Promise<ExplainListItem[]>>().mockResolvedValue([
        item({ caption: 'Explaining…', status: 'running', artifact_id: null }),
      ]),
    })
    render(<ExplainGallery project="kaidera-os" client={client} onView={onView} />)
    await waitFor(() => expect(screen.getByText(/generating/i)).toBeInTheDocument())
  })

  it('shows an errored chip for a failed explain run', async () => {
    const onView = vi.fn()
    const client = fakeClient({
      getExplainList: vi.fn<(...a: unknown[]) => Promise<ExplainListItem[]>>().mockResolvedValue([
        item({ caption: 'Explain file: x.py', status: 'error', artifact_id: null }),
      ]),
    })
    render(<ExplainGallery project="kaidera-os" client={client} onView={onView} />)
    await waitFor(() => expect(screen.getByText(/errored/i)).toBeInTheDocument())
  })

  it('shows a recovered chip when the server salvaged valid HTML from an errored run', async () => {
    const client = fakeClient({
      getExplainList: vi.fn<(...a: unknown[]) => Promise<ExplainListItem[]>>().mockResolvedValue([
        item({ caption: 'Saved explainer', status: 'recovered', artifact_id: null }),
      ]),
    })
    render(<ExplainGallery project="kaidera-os" client={client} onView={vi.fn()} />)
    await waitFor(() => expect(screen.getByText(/· recovered/i)).toBeInTheDocument())
  })
})
