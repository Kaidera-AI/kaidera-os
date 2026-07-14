#!/usr/bin/env bash
# GATE: self-contained mode reads NO host-user files. The distributable runs as a native
# console on a fresh Linux VM with NO Mac host — so in KAIDERA_DEPLOY_MODE=selfcontained the
# auth path must never read ~/.pi, ~/.claude, or local-cortex/.env; auth comes from the app-DB
# settings store only. Pins app/deploy_mode.py + the gated sinks. (WAY_OF_DEVELOPMENT §6)
set -uo pipefail
ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
name="selfcontained-no-host"
CONSOLE="$ROOT/local-cortex/console"

[ -d "$CONSOLE" ] || { echo "  ⏭  $name — console dir not found, skipped"; exit 0; }
cd "$CONSOLE" || exit 1

# (1) BEHAVIOURAL — the real guarantee: with a real host auth file + .env present, the
#     auth sinks return "" in self-contained mode. If a guard is removed, this fails.
if ! python3 -m pytest tests/test_deploy_mode.py -q >/tmp/sc_gate.txt 2>&1; then
  echo "  ❌ $name — self-contained auth tests FAILED (a host-file read is not gated)"
  tail -8 /tmp/sc_gate.txt | sed 's/^/      /'
  rm -f /tmp/sc_gate.txt
  exit 1
fi
rm -f /tmp/sc_gate.txt

# (2) STATIC backstop: the auth module that reads host-user files must reference the
#     deploy-mode guard, so a NEW ungated read can't quietly land. The host-user-file reads
#     (~/.pi auth.json + local-cortex/.env) live in ONE shared module now — app/providers_env.py
#     (the canonical env/auth helpers app/providers.py + app/provider_check.py delegate to) — and
#     it MUST gate them. (Before the providers_env carve this pinned both consumer modules; the
#     guard moved with the reads, so the pin follows it. Part (1) above still proves the behaviour
#     for BOTH consumer modules' delegating wrappers via tests/test_deploy_mode.py.)
miss=""
f="app/providers_env.py"
grep -q "is_selfcontained" "$f" || miss=" $f"
if [ -n "$miss" ]; then
  echo "  ❌ $name — host-file auth module(s) missing the is_selfcontained() guard:$miss"
  exit 1
fi

echo "  ✅ $name — self-contained mode reads no ~/.pi / ~/.claude / .env (gated + tested)"
exit 0
