#!/usr/bin/env bash
# Fitness gate — the COMMITTED tree (exactly what a signed release archive delivers to a new
# user / client / dev box) must contain EVERY artifact that install.sh and the running app need
# on a BLANK machine.
#
# This is the guard against the "gitignored-but-needed" CLASS of bug: a runtime file that is
# present in a developer's working tree but NOT in the commit, so it silently vanishes from the
# redistributable. The bug that motivated it: the pre-built SPA JS bundle (spa/dist/assets/*.js)
# is gitignored + force-added, and a worktree ship using `git add -A` dropped it — a fresh
# install then died "SPA bundle missing" because a npm-less server can't rebuild it.
#
# CRITICAL: every check reads HEAD (the COMMIT), never the working tree — so a present-but-
# uncommitted file can NOT mask a gap. `git cat-file -e HEAD:<path>` is true iff <path> is in
# the commit. Drop a new `need` line here whenever install.sh starts depending on a new file.
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

FAILS=0
need() {  # need <path-relative-to-repo> <why it's required on a fresh machine>
  if ! git cat-file -e "HEAD:$1" 2>/dev/null; then
    printf '  \033[31m✗ NOT in the commit:\033[0m %s — %s\n' "$1" "$2"
    FAILS=$((FAILS + 1))
  fi
}

C=local-cortex/console

# 1. The installer + everything it directly reads / calls (install.sh steps 2–3).
need install.sh                              "the installer itself"
need uninstall.sh                            "the clean/greenfield wipe — a fresh deploy reinstalls onto a wiped box"
need "$C/requirements.txt"                   "the console venv deps (install.sh step 3 — else the console won't import)"
need "$C/scripts/build-spa.sh"               "install.sh's SPA-build fallback (called when a source checkout has npm)"
need redistributable/scripts/install-herdr-runtime.sh "install.sh's Herdr prerequisite installer/checker"
need .agents/docker-compose.cortex.yml       "the Cortex + app-DB stack (install.sh step 2 dies if absent)"
need "$C/app/main.py"                         "the console ASGI app the systemd unit runs"
need "$C/app/version.py"                      "the build stamp"

# 2. The SPA bundle — the load-bearing one. index.html AND every asset it references must ALL be
#    committed: a fresh, npm-less server CANNOT rebuild it. This is the exact dropped-asset bug.
need "$C/spa/dist/index.html"                "the SPA shell (without it /app is blank)"
INDEX="$(git show "HEAD:$C/spa/dist/index.html" 2>/dev/null || true)"
if [ -n "$INDEX" ]; then
  refs="$(printf '%s' "$INDEX" | grep -oE 'assets/[A-Za-z0-9._-]+\.(js|css)' | sort -u || true)"
  if [ -z "$refs" ]; then
    printf '  \033[31m✗ broken bundle:\033[0m spa/dist/index.html references NO assets/*.js (empty/failed build)\n'
    FAILS=$((FAILS + 1))
  fi
  for a in $refs; do
    need "$C/spa/dist/$a" "referenced by the SPA index.html — a fresh server has no npm to rebuild it"
  done
fi

# 3. The Cortex + app-DB stack's build + bootstrap inputs. A fresh `docker compose up` BUILDS the
#    api/worker images from these contexts and BOOTSTRAPS a fresh DB from the committed SQL — so
#    they must all be in the commit (Agent-audited: every one is committed today; keep it so).
need .agents/data/initdb/00-cortex-bootstrap.sh        "cortex-pg fresh-volume bootstrap (roles + schema apply)"
need .agents/data/cortex-schema-full.sql               "the Cortex schema a fresh cortex-pg volume loads"
need .agents/api/Dockerfile                            "cortex-api build context"
need .agents/api/main.py                               "the Cortex API"
need .agents/scripts/cortex-boot                       "the Cortex boot/context command"
need .agents/scripts/cortex-handoff                    "the Cortex coordination command"
need .agents/scripts/cortex-log                        "the Cortex durable-log command"
need .agents/scripts/cortex-search                     "the Cortex retrieval command"
need local-cortex/containers/graph-worker/Dockerfile   "graph-worker build context"
need local-cortex/containers/graph-worker/worker.py    "graph-worker implementation"
need local-cortex/containers/embed-worker/Dockerfile   "embed-worker build context"
need local-cortex/containers/embed-worker/worker.py    "embed-worker implementation"
need local-cortex/containers/pdf-worker/Dockerfile     "pdf-worker build context"
need local-cortex/containers/pdf-worker/worker.py      "pdf-worker implementation"
# Profiled build contexts — needed when a deploy adds `--profile full` (audio/vision L5) or builds
# the cortex-cli image. They're not in the default `up`, but a fresh `--profile full` build dies if
# the Dockerfile context isn't in the commit, so guard them here too.
need local-cortex/containers/audio-worker/Dockerfile   "audio-worker build context (KAIDERA_CORTEX_PROFILE=full)"
need local-cortex/containers/audio-worker/worker.py    "audio-worker implementation (KAIDERA_CORTEX_PROFILE=full)"
need local-cortex/containers/vision-worker/Dockerfile  "vision-worker build context (KAIDERA_CORTEX_PROFILE=full)"
need local-cortex/containers/vision-worker/worker.py   "vision-worker implementation (KAIDERA_CORTEX_PROFILE=full)"
need local-cortex/containers/cli/Dockerfile            "cortex-cli build context"
# The app-DB has no schema unless ≥1 migration ships (the migrate one-shot applies /sql/*.sql).
if [ -z "$(git ls-tree -r HEAD --name-only -- .agents/data/appdb 2>/dev/null | grep '\.sql$' || true)" ]; then
  printf '  \033[31m✗ no .agents/data/appdb/*.sql in the commit\033[0m — the app-DB would boot with NO schema\n'
  FAILS=$((FAILS + 1))
fi

# GREENFIELD redist: a FRESH Cortex DB bootstraps from cortex-schema-full.sql (the `need` above), NOT by
# replaying .agents/data/migrations/*.sql — those are the DOGFOOD upgrade path for an existing DB. So an
# uncommitted migration does NOT break a fresh install (the schema dump is the source of truth) and must
# NOT red the redist gate or fight in-flight migration work. We only WARN, so the repo-hygiene gap
# (a deployed migration not yet committed) is visible without blocking the greenfield package.
untracked_sql="$(git status --porcelain -- .agents/data/migrations .agents/data/appdb 2>/dev/null | grep -E '^\?\?.*\.sql$' | awk '{print $2}' || true)"
if [ -n "$untracked_sql" ]; then
  printf '  \033[33m⚠ uncommitted migration(s)\033[0m (dogfood upgrade path; NOT the greenfield fresh-DB source — commit when stable):\n'
  printf '%s\n' "$untracked_sql" | sed 's/^/       /'
fi

if [ "$FAILS" -gt 0 ]; then
  printf '  \033[1;31m❌ redist-complete — %s required artifact(s) are NOT in the commit; a fresh install WOULD break.\033[0m\n' "$FAILS"
  printf '     A signed release only delivers COMMITTED files. Commit the missing ones.\n'
  printf '     For gitignored build output (the SPA bundle): `git add -f %s/spa/dist && git commit`.\n' "$C"
  exit 1
fi
printf '  \033[1;32m✅ redist-complete — the commit carries the full SPA bundle + every file install.sh needs\033[0m\n'
