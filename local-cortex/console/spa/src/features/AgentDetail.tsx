/**
 * AgentDetail — the MAIN area. The selected agent's detail + its LIVE run.
 *
 * Layout: an agent header (identity + effective config from /agents/.../detail),
 * a thin run RAIL (the agent's recent runs from the run board, selectable), and
 * the continuous chat/thinking feed. The feed stitches the agent's recent runs
 * into one stream, while the SSE hook (useRunStateStream) overlays the selected
 * run with its freshest live transcript.
 *
 * Note: the agent's run rail is derived by filtering the project run board to this
 * agent — the dedicated per-agent rail endpoint is the live SSE first-paint; here
 * the board gives us the recent set without a second round-trip.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, useChatSend, useResource, useRunStateStream } from '../api'
import {
  clearActiveChatRun,
  isTerminalRun,
  readActiveChatRun,
} from '../api/activeChatRun'
import { GlassModal, GlassPanel, StatusDot } from '../components/glass'
import { cx } from '../components/ui'
import { ChatComposer } from './ChatComposer'
import { AgentFeedView } from './AgentFeedView'
import { AgentConfigEditor } from './AgentConfigEditor'
import { supportsVisionAttachments } from './attachmentCapabilities'
import type { AgentConfigEditorClient } from './AgentConfigEditor'
import type { DeregisterClient } from './RegistrationForms'
import type { AgentDetail as AgentDetailT, RunBoard, RunRow } from '../api'
import type { ProvidersConfig, UsageBreakdown } from '../api/types'

/**
 * The default in-pane config-editor client — built from the real `api`. The editor's
 * `agentConfigView` reads off the SAME `GET …/detail` route the header uses (its
 * `config_view` is the resolved effective+registry+override-flag shape), and the save
 * is `api.setAgentConfig`. The shell can inject a fake for tests.
 */
const apiConfigClient: AgentConfigEditorClient = {
  configCatalog: (project, signal) => api.configCatalog(project, signal),
  agentConfigView: async (project, agent, signal) => {
    const d = await api.agentDetail(project, agent, signal)
    return d.config_view
  },
  setAgentConfig: (project, agent, override) => api.setAgentConfig(project, agent, override),
  // Explicit "Promote to registry" (feature-gap #81) — distinct from the console-local Save.
  promoteAgent: (project, agent) => api.promoteAgent(project, agent),
  setAppSettings: (project, settings) => api.setAppSettings(project, settings),
}

interface AgentDetailProps {
  project: string | null
  agent: string | null
  /** The shared project run board (already polled by the shell). */
  runBoard: RunBoard | null
  /** The in-pane config-editor data client (defaults to the real `api`). */
  configClient?: AgentConfigEditorClient
  /**
   * Called after a successful in-pane config save — the shell refetches catalogs so a
   * designation change REGROUPS the agents column (same effect a Settings save had).
   */
  onConfigSaved?: () => void
  /** Optional deregister client (feature-gap #81) — enables the "Deregister" action in the
   * config modal. When absent, no remove action. */
  registrationClient?: DeregisterClient
  /** Called after a successful deregister — the shell refetches the roster (the agent disappears). */
  onAgentRemoved?: () => void
  /**
   * The configured/active providers (key-presence per provider). Used to softly gate the chat
   * composer (T1.7): when no key is set, Send is disabled with a hint. Optional + null-safe —
   * when absent (or empty), the composer is treated as ready so it NEVER falsely blocks.
   */
  providersConfig?: ProvidersConfig | null
  /**
   * Rename the seeded "lead" worker (T1.6). When provided AND the selected agent is `lead`, the
   * header shows a small Rename affordance; on save with a non-blank name this is called with the
   * trimmed value. Optional — absent ⇒ no rename affordance.
   */
  onRenameLead?: (newName: string) => void
  /** The selected project's on-disk repo_root (git worktree) — shown in the status line. */
  repoRoot?: string | null
}

/** The agent's runs, newest-first, filtered out of the project board. */
function agentRuns(board: RunBoard | null, agent: string | null): RunRow[] {
  if (!board || !agent) return []
  const target = agent.toLowerCase()
  const rows = [...board.active, ...board.recent].filter(
    (r) => (r.agent ?? '').toLowerCase() === target,
  )
  // De-dup by run_id (active + recent can overlap), preserve first (active) order.
  const seen = new Set<string>()
  const out: RunRow[] = []
  for (const r of rows) {
    if (seen.has(r.run_id)) continue
    seen.add(r.run_id)
    out.push(r)
  }
  return out
}

/** A compact worktree label: the last two path segments, prefixed with '…/' when deeper. */
function worktreeTail(path: string): string {
  const parts = path.replace(/\/+$/, '').split('/').filter(Boolean)
  return parts.length <= 2 ? path : '…/' + parts.slice(-2).join('/')
}

function AgentWorkStatus({
  agentLabel,
  thinking,
  working,
  statusLabel,
  stopping = false,
  onStop,
}: {
  agentLabel: string
  thinking: boolean
  working: boolean
  statusLabel?: string | null
  stopping?: boolean
  onStop?: () => void
}) {
  if (!thinking && !working) return null
  const verb = thinking ? 'thinking' : 'working'
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex shrink-0 items-center gap-2 border-t border-glass-line bg-base-950/80 px-5 py-2 text-[11px] text-ink-400"
      data-agent-work-status
    >
      <StatusDot status="running" pulse />
      <span className="font-medium text-ink-200">
        {agentLabel} is {verb}
      </span>
      <span className="flex w-6 items-center gap-0.5" aria-hidden="true">
        <span className="h-1 w-1 animate-pulse rounded-full bg-mint-300" />
        <span className="h-1 w-1 animate-pulse rounded-full bg-mint-300 [animation-delay:120ms]" />
        <span className="h-1 w-1 animate-pulse rounded-full bg-mint-300 [animation-delay:240ms]" />
      </span>
      {statusLabel && (
        <>
          <span aria-hidden className="text-ink-600">·</span>
          <span className="truncate text-ink-500">{statusLabel}</span>
        </>
      )}
      {onStop && (
        <button
          type="button"
          onClick={onStop}
          disabled={stopping}
          title="Stop this running worker"
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-run-errored/10 px-2.5 py-1 text-[11px] font-medium text-run-errored ring-1 ring-run-errored/30 transition-colors hover:bg-run-errored/20 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <svg
            viewBox="0 0 24 24"
            fill="currentColor"
            className="h-3 w-3"
            aria-hidden="true"
          >
            <rect x="6" y="6" width="12" height="12" rx="1.5" />
          </svg>
          {stopping ? 'Stopping...' : 'Stop'}
        </button>
      )}
    </div>
  )
}

export function AgentDetail({
  project,
  agent,
  runBoard,
  configClient = apiConfigClient,
  onConfigSaved,
  registrationClient,
  onAgentRemoved,
  providersConfig,
  onRenameLead,
  repoRoot,
}: AgentDetailProps) {
  // Agent header detail (override-first config). Polls lightly.
  const detail = useResource<AgentDetailT>(
    project && agent ? (signal) => api.agentDetail(project, agent, signal) : null,
    [project, agent],
    { pollMs: 20000 },
  )

  // Project usage breakdown — for the status line's per-agent tokens · cost. Polls slowly.
  const usage = useResource<UsageBreakdown>(
    project ? (signal) => api.usage(project, signal) : null,
    [project],
    { pollMs: 30000 },
  )

  // Which run is pinned in the rail (null = follow running/newest). The pin is
  // SCOPED to its (project, agent) so it auto-resets on a switch with no effect —
  // a stale pin for a different agent is simply ignored (derived, not mutated).
  const scopeKey = `${project ?? ''}/${agent ?? ''}`
  const [pinned, setPinned] = useState<{
    key: string
    run: string
    restored: boolean
  } | null>(null)
  const scopedPin = pinned && pinned.key === scopeKey ? pinned : null
  const pinnedRun = scopedPin?.run ?? null
  const restoredPin = scopedPin?.restored ?? false
  const pinRun = useCallback(
    (run: string | null, restored = false) =>
      setPinned(run ? { key: scopeKey, run, restored } : null),
    [scopeKey],
  )

  // Whether the per-agent CONFIG popup is open (the CTO's "config behind a Config
  // button next to the live indicator" directive). It's a plain boolean — the editor
  // inside is keyed by (project, agent) so it always reflects the selected agent; a
  // project/agent switch is handled by the editor's own scope reset, but we also close
  // the popup on a switch (below) so a stale modal never lingers over a new agent.
  const [configOpen, setConfigOpen] = useState(false)
  const [configScope, setConfigScope] = useState(scopeKey)
  const [cancellingRunId, setCancellingRunId] = useState<string | null>(null)
  const [cancelError, setCancelError] = useState<string | null>(null)
  if (configScope !== scopeKey) {
    setConfigScope(scopeKey)
    setConfigOpen(false)
  }

  const runs = useMemo(() => agentRuns(runBoard, agent), [runBoard, agent])
  // This agent's usage+cost row (matched by name) out of the project usage breakdown.
  const usageRow = useMemo(
    () =>
      usage.data?.cost_rows?.find(
        (r) => (r.agent ?? '').toLowerCase() === (agent ?? '').toLowerCase(),
      ) ?? null,
    [usage.data, agent],
  )
  const head = detail.data?.agent
  const cfg = detail.data?.config_view
  // Capability gates — mirror app.domain.designation (the backend single owner of
  // these rules): ONLY an `interactive` agent shows a chat box; `autonomous` +
  // `deterministic` agents run without one (their activity still streams above). The
  // stored `autonomous` value reads as "non-interactive" to match the product wording.
  const designation = detail.data?.designation ?? ''
  const isInteractive = designation === 'interactive'
  const designationLabel =
    designation === 'autonomous' ? 'non-interactive' : designation || '—'
  const imageAttachmentsEnabled = supportsVisionAttachments(
    cfg?.harness ?? head?.harness,
    cfg?.model ?? head?.model,
  )

  // Soft provider-readiness gate for the composer (T1.7). Derived to NEVER falsely block:
  // with no providersConfig (resource absent/loading) OR at least one key set, chat is ready;
  // only a loaded config with zero keys disables Send (and shows the hint).
  const chatReady =
    !providersConfig || (providersConfig.providers ?? []).some((p) => p.key_is_set)

  // Rename-the-seeded-lead affordance (T1.6) — local open/value state for the inline editor
  // shown in the header only when this is the seeded `lead` worker and a handler is wired.
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const canRenameLead = agent === 'lead' && !!onRenameLead
  const submitRename = useCallback(() => {
    const name = renameValue.trim()
    if (!name) return
    onRenameLead?.(name)
    setRenameValue('')
    setRenameOpen(false)
  }, [renameValue, onRenameLead])

  // Interactive chat SEND: on the `run` frame's run_id we PIN the transcript at that
  // run, so the reply streams in via the SAME run-state SSE (useRunStateStream below)
  // autonomous runs use — the durable source. The composer also shows an instant echo
  // + the local-mode delta text while the run-state catch-up lands.
  const chat = useChatSend({
    project,
    agent,
    onRun: useCallback((runId: string) => pinRun(runId), [pinRun]),
  })

  // RECONNECT RESTORE: if this (project, agent, chat session) had an active chat
  // run when the page was reloaded, re-pin it into the existing run-state stream.
  useEffect(() => {
    if (!project || !agent || !chat.sessionId || pinnedRun) return
    const restored = readActiveChatRun(project, agent, chat.sessionId)
    if (restored) queueMicrotask(() => pinRun(restored, true))
  }, [agent, chat.sessionId, pinRun, pinnedRun, project])

  // Live transcript over SSE, scoped to this agent (+ the pinned run if any).
  const { frame, status } = useRunStateStream({
    project,
    agent,
    run: pinnedRun,
    enabled: !!project && !!agent,
  })

  // The feed hydrates every run from /runs/run/{id}. If chat just minted a run
  // and the board poll has not seen it yet, inject a small queued row so the feed
  // can render/follow it without probing a durable detail record that does not
  // exist yet. The SSE transcript or real board row takes over next.
  const feedRuns = useMemo<RunRow[]>(() => {
    if (!pinnedRun || runs.some((r) => r.run_id === pinnedRun)) return runs
    return [
      {
        run_id: pinnedRun,
        project,
        agent,
        agent_display: head?.display_name ?? agent,
        handoff_id: null,
        handoff_short: null,
        model: null,
        harness: null,
        status: 'queued',
        running: true,
        started_ts: null,
        updated_ts: null,
        started_ago: '',
        updated_ago: 'now',
        status_label: 'starting',
      },
      ...runs,
    ]
  }, [agent, head?.display_name, pinnedRun, project, runs])

  const liveTranscript = frame?.selected ?? null
  const sseLive = status === 'open'

  // Clear only the persisted active-run marker when the followed run is terminal.
  // Keep the in-memory pin so the finished transcript stays visible until the board
  // poll catches up. If a restored run is missing/invalid server-side, unpin it.
  useEffect(() => {
    if (!project || !agent || !chat.sessionId || !pinnedRun) return

    const selected = liveTranscript?.run_id === pinnedRun ? liveTranscript : null
    if (selected && isTerminalRun(selected)) {
      clearActiveChatRun(project, agent, chat.sessionId, pinnedRun)
      return
    }

    const row = runs.find((r) => r.run_id === pinnedRun)
    if (row && isTerminalRun(row)) {
      clearActiveChatRun(project, agent, chat.sessionId, pinnedRun)
      return
    }

    if (restoredPin && frame && !frame.selected) {
      clearActiveChatRun(project, agent, chat.sessionId, pinnedRun)
      queueMicrotask(() => pinRun(null))
    }
  }, [
    agent,
    chat.sessionId,
    frame,
    liveTranscript,
    pinRun,
    pinnedRun,
    project,
    restoredPin,
    runs,
  ])

  // After an in-pane config save: refetch the header detail (so its effective config +
  // designation badge update) AND bubble to the shell (which regroups the agents column
  // on a designation change — the same refresh a Settings save used to trigger).
  const handleConfigSaved = useCallback(() => {
    detail.refetch()
    onConfigSaved?.()
  }, [detail, onConfigSaved])

  // An explicit operator/chat pin is authoritative. In particular, a stale stream
  // frame for a previously selected autonomous run must not replace a just-started
  // chat or redirect the Stop action at the wrong run.
  const selectedRunId =
    pinnedRun ??
    frame?.selected_id ??
    liveTranscript?.run_id ??
    runs.find((r) => r.running)?.run_id ??
    runs[0]?.run_id ??
    null
  const selectedRunRunning = Boolean(
    selectedRunId && feedRuns.some((r) => r.run_id === selectedRunId && r.running),
  )
  const workStatusLabel =
    liveTranscript?.status_label ||
    feedRuns.find((r) => r.run_id === selectedRunId)?.status_label ||
    (chat.sending ? 'starting run' : null)
  const selectedLiveRunning = Boolean(
    selectedRunId &&
      liveTranscript?.run_id === selectedRunId &&
      liveTranscript.running,
  )
  const canCancelSelectedRun = Boolean(selectedRunId && (selectedRunRunning || selectedLiveRunning))
  const cancelSelectedRun = useCallback(async () => {
    if (!selectedRunId || !canCancelSelectedRun || cancellingRunId) return
    setCancelError(null)
    setCancellingRunId(selectedRunId)
    try {
      await api.cancelRun(selectedRunId)
      if (project && agent && chat.sessionId) {
        clearActiveChatRun(project, agent, chat.sessionId, selectedRunId)
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setCancelError(message || 'Unable to stop the run.')
    } finally {
      setCancellingRunId(null)
    }
  }, [
    agent,
    canCancelSelectedRun,
    cancellingRunId,
    chat.sessionId,
    project,
    selectedRunId,
  ])

  if (!project || !agent) {
    return (
      <GlassPanel className="flex-1">
        <div className="flex h-full items-center justify-center p-10">
          <p className="text-sm text-ink-500">
            Select a worker to see its live work.
          </p>
        </div>
      </GlassPanel>
    )
  }

  return (
    <GlassPanel className="min-w-0 flex-1">
      {/* Agent header. */}
      <header className="flex items-center gap-3 border-b border-glass-line px-5 py-4">
        <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-mint-500/15 text-sm font-semibold text-mint-200">
          {head?.initials ?? agent.slice(0, 2).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-base font-semibold text-ink-100">
              {head?.display_name ?? agent}
            </h1>
            {/* Role from config (CPO/CMO/Lead/…), not a hardcoded CPO tag. */}
            {head?.role && (
              <span className="rounded bg-mint-500/15 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-mint-300">
                {head.role}
              </span>
            )}
            <span
              className={cx(
                'rounded bg-base-700/70 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide',
                isInteractive
                  ? 'text-ink-300'
                  : designation === 'deterministic'
                    ? 'text-ink-500'
                    : 'text-ink-400',
              )}
              title={
                isInteractive
                  ? 'Interactive Lead — chat + model'
                  : designation === 'deterministic'
                    ? 'Deterministic mini worker — no chat, no model'
                    : 'Non-interactive AI worker — model, no chat'
              }
            >
              {designationLabel}
            </span>
          </div>
          {/* Below the name/role/designation: just harness · model (no repeated role). */}
          <div className="mt-0.5 truncate text-xs text-ink-400">{head?.row_sub}</div>
        </div>
        {/* Live indicator + the Config button — one control cluster on the right.
            The config controls live BEHIND this button in a popup (the CTO's "Config
            button next to the live indicator" directive), keeping the pane itself
            clean: header + run rail + transcript + composer. */}
        <div className="flex shrink-0 items-center gap-2">
          {/* Rename the seeded "lead" worker (T1.6) — a compact inline editor that lets the
              operator give the onboarding-seeded lead a real name. Only shown for `lead`
              with a handler wired; otherwise nothing renders. */}
          {canRenameLead &&
            (renameOpen ? (
              <div className="flex items-center gap-1.5">
                <input
                  type="text"
                  autoFocus
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      submitRename()
                    } else if (e.key === 'Escape') {
                      e.preventDefault()
                      setRenameValue('')
                      setRenameOpen(false)
                    }
                  }}
                  placeholder="new name"
                  aria-label="New lead name"
                  className="glass-soft w-28 rounded-lg px-2 py-1.5 text-[11px] text-ink-100 placeholder:text-ink-500 focus:outline-none"
                />
                <button
                  type="button"
                  onClick={submitRename}
                  disabled={!renameValue.trim()}
                  className="rounded-lg bg-mint-500/15 px-2.5 py-1.5 text-[11px] font-medium text-mint-200 ring-1 ring-mint-400/40 transition-colors hover:bg-mint-500/25 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Save
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setRenameOpen(true)}
                title="Give this seeded lead a real name"
                className="glass-soft rounded-lg px-2.5 py-1.5 text-[11px] font-medium text-ink-300 transition-colors hover:border-mint-400/30 hover:bg-base-800/60 hover:text-ink-100"
              >
                Rename
              </button>
            ))}
          {/* SSE connection indicator. */}
          <div className="flex items-center gap-1.5" title={`live stream: ${status}`}>
            <StatusDot status={sseLive ? 'running' : 'idle'} pulse={sseLive} />
            <span className="text-[10px] uppercase tracking-wide text-ink-500">
              {sseLive ? 'live' : status}
            </span>
          </div>
          <button
            type="button"
            onClick={() => setConfigOpen(true)}
            title="Configure this worker — harness · model · reasoning · designation · role"
            className="glass-soft flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-medium text-ink-300 transition-colors hover:border-mint-400/30 hover:bg-base-800/60 hover:text-ink-100"
          >
            <svg
              viewBox="0 0 20 20"
              className="h-3.5 w-3.5"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.5}
              aria-hidden="true"
            >
              <circle cx="10" cy="10" r="2.6" />
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M10 1.8v2M10 16.2v2M3.8 3.8l1.4 1.4M14.8 14.8l1.4 1.4M1.8 10h2M16.2 10h2M3.8 16.2l1.4-1.4M14.8 5.2l1.4-1.4"
              />
            </svg>
            Config
          </button>
        </div>
      </header>

      {/* Per-agent CONFIG editor — now behind the header's Config button in a glass
          popup (the CTO's "config in a modal next to the live indicator" directive).
          The pane stays clean; the editor keeps ALL its behavior (harness-aware
          model/reasoning, designation, role, registry hints, override dots, save →
          POST + refetch + roster-regroup). Closing (×/backdrop/Esc) returns to the
          clean pane; the modal stays open after a save so the "Saved ✓" is visible. */}
      <GlassModal
        open={configOpen}
        onClose={() => setConfigOpen(false)}
        title={`Configure · ${head?.display_name ?? agent}`}
      >
        <AgentConfigEditor
          project={project}
          agent={agent}
          client={configClient}
          onSaved={handleConfigSaved}
          registrationClient={registrationClient}
          onRemoved={() => {
            // The agent is gone — close the config modal + ask the shell to refetch.
            setConfigOpen(false)
            onAgentRemoved?.()
          }}
        />
      </GlassModal>

      {/* Continuous chat/thinking feed. */}
      <div className="min-h-0 flex-1">
        <AgentFeedView
          runs={feedRuns}
          liveTranscript={liveTranscript}
          selectedRunId={selectedRunId}
          live={sseLive}
          emptyHint={
            runs.length === 0
              ? 'This agent has no runs yet. Send a message below to start one.'
              : 'Loading transcript…'
          }
        />
      </div>

      {/* Status line — worktree · runs · tokens · cost · tasks · sub-agents (cmux/Claude
          Code style). Model+harness already in the header. The tasks/sub-agents segments
          appear ONLY when the current chat turn reported them (a normal one-shot turn
          shows nothing extra). */}
      <div className="flex shrink-0 items-center gap-2.5 overflow-x-auto border-t border-glass-line px-5 py-1.5 text-[11px] text-ink-500">
        {repoRoot && (
          <span className="flex min-w-0 items-center gap-1 truncate font-mono" title={repoRoot}>
            <span className="text-ink-600">⌥</span>
            <span className="truncate">{worktreeTail(repoRoot)}</span>
          </span>
        )}
        <span aria-hidden className="text-ink-600">·</span>
        <span className="shrink-0">{runs.length} {runs.length === 1 ? 'run' : 'runs'}</span>
        {usageRow?.tokens != null && (
          <>
            <span aria-hidden className="text-ink-600">·</span>
            <span className="shrink-0">{usageRow.tokens_h ?? usageRow.tokens} tok</span>
          </>
        )}
        {usageRow?.cost != null && (
          <>
            <span aria-hidden className="text-ink-600">·</span>
            <span className="shrink-0">{usageRow.cost_h ?? `$${usageRow.cost}`}</span>
          </>
        )}
        {chat.tasks && chat.tasks.total > 0 && (
          <>
            <span aria-hidden className="text-ink-600">·</span>
            <span className="shrink-0" title="tasks completed in this turn (TodoWrite)">
              ✓ {chat.tasks.done}/{chat.tasks.total} tasks
            </span>
          </>
        )}
        {chat.subagents > 0 && (
          <>
            <span aria-hidden className="text-ink-600">·</span>
            <span className="shrink-0" title="sub-agents spawned in this turn (Task)">
              ⛓ {chat.subagents} {chat.subagents === 1 ? 'sub-agent' : 'sub-agents'}
            </span>
          </>
        )}
      </div>

      {/* The composer — ONLY for interactive agents. A non-interactive AI worker and a
          deterministic agent run WITHOUT a chat box (their activity streams above); show
          a short note instead of an input. Lives IN this pane (no new page). */}
      {isInteractive ? (
        <>
          <AgentWorkStatus
            agentLabel={head?.display_name ?? agent}
            thinking={chat.sending && !liveTranscript?.running}
            working={Boolean(liveTranscript?.running || selectedRunRunning)}
            statusLabel={workStatusLabel}
            stopping={cancellingRunId === selectedRunId}
            onStop={canCancelSelectedRun ? cancelSelectedRun : undefined}
          />
          <ChatComposer
            agentLabel={head?.display_name ?? agent}
            sending={chat.sending}
            error={cancelError ?? chat.error}
            reply={chat.reply}
            onSend={chat.send}
            stop={chat.stop}
            ready={chatReady}
            imageAttachmentsEnabled={imageAttachmentsEnabled}
            // Prior turns of this conversation threaded into context (multi-turn chat,
            // feature-gap step 6). turns counts turns sent incl. the current one, so prior
            // history = turns - 1; clamp at 0 so the very first turn shows no indicator.
            threadTurns={Math.max(0, chat.turns - 1)}
          />
        </>
      ) : (
        <div className="border-t border-glass-line px-5 py-3 text-xs text-ink-500">
          {designation === 'deterministic'
            ? 'Deterministic agent — runs on a schedule or trigger with no LLM. No chat.'
            : 'Non-interactive AI worker — runs autonomously, managed by the lead. No direct chat; its activity streams above.'}
        </div>
      )}
    </GlassPanel>
  )
}
