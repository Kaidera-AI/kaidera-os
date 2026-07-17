/**
 * Typed view of the console backend's clean module JSON catalogs (Track A).
 *
 * These shapes are transcribed 1:1 from the module `service.py` payloads — NOT
 * guessed. Sources:
 *   - agents:    app/agents/service.py   (agent_view / list_agents / get_agent)
 *   - runs:      app/runs/service.py      (store_run_row / store_transcript_view / board)
 *   - dispatch:  app/dispatch/service.py  (board)        — docs/sdk/modules/dispatch.md §4
 *   - analytics: app/analytics/service.py (usage_cost)   — docs/sdk/modules/analytics.md §4
 *   - settings:  app/settings_module/service.py (flags / app)
 *   - projects:  cortex_client.get_active_projects() (project rows)
 *
 * Fields the backend may omit / null are typed optional/nullable accordingly.
 * Where a module returns an open-ended dict (e.g. analytics breakdown rows) we
 * keep a permissive but useful shape rather than over-constraining it.
 */

// ---------------------------------------------------------------------------
//  Console shell metadata — GET /console/version
// ---------------------------------------------------------------------------

export interface AppVersion {
  version: string
}

// ---------------------------------------------------------------------------
//  Console update status — GET /console/update-status
// ---------------------------------------------------------------------------

export interface UpdateStatus {
  current_version: string
  latest_version?: string | null
  latest_tag?: string | null
  update_available?: boolean | null
  check_ok: boolean
  source: string
  repo: string
  update_command: string
  apply_endpoint?: string | null
  job_endpoint?: string | null
  can_apply?: boolean
  admin_required?: boolean
  release_name?: string | null
  release_notes?: string | null
  impact?: string[]
  backup_guidance?: string[]
  rollback_guidance?: string[]
  post_update_checks?: string[]
  error?: string | null
  published_at?: string | null
  release_url?: string | null
  checked_at?: string | null
  cached?: boolean
  stale?: boolean
  refreshing?: boolean
}

export interface UpdateHealthCheck {
  name: string
  status: 'ok' | 'failed' | 'warn' | 'unknown' | string
  detail?: string | null
  url?: string | null
  checked_at?: string | null
}

export interface UpdateJob {
  status: 'idle' | 'starting' | 'running' | 'succeeded' | 'failed' | 'unknown' | string
  job_id?: string | null
  pid?: number | null
  started_at?: string | null
  finished_at?: string | null
  return_code?: number | null
  log_path?: string | null
  health_checks?: UpdateHealthCheck[]
  command?: string | null
  error?: string | null
}

export interface UpdateApplyResult {
  accepted: boolean
  already_running: boolean
  job: UpdateJob
}

/** The signed-in user from Kaidera AI first-party auth, with legacy upstream-header fallback. */
export interface Whoami {
  authenticated?: boolean
  id?: string
  name: string
  display_name?: string
  email: string
  is_admin: boolean
  role?: 'admin' | 'user' | string
  status?: 'active' | 'disabled' | string
  last_login_at?: string | null
}

// ---------------------------------------------------------------------------
//  Auth admin panel + profile — /auth/users{,/{id}}, /auth/profile
// ---------------------------------------------------------------------------

/** One user row in the admin Users table (the `user_payload` shape from app/auth.py). */
export interface AuthUser {
  id?: string
  name: string
  display_name?: string
  email: string
  is_admin: boolean
  role: 'admin' | 'user' | string
  // 'disabled' is the schema/stored value for a blocked account (the UI labels it "Blocked").
  status: 'active' | 'disabled' | string
  last_login_at?: string | null
}

export interface AuthUsersList {
  users: AuthUser[]
}

/** The {ok, user} echo from a create / role-status patch / profile update. */
export interface AuthUserResult {
  ok: boolean
  user: AuthUser
}

export interface AuthDeleteResult {
  ok: boolean
  removed: boolean
  id: string
}

// ---------------------------------------------------------------------------
//  Projects — GET /projects  (Cortex registry rows; see note in client.ts)
// ---------------------------------------------------------------------------

export interface Project {
  project_key: string
  display_name?: string | null
  status?: string | null
  repo_root?: string | null
  /** How many agents the project's roster carries (rendered in the rail row). */
  agent_count?: number | null
  // Cortex may attach extra registry fields; keep them addressable.
  [k: string]: unknown
}

// ---------------------------------------------------------------------------
//  Agents — GET /agents/{project}  and  /agents/{project}/{agent}/detail
// ---------------------------------------------------------------------------

/** One flattened agent row (agents column + detail header). `AgentsService.agent_view`. */
export interface AgentView {
  name: string
  display_name: string
  initials: string
  role: string
  model: string | null
  model_label: string | null
  harness: string | null
  harness_label: string
  thinking: string | null
  writer_scope: string | null
  capabilities: string[]
  row_sub: string
  is_test: boolean
  interactive: boolean
  designation_override: boolean
  cpo_tag: boolean
}

/** GET /agents/{project} — the roster catalog. */
export interface AgentsCatalog {
  project: string
  interactive: AgentView[]
  autonomous: AgentView[]
  orchestrator: string | null
  lead: string | null
}

// ---------------------------------------------------------------------------
//  Agent epics + metrics — GET /agents/{project}/epics
//  The col-2 Active-Epic widget (per-epic progress + per-increment mini-bars) + the
//  project metrics block. Transcribed 1:1 from app/agents/epics.py (the shaper the
//  legacy HTML col-2 also uses). Graceful-degrade: mode='continuous' on no-epics / a
//  down Cortex; null metric counters render '—'.
// ---------------------------------------------------------------------------

/** The 3 visual families an increment bar/dot draws as: done = filled, prog = teal in-flight,
 * todo = empty track. Mirrors the backend `_inc_status_kind`. */
export type IncrementKind = 'done' | 'prog' | 'todo' | string

/** One increment of an epic (a mini progress segment). `num`/`status` may be null when the row
 * carried none; `pct` is clamped 0–100 server-side. */
export interface IncrementView {
  num: number | null
  label: string
  title: string
  pct: number
  status: string
  kind: IncrementKind
}

/** One shaped epic — epic_id · title · overall_pct + its increments. `is_active` flags the
 * build/active epic (drawn as the lead). */
export interface EpicView {
  epic_id: string
  title: string
  status: string
  overall_pct: number
  increments: IncrementView[]
  increment_count: number
  is_active: boolean
  updated_at?: string | null
}

/**
 * The Active-Epic section. `mode==='epics'` → the active-major epic stack; `'continuous'` →
 * the 'continuous · no epics' line (a continuous-backlog project, no epics, OR a degraded
 * /epics read — NEVER fabricated progress). `label` is the continuous line (absent in 'epics').
 */
export interface EpicSection {
  mode: 'epics' | 'continuous' | string
  epics: EpicView[]
  epic_count: number
  label?: string
}

/** The compact project metrics block. A null counter means /state was unreachable (render '—');
 * `pending_tasks` is derived from the board server-side. */
export interface MetricsBlock {
  active_tasks: number | null
  pending_tasks: number | null
  pending_handoffs: number | null
  events_24h: number | null
}

/** GET /agents/{project}/epics → the Active-Epic widget data + the metrics block. */
export interface AgentEpicsPayload {
  project: string
  epic: EpicSection
  metrics: MetricsBlock
}

/** The inline-editable config row model (shape is harness-shaper dependent). */
export interface AgentConfigView {
  name: string
  display_name: string
  role: string
  designation: string
  reg_designation?: string
  /** VALIDITY (feature #99): true when the stored model was invalid for the effective
   * harness and the backend coerced it to the harness default (so the controls show a
   * runnable pair, never an impossible one). */
  model_coerced?: boolean
  /** The original (impossible) stored model value, for the "model was invalid" hint. */
  model_invalid_original?: string | null
  [k: string]: unknown
}

/** GET /agents/{project}/{agent}/detail — one agent resolved. */
export interface AgentDetail {
  project: string
  agent: AgentView
  designation: string
  role: string
  registry_designation: string
  config_view: AgentConfigView
}

// ---------------------------------------------------------------------------
//  Agent config catalog — GET /agents/{project}/config-catalog
//  The FULL harness→model+reasoning option sets, so the Configure UI can
//  repopulate the model/reasoning dropdowns CLIENT-SIDE when the harness
//  <select> changes (no per-keystroke round-trip). Transcribed 1:1 from
//  app/agents/service.py build_config_catalog.
// ---------------------------------------------------------------------------

/** A harness <select> option (+ its lane metadata + model source). */
export interface HarnessOption {
  value: string
  label: string
  /** Dynamic catalog source (`claude-catalog`, `codex-catalog`, `pi-catalog`, or provider `catalog`). */
  model_source: 'fixed' | 'catalog' | string
  lane?: string | null
  lane_label?: string | null
}

/** A model <select> option. `provider` is present on the catalog (kaidera/pi) lanes, for `<optgroup>` grouping. */
export interface ModelOption {
  value: string
  label: string
  provider?: string
  /**
   * This model's own discovered reasoning levels. The reasoning dropdown shows
   * these instead of a per-harness fallback. Empty means a known non-reasoner;
   * absent means capability metadata was unavailable.
   * `['supported']` ⇒ reasons but no selectable ladder (a binary toggle).
   */
  reasoning_levels?: string[]
}

/** A reasoning/effort <select> option (uniform {value,label} with the model options). */
export interface ReasoningOption {
  value: string
  label: string
}

/** GET /agents/{project}/config-catalog — the full Configure catalog. */
export interface AgentConfigCatalog {
  project: string
  harnesses: HarnessOption[]
  models_by_harness: Record<string, ModelOption[]>
  /** Operator-added fixed-lane model rows, separated from built-ins so the UI can append safely. */
  custom_models_by_harness?: Record<string, ModelOption[]>
  reasoning_by_harness: Record<string, ReasoningOption[]>
  /**
   * Per-model reasoning options keyed as `<harness>:<model value>`, preventing
   * collisions when two harnesses expose the same model id with different effort
   * support. The client still accepts legacy model-only keys while upgrading.
   */
  reasoning_by_model?: Record<string, ReasoningOption[]>
  default_harness: string
  default_model: string
}

// ---------------------------------------------------------------------------
//  Runs — GET /runs/{project}  ·  /runs/run/{id}  ·  /runs/{project}/by-handoff/{hid}
//  and the SSE first-paint selected-run shape (same view-model).
// ---------------------------------------------------------------------------

export type RunStatusLabel = 'queued' | 'running' | 'completed' | 'errored' | string

/** A run RAIL header row. `RunsService.store_run_row`. */
export interface RunRow {
  run_id: string
  project: string | null
  agent: string | null
  agent_display: string | null
  handoff_id: string | null
  handoff_short: string | null
  model: string | null
  harness: string | null
  status: string
  running: boolean
  started_ts: string | null
  updated_ts: string | null
  started_ago: string
  updated_ago: string
  status_label: RunStatusLabel
}

/** A transcript segment (RunSpan.kind → seg-{kind}). */
export interface RunSegment {
  kind: string
  text: string
}

/** A run WITH its hydrated body. `RunsService.store_transcript_view` (extends RunRow). */
export interface RunTranscript extends RunRow {
  error: string | null
  ended_ts: string | null
  ended_ago: string
  segments: RunSegment[]
  body: string
  truncated: boolean
}

/** GET /runs/{project} — the run board. */
export interface RunBoard {
  project: string
  active: RunRow[]
  active_count: number
  recent: RunRow[]
  recent_count: number
}

/** POST /runs/run/{run_id}/cancel — explicit best-effort stop for an in-flight run. */
export interface CancelRunResult {
  run_id: string
  ok?: boolean
  cancelled?: boolean
  status?: string | null
  error?: string | null
  [k: string]: unknown
}

// ---------------------------------------------------------------------------
//  Dispatch — GET /dispatch/{project}/board
// ---------------------------------------------------------------------------

/**
 * The proposed-agent sub-object on a dispatch row. Transcribed 1:1 from
 * `DispatchService.propose_agent`, which returns
 * `{name, display_name, harness, harness_label, model, matched_on}` (or the row's
 * `proposed` is null → 'unassigned'). Kept permissive for any extra shaping.
 */
export interface DispatchProposal {
  name?: string | null
  display_name?: string | null
  harness?: string | null
  harness_label?: string | null
  model?: string | null
  matched_on?: string | null
  resolution_status?: string | null
  resolution_reason_code?: string | null
  resolution_reason?: string | null
  [k: string]: unknown
}

/** Routing explanation for a dispatch row. */
export interface DispatchResolution {
  status?: 'resolved' | 'blocked' | 'unresolved' | string
  reason_code?: string | null
  reason?: string | null
  target_type?: 'agent' | 'role' | 'none' | string
  target?: string | null
  matched_on?: string | null
  [k: string]: unknown
}

export type HandoffPolicyObject = Record<string, unknown>

/**
 * One dispatch-board row. Keys verified against the live board:
 * id, compound, summary, summary_full, from_agent, to_target, priority,
 * proposed, execution policy objects, created_at. `proposed` is the rule-based
 * proposal sub-object (or null when unassigned). Kept permissive for any
 * additional shaping.
 */
export interface DispatchRow {
  id: string
  compound?: string
  summary?: string
  summary_full?: string
  from_agent?: string | null
  to_target?: string | null
  priority?: string
  proposed?: DispatchProposal | null
  resolution?: DispatchResolution | null
  resolution_status?: string | null
  resolution_reason_code?: string | null
  resolution_reason?: string | null
  acceptance?: HandoffPolicyObject
  evidence?: HandoffPolicyObject
  retry?: HandoffPolicyObject
  escalation?: HandoffPolicyObject
  created_at?: string | null
  [k: string]: unknown
}

export interface DispatchBoard {
  project: string
  rows: DispatchRow[]
  dispatch_count: number
  dispatch_proposed_count: number
  dispatch_unassigned_count: number
  autonomous_on: boolean
  propose_mode_on: boolean
  awaiting_approval_ids: string[]
  // The live board also includes `active_view` + `selected_key` (HTML-context
  // carryovers) — addressable via the index signature, not needed by the SPA.
  [k: string]: unknown
}

// ---------------------------------------------------------------------------
//  Dispatch — GET /dispatch/{project}/activity  (orchestrator ring + the wave plan)
//
//  Transcribed 1:1 from main._dispatch_activity_context: the orchestrator's
//  in-memory activity ring (newest-first) + the E007 per-epic wave summary + the
//  live loop/inflight telemetry. A None / degraded orchestrator yields the clean
//  idle/empty payload (empty activity, no waves, OFF) — never a 500.
// ---------------------------------------------------------------------------

/** One activity-feed row (the orchestrator ActivityFeed ring, shaped for the SPA). */
export interface DispatchActivityItem {
  kind: string
  level: string
  text: string
  agent?: string | null
  handoff_short?: string | null
  /** A compact relative age ('now' · 'Ns' · 'Nm' · 'Nh' · 'Nd'); '' when unknown. */
  ago: string
}

/** One epic's wave summary (E007 Phase 1.5). `active_wave` null → all waves complete. */
export interface DispatchWave {
  epic: string
  active_wave: number | null
  running: number
  waiting: number
}

/** GET /dispatch/{project}/activity — the activity feed + wave-plan strip. */
export interface DispatchActivity {
  project: string
  activity: DispatchActivityItem[]
  activity_count: number
  waves: DispatchWave[]
  waves_any: boolean
  loop_running: boolean
  inflight: number
  cap: number
  /** True only when the orchestrator loop itself didn't start (degrade copy). */
  no_orch: boolean
}

// ---------------------------------------------------------------------------
//  Automation feeders — durable schedules
// ---------------------------------------------------------------------------

export type ScheduledJobSchedule = Record<string, unknown>
export type ScheduledJobPayload = Record<string, unknown>

export interface ScheduledJob {
  project: string
  id: string
  name: string
  enabled: boolean
  schedule: ScheduledJobSchedule
  payload: ScheduledJobPayload
  next_run_at?: string | null
  last_run_at?: string | null
  last_status?: string | null
  last_error?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface ScheduledJobsResult {
  jobs: ScheduledJob[]
  connected: boolean
}

export interface ScheduledJobWritePayload {
  id?: string
  name: string
  enabled?: boolean
  schedule: ScheduledJobSchedule
  payload: ScheduledJobPayload
  next_run_at?: string | null
}

export interface ScheduledJobWriteResult {
  ok: boolean
  job?: ScheduledJob | null
  error?: string | null
}

export interface ScheduledJobRunNowResult {
  ok: boolean
  job?: ScheduledJob | null
  error?: string | null
}

export interface PlanningBeatStatus {
  connected: boolean
  configured: boolean
  job?: ScheduledJob | null
  recommended: {
    from_agent?: string
    to_agent?: string
    to_role?: string
    every_minutes?: number
    mode?: string
    skill?: string
  }
}

export interface PlanningBeatWritePayload {
  enabled?: boolean
  from_agent?: string
  planner_agent?: string
  to_role?: string
  every_minutes?: number
  summary?: string
}

export interface AutomationDeleteResult {
  ok: boolean
  deleted: boolean
  id?: string
  error?: string | null
}

export interface AutomationFeedersExportResult {
  project: string
  version: number
  scheduled_jobs: ScheduledJob[]
  connected: boolean
}

export interface AutomationFeedersImportPayload {
  scheduled_jobs?: ScheduledJobWritePayload[]
}

export interface AutomationFeedersImportError {
  kind: 'scheduled_job' | string
  id?: string | null
  index?: number | null
  error: string
}

export interface AutomationFeedersImportResult {
  ok: boolean
  imported: {
    scheduled_jobs: number
  }
  errors: AutomationFeedersImportError[]
  error?: string | null
}

// ---------------------------------------------------------------------------
//  Analytics — GET /analytics/{project}/usage
// ---------------------------------------------------------------------------

/** A proportional bar row (label + value + a 0–100 pct of the top bar). `_bar_rows`. */
export interface UsageBar {
  label: string
  value: number
  value_h: string
  pct: number
}

/** A row of the "total usage by model" table. `shape_usage_cost` by_model_table. */
export interface UsageModelRow {
  model: string
  provider: string
  tokens: number
  tokens_h: string
}

/** A provider group (total + its models), for the model×provider breakdown. */
export interface UsageProviderGroup {
  provider: string
  label: string
  tokens: number
  tokens_h: string
  models: { model: string; tokens: number; tokens_h: string }[]
}

/** A per-agent usage + est-cost row (the model-usage-per-agent + cost tables). */
export interface UsageAgentRow {
  agent: string
  display: string
  model: string | null
  model_known: boolean
  provider: string | null
  tokens: number | null
  tokens_h: string | null
  input: number | null
  output: number | null
  priced: boolean
  price_in_h: string
  price_out_h: string
  cost: number | null
  cost_h: string
  cost_na_reason: string | null
}

/**
 * GET /analytics/{project}/usage — the usage + est-cost breakdown.
 *
 * Transcribed 1:1 from AnalyticsService.shape_usage_cost (docs/sdk/modules/
 * analytics.md §4). Every breakdown is empty when the store is down/empty;
 * `store_connected` + `total_runs` drive the graceful empty states. Kept with an
 * index signature for any additional shaping the service may carry.
 */
export interface UsageBreakdown {
  project: string
  store_connected: boolean
  total_runs: number
  total_tokens: number
  total_tokens_h: string | null
  by_model_bars: UsageBar[]
  by_model_table: UsageModelRow[]
  model_count: number
  by_provider: UsageProviderGroup[]
  by_provider_bars: UsageBar[]
  provider_count: number
  rows: UsageAgentRow[]
  agent_count: number
  agents_with_usage: number
  cost_rows: UsageAgentRow[]
  project_cost: number | null
  project_cost_h: string
  priced_agent_count: number
  [k: string]: unknown
}

/**
 * GET /analytics/{project}/kpis — the Analytics view's slim headline KPI strip.
 *
 * Transcribed 1:1 from app/analytics/api.py `kpis_endpoint`. The three /state counters +
 * `decisions_recent` are null when their Cortex read was unreachable (the view renders 'n/a' —
 * NEVER fabricated zeros); `tokens_recent`/`tokens_recent_h` come from the App-DB project token
 * rollup (the same total the usage breakdown shows). `window_days` is the trailing window the
 * decisions count covers (for the 'Decisions · Nd' label).
 */
export interface AnalyticsKpis {
  project: string
  events_24h: number | null
  active_tasks: number | null
  pending_handoffs: number | null
  decisions_recent: number | null
  window_days: number
  tokens_recent: number
  tokens_recent_h: string | null
}

// ---------------------------------------------------------------------------
//  Settings — GET /settings/{project}/flags  ·  /settings/{project}/app
// ---------------------------------------------------------------------------

export interface ProjectFlags {
  project: string
  autonomous: boolean
  propose_mode: boolean
}

export interface AppSettings {
  project: string
  settings: Record<string, unknown>
  store_connected: boolean
}

// ---------------------------------------------------------------------------
//  Settings WRITE responses — POST /settings/{project}/{flags|app|agents/.../config}
//  (Track C). Each echoes the authoritative post-write state + an `ok` flag (false
//  on a down store / failed write — the SPA stays truthful by refetching anyway).
// ---------------------------------------------------------------------------

/** POST /settings/{project}/flags → the authoritative flag state after the write. */
export interface FlagsWriteResult extends ProjectFlags {
  ok: boolean
}

/** POST /settings/{project}/app → the authoritative app-settings map after the write. */
export interface AppSettingsWriteResult extends AppSettings {
  ok: boolean
}

/**
 * POST /settings/{project}/agents/{agent}/config → the post-save effective override.
 *
 * Console-LOCAL by design (feature-gap #81, the CTO's reversed decision): a save writes
 * ONLY the console-local override — it does NOT touch the Cortex registry. Committing the
 * config to the registry is the separate, explicit `promoteAgent` action.
 */
export interface AgentConfigWriteResult {
  project: string
  agent: string
  override: Record<string, string>
  designation: string
  ok: boolean
  error?: string | null
}

/**
 * POST /settings/{project}/agents/{agent}/promote → the EXPLICIT "Promote to registry"
 * result (feature-gap #81). `ok=true` when the agent's current effective config was
 * pushed into the Cortex registry; a graceful `ok=false` + a human `error` when it
 * wasn't (Cortex unreachable / the console's writer isn't authorised / no resolvable
 * role). NEVER leaks a token. The console-local override is untouched either way.
 */
export interface PromoteResult {
  ok: boolean
  error: string | null
}

/** The mutable flag fields a `setFlags` call may carry (either/both; partial). */
export interface FlagsPatch {
  autonomous?: boolean
  propose_mode?: boolean
}

/** The agent-override fields a `setAgentConfig` call may carry (a blank value clears). */
export interface AgentOverridePatch {
  harness?: string
  model?: string
  reasoning?: string
  designation?: string
  role?: string
  auto_dispatch?: string
}

// ---------------------------------------------------------------------------
//  Settings — STEP 3a JSON catalogs (the [API]-gap endpoints the SPA tabs consume)
//
//  Transcribed from app/settings_module/service.py and api.py.
//  These are NOT guessed — each field maps to a key the service emits.
// ---------------------------------------------------------------------------

/** The closed set of System-form field types the schema JSON exposes. */
export type SystemFieldType = 'text' | 'number' | 'bool' | 'secret' | 'readonly' | 'select'

/**
 * One typed System-form field. Every field carries `key/label/type/group/help`.
 * The VALUE side is type-dependent:
 *   - text / number / readonly → `value` (the stored, already-typed value).
 *   - bool                     → `value` is a boolean.
 *   - secret                   → NEVER a raw value: `is_set` (whether a secret is
 *                                stored) + `placeholder` (the "•••• set" mask when
 *                                set, "" otherwise). `value` is always "" for a secret.
 * `placeholder` is the input placeholder for text/number, and the masked marker for
 * a secret.
 */
export interface SystemField {
  key: string
  label: string
  type: SystemFieldType
  group: string
  help: string
  placeholder: string
  /** Present for text/number/bool/readonly/select; always "" (never the raw secret) for a secret. */
  value?: unknown
  /** Present ONLY on a secret field — whether a secret is currently stored. */
  is_set?: boolean
  /** Present ONLY on a `select` field — the STATIC choosable options (e.g. the harnesses). */
  options?: string[]
  /** Present ONLY on a `select` field — a DYNAMIC options source the SPA resolves from
   *  live data (e.g. "projects" → the registered-project keys). When set, the SPA fills
   *  the dropdown from that source instead of (or in addition to) `options`. */
  options_source?: string
}

/** A System-form group (a collapsible card of typed fields). */
export interface SystemSchemaGroup {
  key: string
  label: string
  fields: SystemField[]
}

/** GET /settings/{project}/system-schema — the typed System form as JSON. */
export interface SystemSchema {
  project?: string
  groups: SystemSchemaGroup[]
  store_connected?: boolean
}

/** API-owned Cortex ingestion/search config — GET/POST /cortex/config. */
export interface CortexPlatformConfig {
  embedding_provider?: string
  embedding_model?: string
  embedding_dims?: number
  rerank_enabled?: boolean
  rerank_provider?: string
  rerank_model?: string
  embed_input_max_chars?: number
  rerank_input_max_chars?: number
  embed_timeout_ms?: number
  rerank_timeout_ms?: number
  updated_at?: string | null
  [k: string]: unknown
}

export interface CortexConfigResult {
  ok: boolean
  config: CortexPlatformConfig
  error: string | null
}

export interface CortexEmbeddingCoverage {
  total: number
  embedded: number
  backlog: number
  skipped: number
  pct: number
}

export interface CortexEmbeddingBacklogResult {
  ok: boolean
  project: string
  backlog: Record<string, number>
  coverage: Record<string, CortexEmbeddingCoverage>
  error: string | null
}

export interface CortexEmbeddingBackfillRequest {
  table?: string
  limit?: number
  chunk_size?: number
  max_errors?: number
  error_threshold?: number
  dry_run?: boolean
  async_job?: boolean
}

export interface CortexEmbeddingBackfillResult {
  ok: boolean
  project: string
  result: Record<string, unknown>
  error: string | null
}

export interface CortexEmbeddingJobResult {
  ok: boolean
  project: string
  job: Record<string, unknown>
  error: string | null
}

/**
 * POST /settings/{project}/workspace → the repo_root edit result. On success
 * `repo_root` is the new path + `previous_repo_root` the prior one (so the UI shows
 * `previous → new`); on failure `error` is a clear human string + `ok=false`.
 */
export interface WorkspaceResult {
  project: string
  project_key: string
  ok: boolean
  repo_root: string | null
  previous_repo_root: string | null
  error: string | null
}

// ---------------------------------------------------------------------------
//  Registration — feature-gap #81 (the in-console add-agent / add-project / remove)
//
//  Transcribed 1:1 from app/registration_api.py. Each write graceful-degrades to a
//  friendly `{ok, error}` (never a 500; the admin token is NEVER echoed). The
//  payloads are the request bodies the routes accept.
// ---------------------------------------------------------------------------

/** The new-agent form fields → POST /agents/{project}/register. name+role required; the
 * config fields fold into the registry `capabilities`; writer_scope is the register scope. */
export interface RegisterAgentPayload {
  name: string
  role: string
  harness?: string
  model?: string
  reasoning?: string
  designation?: string
  auto_dispatch?: string
  writer_scope?: string
  role_description?: string
}

/** POST /agents/{project}/register → the friendly echo. `ok=false` carries a human `error`
 * (e.g. the caller isn't a registered writer, or Cortex is unreachable). */
export interface RegisterAgentResult {
  ok: boolean
  agent: string | null
  role: string | null
  error: string | null
}

/** POST /agents/{project}/{agent}/deregister → the friendly echo. `ok=false`+`error` nudges
 * the admin-token requirement (the remove is admin-gated). */
export interface DeregisterAgentResult {
  ok: boolean
  removed: boolean
  agent: string | null
  error: string | null
}

/** The new-project form fields → POST /projects/register. project_key required; repo_root must
 * be ABSOLUTE (same rule as the workspace editor). */
export interface RegisterProjectPayload {
  project_key: string
  display_name?: string
  /** The project's initial SCOPE — what this project is for. Seeds the first lead
   *  worker's persona (its role/skills are built from this + the first conversation). */
  description?: string
  repo_root?: string
  repo_type?: string
  default_agent?: string
  /** Name for the seeded first lead worker (defaults to "lead" when blank). */
  lead_name?: string
  /** Optional installed pack key under repo_root/.kaidera-os/project-packs/<key>. */
  project_pack_key?: string
}

/** One installed project pack discovered from a project-owned `.kaidera-os/project-packs` dir. */
export interface ProjectPackExtension {
  module: string
  required: boolean
  description?: string | null
  enabled: boolean
  loaded: boolean
  status:
    | 'loaded'
    | 'enabled_restart_required'
    | 'loaded_disable_restart_required'
    | 'disabled'
    | string
  restart_required: boolean
}

export interface ProjectPackPortal {
  key: string
  type?: string | null
  agent?: string | null
  route_prefix: string
  auth?: string | null
  stream_contract?: string | null
  runtime_contract?: {
    contract: string
    chat_endpoint_template: string
    stream_endpoint_template: string
    run_endpoint_template: string
    chat_events: string[]
    stream_events: string[]
    selected_payload?: {
      path: string
      segments_path: string
      segment_fields: string[]
      status_path: string
    }
    rules: string[]
  } | null
  frontend_path?: string | null
  frontend_exists: boolean
  required: boolean
  description?: string | null
  status: 'ready' | 'missing_frontend' | 'frontend_not_installed' | 'metadata_only' | string
}

export interface ProjectPackOption {
  key: string
  name: string
  version: string
  description?: string | null
  default_project_key?: string | null
  seed_files: string[]
  seed_count: number
  extension_modules: string[]
  extensions?: ProjectPackExtension[]
  extensions_enabled?: string[]
  extension_env?: string | null
  extension_paths_env?: string | null
  extension_path?: string | null
  portals?: ProjectPackPortal[]
  restart_required?: boolean
}

/** GET /project-packs?repo_root=... → installed packs for the selected folder. */
export interface ProjectPackListResult {
  ok: boolean
  packs: ProjectPackOption[]
  error: string | null
}

export interface ProjectPackExtensionPatch {
  repo_root: string
  pack_key: string
  module: string
  enabled: boolean
}

export interface ProjectPackExtensionResult {
  ok: boolean
  pack: ProjectPackOption | null
  error: string | null
}

export interface ProjectPackIngestResult {
  key: string
  name?: string | null
  seed_files: string[]
  seed_count: number
  ingested: number
  errors: string[]
}

/** POST /projects/register → the friendly echo. `ok=false`+`error` nudges the admin-token
 * requirement (the project register is admin-gated). */
export interface RegisterProjectResult {
  ok: boolean
  project_key: string | null
  project_pack?: ProjectPackIngestResult | null
  error: string | null
}

/**
 * POST /agents/{p}/{a}/chat/upload — the client-safe echo for ONE uploaded chat
 * attachment (chat file-attachments, feature-gap step 6). The host path is NEVER
 * returned (server-side only), so this carries only the minted id + the metadata the
 * composer shows as a chip.
 */
export interface AttachmentUploadResult {
  attachment_id: string
  filename: string
  size_bytes: number
}

// ---------------------------------------------------------------------------
//  SSE — GET /runstate/stream  (event: runstate)
// ---------------------------------------------------------------------------

/** The `data` JSON of each `event: runstate` SSE frame (main._runstate_stream_gen). */
export interface RunStateFrame {
  project: string
  agent: string | null
  wake_run_id: string | null
  running: number
  count: number
  selected_id: string | null
  selected: RunTranscript | null
  html: string
}

export interface RunStateRestartRow {
  run_id: string | null
  project?: string | null
  agent?: string | null
  handoff_id?: string | null
  status?: string | null
  lease_owner?: string | null
  pid?: number | null
  heartbeat_at?: string | null
  updated_at?: string | null
  lifecycle: string
  restart_survivable: boolean
  needs_reconcile: boolean
}

export interface RunStateRestartStatus {
  ok: boolean
  project: string
  store: 'ok' | 'degraded' | 'error' | string
  current_pid?: number | null
  active: RunStateRestartRow[]
  counts: {
    active: number
    restart_survivable: number
    request_lived: number
    needs_reconcile: number
  }
  error?: string | null
}

// ---------------------------------------------------------------------------
//  Explain — POST /explain/{project}  ·  GET /explain/{project}/list
//
//  The visual code explainer (host-side generation → Cortex L5). The SPA STARTS a
//  generation (POST), then follows the run via the existing runs surface
//  (GET /runs/run/{run_id}) — the run's `output` spans carry the full self-contained
//  HTML as it streams, so the full document = concat(output spans). See
//  docs/sdk/modules/explain.md §4 + §8 (the documented API gap: Cortex search returns
//  only a 300-char preview + there is no full-artifact-by-id endpoint, so the FULL
//  HTML is read from the run's spans, NOT the result route).
// ---------------------------------------------------------------------------

/** The explain target kinds (mirrors the backend `_VALID_KINDS`). */
export type ExplainKind = 'project' | 'file' | 'blast' | 'dir' | 'diff'

/**
 * The `POST /explain/{project}` body. `kind` is required; the conditional inputs are
 * per-kind (`path` for file/dir, `fn_name` for blast, `git_rev` for diff — optional).
 * `repo` is resolved SERVER-SIDE from the project's repo_root (NOT client-supplied), so
 * it is omitted by the SPA. `harness`/`model` are optional overrides.
 */
export interface ExplainRequest {
  kind: ExplainKind
  repo?: string
  path?: string
  fn_name?: string
  git_rev?: string
  harness?: string
  model?: string
}

/** `POST /explain/{project}` → the started run. `accepted=false` when the host seam rejected. */
export interface ExplainStartResult {
  run_id: string
  accepted: boolean
  error: string | null
}

/**
 * One gallery item — `GET /explain/{project}/list` → `{artifacts: [...]}`.
 *
 * The gallery is now enumerated from the console's OWN run_state (every explain run is a
 * `lease_owner='explain'` row), NOT Cortex content search — which can't reliably
 * prefix-enumerate artifacts (the live-testing bug this fixed). So `run_id` is now
 * FIRST-CLASS in the payload (no source_file derivation needed), and `artifact_id` +
 * `target_kind`/`target_path` + `caption` + `created_at` + `status` come from the run's
 * `metadata` sidecar + header. `source_file` (= `explain/<run_id>.html`) is still sent
 * for back-compat (the client keeps a derive-from-source_file fallback). "View" loads
 * the full HTML from the run's spans (`GET /runs/run/{run_id}`) — the unchanged render
 * path. `artifact_id` may be null (still generating / L5 write degraded).
 */
export interface ExplainListItem {
  artifact_id: string | null
  run_id: string | null
  caption: string
  source_file: string
  modality: string
  target_kind?: string | null
  target_path?: string | null
  created_at?: string | null
  /** Run status plus the read-model-only 'recovered' state for errored runs with valid HTML. */
  status?: string | null
}

/** `GET /explain/{project}/list` → the gallery payload. */
export interface ExplainList {
  artifacts: ExplainListItem[]
}

// ---------------------------------------------------------------------------
//  Workspace — GET /workspace/{project}/filetree?path=
//
//  One directory of the project's working folder (repo_root), lazy-loaded per folder
//  expand by the right-side Workspace column. Secure walk server-side (ws.list_dir).
// ---------------------------------------------------------------------------

/** One entry in a workspace directory. `path` is relative to repo_root. */
export interface WorkspaceEntry {
  name: string
  path: string
  is_dir: boolean
  size: number | null
}

/** `GET /workspace/{project}/filetree?path=` → one directory's listing. */
export interface WorkspaceTree {
  path: string
  entries: WorkspaceEntry[]
  error?: string
}

/** `GET /workspace/{project}/filecontent?path=` → one file's content for the viewer/editor. */
export interface WorkspaceFile {
  path: string
  size: number
  binary: boolean
  truncated: boolean
  content: string | null
  lines: number | null
  error?: string
}

// ---------------------------------------------------------------------------
//  Plan — GET /plan/{project}/list  ·  GET /plan/{project}/file?path=  ·
//         GET /plan/{project}/status · POST /plan/{project}/bootstrap
//
//  The Visual Plan read surface: `.mdx` plans authored with the visual-plan skill under
//  the project's `docs/plans/`. Bootstrap creates a handoff asking the project lead to
//  author the first plan; it does not write files directly.
// ---------------------------------------------------------------------------

/** One plan file under `docs/plans/`. `path` is relative to that root. */
export interface PlanListItem {
  path: string
  name: string
  slug: string
  kind: 'plan' | 'canvas' | 'prototype' | 'recap'
  size: number
  /** st_mtime epoch seconds — the list is sorted newest-first. */
  modified_at: number
}

/** `GET /plan/{project}/list` → the plan index. */
export interface PlanList {
  plans: PlanListItem[]
}

/** `GET /plan/{project}/file?path=` → one plan's raw MDX. */
export interface PlanFile {
  path: string
  text: string
}

/** `GET /plan/{project}/status` → v2 project plan readiness metadata. */
export interface PlanStatus {
  project: string
  ready: boolean
  has_repo_root: boolean
  repo_root?: string
  has_plan: boolean
  plan_count: number
  lead: string
  latest_plan?: PlanListItem | null
  recommended_path: string
  bootstrap_available: boolean
  reason?: string | null
}

/** Request body for `POST /plan/{project}/bootstrap`. */
export interface PlanBootstrapRequest {
  title?: string
  objective?: string
  slug?: string
}

/** `POST /plan/{project}/bootstrap` → lead handoff creation result. */
export interface PlanBootstrapResult {
  ok: boolean
  lead?: string | null
  path?: string | null
  handoff?: Record<string, unknown> | null
  error?: string | null
}

// ---------------------------------------------------------------------------
//  Graph — GET /graph/{project}  ·  GET /graph/{project}/search?q=&limit=
//
//  The knowledge/code-graph view (the marquee feature-gap #80). The backend shapes
//  Cortex's dual-level `/cortex-graph-search` (L4 entities → nodes; relationships →
//  edges) + `/graph/stats` into a cytoscape-AGNOSTIC `{nodes, edges, stats}` — BOUNDED at
//  ~140 nodes (the search hits + their 1-hop neighbours), never the whole ~5,868-node
//  graph. The SPA `GraphView` maps these onto cytoscape elements. Transcribed 1:1 from
//  app/graph/shape.py + app/graph/api.py (the shaped node/edge/stats shapes).
// ---------------------------------------------------------------------------

/** The node-kind family — drives the colour palette (code = L3 file/fn, work = L1 handoff/
 * task/agent, mem = L4 concept/decision/lesson/…). Mirrors the backend `entity_kind`. */
export type GraphNodeKind = 'code' | 'mem' | 'work' | string

/** One graph NODE (cytoscape-agnostic). `hit=1` for a direct search-entity, `0` for a
 * synthesised 1-hop neighbour. `full` is the un-truncated name (the label is clipped). */
export interface GraphNode {
  id: string
  label: string
  full: string
  kind: GraphNodeKind
  etype: string
  desc: string
  /** 1 for a direct search hit, 0 for a 1-hop neighbour (the SPA draws hits larger). */
  hit: number
  /** Memory graph only: how many source refs contributed to this entity. */
  source_count?: number | null
  /** Memory graph only: last update timestamp carried by Cortex, when available. */
  updated_at?: string | null
}

/** One graph EDGE (a relationship). `label` is the relationship_type. */
export interface GraphEdge {
  id: string
  source: string
  target: string
  label: string
}

/** One per-repo row in the stats header's repo context (own flagged). */
export interface GraphRepoStat {
  name: string | null
  nodes: number
  edges: number
  is_own: boolean
}

/** One Cortex layer status row rendered above the graph canvas. */
export interface GraphLayerStat {
  id: 'L1' | 'L2' | 'L3' | 'L4' | 'L5' | 'L6' | string
  name: string
  status: string
  count?: number | null
  edges?: number | null
  backlog?: number | null
  detail?: string | null
}

/**
 * The graph stats block — the bounded-view header. `own_*` are this project's own
 * code-graph repo counts; `total_*` are the cross-repo totals; `shown_*` are what's
 * actually in this payload; `total_shown_nodes` is the M for "showing N of M". Counts
 * are null when absent (a down/empty Cortex) so the header renders '—'.
 */
export interface GraphStats {
  own_nodes: number | null
  own_edges: number | null
  total_nodes: number | null
  total_edges: number | null
  repo_count: number
  repos: GraphRepoStat[]
  shown_nodes: number
  shown_edges: number
  /** M for the "showing N of M nodes" note (own-repo total, else cross-repo total, else null). */
  total_shown_nodes: number | null
  kind_counts: { code: number; mem: number; work: number }
  /** L4 Cortex entity stats for the selected project. */
  entity_count?: number
  relationship_count?: number
  source_counts?: Record<string, number>
  backlog?: Record<string, number>
  /** Cortex L1-L6 layer status for this project. */
  layers?: GraphLayerStat[]
  node_cap: number
  /** True only when the ~140 cap actually clipped the neighbourhood ("search to explore"). */
  capped: boolean
  /** Backend graph mode: bounded search neighbourhood or project memory graph. */
  mode?: 'search' | 'memory' | string
}

/** `GET /graph/{project}` or `…/search` → the bounded node-edge graph + stats. */
export interface GraphPayload {
  nodes: GraphNode[]
  edges: GraphEdge[]
  stats: GraphStats
}

// ---------------------------------------------------------------------------
//  History — GET /history/{project}?limit=N
//
//  The cross-agent activity-timeline view (feature-gap #80, the second piece). The backend
//  shapes Cortex's noisy `/history` stream into a readable `events` timeline (each row run
//  through the PORTED summariser — a clean line, NOT raw tool-call JSON), folds a recent-
//  `decisions` feed (from `/search`) + a roster `agent_count`. Transcribed 1:1 from
//  app/history/shape.py + app/history/api.py. Every section graceful-degrades to []/0 on a
//  down/empty Cortex (the route never 500s).
// ---------------------------------------------------------------------------

/** A timeline row's kind — drives the row tint (say = a message, tool = an action, think =
 * a reasoning step). Mirrors the backend summariser's `kind`. */
export type HistoryEventKind = 'say' | 'tool' | 'think' | string

/** One activity-timeline EVENT (a summarised /history row). `summary` is the readable line
 * (the detail, e.g. a shell cmd, is already folded in) — NEVER the raw noisy content. */
export interface HistoryEvent {
  /** The raw ISO timestamp ('' when the row carried none). */
  ts: string
  /** A compact 'how long ago' label ('now' · 'Ns' · 'Nm' · 'Nh' · 'Nd'); '' when unknown. */
  ts_ago: string
  agent: string
  role: string
  kind: HistoryEventKind
  /** A human noun for the kind ('message' | 'action' | 'reasoning' | 'activity'). */
  kind_label: string
  summary: string
}

/** One recent-`decisions` feed row (from `/search`). `ts`/`ts_ago`/`agent` are best-effort
 * ('' when the search row carries none). `source` is the layer (decisions/lessons/...). */
export interface HistoryDecision {
  ts: string
  ts_ago: string
  agent: string
  summary: string
  source: string
  category: string
}

/** `GET /history/{project}` → the activity timeline + recent-decisions feed + roster count. */
export interface HistoryPayload {
  events: HistoryEvent[]
  decisions: HistoryDecision[]
  agent_count: number
}

// ---------------------------------------------------------------------------
//  Skills — GET /skills/{project}, POST /skills/{project}/install,
//           POST /skills/{project}/{slug}/bind
//
//  The Skills tab: browse the installed skills, install a new one from a GitHub URL, and
//  bind a skill to an agent/role. The console proxies the live Cortex skills surface (the
//  catalogue read + the bind write) and shells out to the `cortex-skill install` CLI for the
//  clone/register. Every section graceful-degrades server-side (an empty catalogue / a
//  friendly `{ok,error}`), so the view never sees a raw 500.
// ---------------------------------------------------------------------------

/** A skill's delivery scope: `global` (the shared skills repo — reaches every project/agent at
 * boot, no binding) · `project` / `agent` (bound to a subject to be delivered). */
export type SkillScope = 'global' | 'project' | 'agent' | string

/** One row in the skills catalogue (a Cortex `agent_skills` row). `skill_slug` is the stable id
 * the bind/delete target; the rest describe the skill. Optional fields carry through whatever the
 * Cortex row holds (the view only relies on slug/name/description/scope/version/status). */
export interface SkillRow {
  /** The Cortex row id (uuid). Optional — the view keys on `skill_slug`. */
  id?: string
  /** The owning project ('*' for a global skill). */
  project?: string
  /** The stable slug — the bind/install/delete target. */
  skill_slug: string
  name?: string
  description?: string
  scope: SkillScope
  version?: string
  /** active | retired | ... (the view tints a non-active row). */
  status?: string
  /** The skill body pointer (e.g. `.agents/skills/<slug>/SKILL.md`). */
  body_ref?: string
  skill_type?: string
  trust_tier?: string
}

/** `GET /skills/{project}` → the global + this-project skills catalogue. */
export interface SkillsPayload {
  skills: SkillRow[]
}

/** `POST /skills/{project}/install` → the install result + the REFRESHED catalogue (so the view
 * re-renders without a second call). `ok=false` + `error` on a blank url / a failed CLI install
 * (the error is a friendly, non-leaky line). */
export interface SkillInstallResult {
  ok: boolean
  error: string | null
  skills: SkillRow[]
}

/** `POST /skills/{project}/{slug}/bind` → the bind echo. `ok=false` + `error` on a blank subject /
 * a degraded write (Cortex unreachable / the console's writer isn't authorised). */
export interface SkillBindResult {
  ok: boolean
  slug: string
  subject: string | null
  error: string | null
}
