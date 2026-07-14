#!/usr/bin/env bash
# GATE: the Kaidera OS release ships as ONE versioned bundle — console, cortex-api, and the
# cortex-* workers all build from a single source tree, so they can't drift to different
# versions (the deployment scar: console v0.1.154 deployed against cortex-api v0.1.148). Enforced via
# RELEASE_MANIFEST.json, which is DERIVED from the shipped compose + version.py and so cannot
# go stale: this gate regenerates it and diffs. (WAY_OF_DEVELOPMENT §6; handoff 72f8b14a)
#
# Three checks:
#   1. FRESH — regenerate the manifest and diff vs the committed file. Bumping the version,
#      adding a worker, or adding a migration without re-cutting the manifest fails here.
#   2. AGREE — manifest.release_version == version.py == top CHANGELOG entry.
#   3. NO DRIFT — manifest.drift_violations empty (no Kaidera OS unit pinned to a prebuilt image
#      tag instead of built from source).
set -uo pipefail
ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
name="package-unified"
GEN="$ROOT/scripts/release/gen-release-manifest.py"
MAN="$ROOT/RELEASE_MANIFEST.json"
VER_FILE="$ROOT/local-cortex/console/app/version.py"
CHG_FILE="$ROOT/local-cortex/console/CHANGELOG.md"

[ -f "$GEN" ] && [ -f "$MAN" ] || { echo "  ⏭  $name — generator/manifest not found, skipped"; exit 0; }

# 1. FRESH — the committed manifest must equal a fresh regeneration from the current tree.
# Generate to a temp FILE (never a shell var — command substitution strips the trailing
# newline and would mismatch an identical manifest) and diff file-to-file.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
if ! python3 "$GEN" --stdout > "$tmp" 2>/dev/null; then
  echo "  ❌ $name — manifest generator failed to run"
  exit 1
fi
if ! diff -q "$tmp" "$MAN" >/dev/null 2>&1; then
  echo "  ❌ $name — RELEASE_MANIFEST.json is STALE (tree changed). Run: python3 scripts/release/gen-release-manifest.py"
  exit 1
fi

# 2. AGREE — the one version across manifest, version.py, and the CHANGELOG.
mver="$(python3 -c "import json,sys;print(json.load(open('$MAN'))['release_version'])" 2>/dev/null)"
ver="$(grep -oE '__version__[[:space:]]*=[[:space:]]*"[^"]+"' "$VER_FILE" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
chg="$(grep -oE '^## v[0-9]+\.[0-9]+\.[0-9]+' "$CHG_FILE" | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
if [ "$mver" != "$ver" ] || [ "$mver" != "$chg" ]; then
  echo "  ❌ $name — version skew: manifest=$mver version.py=$ver CHANGELOG=$chg (all must agree)"
  exit 1
fi

# 3. NO DRIFT — no Kaidera OS unit pinned to a prebuilt image instead of built from source.
viol="$(python3 -c "import json;print(len(json.load(open('$MAN'))['drift_violations']))" 2>/dev/null)"
if [ "$viol" != "0" ]; then
  echo "  ❌ $name — $viol Kaidera OS unit(s) pinned to a prebuilt image (drift). See RELEASE_MANIFEST.json drift_violations"
  exit 1
fi

units="$(python3 -c "import json;print(len(json.load(open('$MAN'))['kaidera_os_units']))" 2>/dev/null)"
echo "  ✅ $name — $units Kaidera OS units unified at v$mver, all source-built, manifest fresh"
exit 0
