#!/usr/bin/env bash
# Idempotent installer for the Kaidera OS fitness pre-push hook.
#
#   install-hooks.sh              install (symlink scripts/fitness/pre-push
#                                 -> <repo>/.git/hooks/pre-push)
#   install-hooks.sh --uninstall  remove our symlink, restore any backup
#
# Behaviour:
#   - Installs via SYMLINK so edits to the source hook take effect with no
#     reinstall.
#   - If a non-symlink (foreign) pre-push already exists, it is backed up to
#     pre-push.bak-<n> before we install — never clobbered.
#   - Idempotent: re-running when already correctly linked is a no-op.
#   - --uninstall removes our symlink and, if a backup exists, restores the
#     most recent one. Fully reversible.
#
# Paths are resolved from THIS script's own location — no hardcoded absolutes.
# The git dir is overridable via FITNESS_INSTALL_GIT_DIR (used by tests against
# throwaway fixture repos); it defaults to <repo>/.git.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SOURCE_HOOK="$SCRIPT_DIR/pre-push"

GIT_DIR="${FITNESS_INSTALL_GIT_DIR:-$REPO_ROOT/.git}"
HOOKS_DIR="$GIT_DIR/hooks"
TARGET="$HOOKS_DIR/pre-push"

uninstall=0
if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall=1
elif [[ -n "${1:-}" ]]; then
    echo "install-hooks.sh: unknown argument '$1' (expected --uninstall or none)" >&2
    exit 2
fi

# Is TARGET our symlink (points at SOURCE_HOOK)?
is_our_link() {
    [[ -L "$TARGET" ]] || return 1
    local dest
    dest="$(cd "$(dirname "$TARGET")" && cd "$(dirname "$(readlink "$TARGET")")" 2>/dev/null && pwd)/$(basename "$(readlink "$TARGET")")" || return 1
    [[ "$dest" == "$SOURCE_HOOK" ]]
}

# Most recent existing backup file (pre-push.bak-<n>), or empty.
latest_backup() {
    local b last=""
    for b in "$HOOKS_DIR"/pre-push.bak-*; do
        [[ -e "$b" ]] || continue
        last="$b"
    done
    printf '%s' "$last"
}

if [[ "$uninstall" -eq 1 ]]; then
    if is_our_link; then
        rm -f "$TARGET"
        echo "uninstall: removed fitness pre-push symlink ($TARGET)"
        backup="$(latest_backup)"
        if [[ -n "$backup" ]]; then
            mv "$backup" "$TARGET"
            echo "uninstall: restored previous hook from $(basename "$backup")"
        fi
    elif [[ -e "$TARGET" || -L "$TARGET" ]]; then
        echo "uninstall: $TARGET is not our symlink — leaving it untouched"
    else
        echo "uninstall: no fitness pre-push installed — nothing to do"
    fi
    exit 0
fi

# ── install ──────────────────────────────────────────────────────────────────
[[ -f "$SOURCE_HOOK" ]] || { echo "install: source hook missing: $SOURCE_HOOK" >&2; exit 1; }
mkdir -p "$HOOKS_DIR"

if is_our_link; then
    echo "install: fitness pre-push already linked ($TARGET) — no-op"
    exit 0
fi

# Something else is there (foreign hook or stale link) — back it up first.
if [[ -e "$TARGET" || -L "$TARGET" ]]; then
    n=1
    while [[ -e "$HOOKS_DIR/pre-push.bak-$n" ]]; do
        n=$((n + 1))
    done
    backup="$HOOKS_DIR/pre-push.bak-$n"
    mv "$TARGET" "$backup"
    echo "install: backed up existing pre-push -> $(basename "$backup")"
fi

ln -s "$SOURCE_HOOK" "$TARGET"
echo "install: linked fitness pre-push ($TARGET -> $SOURCE_HOOK)"
exit 0
