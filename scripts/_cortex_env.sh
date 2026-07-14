#!/usr/bin/env bash
# Cortex shell defaults for this Kaidera OS deployment. Do not store secrets here.

: "${CORTEX_PROJECT:?Set CORTEX_PROJECT from startup wizard output or deployment env before sourcing scripts/_cortex_env.sh}"
export CORTEX_API_URL="${CORTEX_API_URL:-http://localhost:8501}"
export CORTEX_ADMIN_TOKEN="${CORTEX_ADMIN_TOKEN:-cortex-local-admin}"
export PG_PORT="${PG_PORT:-5499}"
export PG_USER="${PG_USER:-postgres}"
export PG_PASS="${PG_PASS:-postgres}"
export PG_DB="${PG_DB:-platform_agent_memory}"
export REDIS_PORT="${REDIS_PORT:-6399}"
