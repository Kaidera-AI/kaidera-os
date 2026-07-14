#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

if [[ -z "${API_PYTHON:-}" ]]; then
  if [[ -x "$ROOT/.venv/api/bin/python" ]]; then
    API_PYTHON="$ROOT/.venv/api/bin/python"
  else
    API_PYTHON=python3
  fi
fi
if [[ -z "${CONSOLE_PYTHON:-}" ]]; then
  if [[ -x "$ROOT/local-cortex/console/.venv/bin/python" ]]; then
    CONSOLE_PYTHON="$ROOT/local-cortex/console/.venv/bin/python"
  else
    CONSOLE_PYTHON=python3
  fi
fi

step "Static Python and shell checks"
"$CONSOLE_PYTHON" -m ruff check .
"$CONSOLE_PYTHON" -m mypy \
  --ignore-missing-imports --follow-imports=skip --check-untyped-defs \
  local-cortex/console/app/auth.py \
  local-cortex/console/app/harness_runner.py \
  local-cortex/console/app/orchestrator.py \
  local-cortex/console/app/settings.py \
  local-cortex/console/app/skill_embed.py
while IFS= read -r -d '' file; do
  [[ -f "$file" ]] || continue
  shellcheck -x --severity=error "$file"
done < <(git ls-files -z '*.sh')
"$CONSOLE_PYTHON" -m compileall -q .agents/api .agents/scripts local-cortex/console/app \
  local-cortex/containers scripts redistributable/scripts
while IFS= read -r -d '' file; do
  [[ -f "$file" ]] || continue
  bash -n "$file"
done < <(git ls-files -z '*.sh')

step "Cortex API tests"
(
  cd .agents/api
  PYTHONPATH=. "$API_PYTHON" -m pytest -q tests
)

step "Cortex scripts and package fitness tests"
PYTHONPATH=.agents/api "$API_PYTHON" -m pytest -q .agents/tests
"$API_PYTHON" -m pytest -q scripts/fitness/tests

step "Kaidera OS console tests"
(
  cd local-cortex/console
  "$CONSOLE_PYTHON" -m pytest -q
)

step "SPA tests, types, lint, and production bundle"
(
  cd local-cortex/console/spa
  npm test
  npm run typecheck
  npm run lint
  npm run build
)

step "Release fitness"
scripts/fitness/run.sh

printf '\n\033[1;32mAll Kaidera OS QA checks passed.\033[0m\n'
