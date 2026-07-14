import type { RunRow } from './types'

const ACTIVE_CHAT_RUN_PREFIX = 'kaidera-os:active-chat-run'
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const NON_TERMINAL_STATUSES = new Set(['queued', 'running', 'starting', 'pending'])

function storageKey(project: string, agent: string, sessionId: string): string {
  return `${ACTIVE_CHAT_RUN_PREFIX}:${project}:${agent}:${sessionId}`
}

export function isValidRunId(runId: string | null | undefined): runId is string {
  return typeof runId === 'string' && UUID_RE.test(runId.trim())
}

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

export function readActiveChatRun(
  project: string,
  agent: string,
  sessionId: string,
): string | null {
  const s = storage()
  if (!s) return null
  const key = storageKey(project, agent, sessionId)
  const raw = s.getItem(key)
  if (!raw) return null
  const runId = raw.trim()
  if (isValidRunId(runId)) return runId
  s.removeItem(key)
  return null
}

export function writeActiveChatRun(
  project: string,
  agent: string,
  sessionId: string,
  runId: string,
): void {
  if (!isValidRunId(runId)) return
  const s = storage()
  if (!s) return
  try {
    s.setItem(storageKey(project, agent, sessionId), runId)
  } catch {
    /* ignore quota / disabled storage */
  }
}

export function clearActiveChatRun(
  project: string,
  agent: string,
  sessionId: string,
  runId?: string | null,
): void {
  const s = storage()
  if (!s) return
  const key = storageKey(project, agent, sessionId)
  if (runId) {
    const current = s.getItem(key)
    if (current && current !== runId) return
  }
  s.removeItem(key)
}

export function isTerminalRun(
  run: Pick<RunRow, 'running' | 'status' | 'status_label'>,
): boolean {
  if (run.running) return false
  const status = (run.status ?? '').toLowerCase()
  const label = (run.status_label ?? '').toLowerCase()
  return !NON_TERMINAL_STATUSES.has(status) && !NON_TERMINAL_STATUSES.has(label)
}
