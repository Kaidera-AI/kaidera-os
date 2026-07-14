import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useResource } from './useResource'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

/**
 * The stale-catalog race (cross-project roster mis-display):
 *
 * App.tsx drives `useResource(api.agents(project), [project])` for the agents
 * catalog. When the selected project changes, the OLD behaviour kept the
 * previous project's `data` on screen until the new fetch resolved — so the
 * AgentsColumn briefly rendered the PREVIOUS project's roster under the NEW
 * project (the `attio-curator under paul` mis-display). The fix: a deps change
 * must clear stale data (show loading), while a poll tick must NOT flash empty.
 */
describe('useResource — project-switch stale-data guard', () => {
  it('clears stale data immediately when deps (project) change', async () => {
    // First project resolves to roster A; second to roster B, but slowly so we can
    // observe the in-between window.
    type Roster = { project: string; agents: string[] }
    let resolveB: (v: Roster) => void = () => {}
    const fetcherA = vi.fn(async (): Promise<Roster> => ({ project: 'paul', agents: ['attio-curator'] }))
    const fetcherB = vi.fn(
      () => new Promise<Roster>((res) => { resolveB = res }),
    )

    const { result, rerender } = renderHook(
      ({ project }: { project: string }) =>
        useResource(project === 'paul' ? fetcherA : fetcherB, [project]),
      { initialProps: { project: 'paul' } },
    )

    // Roster A lands.
    await waitFor(() => expect(result.current.data).toEqual({ project: 'paul', agents: ['attio-curator'] }))

    // Switch project — the new fetch is in flight (resolveB not called yet).
    rerender({ project: 'kaidera-os' })

    // CRITICAL: the previous project's data must NOT linger. It must be cleared
    // (null) so the column shows loading, never paul's agents under kaidera-os.
    expect(result.current.data).toBeNull()
    expect(result.current.loading).toBe(true)

    // Now the new roster resolves.
    await act(async () => {
      resolveB({ project: 'kaidera-os', agents: ['kai', 'ren'] })
    })
    await waitFor(() =>
      expect(result.current.data).toEqual({ project: 'kaidera-os', agents: ['kai', 'ren'] }),
    )
  })

  it('does NOT clear data on a poll-driven refetch (no empty flash between polls)', async () => {
    vi.useFakeTimers()
    let calls = 0
    const fetcher = vi.fn(async () => {
      calls += 1
      return { project: 'kaidera-os', tick: calls }
    })

    const { result } = renderHook(() => useResource(fetcher, ['kaidera-os'], { pollMs: 1000 }))

    // Initial fetch resolves.
    await vi.waitFor(() => expect(result.current.data).toEqual({ project: 'kaidera-os', tick: 1 }))

    // Advance to the next poll tick. Data must remain populated across the
    // in-flight poll fetch (a poll is the SAME project — never clear to null).
    await act(async () => {
      vi.advanceTimersByTime(1000)
    })
    // Even mid-poll, data is not nulled (it holds the previous tick until the new
    // one lands).
    expect(result.current.data).not.toBeNull()

    await vi.waitFor(() => expect(result.current.data).toEqual({ project: 'kaidera-os', tick: 2 }))
  })

  it('keeps loading FALSE across poll ticks once data is present (the v0.1.85 silent-poll flicker fix)', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn(async () => ({ project: 'kaidera-os', ok: true }))
    const loadings: boolean[] = []
    const { result } = renderHook(() => {
      const r = useResource(fetcher, ['kaidera-os'], { pollMs: 1000 })
      loadings.push(r.loading)
      return r
    })

    await vi.waitFor(() => expect(result.current.data).toEqual({ project: 'kaidera-os', ok: true }))
    expect(result.current.loading).toBe(false)
    const afterFirstLoad = loadings.length

    // two background poll ticks
    await act(async () => { vi.advanceTimersByTime(1000) })
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2))
    await act(async () => { vi.advanceTimersByTime(1000) })
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3))

    // CONTRACT: after the first load, `loading` must NEVER flip back to true on a background poll.
    // That flip was the "page refreshes by itself" flicker AND the trigger for the modal
    // focus-steal (a parent re-entering loading re-rendered/re-focused the tree).
    expect(loadings.slice(afterFirstLoad).every((l) => l === false)).toBe(true)
  })
})
