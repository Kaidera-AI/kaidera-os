# Kaidera OS Harness Console — Inc00

A **runnable, read-only** FastAPI + HTMX dashboard over the **live local Cortex
API** (`http://localhost:8501`). It shows the real agent fleet: active projects,
their rosters, and their handoffs, plus a live health pill.

This is the Inc00 read-only slice — **not** a port of the full
`design/console-v2.html` prototype. The prototype locked the *look*; this builds
the real thing for one read-only surface.

## What it shows

- **Project switcher** — active projects (display name · agent count · repo root).
- **Detail panel** — the selected project's **roster** (name · role · model ·
  writer_scope) and **handoffs** (priority · status · summary · from · created_at),
  pulled live with the `X-Project` header.
- **Health pill** — `/health` status + `surface_version`, top-right.
- **Live refresh** — an explicit **Refresh** button on the panel, plus a
  `hx-trigger="every 10s"` poll as the live-feed fallback. Both the panel poll
  and the health-pill poll carry a `TODO(SSE)` marker for the future
  `/events` EventSource swap.

It is strictly **read-only**: no route writes to Cortex.

## Requirements

- Python 3.11+ (developed/verified on 3.14)
- The local Cortex API running at `http://localhost:8501` (read-only). The page
  still renders if Cortex is down — the health pill goes red and panels show
  empty states.

## Run (dev)

From this directory (`local-cortex/console/`):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Use port 8765 (the Cortex API owns 8501)
uvicorn app.main:app --port 8765 --reload
```

Then open: **http://127.0.0.1:8765/**

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CORTEX_BASE_URL` | `http://localhost:8501` | Local Cortex API base URL |
| `CORTEX_CONSOLE_AGENT` | `ren` | `X-Agent-Name` sent on scoped (RLS) reads |

## Routes

| Route | Returns |
|---|---|
| `GET /` | Full dashboard (header, health pill, project list, default panel) |
| `GET /projects/{key}` | HTMX partial — one project's roster + handoffs |
| `GET /health-pill` | HTMX partial — the health pill (independent live refresh) |
| `GET /static/*` | Vendored assets (HTMX, CSS, Kaidera AI white logo) |

## Layout

```
console/
├── bootstrap.py          # pywebview launcher (for later; dev uses uvicorn directly)
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app + routes (shell-agnostic; no pywebview here)
│   ├── cortex_client.py  # async httpx client over :8501 (configurable via CORTEX_BASE_URL)
│   ├── templates/        # base / dashboard / _project_panel / _health_pill
│   └── static/           # kaidera-logo-official-white.svg, app.css, htmx.min.js (vendored)
├── requirements.txt
└── README.md
```

`app/main.py` is shell-agnostic (no pywebview imports) so the same ASGI app runs
identically under `uvicorn` and inside the packaged pywebview window later
(`bootstrap.py`). HTMX is vendored locally — no CDN.

## Not in Inc00 (deferred)

- **SSE** `/events` stream (the `every 10s` polls are the placeholder; see
  `TODO(SSE)` markers in `base.html` and `_project_panel.html`). `sse-starlette`
  is already in `requirements.txt`.
- **Packaging** (pywebview window / PyInstaller). `bootstrap.py` exists but is
  not exercised yet.
- Any **writes/mutations** — Inc00 is read-only by design.
