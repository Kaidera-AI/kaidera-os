/**
 * UsersView — the admin "Users" surface (admins only). A simple table of every user
 * (email, role, status, last login) with row actions: toggle admin/user, block/unblock,
 * delete (with a confirm), plus an "Add user" dialog (email + role).
 *
 * Wired to the first-party auth endpoints via the injected client (api): GET /auth/users,
 * POST /auth/users, PATCH /auth/users/{id}, DELETE /auth/users/{id}. The backend enforces
 * the last-active-admin lockout guard; a 409 there surfaces here as a friendly message.
 * Consistent with the rest of the SPA (glass surfaces, mint accents, GlassModal, no router).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { GlassPanel, GlassModal } from '../components/glass'
import { cx, formatRelative } from '../components/ui'
import { ApiError } from '../api'
import type { AuthUser } from '../api'

export interface UsersClient {
  authUsers: (signal?: AbortSignal) => Promise<{ users: AuthUser[] }>
  createAuthUser: (
    body: { email: string; role: 'admin' | 'user'; display_name?: string },
    signal?: AbortSignal,
  ) => Promise<{ ok: boolean; user: AuthUser }>
  updateAuthUser: (
    userId: string,
    body: { role?: 'admin' | 'user'; status?: 'active' | 'disabled' },
    signal?: AbortSignal,
  ) => Promise<{ ok: boolean; user: AuthUser }>
  deleteAuthUser: (userId: string, signal?: AbortSignal) => Promise<{ ok: boolean; removed: boolean }>
}

/** Map a backend machine error code (carried as the ApiError message) to a sentence. */
function actionErrorMessage(err: unknown): string {
  const code = err instanceof ApiError ? err.message : String(err)
  if (code.includes('cannot_demote_last_admin')) return "Can't remove admin — at least one active admin is required."
  if (code.includes('cannot_block_last_admin')) return "Can't block the last active admin."
  if (code.includes('cannot_delete_last_admin')) return "Can't delete the last active admin."
  if (code.includes('email_already_in_use')) return 'That email is already in use.'
  if (code.includes('valid_email_required')) return 'Enter a valid email address.'
  if (code.includes('invalid_role')) return 'Pick a valid role.'
  return 'Action failed. Please try again.'
}

function RoleBadge({ role }: { role: string }) {
  const admin = role === 'admin'
  return (
    <span
      className={cx(
        'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium',
        admin ? 'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/30' : 'bg-base-800/70 text-ink-300',
      )}
    >
      {admin ? 'Admin' : 'User'}
    </span>
  )
}

// A non-active status (the schema stores 'disabled') is a blocked account; the UI labels it "Blocked".
function isBlocked(status: string): boolean {
  return status !== 'active'
}

function StatusBadge({ status }: { status: string }) {
  const blocked = isBlocked(status)
  return (
    <span
      className={cx(
        'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium',
        blocked ? 'bg-run-errored/15 text-run-errored ring-1 ring-run-errored/30' : 'bg-base-800/70 text-ink-300',
      )}
    >
      {blocked ? 'Blocked' : 'Active'}
    </span>
  )
}

function actionBtnCls(tone: 'default' | 'danger' = 'default') {
  return cx(
    'rounded-md px-2 py-1 text-xs font-medium transition-colors disabled:cursor-default disabled:opacity-40',
    tone === 'danger'
      ? 'text-run-errored/90 hover:bg-run-errored/10 hover:text-run-errored'
      : 'text-ink-300 hover:bg-base-800/70 hover:text-ink-100',
  )
}

export function UsersView({ client }: { client: UsersClient }) {
  const [users, setUsers] = useState<AuthUser[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<AuthUser | null>(null)
  const [showAdd, setShowAdd] = useState(false)

  // Keep the latest client in a ref so the fetch closure is STABLE (empty-deps useCallback) —
  // the same shape HistoryView/GraphView use, which keeps the set-state-in-effect analysis
  // happy (the effect calls an opaque stable callback).
  const clientRef = useRef(client)
  useEffect(() => {
    clientRef.current = client
  })

  // No synchronous setState here (loading starts true via initial state; a post-mutation reload
  // refreshes in place) — keeps the calling effect clear of the set-state-in-effect rule, matching
  // HistoryView's stable-callback shape.
  const load = useCallback((signal?: AbortSignal) => {
    return clientRef.current
      .authUsers(signal)
      .then((res) => {
        if (signal?.aborted) return
        setUsers(res.users || [])
        setError(null)
      })
      .catch((e: unknown) => {
        if (signal?.aborted) return
        setError(e instanceof ApiError && e.status === 403 ? 'Admins only.' : 'Could not load users.')
      })
      .finally(() => {
        if (!signal?.aborted) setLoading(false)
      })
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    load(ctrl.signal)
    return () => ctrl.abort()
  }, [load])

  const runAction = async (id: string, fn: () => Promise<unknown>) => {
    setBusyId(id)
    setError(null)
    try {
      await fn()
      await load()
    } catch (err) {
      setError(actionErrorMessage(err))
    } finally {
      setBusyId(null)
    }
  }

  const toggleRole = (u: AuthUser) =>
    runAction(u.id!, () => client.updateAuthUser(u.id!, { role: u.role === 'admin' ? 'user' : 'admin' }))

  const toggleStatus = (u: AuthUser) =>
    runAction(u.id!, () =>
      client.updateAuthUser(u.id!, { status: isBlocked(u.status) ? 'active' : 'disabled' }),
    )

  const doDelete = (u: AuthUser) => {
    setConfirmDelete(null)
    return runAction(u.id!, () => client.deleteAuthUser(u.id!))
  }

  return (
    <GlassPanel className="min-w-0 flex-1 p-6">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4">
        <header className="flex items-end justify-between gap-3">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-500">Admin</p>
            <h2 className="mt-1 text-lg font-semibold text-ink-100">Users</h2>
            <p className="mt-1 text-sm text-ink-400">Create accounts and manage access.</p>
          </div>
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            className="rounded-lg bg-mint-500/20 px-3 py-2 text-sm font-semibold text-mint-200 ring-1 ring-mint-400/40 transition-colors hover:bg-mint-500/30"
          >
            Add user
          </button>
        </header>

        {error && (
          <p className="rounded-lg border border-run-errored/30 bg-run-errored/10 px-3 py-2 text-sm text-run-errored">
            {error}
          </p>
        )}

        {loading ? (
          <p className="text-sm text-ink-500">Loading…</p>
        ) : users.length === 0 ? (
          <p className="text-sm text-ink-500">No users yet.</p>
        ) : (
          <div className="overflow-hidden rounded-xl border border-glass-line">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-glass-line bg-base-900/40 text-left text-[11px] uppercase tracking-wider text-ink-500">
                  <th className="px-3 py-2 font-medium">Email</th>
                  <th className="px-3 py-2 font-medium">Role</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Last login</th>
                  <th className="px-3 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const busy = busyId === u.id
                  return (
                    <tr key={u.id} className="border-b border-glass-line/60 last:border-0">
                      <td className="px-3 py-2.5">
                        <span className="block truncate text-ink-100">{u.email}</span>
                        {u.display_name && (
                          <span className="block truncate text-[11px] text-ink-500">{u.display_name}</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <RoleBadge role={u.role} />
                      </td>
                      <td className="px-3 py-2.5">
                        <StatusBadge status={u.status} />
                      </td>
                      <td className="px-3 py-2.5 text-ink-400">
                        {u.last_login_at ? formatRelative(u.last_login_at) : '—'}
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center justify-end gap-1">
                          <button
                            type="button"
                            disabled={busy || !u.id}
                            onClick={() => toggleRole(u)}
                            className={actionBtnCls()}
                          >
                            {u.role === 'admin' ? 'Make user' : 'Make admin'}
                          </button>
                          <button
                            type="button"
                            disabled={busy || !u.id}
                            onClick={() => toggleStatus(u)}
                            className={actionBtnCls()}
                          >
                            {isBlocked(u.status) ? 'Unblock' : 'Block'}
                          </button>
                          <button
                            type="button"
                            disabled={busy || !u.id}
                            onClick={() => setConfirmDelete(u)}
                            className={actionBtnCls('danger')}
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <AddUserModal
        key={showAdd ? 'add-open' : 'add-closed'}
        open={showAdd}
        onClose={() => setShowAdd(false)}
        onCreate={async (body) => {
          await client.createAuthUser(body)
          setShowAdd(false)
          await load()
        }}
      />

      <GlassModal
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title="Delete user"
      >
        <div className="flex flex-col gap-4 p-5">
          <p className="text-sm text-ink-300">
            Delete <span className="font-semibold text-ink-100">{confirmDelete?.email}</span>? This
            removes the account and signs them out. This cannot be undone.
          </p>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setConfirmDelete(null)}
              className="rounded-lg px-3 py-2 text-sm font-medium text-ink-300 transition-colors hover:bg-base-800/70 hover:text-ink-100"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => confirmDelete && doDelete(confirmDelete)}
              className="rounded-lg bg-run-errored/20 px-3 py-2 text-sm font-semibold text-run-errored ring-1 ring-run-errored/40 transition-colors hover:bg-run-errored/30"
            >
              Delete
            </button>
          </div>
        </div>
      </GlassModal>
    </GlassPanel>
  )
}

function AddUserModal({
  open,
  onClose,
  onCreate,
}: {
  open: boolean
  onClose: () => void
  onCreate: (body: { email: string; role: 'admin' | 'user' }) => Promise<void>
}) {
  // The dialog is remounted on each open (keyed by the parent), so plain initial state IS the
  // per-open reset — no reset-in-effect needed (which the set-state-in-effect rule flags).
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<'admin' | 'user'>('user')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    if (saving) return
    setSaving(true)
    setError(null)
    try {
      await onCreate({ email: email.trim(), role })
    } catch (err) {
      setError(actionErrorMessage(err))
      setSaving(false)
    }
  }

  const inputCls =
    'w-full rounded-lg border border-glass-line bg-base-900/60 px-3 py-2 text-sm text-ink-100 outline-none transition-colors placeholder:text-ink-600 focus:border-mint-400/50'

  return (
    <GlassModal open={open} onClose={onClose} title="Add user">
      <div className="flex flex-col gap-4 p-5">
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-300">Email</span>
          <input
            className={inputCls}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="person@example.com"
            type="email"
            autoComplete="off"
          />
        </label>
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-300">Role</span>
          <select
            className={inputCls}
            value={role}
            onChange={(e) => setRole(e.target.value as 'admin' | 'user')}
          >
            <option value="user">User</option>
            <option value="admin">Admin</option>
          </select>
        </label>
        {error && (
          <p className="rounded-lg border border-run-errored/30 bg-run-errored/10 px-3 py-2 text-sm text-run-errored">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-2 text-sm font-medium text-ink-300 transition-colors hover:bg-base-800/70 hover:text-ink-100"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={saving || !email.trim()}
            className={cx(
              'rounded-lg px-3 py-2 text-sm font-semibold transition-colors',
              saving || !email.trim()
                ? 'cursor-default bg-base-800/60 text-ink-500'
                : 'bg-mint-500/20 text-mint-200 ring-1 ring-mint-400/40 hover:bg-mint-500/30',
            )}
          >
            {saving ? 'Adding…' : 'Add user'}
          </button>
        </div>
      </div>
    </GlassModal>
  )
}
