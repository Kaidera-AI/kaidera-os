/**
 * The glass design-system primitives — the small reusable set the UI/UX directive
 * calls for: GlassPanel, GlassCard, StatPill, StatusDot. All translucent (blur +
 * low-opacity fill + hairline edge + inner glow), all on the dark mint/teal base.
 *
 * They are deliberately thin: a `.glass`/`.glass-soft` utility (defined in
 * index.css) carries the surface treatment; these components add structure +
 * sane defaults so callers stay declarative and the look stays consistent.
 */

import { useCallback, useEffect, useId, useRef } from 'react'
import type { HTMLAttributes, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { cx, type RunStatusKind } from './ui'

// ---------------------------------------------------------------------------
//  GlassPanel — a full-height structural region (the columns).
// ---------------------------------------------------------------------------

interface GlassPanelProps extends HTMLAttributes<HTMLElement> {
  children: ReactNode
  /** Render as <aside> for the rail columns; defaults to <section>. */
  as?: 'section' | 'aside' | 'div'
}

export function GlassPanel({ children, className, as = 'section', ...rest }: GlassPanelProps) {
  const Tag = as
  return (
    <Tag className={cx('glass flex flex-col overflow-hidden rounded-2xl', className)} {...rest}>
      {children}
    </Tag>
  )
}

// ---------------------------------------------------------------------------
//  GlassCard — an inset interactive surface (an agent row, a metric tile).
// ---------------------------------------------------------------------------

interface GlassCardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode
  active?: boolean
  interactive?: boolean
}

export function GlassCard({
  children,
  className,
  active = false,
  interactive = false,
  ...rest
}: GlassCardProps) {
  return (
    <div
      className={cx(
        'glass-soft rounded-xl transition-all duration-150',
        interactive && 'cursor-pointer hover:border-mint-400/30 hover:bg-base-800/60',
        active && 'ring-mint border-mint-400/50 bg-base-800/70',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
//  GlassModal — a centered glass dialog over a blurred backdrop.
// ---------------------------------------------------------------------------
//
//  A small, reusable popup: a click-through backdrop + a centered glass panel +
//  a × close affordance. Dismisses on the × button, a backdrop click, or Esc;
//  a panel click never bubbles to the backdrop. Focus-trap-lite: on open it
//  moves focus into the panel (the close button) and restores it on close, and
//  Tab is kept inside the panel. role="dialog" + aria-modal label it; the title
//  is the accessible name. Rendered through a portal at document.body so it
//  floats above the column layout regardless of stacking context.

interface GlassModalProps {
  /** Whether the modal is shown. When false it renders nothing (no portal). */
  open: boolean
  /** Called on any dismiss intent: × button, backdrop click, or Escape. */
  onClose: () => void
  /** The dialog's accessible name, shown in the header. */
  title: ReactNode
  children: ReactNode
  /** Extra classes for the centered panel (e.g. a width clamp). */
  className?: string
}

export function GlassModal({ open, onClose, title, children, className }: GlassModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()

  // Esc-to-close + a lite focus-trap (keep Tab inside the panel). Bound only
  // while open; the cleanup detaches it so a closed modal is fully inert.
  const onKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
        return
      }
      if (e.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const focusable = panel.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (e.shiftKey && active === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    },
    [onClose],
  )

  // Move focus into the dialog when it OPENS, and restore it to the trigger on close.
  // Gated on `open` ALONE — NOT on onKeyDown/onClose identity. Parents pass an inline
  // `onClose={() => …}` that gets a fresh identity on every re-render (e.g. each background
  // poll), so coupling focus to it re-stole focus to the close button mid-typing — the "popup
  // flickers + jumps to the X, have to keep re-clicking the field" bug. Focus now moves exactly
  // once per open.
  useEffect(() => {
    if (!open) return
    const prevActive = document.activeElement as HTMLElement | null
    closeRef.current?.focus()
    return () => prevActive?.focus?.()
  }, [open])

  // Esc-to-close + Tab focus-trap. Re-subscribed when the handler identity changes, but it
  // NEVER moves focus on its own, so re-subscription is invisible (no focus thrash).
  useEffect(() => {
    if (!open) return
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onKeyDown])

  if (!open) return null

  return createPortal(
    <div
      data-testid="modal-backdrop"
      onMouseDown={(e) => {
        // Only a press that STARTS on the backdrop itself dismisses (so a drag
        // that ends on the backdrop after starting in the panel doesn't close).
        if (e.target === e.currentTarget) onClose()
      }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-base-950/70 p-4 backdrop-blur-sm"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(e) => e.stopPropagation()}
        className={cx(
          'glass relative flex max-h-[88vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl',
          className,
        )}
      >
        <header className="flex shrink-0 items-center gap-3 border-b border-glass-line px-5 py-3.5">
          <h2
            id={titleId}
            className="text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-300"
          >
            {title}
          </h2>
          <button
            ref={closeRef}
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="ml-auto flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-ink-400 transition-colors hover:bg-base-800/60 hover:text-ink-100"
          >
            <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth={1.6} aria-hidden="true">
              <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
            </svg>
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

// ---------------------------------------------------------------------------
//  StatPill — a compact label:value metric chip.
// ---------------------------------------------------------------------------

type PillTone = 'default' | 'mint' | 'muted'

interface StatPillProps {
  label: string
  value: ReactNode
  tone?: PillTone
  className?: string
  title?: string
}

const PILL_TONE: Record<PillTone, string> = {
  default: 'text-ink-100',
  mint: 'text-mint-300',
  muted: 'text-ink-400',
}

export function StatPill({ label, value, tone = 'default', className, title }: StatPillProps) {
  return (
    <div
      title={title}
      className={cx(
        'glass-soft flex min-w-0 flex-col items-start gap-0.5 rounded-lg px-2.5 py-1.5',
        className,
      )}
    >
      <span className={cx('text-base font-semibold leading-none tabular-nums', PILL_TONE[tone])}>
        {value}
      </span>
      <span className="text-[10px] font-medium uppercase tracking-wider text-ink-500">
        {label}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
//  StatusDot — the run-state indicator (queued/running/completed/errored).
// ---------------------------------------------------------------------------

interface StatusDotProps {
  status: RunStatusKind
  /** Add a soft pulse for the live 'running' state. */
  pulse?: boolean
  className?: string
  title?: string
}

const DOT_COLOR: Record<RunStatusKind, string> = {
  running: 'bg-run-running',
  queued: 'bg-run-queued',
  completed: 'bg-run-completed',
  errored: 'bg-run-errored',
  idle: 'bg-ink-500',
}

export function StatusDot({ status, pulse = false, className, title }: StatusDotProps) {
  const live = pulse && status === 'running'
  return (
    <span className={cx('relative inline-flex h-2.5 w-2.5', className)} title={title ?? status}>
      {live && (
        <span
          className={cx(
            'absolute inline-flex h-full w-full animate-ping rounded-full opacity-60',
            DOT_COLOR[status],
          )}
        />
      )}
      <span
        className={cx(
          'relative inline-flex h-2.5 w-2.5 rounded-full',
          DOT_COLOR[status],
          status === 'running' && 'shadow-[0_0_8px_rgba(67,224,182,0.7)]',
        )}
      />
    </span>
  )
}
