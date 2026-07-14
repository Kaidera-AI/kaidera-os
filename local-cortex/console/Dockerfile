# ─────────────────────────────────────────────────────────────────────────────
# Kaidera OS Harness Console - HEADLESS container image (Milestone 1 T13).
#
# WHY THIS EXISTS: Milestone 1 moved live run-state out of process memory into the
# durable app-DB (run_state / run_span). With state no longer in the process, the
# console backend can be RESTARTED mid-run without losing what every agent is
# doing - so it becomes a proper headless Docker service in the `kaidera-os-cortex`
# stack, not a Mac app. This is the containerization that whole move enables.
#
# SAME ASGI APP, NO CODE BRANCHES: `app/main.py` is shell-agnostic (no pywebview
# import at module load; the harness CLIs are invoked via subprocess, never
# imported). The exact same `app.main:app` runs here under plain uvicorn that runs
# under the packaged pywebview window (../bootstrap.py) and the dev `uvicorn` run.
# This image just picks the uvicorn shape.
#
# HEADLESS = NO DESKTOP DEPS: we install requirements.txt ONLY. pywebview /
# pyinstaller (requirements-build.txt) are the native-window packaging path and
# are NOT needed — and would not work — in a slim headless container. uvicorn
# (with the [standard] extras already pinned in requirements.txt) serves HTTP+SSE.
#
# ⚠️ WORKER-SPAWN BOUNDARY (the honest M1 limit): the LLM harness CLIs
# (`claude-code` / `pi`) and their login live on the HOST Mac, not in this slim
# image. So this container serves the backend's HTTP / SSE / store / orchestrator
# / watchdog surface, but autonomous agent *spawning* is NOT container-resident
# yet — it stays host-side until the platform-later remote harness-service. The
# state move does NOT require the spawn to move: the detached worker writes the
# app-DB directly whether it runs on host or in-container, so a restart of THIS
# container never loses run-state. Do not try to bake the harness CLIs in here.
#
# SPA (Track C): the refined React/Vite console is built in a SEPARATE node stage
# (below) and only its static `dist/` bundle is COPYed into the slim python runtime
# — so node + node_modules never reach the final image. The runtime serves that
# bundle at /app (StaticFiles mount in app/main.py); the legacy HTML console at /
# is unchanged.
#
# Local machine only. NOT the Kaidera AI platform deployment, NOT a customer deploy.
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: build the SPA (Track C) ─────────────────────────────────────────
# node-only build stage: install the locked deps + run the production Vite build
# (`npm run build` = tsc -b + vite build, base '/app/'), producing spa/dist. This
# stage is DISCARDED — only its dist/ output is copied forward, so the runtime image
# carries no node, no node_modules, no SPA source. Pinned to an LTS node on the same
# Debian (bookworm) base family as python:3.12-slim for a consistent toolchain.
FROM node:22-bookworm-slim AS spa-build
WORKDIR /spa
# Deps first (layer-cached until the lockfile changes). NOTE: a macOS-generated
# package-lock.json omits linux-only optional deps (e.g. @emnapi/runtime, pulled
# in transitively by native build tooling), and `npm ci` demands a COMPLETE lock,
# so it fails in this linux stage. `npm install` resolves the platform deps at
# build time instead (a cross-platform lock regeneration is a reproducibility
# follow-up).
COPY spa/package.json spa/package-lock.json ./
RUN npm install --no-audit --no-fund
# The SPA source, then the production build → /spa/dist. (spa/node_modules +
# spa/dist are excluded from the build context via .dockerignore, so the COPY
# brings only source and the build runs clean.)
COPY spa/ ./
RUN npm run build

# ── Stage 2: the headless python runtime ─────────────────────────────────────
FROM python:3.12-slim

# Fail fast, no .pyc clutter, unbuffered logs (so `docker logs` is live).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first (layer-cached until requirements.txt changes). Headless set
# only — uvicorn[standard], fastapi, jinja2, httpx, sse-starlette, asyncpg,
# psycopg2-binary, lzstring. We install requirements.txt but STRIP the desktop-only
# `pywebview` line: it is the native-window shell's dep (bootstrap.py, excluded
# here) and nothing under app/ imports it (main.py is shell-agnostic), so it is
# dead weight headless. pyinstaller lives in requirements-build.txt, which is not
# in the build context (.dockerignore) — so it is already absent.
COPY requirements.txt ./
RUN grep -ivE '^[[:space:]]*pywebview\b' requirements.txt > requirements.headless.txt \
    && pip install -r requirements.headless.txt

# The ASGI app. Only the package is needed at runtime; the desktop entry point
# (bootstrap.py / console.spec) and build artifacts are excluded via .dockerignore.
COPY app ./app

# The built SPA bundle (Track C) from the node stage → /app/spa/dist, the exact
# path app/main.py serves at /app (SPA_DIST_DIR = <pkg-parent>/spa/dist; with the
# app package at /app/app, its parent is /app, so the bundle is /app/spa/dist). The
# mount is guarded — a missing bundle is logged + skipped — but this COPY ensures it
# is present, so the deployed container serves the SPA at /app. Only the static
# output is copied; no node/node_modules enter the runtime image.
COPY --from=spa-build /spa/dist ./spa/dist

# The console's HTTP/SSE port (matches the dev port + the compose publish).
EXPOSE 8765

# Single uvicorn worker — the M1 win is RESTARTABILITY, not horizontal scale
# (single-user local console). No code branch: the same app object as every other
# shape. host 0.0.0.0 so the published 127.0.0.1:8765 mapping reaches it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
