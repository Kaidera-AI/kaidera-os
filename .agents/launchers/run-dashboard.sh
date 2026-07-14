#!/usr/bin/env bash
set -euo pipefail

ROOT="${CORTEX_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}"
REFRESH="${CORTEX_DASHBOARD_REFRESH:-30}"

cd "${ROOT}"
export CORTEX_WORKSPACE_ROOT="${CORTEX_WORKSPACE_ROOT:-${ROOT}}"
export PYTHONUNBUFFERED=1

printf '\033]0;Cortex Dashboard\007'
printf 'Starting Cortex Dashboard from %s (refresh %ss)\n' "${ROOT}" "${REFRESH}"
exec .agents/scripts/cortex-dashboard-md --watch "${REFRESH}" --pane
