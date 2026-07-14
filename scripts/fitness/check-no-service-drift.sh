#!/usr/bin/env bash
# GATE: a RUNNING service's code is committed — "git matches what's running".
# WHY: the handoff-dedupe guardrail sat UNCOMMITTED + undeployed for who-knows-how-long — live
# behavior the repo had no record of, and the reason a duplicate slipped through. If a service's
# code dir has uncommitted changes while that service is up, git and reality have drifted.
# (WAY_OF_DEVELOPMENT §4.3)
set -uo pipefail
ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
name="no-service-drift"
drift=""

# A service dir drifts only if it's BOTH running AND dirty (noise dirs excluded).
check_dir() {
  local dir="$1" running="$2"
  [ "$running" = "yes" ] || return 0
  [ -d "$ROOT/$dir" ] || return 0
  local d
  d="$(git -C "$ROOT" status --porcelain -- "$dir" 2>/dev/null | grep -vE '\.obsidian|__pycache__|\.pyc|/\.venv/' || true)"
  [ -n "$d" ] && drift="${drift}
  ❌ $name — '$dir' is RUNNING but has uncommitted changes (commit before it counts as deployed):
$(echo "$d" | sed 's/^/       /')"
}

cortex_up=no;  curl -s -o /dev/null --max-time 2 http://localhost:8501/health >/dev/null 2>&1 && cortex_up=yes
console_up=no; curl -s -o /dev/null --max-time 2 http://127.0.0.1:8765/      >/dev/null 2>&1 && console_up=yes

check_dir ".agents/api"             "$cortex_up"     # the Cortex API (port 8501)
check_dir "local-cortex/console/app" "$console_up"   # the console (port 8765)

if [ -n "$drift" ]; then
  echo "$drift"
  exit 1
fi
echo "  ✅ $name — running services match git (no uncommitted live code)"
exit 0
