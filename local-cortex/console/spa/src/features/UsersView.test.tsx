import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { UsersView } from './UsersView'
import type { UsersClient } from './UsersView'
import { ApiError } from '../api'
import type { AuthUser } from '../api'

function user(over: Partial<AuthUser> = {}): AuthUser {
  return {
    id: 'user_1',
    name: 'Alice',
    display_name: 'Alice',
    email: 'alice@example.com',
    is_admin: true,
    role: 'admin',
    status: 'active',
    last_login_at: new Date(Date.now() - 60_000).toISOString(),
    ...over,
  }
}

function fakeClient(over: Partial<UsersClient> = {}): UsersClient {
  return {
    authUsers: vi.fn().mockResolvedValue({ users: [user()] }),
    createAuthUser: vi
      .fn()
      .mockResolvedValue({ ok: true, user: user({ id: 'user_2', email: 'new@example.com', role: 'user' }) }),
    updateAuthUser: vi.fn().mockResolvedValue({ ok: true, user: user() }),
    deleteAuthUser: vi.fn().mockResolvedValue({ ok: true, removed: true }),
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('UsersView — table', () => {
  it('renders a user row: email, role, status, last login', async () => {
    render(<UsersView client={fakeClient()} />)
    const emailCell = await screen.findByText('alice@example.com')
    const row = emailCell.closest('tr') as HTMLElement
    // The role + status badges live in the row (the "Make admin" action button text is excluded
    // by scoping to badge-only matches via the row's cells).
    expect(within(row).getByText('Admin')).toBeInTheDocument()
    expect(within(row).getByText('Active')).toBeInTheDocument()
    // last-login relative (e.g. "1m")
    expect(within(row).getByText(/\dm|\ds|now/)).toBeInTheDocument()
  })

  it('renders a disabled user as "Blocked" with an "Unblock" action', async () => {
    const client = fakeClient({
      authUsers: vi
        .fn()
        .mockResolvedValue({ users: [user({ status: 'disabled', role: 'user' })] }),
    })
    render(<UsersView client={client} />)
    const emailCell = await screen.findByText('alice@example.com')
    const row = emailCell.closest('tr') as HTMLElement
    expect(within(row).getByText('Blocked')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Unblock' })).toBeInTheDocument()
    await userEvent.click(within(row).getByRole('button', { name: 'Unblock' }))
    await waitFor(() =>
      expect(client.updateAuthUser).toHaveBeenCalledWith('user_1', { status: 'active' }),
    )
  })

  it('shows "Admins only." when the list 403s', async () => {
    const client = fakeClient({
      authUsers: vi.fn().mockRejectedValue(new ApiError(403, '/auth/users', 'admin_required')),
    })
    render(<UsersView client={client} />)
    expect(await screen.findByText('Admins only.')).toBeInTheDocument()
  })
})

describe('UsersView — actions', () => {
  it('toggles role via updateAuthUser', async () => {
    const client = fakeClient()
    render(<UsersView client={client} />)
    await screen.findByText('alice@example.com')
    // Alice is admin → the toggle reads "Make user"
    await userEvent.click(screen.getByRole('button', { name: 'Make user' }))
    await waitFor(() =>
      expect(client.updateAuthUser).toHaveBeenCalledWith('user_1', { role: 'user' }),
    )
  })

  it('blocks a user via updateAuthUser', async () => {
    const client = fakeClient()
    render(<UsersView client={client} />)
    await screen.findByText('alice@example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Block' }))
    await waitFor(() =>
      expect(client.updateAuthUser).toHaveBeenCalledWith('user_1', { status: 'disabled' }),
    )
  })

  it('confirms before deleting, then calls deleteAuthUser', async () => {
    const client = fakeClient()
    render(<UsersView client={client} />)
    await screen.findByText('alice@example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    // A confirm dialog appears; the row action did NOT fire yet.
    expect(client.deleteAuthUser).not.toHaveBeenCalled()
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(client.deleteAuthUser).toHaveBeenCalledWith('user_1'))
  })

  it('surfaces the last-admin guard message on a 409', async () => {
    const client = fakeClient({
      updateAuthUser: vi
        .fn()
        .mockRejectedValue(new ApiError(409, '/auth/users/user_1', 'cannot_demote_last_admin')),
    })
    render(<UsersView client={client} />)
    await screen.findByText('alice@example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Make user' }))
    expect(await screen.findByText(/at least one active admin is required/i)).toBeInTheDocument()
  })

  it('creates a user through the Add dialog', async () => {
    const client = fakeClient()
    render(<UsersView client={client} />)
    await screen.findByText('alice@example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Add user' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.type(within(dialog).getByPlaceholderText('person@example.com'), 'new@example.com')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Add user' }))
    await waitFor(() =>
      expect(client.createAuthUser).toHaveBeenCalledWith({ email: 'new@example.com', role: 'user' }),
    )
  })
})
