#!/usr/bin/env bash
# GATE: provider VISIBILITY is edition-gated; only Manifold runtime auth may use
# the signed manifold_access entitlement.
# WHY: the product rule is "providers are restricted ONLY programmatically". A
# restriction a license can toggle is a restriction a license can UN-toggle — so the
# provider catalog must remain structurally unreachable from license features. Layer C
# adds one narrow runtime exception: the PUBLIC-visible Manifold row resolves credentials
# only when the signed grant contains manifold_access. That atom cannot expose any other
# provider.
#
# Two structural invariants (the third — "no provider:* feature is honored" — is
# covered by the unit test tests/test_edition_entitlements.py::test_provider_feature_is_ignored):
#   1. app/edition.py MUST NOT import app.license  (edition owns providers; it must not
#      depend on the license layer, or the separation is only skin-deep).
#   2. app/providers.py MUST NOT reference the license layer except explicitly marked
#      manifold_access checks. The provider whitelist still keys ONLY on edition.is_public().
#
# House contract (matches the sibling check-*.sh): prints one "  ✅/❌ <name> — <msg>";
# exits 0 (pass) / 1 (fail). run.sh auto-discovers it.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FITNESS_ROOT="${FITNESS_ROOT:-$(cd "$DIR/../.." && pwd)}"
SCAN_ROOT="${FITNESS_SCAN_ROOT:-$FITNESS_ROOT}"

name="edition-not-license-gated"
app="$SCAN_ROOT/local-cortex/console/app"
edition_file="$app/edition.py"
providers_file="$app/providers.py"

fail=0
detail=""

# Invariant 1 — edition.py must not import the license module.
if [ -f "$edition_file" ]; then
  hit="$(grep -nE '^[[:space:]]*(from[[:space:]]+app[[:space:]]+import[[:space:]].*\blicense\b|from[[:space:]]+(app\.|\.)?license[[:space:]]+import|import[[:space:]]+(app\.)?license)' \
    "$edition_file" 2>/dev/null | grep -vE '# *fitness:allow' || true)"
  if [ -n "$hit" ]; then
    fail=1
    detail="${detail}     edition.py imports the license layer (it must not — providers are edition-only):
$(printf '%s\n' "$hit" | sed 's/^/        /')
"
  fi
fi

# Invariant 2 — providers.py must not call into the license layer except the audited
# manifold_access runtime check. Match CODE refs (never prose).
if [ -f "$providers_file" ]; then
  hit="$(grep -nE '(\bentitlements[[:space:]]*\(|\bhas_harness\b|^[[:space:]]*(from[[:space:]]+app[[:space:]]+import[[:space:]].*\blicense\b|import[[:space:]]+app\.license)|\bapp\.license\b|\blicense\.[A-Za-z_])' \
    "$providers_file" 2>/dev/null | grep -vE '# *fitness:allow-manifold-entitlement' || true)"
  if [ -n "$hit" ]; then
    fail=1
    detail="${detail}     providers.py has an unapproved license dependency (visibility must remain edition-only):
$(printf '%s\n' "$hit" | sed 's/^/        /')
"
  fi

  marker_misuse="$(grep -nE '# *fitness:allow-manifold-entitlement' "$providers_file" 2>/dev/null \
    | grep -vE '(from app import license as lic_mod|has_advanced\("manifold_access"\))' || true)"
  if [ -n "$marker_misuse" ]; then
    fail=1
    detail="${detail}     manifold entitlement exception marker used outside its narrow contract:
$(printf '%s\n' "$marker_misuse" | sed 's/^/        /')
"
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "  ❌ $name — the provider lockdown leaked into the license layer:"
  printf '%s' "$detail"
  echo "     fix: keep provider visibility EDITION-gated; only the marked manifold_access"
  echo "          runtime credential check may reach app/license.entitlements."
  exit 1
fi

echo "  ✅ $name — visibility is edition-only; license reachability is limited to manifold_access runtime auth."
exit 0
