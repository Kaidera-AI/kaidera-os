#!/usr/bin/env bash
# CLEAN-ROOM install PROOF — boots the native console from ONLY the COMMITTED tree (git archive
# HEAD = exactly what a signed release archive delivers) inside a FRESH
# container that has nothing but a base Python image. It proves the redistributable actually RUNS
# on a blank machine — the runtime backstop to the file-level `check-redist-complete` fitness gate.
#
# It does NOT need Docker-in-Docker or the Cortex stack: the console degrades gracefully with the
# app-DB / Cortex absent (settings → {}, data degraded), so /console/version + the static SPA still
# serve. The proof sets KAIDERA_AUTH_ENABLED=0 explicitly because it verifies the local/private
# package boot path; hosted/shared auth-on login flows are covered by the auth test suites.
#
# Heavier than the fitness gates (pulls an image, builds a venv) — run it before a release, e.g.
#   bash scripts/test-clean-install.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
IMAGE="${CLEANROOM_IMAGE:-python:3.12-slim}"
PORT=8799
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '  \033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

printf '\n\033[1;36m== clean-room: boot the console from the COMMITTED tree in a fresh %s ==\033[0m\n' "$IMAGE"
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  die "docker not available — this proof needs Docker to spin the clean container."
fi

# 1. The exact fresh-clone tree (tracked files only — a dropped/gitignored artifact shows up absent).
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
git archive HEAD | tar -x -C "$TMP"
D="$TMP/local-cortex/console"
[ -f "$D/spa/dist/index.html" ] || die "the COMMIT has no spa/dist/index.html — a fresh install would have a blank /app."
ls "$D"/spa/dist/assets/*.js >/dev/null 2>&1 || die "the COMMIT has no spa/dist/assets/*.js — THE bug (npm-less server can't rebuild)."
ok "committed tree carries the full SPA bundle"

# 2. In a bare container: build the venv from requirements.txt, run install.sh's SPA check, boot
#    uvicorn, and prove BOTH the API (/console/version) AND a referenced SPA asset are served 200.
docker run --rm -v "$D:/app:ro" -w /app "$IMAGE" bash -c '
  set -e
  # the slim base has no curl — install it for the HTTP probes (apt writes to the container fs,
  # not the read-only mount). This is test scaffolding, NOT an install.sh dependency.
  apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq curl >/dev/null 2>&1 || true
  command -v curl >/dev/null 2>&1 || { echo "could not install curl in the test container"; exit 1; }
  python -m venv /tmp/v && /tmp/v/bin/pip -q install --upgrade pip >/dev/null
  /tmp/v/bin/pip -q install -r requirements.txt >/dev/null
  [ -f spa/dist/index.html ] && ls spa/dist/assets/*.js >/dev/null || { echo "SPA bundle missing (install.sh step-3 check)"; exit 1; }
  KAIDERA_AUTH_ENABLED=0 KAIDERA_DEPLOY_MODE=selfcontained /tmp/v/bin/uvicorn app.main:app --host 127.0.0.1 --port '"$PORT"' >/tmp/uvlog 2>&1 &
  for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:'"$PORT"'/console/version" >/dev/null 2>&1 && break; sleep 0.5; done
  ver="$(curl -sf "http://127.0.0.1:'"$PORT"'/console/version" || true)"
  [ -n "$ver" ] || { echo "=== CONSOLE DID NOT BOOT — last log lines:"; tail -25 /tmp/uvlog; exit 1; }
  echo "    console booted: $ver"
  # The SPA is mounted at /app (StaticFiles); assets serve at /app/assets/*. Use a trailing slash
  # + -L so the mount redirect is followed, then prove the referenced asset serves 200 from /app/.
  html="$(curl -fsSL "http://127.0.0.1:'"$PORT"'/app/" || true)"
  [ -n "$html" ] || { echo "    /app/ served no HTML — the SPA mount is missing"; tail -25 /tmp/uvlog; exit 1; }
  asset="$(printf "%s" "$html" | grep -oE "assets/[A-Za-z0-9._-]+\.js" | head -1)"
  [ -n "$asset" ] || { echo "    /app/ HTML references no assets/*.js — a broken bundle"; exit 1; }
  code="$(curl -s -o /dev/null -w "%{http_code}" -L "http://127.0.0.1:'"$PORT"'/app/$asset")"
  [ "$code" = "200" ] || { echo "    /app/$asset -> HTTP $code (a blank /app)"; exit 1; }
  echo "    SPA shell + asset served 200: /app/$asset"
' || die "clean-room console boot FAILED (see above) — a fresh machine would NOT come up."

# Cortex stack sanity — the GREENFIELD redist bootstraps a fresh Cortex from the committed compose +
# schema dump (a fresh DB loads cortex-schema-full.sql; it does NOT replay the migrations — those are
# the dogfood UPGRADE path). So validate: the compose PARSES + every build context resolves from the
# committed tree, and the schema dump is present + non-empty. A full `up` is deployment-specific.
: > "$TMP/local-cortex/.env" 2>/dev/null || true   # install.sh creates this; mimic it for the validate
if docker compose -f "$TMP/.agents/docker-compose.cortex.yml" config -q 2>/tmp/cclog; then
  ok "Cortex compose parses + all build contexts resolve (committed)"
else
  echo "    compose config output:"; sed 's/^/      /' /tmp/cclog | head -12
  die "Cortex compose INVALID from the committed tree — a fresh deploy's stack would not come up"
fi
if grep -q 'CREATE TABLE' "$TMP/.agents/data/cortex-schema-full.sql" 2>/dev/null; then
  ok "cortex-schema-full.sql present + non-empty (the greenfield fresh-DB bootstrap source)"
else
  die "cortex-schema-full.sql missing/empty — a fresh Cortex DB would bootstrap with NO schema"
fi

ok "clean-room PASSED — the committed redistributable boots a console + serves the SPA, and the Cortex stack validates on a bare box"
