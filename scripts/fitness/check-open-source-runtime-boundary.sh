#!/usr/bin/env bash
set -uo pipefail

NAME="open-source-runtime-boundary"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${FITNESS_ROOT:-$(cd "$DIR/../.." && pwd)}"
failures=""

fail() {
  failures="${failures}$1
"
}

forbidden_paths=(
  "local-cortex/console/app/license.py"
  "local-cortex/console/app/license_client.py"
  "local-cortex/console/app/license_refresh.py"
  "local-cortex/console/app/native_operator.py"
  "local-cortex/console/app/operator_menubar.py"
  "local-cortex/console/operator.spec"
  "local-cortex/console/scripts/kaidera-os-license-gen"
  "local-cortex/console/app/templates/_settings_custom_providers.html"
  "local-cortex/console/app/templates/_settings_license_login_message.html"
  "local-cortex/console/app/templates/_settings_license_polling.html"
  "native/macos/KaideraOSOperator/Package.swift"
  "scripts/macos/build-console-dmg.sh"
  "scripts/macos/build-operator-dmg.sh"
  "scripts/release/bake-public-edition.py"
  ".kaidera-os-edition"
)

for relative in "${forbidden_paths[@]}"; do
  if [ -e "$ROOT/$relative" ]; then
    fail "commercial/native runtime path exists: $relative"
  fi
done

provider_files=(
  "$ROOT/local-cortex/console/app/providers.py"
  "$ROOT/local-cortex/console/app/providers_env.py"
  "$ROOT/local-cortex/console/app/provider_check.py"
  "$ROOT/local-cortex/console/app/settings_module/api.py"
  "$ROOT/local-cortex/console/app/settings_module/service.py"
  "$ROOT/local-cortex/console/app/skill_embed.py"
)
provider_pattern='(anthropic_api_key|openai_api_key|openrouter_api_key|fireworks_api_key|gemini_api_key|aws_access_key_id|aws_secret_access_key|custom[-_ ]providers?|/custom-providers)'
provider_hits="$(grep -nEi "$provider_pattern" "${provider_files[@]}" 2>/dev/null || true)"
[ -z "$provider_hits" ] || fail "direct/custom provider implementation found in the public provider seam:
$provider_hits"

spa_root="$ROOT/local-cortex/console/spa/src"
if [ -d "$spa_root" ]; then
  spa_hits="$(grep -RInE '(license(Login|Activate|Heartbeat|Restore)|addCustomProvider|deleteCustomProvider|/settings/.*/billing)' "$spa_root" --include='*.ts' --include='*.tsx' 2>/dev/null | grep -vE '[.]test[.](ts|tsx):' || true)"
  [ -z "$spa_hits" ] || fail "commercial/custom client contract found in the public SPA:
$spa_hits"
fi

if [ -n "$failures" ]; then
  echo "  FAIL $NAME"
  printf '%s' "$failures" | sed '/^$/d' | sed 's/^/       /'
  exit 1
fi

echo "  PASS $NAME - Manifold-only runtime; no commercial activation or native operator"
