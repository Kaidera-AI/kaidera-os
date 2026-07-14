#!/usr/bin/env bash
# GATE: named package content must not enter Kaidera OS core runtime/redist source.
#
# Kaidera OS is the harness. Domain packages run on it. This gate fails
# package-specific markers in source paths that ship or run as core.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FITNESS_ROOT="${FITNESS_ROOT:-$(cd "$DIR/../.." && pwd)}"

NAME="package-boundary"
SCAN_ROOT="${FITNESS_SCAN_ROOT:-$FITNESS_ROOT}"
BASELINE_FILE="${FITNESS_BASELINE_FILE:-$DIR/baselines/package-boundary.txt}"
DEFAULT_MARKERS="samplepkg|customerpkg|devsuitepkg"
MARKERS_FILE="${FITNESS_PACKAGE_MARKERS_FILE:-$DIR/local/package-boundary-markers.txt}"
SCAN_PATHS="${FITNESS_PACKAGE_SCAN_PATHS:-local-cortex/console/app local-cortex/console/spa/src redistributable install.sh}"
WRITE_BASELINE=0
[ "${1:-}" = "--write-baseline" ] && WRITE_BASELINE=1

load_markers_file() {
  local file="$1"
  awk '
    /^[[:space:]]*(#|$)/ { next }
    {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
      if ($0 != "") print $0
    }
  ' "$file" 2>/dev/null | paste -sd '|' -
}

if [ -n "${FITNESS_PACKAGE_MARKERS:-}" ]; then
  MARKERS="$FITNESS_PACKAGE_MARKERS"
elif [ -f "$MARKERS_FILE" ]; then
  MARKERS="$(load_markers_file "$MARKERS_FILE")"
  [ -n "$MARKERS" ] || MARKERS="$DEFAULT_MARKERS"
else
  MARKERS="$DEFAULT_MARKERS"
fi

# Underscore is a delimiter for path/module names, but ordinary alphanumerics
# are not. This catches samplepkg_worker while avoiding false hits inside message.
BOUNDARY="(^|[^A-Za-z0-9-])(${MARKERS})([^A-Za-z0-9-]|\$)"

collect_files() {
  local path abs
  for path in $SCAN_PATHS; do
    abs="$SCAN_ROOT/$path"
    if [ -f "$abs" ]; then
      printf '%s\n' "$abs"
    elif [ -d "$abs" ]; then
      find "$abs" -type f 2>/dev/null
    fi
  done \
    | sed "s#^$SCAN_ROOT/##" \
    | grep -vE '(^|/)(__pycache__|\.pytest_cache|node_modules)/|\.pyc$|\.pyo$' \
    | grep -vE '(^|/)tests?/|\.test\.[A-Za-z0-9]+$|\.spec\.[A-Za-z0-9]+$' \
    | grep -vE '^local-cortex/console/app/static/|^local-cortex/console/spa/dist/' \
    | sort -u
}

file_has_marker() {
  local rel="$1"
  printf '%s\n' "$rel" | grep -Eiq "$BOUNDARY" && return 0
  grep -Iq . "$SCAN_ROOT/$rel" 2>/dev/null || return 1
  grep -IiqE "$BOUNDARY" "$SCAN_ROOT/$rel" 2>/dev/null
}

current_offenders=""
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  if file_has_marker "$rel"; then
    current_offenders="${current_offenders}${rel}
"
  fi
done <<EOF
$(collect_files)
EOF
current_offenders="$(printf '%s\n' "$current_offenders" | sed '/^[[:space:]]*$/d' | sort -u)"

if [ "$WRITE_BASELINE" -eq 1 ]; then
  {
    echo "# Baseline for check-package-boundary.sh."
    echo "# One known package-proof file per line."
    echo "# Target: empty. Package payloads live in Kaidera AI turnkey project repos."
    printf '%s\n' "$current_offenders"
  } > "$BASELINE_FILE"
  count="$(printf '%s\n' "$current_offenders" | sed '/^[[:space:]]*$/d' | grep -c '' || true)"
  echo "  ✅ $NAME — wrote baseline ($count file(s)) to $BASELINE_FILE"
  exit 0
fi

baseline=""
if [ -f "$BASELINE_FILE" ]; then
  baseline="$(grep -vE '^[[:space:]]*(#|$)' "$BASELINE_FILE" 2>/dev/null | sed 's/[[:space:]]*$//' || true)"
fi

is_baselined() {
  printf '%s\n' "$baseline" | grep -Fxq -- "$1"
}

new=""
known=0
report=""
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  if is_baselined "$rel"; then
    known=$((known + 1))
  else
    new="${new}${rel}
"
    report="${report}       • ${rel}
"
  fi
done <<EOF
$current_offenders
EOF

if [ -n "$new" ]; then
  echo "  ❌ $NAME — package-specific content entered Kaidera OS core/runtime source:"
  printf '%s' "$report"
  echo "     fix: move the content into a project pack/package, or mark it as a non-shipping example/test."
  echo "     to re-pin after deliberate extraction work: bash scripts/fitness/check-package-boundary.sh --write-baseline"
  exit 1
fi

if [ "$known" -gt 0 ]; then
  echo "  ✅ $NAME — no NEW package-specific content (baseline: $known known proof file(s), target 0)"
else
  echo "  ✅ $NAME — no package-specific content in core runtime/redist source"
fi
exit 0
