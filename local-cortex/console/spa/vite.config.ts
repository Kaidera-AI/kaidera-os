/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The console backend (FastAPI) — the SPA is a pure thin client of its catalogs.
const BACKEND = 'http://127.0.0.1:8765'
const __dirname = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(__dirname, '../../..')

// Every backend surface the SPA consumes is proxied in dev so the SPA can run on
// its own port (5173) while same-origin fetch()/EventSource hit the live backend.
// SSE (`/runstate/stream`) needs buffering disabled — that is the default for the
// http-proxy Vite uses; we only register the path prefixes.
const proxied = [
  '/projects',
  '/agents',
  '/runs',
  '/dispatch',
  '/analytics',
  '/settings',
  '/runstate',
  '/explain',
  '/graph',
  '/history',
  '/cortex',
]

// https://vite.dev/config/
//
// `base` is set PER COMMAND so the same source serves correctly in both places:
//   - build → '/app/'   The console serves the production bundle at /app
//     (StaticFiles mount in app/main.py), so asset/import URLs must resolve under
//     /app — Vite rewrites them to /app/assets/* and /app/favicon.svg. (Only the
//     STATIC asset URLs; runtime fetch()/EventSource calls stay ROOT-relative —
//     /agents · /runs · /dispatch · /analytics · /settings · /projects ·
//     /runstate/stream — because the module APIs live at the SAME ORIGIN's root,
//     so no CORS/proxy is needed when served from :8765. `base` never touches
//     those string literals.)
//   - serve → '/'       Local dev is UNCHANGED: the dev server stays at
//     http://localhost:5173/ and the proxy below forwards the API/SSE prefixes to
//     the backend, so same-origin fetch()/EventSource work exactly as before.
export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/app/' : '/',
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    fs: {
      allow: [REPO_ROOT],
    },
    proxy: Object.fromEntries(
      proxied.map((p) => [
        p,
        { target: BACKEND, changeOrigin: true },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    css: false,
  },
}))
