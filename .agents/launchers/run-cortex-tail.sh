#!/usr/bin/env bash
set -euo pipefail

ROOT="${CORTEX_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}"
cd "${ROOT}"
export PATH="${ROOT}/.agents/scripts:${HOME}/.local/bin:${PATH:-}"
exec cortex-tail --count 5
