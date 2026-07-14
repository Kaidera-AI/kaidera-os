#!/usr/bin/env bash
# Gate: OSS/public redistributables must not carry local state, credentials, or
# customer/project payloads. This scans the shipped archive by default, not the
# whole development checkout.
set -uo pipefail

NAME="oss-package-hygiene"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${FITNESS_ROOT:-$(cd "$DIR/../.." && pwd)}"
SCAN_ROOT="${FITNESS_OSS_SCAN_ROOT:-}"
TMP_DIR=""

cleanup() {
  [ -z "$TMP_DIR" ] || rm -rf "$TMP_DIR"
}
trap cleanup EXIT

failures=""
add_failure() {
  failures="${failures}$1
"
}

prepare_archive_scan_root() {
  if [ -n "$SCAN_ROOT" ]; then
    [ -d "$SCAN_ROOT" ] || {
      echo "  ❌ $NAME — FITNESS_OSS_SCAN_ROOT does not exist: $SCAN_ROOT"
      exit 1
    }
    return
  fi

  cd "$ROOT" || {
    echo "  ❌ $NAME — cannot cd to repo root"
    exit 1
  }
  TMP_DIR="$(mktemp -d)"
  ref="$(git stash create 2>/dev/null || true)"
  ref="${ref:-HEAD}"
  git archive --worktree-attributes "$ref" 2>/dev/null | tar -x -C "$TMP_DIR" 2>/dev/null || {
    echo "  ❌ $NAME — could not build the redist archive"
    exit 1
  }
  SCAN_ROOT="$TMP_DIR"
}

relative_files() {
  find "$SCAN_ROOT" -type f 2>/dev/null | sed "s#^$SCAN_ROOT/##" | sort -u
}

relative_paths() {
  find "$SCAN_ROOT" \( -type f -o -type d \) 2>/dev/null | sed "s#^$SCAN_ROOT/##" | sed '/^$/d' | sort -u
}

text_hits() {
  pattern="$1"
  grep -RIlE "$pattern" "$SCAN_ROOT" 2>/dev/null \
    | sed "s#^$SCAN_ROOT/##" \
    | grep -vE '(^|/)(__pycache__|node_modules|\.venv|dist|tests?)/|[.]test[.][A-Za-z0-9]+$|[.]spec[.][A-Za-z0-9]+$|^scripts/fitness/|^local-cortex/console/app/static/excalidraw/' \
    | sort -u \
    || true
}

prepare_archive_scan_root

# Generated/local state must be regenerated per installation, never inherited by
# a public package.
generated_state="$(
  relative_paths | grep -E '^(\.cortex(/|$)|\.obsidian(/|$)|Program(/|$)|plans(/|$)|_adr(/|$)|_research(/|$)|_design_knowledge(/|$)|_setup(/|$)|\.agents/(agents|bootstrap|prompts|roles|rules|skills|backups)(/|$)|\.agents/config/(workspace[.]json|runtime[.]yaml)$|\.agents/memory/imports/external(/|$))' || true
)"
[ -z "$generated_state" ] || add_failure "generated/local state paths ship:
$(printf '%s\n' "$generated_state" | sed 's/^/       • /')"

vendor_payload="$(relative_paths | grep -E '^\.agents/data/vendor(/|$)' || true)"
[ -z "$vendor_payload" ] || add_failure "development-only vendor payload ships:
$(printf '%s\n' "$vendor_payload" | sed 's/^/       • /')"

retired_brand_pattern='engen(ai|os)'
retired_brand_paths="$(relative_paths | grep -Ei "$retired_brand_pattern" || true)"
[ -z "$retired_brand_paths" ] || add_failure "retired brand identifiers ship in paths:
$(printf '%s\n' "$retired_brand_paths" | sed 's/^/       • /')"
retired_brand_content="$(grep -RIlE "$retired_brand_pattern" "$SCAN_ROOT" 2>/dev/null | sed "s#^$SCAN_ROOT/##" | sort -u || true)"
[ -z "$retired_brand_content" ] || add_failure "retired brand identifiers ship in content:
$(printf '%s\n' "$retired_brand_content" | sed 's/^/       • /')"

# Project/customer payloads belong in external turnkey project repos or generic
# examples, not in Kaidera OS core. Keep this default narrow; deployments can add
# stricter names through KAIDERA_OS_OSS_FORBIDDEN_PROJECT_PATTERNS.
PROJECT_PATTERNS="${KAIDERA_OS_OSS_FORBIDDEN_PROJECT_PATTERNS:-talib|dxb|marlow|marketing-os|marketing_os|asw-marketing|asw_marketing}"
if [ -n "$PROJECT_PATTERNS" ]; then
  project_paths="$(
    relative_paths | grep -Ei "(^|/)(${PROJECT_PATTERNS})(/|$)" || true
  )"
  [ -z "$project_paths" ] || add_failure "customer/project package paths ship:
$(printf '%s\n' "$project_paths" | sed 's/^/       • /')"
fi

# Local secret-bearing files must not be present. Templates are allowed.
secret_paths="$(
  relative_files \
    | grep -Ei '(^|/)([.]env($|[.])|[.]npmrc$|[.]pypirc$|.*[.](pem|p12|p8|mobileprovision)$|.*(credentials|service-account).*[.](json|ya?ml)$)' \
    | grep -vE '(^|/)[.]env[.]example$|(^|/)tests?/' \
    || true
)"
[ -z "$secret_paths" ] || add_failure "secret-like files ship:
$(printf '%s\n' "$secret_paths" | sed 's/^/       • /')"

personal_hits="$(text_hits '(/Users/[^/"[:space:]]+|/home/amadmalik|GoogleDrive-amad@adaptech[.]ai)')"
[ -z "$personal_hits" ] || add_failure "personal host paths ship:
$(printf '%s\n' "$personal_hits" | sed 's/^/       • /')"

credential_hits="$(text_hits '(-----BEGIN (RSA |OPENSSH |EC |DSA |PRIVATE )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[0-9A-Za-z-]{20,}|sk-[A-Za-z0-9][A-Za-z0-9_-]{20,})')"
[ -z "$credential_hits" ] || add_failure "credential-looking payloads ship:
$(printf '%s\n' "$credential_hits" | sed 's/^/       • /')"

if [ -n "${KAIDERA_OS_OSS_FORBIDDEN_PATTERNS:-}" ]; then
  forbidden_hits="$(text_hits "$KAIDERA_OS_OSS_FORBIDDEN_PATTERNS")"
  [ -z "$forbidden_hits" ] || add_failure "deployment-forbidden patterns ship:
$(printf '%s\n' "$forbidden_hits" | sed 's/^/       • /')"
fi

if [ -n "$failures" ]; then
  echo "  ❌ $NAME — OSS/public package hygiene failed:"
  printf '%s' "$failures" | sed '/^$/d'
  echo "     fix: export-ignore local state, remove credentials, or move project payloads into turnkey packages."
  exit 1
fi

echo "  ✅ $NAME — shipped archive has no generated state, local secrets, personal paths, or project payloads"
exit 0
