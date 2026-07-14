import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useSelection } from './useSelection'

describe('useSelection', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '#/doha-dt/oryx')
  })

  it('clears the previous project agent when switching to an unseen project', async () => {
    const { result } = renderHook(() => useSelection())

    act(() => result.current.selectProject('kaidera-os'))

    expect(result.current.project).toBe('kaidera-os')
    expect(result.current.agent).toBeNull()
    await waitFor(() => expect(window.location.hash).toBe('#/kaidera-os'))
  })

  it('restores only the agent remembered for the selected project', async () => {
    const { result } = renderHook(() => useSelection())

    act(() => result.current.selectProject('kaidera-os'))
    act(() => result.current.selectAgent('ren'))
    act(() => result.current.selectProject('doha-dt'))

    expect(result.current.agent).toBe('oryx')
    act(() => result.current.selectProject('kaidera-os'))
    expect(result.current.agent).toBe('ren')
    await waitFor(() => expect(window.location.hash).toBe('#/kaidera-os/ren'))
  })
})
