#!/usr/bin/env bash
# Kaidera OS local deployment - TEARDOWN / RESET.
#
# Removes the entire local stack so you can start fresh OR reclaim disk: containers,
# images, build cache, ALL Cortex/app volumes (agent memory included), the console venv,
# the generated runner/unit files, and the systemd service.
#
# DESTRUCTIVE: this DELETES the Cortex database (agents, memory, projects). On a fresh VM
# that's the point; on a machine with data you care about, don't run it. Requires a
# confirmation unless you pass --yes / -y.
#
#   ./cleanup.sh           # prompts before deleting
#   ./cleanup.sh --yes     # no prompt (scripted)
#
# After it runs, a single ./install.sh rebuilds everything clean.
set -uo pipefail   # NOT -e: best-effort — keep going past anything already gone.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORTEX_COMPOSE="$REPO_ROOT/.agents/docker-compose.cortex.yml"
COMPOSE_PROJECT="${KAIDERA_COMPOSE_PROJECT:-kaidera-os-cortex}"   # fitness:allow-literal product stack name

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }

ASSUME_YES=0
case "${1:-}" in --yes|-y) ASSUME_YES=1 ;; esac

if [ "$ASSUME_YES" != "1" ]; then
  printf '\033[1;33m⚠ This DELETES the entire local stack INCLUDING the Cortex database\n'
  printf '   (all agents, memory, projects). It cannot be undone.\033[0m\n'
  read -r -p "Type 'wipe' to proceed: " reply
  [ "$reply" = "wipe" ] || { echo "aborted."; exit 1; }
fi

say "1/6 Disk before"
df -h / | sed 's/^/  /'

say "2/6 Stop + remove the compose stack"
docker compose -p "$COMPOSE_PROJECT" -f "$CORTEX_COMPOSE" down -v --remove-orphans 2>/dev/null && ok "compose down -v"

say "3/6 Remove leftover named containers + volumes (compose uses explicit names)"
for c in cortex-pg cortex-api cortex-graph-worker cortex-embed-worker cortex-pdf-worker \
         cortex-audio-worker cortex-vision-worker harness-appdb harness-appdb-migrate cortex-console; do
  docker rm -f "$c" >/dev/null 2>&1 && ok "rm container $c" || true
done
for v in cortex-pg-data cortex-graphs cortex-models cortex-vendor harness-appdb-data; do
  docker volume rm "$v" >/dev/null 2>&1 && ok "rm volume $v" || true
done

say "4/6 Remove built images + prune build cache (the big disk reclaim)"
docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
  | grep -E "kaidera([-_]os)?[-_]cortex|cortex-(api|pg|.*worker)" | xargs -r docker rmi -f >/dev/null 2>&1
docker system prune -af >/dev/null 2>&1 && ok "pruned unused images + build cache"

say "5/6 Console venv + generated files + systemd unit"
rm -rf "$REPO_ROOT/local-cortex/console/.venv" 2>/dev/null && ok "removed console venv"
rm -f "$REPO_ROOT/run-kaidera-os-console.sh" "$REPO_ROOT/kaidera-os-console.service" \
      "$REPO_ROOT/local-cortex/.env" 2>/dev/null && ok "removed generated files"
if command -v systemctl >/dev/null 2>&1; then
  service="kaidera-os-console"
  if [ -f "/etc/systemd/system/$service.service" ]; then
    sudo systemctl disable --now "$service" >/dev/null 2>&1
    sudo rm -f "/etc/systemd/system/$service.service" >/dev/null 2>&1
    ok "removed systemd service $service"
  fi
  sudo systemctl daemon-reload >/dev/null 2>&1
fi

say "6/6 Disk after"
df -h / | sed 's/^/  /'
echo ""
ok "Clean slate. Re-run ./install.sh to rebuild from scratch."
