/**
 * ProfileMenu — the bottom-of-rail account control.
 *
 * Shows the signed-in user (name + email, from /whoami) and, on click, a small
 * upward popover: Profile (any user), Users (admins only), and Logout.
 *
 * Profile + Users are MAIN-AREA views (not separate pages) — the items call
 * `onNavigateView`, which the shell uses to switch the main area to that view. The Users
 * item is gated on `is_admin` (hidden for non-admins; the backend also 403s the data).
 */

import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useResource } from '../api/useResource'
import type { Whoami } from '../api/types'
import { cx } from './ui'

export function ProfileMenu({
  onNavigateView,
}: {
  onNavigateView?: (view: 'profile' | 'users') => void
}) {
  const userRes = useResource<Whoami>((signal) => api.whoami(signal), [])
  const user = userRes.data
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // Close on outside-click or Escape (only while open).
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const name = user?.name?.trim() || 'User'
  const email = user?.email?.trim() || ''
  const isAdmin = user?.is_admin ?? false
  const initial = (name[0] || 'U').toUpperCase()
  const logout = async () => {
    await fetch('/auth/logout', { method: 'POST' }).catch(() => undefined)
    window.location.href = '/auth/login'
  }

  const itemCls =
    'flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-base-800/70'

  const go = (view: 'profile' | 'users') => {
    setOpen(false)
    onNavigateView?.(view)
  }

  return (
    <div ref={ref} className="relative border-t border-glass-line px-2 py-2">
      {open && (
        <div
          role="menu"
          className="absolute bottom-full left-2 right-2 mb-1 overflow-hidden rounded-lg border border-glass-line bg-base-900/95 shadow-xl backdrop-blur-sm"
        >
          <button
            type="button"
            onClick={() => go('profile')}
            role="menuitem"
            className={cx(itemCls, 'text-ink-300 hover:text-ink-100')}
          >
            Profile
          </button>
          {isAdmin && (
            <button
              type="button"
              onClick={() => go('users')}
              role="menuitem"
              className={cx(itemCls, 'text-ink-300 hover:text-ink-100')}
            >
              Users
            </button>
          )}
          <button
            type="button"
            onClick={logout}
            role="menuitem"
            className={cx(itemCls, 'border-t border-glass-line text-run-errored/90 hover:text-run-errored')}
          >
            Logout
          </button>
        </div>
      )}

      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={email || name}
        className={cx(
          'flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors',
          open ? 'bg-base-800/70' : 'hover:bg-base-800/40',
        )}
      >
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-mint-400/20 text-xs font-semibold text-mint-200">
          {initial}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-ink-200">{name}</span>
          {email && <span className="block truncate text-[10px] text-ink-500">{email}</span>}
        </span>
        <svg
          viewBox="0 0 20 20"
          className={cx('h-4 w-4 shrink-0 text-ink-500 transition-transform', open && 'rotate-180')}
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          aria-hidden="true"
        >
          <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
    </div>
  )
}
