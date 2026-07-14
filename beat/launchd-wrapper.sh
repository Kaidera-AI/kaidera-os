#!/usr/bin/env bash
# LaunchAgent entry point for Beat's 25-minute heartbeat.
#
# launchd does not inherit the operator shell environment. Source the canonical
# local Cortex .env file before invoking Python so provider keys and the rest of
# the Cortex config have one source of truth.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=/dev/null
source "${ROOT}/beat/source-cortex-env.sh"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "${PYTHON_BIN}" ]; then
    PYTHON_BIN="/usr/bin/python3"
fi

PATH="${ROOT}/.agents/scripts:${HOME}/.local/bin:$(dirname "${PYTHON_BIN}"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}" # fitness:allow-literal standard operator PATH
export PATH
if [ -f "${ROOT}/beat/runtime-profile.py" ]; then
    eval "$("${PYTHON_BIN}" "${ROOT}/beat/runtime-profile.py" --shell --root "${ROOT}" 2>/dev/null || true)"
fi

if [ -z "${CORTEX_PROJECT:-}" ]; then
    printf 'launchd-wrapper: ERROR CORTEX_PROJECT is required; Kaidera OS will not guess a project key.\n' >&2
    exit 67
fi
export CORTEX_PROJECT
export CORTEX_API_URL="${CORTEX_API_URL:-http://localhost:8501}"
if [ -z "${CORTEX_ADMIN_TOKEN:-}" ]; then
    printf 'launchd-wrapper: ERROR CORTEX_ADMIN_TOKEN missing from %s\n' "${CORTEX_KEYS_FILE:-local-cortex/.env}" >&2
    exit 78
fi
export CORTEX_ADMIN_TOKEN
export BEAT_CORTEX_AGENT="${BEAT_CORTEX_AGENT:-beat@${CORTEX_PROJECT}}"
export CORTEX_WORKSPACE_ROOT="${CORTEX_WORKSPACE_ROOT:-${ROOT}}"
export BEAT_EMBED_TIMEOUT="${BEAT_EMBED_TIMEOUT:-45}"
export BEAT_CHAT_DUMP_TIMEOUT="${BEAT_CHAT_DUMP_TIMEOUT:-20}"
export BEAT_INGEST_COMMAND_TIMEOUT="${BEAT_INGEST_COMMAND_TIMEOUT:-90}"
export BEAT_GRAPH_COMMAND_TIMEOUT="${BEAT_GRAPH_COMMAND_TIMEOUT:-30}"
export BEAT_ENTITY_TIMEOUT="${BEAT_ENTITY_TIMEOUT:-30}"
export BEAT_MEMORY_AUDIT_TIMEOUT="${BEAT_MEMORY_AUDIT_TIMEOUT:-30}"
export BEAT_ENTITY_LIMIT="${BEAT_ENTITY_LIMIT:-5}"
export BEAT_EMBED_LIMIT="${BEAT_EMBED_LIMIT:-5}"
export BEAT_MEMORY_EMBED_LIMIT="${BEAT_MEMORY_EMBED_LIMIT:-10}"
export BEAT_AGENT_INGEST_BATCH_SIZE="${BEAT_AGENT_INGEST_BATCH_SIZE:-2}"
export BEAT_ACTION_RETRIES="${BEAT_ACTION_RETRIES:-0}"
export BEAT_LAUNCHD_LIGHT_CRON="${BEAT_LAUNCHD_LIGHT_CRON:-0}"
export CORTEX_ENTITY_TIMEOUT_SECONDS="${CORTEX_ENTITY_TIMEOUT_SECONDS:-6}"

cd "${ROOT}"

if [ -n "${OPENROUTER_API_KEY:-}" ]; then
    printf 'launchd-wrapper: OPENROUTER_API_KEY present from %s\n' "${CORTEX_KEYS_FILE}"
else
    printf 'launchd-wrapper: WARNING OPENROUTER_API_KEY missing from %s; embed_catchup will skip gracefully\n' "${CORTEX_KEYS_FILE}"
fi

if [ -z "${KAIDERA_OS_BEAT_ACTIONS_SCRIPT:-}" ]; then
    printf 'launchd-wrapper: no KAIDERA_OS_BEAT_ACTIONS_SCRIPT configured; heartbeat is a no-op.\n'
    exit 0
fi

exec "${PYTHON_BIN}" \
    "${KAIDERA_OS_BEAT_ACTIONS_SCRIPT}" once --source launchd
