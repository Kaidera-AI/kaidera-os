#!/usr/bin/env bash
# Fitness gate — the console must NEVER bake CORTEX_ADMIN_TOKEN into its systemd unit or run-script.
#
# The Cortex admin token has exactly ONE home: local-cortex/.env, read by BOTH the console
# (app/cortex_client.resolve_admin_token) and cortex-api (compose env_file). A second copy baked
# into the systemd `Environment=` line or the run-script `env` resurrects the drift that produced
# the recurring "Cortex didn't register the project — admin token configured" error (root-fixed in
# v0.1.88). This gate freezes that fix shut.
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

# Match an ACTUAL bake — `CORTEX_ADMIN_TOKEN=$ADMIN_TOKEN` / `="$ADMIN_TOKEN"` (assigning the
# generated token) — and ignore comment lines that merely MENTION the variable.
HITS="$(grep -nE 'CORTEX_ADMIN_TOKEN=["'"'"']?\$\{?ADMIN_TOKEN' install.sh 2>/dev/null \
        | grep -vE '^[0-9]+:[[:space:]]*#' || true)"

if [ -n "$HITS" ]; then
  printf '  \033[1;31m❌ no-token-baking — install.sh bakes CORTEX_ADMIN_TOKEN into the console env:\033[0m\n'
  printf '%s\n' "$HITS" | sed 's/^/       /'
  printf '     The token has ONE home: local-cortex/.env. Remove the bake — the console reads it via\n'
  printf '     resolve_admin_token() at request time, the same file cortex-api reads. (v0.1.88)\n'
  exit 1
fi
printf '  \033[1;32m✅ no-token-baking — CORTEX_ADMIN_TOKEN lives only in local-cortex/.env (one source, no drift)\033[0m\n'
