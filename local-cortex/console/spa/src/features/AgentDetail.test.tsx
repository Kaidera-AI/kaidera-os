/**
 * AgentDetail — the composer wiring (step 2 of the feature-gap build) + the
 * per-agent config POPUP (the CTO's "config behind a Config button next to the
 * live indicator" directive — v0.1.42).
 *
 * These tests cover the NEW behavior: the bottom composer posts a chat turn, the
 * reply is pointed at the run-state transcript (the run_id from the `run` frame pins
 * the pane), and the echo bubble shows immediately. The pre-existing live-SSE +
 * run-rail rendering is covered structurally by the hook/component tests; here we
 * focus on the send → pin → stream-in flow through the real AgentDetail.
 *
 * The config editor is NO LONGER inline: a Config button sits next to the live
 * indicator in the header, and pressing it opens a glass modal that hosts the
 * AgentConfigEditor. The config-popup suite below asserts the button placement, the
 * closed-by-default modal (config controls absent until opened), open/close
 * (×/backdrop/Esc), and that a save inside the modal still posts + fires the roster
 * refresh — and that the inline panel is GONE when the modal is closed.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AgentDetail } from './AgentDetail'
import { api } from '../api'
import { readActiveChatRun, writeActiveChatRun } from '../api/activeChatRun'
import type {
  AgentConfigCatalog,
  AgentDetail as AgentDetailT,
  RunBoard,
  RunStateFrame,
  RunTranscript,
} from '../api'

/** The harness→model+reasoning catalog the in-pane config editor repopulates from. */
function configCatalog(): AgentConfigCatalog {
  return {
    project: 'kaidera-os',
    harnesses: [
      { value: 'claude-code', label: 'Claude Code', model_source: 'fixed', lane: 'subscription' },
      { value: 'pi', label: 'pi', model_source: 'fixed', lane: 'subscription' },
    ],
    models_by_harness: {
      'claude-code': [
        { value: 'opus', label: 'Opus 4.8' },
        { value: 'sonnet', label: 'Sonnet 4.7' },
      ],
      pi: [{ value: 'gpt-5.5', label: 'GPT-5.5' }],
    },
    reasoning_by_harness: {
      'claude-code': [
        { value: 'low', label: 'low' },
        { value: 'high', label: 'high' },
      ],
      pi: [{ value: 'off', label: 'off' }],
    },
    default_harness: 'claude-code',
    default_model: 'opus',
  }
}

// A no-op EventSource for tests that only need the subscription to stay idle.
class NoopEventSource {
  url: string
  constructor(url: string) {
    this.url = url
  }
  addEventListener() {}
  removeEventListener() {}
  close() {}
}

/** A controllable EventSource for reconnect/terminal tests. */
class ControlledEventSource {
  static instances: ControlledEventSource[] = []
  url: string
  closed = false
  private listeners: Record<string, ((ev: unknown) => void)[]> = {}

  constructor(url: string) {
    this.url = url
    ControlledEventSource.instances.push(this)
  }
  addEventListener(type: string, cb: (ev: unknown) => void) {
    ;(this.listeners[type] ||= []).push(cb)
  }
  removeEventListener(type: string, cb: (ev: unknown) => void) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((c) => c !== cb)
  }
  close() {
    this.closed = true
  }
  emit(type: string, ev: unknown) {
    for (const cb of this.listeners[type] ?? []) cb(ev)
  }
}

/** A ReadableStream emitting the given SSE string chunks. */
function sseStream(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  let i = 0
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(enc.encode(chunks[i]))
        i += 1
      } else {
        controller.close()
      }
    },
  })
}

function detail(): AgentDetailT {
  return {
    project: 'kaidera-os',
    agent: {
      name: 'ren',
      display_name: 'Ren',
      initials: 'RE',
      role: 'cpo',
      model: 'opus',
      model_label: 'Opus',
      harness: 'claude-code',
      harness_label: 'Claude Code',
      thinking: null,
      writer_scope: null,
      capabilities: [],
      row_sub: '',
      is_test: false,
      interactive: true,
      designation_override: false,
      cpo_tag: true,
    },
    designation: 'interactive',
    role: 'cpo',
    registry_designation: 'interactive',
    config_view: {
      name: 'ren',
      display_name: 'Ren',
      role: 'cpo',
      designation: 'interactive',
      reg_designation: 'interactive',
      harness: 'claude-code',
      harness_label: 'Claude Code',
      model: 'opus',
      reasoning: 'high',
      reg_harness: 'claude-code',
      reg_harness_label: 'Claude Code',
      reg_model: 'sonnet',
      reg_reasoning: null,
      reg_role: 'cpo',
      ov_harness: false,
      ov_model: true,
      ov_reasoning: true,
      ov_designation: true,
      ov_role: true,
      has_override: true,
    },
  }
}

function transcript(runId: string, over: Partial<RunTranscript> = {}): RunTranscript {
  return {
    run_id: runId,
    project: 'demo-project',
    agent: 'ren',
    agent_display: 'Ren',
    handoff_id: null,
    handoff_short: null,
    model: 'opus',
    harness: 'claude-code',
    status: 'running',
    running: true,
    started_ts: null,
    updated_ts: null,
    started_ago: '1s',
    updated_ago: '1s',
    status_label: 'running',
    error: null,
    ended_ts: null,
    ended_ago: '',
    segments: [{ kind: 'output', text: 'Still working.' }],
    body: 'Still working.',
    truncated: false,
    ...over,
  }
}

function runstateFrame(runId: string, selected: RunTranscript | null): RunStateFrame {
  return {
    project: 'demo-project',
    agent: 'ren',
    wake_run_id: runId,
    running: selected?.running ? 1 : 0,
    count: selected ? 1 : 0,
    selected_id: selected ? runId : null,
    selected,
    html: '',
  }
}

const emptyBoard: RunBoard = {
  project: 'kaidera-os',
  active: [],
  active_count: 0,
  recent: [],
  recent_count: 0,
}

afterEach(() => {
  ControlledEventSource.instances = []
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('AgentDetail — chat composer wiring', () => {
  it('shows a visible chat-area working indicator while the selected agent has a running turn', async () => {
    vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())

    render(
      <AgentDetail
        project="kaidera-os"
        agent="ren"
        runBoard={{
          ...emptyBoard,
          active: [
            {
              run_id: 'run-active',
              project: 'kaidera-os',
              agent: 'ren',
              agent_display: 'Ren',
              handoff_id: null,
              handoff_short: null,
              model: 'opus',
              harness: 'claude-code',
              status: 'running',
              running: true,
              started_ts: null,
              updated_ts: null,
              started_ago: 'now',
              updated_ago: 'now',
              status_label: 'running',
            },
          ],
          active_count: 1,
        }}
      />,
    )

    expect(await screen.findByText(/Ren is working/i)).toBeInTheDocument()
  })

  it('keeps a run-level Stop button visible for a running interactive worker', async () => {
    vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    const cancelRun = vi.spyOn(api, 'cancelRun').mockResolvedValue({
      run_id: 'run-active',
      cancelled: true,
      status: 'cancelled',
    })

    render(
      <AgentDetail
        project="kaidera-os"
        agent="ren"
        runBoard={{
          ...emptyBoard,
          active: [
            {
              run_id: 'run-active',
              project: 'kaidera-os',
              agent: 'ren',
              agent_display: 'Ren',
              handoff_id: null,
              handoff_short: null,
              model: 'opus',
              harness: 'claude-code',
              status: 'running',
              running: true,
              started_ts: null,
              updated_ts: null,
              started_ago: 'now',
              updated_ago: 'now',
              status_label: 'running',
            },
          ],
          active_count: 1,
        }}
      />,
    )

    fireEvent.click(await screen.findByRole('button', { name: /^stop$/i }))

    await waitFor(() => expect(cancelRun).toHaveBeenCalledWith('run-active'))
  })

  it('enables image picking when the resolved config is a vision-capable pi model', async () => {
    vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
    const d = detail()
    d.agent.harness = 'pi'
    d.agent.model = 'gpt-5.4'
    d.config_view.harness = 'pi'
    d.config_view.model = 'gpt-5.4'
    vi.spyOn(api, 'agentDetail').mockResolvedValue(d)
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())

    render(<AgentDetail project="kaidera-os" agent="ren" runBoard={emptyBoard} />)

    const input = (await screen.findByTestId('attachment-input')) as HTMLInputElement
    expect(input.accept).toContain('image/*')
  })

  it('posts a chat turn and points the transcript at the run_id', async () => {
    vi.stubGlobal('EventSource', ControlledEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())

    // The chat SSE: a run frame (the run_id to pin) + a streamed delta + done.
    const chatSpy = vi.spyOn(api, 'chat').mockResolvedValue({
      ok: true,
      status: 200,
      body: sseStream([
        'event: run\ndata: {"run_id":"run-xyz"}\n\n',
        'event: delta\ndata: {"text":"On it."}\n\n',
        'event: done\ndata: {}\n\n',
      ]),
    } as unknown as Response)

    const runSpy = vi.spyOn(api, 'run').mockResolvedValue(transcript('run-xyz'))

    render(<AgentDetail project="kaidera-os" agent="ren" runBoard={emptyBoard} />)

    // Type a message and send.
    // target the composer by its placeholder (the pane is clean: header + run rail +
    // transcript + composer; the config lives behind the Config button's modal)
    const ta = await screen.findByPlaceholderText(/talk to/i)
    fireEvent.change(ta, { target: { value: 'please start' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    // 1. The chat route was POSTed with (project, agent, message, sessionId, clientRunId,
    //    attachmentIds). The clientRunId is now ALWAYS minted up front (so the pane pins the
    //    run immediately, regardless of the streamed frame's timing); with no attachments
    //    the attachmentIds stay undefined.
    await waitFor(() =>
      expect(chatSpy).toHaveBeenCalledWith(
        'kaidera-os', 'ren', 'please start', expect.any(String), expect.any(String), undefined,
        expect.anything(),  // the AbortSignal threaded for the Stop control (v0.1.167)
      ),
    )

    // 2. The pane follows the captured run through run-state SSE immediately. The
    //    optimistic row remains queued, so it must not probe durable detail before
    //    the backend has created that record.
    await waitFor(() =>
      expect(ControlledEventSource.instances.some((es) => es.url.includes('run=run-xyz'))).toBe(true),
    )
    expect(await screen.findByText('On it.')).toBeInTheDocument()
    expect(runSpy).not.toHaveBeenCalled()
  })

  it('restores a persisted active chat run on reload and follows it via runstate SSE', async () => {
    const sessionId = 'session-restore'
    const runId = '11111111-1111-4111-8111-111111111111'
    localStorage.setItem('kaidera-os:chat-session:demo-project:ren', sessionId)
    writeActiveChatRun('demo-project', 'ren', sessionId, runId)
    vi.stubGlobal('EventSource', ControlledEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    const runSpy = vi.spyOn(api, 'run').mockResolvedValue(transcript(runId))

    render(<AgentDetail project="demo-project" agent="ren" runBoard={emptyBoard} />)

    await waitFor(() =>
      expect(ControlledEventSource.instances.some((es) => es.url.includes(`run=${runId}`))).toBe(true),
    )
    const restoredStream = ControlledEventSource.instances.find((es) => es.url.includes(`run=${runId}`))
    expect(restoredStream).toBeDefined()

    restoredStream?.emit('open', {})
    restoredStream?.emit('runstate', { data: JSON.stringify(runstateFrame(runId, transcript(runId))) })

    expect(await screen.findByText('Still working.')).toBeInTheDocument()
    expect(runSpy).not.toHaveBeenCalled()
  })

  it('clears the persisted active chat run when the restored stream reaches terminal', async () => {
    const sessionId = 'session-terminal'
    const runId = '22222222-2222-4222-8222-222222222222'
    localStorage.setItem('kaidera-os:chat-session:demo-project:ren', sessionId)
    writeActiveChatRun('demo-project', 'ren', sessionId, runId)
    vi.stubGlobal('EventSource', ControlledEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    vi.spyOn(api, 'run').mockResolvedValue(transcript(runId))

    render(<AgentDetail project="demo-project" agent="ren" runBoard={emptyBoard} />)

    await waitFor(() =>
      expect(ControlledEventSource.instances.some((es) => es.url.includes(`run=${runId}`))).toBe(true),
    )
    const restoredStream = ControlledEventSource.instances.find((es) => es.url.includes(`run=${runId}`))
    expect(restoredStream).toBeDefined()

    const terminal = transcript(runId, {
      status: 'ok',
      running: false,
      status_label: 'completed',
      ended_ago: 'now',
      body: 'Done.',
      segments: [{ kind: 'output', text: 'Done.' }],
    })
    restoredStream?.emit('open', {})
    restoredStream?.emit('runstate', { data: JSON.stringify(runstateFrame(runId, terminal)) })

    await waitFor(() => expect(readActiveChatRun('demo-project', 'ren', sessionId)).toBeNull())
  })

  it('clears and unpins a restored active chat run when the stream reports it missing', async () => {
    const sessionId = 'session-missing'
    const runId = '33333333-3333-4333-8333-333333333333'
    localStorage.setItem('kaidera-os:chat-session:demo-project:ren', sessionId)
    writeActiveChatRun('demo-project', 'ren', sessionId, runId)
    vi.stubGlobal('EventSource', ControlledEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    vi.spyOn(api, 'run').mockRejectedValue(new Error('not found'))

    render(<AgentDetail project="demo-project" agent="ren" runBoard={emptyBoard} />)

    await waitFor(() =>
      expect(ControlledEventSource.instances.some((es) => es.url.includes(`run=${runId}`))).toBe(true),
    )
    const restoredStream = ControlledEventSource.instances.find((es) => es.url.includes(`run=${runId}`))
    restoredStream?.emit('runstate', { data: JSON.stringify(runstateFrame(runId, null)) })

    await waitFor(() => expect(readActiveChatRun('demo-project', 'ren', sessionId)).toBeNull())
    await waitFor(() =>
      expect(
        ControlledEventSource.instances[
          ControlledEventSource.instances.length - 1
        ].url.includes(`run=${runId}`),
      ).toBe(false),
    )
  })

  it('surfaces tasks + sub-agent counts in the status line when the turn reports them', async () => {
    vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    vi.spyOn(api, 'run').mockResolvedValue({
      run_id: 'r', project: 'kaidera-os', agent: 'ren', agent_display: 'Ren',
      handoff_id: null, handoff_short: null, model: null, harness: null,
      status: 'running', running: true, started_ts: null, updated_ts: null,
      started_ago: '', updated_ago: '', status_label: 'running', error: null,
      ended_ts: null, ended_ago: '', segments: [], body: '', truncated: false,
    } as RunTranscript)

    vi.spyOn(api, 'chat').mockResolvedValue({
      ok: true,
      status: 200,
      body: sseStream([
        'event: run\ndata: {"run_id":"r"}\n\n',
        'event: tasks\ndata: [{"content":"a","status":"completed"},{"content":"b","status":"in_progress"},{"content":"c","status":"pending"}]\n\n',
        'event: subagent\ndata: {"label":"Investigate"}\n\n',
        'event: done\ndata: {}\n\n',
      ]),
    } as unknown as Response)

    render(<AgentDetail project="kaidera-os" agent="ren" runBoard={emptyBoard} />)
    const ta = await screen.findByPlaceholderText(/talk to/i)
    fireEvent.change(ta, { target: { value: 'plan and delegate' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    // The status line shows "1/3 tasks" and "1 sub-agent" once the frames arrive.
    expect(await screen.findByText(/1\/3 tasks/)).toBeInTheDocument()
    expect(await screen.findByText(/1 sub-agent/)).toBeInTheDocument()
  })

  it('shows a clean error bubble when the chat stream emits an error frame', async () => {
    vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    vi.spyOn(api, 'run').mockResolvedValue({
      run_id: 'r',
      project: 'kaidera-os',
      agent: 'ren',
      agent_display: 'Ren',
      handoff_id: null,
      handoff_short: null,
      model: null,
      harness: null,
      status: 'running',
      running: true,
      started_ts: null,
      updated_ts: null,
      started_ago: '',
      updated_ago: '',
      status_label: 'running',
      error: null,
      ended_ts: null,
      ended_ago: '',
      segments: [],
      body: '',
      truncated: false,
    } as RunTranscript)

    vi.spyOn(api, 'chat').mockResolvedValue({
      ok: true,
      status: 200,
      body: sseStream([
        'event: run\ndata: {"run_id":"r"}\n\n',
        'event: error\ndata: {"message":"model not available","category":"model_unavailable"}\n\n',
        'event: done\ndata: {}\n\n',
      ]),
    } as unknown as Response)

    render(<AgentDetail project="kaidera-os" agent="ren" runBoard={emptyBoard} />)
    // the composer textarea (the pane now also has the config editor's role input, so
    // target the composer by its placeholder rather than the generic textbox role)
    const ta = await screen.findByPlaceholderText(/talk to/i)
    fireEvent.change(ta, { target: { value: 'hi' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    expect(await screen.findByText('model not available')).toBeInTheDocument()
  })
})

describe('AgentDetail — per-agent config POPUP (Config button + modal)', () => {
  /** Boot the pane for the selected agent with the config-catalog + detail stubbed. */
  function mountPane(props: Partial<Parameters<typeof AgentDetail>[0]> = {}) {
    vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    return render(
      <AgentDetail project="kaidera-os" agent="ren" runBoard={emptyBoard} {...props} />,
    )
  }

  it('renders a Config button NEXT TO the live indicator, with the config controls closed by default', async () => {
    mountPane()

    // the header is up once the agent name has painted
    await screen.findByRole('heading', { name: /ren/i })

    // a Config button is in the header…
    const configBtn = screen.getByRole('button', { name: /config/i })
    expect(configBtn).toBeInTheDocument()

    // …sitting right beside the "live" indicator (same header control cluster).
    // The live indicator is the element titled "live stream: <status>"; its parent
    // div is the cluster that also holds the Config button.
    const liveIndicator = document.querySelector('[title^="live stream:"]') as HTMLElement
    expect(liveIndicator).toBeTruthy()
    const cluster = liveIndicator.parentElement as HTMLElement
    expect(within(cluster).getByRole('button', { name: /config/i })).toBe(configBtn)

    // the modal is NOT shown initially → the config controls are absent from the DOM
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/harness/i)).not.toBeInTheDocument()
    // and there is no inline config panel left behind in the pane
    expect(document.querySelector('[data-agent-config-editor]')).toBeNull()
  })

  it('opens the modal with the AgentConfigEditor (harness/model/reasoning/role preset/role) when Config is clicked', async () => {
    const user = userEvent.setup()
    mountPane()
    await screen.findByRole('heading', { name: /ren/i })

    await user.click(screen.getByRole('button', { name: /config/i }))

    // the modal dialog is now open…
    const dialog = await screen.findByRole('dialog')
    // …and hosts the full config editor for THIS agent, with current values
    const editor = (await within(dialog).findByLabelText(/harness/i)).closest(
      '[data-agent-config-editor]',
    ) as HTMLElement
    expect(editor).toBeTruthy()
    expect((within(editor).getByLabelText(/harness/i) as HTMLSelectElement).value).toBe('claude-code')
    expect((within(editor).getByLabelText(/^model$/i) as HTMLSelectElement).value).toBe('opus')
    expect((within(editor).getByLabelText(/reasoning/i) as HTMLSelectElement).value).toBe('high')
    expect((within(editor).getByLabelText(/role preset/i) as HTMLSelectElement).value).toBe('interactive-lead')
    expect(within(editor).getByLabelText(/^role$/i)).toHaveValue('cpo')
    // the registry hint + an override dot survive the move into the modal
    expect(within(editor).getAllByText(/registry:/i).length).toBeGreaterThan(0)
    expect(within(editor).getAllByTestId('override-dot').length).toBeGreaterThan(0)
  })

  it('repopulates model + reasoning options when the harness changes inside the modal', async () => {
    const user = userEvent.setup()
    mountPane()
    await screen.findByRole('heading', { name: /ren/i })
    await user.click(screen.getByRole('button', { name: /config/i }))
    const dialog = await screen.findByRole('dialog')

    await user.selectOptions(within(dialog).getByLabelText(/harness/i), 'pi')

    const modelValues = Array.from(
      (within(dialog).getByLabelText(/^model$/i) as HTMLSelectElement).options,
    ).map((o) => o.value)
    expect(modelValues).toContain('gpt-5.5')
    expect(modelValues).not.toContain('opus')
  })

  it('closes the modal via the × close button', async () => {
    const user = userEvent.setup()
    mountPane()
    await screen.findByRole('heading', { name: /ren/i })
    await user.click(screen.getByRole('button', { name: /config/i }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('button', { name: /close/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // back to the clean pane: no config controls present
    expect(screen.queryByLabelText(/harness/i)).not.toBeInTheDocument()
  })

  it('closes the modal on a backdrop click', async () => {
    const user = userEvent.setup()
    mountPane()
    await screen.findByRole('heading', { name: /ren/i })
    await user.click(screen.getByRole('button', { name: /config/i }))
    await screen.findByRole('dialog')

    await user.click(screen.getByTestId('modal-backdrop'))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('closes the modal on Escape', async () => {
    const user = userEvent.setup()
    mountPane()
    await screen.findByRole('heading', { name: /ren/i })
    await user.click(screen.getByRole('button', { name: /config/i }))
    await screen.findByRole('dialog')

    fireEvent.keyDown(window, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('saving inside the modal POSTs the override for the selected agent + refetches + shows Saved', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
    const detailSpy = vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    const saveSpy = vi
      .spyOn(api, 'setAgentConfig')
      .mockResolvedValue({ project: 'kaidera-os', agent: 'ren', override: {}, designation: 'interactive', ok: true })

    render(<AgentDetail project="kaidera-os" agent="ren" runBoard={emptyBoard} />)
    await screen.findByRole('heading', { name: /ren/i })
    await user.click(screen.getByRole('button', { name: /config/i }))
    const dialog = await screen.findByRole('dialog')

    await user.selectOptions(within(dialog).getByLabelText(/^model$/i), 'sonnet')
    await user.click(within(dialog).getByRole('button', { name: /save config/i }))

    await waitFor(() =>
      expect(saveSpy).toHaveBeenCalledWith(
        'kaidera-os',
        'ren',
        expect.objectContaining({ model: 'sonnet' }),
      ),
    )
    // refetch-on-success: the agent's detail is re-fetched (config-view comes off it)
    await waitFor(() => expect(detailSpy.mock.calls.length).toBeGreaterThan(1))
    // the modal stays open after save so the user sees the confirmation
    await waitFor(() => expect(within(dialog).getByText(/saved/i)).toBeInTheDocument())
  })

  it('saving a role-preset worker-type change in the modal fires the roster-regroup refresh (onConfigSaved)', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('EventSource', NoopEventSource as unknown as typeof EventSource)
    vi.spyOn(api, 'agentDetail').mockResolvedValue(detail())
    vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    vi.spyOn(api, 'setAgentConfig').mockResolvedValue({
      project: 'kaidera-os',
      agent: 'ren',
      override: {},
      designation: 'autonomous',
      ok: true,
    })
    const onConfigSaved = vi.fn()

    render(
      <AgentDetail
        project="kaidera-os"
        agent="ren"
        runBoard={emptyBoard}
        onConfigSaved={onConfigSaved}
      />,
    )
    await screen.findByRole('heading', { name: /ren/i })
    await user.click(screen.getByRole('button', { name: /config/i }))
    const dialog = await screen.findByRole('dialog')

    await user.selectOptions(within(dialog).getByLabelText(/role preset/i), 'ai-worker')
    await user.click(within(dialog).getByRole('button', { name: /save config/i }))

    await waitFor(() => expect(onConfigSaved).toHaveBeenCalled())
  })

  it('does not render a Config button (or fetch the catalog) when no agent is selected', () => {
    const catalogSpy = vi.spyOn(api, 'configCatalog').mockResolvedValue(configCatalog())
    render(<AgentDetail project="kaidera-os" agent={null} runBoard={emptyBoard} />)
    expect(screen.getByText(/select a worker/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /config/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/harness/i)).not.toBeInTheDocument()
    expect(catalogSpy).not.toHaveBeenCalled()
  })
})
