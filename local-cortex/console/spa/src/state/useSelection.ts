/**
 * useSelection — the shell's selected (project, agent) state.
 *
 * Persists to the URL hash (`#/<project>/<agent>`) so a reload / deep-link
 * restores the view, and remembers the last agent PER PROJECT so switching back
 * to a project re-lands on the agent you were on. Selecting a new project clears
 * the agent (the shell then defaults it to that project's lead).
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface Selection {
  project: string | null
  agent: string | null
  selectProject: (project: string) => void
  selectAgent: (agent: string) => void
}

function parseHash(): { project: string | null; agent: string | null } {
  const raw = window.location.hash.replace(/^#\/?/, '')
  if (!raw) return { project: null, agent: null }
  const [p, a] = raw.split('/')
  return {
    project: p ? decodeURIComponent(p) : null,
    agent: a ? decodeURIComponent(a) : null,
  }
}

function writeHash(project: string | null, agent: string | null) {
  const parts = [project, agent].filter(Boolean).map((s) => encodeURIComponent(s as string))
  const next = parts.length ? `#/${parts.join('/')}` : '#/'
  if (window.location.hash !== next) {
    window.history.replaceState(null, '', next)
  }
}

export function useSelection(): Selection {
  // Read the hash ONCE at mount (lazy initializers — no render-time side effects).
  const [initial] = useState(parseHash)
  const [project, setProject] = useState<string | null>(initial.project)
  const [agent, setAgent] = useState<string | null>(initial.agent)
  // Last agent seen per project, so a project re-select restores it. Seeded once
  // from the initial hash via a lazy ref initializer (no mutation during render).
  const lastAgent = useRef<Record<string, string>>(
    initial.project && initial.agent ? { [initial.project]: initial.agent } : {},
  )

  const selectProject = useCallback((next: string) => {
    if (project === next) return
    // Update both axes in the same event. Nesting setAgent inside a setProject
    // updater can leave one render carrying the previous project's agent.
    setProject(next)
    setAgent(lastAgent.current[next] ?? null)
  }, [project])

  const selectAgent = useCallback((next: string) => {
    setAgent(next)
    setProject((p) => {
      if (p) lastAgent.current[p] = next
      return p
    })
  }, [])

  // Keep the hash in sync with the state.
  useEffect(() => {
    writeHash(project, agent)
  }, [project, agent])

  return { project, agent, selectProject, selectAgent }
}
