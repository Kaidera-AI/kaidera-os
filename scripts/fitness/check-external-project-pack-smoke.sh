#!/usr/bin/env bash
# Smoke a project-pack source outside Kaidera OS core.
set -euo pipefail

NAME="external-project-pack-smoke"
ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CLI="$ROOT/redistributable/scripts/cortex-project-pack"

source_root="${KAIDERA_OS_TURNKEY_PROJECTS_ROOT:-$ROOT/../kaidera-turnkey-projects}"
mode="external checkout"
if [ ! -d "$source_root" ]; then
  source_root="$ROOT/redistributable/examples/project-pack-basic"
  mode="bundled example fallback"
fi

target="$(mktemp -d "${TMPDIR:-/tmp}/kaidera-os-pack-smoke.XXXXXX")"
out="$(mktemp "${TMPDIR:-/tmp}/kaidera-os-pack-smoke.out.XXXXXX")"
trap 'rm -rf "$target" "$out"' EXIT

if ! python3 "$CLI" smoke "$source_root" --target "$target" >"$out" 2>&1; then
  echo "  ❌ $NAME — project-pack smoke failed ($mode)"
  sed 's/^/     /' "$out"
  exit 1
fi

echo "  ✅ $NAME — project-pack smoke passed ($mode)"
