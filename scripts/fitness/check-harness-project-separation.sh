#!/usr/bin/env bash
# GATE: project content/modules/imports must never (re-)enter the harness tree.
# WHY: the harness app is ONE clean, project-agnostic codebase — it carries
# no project content and ships identically to everyone; each project is its OWN codebase, kept apart
# by a separate repo or a dedicated branch. The test: delete every project and the harness still
# builds + runs; the harness never imports project code (THE_WAY_OF_DEVELOPMENT.md §2.9 + §8.1).
# This is the structural enforcement that stops the harness re-entangling with project content as we
# add projects (the harness-runtime half of the backlog's `check-product-boundary`).
#
# This gate is NOT about project-literal STRINGS — `check-no-project-literals` covers baked-in
# literals (project keys, hexes, agent names, Mac paths). This one is about STRUCTURE: whole project
# TREES, project-NAMED modules, and real harness→project IMPORTS.
#
# It detects FOUR classes of offender inside the harness tree (local-cortex/console + local-cortex/*):
#   1. Project trees      — a dir under local-cortex/ that looks like a self-contained turnkey
#                           (has BOTH a config-schema/ or projects/ subdir AND a connectors/ or
#                           playbook/ subdir).
#   2. Project modules     — console files named for a project package.
#   3. Harness→project import — any local-cortex/console/app/**.py that actually imports a project
#                           package. Prose/docstring mentions are NOT imports and do NOT count.
#                           Today: NONE (this locks it clean).
#   4. Hardcoded AI Worker / persona NAMES — a console/app/**.py line with a worker/persona name as
#                           a QUOTED string literal or a shell-style `name)` case label. The harness is a PURE RUNTIME
#                           and names NO worker — "agents" are AI Workers owned by the running PROJECT
#                           (CTO 2026-06-18). A runtime literal/label that bakes in a worker name is
#                           project content; the autonomy loop must read its workers from the Cortex
#                           registry/role policy, never a hardcoded name. Reported as `worker-literal:
#                           <file>:<line>` and baselined like the rest (legitimately-remaining ones —
#                           e.g. a project module's own name —
#                           are pinned). A `# fitness:allow-literal` line is exempt (genuine product
#                           identifier). NOTE: check-no-project-literals also counts these as STRINGS;
#                           this gate pins them with file:line precision as a STRUCTURE offender class
#                           so a NEW hardcoded worker label fails even if the per-file count gate has
#                           headroom.
#
# RATCHET (not an absolute ban): a baseline file pins today's KNOWN offenders. The gate PASSES while
# the found offenders are a subset of the baseline, and FAILS the moment a NEW offender appears.
# Each separation step shrinks the baseline; an empty baseline means fully separated.
#
# House contract (matches the sibling check-*.sh): prints one "  ✅/❌ <name> — <msg>"; may add
# indented detail lines; exits 0 (pass) / 1 (fail). run.sh auto-discovers it. (WAY_OF_DEVELOPMENT §6)
set -uo pipefail

# Resolve from THIS script's own location — never a hardcoded /Users/... (don't be our own violation).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FITNESS_ROOT="${FITNESS_ROOT:-$(cd "$DIR/../.." && pwd)}"

name="harness-project-separation"

# Overridable for hermetic testing (defaults to the real repo + shipped baseline):
#   FITNESS_SCAN_ROOT      root to scan                 (default the repo root)
#   FITNESS_BASELINE_FILE  baseline file to compare to  (default the shipped baseline)
SCAN_ROOT="${FITNESS_SCAN_ROOT:-$FITNESS_ROOT}"
BASELINE_FILE="${FITNESS_BASELINE_FILE:-$DIR/baselines/harness-project-separation.txt}"

# A flag to (re)generate the baseline from the current scan: `--write-baseline`.
WRITE_BASELINE=0
[ "${1:-}" = "--write-baseline" ] && WRITE_BASELINE=1

# Project-named module prefixes (extensible). A console file named "<prefix>_*" is project content.
# Provide deployment-specific policy through FITNESS_PROJECT_MODULE_PREFIXES.
PROJECT_MODULE_PREFIXES="${FITNESS_PROJECT_MODULE_PREFIXES:-}"

# AI Worker / persona names that must NEVER be baked into the harness runtime as a literal/label
# (Class 4). Extensible; alternation for POSIX ERE. The harness names no worker (§ pure-runtime) —
# these come from the running project's Cortex registry/role policy, never a hardcoded string.
WORKER_NAMES="${FITNESS_WORKER_NAMES:-}"

# ── Collect the current offenders (relative offender ids, one per line). ─────────────
collect_offenders() {
  local cdir="$SCAN_ROOT/local-cortex"

  # Class 1 — project trees: a dir directly under local-cortex/ that looks turnkey.
  # Heuristic: (config-schema/ OR projects/) AND (connectors/ OR playbook/).
  if [ -d "$cdir" ]; then
    local d rel
    for d in "$cdir"/*/; do
      [ -d "$d" ] || continue
      if { [ -d "${d}config-schema" ] || [ -d "${d}projects" ]; } &&
         { [ -d "${d}connectors" ] || [ -d "${d}playbook" ]; }; then
        rel="${d#"$SCAN_ROOT"/}"; rel="${rel%/}"
        printf '%s\n' "$rel"
      fi
    done
  fi

  # Class 2 — project-named modules in the console.
  local app="$SCAN_ROOT/local-cortex/console/app"
  if [ -d "$app" ]; then
    local pfx f rel
    [ -n "$PROJECT_MODULE_PREFIXES" ] || return 0
    for pfx in $PROJECT_MODULE_PREFIXES; do
      # app/<pfx>_*.py
      for f in "$app/${pfx}_"*.py; do
        [ -e "$f" ] || continue
        rel="${f#"$SCAN_ROOT"/}"; printf '%s\n' "$rel"
      done
      # app/templates/<pfx>_*.html
      for f in "$app/templates/${pfx}_"*.html; do
        [ -e "$f" ] || continue
        rel="${f#"$SCAN_ROOT"/}"; printf '%s\n' "$rel"
      done
    done
  fi

  # Class 3 — harness→project imports: a REAL python import of a project package in console/app.
  # POSIX ERE (BSD /usr/bin/grep has no -P). Match only import statements, never prose/docstrings:
  #   ^\s*from <pkg> import ...   |   ^\s*from <pkg.sub> import ...   |   ^\s*import <pkg>...
  # where <pkg> is a known project package (configured by env; extensible).
  if [ -d "$app" ]; then
    local proj_pkgs="${FITNESS_PROJECT_PACKAGE_IMPORTS:-sample_project_pkg|sample-project-pkg}"
    local hits
    hits="$(grep -rnE \
      "^[[:space:]]*(from[[:space:]]+(${proj_pkgs})([._][A-Za-z0-9_]*)*[[:space:]]+import[[:space:]]|import[[:space:]]+(${proj_pkgs})($|[[:space:]]|[.,]))" \
      "$app" --include='*.py' 2>/dev/null || true)"
    if [ -n "$hits" ]; then
      printf '%s\n' "$hits" | sed "s#^$SCAN_ROOT/#import: #; s#:[0-9].*##"
    fi
  fi

  # Class 4 — hardcoded AI Worker / persona names in console/app/**.py. Match a worker name only
  # as a QUOTED literal or a shell-style case label, so ordinary
  # identifiers/prose tokens never trip it. A `# fitness:allow-literal` line is exempt. The offender
  # is the FILE (`worker-literal: <relpath>`) — set-membership, stable against line-number shifts and
  # matching the gate's ratchet model. (Per-line drift WITHIN an already-flagged file is caught by the
  # sibling count gate check-no-project-literals; this gate fails the moment a worker name enters a
  # currently-CLEAN console/app file.)
  if [ -d "$app" ]; then
    local wfiles
    [ -n "$WORKER_NAMES" ] || return 0
    wfiles="$(grep -rlE \
      "([\"'](${WORKER_NAMES})[\"']|(^|[^0-9A-Za-z_-])(${WORKER_NAMES})\\)|\\|(${WORKER_NAMES})\\))" \
      "$app" --include='*.py' 2>/dev/null \
      | grep -vE '/__pycache__/' || true)"
    # grep -l can't honor the per-line allow-literal escape, so re-confirm each file has at least one
    # NON-exempt match before flagging it (a file whose every worker-name line is allow-literal'd is clean).
    if [ -n "$wfiles" ]; then
      local wf
      while IFS= read -r wf; do
        [ -n "$wf" ] || continue
        if grep -nE \
          "([\"'](${WORKER_NAMES})[\"']|(^|[^0-9A-Za-z_-])(${WORKER_NAMES})\\)|\\|(${WORKER_NAMES})\\))" \
          "$wf" 2>/dev/null | grep -vqE '# *fitness:allow-literal'; then
          printf 'worker-literal: %s\n' "${wf#"$SCAN_ROOT"/}"
        fi
      done <<EOF
$wfiles
EOF
    fi
  fi
}

current_offenders="$(collect_offenders | grep -v '^[[:space:]]*$' | sort -u)"

# ── --write-baseline: emit the sorted offender list (with a header) and exit. ───────
if [ "$WRITE_BASELINE" -eq 1 ]; then
  {
    echo "# Baseline for check-harness-project-separation.sh — regenerated $(date +%Y-%m-%d)."
    echo "# One offender token per line; #-comments and blanks ignored. Target: empty (fully separated)."
    printf '%s\n' "$current_offenders"
  } > "$BASELINE_FILE"
  total="$(printf '%s\n' "$current_offenders" | grep -c '^[^#[:space:]]' || true)"
  echo "  ✅ $name — wrote baseline ($total offender(s)) to $BASELINE_FILE"
  exit 0
fi

# ── Load the baselined offender tokens (strip #-comments + blanks). ─────────────────
baselined_tokens=""
if [ -f "$BASELINE_FILE" ]; then
  baselined_tokens="$(grep -vE '^[[:space:]]*(#|$)' "$BASELINE_FILE" 2>/dev/null | sed 's/[[:space:]]*$//' || true)"
fi
is_baselined() {  # exact-match a token against the baseline set
  printf '%s\n' "$baselined_tokens" | grep -Fxq -- "$1"
}

# ── Walk the found offenders: classify each as baselined or NEW. ─────────────────────
new_offenders=""
known_count=0
report=""
while IFS= read -r off; do
  [ -n "$off" ] || continue
  if is_baselined "$off"; then
    known_count=$((known_count + 1))
    report="${report}       • $off  [baselined]
"
  else
    new_offenders="${new_offenders}$off
"
    report="${report}       • $off  [NEW — not baselined]
"
  fi
done <<EOF
$current_offenders
EOF

# ── Verdict. ─────────────────────────────────────────────────────────────────────────
if [ -n "$new_offenders" ]; then
  echo "  ❌ $name — NEW project content in the harness (it must stay project-agnostic, §2.9/§8.1):"
  [ -n "$report" ] && printf '%s' "$report"
  echo "     new offender(s):"
  printf '%s' "$new_offenders" | grep -v '^[[:space:]]*$' | sed 's/^/        - /'
  echo "     fix: keep the harness clean — a project belongs in its OWN repo/branch, installed via the"
  echo "          §8 public surfaces; the harness never imports project code (THE_WAY_OF_DEVELOPMENT.md §8.1)."
  echo "     note: project-literal STRINGS are covered by check-no-project-literals — this gate is structure."
  echo "     to re-pin after a deliberate change: bash scripts/fitness/check-harness-project-separation.sh --write-baseline"
  exit 1
fi

# Green: all found offenders are baselined (or there are none). Print the ratchet line.
if [ -n "$report" ]; then
  echo "  ✅ $name — no NEW project content in the harness (all $known_count offender(s) baselined):"
  printf '%s' "$report"
else
  echo "  ✅ $name — no project content in the harness."
fi
echo "     RATCHET: $known_count known offender(s) remaining to remove (target 0) — harness fully project-agnostic."
exit 0
