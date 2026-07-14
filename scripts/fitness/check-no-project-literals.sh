#!/usr/bin/env bash
# GATE: no per-project literals baked into the harness code (the SDK-realignment ratchet).
# WHY: the harness is meant to be a drop-in, config-driven SDK — "Normal paths must not
# hardcode project keys, hexes, roots, default agents, rosters, Beat IDs, or launchd labels"
# (ARCHITECTURE.md:24; E75 Inc18). The code drifted: rosters, hexes, agent names, and personal
# paths are baked as defaults/fallbacks that shadow the config-as-data path and break drop-in
# on any other project/host. This gate is the keystone of the realignment plan
# (docs/2026-06-05-sdk-realignment-audit-and-plan.md §4) — the one structural piece that
# mechanically forces the rest to be fixed and KEEPS them fixed.
#
# RATCHET (not an absolute ban): a baseline file pins today's known per-file counts. The gate
# FAILS only when a file EXCEEDS its baseline or a brand-new offending file appears — green at
# baseline, red on anything NEW. Each realignment phase shrinks the baseline; an empty baseline
# means fully enforcing. Offending lines are printed for visibility; pass/fail is the COUNT.
#
# House contract (matches the sibling check-*.sh): prints one "  ✅/❌ <name> — <msg>"; may add
# indented detail lines; exits 0 (pass) / 1 (fail). run.sh auto-discovers it. (WAY_OF_DEVELOPMENT §6)
set -uo pipefail

# Resolve from THIS script's own location — never a hardcoded /Users/... (don't be our own violation).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FITNESS_ROOT="${FITNESS_ROOT:-$(cd "$DIR/../.." && pwd)}"

name="no-project-literals"

# Overridable for hermetic testing (defaults to the real repo + shipped baseline):
#   FITNESS_SCAN_ROOT      root to scan                 (default the repo root)
#   FITNESS_BASELINE_FILE  baseline file to compare to  (default the shipped baseline)
SCAN_ROOT="${FITNESS_SCAN_ROOT:-$FITNESS_ROOT}"
BASELINE_FILE="${FITNESS_BASELINE_FILE:-$DIR/.project-literals-baseline}"

# A flag to (re)generate the baseline from the current scan: `--write-baseline`.
WRITE_BASELINE=0
[ "${1:-}" = "--write-baseline" ] && WRITE_BASELINE=1

# ── Deny patterns (POSIX ERE only — BSD /usr/bin/grep has no -P). ─────────────
# Manual word boundaries via [^0-9A-Za-z_] so a token only matches as a whole word,
# never as a substring.
NB='[^0-9A-Za-z_-]'   # a non-token char; excludes '-' so dashed project keys stay one token
LB="(^|$NB)"          # left boundary
RB="($NB|\$)"         # right boundary  (\$ = literal end-of-line in ERE)

PROJECT_KEYS="${FITNESS_PROJECT_KEYS:-}"
HEXES="${FITNESS_PROJECT_HEXES:-}"
AGENT_NAMES="${FITNESS_AGENT_NAMES:-}"

# Project keys / agent names count only as STRING LITERALS or shell CASE LABELS, so ordinary
# prose/identifiers don't trip the gate. A quote on either side, or a `token)` case label.
Q="[\"']"
# personal paths: macOS user roots, repo-vault names, pinned framework python,
# or Homebrew inside a PATH=.
PAT_PATHS="(/U[s]ers/[^/\"' ]+|D[e]vVault|Python\\.framework/Versions/[0-9.]+|PATH=.*/opt/homebrew|PATH .*/opt/homebrew)"

DENY="$PAT_PATHS"
if [ -n "$PROJECT_KEYS" ]; then
  PAT_PROJECT="(${Q}(${PROJECT_KEYS})|(${PROJECT_KEYS})${Q}|${LB}(${PROJECT_KEYS})\\)|\\|(${PROJECT_KEYS})\\))"
  DENY="(${DENY}|${PAT_PROJECT})"
fi
if [ -n "$AGENT_NAMES" ]; then
  PAT_AGENT="(${Q}(${AGENT_NAMES})|(${AGENT_NAMES})${Q}|${LB}(${AGENT_NAMES})\\)|\\|(${AGENT_NAMES})\\))"
  DENY="(${DENY}|${PAT_AGENT})"
fi
if [ -n "$HEXES" ]; then
  PAT_HEX="(${Q}(${HEXES})${Q}|${LB}(${HEXES})${RB}.*[Hh]ex|[Hh]ex.*${LB}(${HEXES})${RB})"
  DENY="(${DENY}|${PAT_HEX})"
fi

# ── Collect in-scope files (relative paths), honoring the allow-list. ─────────
# Scope: console app *.py, console scripts/*, beat **/*.{sh,py}, .agents/scripts/cortex-*.
collect_files() {
  {
    [ -d "$SCAN_ROOT/local-cortex/console/app" ] &&
      find "$SCAN_ROOT/local-cortex/console/app" -type f -name '*.py' 2>/dev/null
    [ -d "$SCAN_ROOT/local-cortex/console/scripts" ] &&
      find "$SCAN_ROOT/local-cortex/console/scripts" -type f 2>/dev/null
    [ -d "$SCAN_ROOT/beat" ] &&
      find "$SCAN_ROOT/beat" -type f \( -name '*.sh' -o -name '*.py' \) 2>/dev/null
    [ -d "$SCAN_ROOT/.agents/scripts" ] &&
      find "$SCAN_ROOT/.agents/scripts" -type f -name 'cortex-*' 2>/dev/null
  } | sed "s#^$SCAN_ROOT/##" \
    | grep -vE '(^|/)__pycache__/|\.pyc$' \
    | sort -u
}

# An allowed path is config-as-data / a fixture / the gate's own machinery — never flagged.
is_allowed() {
  case "$1" in
    *.project.json) return 0 ;;                 # config-as-data contract examples
    *.env.example)  return 0 ;;                 # env templates
    */tests/*|tests/*) return 0 ;;              # test fixtures
    scripts/fitness/.project-literals-baseline) return 0 ;;
    scripts/fitness/check-no-project-literals.sh) return 0 ;;  # the gate itself
  esac
  return 1
}

# Count offending lines in one file, dropping any line carrying the inline escape.
count_hits() {
  grep -nE "$DENY" "$SCAN_ROOT/$1" 2>/dev/null \
    | grep -vE '# *fitness:allow-literal' \
    || true
}

# ── Build the current per-file counts (and remember offending lines for output). ─
current_counts=""   # lines of "count<TAB>relpath"
offenders=""        # human-readable offending lines, for visibility
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  is_allowed "$rel" && continue
  lines="$(count_hits "$rel")"
  [ -n "$lines" ] || continue
  c="$(printf '%s\n' "$lines" | grep -c '' )"
  current_counts="${current_counts}${c}	${rel}
"
  offenders="${offenders}$(printf '%s\n' "$lines" | sed "s#^#       $rel:#")
"
done <<EOF
$(collect_files)
EOF

# ── --write-baseline: emit the sorted "count<TAB>relpath" baseline and exit. ──
if [ "$WRITE_BASELINE" -eq 1 ]; then
  printf '%s' "$current_counts" | sed '/^$/d' | sort -k2 > "$BASELINE_FILE"
  total="$(printf '%s' "$current_counts" | sed '/^$/d' | grep -c '' )"
  echo "  ✅ $name — wrote baseline ($total file(s)) to $BASELINE_FILE"
  exit 0
fi

# ── Compare current counts against the baseline. ─────────────────────────────
baseline_for() {  # baseline count for a relpath (0 if absent/missing file)
  [ -f "$BASELINE_FILE" ] || { echo 0; return; }
  awk -F'\t' -v f="$1" '$2==f {print $1; found=1} END {if(!found) print 0}' "$BASELINE_FILE" | head -1
}

violations=""
while IFS=$'\t' read -r c rel; do
  [ -n "${rel:-}" ] || continue
  b="$(baseline_for "$rel")"
  if [ "$c" -gt "$b" ]; then
    violations="${violations}  • $rel — $c literal(s), baseline allows $b
"
  fi
done <<EOF
$(printf '%s' "$current_counts" | sed '/^$/d')
EOF

if [ -n "$violations" ]; then
  echo "  ❌ $name — NEW project literals exceed baseline (the harness must stay config-driven):"
  printf '%s' "$violations" | sed 's/^/     /'
  echo "     offending lines:"
  printf '%s' "$offenders" | sed '/^$/d'
  echo "     fix: drive it from config/profile, or (rare, justified) add '# fitness:allow-literal <reason>'."
  echo "     to re-pin after a deliberate baseline change: bash scripts/fitness/check-no-project-literals.sh --write-baseline"
  exit 1
fi

# Count baselined files for the green message (quantifies remaining SDK drift).
baseline_files=0
[ -f "$BASELINE_FILE" ] && baseline_files="$(sed '/^$/d' "$BASELINE_FILE" 2>/dev/null | grep -c '' )"
echo "  ✅ $name — no new project literals (baseline: ${baseline_files} file(s) of known drift, shrinking per phase)"
exit 0
