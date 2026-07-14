import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { PlanView, type PlanClient } from './PlanView'

describe('PlanView', () => {
  it('creates a lead handoff when an empty project needs its first plan', async () => {
    const user = userEvent.setup()
    const client: PlanClient = {
      getPlanList: vi.fn().mockResolvedValue([]),
      getPlanFile: vi.fn(),
      bootstrapPlan: vi.fn().mockResolvedValue({
        ok: true,
        lead: 'marlow',
        path: 'docs/plans/marketing-plan/plan.mdx',
        handoff: { id: 'handoff-plan-1' },
        error: null,
      }),
    }

    render(<PlanView project="marketing" client={client} />)

    await waitFor(() => expect(screen.getByTestId('plan-bootstrap')).toBeInTheDocument())
    await user.type(screen.getByLabelText(/Plan title/i), 'Marketing plan')
    await user.click(screen.getByRole('button', { name: /Ask lead to create plan/i }))

    await waitFor(() =>
      expect(client.bootstrapPlan).toHaveBeenCalledWith('marketing', {
        title: 'Marketing plan',
        objective: undefined,
      }),
    )
    expect(await screen.findByText(/Created handoff for marlow/i)).toHaveTextContent(
      'docs/plans/marketing-plan/plan.mdx',
    )
    expect(client.getPlanFile).not.toHaveBeenCalled()
  })
})
