#!/bin/bash
# cortex-daily-checkpoint — daily ingestion + embedding catchup
#
# Purpose: prevent embedding-backlog accumulation. Runs cortex-sync-workspace
# (transcript ingest) + cortex-embed (catchup unembedded rows) on a daily
# cadence. The general pass keeps fresh L2 tables healthy; the bounded messages
# pass steadily burns down historical chat/message backlog without making the
# full provider-paced drain a release gate.
#
# Authored: 2026-05-03 by rex — addresses the embedding-backlog growth
# observed during the Pass 2 inventory audit (decisions 30%, lessons 47%,
# knowledge 4.6% embedded; backlog was 6826 rows).
#
# Run schedule: daily at 09:00 local via LaunchAgent or Beat.
#
# Output log: <workspace>/local-cortex/logs/cortex-daily-checkpoint.log
# (append-only; auto-rotates when >10K lines, keeping last 5K).
#
# Architecture note: this script lives in `local-cortex/` (workspace-root
# sibling to 02-cust-portal/) — local dev-team-only tooling kept SEPARATE
# from product-repo internals. Per CTO directive 2026-05-03 to keep the
# local Cortex demarcation clear from cust-portal product code.
# The workspace root is resolved from this script location by default so a
# Kaidera OS checkpoint cannot accidentally run against an old migration source.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LOCAL_CORTEX="${CORTEX_LOCAL_CORTEX_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd -P)}"
WORKSPACE="${CORTEX_WORKSPACE_ROOT:-$(cd "${LOCAL_CORTEX}/.." && pwd -P)}"
LOG="$LOCAL_CORTEX/logs/cortex-daily-checkpoint.log"

mkdir -p "$LOCAL_CORTEX/logs"

cd "$WORKSPACE" || { echo "ERR: workspace not found at $WORKSPACE" >> "$LOG"; exit 1; }

# Source env (provides OPENROUTER_API_KEY etc)
if [[ -f .agents/scripts/_cortex_env.sh ]]; then
    # shellcheck disable=SC1091
    source .agents/scripts/_cortex_env.sh
fi

export CORTEX_PROJECT="${CORTEX_PROJECT:-$(basename "${WORKSPACE}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]/-/g; s/^-*//; s/-*$//')}"
export CORTEX_WORKSPACE_ROOT="${CORTEX_WORKSPACE_ROOT:-${WORKSPACE}}"
export PATH="$WORKSPACE/.agents/scripts:$PATH:/opt/homebrew/opt/libpq/bin"

{
    echo ""
    echo "=== Cortex daily checkpoint $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
    echo "workspace: $WORKSPACE"
    echo "project: ${CORTEX_PROJECT}"
    echo ""

    # Health probe — exit fast if cortex-api is down
    health_code=$(curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:8501/health || echo "000")
    if [[ "$health_code" != "200" ]]; then
        echo "ABORT — cortex-api /health returned $health_code (expected 200). Skipping checkpoint."
        echo "=== END (aborted) ==="
        exit 1
    fi
    echo "cortex-api health: $health_code OK"

    # Step 1 — ingest any new Claude/Codex sessions on disk
    echo ""
    echo "--- cortex-sync-workspace --sessions-only ---"
    cortex-sync-workspace --sessions-only 2>&1 | tail -20

    # Step 2 — run cortex-ingest-all to pick up any new artifacts/transcripts
    echo ""
    echo "--- cortex-ingest-all (sessions + artifacts) ---"
    if command -v cortex-ingest-all >/dev/null 2>&1; then
        cortex-ingest-all 2>&1 | tail -10
    else
        echo "cortex-ingest-all not found — skipping"
    fi

    # Step 3 — burn down embedding backlog through cortex-api provider config
    echo ""
    echo "--- cortex-embed catch-up (all L2 tables via API) ---"
    cortex-embed --table all --limit 500 --chunk-size 50 --wait-timeout 900 2>&1 | tail -12 || true

    # Step 3b — historical messages are large and provider-paced; keep this
    # bounded so daily maintenance cannot monopolise the embedding provider.
    echo ""
    echo "--- cortex-embed historical messages catch-up ---"
    msg_limit="${CORTEX_MESSAGES_EMBED_CATCHUP_LIMIT:-1000}"
    msg_chunk="${CORTEX_MESSAGES_EMBED_CATCHUP_CHUNK:-50}"
    msg_timeout="${CORTEX_MESSAGES_EMBED_CATCHUP_TIMEOUT:-900}"
    echo "limit=${msg_limit} chunk=${msg_chunk} timeout=${msg_timeout}s"
    cortex-embed \
        --table messages \
        --limit "${msg_limit}" \
        --chunk-size "${msg_chunk}" \
        --async \
        --wait \
        --wait-timeout "${msg_timeout}" \
        2>&1 | tail -12 || true

    # Step 4 — work-product freshness from host-visible file hashes
    echo ""
    echo "--- cortex-work-product freshness ---"
    cortex-work-product --check-freshness --limit 100 --apply 2>&1 | tail -12 || true

    # Step 5 — final stats
    echo ""
    echo "--- cortex-embed --stats (post-checkpoint) ---"
    cortex-embed --stats 2>&1 | head -10

    # Step 6 — workspace path guardrail (advisory)
    echo ""
    echo "--- workspace path guardrail ---"
    if [[ -x "$WORKSPACE/scripts/check-workspace-paths.sh" ]]; then
        "$WORKSPACE/scripts/check-workspace-paths.sh" 2>&1 | tail -5
    fi

    echo ""
    echo "=== END $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
} >> "$LOG" 2>&1

# Tail rotation: keep last 5000 lines
if [[ -f "$LOG" ]]; then
    line_count=$(wc -l < "$LOG")
    if (( line_count > 10000 )); then
        tail -5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
fi
