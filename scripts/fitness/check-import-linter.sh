#!/usr/bin/env bash
# GATE: the SDK's load-bearing layer rule — the domain core imports nothing outward.
# WHY: `app/domain/` is the pure functional core (Protocols + DTOs + value logic); the
# whole modular-monolith-as-platform-template bet (docs/sdk/README.md) rests on it staying
# pure — a LOCAL adapter (subprocess/httpx/asyncpg) can be swapped for a PLATFORM adapter
# with NO change to the modules, but only because the domain never reaches outward. This
# gate runs import-linter's Forbidden contract (local-cortex/console/.importlinter): if any
# app.domain submodule imports httpx / fastapi / starlette / subprocess / psycopg2 / asyncpg
# / app.adapters / app.main — directly OR indirectly — the contract breaks and the push is
# blocked. It complements the ast source-guards (tests/test_ports_purity.py) with a GRAPH-
# level check (catches indirect leaks + the app.adapters/app.main arrows). (WAY_OF_DEVELOPMENT §6)
#
# SCOPE: domain-purity only — NOT a full Layers contract (main.py is still the un-carved
# blob; a Layers rule would be red today). The module-isolation arrows get added to
# .importlinter as the modules carve (see the recipe in that file's footer + README §5).
#
# House contract (matches the sibling check-*.sh): prints one "  ✅/❌ <name> — <msg>"; may add
# indented detail lines; exits 0 (pass) / 1 (fail). run.sh auto-discovers it. (WAY_OF_DEVELOPMENT §6)
set -uo pipefail

# Resolve from THIS script's own location — never a hardcoded /Users/... (don't be our own
# violation; the no-project-literals gate would flag it anyway).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FITNESS_ROOT="${FITNESS_ROOT:-$(cd "$DIR/../.." && pwd)}"

name="import-linter"

# Overridable for hermetic testing (defaults to the real console + its .venv + shipped config):
#   IMPORT_LINTER_CONSOLE_DIR  the dir to run lint-imports from   (default the console)
#   IMPORT_LINTER_BIN          the lint-imports binary to invoke  (default the console .venv's)
#   IMPORT_LINTER_CONFIG       the contract file (relative to the console dir, or absolute)
CONSOLE_DIR="${IMPORT_LINTER_CONSOLE_DIR:-$FITNESS_ROOT/local-cortex/console}"
LINT_BIN="${IMPORT_LINTER_BIN:-$CONSOLE_DIR/.venv/bin/lint-imports}"
CONFIG="${IMPORT_LINTER_CONFIG:-.importlinter}"

# ── Preconditions: the console dir + the contract must exist. ─────────────────
if [ ! -d "$CONSOLE_DIR" ]; then
  echo "  ⏭  $name — no console dir ($CONSOLE_DIR), skipped"
  exit 0
fi
# Resolve the config to an absolute path for the existence check (it may be relative
# to the console dir, which is where we cd before running the linter).
case "$CONFIG" in
  /*) CONFIG_ABS="$CONFIG" ;;
  *)  CONFIG_ABS="$CONSOLE_DIR/$CONFIG" ;;
esac
if [ ! -f "$CONFIG_ABS" ]; then
  echo "  ❌ $name — contract file not found: $CONFIG_ABS (the domain-purity contract is missing)"
  exit 1
fi

# ── import-linter must be installed — NEVER silently skip (a skipped boundary gate is how
#    the boundary rots). A missing tool is a setup error: clear message, non-zero. ─────────
if [ ! -x "$LINT_BIN" ]; then
  # Fall back to a PATH-resolved lint-imports if the venv binary isn't where we expect,
  # so a differently-laid-out checkout still runs the gate rather than failing setup.
  if command -v lint-imports >/dev/null 2>&1; then
    LINT_BIN="lint-imports"
  else
    echo "  ❌ $name — import-linter not installed (expected $LINT_BIN)."
    echo "       install it:  .venv/bin/python -m pip install import-linter   (from $CONSOLE_DIR)"
    echo "       (it's pinned in local-cortex/console/requirements.txt — a skipped boundary gate is how the boundary rots)"
    exit 1
  fi
fi

# ── Run the contract from the console dir (so `root_package = app` resolves). ─────────────
# Capture both streams; import-linter prints the broken-contract detail to stdout.
out="$(cd "$CONSOLE_DIR" && "$LINT_BIN" --config "$CONFIG" 2>&1)"
rc=$?

if [ "$rc" -eq 0 ]; then
  echo "  ✅ $name — domain core imports nothing outward"
  exit 0
fi

# Non-zero → the contract broke (or the linter errored). Surface the offending import(s):
# import-linter prints lines like "-   app.domain.runstate -> httpx (l.190)" under
# "Broken contracts". Pull those arrows out for a crisp one-glance reason; fall back to the
# raw tail if the format ever changes.
echo "  ❌ $name — domain core reaches OUTWARD (the SDK layer rule is broken):"
offending="$(printf '%s\n' "$out" | grep -E '^[[:space:]]*-[[:space:]].*->' || true)"
if [ -n "$offending" ]; then
  printf '%s\n' "$offending" | sed 's/^[[:space:]]*-[[:space:]]*/       • /'
else
  # Couldn't parse arrows (e.g. a linter/config error) — show the tail so it's never opaque.
  printf '%s\n' "$out" | tail -n 12 | sed 's/^/       /'
fi
echo "       fix: keep app/domain/ pure (stdlib + intra-domain only); move the I/O into app/adapters/."
echo "       contract: local-cortex/console/.importlinter · run: (cd local-cortex/console && .venv/bin/lint-imports --config .importlinter)"
exit 1
