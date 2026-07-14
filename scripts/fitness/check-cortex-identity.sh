#!/usr/bin/env bash
# Cortex is a permanent shared-component name. Product rebrands must not prefix
# or replace it on current operator/runtime surfaces.
set -euo pipefail

ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT"

surfaces=(
  CORTEX_QUICKSTART.md
  cortex.md
  install.sh
  scripts/_cortex_env.sh
  .agents/scripts/cortex-backup
  .agents/scripts/cortex-sync-generate-harness
  local-cortex/README.md
  local-cortex/console/app/templates
  local-cortex/console/spa/src
  redistributable/docs/LOCAL_CORTEX_QUICKSTART.md
)

pattern='Kaidera([[:space:]]+(OS|AI))?[[:space:]]+Cortex'
offenders="$(git grep -nEI "$pattern" -- "${surfaces[@]}" 2>/dev/null || true)"
if [ -n "$offenders" ]; then
  printf '  \033[31m❌ cortex-identity — Cortex was product-prefixed on a current surface:\033[0m\n'
  printf '%s\n' "$offenders" | sed 's/^/       /'
  exit 1
fi

grep -Fq 'Cortex is the canonical, permanent name' docs/design/00-overview.md
grep -Fq 'Cortex is a mandatory part of the Kaidera OS redist' docs/design/02-cortex-integration.md

printf '  \033[1;32m✅ cortex-identity — Cortex keeps its permanent shared-component name.\033[0m\n'
