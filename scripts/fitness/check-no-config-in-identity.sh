#!/usr/bin/env bash
# GATE: no runtime config (harness/model/reasoning) hardcoded in an agent identity file.
# WHY: runtime config has ONE canonical source — the app-DB per-agent config (run + execution +
# display all read it). Duplicating it into the persona file is what drifted into the xhigh-vs-medium
# error. The identity describes the ROLE; the operator sets the ENGINE. (WAY_OF_DEVELOPMENT §4.1)
set -uo pipefail
ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
name="no-config-in-identity"
AGENTS="$ROOT/agents"

[ -d "$AGENTS" ] || { echo "  ⏭  $name — no agents/ dir, skipped"; exit 0; }

# Offenders: frontmatter config keys, or "Run on **<engine>" prose pinning an engine.
hits="$(grep -rEn '^[[:space:]-]*(harness|model|reasoning|designation):|[Hh]arness/[Mm]odel:|Run on \*\*(pi|gpt|claude|opus|codex|haiku)' "$AGENTS"/*_IDENTITY.md 2>/dev/null || true)"
if [ -n "$hits" ]; then
  echo "  ❌ $name — identity files hardcode runtime config (move it to the app-DB config):"
  echo "$hits" | sed 's/^/       /'
  exit 1
fi
echo "  ✅ $name — identities are persona-only (engine lives in the app-DB)"
exit 0
