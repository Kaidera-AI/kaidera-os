/**
 * Typed REST client for the console backend's clean module catalogs.
 *
 * The SPA is a PURE THIN CLIENT — it only reads the JSON the Track A modules
 * expose; it holds no business logic. Same-origin in dev (Vite proxies the path
 * prefixes to http://127.0.0.1:8765), same-origin in prod (served by the console).
 *
 * Endpoints consumed (verified against the module service.py, not guessed):
 *   GET /console/version                      → AppVersion           (global build stamp)
 *   GET /projects                              → Project[]            (the project list)
 *   GET /agents/{project}                      → AgentsCatalog
 *   GET /agents/{project}/{agent}/detail       → AgentDetail
 *   GET /runs/{project}                        → RunBoard
 *   GET /runs/run/{run_id}                     → RunTranscript
 *   POST /runs/run/{run_id}/cancel             → CancelRunResult
 *   GET /runs/{project}/by-handoff/{hid}       → RunTranscript
 *   GET /dispatch/{project}/board              → DispatchBoard
 *   GET /analytics/{project}/usage             → UsageBreakdown
 *   GET  /settings/{project}/app | /flags      → AppSettings | ProjectFlags
 *   POST /settings/{project}/flags             → FlagsWriteResult        (write)
 *   POST /settings/{project}/app               → AppSettingsWriteResult  (write)
 *   POST /settings/{project}/agents/{a}/config  → AgentConfigWriteResult (write, console-local)
 *   POST /settings/{project}/agents/{a}/promote → PromoteResult          (explicit registry promote)
 *   GET  /settings/{project}/system-schema     → SystemSchema            (3a)
 *   GET  /settings/{project}/providers         → ProvidersCatalog        (3a)
 *   GET  /settings/{project}/providers/config  → ProvidersConfig         (Track 2: configured providers)
 *   POST /settings/{project}/custom-providers          → CustomProviderResult  (3a write)
 *   POST /settings/{project}/custom-providers/delete   → CustomProviderResult  (3a write)
 *   POST /settings/{project}/provider-key-test → KeyTestResult           (3a probe)
 *   POST /settings/{project}/workspace         → WorkspaceResult         (3a write)
 *
 * The live SSE channel (/runstate/stream) is consumed by useRunStateStream, not here.
 */

import type {
  AgentConfigCatalog,
  AgentConfigWriteResult,
  AgentDetail,
  AgentEpicsPayload,
  AgentOverridePatch,
  AgentsCatalog,
  AnalyticsKpis,
  AppVersion,
  AppSettings,
  AppSettingsWriteResult,
  AutomationDeleteResult,
  AutomationFeedersExportResult,
  AutomationFeedersImportPayload,
  AutomationFeedersImportResult,
  AttachmentUploadResult,
  AuthDeleteResult,
  AuthUser,
  AuthUsersList,
  AuthUserResult,
  CancelRunResult,
  CortexEmbeddingBackfillRequest,
  CortexEmbeddingBackfillResult,
  CortexEmbeddingBacklogResult,
  CortexEmbeddingJobResult,
  CortexConfigResult,
  CortexPlatformConfig,
  CustomProviderResult,
  DispatchActivity,
  DispatchBoard,
  ExplainList,
  ExplainListItem,
  ExplainRequest,
  ExplainStartResult,
  PlanList,
  PlanListItem,
  PlanFile,
  PlanStatus,
  PlanBootstrapRequest,
  PlanBootstrapResult,
  WorkspaceTree,
  WorkspaceFile,
  FlagsPatch,
  GraphPayload,
  HistoryPayload,
  FlagsWriteResult,
  KeyTestResult,
  PlanningBeatStatus,
  PlanningBeatWritePayload,
  Project,
  ProjectPackExtensionPatch,
  ProjectPackExtensionResult,
  ProjectPackListResult,
  ProjectFlags,
  PromoteResult,
  ProvidersCatalog,
  ProvidersConfig,
  RegisterAgentPayload,
  RegisterAgentResult,
  RegisterProjectPayload,
  RegisterProjectResult,
  DeregisterAgentResult,
  RunBoard,
  RunStateRestartStatus,
  RunTranscript,
  ScheduledJobsResult,
  ScheduledJobRunNowResult,
  ScheduledJobWritePayload,
  ScheduledJobWriteResult,
  SkillBindResult,
  SkillInstallResult,
  BillingStatus,
  LicenseLoginRequest,
  LicenseStatus,
  LicenseTransportResult,
  SkillsPayload,
  SystemSchema,
  UpdateApplyResult,
  UpdateJob,
  UpdateStatus,
  UsageBreakdown,
  Whoami,
  WorkspaceResult,
} from './types'

/**
 * Derive the run_id from an explain artifact's deterministic `source_file`
 * (`explain/<run_id>.html`). The gallery list carries no separate run_id field, so the
 * "View" action re-renders the full HTML from this run's spans. Returns null when the
 * source_file isn't the expected `explain/<id>.html` shape (so the caller can hide View).
 */
export function explainRunIdFromSourceFile(sourceFile: string | null | undefined): string | null {
  if (!sourceFile) return null
  const m = /^explain\/(.+)\.html$/.exec(sourceFile.trim())
  return m ? m[1] : null
}

/** Extract the complete HTML document from a harness stream that may include progress
 * text before/after the payload. The backend applies the same normalization before
 * validation and persistence. */
export function extractExplainHtml(generation: string): string {
  if (!generation) return generation
  const lower = generation.toLowerCase()
  let start = lower.indexOf('<!doctype html')
  if (start < 0) {
    const root = /<html(?:\s|>)/i.exec(generation)
    if (!root || root.index === undefined) return generation
    start = root.index
  }
  const close = '</html>'
  const end = lower.lastIndexOf(close)
  return (end >= start ? generation.slice(start, end + close.length) : generation.slice(start)).trim()
}

/** Same-origin download URL for one project's explainer archive. */
export function explainExportUrl(project: string, runId: string): string {
  return `/explain/${encodeURIComponent(project)}/export/${encodeURIComponent(runId)}`
}

/** The full self-contained HTML for a run = the concatenated text of its `output` spans
 * (the explain document streams in as `output`; other span kinds — `input` — are skipped).
 * See docs/sdk/modules/explain.md §8: the run's spans carry the whole document. */
export function explainHtmlFromRun(run: RunTranscript | null | undefined): string {
  if (!run || !Array.isArray(run.segments)) return ''
  const generation = run.segments
    .filter((s) => (s.kind || 'output') === 'output')
    .map((s) => s.text || '')
    .join('')
  return extractExplainHtml(generation)
}

export class ApiError extends Error {
  readonly status: number
  readonly path: string

  constructor(status: number, path: string, message?: string) {
    super(message ?? `${status} on ${path}`)
    this.name = 'ApiError'
    this.status = status
    this.path = path
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    headers: { Accept: 'application/json' },
    signal,
  })
  if (!res.ok) {
    throw new ApiError(res.status, path, `GET ${path} → ${res.status}`)
  }
  return (await res.json()) as T
}

/**
 * POST a JSON body and parse the JSON reply. The settings WRITE path: small
 * collision-free POSTs to `/settings/{project}/...`. A non-ok response throws the
 * same typed `ApiError` as a read (so callers handle a stale-backend 404 / a 500
 * uniformly). The reply is the module's authoritative post-write echo.
 */
async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) {
    throw new ApiError(res.status, path, `POST ${path} → ${res.status}`)
  }
  return (await res.json()) as T
}

/**
 * Read the FastAPI `{detail: "..."}` error code off a non-ok response (best-effort) and
 * raise an ApiError whose message is that code. The auth admin/profile endpoints reply
 * with machine codes like `cannot_demote_last_admin` / `email_already_in_use`; the views
 * map those to a friendly sentence. Falls back to the status line when there's no detail.
 */
async function failWithDetail(res: Response, method: string, path: string): Promise<never> {
  let detail = ''
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (body && typeof body.detail === 'string') detail = body.detail
  } catch {
    // non-JSON / empty body — keep the generic message
  }
  throw new ApiError(res.status, path, detail || `${method} ${path} → ${res.status}`)
}

/** PATCH a JSON body, surfacing the FastAPI `detail` code on failure (see failWithDetail). */
async function patchJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) return failWithDetail(res, 'PATCH', path)
  return (await res.json()) as T
}

/** DELETE a resource, surfacing the FastAPI `detail` code on failure (see failWithDetail). */
async function deleteJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: 'DELETE',
    headers: { Accept: 'application/json' },
    signal,
  })
  if (!res.ok) return failWithDetail(res, 'DELETE', path)
  return (await res.json()) as T
}

const enc = encodeURIComponent

// Settings-scope encoder. Provider keys + app settings are GLOBAL (the backend
// `app_settings` table is not per-project), so the `{project}` in a settings URL is
// just routing. Before any project exists (fresh install), resolve to the `_system`
// scope so provider keys can be added/rotated during first-run setup. Once a project
// is selected the real key is used (identical global data either way).
const senc = (project: string) => enc(project || '_system')

/**
 * Read a File's bytes and return the BASE64 string (no data-URL prefix). Used by
 * `uploadAttachment` to send a file as base64-in-JSON (the backend takes no multipart).
 * Resolves with the base64 payload; rejects on a read error.
 */
function abortError(): DOMException | Error {
  try {
    return new DOMException('The operation was aborted.', 'AbortError')
  } catch {
    const err = new Error('The operation was aborted.')
    err.name = 'AbortError'
    return err
  }
}

export function fileToBase64(file: Blob, signal?: AbortSignal): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    const reader = new FileReader()
    let settled = false
    let onAbort: () => void = () => {}
    const cleanup = () => signal?.removeEventListener('abort', onAbort)
    const resolveOnce = (value: string) => {
      if (settled) return
      settled = true
      cleanup()
      resolve(value)
    }
    const rejectOnce = (error: unknown) => {
      if (settled) return
      settled = true
      cleanup()
      reject(error)
    }
    onAbort = () => {
      try {
        if (reader.readyState === FileReader.LOADING) reader.abort()
      } catch {
        // FileReader abort is best-effort; reject as AbortError either way.
      }
      rejectOnce(abortError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    reader.onload = () => {
      const result = reader.result
      if (typeof result !== 'string') {
        rejectOnce(new Error('failed to read file'))
        return
      }
      // result is a data URL: "data:<mime>;base64,<payload>". Strip the prefix.
      const comma = result.indexOf(',')
      resolveOnce(comma >= 0 ? result.slice(comma + 1) : result)
    }
    reader.onerror = () => rejectOnce(reader.error ?? new Error('failed to read file'))
    reader.onabort = () => rejectOnce(abortError())
    reader.readAsDataURL(file)
  })
}

/**
 * POST the chat message and return the RAW streaming Response (NOT json-parsed).
 *
 * The interactive-chat route streams SSE over a POST body (`event: run|delta|result|
 * error|done`), so — unlike the JSON catalogs — the caller must read `res.body` with
 * a ReadableStream reader (see chatStream.parseSseStream). The body is urlencoded
 * `message=<…>` (what `_read_posted_form` accepts), matching the legacy composer. A
 * non-ok status throws the same typed ApiError as every other call.
 */
async function postSse(
  path: string,
  message: string,
  sessionId?: string,
  clientRunId?: string,
  attachmentIds?: string[],
  signal?: AbortSignal,
): Promise<Response> {
  // Multi-turn chat (feature-gap step 6, Inc B): include the per-conversation
  // session_id when present so the backend threads the conversation's prior turns.
  // Absent → single-shot (`message=` only), backward-compatible with the legacy body.
  let body = `message=${enc(message)}`
  if (sessionId) body += `&session_id=${enc(sessionId)}`
  // Chat file-attachments (step 6, Inc A): the pre-minted client_run_id (shared with the
  // upload(s)) + the uploaded attachment ids (CSV). Both omitted when there are no
  // attachments → byte-for-byte the existing body.
  if (clientRunId) body += `&client_run_id=${enc(clientRunId)}`
  if (attachmentIds && attachmentIds.length > 0) {
    body += `&attachment_ids=${enc(attachmentIds.join(','))}`
  }
  const res = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Accept: 'text/event-stream',
    },
    body,
    signal,
  })
  if (!res.ok) {
    throw new ApiError(res.status, path, `POST ${path} → ${res.status}`)
  }
  return res
}

/**
 * POST a urlencoded form body, returning the RAW streaming Response (NOT json-parsed)
 * — the "Approve & Run" dispatch variant of `postSse`. Unlike the chat composer (a
 * single `message=` field), the dispatch run carries `summary` + `handoff_id` +
 * `handoff_compound` and routes the agent via a `?agent_name=` QUERY param. The reply
 * is the SAME SSE shape (`event: run|delta|result|error|done`), read by the caller
 * with `parseSseStream`. A non-ok status throws the same typed ApiError.
 */
async function postSseForm(
  path: string,
  fields: Record<string, string>,
  signal?: AbortSignal,
): Promise<Response> {
  const body = Object.entries(fields)
    .map(([k, v]) => `${enc(k)}=${enc(v)}`)
    .join('&')
  const res = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Accept: 'text/event-stream',
    },
    body,
    signal,
  })
  if (!res.ok) {
    throw new ApiError(res.status, path, `POST ${path} → ${res.status}`)
  }
  return res
}

/**
 * POST a urlencoded form with NO meaningful reply body to parse (the approve gate).
 * The live route re-renders HTML (the SPA ignores it + refetches the JSON board on
 * success); we only need the POST to fire + a non-ok to surface as an ApiError.
 */
async function postFormVoid(path: string, signal?: AbortSignal): Promise<void> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: '',
    signal,
  })
  if (!res.ok) {
    throw new ApiError(res.status, path, `POST ${path} → ${res.status}`)
  }
}

export const api = {
  /**
   * The console build stamp shown in the shell. Same source as FastAPI's app version
   * and the legacy Jinja badge: `app/version.py`.
   */
  appVersion: (signal?: AbortSignal) => getJson<AppVersion>('/console/version', signal),
  updateStatus: (signal?: AbortSignal) => getJson<UpdateStatus>('/console/update-status', signal),
  updateJob: (signal?: AbortSignal) => getJson<UpdateJob>('/console/update-job', signal),
  applyUpdate: (signal?: AbortSignal) =>
    postJson<UpdateApplyResult>('/console/update/apply', {}, signal),

  /** The signed-in user for the profile menu. First-party auth first, legacy
   * upstream headers during transition, generic dev admin when auth is disabled. */
  whoami: (signal?: AbortSignal) => getJson<Whoami>('/whoami', signal),

  /**
   * The project list. Canonical contract: `GET /projects` (the Cortex registry's
   * active projects). NOTE: the console build that exposes this as JSON may not be
   * the one running locally yet — callers should treat a 404 here as "backend route
   * not live" and degrade (the UI shows an empty project rail with a hint) rather
   * than crash. This stays a pure read; the SPA never invents the list.
   */
  projects: (signal?: AbortSignal) => getJson<Project[]>('/projects', signal),

  /**
   * Register (or update) a Cortex project — `POST /projects/register` (feature-gap #81). The
   * body is the new-project form (project_key + display_name + repo_root — the working folder,
   * which must be ABSOLUTE). Returns `{ok, project_key, error}`; `ok=false`+`error` nudges the
   * admin-token requirement (the project register is admin-gated; the token is never echoed).
   */
  registerProject: (body: RegisterProjectPayload, signal?: AbortSignal) =>
    postJson<RegisterProjectResult>('/projects/register', body, signal),

  /**
   * Discover installed project packs for an absolute project folder. Packs live
   * under `.kaidera-os/project-packs/<pack>` inside the selected project root and
   * remain project-owned data, not bundled core source.
   */
  listProjectPacks: (repoRoot: string, signal?: AbortSignal) =>
    getJson<ProjectPackListResult>(`/project-packs?repo_root=${enc(repoRoot)}`, signal),

  /** Enable/disable a module declared by an installed project pack. */
  setProjectPackExtension: (body: ProjectPackExtensionPatch, signal?: AbortSignal) =>
    postJson<ProjectPackExtensionResult>('/project-packs/extensions', body, signal),

  // -- agents ---------------------------------------------------------------
  agents: (project: string, signal?: AbortSignal) =>
    getJson<AgentsCatalog>(`/agents/${enc(project)}`, signal),

  agentDetail: (project: string, agent: string, signal?: AbortSignal) =>
    getJson<AgentDetail>(`/agents/${enc(project)}/${enc(agent)}/detail`, signal),

  /**
   * Register (or upsert) ONE agent on a project's roster — `POST /agents/{project}/register`
   * (feature-gap #81). The body is the new-agent form (name+role + the harness/model/reasoning/
   * designation/writer_scope config the backend folds into the registry `capabilities`). Returns
   * the friendly `{ok, agent, role, error}` echo — a degraded write (the console's writer isn't
   * authorised, or Cortex is unreachable) is a soft `ok=false` + a human `error` (never a token).
   */
  registerAgent: (project: string, body: RegisterAgentPayload, signal?: AbortSignal) =>
    postJson<RegisterAgentResult>(`/agents/${enc(project)}/register`, body, signal),

  /**
   * Deregister (deactivate) an agent from a project's roster — `POST /agents/{project}/{agent}/
   * deregister` (feature-gap #81). History is preserved (roster-only). Returns `{ok, removed,
   * agent, error}`; `ok=false`+`error` nudges the admin-token requirement (the remove is
   * admin-gated). The body is empty (the agent is in the path).
   */
  deregisterAgent: (project: string, agent: string, signal?: AbortSignal) =>
    postJson<DeregisterAgentResult>(
      `/agents/${enc(project)}/${enc(agent)}/deregister`,
      {},
      signal,
    ),

  /**
   * The FULL harness→model+reasoning option catalog (every harness's option sets),
   * so the Configure UI can repopulate the model/reasoning dropdowns client-side
   * when the harness <select> changes — no per-keystroke round-trip. `GET
   * /agents/{project}/config-catalog`.
   */
  configCatalog: (project: string, signal?: AbortSignal) =>
    getJson<AgentConfigCatalog>(`/agents/${enc(project)}/config-catalog`, signal),

  /**
   * The col-2 Active-Epic widget + metrics block — `GET /agents/{project}/epics` →
   * `{project, epic, metrics}`. `epic.mode==='epics'` carries the active-major epic stack (per-
   * epic progress + per-increment mini-bars); `'continuous'` is the 'continuous · no epics'
   * line. `metrics` is the {active_tasks, pending_tasks, pending_handoffs, events_24h} block (a
   * null counter renders '—'). Shaped from Cortex /epics + /state + /board; degrades to the
   * continuous/empty payload server-side on a down Cortex (never raises here).
   */
  agentEpics: (project: string, signal?: AbortSignal) =>
    getJson<AgentEpicsPayload>(`/agents/${enc(project)}/epics`, signal),

  /**
   * Send an interactive chat turn to an agent — `POST /agents/{p}/{a}/chat`.
   * Returns the RAW streaming Response; the caller reads the SSE body (the `run`
   * frame carries the run_id to pin the live transcript at; delta/result/error/done
   * are the local-mode signals — the durable reply also arrives via /runstate/stream).
   *
   * `sessionId` (OPTIONAL, multi-turn chat) is the stable per-conversation id the
   * composer mints; passed so the backend threads prior turns into the prompt. Omitted
   * → single-shot (the legacy `api.chat(project, agent, message)` call still works).
   */
  chat: (
    project: string,
    agent: string,
    message: string,
    sessionId?: string,
    clientRunId?: string,
    attachmentIds?: string[],
    signal?: AbortSignal,
  ) =>
    postSse(
      `/agents/${enc(project)}/${enc(agent)}/chat`,
      message,
      sessionId,
      clientRunId,
      attachmentIds,
      signal,
    ),

  /**
   * Load a chat conversation's prior turns — `GET /agents/{p}/{a}/chat/history?session_id=…`.
   * Returns `{turns: [{user, reply}]}` oldest-first, so the SPA can restore a conversation
   * after a page reload (the session_id is persisted in localStorage; without this a
   * reload minted a fresh session and the history "disappeared" even though it was
   * always in run_state/run_span). Degrades to an empty turns list on a blank session /
   * a non-ok response — never throws — so the composer renders empty and a fresh chat
   * still works.
   */
  chatHistory: async (
    project: string,
    agent: string,
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<{ turns: { user: string; reply: string }[] }> => {
    const res = await fetch(
      `/agents/${enc(project)}/${enc(agent)}/chat/history?session_id=${enc(sessionId)}`,
      { headers: { Accept: 'application/json' }, signal },
    )
    if (!res.ok) return { turns: [] }
    try {
      const data = await res.json()
      const turns = Array.isArray(data?.turns) ? data.turns : []
      return {
        turns: turns
          .filter((t: unknown) => t && typeof t === 'object')
          .map((t: unknown) => {
            const row = t as { user?: unknown; reply?: unknown }
            return { user: String(row.user ?? ''), reply: String(row.reply ?? '') }
          })
          .filter((t: { user: string; reply: string }) => t.user || t.reply),
      }
    } catch {
      return { turns: [] }
    }
  },

  /**
   * Upload ONE chat attachment (chat file-attachments, feature-gap step 6) — `POST
   * /agents/{p}/{a}/chat/upload`. The file is base64-encoded (via FileReader) and sent as
   * JSON `{run_id, filename, content_type, data}` (NO multipart — base64-in-JSON, the
   * backend's no-`python-multipart` discipline). `clientRunId` is the pre-minted uuid4 the
   * composer also sends on the chat POST, so the bytes land under the SAME run. Returns
   * `{attachment_id, filename, size_bytes}` — the id the composer echoes on send (the host
   * path is NEVER returned). A non-ok status throws the same typed ApiError.
   */
  uploadAttachment: (
    project: string,
    agent: string,
    clientRunId: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<AttachmentUploadResult> =>
    fileToBase64(file, signal).then((data) =>
      postJson<AttachmentUploadResult>(
        `/agents/${enc(project)}/${enc(agent)}/chat/upload`,
        {
          run_id: clientRunId,
          filename: file.name,
          content_type: file.type || 'application/octet-stream',
          data,
        },
        signal,
      ),
    ),

  // -- runs -----------------------------------------------------------------
  runBoard: (project: string, signal?: AbortSignal) =>
    getJson<RunBoard>(`/runs/${enc(project)}`, signal),

  run: (runId: string, signal?: AbortSignal) =>
    getJson<RunTranscript>(`/runs/run/${enc(runId)}`, signal),

  cancelRun: (runId: string, signal?: AbortSignal) =>
    postJson<CancelRunResult>(`/runs/run/${enc(runId)}/cancel`, {}, signal),

  runByHandoff: (project: string, handoffId: string, signal?: AbortSignal) =>
    getJson<RunTranscript>(`/runs/${enc(project)}/by-handoff/${enc(handoffId)}`, signal),

  runstateRestartStatus: (project: string, signal?: AbortSignal) =>
    getJson<RunStateRestartStatus>(`/runstate/restart-status?project=${enc(project)}`, signal),

  // -- explain (the visual code explainer) ----------------------------------
  /**
   * START a visual code explainer generation — `POST /explain/{project}`. The body is
   * `{kind, path?, fn_name?, git_rev?, harness?, model?}`; the repo is resolved
   * SERVER-SIDE from the project's repo_root (never client-supplied). Returns
   * `{run_id, accepted, error}` — the SPA then FOLLOWS the run (`api.run(run_id)`),
   * whose `output` spans carry the full HTML as it streams. `accepted=false` (200) means
   * the host harness-service rejected/was unreachable (the run is marked errored).
   */
  postExplain: (project: string, body: ExplainRequest, signal?: AbortSignal) =>
    postJson<ExplainStartResult>(`/explain/${enc(project)}`, body, signal),

  /**
   * The Explain gallery — `GET /explain/{project}/list` → recent explain RUNS,
   * enumerated SERVER-SIDE from run_state (`lease_owner='explain'`), NOT Cortex search.
   * Each row carries `run_id` FIRST-CLASS (+ `artifact_id`/`target_*`/`caption`/
   * `created_at`/`status` from the run's metadata), so "View" re-renders the full HTML
   * from that run's spans (`GET /runs/run/{run_id}`). We keep a `source_file`-derive
   * FALLBACK for `run_id` (defensive — the server now always sends it). Degrades to `[]`
   * server-side on a down/None run-state store (never raises here).
   */
  getExplainList: (project: string, signal?: AbortSignal): Promise<ExplainListItem[]> =>
    getJson<ExplainList>(`/explain/${enc(project)}/list`, signal).then((res) =>
      (res.artifacts ?? []).map((a) => ({
        ...a,
        run_id: a.run_id ?? explainRunIdFromSourceFile(a.source_file),
      })),
    ),

  // -- plan (the Visual Plan read surface over docs/plans/**/*.mdx) ----------
  /** List the project's `.mdx` plans (newest-first), or `[]` when none / no plans dir. */
  getPlanList: (project: string, signal?: AbortSignal): Promise<PlanListItem[]> =>
    getJson<PlanList>(`/plan/${enc(project)}/list`, signal).then((r) => r.plans ?? []),

  /** Read one plan's raw MDX text by its `docs/plans/`-relative path. */
  getPlanFile: (project: string, path: string, signal?: AbortSignal): Promise<PlanFile> =>
    getJson<PlanFile>(`/plan/${enc(project)}/file?path=${enc(path)}`, signal),

  /** Read project plan readiness metadata for v2 plan surfaces. */
  getPlanStatus: (project: string, signal?: AbortSignal): Promise<PlanStatus> =>
    getJson<PlanStatus>(`/plan/${enc(project)}/status`, signal),

  /** Ask the project lead to create the first visual plan via a Cortex handoff. */
  bootstrapPlan: (
    project: string,
    body: PlanBootstrapRequest,
    signal?: AbortSignal,
  ): Promise<PlanBootstrapResult> =>
    postJson<PlanBootstrapResult>(`/plan/${enc(project)}/bootstrap`, body, signal),

  // -- workspace (the right-side working-folder file tree + viewer/editor) ---
  /** List one directory of the project's working folder (lazy per folder expand). */
  getWorkspaceTree: (project: string, path = '', signal?: AbortSignal): Promise<WorkspaceTree> =>
    getJson<WorkspaceTree>(`/workspace/${enc(project)}/filetree?path=${enc(path)}`, signal),

  /** Read one workspace file's content for the viewer/editor. */
  getWorkspaceFile: (project: string, path: string, signal?: AbortSignal): Promise<WorkspaceFile> =>
    getJson<WorkspaceFile>(`/workspace/${enc(project)}/filecontent?path=${enc(path)}`, signal),

  /** Save edited content back to a workspace file. */
  saveWorkspaceFile: (
    project: string,
    path: string,
    content: string,
    signal?: AbortSignal,
  ): Promise<{ ok: boolean; path: string; error?: string }> =>
    postJson(`/workspace/${enc(project)}/filecontent`, { path, content }, signal),

  // -- graph (the knowledge/code-graph view, feature-gap #80) ---------------
  /**
   * The SEED/default knowledge graph — `GET /graph/{project}` → `{nodes, edges, stats}`.
   * The backend runs a project-flavoured catch-all term against Cortex
   * `/cortex-graph-search` so the canvas isn't empty on first paint. BOUNDED at ~140 nodes
   * (the hits + their 1-hop neighbours), cytoscape-AGNOSTIC (id/label/kind for nodes,
   * id/source/target/label for edges). Degrades to an empty graph server-side on a
   * down/empty Cortex (never raises here).
   */
  graph: (project: string, signal?: AbortSignal) =>
    getJson<GraphPayload>(`/graph/${enc(project)}`, signal),

  /**
   * Re-centre the graph on a search term — `GET /graph/{project}/search?q=<term>`. The
   * matching entities + their 1-hop neighbours become the bounded graph. Same
   * `{nodes, edges, stats}` shape as `graph`. A blank `q` falls back to the seed term.
   */
  graphSearch: (project: string, q: string, signal?: AbortSignal) =>
    getJson<GraphPayload>(`/graph/${enc(project)}/search?q=${enc(q)}`, signal),

  /** Project-scoped Cortex memory graph — all extracted L4 entities/relationships, bounded. */
  graphMemory: (project: string, signal?: AbortSignal) =>
    getJson<GraphPayload>(`/graph/${enc(project)}/memory`, signal),

  // -- history (the cross-agent activity timeline, feature-gap #80) ----------
  /**
   * The activity timeline + optional recent-decisions feed — `GET /history/{project}?limit=N` →
   * `{events, decisions, agent_count}`. `events` is the reverse-chronological cross-agent
   * timeline (each Cortex `/history` row run SERVER-SIDE through the ported summariser → a
   * readable line, never raw tool-call JSON); `decisions` is the recent-decisions feed (from
   * Cortex `/search`); `agent_count` is the roster size. `limit` (optional) sizes the raw
   * /history window (the rendered timeline is bounded server-side regardless). Pass
   * `includeDecisions` only for the History tab; dashboard polling stays on the cheap
   * timeline path. Degrades to
   * empty sections server-side on a down/empty Cortex (never raises here). Pure read.
   */
  history: (
    project: string,
    limit?: number,
    signal?: AbortSignal,
    opts?: { includeDecisions?: boolean },
  ) => {
    const params = new URLSearchParams()
    if (limit) params.set('limit', String(limit))
    if (opts?.includeDecisions) params.set('include_decisions', '1')
    const query = params.toString()
    return getJson<HistoryPayload>(`/history/${enc(project)}${query ? `?${query}` : ''}`, signal)
  },

  // -- skills (the Skills tab: catalogue + install + bind) ------------------
  /**
   * The skills catalogue — `GET /skills/{project}` → `{skills: [...]}`. Lists every GLOBAL
   * skill (the shared skills repo) plus this project's own project/agent-scoped skills (slug ·
   * name · description · scope · version · status). Degrades to `{skills: []}` server-side on a
   * down/empty Cortex (never raises here). Pure read.
   */
  skills: (project: string, signal?: AbortSignal) =>
    getJson<SkillsPayload>(`/skills/${enc(project)}`, signal),

  /**
   * Install a skill from a GitHub URL (or local path) — `POST /skills/{project}/install`. The
   * backend shells out to the `cortex-skill install` CLI (clone + SKILL.md parse + register), then
   * returns the install result + the REFRESHED catalogue so the view re-renders without a second
   * call: `{ok, error, skills}`. `scope` (optional: `global|project|agent`) overrides the CLI's
   * frontmatter precedence. A non-ok status throws the same typed ApiError; a failed-but-200
   * install surfaces as `ok=false` + a friendly `error`.
   */
  installSkill: (
    project: string,
    body: { url: string; scope?: string },
    signal?: AbortSignal,
  ) => postJson<SkillInstallResult>(`/skills/${enc(project)}/install`, body, signal),

  /**
   * Bind (deliver) a skill to a subject — `POST /skills/{project}/{slug}/bind`. The body is
   * `{subject, subject_kind?}` (`subject_kind` defaults to `role`; pass `agent` to bind a single
   * agent). Returns `{ok, slug, subject, error}` — a degraded write (Cortex unreachable / the
   * console's writer isn't authorised) is a soft `ok=false` + a friendly error (never a token).
   */
  bindSkill: (
    project: string,
    slug: string,
    body: { subject: string; subject_kind?: string },
    signal?: AbortSignal,
  ) => postJson<SkillBindResult>(`/skills/${enc(project)}/${enc(slug)}/bind`, body, signal),

  // -- dispatch -------------------------------------------------------------
  dispatchBoard: (project: string, signal?: AbortSignal) =>
    getJson<DispatchBoard>(`/dispatch/${enc(project)}/board`, signal),

  /**
   * Orchestrator autonomous-activity feed (newest-first) + the E007 wave-plan strip —
   * `GET /dispatch/{project}/activity`. A None / degraded orchestrator degrades to
   * the idle/empty payload server-side (never raises here). Pure read.
   */
  dispatchActivity: (project: string, signal?: AbortSignal) =>
    getJson<DispatchActivity>(`/dispatch/${enc(project)}/activity`, signal),

  /**
   * "Approve & Run" a proposed dispatch — `POST /dispatch/{project}/run?agent_name=…`.
   * The PROPOSE-MODE human-in-the-loop trigger: it CLAIMS the handoff, opens a
   * run-state row, and STREAMS the harness reply back as SSE (the SAME
   * `event: run|delta|result|error|done` shape as chat). Returns the RAW streaming
   * Response; the caller reads `res.body` with `parseSseStream` and captures the
   * `run_id` from the `run` frame (to point the transcript at the live run). The body
   * carries the handoff `summary` (the work) + its `id`/`compound` (system framing).
   */
  dispatchRun: (
    project: string,
    agentName: string,
    body: { summary: string; handoff_id: string; handoff_compound: string },
    signal?: AbortSignal,
  ) =>
    postSseForm(
      `/dispatch/${enc(project)}/run?agent_name=${enc(agentName)}`,
      { summary: body.summary, handoff_id: body.handoff_id, handoff_compound: body.handoff_compound },
      signal,
    ),

  /**
   * Approve a handoff the propose-mode gate parked — `POST /projects/{project}/
   * handoffs/{handoff_id}/approve`. Clears the awaiting-approval park so the next
   * Dispatch sweep spawns the agent. Idempotent server-side. The route re-renders
   * HTML (ignored); the SPA refetches the JSON board on success. Resolves on a 2xx,
   * throws an ApiError otherwise.
   */
  approveHandoff: (project: string, handoffId: string, signal?: AbortSignal) =>
    postFormVoid(`/projects/${enc(project)}/handoffs/${enc(handoffId)}/approve`, signal),

  // -- automation feeders ---------------------------------------------------
  /**
   * Durable project schedules — stored in the app-DB and emitted as normal Cortex
   * handoffs when due. They do not run agents directly; project autonomy/propose and
   * per-agent auto-dispatch still decide execution.
   */
  scheduledJobs: (project: string, signal?: AbortSignal) =>
    getJson<ScheduledJobsResult>(`/automation/${enc(project)}/scheduled-jobs`, signal),

  planningBeat: (project: string, signal?: AbortSignal) =>
    getJson<PlanningBeatStatus>(`/automation/${enc(project)}/planning-beat`, signal),

  savePlanningBeat: (
    project: string,
    body: PlanningBeatWritePayload,
    signal?: AbortSignal,
  ) =>
    postJson<ScheduledJobWriteResult>(
      `/automation/${enc(project)}/planning-beat`,
      body,
      signal,
    ),

  saveScheduledJob: (
    project: string,
    body: ScheduledJobWritePayload,
    signal?: AbortSignal,
  ) =>
    postJson<ScheduledJobWriteResult>(
      `/automation/${enc(project)}/scheduled-jobs`,
      body,
      signal,
    ),

  runScheduledJobNow: (project: string, jobId: string, signal?: AbortSignal) =>
    postJson<ScheduledJobRunNowResult>(
      `/automation/${enc(project)}/scheduled-jobs/${enc(jobId)}/run-now`,
      {},
      signal,
    ),

  deleteScheduledJob: (project: string, jobId: string, signal?: AbortSignal) =>
    deleteJson<AutomationDeleteResult>(
      `/automation/${enc(project)}/scheduled-jobs/${enc(jobId)}`,
      signal,
    ),

  exportAutomationFeeders: (project: string, signal?: AbortSignal) =>
    getJson<AutomationFeedersExportResult>(`/automation/${enc(project)}/feeders/export`, signal),

  importAutomationFeeders: (
    project: string,
    body: AutomationFeedersImportPayload,
    signal?: AbortSignal,
  ) =>
    postJson<AutomationFeedersImportResult>(
      `/automation/${enc(project)}/feeders/import`,
      body,
      signal,
    ),

  // -- analytics ------------------------------------------------------------
  usage: (project: string, signal?: AbortSignal) =>
    getJson<UsageBreakdown>(`/analytics/${enc(project)}/usage`, signal),

  /**
   * The Analytics view's slim headline KPI strip — `GET /analytics/{project}/kpis` →
   * `{events_24h, active_tasks, pending_handoffs, decisions_recent, window_days, tokens_recent,
   * tokens_recent_h}`. The counters are null when their Cortex read was unreachable (the view
   * renders 'n/a'); recent tokens come from the App-DB project rollup. The `/usage` route covers
   * the tokens/cost BREAKDOWN; this covers the KPI COUNTERS. Degrades to null counters server-
   * side (never raises here).
   */
  analyticsKpis: (project: string, signal?: AbortSignal) =>
    getJson<AnalyticsKpis>(`/analytics/${enc(project)}/kpis`, signal),

  // -- settings -------------------------------------------------------------
  appSettings: (project: string, signal?: AbortSignal) =>
    getJson<AppSettings>(`/settings/${senc(project)}/app`, signal),

  flags: (project: string, signal?: AbortSignal) =>
    getJson<ProjectFlags>(`/settings/${enc(project)}/flags`, signal),

  // -- settings writes (Track C) --------------------------------------------
  /**
   * Set the project's autonomy and/or propose-mode kill-switches. `patch` carries
   * either or both flags (a partial — an omitted flag is left untouched server-
   * side). POSTs the patch verbatim to `/settings/{project}/flags`; returns the
   * authoritative post-write flag state.
   */
  setFlags: (project: string, patch: FlagsPatch, signal?: AbortSignal) =>
    postJson<FlagsWriteResult>(`/settings/${enc(project)}/flags`, patch, signal),

  /**
   * Upsert ONE app/system setting (key→value). Wraps it as `{settings: {key:
   * value}}` — the endpoint upserts only the supplied keys. Returns the
   * authoritative post-write settings map.
   */
  setAppSetting: (project: string, key: string, value: unknown, signal?: AbortSignal) =>
    postJson<AppSettingsWriteResult>(
      `/settings/${senc(project)}/app`,
      { settings: { [key]: value } },
      signal,
    ),

  /**
   * Upsert a BATCH of app/system settings in one POST — the typed System form's
   * "save only the changed keys" path. Wraps the partial map as `{settings: {...}}`
   * (the endpoint upserts only the supplied keys). Returns the authoritative
   * post-write settings map. `POST /settings/{project}/app`.
   */
  setAppSettings: (project: string, settings: Record<string, unknown>, signal?: AbortSignal) =>
    postJson<AppSettingsWriteResult>(`/settings/${senc(project)}/app`, { settings }, signal),

  /**
   * The license posture for the Settings → License panel — `GET /settings/{project}/license`.
   * edition + validity + customer/expiry + resolved entitlements (unlocked harnesses +
   * capacity caps). Never carries the raw token. Apply a token via `setAppSetting(project,
   * 'license_key', token)` then re-fetch — the gates re-read it live.
   */
  license: (project: string, signal?: AbortSignal) =>
    getJson<LicenseStatus>(`/settings/${senc(project)}/license`, signal),

  licenseLogin: (project: string, request: LicenseLoginRequest, signal?: AbortSignal) =>
    postJson<LicenseTransportResult>(`/settings/${senc(project)}/license/login`, request, signal),

  licenseActivate: (project: string, orgLoginToken: string, signal?: AbortSignal) =>
    postJson<LicenseTransportResult>(
      `/settings/${senc(project)}/license/activate`,
      { org_login_token: orgLoginToken },
      signal,
    ),

  licenseHeartbeat: (project: string, signal?: AbortSignal) =>
    postJson<LicenseTransportResult>(`/settings/${senc(project)}/license/heartbeat`, {}, signal),

  licenseRestore: (project: string, signal?: AbortSignal) =>
    postJson<LicenseTransportResult>(`/settings/${senc(project)}/license/restore`, {}, signal),

  /**
   * The Billing-tab view — `GET /settings/{project}/billing`: per-entitlement usage
   * (counted from Cortex) vs the entitled total, the wallet balance, and active add-ons.
   * Buying add-ons / topping up the wallet lives in the Kaidera AI cust-portal (`portal_url`).
   */
  billing: (project: string, signal?: AbortSignal) =>
    getJson<BillingStatus>(`/settings/${senc(project)}/billing`, signal),

  /**
   * Save one agent's console-local override (designation/harness/model/…). Wraps
   * the field patch as `{override: {...}}` (MERGE semantics server-side: a blank
   * value clears that field). Returns the post-save effective override +
   * designation.
   */
  setAgentConfig: (
    project: string,
    agent: string,
    override: AgentOverridePatch,
    signal?: AbortSignal,
  ) =>
    postJson<AgentConfigWriteResult>(
      `/settings/${enc(project)}/agents/${enc(agent)}/config`,
      { override },
      signal,
    ),

  /**
   * EXPLICIT "Promote to registry" (feature-gap #81) — push the agent's CURRENT
   * effective console override into the Cortex registry on demand. `POST /settings/
   * {project}/agents/{agent}/promote` (empty body; the agent is in the path). Returns
   * `{ok, error}` — a graceful `ok=false` + a human `error` when Cortex is unreachable
   * or the console's writer isn't authorised (never a token). This does NOT mutate the
   * console-local override; it's the deliberate commit gesture distinct from Save.
   */
  promoteAgent: (project: string, agent: string, signal?: AbortSignal) =>
    postJson<PromoteResult>(
      `/settings/${enc(project)}/agents/${enc(agent)}/promote`,
      {},
      signal,
    ),

  // -- settings reads (step 3a — the [API]-gap catalogs) ---------------------
  /**
   * The typed System form as JSON: groups → fields, each typed (`text|number|bool|
   * secret|readonly`). A SECRET field carries ONLY `is_set` + a masked placeholder —
   * NEVER the raw secret value. `GET /settings/{project}/system-schema`.
   */
  systemSchema: (project: string, signal?: AbortSignal) =>
    getJson<SystemSchema>(`/settings/${senc(project)}/system-schema`, signal),

  /**
   * The live model catalog grouped by provider (model/type/reasoning-tiers/pricing/
   * context/source/freshness). `GET /settings/{project}/providers`. Degrades to
   * `{providers: []}` server-side on a fetch error (never raises here).
   */
  providers: (project: string, signal?: AbortSignal) =>
    getJson<ProvidersCatalog>(`/settings/${senc(project)}/providers`, signal),

  /**
   * The CONFIGURED/ACTIVE providers for the Providers control surface — which
   * providers have a key set + their Test target. `GET /settings/{project}/
   * providers/config` → `{providers:[{name, label, key_is_set, is_custom, testable,
   * provider_ref, key_field?, base_url?}]}`. NEVER a raw key (only `key_is_set`).
   * Degrades to the built-in list + `store_connected=false` server-side on a down
   * store (never raises here).
   */
  providersConfig: (project: string, signal?: AbortSignal) =>
    getJson<ProvidersConfig>(`/settings/${senc(project)}/providers/config`, signal),

  /**
   * Read the API-owned Cortex ingestion/search model config. This is global platform
   * state, not project memory: embedding/rerank provider/model/dimensions and search
   * tuning. It soft-fails server-side when Cortex/admin auth is not available.
   */
  cortexConfig: (signal?: AbortSignal) => getJson<CortexConfigResult>('/cortex/config', signal),

  /**
   * Patch the API-owned Cortex ingestion/search model config. The backend applies only
   * supplied fields and returns the effective config row after write.
   */
  setCortexConfig: (config: Partial<CortexPlatformConfig>, signal?: AbortSignal) =>
    postJson<CortexConfigResult>('/cortex/config', { config }, signal),

  /**
   * Project embedding coverage/backlog from Cortex. This is the operator-facing
   * read model for vector-space rebuild decisions.
   */
  cortexEmbeddingBacklog: (project: string, signal?: AbortSignal) =>
    getJson<CortexEmbeddingBacklogResult>(
      `/cortex/embeddings/backlog?project=${enc(project)}`,
      signal,
    ),

  /**
   * Dry-run or start an embedding backfill through the console proxy. The admin
   * token stays backend-side.
   */
  cortexEmbeddingBackfill: (
    project: string,
    request: CortexEmbeddingBackfillRequest,
    signal?: AbortSignal,
  ) =>
    postJson<CortexEmbeddingBackfillResult>(
      `/cortex/embeddings/backfill?project=${enc(project)}`,
      request,
      signal,
    ),

  cortexEmbeddingJob: (project: string, jobId: string, signal?: AbortSignal) =>
    getJson<CortexEmbeddingJobResult>(
      `/cortex/embeddings/jobs/${enc(jobId)}?project=${enc(project)}`,
      signal,
    ),

  // -- settings writes (step 3a) --------------------------------------------
  /**
   * Add an operator-defined custom provider (`name` + `base_url` + `api_key`).
   * Returns `{ok, added, error, custom_providers}` where `custom_providers` is the
   * refreshed MASKED list (the raw key is NEVER echoed back). `POST /settings/
   * {project}/custom-providers`.
   */
  addCustomProvider: (
    project: string,
    body: { name: string; base_url: string; api_key: string },
    signal?: AbortSignal,
  ) =>
    postJson<CustomProviderResult>(
      `/settings/${senc(project)}/custom-providers`,
      body,
      signal,
    ),

  /**
   * Remove a custom provider by `id` (or `name`). Returns `{ok, removed, error,
   * custom_providers}` with the refreshed masked list. `POST /settings/{project}/
   * custom-providers/delete`.
   */
  deleteCustomProvider: (project: string, id: string, signal?: AbortSignal) =>
    postJson<CustomProviderResult>(
      `/settings/${senc(project)}/custom-providers/delete`,
      { id },
      signal,
    ),

  /**
   * Probe a provider key (read-only — lists models / key-info, never a completion,
   * so it spends no tokens). `provider` is a built-in secret-key field (e.g.
   * `anthropic_api_key`) or `custom:<id>`. Pass `key` to test a freshly-typed value,
   * or `use_stored:true` / omit it to test the stored/env key. The key is NEVER
   * echoed — only `{ok, detail, status, label}`. `POST /settings/{project}/
   * provider-key-test`.
   */
  providerKeyTest: (
    project: string,
    body: { provider: string; key?: string; use_stored?: boolean },
    signal?: AbortSignal,
  ) =>
    postJson<KeyTestResult>(
      `/settings/${senc(project)}/provider-key-test`,
      body,
      signal,
    ),

  /**
   * Set a project's canonical working folder (`repo_root`) via the admin path. On
   * success the result carries `previous_repo_root → repo_root`; on failure a clear
   * human `error`. The admin token is sourced + sent SERVER-SIDE and never exposed.
   * `POST /settings/{project}/workspace`.
   */
  setWorkspace: (
    project: string,
    body: { repo_root: string; project_key?: string },
    signal?: AbortSignal,
  ) =>
    postJson<WorkspaceResult>(`/settings/${enc(project)}/workspace`, body, signal),

  // -- auth: admin user management + own profile -----------------------------
  /** The signed-in user's own profile (first-party auth). `GET /auth/profile`. */
  authProfile: (signal?: AbortSignal) => getJson<AuthUser>('/auth/profile', signal),

  /** Edit the CURRENT user's own email + display name. `PATCH /auth/profile`. A duplicate
   * email is a 409 (`email_already_in_use`); an invalid email a 400 — both surface as the
   * ApiError message the Profile view maps to a sentence. */
  updateProfile: (
    body: { email?: string; display_name?: string },
    signal?: AbortSignal,
  ) => patchJson<AuthUserResult>('/auth/profile', body, signal),

  /** The admin Users table — every user (email, role, status, last login). `GET /auth/users`
   * (require_admin). */
  authUsers: (signal?: AbortSignal) => getJson<AuthUsersList>('/auth/users', signal),

  /** Admin: create a user (email + role). `POST /auth/users`. */
  createAuthUser: (
    body: { email: string; role: 'admin' | 'user'; display_name?: string },
    signal?: AbortSignal,
  ) => postJson<AuthUserResult>('/auth/users', body, signal),

  /** Admin: change a user's role and/or status. `PATCH /auth/users/{id}`. The last-active-admin
   * guard returns 409 (`cannot_demote_last_admin` / `cannot_block_last_admin`). */
  updateAuthUser: (
    userId: string,
    body: { role?: 'admin' | 'user'; status?: 'active' | 'disabled' },
    signal?: AbortSignal,
  ) => patchJson<AuthUserResult>(`/auth/users/${enc(userId)}`, body, signal),

  /** Admin: delete a user. `DELETE /auth/users/{id}`. Refuses the last active admin (409). */
  deleteAuthUser: (userId: string, signal?: AbortSignal) =>
    deleteJson<AuthDeleteResult>(`/auth/users/${enc(userId)}`, signal),
}

export type Api = typeof api
