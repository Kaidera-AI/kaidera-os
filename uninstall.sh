#!/usr/bin/env bash
# Kaidera OS local deployment - CLEAN UNINSTALL / GREENFIELD WIPE.
#
# Removes EVERYTHING this install created so a subsequent ./install.sh starts from absolute zero:
#   * the kaidera-os-console systemd/launchd service (plus retired labels),
#   • the Cortex + app-DB containers AND their DATA VOLUMES (all agent memory + app-DB rows),
#   • the console venv + SPA node_modules,
#   • the install-generated files (the admin token in local-cortex/.env, the .console-host memo,
#     the run script, the systemd unit file).
#
# DESTRUCTIVE + IRREVERSIBLE — it wipes ALL local Cortex/app-DB DATA on this machine. Use it for a
# clean greenfield re-deploy (e.g. moving a project onto a new redist). It does NOT touch code —
# run the signed bootstrap/update path separately to refresh the package.
#
#   ./uninstall.sh            # interactive — type "wipe" to confirm
#   ./uninstall.sh --yes      # non-interactive (scripted / CI)
#
# It is intentionally SAFE on a half-installed or never-installed box: every step is guarded + no-ops
# gracefully if the service/containers/files aren't there.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_PROJECT="${KAIDERA_COMPOSE_PROJECT:-kaidera-os-cortex}"
CORTEX_COMPOSE="$REPO_ROOT/.agents/docker-compose.cortex.yml"
CONSOLE_DIR="$REPO_ROOT/local-cortex/console"

say(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }

# A `sudo` shim — use sudo when present + not already root, else run bare (and tolerate failure).
SUDO=""; [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

if [ "${1:-}" != "--yes" ]; then
  printf '\033[1;31m'
  printf 'GREENFIELD WIPE - this removes the Kaidera OS console service, the Cortex containers AND their\n'
  printf 'DATA VOLUMES (every agent-memory + app-DB row), the venv, and the generated admin token/config.\n'
  printf 'There is NO undo. Type "wipe" to proceed: \033[0m'
  read -r ans
  [ "$ans" = "wipe" ] || { echo "  aborted — nothing changed."; exit 1; }
fi

say "1/3 Stop + remove the console service"
if command -v systemctl >/dev/null 2>&1; then
  service="kaidera-os-console"
  $SUDO systemctl stop "$service" 2>/dev/null || true
  $SUDO systemctl disable "$service" 2>/dev/null || true
  $SUDO rm -f "/etc/systemd/system/$service.service" 2>/dev/null || true
  $SUDO systemctl daemon-reload 2>/dev/null || true
  ok "Kaidera OS and retired console services stopped + removed"
else
  ok "no systemd — nothing to stop"
fi
if command -v launchctl >/dev/null 2>&1; then
  for label in ai.kaidera.kaidera-os.console ai.adaptech.kaidera.console; do
    plist="$HOME/Library/LaunchAgents/$label.plist"
    launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
    rm -f "$plist" 2>/dev/null || true
  done
  ok "Kaidera OS and retired console LaunchAgents removed"
fi
# also kill any foreground uvicorn started via the run script
pkill -f 'uvicorn app.main:app' 2>/dev/null || true

say "2/3 Remove the Cortex containers + DATA VOLUMES (the greenfield wipe)"
if command -v docker >/dev/null 2>&1 && [ -f "$CORTEX_COMPOSE" ]; then
  # -v removes the named volumes (cortex-pg-data, harness-appdb-data) = ALL the data.
  docker compose -p "$COMPOSE_PROJECT" -f "$CORTEX_COMPOSE" down -v --remove-orphans 2>/dev/null || true
  ok "Cortex stack down + data volumes removed (project: $COMPOSE_PROJECT)"
else
  ok "docker/compose or the compose file absent — nothing to remove"
fi

say "3/3 Remove the venv + install-generated files"
rm -rf "$CONSOLE_DIR/.venv" "$CONSOLE_DIR/spa/node_modules" 2>/dev/null || true
rm -f "$REPO_ROOT/run-kaidera-os-console.sh" "$REPO_ROOT/kaidera-os-console.service" 2>/dev/null || true
rm -f  "$REPO_ROOT/local-cortex/.console-host" "$REPO_ROOT/local-cortex/.env" 2>/dev/null || true
# The Cortex-CLI PATH drop-in (install.sh step 4b). Remove the system-wide profile.d file (sudo
# if needed) AND the grep-guarded fallback block from the invoking user's shell rc, if present.
$SUDO rm -f /etc/profile.d/kaidera-cortex.sh 2>/dev/null || true
for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
  [ -f "$RC" ] || continue
  sed -i.kaidera-bak \
    -e '/# >>> kaidera-cortex-cli >>>/,/# <<< kaidera-cortex-cli <<</d' \
    "$RC" 2>/dev/null && rm -f "$RC.kaidera-bak" 2>/dev/null || true
done
ok "venv + node_modules + generated token/host/run-script/unit + Cortex-CLI PATH removed"

say "Done — clean slate"
cat <<EOF
  Everything this install created is gone (the git tree is untouched).

  For a fresh GREENFIELD install of the clean package:
    gh release download -R Kaidera-AI/homebrew-kaidera -p bootstrap.sh -O bootstrap.sh
    KAIDERA_CONSOLE_HOST=0.0.0.0 bash bootstrap.sh

  A fresh Cortex DB will bootstrap cortex-schema-full.sql (the clean final schema) — born with the
  normalized identity, no legacy data, a fresh admin token. install.sh rebuilds the container images
  so the new code takes effect.
EOF
