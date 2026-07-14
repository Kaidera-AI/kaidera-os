/**
 * ThemeToggle — flips the app between dark (default) and light, top-right of the main header.
 *
 * Theming is a class on <html> (`dark` | `light`); `index.css` redefines the design tokens +
 * glass surfaces under `html.light`. The choice persists in localStorage and is applied
 * flash-free by a tiny inline script in index.html before the bundle loads; this component
 * re-applies it on mount (idempotent) and owns the toggle.
 */
import { useEffect, useState } from 'react'

const THEME_KEY = 'kaidera-os:theme'
type Theme = 'dark' | 'light'

function applyTheme(t: Theme) {
  const el = document.documentElement
  el.classList.toggle('light', t === 'light')
  el.classList.toggle('dark', t === 'dark')
  el.style.colorScheme = t
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => {
    if (typeof localStorage === 'undefined') return 'dark'
    return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
  })

  useEffect(() => {
    applyTheme(theme)
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* private mode / quota — the toggle still works for this session */
    }
  }, [theme])

  const next: Theme = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      title={`Switch to ${next} theme`}
      aria-label={`Switch to ${next} theme`}
      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-glass-line text-ink-300 transition-colors hover:bg-base-800/60 hover:text-ink-100"
    >
      <span aria-hidden="true" className="text-sm leading-none">{theme === 'dark' ? '☀' : '☾'}</span>
    </button>
  )
}
