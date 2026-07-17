#!/usr/bin/env bash
set -euo pipefail

ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
NAME="community-source-boundary"
FAIL=0

fail() {
  printf '  ERROR %s - %s\n' "$NAME" "$1" >&2
  FAIL=1
}

for path in \
  local-cortex/console/app/providers.py \
  local-cortex/console/app/providers_env.py \
  local-cortex/console/app/provider_check.py \
  local-cortex/console/app/license.py \
  local-cortex/console/app/license_client.py \
  local-cortex/console/app/license_refresh.py \
  local-cortex/console/app/kaidera_agent.py \
  local-cortex/console/app/skill_embed.py \
  local-cortex/console/app/platform_config.py \
  local-cortex/console/app/codex_oauth.py \
  local-cortex/console/app/native_operator.py \
  local-cortex/console/scripts/kaidera-os-license-gen; do
  [ ! -e "$ROOT/$path" ] || fail "forbidden implementation path exists: $path"
done
for directory in scripts/macos native/macos; do
  if [ -d "$ROOT/$directory" ] && find "$ROOT/$directory" -type f -print -quit | grep -q .; then
    fail "forbidden implementation directory contains files: $directory"
  fi
done

SCAN_PATHS=(
  "$ROOT/install.sh"
  "$ROOT/update.sh"
  "$ROOT/redistributable/scripts"
  "$ROOT/local-cortex/console/app"
  "$ROOT/local-cortex/console/spa/src"
)

PATTERN='KAIDERA_MANIFOLD_|KAIDERA_OS_LICENSE|/api/v1/license|license_token|org_login_token|manifold_access|pydantic_ai[.]providers|custom-providers|provider-key-test|PROVIDER_CATALOG|build_provider_key_plan|api[.]openai[.]com|api[.]anthropic[.]com|openrouter[.]ai/api|api[.]fireworks[.]ai'
HITS="$(grep -RInE "$PATTERN" "${SCAN_PATHS[@]}" 2>/dev/null \
  | grep -vE '/tests?/|[.]test[.](py|tsx|ts|js)$|app/static/excalidraw/' || true)"
if [ -n "$HITS" ]; then
  fail "provider, licensing, or commercial runtime markers found"
  printf '%s\n' "$HITS" | sed 's/^/      /' >&2
fi

if grep -Eq 'cryptography|pydantic-ai|pywebview' "$ROOT/local-cortex/console/requirements.txt"; then
  fail "commercial/provider dependency remains in console requirements"
fi

if grep -Eq 'getenv|environ|KAIDERA_OS_EDITION' "$ROOT/local-cortex/console/app/edition.py"; then
  fail "community identity is runtime-selectable"
fi

if ! PYTHONPATH="$ROOT/local-cortex/console" python3 - <<'PY'
from app import harness

expected = ["claude-code", "codex", "pi"]
assert harness.HARNESS_ORDER == expected
assert list(harness.HARNESSES) == expected
assert harness.visible_harness_order() == expected
PY
then
  fail "external harness boundary is not exactly claude-code, codex, pi"
fi

if [ "$FAIL" -ne 0 ]; then
  exit 1
fi
printf '  OK %s - provider-free community boundary is intact\n' "$NAME"
