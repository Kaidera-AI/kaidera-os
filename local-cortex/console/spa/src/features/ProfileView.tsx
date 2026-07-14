/**
 * ProfileView — any signed-in user edits their OWN account: email + display name.
 *
 * Reached from the ProfileMenu's "Profile" item (a MainView tab in the shell). Pure +
 * self-contained: it fetches the current user once (GET /auth/profile), holds a small
 * edit form, and PATCHes /auth/profile on Save. A duplicate / invalid email surfaces as
 * a friendly inline message (the backend's machine code is mapped here). Consistent with
 * the rest of the SPA: glass panel, mint accent, no router.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { GlassPanel } from '../components/glass'
import { cx } from '../components/ui'
import { ApiError } from '../api'
import type { AuthUser } from '../api'

export interface ProfileClient {
  authProfile: (signal?: AbortSignal) => Promise<AuthUser>
  updateProfile: (
    body: { email?: string; display_name?: string },
    signal?: AbortSignal,
  ) => Promise<{ ok: boolean; user: AuthUser }>
}

/** Map the backend's machine error code (carried as the ApiError message) to a sentence. */
function profileErrorMessage(err: unknown): string {
  const code = err instanceof ApiError ? err.message : String(err)
  if (code.includes('email_already_in_use')) return 'That email is already in use by another account.'
  if (code.includes('valid_email_required')) return 'Enter a valid email address.'
  if (code.includes('email_or_display_name_required')) return 'Nothing to save — change a field first.'
  return 'Could not save your profile. Please try again.'
}

export function ProfileView({ client }: { client: ProfileClient }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  // Keep the latest client in a ref so the load closure is STABLE (empty-deps useCallback) —
  // the opaque-callback shape HistoryView uses to satisfy the set-state-in-effect rule.
  const clientRef = useRef(client)
  useEffect(() => {
    clientRef.current = client
  })

  // No synchronous setState here (loading starts true via initial state) — keeps the effect that
  // calls this clear of the set-state-in-effect rule, matching HistoryView's stable-callback shape.
  const load = useCallback((signal: AbortSignal) => {
    clientRef.current
      .authProfile(signal)
      .then((u) => {
        if (signal.aborted) return
        setUser(u)
        setEmail(u.email || '')
        setDisplayName(u.display_name || (u.name && u.name !== u.email ? u.name : ''))
      })
      .catch(() => {
        // Auth-off (local dev) has no signed-in user → the profile endpoint 401s. In the
        // redist (auth enabled) this resolves the real user. Keep the message non-alarming.
        if (!signal.aborted)
          setError(
            'Profile is available when signed in. This deployment may have authentication disabled (local dev).',
          )
      })
      .finally(() => {
        if (!signal.aborted) setLoading(false)
      })
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    load(ctrl.signal)
    return () => ctrl.abort()
  }, [load])

  const dirty =
    !!user && (email.trim() !== (user.email || '') || displayName.trim() !== (user.display_name || ''))

  const onSave = async () => {
    if (!user || saving) return
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const res = await client.updateProfile({
        email: email.trim(),
        display_name: displayName.trim(),
      })
      setUser(res.user)
      setEmail(res.user.email || '')
      setDisplayName(res.user.display_name || '')
      setSaved(true)
    } catch (err) {
      setError(profileErrorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const inputCls =
    'w-full rounded-lg border border-glass-line bg-base-900/60 px-3 py-2 text-sm text-ink-100 outline-none transition-colors placeholder:text-ink-600 focus:border-mint-400/50'

  return (
    <GlassPanel className="min-w-0 flex-1 p-6">
      <div className="mx-auto w-full max-w-md">
        <header className="mb-5">
          <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-500">Account</p>
          <h2 className="mt-1 text-lg font-semibold text-ink-100">Your profile</h2>
          <p className="mt-1 text-sm text-ink-400">Update your email and display name.</p>
        </header>

        {loading ? (
          <p className="text-sm text-ink-500">Loading…</p>
        ) : (
          <div className="flex flex-col gap-4">
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-ink-300">Display name</span>
              <input
                className={inputCls}
                value={displayName}
                onChange={(e) => {
                  setDisplayName(e.target.value)
                  setSaved(false)
                }}
                placeholder="Your name"
                autoComplete="name"
              />
            </label>

            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-ink-300">Email</span>
              <input
                className={inputCls}
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value)
                  setSaved(false)
                }}
                placeholder="you@example.com"
                type="email"
                autoComplete="email"
              />
            </label>

            {user && (
              <p className="text-xs text-ink-500">
                Role: <span className="font-medium text-ink-300">{user.role}</span>
              </p>
            )}

            {error && (
              <p className="rounded-lg border border-run-errored/30 bg-run-errored/10 px-3 py-2 text-sm text-run-errored">
                {error}
              </p>
            )}
            {saved && !error && (
              <p className="rounded-lg border border-mint-400/30 bg-mint-500/10 px-3 py-2 text-sm text-mint-200">
                Saved.
              </p>
            )}

            <div>
              <button
                type="button"
                onClick={onSave}
                disabled={!dirty || saving}
                className={cx(
                  'rounded-lg px-4 py-2 text-sm font-semibold transition-colors',
                  !dirty || saving
                    ? 'cursor-default bg-base-800/60 text-ink-500'
                    : 'bg-mint-500/20 text-mint-200 ring-1 ring-mint-400/40 hover:bg-mint-500/30',
                )}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        )}
      </div>
    </GlassPanel>
  )
}
