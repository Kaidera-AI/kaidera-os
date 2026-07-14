#!/usr/bin/env bash
# Kaidera OS fitness functions — automated gates from docs/THE_WAY_OF_DEVELOPMENT.md §6.
#
# The discipline lives in the SYSTEM, not anyone's head: each architectural rule is a runnable
# gate that FAILS when violated. Run this before any deploy; nothing ships with a red gate.
#
# Each check-*.sh is a standalone gate with one standardised contract:
#   - prints its own one-line "  ✅/❌ <name> — <msg>"  (indented; may add detail lines)
#   - exits 0 (pass) / 1 (fail)
# Add a gate = drop a new check-*.sh in this dir. No runner change needed.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FITNESS_ROOT="$(cd "$DIR/../.." && pwd)"
export FITNESS_ROOT

echo "── Kaidera OS fitness gates (THE_WAY_OF_DEVELOPMENT.md §6) ──"
fail=0; n=0
for check in "$DIR"/check-*.sh; do
  [ -e "$check" ] || continue
  n=$((n + 1))
  bash "$check" || fail=$((fail + 1))
done
echo "──────────────────────────────────────────────────────────"
if [ "$fail" -eq 0 ]; then
  echo "✅ all $n gates green"
  exit 0
fi
echo "❌ $fail/$n gate(s) RED — do not deploy until green"
exit 1
