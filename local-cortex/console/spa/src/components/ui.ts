/**
 * Non-component UI helpers shared across the design-system + features.
 * Kept out of the component files so react-refresh (fast refresh) stays happy —
 * a component module must export only components.
 */

/** Join class fragments, dropping falsy ones. */
export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

/**
 * Format a backend timestamp as a compact relative label ("3m", "2h", "5d").
 *
 * The dispatch board carries a raw `created_at` (unlike the run rows, which the
 * backend pre-formats as `*_ago`), so the view formats it client-side. Accepts an
 * ISO string / epoch seconds / epoch millis; returns '' for an unparseable / empty
 * value (the caller then omits the chip). A future time clamps to "now".
 */
export function formatRelative(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return ''
  let ms: number
  if (typeof value === 'number') {
    // Heuristic: < 1e12 looks like epoch SECONDS, else millis.
    ms = value < 1e12 ? value * 1000 : value
  } else {
    const n = Number(value)
    if (!Number.isNaN(n) && /^\d+(\.\d+)?$/.test(value.trim())) {
      ms = n < 1e12 ? n * 1000 : n
    } else {
      ms = Date.parse(value)
    }
  }
  if (Number.isNaN(ms)) return ''
  const diff = Date.now() - ms
  if (diff < 0) return 'now'
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d}d`
  const mo = Math.floor(d / 30)
  if (mo < 12) return `${mo}mo`
  return `${Math.floor(mo / 12)}y`
}

export type RunStatusKind = 'queued' | 'running' | 'completed' | 'errored' | 'idle'

/** Map a backend status_label (or raw status) to a StatusDot kind. */
export function statusKind(label: string | null | undefined): RunStatusKind {
  switch ((label ?? '').toLowerCase()) {
    case 'running':
      return 'running'
    case 'queued':
      return 'queued'
    case 'completed':
    case 'ok':
      return 'completed'
    case 'errored':
    case 'error':
      return 'errored'
    default:
      return 'idle'
  }
}
