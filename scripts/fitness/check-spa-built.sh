#!/usr/bin/env bash
# GATE: if the console serves an SPA at /app (main.py references SPA_DIST_DIR), the prebuilt bundle
# MUST exist on disk — index.html + at least one JS chunk. A push with an absent/empty dist deploys
# a BLANK /app (the v0.1.79–0.1.81 scar: the gitignored bundle silently dropped). Build it with
# local-cortex/console/scripts/build-spa.sh. Filesystem check (dist is gitignored) — pairs with the
# release force-add of spa/dist.
set -uo pipefail
name="spa-built"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MAIN="$ROOT/local-cortex/console/app/main.py"
DIST="$ROOT/local-cortex/console/spa/dist"

# Only enforce when the app actually mounts an SPA bundle.
grep -q "SPA_DIST_DIR" "$MAIN" 2>/dev/null || { echo "  ⏭  $name — app doesn't mount an SPA, skipped"; exit 0; }

if [ ! -f "$DIST/index.html" ]; then
  echo "  ❌ $name — main.py mounts an SPA but $DIST/index.html is MISSING → /app would be blank."
  echo "     fix: bash local-cortex/console/scripts/build-spa.sh"
  exit 1
fi
if ! ls "$DIST"/assets/*.js >/dev/null 2>&1; then
  echo "  ❌ $name — dist/index.html present but NO dist/assets/*.js (incomplete bundle → blank /app)."
  echo "     fix: bash local-cortex/console/scripts/build-spa.sh"
  exit 1
fi
echo "  ✅ $name — SPA bundle present (index.html + assets/*.js)"
exit 0
