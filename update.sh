#!/usr/bin/env bash
# update.sh — ONE deterministic command to move this install to the latest published release and
# VERIFY the exact things that have bitten us before (version drift, admin-token drift, host
# binding). Run this instead of hand-typing git commands or re-running installers by hand: this
# goes through the signed release bootstrap, prunes stale release-managed files, then tells you
# whether the restart took, whether the token is wired, and which host you're bound to.
#
#   ./update.sh                                  # update + KEEP the current host binding
#   KAIDERA_CONSOLE_HOST=0.0.0.0 ./update.sh      # update + (re)expose over Tailscale / LAN
#
# Idempotent + safe to re-run. Never touches the DB or your provider keys.
set -euo pipefail
cd "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

say(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
ok(){  printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
bad(){ printf '  \033[1;31m✗\033[0m %s\n' "$1"; }

PORT="${KAIDERA_CONSOLE_PORT:-8765}"
COMPOSE_PROJECT="${KAIDERA_COMPOSE_PROJECT:-kaidera-os-cortex}"

say "1/2  Apply the latest signed release (canonical source; bootstrap runs install.sh)"
[ -f dist/bootstrap.sh ] || { bad "dist/bootstrap.sh missing — this install cannot verify signed updates."; exit 1; }
DEST="${KAIDERA_DEST:-$PWD}"
if [ -d "$DEST/.git" ] && [ "${KAIDERA_ALLOW_GIT_DEST:-0}" != "1" ]; then
  bad "refusing to apply a redistributable update into a Git checkout: $DEST"
  printf '       signed updates prune files that are outside the redistributable package.\n'
  printf '       for development checkouts, pull/build normally and run ./install.sh.\n'
  printf '       for installed apps, set KAIDERA_DEST to a non-Git install directory.\n'
  exit 1
fi
KAIDERA_DEST="$DEST" bash dist/bootstrap.sh
cd "$DEST"
REL_VER="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' local-cortex/console/app/version.py | head -1)"
REV="$(git rev-parse --short HEAD 2>/dev/null || true)"
ok "on-disk release: v${REL_VER}${REV:+  ($REV)}"

say "2/2  Verify the recurring failure points"
RC=0

# The console restart (inside install.sh) is ASYNC — uvicorn takes a few seconds to import the app
# and bind the port. Wait for it to actually answer before judging, so a slow boot isn't
# misreported as 'unreachable' (a false alarm that sent you chasing a non-problem).
printf '  waiting for the console to answer'
_console_up=0
for _ in $(seq 1 20); do
  if curl -fsS "http://localhost:${PORT}/console/version" >/dev/null 2>&1; then _console_up=1; printf ' up\n'; break; fi
  printf '.'; sleep 1
done
[ "$_console_up" = "1" ] || printf ' no answer after 20s\n'

# --- console version: the RUNNING process, not just the disk (the 'stale 0.1.x' trap) ---
RUN_VER="$(curl -s "http://localhost:${PORT}/console/version" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
if [ "${RUN_VER}" = "${REL_VER}" ]; then
  ok "console process is v${RUN_VER} (matches the release)"
else
  bad "console reports '${RUN_VER:-unreachable}' but the release is v${REL_VER} — the restart didn't take."
  printf '       fix: sudo systemctl restart kaidera-os-console\n'; RC=1
fi

# --- admin token: the SAME gate project-creation uses (the 'can't register project' trap) ---
TOK="$(curl -s "http://localhost:${PORT}/cortex/admin-status" 2>/dev/null | grep -oE '"status":"[a-z_]+"' | cut -d'"' -f4 || true)"
case "${TOK}" in
  ok)        ok "Cortex admin token: ok — project creation will work";;
  mismatch)  bad "Cortex admin token MISMATCH (console vs cortex-api)."
             printf '       fix: docker compose -p %s -f .agents/docker-compose.cortex.yml up -d --force-recreate --no-deps cortex-api\n' "$COMPOSE_PROJECT"; RC=1;;
  no_token)  bad "Cortex admin token MISSING from local-cortex/.env."
             printf '       fix: re-run ./install.sh (it generates + writes one)\n'; RC=1;;
  *)         bad "Cortex admin status '${TOK:-unreachable}' — is cortex-api up?"
             printf '       check: docker compose -p %s -f .agents/docker-compose.cortex.yml ps\n' "$COMPOSE_PROJECT"; RC=1;;
esac

# --- host binding: localhost vs exposed (the 'can't open over Tailscale' trap) ---
if [ -f local-cortex/.console-host ]; then
  HOST="$(tr -d '[:space:]' < local-cortex/.console-host 2>/dev/null || true)"
fi
HOST="${HOST:-127.0.0.1}"
if [ "${HOST}" = "0.0.0.0" ]; then
  TSIP="$( (command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1) || true )"
  ok "console bound 0.0.0.0 — open it at http://${TSIP:-<this-VM-IP>}:${PORT}"
else
  ok "console bound ${HOST} (localhost only)"
  printf '       to expose over Tailscale: KAIDERA_CONSOLE_HOST=0.0.0.0 ./update.sh\n'
fi

echo ""
[ "${RC}" -eq 0 ] && printf '\033[1;32mUpdate complete — all checks green. Hard-refresh the browser.\033[0m\n' \
                  || printf '\033[1;33mUpdate applied, but a check above needs the one-line fix next to it.\033[0m\n'
exit "${RC}"
