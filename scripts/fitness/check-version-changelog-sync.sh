#!/usr/bin/env bash
# GATE: the app version and the CHANGELOG agree — so a shipped change can't quietly skip the
# version bump (the "version never updated" scar). version.py __version__ must equal the TOP
# CHANGELOG entry. Bump one, you bump the other. (WAY_OF_DEVELOPMENT §6)
set -uo pipefail
ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
name="version-changelog-sync"
VER_FILE="$ROOT/local-cortex/console/app/version.py"
CHG_FILE="$ROOT/local-cortex/console/CHANGELOG.md"

[ -f "$VER_FILE" ] && [ -f "$CHG_FILE" ] || { echo "  ⏭  $name — version.py / CHANGELOG not found, skipped"; exit 0; }

ver="$(grep -oE '__version__[[:space:]]*=[[:space:]]*"[^"]+"' "$VER_FILE" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
chg="$(grep -oE '^## v[0-9]+\.[0-9]+\.[0-9]+' "$CHG_FILE" | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"

if [ -z "$ver" ] || [ -z "$chg" ]; then
  echo "  ❌ $name — couldn't parse version (got '$ver') or top CHANGELOG entry (got '$chg')"
  exit 1
fi
if [ "$ver" != "$chg" ]; then
  echo "  ❌ $name — version.py ($ver) != top CHANGELOG entry ($chg) — a shipped change must bump BOTH"
  exit 1
fi
echo "  ✅ $name — version.py == CHANGELOG ($ver)"
exit 0
