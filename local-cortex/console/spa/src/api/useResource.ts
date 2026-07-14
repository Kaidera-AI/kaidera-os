/**
 * useResource — a tiny async-fetch hook for the REST catalogs (not the SSE).
 *
 * Wraps a fetcher (one of the `api.*` calls) with loading / error / data state,
 * an AbortController per run, and an optional poll interval (the catalogs are
 * snapshots — the run rail + metrics refresh on a light poll; the live transcript
 * itself is pushed by useRunStateStream, so this poll stays gentle). A null
 * `fetcher` (e.g. no project selected yet) parks in the idle state.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface Resource<T> {
  data: T | null
  error: Error | null
  loading: boolean
  /** Imperatively re-fetch (used by the poll + manual refresh). */
  refetch: () => void
}

export function useResource<T>(
  fetcher: ((signal: AbortSignal) => Promise<T>) | null,
  deps: unknown[],
  opts: { pollMs?: number } = {},
): Resource<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState<boolean>(!!fetcher)
  // Keep the latest fetcher in a ref so the poll closure never goes stale while
  // the dependency array (not the function identity) drives re-subscription.
  // Updated in an effect (not during render) so refs are only touched off-render.
  const fetcherRef = useRef(fetcher)
  useEffect(() => {
    fetcherRef.current = fetcher
  })

  const run = useCallback((signal: AbortSignal, silent = false) => {
    const f = fetcherRef.current
    if (!f) {
      setData(null)
      setError(null)
      setLoading(false)
      return
    }
    // Background refreshes (poll ticks, a manual refetch with data already on
    // screen) run SILENT: they must NOT toggle `loading`. Otherwise every poll
    // flips loading true→false, which flashes skeletons and — where a parent
    // gates on `loading` (the onboarding screen) — unmounts/remounts the subtree,
    // wiping a half-typed key. `loading` is reserved for first paint + a genuine
    // deps change; a background refresh swaps the data in place, invisibly.
    if (!silent) setLoading(true)
    f(signal)
      .then((d) => {
        if (!signal.aborted) {
          setData(d)
          setError(null)
        }
      })
      .catch((e: unknown) => {
        if (signal.aborted) return
        if (e instanceof DOMException && e.name === 'AbortError') return
        setError(e instanceof Error ? e : new Error(String(e)))
      })
      .finally(() => {
        if (!signal.aborted && !silent) setLoading(false)
      })
  }, [])

  const [nonce, setNonce] = useState(0)
  const refetch = useCallback(() => setNonce((n) => n + 1), [])

  // Drop stale data the instant the dependency identity changes (e.g. the
  // selected project switches) so a consumer never renders the PREVIOUS deps'
  // payload under the NEW deps while the refetch is in flight — that was the
  // cross-project roster mis-display (paul's agents flashing under the next
  // project). A poll tick only bumps `nonce` (deps unchanged), so it does NOT
  // clear here: polling keeps the last snapshot until the new one lands (no
  // empty flash between polls). Serialize for a value compare; the deps here are
  // simple scalars (project keys), so JSON is sufficient and cheap.
  const depsKey = JSON.stringify(deps)
  const prevDepsKeyRef = useRef<string | null>(null)
  useEffect(() => {
    if (prevDepsKeyRef.current !== null && prevDepsKeyRef.current !== depsKey) {
      // Deps genuinely changed since the last committed run — clear stale state.
      setData(null)
      setError(null)
      setLoading(!!fetcherRef.current)
    }
    prevDepsKeyRef.current = depsKey
  }, [depsKey])

  // A run is "background" (silent) when the deps did NOT change since the last
  // run — i.e. a poll tick or a manual refetch — AND we already have data to keep
  // showing. First load and every genuine deps change run loud (loading=true; the
  // deps-change effect above has already dropped the stale payload first).
  const lastRunDepsKeyRef = useRef<string | null>(null)
  useEffect(() => {
    const depsChanged = lastRunDepsKeyRef.current !== depsKey
    lastRunDepsKeyRef.current = depsKey
    const ctrl = new AbortController()
    run(ctrl.signal, !depsChanged && data !== null)

    let timer: ReturnType<typeof setInterval> | null = null
    if (opts.pollMs && fetcherRef.current) {
      timer = setInterval(() => {
        // Each poll tick gets its own controller via refetch's nonce bump.
        setNonce((n) => n + 1)
      }, opts.pollMs)
    }

    return () => {
      ctrl.abort()
      if (timer) clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, opts.pollMs, run])

  return { data, error, loading, refetch }
}
