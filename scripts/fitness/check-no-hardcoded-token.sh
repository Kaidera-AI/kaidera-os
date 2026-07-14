#!/usr/bin/env bash
# Fitness gate — the Cortex admin token must NEVER appear as a HARDCODED literal in tracked code.
#
# The token has exactly ONE home: local-cortex/.env (gitignored), read by BOTH the console
# (app/cortex_client.resolve_admin_token) and cortex-api (compose env_file). A quoted, high-entropy
# literal assigned to an admin-token variable in tracked code is a LEAKED credential — and it
# resurrects the very drift check-no-token-baking exists to prevent. This gate is the complement to
# that one: no-token-baking catches the `=$ADMIN_TOKEN` systemd/run-script bake; THIS catches a real
# token VALUE pasted straight into code/config. (Security backlog #138 — fail-closed + grep gate.)
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

# A token var (CORTEX_ADMIN_TOKEN / ADMIN_TOKEN) assigned a QUOTED 32+ char alnum/hex literal — a
# real token value (shell `=`, python `=`, or JSON `:`). This deliberately does NOT match:
#   * a $-interpolation (`=$ADMIN_TOKEN` — that is the bake gate's job),
#   * an empty string (`=""`), nor a bare name reference (`os.environ.get("CORTEX_ADMIN_TOKEN")`).
# Test fixtures are excluded (a fixture token is fine under tests/); a `# fitness:allow-literal`
# marker exempts a justified line; comment lines that merely name the variable are ignored.
PATTERN='(CORTEX_ADMIN_TOKEN|ADMIN_TOKEN)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][0-9A-Za-z]{32,}'

HITS="$(git grep -nIE "$PATTERN" -- . \
          ':(exclude)*/tests/*' ':(exclude)*test_*' ':(exclude)*conftest*' \
          ':(exclude)*/.venv/*' ':(exclude)*/node_modules/*' 2>/dev/null \
        | grep -vE '# *fitness:allow-literal' \
        | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' || true)"

if [ -n "$HITS" ]; then
  printf '  \033[1;31m❌ no-hardcoded-token — a hardcoded admin-token literal is in tracked code:\033[0m\n'
  printf '%s\n' "$HITS" | sed 's/^/       /'
  printf '     The token has ONE home: local-cortex/.env (gitignored). Remove the literal — the\n'
  printf '     console + cortex-api both read it from .env at runtime (resolve_admin_token / env_file).\n'
  exit 1
fi
printf '  \033[1;32m✅ no-hardcoded-token — no admin-token literal in tracked code (it lives only in .env)\033[0m\n'
