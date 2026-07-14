#!/usr/bin/env bash
# Build the console SPA (npm ci + vite build) and ASSERT the bundle is complete. This is the ONE
# build contract — called by install.sh (step 3), the Dockerfile build stage, console.spec, and
# the check-spa-built fitness gate — so the gitignored spa/dist can never silently ship empty.
# That blank-/app class of bug (the JS bundle vanishing from a push, or a fresh checkout never
# building it) burned us repeatedly in v0.1.79–0.1.81; one contract + a hard assert closes it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPA_DIR="$(cd "$HERE/../spa" && pwd)"

command -v npm >/dev/null 2>&1 || {
  echo "build-spa: npm not found — install Node.js to build the SPA." >&2
  exit 1
}

echo "build-spa: npm ci + build in $SPA_DIR"
( cd "$SPA_DIR" && npm ci --no-audit --no-fund && npm run build )

INDEX="$SPA_DIR/dist/index.html"
[ -f "$INDEX" ] || { echo "build-spa: FAILED — $INDEX missing after build." >&2; exit 1; }
# The gitignore-drops-JS footgun: an index.html with no JS bundle renders blank. Assert a .js exists.
ls "$SPA_DIR"/dist/assets/*.js >/dev/null 2>&1 || {
  echo "build-spa: FAILED — no dist/assets/*.js after build (incomplete bundle)." >&2
  exit 1
}
echo "build-spa: ✓ bundle complete ($INDEX + assets/*.js)"
