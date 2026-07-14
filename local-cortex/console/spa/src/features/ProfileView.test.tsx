import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProfileView } from './ProfileView'
import type { ProfileClient } from './ProfileView'
import { ApiError } from '../api'
import type { AuthUser } from '../api'

function me(over: Partial<AuthUser> = {}): AuthUser {
  return {
    id: 'user_1',
    name: 'Me',
    display_name: 'Me',
    email: 'me@example.com',
    is_admin: false,
    role: 'user',
    status: 'active',
    last_login_at: null,
    ...over,
  }
}

function fakeClient(over: Partial<ProfileClient> = {}): ProfileClient {
  return {
    authProfile: vi.fn().mockResolvedValue(me()),
    updateProfile: vi.fn().mockResolvedValue({ ok: true, user: me({ display_name: 'New Name' }) }),
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProfileView', () => {
  it('loads the current profile into the form', async () => {
    render(<ProfileView client={fakeClient()} />)
    expect(await screen.findByDisplayValue('me@example.com')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Me')).toBeInTheDocument()
  })

  it('Save is disabled until a field changes, then PATCHes the profile', async () => {
    const client = fakeClient()
    render(<ProfileView client={client} />)
    await screen.findByDisplayValue('me@example.com')
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    const nameField = screen.getByDisplayValue('Me')
    await userEvent.clear(nameField)
    await userEvent.type(nameField, 'New Name')
    expect(save).toBeEnabled()
    await userEvent.click(save)
    await waitFor(() =>
      expect(client.updateProfile).toHaveBeenCalledWith({
        email: 'me@example.com',
        display_name: 'New Name',
      }),
    )
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
  })

  it('shows a friendly message when the email is already in use (409)', async () => {
    const client = fakeClient({
      updateProfile: vi
        .fn()
        .mockRejectedValue(new ApiError(409, '/auth/profile', 'email_already_in_use')),
    })
    render(<ProfileView client={client} />)
    const emailField = await screen.findByDisplayValue('me@example.com')
    await userEvent.clear(emailField)
    await userEvent.type(emailField, 'taken@example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/already in use/i)).toBeInTheDocument()
  })
})
