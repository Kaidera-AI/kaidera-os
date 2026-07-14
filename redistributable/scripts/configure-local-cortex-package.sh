#!/usr/bin/env bash
# Configure an extracted local Cortex redistributable on this Mac.
#
# This script renders machine-local files that must contain absolute paths:
# - ~/Library/LaunchAgents/<Cortex runtime Beat label>.plist
# - ~/.local/bin/cortex and cortex-session-start dispatchers

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  configure-local-cortex-package.sh [PROJECT_ROOT] [--customer-only]

Defaults:
  PROJECT_ROOT       $CORTEX_PROJECT_ROOT, or current working directory
  CORTEX_HOME_BIN    $HOME/.local/bin
  PYTHON_BIN         first python3 on PATH

The generated LaunchAgent is installed but not loaded. Start it with:
  launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/<runtime-label>.plist"
EOF
}

PROJECT_ROOT="${CORTEX_PROJECT_ROOT:-$(pwd -P)}"
CUSTOMER_ONLY=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --customer-only)
            CUSTOMER_ONLY=1
            shift
            ;;
        --*)
            printf 'ERROR: unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
        *)
            PROJECT_ROOT="$1"
            shift
            ;;
    esac
done

PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd -P)"
LOCAL_BIN="${CORTEX_HOME_BIN:-${HOME}/.local/bin}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
if [ -z "$PYTHON_BIN" ]; then
    PYTHON_BIN="/usr/bin/python3"
fi

require_file() {
    if [ ! -f "$1" ]; then
        printf 'ERROR: missing required file: %s\n' "$1" >&2
        exit 1
    fi
}

require_file "$PROJECT_ROOT/beat/ai.kaidera.kaidera-os.beat.plist.template"
require_file "$PROJECT_ROOT/beat/launchd-wrapper.sh"
require_file "$PROJECT_ROOT/.local/bin/cortex"
require_file "$PROJECT_ROOT/.agents/docker-compose.cortex.yml"
require_file "$PROJECT_ROOT/redistributable/scripts/install-herdr-runtime.sh"

if [ ! -x "$PROJECT_ROOT/redistributable/scripts/install-herdr-runtime.sh" ]; then
    printf 'ERROR: Herdr installer helper is not executable: %s\n' "$PROJECT_ROOT/redistributable/scripts/install-herdr-runtime.sh" >&2
    exit 1
fi
HERDR_BIN="$("$PROJECT_ROOT/redistributable/scripts/install-herdr-runtime.sh" --print-path)"

mkdir -p "$PROJECT_ROOT/beat/logs" "$HOME/Library/LaunchAgents" "$LOCAL_BIN"

install -m 0755 "$PROJECT_ROOT/.local/bin/cortex" "$LOCAL_BIN/cortex"
if [ -f "$PROJECT_ROOT/.local/bin/cortex-session-start" ]; then
    install -m 0755 "$PROJECT_ROOT/.local/bin/cortex-session-start" "$LOCAL_BIN/cortex-session-start"
fi

"$PYTHON_BIN" - "$PROJECT_ROOT" "$HOME" "$LOCAL_BIN" "$PYTHON_BIN" "$CUSTOMER_ONLY" "$HERDR_BIN" <<'PYEOF'
from pathlib import Path
from sys import argv
from xml.sax.saxutils import escape
import json
import os
import secrets
import sys

root = Path(argv[1]).resolve()
home = Path(argv[2]).resolve()
local_bin = Path(argv[3]).expanduser()
python_bin = Path(argv[4]).resolve()
customer_only = argv[5] == "1"
herdr_bin = argv[6]

template = root / "beat" / "ai.kaidera.kaidera-os.beat.plist.template"
python_dir = python_bin.parent

sys.path.insert(0, str(root / "beat"))
try:
    from cortex.runtime_profile import load_runtime_profile
    runtime_profile = load_runtime_profile(root)
except Exception:
    runtime_profile = {}

project_key = runtime_profile.get("project_key") or "local-cortex"
api_url = runtime_profile.get("api_url") or "http://localhost:8501"
beat_agent = runtime_profile.get("beat_agent") or f"beat@{project_key}"
plist_label = runtime_profile.get("beat_launchd_label") or f"com.cortex.{project_key}.beat"
start_interval = str(runtime_profile.get("beat_cadence_seconds") or 1500)
plist_out = home / "Library" / "LaunchAgents" / f"{plist_label}.plist"
(root / "beat" / "state").mkdir(parents=True, exist_ok=True)
(root / "beat" / "state" / "configured-launchagent-path").write_text(str(plist_out), encoding="utf-8")

values = {
    "__PROJECT_ROOT__": str(root),
    "__HOME__": str(home),
    "__LOCAL_BIN__": str(local_bin),
    "__PYTHON_BIN__": str(python_bin),
    "__PYTHON_DIR__": str(python_dir),
    "__PLIST_LABEL__": str(plist_label),
    "__START_INTERVAL__": str(start_interval),
    "__CORTEX_PROJECT__": str(project_key),
    "__CORTEX_API_URL__": str(api_url).rstrip("/"),
    "__CORTEX_ADMIN_TOKEN__": os.environ.get("CORTEX_ADMIN_TOKEN") or secrets.token_urlsafe(32),
    "__BEAT_CORTEX_AGENT__": str(beat_agent),
    "__KAIDERA_OS_HERDR_BIN__": herdr_bin,
}
text = template.read_text(encoding="utf-8")
for key, value in values.items():
    text = text.replace(key, escape(value))
plist_out.write_text(text, encoding="utf-8")

# Patch only machine-local runtime metadata. Keep docs generic.
for rel in (".agents/config/workspace.json",):
    path = root / rel
    if path.exists():
        content = path.read_text(encoding="utf-8")
        content = content.replace("${CORTEX_PROJECT_ROOT}", str(root))
        content = content.replace("$CORTEX_PROJECT_ROOT", str(root))
        content = content.replace("__PROJECT_ROOT__", str(root))
        path.write_text(content, encoding="utf-8")

print(plist_out)
PYEOF

PLIST_PATH="$(cat "$PROJECT_ROOT/beat/state/configured-launchagent-path")"
plutil -lint "$PLIST_PATH" >/dev/null
HERDR_STATUS="${HERDR_BIN:-optional (not found)}"

cat <<EOF
Configured local Cortex package:
  project root:  $PROJECT_ROOT
  dispatcher:    $LOCAL_BIN/cortex
  LaunchAgent:   $PLIST_PATH
  Herdr runtime:  $HERDR_STATUS

Next commands:
  cd "$PROJECT_ROOT"
  docker compose -f .agents/docker-compose.cortex.yml up -d
  launchctl bootstrap gui/\$(id -u) "$PLIST_PATH"
  ./beat/beatctl status
EOF
