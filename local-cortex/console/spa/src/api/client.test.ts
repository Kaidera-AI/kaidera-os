import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  api,
  ApiError,
  explainExportUrl,
  explainHtmlFromRun,
  explainRunIdFromSourceFile,
  extractExplainHtml,
} from './client'
import type { RunTranscript } from './types'

function mockFetchOnce(body: unknown, ok = true, status = 200) {
  const fn = vi.fn().mockResolvedValue({
    ok,
    status,
    json: async () => body,
  } as Response)
  vi.stubGlobal('fetch', fn)
  return fn
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('api client URL construction', () => {
  it('hits the documented module paths with encoded params', async () => {
    const fetchFn = mockFetchOnce({ project: 'kaidera-os', interactive: [], autonomous: [] })

    await api.appVersion()
    expect(fetchFn).toHaveBeenCalledWith('/console/version', expect.anything())

    await api.updateStatus()
    expect(fetchFn).toHaveBeenLastCalledWith('/console/update-status', expect.anything())

    await api.updateJob()
    expect(fetchFn).toHaveBeenLastCalledWith('/console/update-job', expect.anything())

    await api.agents('kaidera-os')
    expect(fetchFn).toHaveBeenCalledWith('/agents/kaidera-os', expect.anything())

    await api.agentDetail('kaidera-os', 'ren')
    expect(fetchFn).toHaveBeenLastCalledWith('/agents/kaidera-os/ren/detail', expect.anything())

    await api.configCatalog('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith(
      '/agents/kaidera-os/config-catalog',
      expect.anything(),
    )

    await api.runBoard('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith('/runs/kaidera-os', expect.anything())

    await api.run('run-123')
    expect(fetchFn).toHaveBeenLastCalledWith('/runs/run/run-123', expect.anything())

    await api.runByHandoff('kaidera-os', 'abcd:5872')
    expect(fetchFn).toHaveBeenLastCalledWith(
      '/runs/kaidera-os/by-handoff/abcd%3A5872',
      expect.anything(),
    )

    await api.dispatchBoard('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith('/dispatch/kaidera-os/board', expect.anything())

    await api.usage('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith('/analytics/kaidera-os/usage', expect.anything())

    await api.flags('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith('/settings/kaidera-os/flags', expect.anything())
  })

  it('throws a typed ApiError on a non-ok response', async () => {
    mockFetchOnce({ detail: 'Not Found' }, false, 404)
    await expect(api.agents('nope')).rejects.toBeInstanceOf(ApiError)
    await expect(api.agents('nope')).rejects.toMatchObject({ status: 404 })
  })

  it('applyUpdate POSTs to the admin-gated update endpoint', async () => {
    const fetchFn = mockFetchOnce({
      accepted: true,
      already_running: false,
      job: { status: 'running', job_id: 'abc' },
    })

    const out = await api.applyUpdate()

    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    expect(path).toBe('/console/update/apply')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(JSON.parse(String(init.body))).toEqual({})
    expect(out.accepted).toBe(true)
  })

  it('cancelRun POSTs an empty body to /runs/run/{run_id}/cancel', async () => {
    const fetchFn = mockFetchOnce({ run_id: 'run/123', cancelled: true, status: 'cancelled' })

    const out = await api.cancelRun('run/123')

    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    expect(path).toBe('/runs/run/run%2F123/cancel')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(JSON.parse(String(init.body))).toEqual({})
    expect(out.cancelled).toBe(true)
  })
})

describe('api client settings WRITE methods (Track C)', () => {
  /** Read the (path, init) the client POSTed and the JSON body it sent. */
  function lastPost(fetchFn: ReturnType<typeof vi.fn>) {
    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    return { path, init, body: JSON.parse(String(init.body)) }
  }

  it('setFlags POSTs the flag payload to /settings/{project}/flags', async () => {
    const fetchFn = mockFetchOnce({
      project: 'kaidera-os',
      autonomous: true,
      propose_mode: false,
      ok: true,
    })
    const out = await api.setFlags('kaidera-os', { autonomous: true })
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/settings/kaidera-os/flags')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(body).toEqual({ autonomous: true })
    // returns the parsed authoritative flag state
    expect(out.autonomous).toBe(true)
    expect(out.ok).toBe(true)
  })

  it('setFlags can send propose_mode alone (partial update)', async () => {
    const fetchFn = mockFetchOnce({
      project: 'kaidera-os',
      autonomous: false,
      propose_mode: true,
      ok: true,
    })
    await api.setFlags('kaidera-os', { propose_mode: true })
    expect(lastPost(fetchFn).body).toEqual({ propose_mode: true })
  })

  it('setAppSetting POSTs {settings:{key:value}} to /settings/{project}/app', async () => {
    const fetchFn = mockFetchOnce({
      project: 'kaidera-os',
      settings: { default_harness: 'pi' },
      store_connected: true,
      ok: true,
    })
    const out = await api.setAppSetting('kaidera-os', 'default_harness', 'pi')
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/settings/kaidera-os/app')
    expect(init.method).toBe('POST')
    expect(body).toEqual({ settings: { default_harness: 'pi' } })
    expect(out.settings.default_harness).toBe('pi')
    expect(out.ok).toBe(true)
  })

  it('setAgentConfig POSTs {override} to /settings/{project}/agents/{agent}/config', async () => {
    const fetchFn = mockFetchOnce({
      project: 'kaidera-os',
      agent: 'bob',
      override: { model: 'claude-opus-4-8[1m]' },
      designation: '',
      ok: true,
    })
    const out = await api.setAgentConfig('kaidera-os', 'bob', {
      model: 'claude-opus-4-8[1m]',
      harness: '',
    })
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/settings/kaidera-os/agents/bob/config')
    expect(init.method).toBe('POST')
    expect(body).toEqual({ override: { model: 'claude-opus-4-8[1m]', harness: '' } })
    expect(out.override.model).toBe('claude-opus-4-8[1m]')
    expect(out.ok).toBe(true)
  })

  it('encodes params in write paths', async () => {
    const fetchFn = mockFetchOnce({ project: 'a/b', agent: 'x', override: {}, designation: '', ok: true })
    await api.setAgentConfig('a/b', 'x y', { role: 'r' })
    expect(lastPost(fetchFn).path).toBe('/settings/a%2Fb/agents/x%20y/config')
  })

  it('promoteAgent POSTs an empty body to /settings/{project}/agents/{agent}/promote', async () => {
    const fetchFn = mockFetchOnce({ ok: true, error: null })
    const out = await api.promoteAgent('kaidera-os', 'ren')
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/settings/kaidera-os/agents/ren/promote')
    expect(init.method).toBe('POST')
    expect(body).toEqual({})
    expect(out.ok).toBe(true)
  })

  it('promoteAgent encodes params + surfaces a graceful degraded echo', async () => {
    const fetchFn = mockFetchOnce({ ok: false, error: 'Cortex is unreachable' })
    const out = await api.promoteAgent('a/b', 'x y')
    expect(lastPost(fetchFn).path).toBe('/settings/a%2Fb/agents/x%20y/promote')
    expect(out.ok).toBe(false)
    expect(out.error).toContain('unreachable')
  })

  it('throws a typed ApiError when a write returns non-ok', async () => {
    mockFetchOnce({ detail: 'boom' }, false, 500)
    await expect(api.setFlags('kaidera-os', { autonomous: true })).rejects.toBeInstanceOf(ApiError)
    await expect(api.setFlags('kaidera-os', { autonomous: true })).rejects.toMatchObject({ status: 500 })
  })
})

describe('api client settings methods (System / Workspace / Cortex tabs)', () => {
  function lastPost(fetchFn: ReturnType<typeof vi.fn>) {
    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    return { path, init, body: JSON.parse(String(init.body)) }
  }

  it('systemSchema GETs /settings/{project}/system-schema', async () => {
    const fetchFn = mockFetchOnce({ project: 'kaidera-os', groups: [], store_connected: true })
    const out = await api.systemSchema('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith('/settings/kaidera-os/system-schema', expect.anything())
    expect(out.groups).toEqual([])
  })

  it('setAppSettings POSTs the BATCH {settings:{…}} (save-only-changed-keys)', async () => {
    const fetchFn = mockFetchOnce({ project: 'kaidera-os', settings: { a: 1, b: 2 }, store_connected: true, ok: true })
    await api.setAppSettings('kaidera-os', { a: 1, b: 2 })
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/settings/kaidera-os/app')
    expect(init.method).toBe('POST')
    expect(body).toEqual({ settings: { a: 1, b: 2 } })
  })

  it('setWorkspace POSTs {repo_root} to /workspace', async () => {
    const fetchFn = mockFetchOnce({
      project: 'kaidera-os', project_key: 'kaidera-os', ok: true,
      repo_root: '/new', previous_repo_root: '/old', error: null,
    })
    const out = await api.setWorkspace('kaidera-os', { repo_root: '/new' })
    const { path, body } = lastPost(fetchFn)
    expect(path).toBe('/settings/kaidera-os/workspace')
    expect(body).toEqual({ repo_root: '/new' })
    expect(out.previous_repo_root).toBe('/old')
    expect(out.repo_root).toBe('/new')
  })

  it('cortexConfig GETs the global Cortex platform config', async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      error: null,
      config: {
        embedding_provider: 'openrouter',
        embedding_model: 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
      },
    })
    const out = await api.cortexConfig()
    expect(fetchFn).toHaveBeenLastCalledWith('/cortex/config', expect.anything())
    expect(out.config.embedding_provider).toBe('openrouter')
  })

  it('setCortexConfig POSTs {config} to the global Cortex platform config', async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      error: null,
      config: { rerank_provider: 'nvidia', rerank_model: 'nv-rerank-qa-mistral-4b:1' },
    })
    await api.setCortexConfig({ rerank_provider: 'nvidia', rerank_model: 'nv-rerank-qa-mistral-4b:1' })
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/cortex/config')
    expect(init.method).toBe('POST')
    expect(body).toEqual({
      config: { rerank_provider: 'nvidia', rerank_model: 'nv-rerank-qa-mistral-4b:1' },
    })
  })

  it('cortexEmbeddingBacklog GETs project-scoped embedding coverage', async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      project: 'kaidera-os',
      backlog: { total: 3 },
      coverage: { knowledge: { total: 4, embedded: 2, backlog: 2, skipped: 0, pct: 50 } },
      error: null,
    })
    const out = await api.cortexEmbeddingBacklog('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith(
      '/cortex/embeddings/backlog?project=kaidera-os',
      expect.anything(),
    )
    expect(out.coverage.knowledge.backlog).toBe(2)
  })

  it('cortexEmbeddingBackfill POSTs the rebuild request through the console proxy', async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      project: 'kaidera-os',
      result: { status: 'queued', job_id: 'job-1' },
      error: null,
    })
    await api.cortexEmbeddingBackfill('kaidera-os', { table: 'knowledge', limit: 50, dry_run: true })
    const { path, body } = lastPost(fetchFn)
    expect(path).toBe('/cortex/embeddings/backfill?project=kaidera-os')
    expect(body).toEqual({ table: 'knowledge', limit: 50, dry_run: true })
  })
})

describe('api.chat (interactive chat — SSE POST)', () => {
  it('POSTs message= urlencoded to /agents/{p}/{a}/chat and returns the raw Response', async () => {
    // The chat route streams SSE; the client returns the Response so the caller can
    // read res.body (the legacy composer used fetch()+ReadableStream the same way).
    const fakeResp = { ok: true, status: 200, body: {} } as unknown as Response
    const fetchFn = vi.fn().mockResolvedValue(fakeResp)
    vi.stubGlobal('fetch', fetchFn)

    const out = await api.chat('kaidera-os', 'ren', 'hello there')

    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    expect(path).toBe('/agents/kaidera-os/ren/chat')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe(
      'application/x-www-form-urlencoded',
    )
    expect((init.headers as Record<string, string>)['Accept']).toBe('text/event-stream')
    // Body is the urlencoded message the backend's _read_posted_form accepts.
    expect(String(init.body)).toBe('message=hello%20there')
    // The raw Response is handed back (NOT json-parsed) for streaming.
    expect(out).toBe(fakeResp)
  })

  it('encodes the project + agent path params and the message body', async () => {
    const fakeResp = { ok: true, status: 200, body: {} } as unknown as Response
    const fetchFn = vi.fn().mockResolvedValue(fakeResp)
    vi.stubGlobal('fetch', fetchFn)

    await api.chat('a/b', 'x y', 'a & b = c?')
    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    expect(path).toBe('/agents/a%2Fb/x%20y/chat')
    expect(String(init.body)).toBe('message=a%20%26%20b%20%3D%20c%3F')
  })

  it('throws a typed ApiError on a non-ok chat response', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ ok: false, status: 500, body: null } as Response)
    vi.stubGlobal('fetch', fetchFn)
    await expect(api.chat('kaidera-os', 'ren', 'hi')).rejects.toBeInstanceOf(ApiError)
    await expect(api.chat('kaidera-os', 'ren', 'hi')).rejects.toMatchObject({ status: 500 })
  })

  it('includes client_run_id + attachment_ids in the chat body when given (step 6)', async () => {
    const fakeResp = { ok: true, status: 200, body: {} } as unknown as Response
    const fetchFn = vi.fn().mockResolvedValue(fakeResp)
    vi.stubGlobal('fetch', fetchFn)

    await api.chat('kaidera-os', 'ren', 'hi', 'sess-1', 'run-uuid', ['att-1', 'att-2'])
    const [, init] = fetchFn.mock.lastCall as [string, RequestInit]
    const body = String(init.body)
    expect(body).toContain('message=hi')
    expect(body).toContain('session_id=sess-1')
    expect(body).toContain('client_run_id=run-uuid')
    expect(body).toContain('attachment_ids=att-1%2Catt-2')
  })

  it('omits the attachment fields when there are none (back-compat body)', async () => {
    const fakeResp = { ok: true, status: 200, body: {} } as unknown as Response
    const fetchFn = vi.fn().mockResolvedValue(fakeResp)
    vi.stubGlobal('fetch', fetchFn)

    await api.chat('kaidera-os', 'ren', 'hi', 'sess-1')
    const [, init] = fetchFn.mock.lastCall as [string, RequestInit]
    const body = String(init.body)
    expect(body).toBe('message=hi&session_id=sess-1')
  })
})

describe('api.uploadAttachment (chat file-attachments — step 6)', () => {
  it('POSTs base64 JSON to /chat/upload and returns the client-safe echo', async () => {
    const fetchFn = mockFetchOnce({ attachment_id: 'att-9', filename: 'a.txt', size_bytes: 3 })

    const file = new File(['abc'], 'a.txt', { type: 'text/plain' })
    const out = await api.uploadAttachment('kaidera-os', 'ren', 'run-uuid', file)

    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    expect(path).toBe('/agents/kaidera-os/ren/chat/upload')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    const sent = JSON.parse(String(init.body)) as Record<string, unknown>
    expect(sent.run_id).toBe('run-uuid')
    expect(sent.filename).toBe('a.txt')
    expect(sent.content_type).toBe('text/plain')
    // base64 of "abc" is "YWJj".
    expect(sent.data).toBe('YWJj')
    expect(out).toEqual({ attachment_id: 'att-9', filename: 'a.txt', size_bytes: 3 })
  })

  it('throws a typed ApiError on a non-ok upload', async () => {
    mockFetchOnce({ error: 'too big' }, false, 400)
    const file = new File(['x'], 'x.txt', { type: 'text/plain' })
    await expect(
      api.uploadAttachment('kaidera-os', 'ren', 'run-uuid', file),
    ).rejects.toBeInstanceOf(ApiError)
  })
})

describe('api explain methods (the visual code explainer)', () => {
  it('postExplain POSTs the target body to /explain/{project} and returns the run', async () => {
    const fetchFn = mockFetchOnce({ run_id: 'run-77', accepted: true, error: null })
    const out = await api.postExplain('kaidera-os', { kind: 'file', path: 'app/main.py' })
    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    expect(path).toBe('/explain/kaidera-os')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(JSON.parse(String(init.body))).toEqual({ kind: 'file', path: 'app/main.py' })
    expect(out).toEqual({ run_id: 'run-77', accepted: true, error: null })
  })

  it('postExplain encodes the project key', async () => {
    const fetchFn = mockFetchOnce({ run_id: 'r', accepted: true, error: null })
    await api.postExplain('a/b', { kind: 'blast', fn_name: 'explain_one' })
    const [path] = fetchFn.mock.lastCall as [string, RequestInit]
    expect(path).toBe('/explain/a%2Fb')
  })

  it('getExplainList passes through the run_id the server now sends first-class (+ metadata fields)', async () => {
    // The gallery is enumerated from run_state — run_id is FIRST-CLASS in the payload,
    // with target/created_at/status from the run's metadata sidecar.
    const fetchFn = mockFetchOnce({
      artifacts: [
        {
          artifact_id: 'a1',
          run_id: 'run-1',
          caption: 'Explains main.py',
          source_file: 'explain/run-1.html',
          modality: 'html',
          target_kind: 'file',
          target_path: 'app/main.py',
          created_at: '2026-06-07T10:00:00',
          status: 'ok',
        },
      ],
    })
    const out = await api.getExplainList('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith('/explain/kaidera-os/list', expect.anything())
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({
      artifact_id: 'a1',
      run_id: 'run-1',
      target_kind: 'file',
      target_path: 'app/main.py',
      created_at: '2026-06-07T10:00:00',
      status: 'ok',
    })
  })

  it('getExplainList still DERIVES run_id from source_file when the server omits it (fallback)', async () => {
    const fetchFn = mockFetchOnce({
      artifacts: [
        { artifact_id: 'a1', caption: 'Explains main.py', source_file: 'explain/run-1.html', modality: 'html' },
        { artifact_id: 'a2', caption: 'Explains the blast', source_file: 'explain/run-2.html', modality: 'html' },
      ],
    })
    const out = await api.getExplainList('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith('/explain/kaidera-os/list', expect.anything())
    expect(out).toHaveLength(2)
    expect(out[0]).toMatchObject({ artifact_id: 'a1', run_id: 'run-1', source_file: 'explain/run-1.html' })
    expect(out[1].run_id).toBe('run-2')
  })

  it('getExplainList degrades to [] when the payload has no artifacts', async () => {
    mockFetchOnce({})
    const out = await api.getExplainList('kaidera-os')
    expect(out).toEqual([])
  })

  it('explainRunIdFromSourceFile parses explain/<id>.html and rejects other shapes', () => {
    expect(explainRunIdFromSourceFile('explain/abc-123.html')).toBe('abc-123')
    expect(explainRunIdFromSourceFile('chat/abc.html')).toBeNull()
    expect(explainRunIdFromSourceFile('')).toBeNull()
    expect(explainRunIdFromSourceFile(null)).toBeNull()
  })

  it('explainHtmlFromRun concatenates ONLY the output spans (skips input)', () => {
    const run = {
      run_id: 'r',
      status: 'ok',
      segments: [
        { kind: 'input', text: 'Explain file: app/main.py' },
        { kind: 'output', text: '<!DOCTYPE html><html>' },
        { kind: 'output', text: '<body>hi</body></html>' },
      ],
      body: 'ignored',
    } as unknown as RunTranscript
    expect(explainHtmlFromRun(run)).toBe('<!DOCTYPE html><html><body>hi</body></html>')
    expect(explainHtmlFromRun(null)).toBe('')
  })

  it('extracts a complete HTML document from harness progress chatter', () => {
    const html = '<!DOCTYPE html><html><body>hi</body></html>'
    expect(extractExplainHtml(`Using the HTML skill.\n${html}\nFinished.`)).toBe(html)
    expect(
      explainHtmlFromRun({
        segments: [
          { kind: 'output', text: 'Preparing.\n' },
          { kind: 'output', text: html },
        ],
      } as never),
    ).toBe(html)
  })

  it('builds a project-scoped encoded explainer export URL', () => {
    expect(explainExportUrl('a/b', 'run one')).toBe('/explain/a%2Fb/export/run%20one')
  })
})

describe('api graph methods (the knowledge/code-graph view)', () => {
  it('graph GETs the seed view at /graph/{project}', async () => {
    const fetchFn = mockFetchOnce({ nodes: [], edges: [], stats: {} })
    await api.graph('kaidera-os')
    expect(fetchFn).toHaveBeenCalledWith('/graph/kaidera-os', expect.anything())
  })

  it('graphSearch GETs /graph/{project}/search with the encoded term', async () => {
    const fetchFn = mockFetchOnce({ nodes: [], edges: [], stats: {} })
    await api.graphSearch('kaidera-os', 'cortex client')
    expect(fetchFn).toHaveBeenLastCalledWith(
      '/graph/kaidera-os/search?q=cortex%20client',
      expect.anything(),
    )
  })

  it('graph methods encode the project key', async () => {
    const fetchFn = mockFetchOnce({ nodes: [], edges: [], stats: {} })
    await api.graph('a/b')
    expect(fetchFn).toHaveBeenLastCalledWith('/graph/a%2Fb', expect.anything())
  })

  it('graph passes through the backend nodes/edges/stats payload', async () => {
    mockFetchOnce({
      nodes: [{ id: 'app/main.py', label: 'app/main.py', full: 'app/main.py', kind: 'code', etype: 'file', desc: '', hit: 1 }],
      edges: [{ id: 'e0', source: 'app/main.py', target: 'x', label: 'imports' }],
      stats: { own_nodes: 5868, rendered_nodes: 1 },
    })
    const out = await api.graph('kaidera-os')
    expect(out.nodes[0].kind).toBe('code')
    expect(out.edges[0].label).toBe('imports')
    expect(out.stats.own_nodes).toBe(5868)
  })
})

describe('api history method (the cross-agent activity timeline)', () => {
  it('history GETs /history/{project} with no query when no limit is given', async () => {
    const fetchFn = mockFetchOnce({ events: [], decisions: [], agent_count: 0 })
    await api.history('kaidera-os')
    expect(fetchFn).toHaveBeenCalledWith('/history/kaidera-os', expect.anything())
  })

  it('history forwards the limit as a query param', async () => {
    const fetchFn = mockFetchOnce({ events: [], decisions: [], agent_count: 0 })
    await api.history('kaidera-os', 42)
    expect(fetchFn).toHaveBeenLastCalledWith('/history/kaidera-os?limit=42', expect.anything())
  })

  it('history opts into decisions only when requested', async () => {
    const fetchFn = mockFetchOnce({ events: [], decisions: [], agent_count: 0 })
    await api.history('kaidera-os', undefined, undefined, { includeDecisions: true })
    expect(fetchFn).toHaveBeenLastCalledWith('/history/kaidera-os?include_decisions=1', expect.anything())
  })

  it('history encodes the project key', async () => {
    const fetchFn = mockFetchOnce({ events: [], decisions: [], agent_count: 0 })
    await api.history('a/b')
    expect(fetchFn).toHaveBeenLastCalledWith('/history/a%2Fb', expect.anything())
  })

  it('history passes through the backend events/decisions/agent_count payload', async () => {
    mockFetchOnce({
      events: [{ ts: '2026-06-07T12:00:30Z', ts_ago: '2m', agent: 'ada', role: 'assistant', kind: 'tool', kind_label: 'action', summary: 'ran exec_command · pytest -q' }],
      decisions: [{ ts: '', ts_ago: '', agent: '', summary: 'decided to degrade gracefully', source: 'decisions', category: 'architecture' }],
      agent_count: 3,
    })
    const out = await api.history('kaidera-os')
    expect(out.events[0].kind).toBe('tool')
    expect(out.events[0].summary).toContain('exec_command')
    expect(out.decisions[0].source).toBe('decisions')
    expect(out.agent_count).toBe(3)
  })

  it('runstateRestartStatus reads the restart-survival snapshot for a project', async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      project: 'kaidera-os',
      store: 'ok',
      active: [],
      counts: { active: 0, restart_survivable: 0, request_lived: 0, needs_reconcile: 0 },
      error: null,
    })
    const out = await api.runstateRestartStatus('kaidera-os')
    expect(fetchFn).toHaveBeenLastCalledWith(
      '/runstate/restart-status?project=kaidera-os',
      expect.anything(),
    )
    expect(out.store).toBe('ok')
  })
})

describe('api client registration WRITE methods (feature-gap #81)', () => {
  /** Read the (path, init) the client POSTed and the JSON body it sent. */
  function lastPost(fetchFn: ReturnType<typeof vi.fn>) {
    const [path, init] = fetchFn.mock.lastCall as [string, RequestInit]
    return { path, init, body: JSON.parse(String(init.body)) }
  }

  it('registerAgent POSTs the new-agent form to /agents/{project}/register', async () => {
    const fetchFn = mockFetchOnce({ ok: true, agent: 'newbie', role: 'qa', error: null })
    const out = await api.registerAgent('kaidera-os', {
      name: 'newbie',
      role: 'qa',
      harness: 'claude-code',
      model: 'opus',
      writer_scope: 'work',
    })
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/agents/kaidera-os/register')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(body).toEqual({
      name: 'newbie',
      role: 'qa',
      harness: 'claude-code',
      model: 'opus',
      writer_scope: 'work',
    })
    expect(out.ok).toBe(true)
    expect(out.agent).toBe('newbie')
  })

  it('deregisterAgent POSTs an empty body to /agents/{project}/{agent}/deregister', async () => {
    const fetchFn = mockFetchOnce({ ok: true, removed: true, agent: 'gone', error: null })
    const out = await api.deregisterAgent('kaidera-os', 'gone')
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/agents/kaidera-os/gone/deregister')
    expect(init.method).toBe('POST')
    expect(body).toEqual({})
    expect(out.removed).toBe(true)
  })

  it('registerProject POSTs the new-project form to /projects/register', async () => {
    const fetchFn = mockFetchOnce({ ok: true, project_key: 'demo', error: null })
    const out = await api.registerProject({
      project_key: 'demo',
      display_name: 'Demo',
      repo_root: '/abs/demo',
      project_pack_key: 'basic-project-pack',
    })
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/projects/register')
    expect(init.method).toBe('POST')
    expect(body).toEqual({
      project_key: 'demo',
      display_name: 'Demo',
      repo_root: '/abs/demo',
      project_pack_key: 'basic-project-pack',
    })
    expect(out.ok).toBe(true)
    expect(out.project_key).toBe('demo')
  })

  it('listProjectPacks reads installed packs for an absolute project folder', async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      packs: [{ key: 'basic-project-pack', name: 'Basic Project Pack', version: '0.1.0' }],
      error: null,
    })
    const out = await api.listProjectPacks('/abs/demo')
    expect(fetchFn).toHaveBeenLastCalledWith(
      '/project-packs?repo_root=%2Fabs%2Fdemo',
      expect.objectContaining({ headers: { Accept: 'application/json' } }),
    )
    expect(out.ok).toBe(true)
    expect(out.packs[0].key).toBe('basic-project-pack')
  })

  it('setProjectPackExtension POSTs one manifest-declared extension toggle', async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      pack: { key: 'basic-project-pack', name: 'Basic Project Pack', version: '0.1.0' },
      error: null,
    })
    const out = await api.setProjectPackExtension({
      repo_root: '/abs/demo',
      pack_key: 'basic-project-pack',
      module: 'basic_project_pack.example_worker',
      enabled: false,
    })
    const { path, init, body } = lastPost(fetchFn)
    expect(path).toBe('/project-packs/extensions')
    expect(init.method).toBe('POST')
    expect(body).toEqual({
      repo_root: '/abs/demo',
      pack_key: 'basic-project-pack',
      module: 'basic_project_pack.example_worker',
      enabled: false,
    })
    expect(out.ok).toBe(true)
  })

  it('registration methods encode params + surface a friendly degraded echo', async () => {
    const fetchFn = mockFetchOnce({ ok: false, agent: 'x', role: 'r', error: 'not a registered writer' })
    const out = await api.registerAgent('a/b', { name: 'x y', role: 'r' })
    expect(fetchFn).toHaveBeenLastCalledWith('/agents/a%2Fb/register', expect.objectContaining({ method: 'POST' }))
    expect(out.ok).toBe(false)
    expect(out.error).toContain('writer')

    const fetchFn2 = mockFetchOnce({ ok: false, removed: false, agent: 'a b', error: 'admin token' })
    await api.deregisterAgent('p', 'a b')
    expect(fetchFn2).toHaveBeenLastCalledWith('/agents/p/a%20b/deregister', expect.objectContaining({ method: 'POST' }))
  })
})
