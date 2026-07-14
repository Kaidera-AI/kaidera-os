#!/usr/bin/env bash
# check-no-project-content-ships.sh — the SHIPPED product must carry NO external/customer
# identifiers. This scans the ACTUAL redist tree (`git archive`, export-ignore applied) — not the
# source tree — so it catches a leak that export-ignore should have stripped but didn't (for example,
# a customer handoff under .agents/memory/imports/external/ or a customer URL baked into an installer).
#
# DENYLIST = strings with NO legitimate use in a generic Kaidera OS build: another project's domain,
# an external project's name, a specific customer deployment URL. (Generic words like worker/
# marketing are NOT denylisted — they appear in legitimate explanatory comments and are excluded at
# the tree level by .gitattributes; this gate targets only never-legitimate customer/external text.)
#
# Contract: prints "  ✅/❌ <name> — <msg>"; exits 0 (pass) / 1 (fail).
set -uo pipefail
NAME="no-project-content-ships"
# Optional customer DOMAINS / deployment URLs — never legitimate in a generic
# build. The generic product does not bake customer-specific deny patterns; a
# deployment can supply them when auditing a private release branch.
DENY="${KAIDERA_OS_FORBIDDEN_CUSTOMER_PATTERNS:-}"

ROOT="${FITNESS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT" || { echo "  ❌ $NAME — cannot cd to repo root"; exit 1; }

# Archive the to-be-committed state (working tree via `stash create`, else HEAD) WITH worktree
# .gitattributes so an uncommitted export-ignore edit is honoured.
ref="$(git stash create 2>/dev/null || true)"; ref="${ref:-HEAD}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
git archive --worktree-attributes "$ref" 2>/dev/null | tar -x -C "$tmp" 2>/dev/null \
  || { echo "  ❌ $NAME — could not build the redist archive"; exit 1; }

if [ -z "$DENY" ]; then
  echo "  ✅ $NAME — no deployment-specific denylist configured"
  exit 0
fi

hits="$(grep -rilE "$DENY" "$tmp" 2>/dev/null | sed "s#$tmp/##" | grep -vE 'node_modules' || true)"
if [ -n "$hits" ]; then
  echo "  ❌ $NAME — the shipped redist carries external/customer identifiers:"
  printf '%s\n' "$hits" | sed 's/^/       • /'
  echo "     fix: genericize the string, or export-ignore the file in .gitattributes."
  exit 1
fi
echo "  ✅ $NAME — no external/customer identifiers in the shipped redist"
exit 0
