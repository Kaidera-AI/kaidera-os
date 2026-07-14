#!/usr/bin/env bash
# Kaidera OS console - SECURE install bootstrap.
#
# This is the ONLY piece you fetch directly. It downloads the latest signed release from
# the public distribution repo, VERIFIES its minisign signature + SHA-256, and only then extracts
# and runs install.sh. A tampered or corrupt artifact fails verification and aborts — so the
# rest of the install is trustworthy even though this tiny script is fetched over the wire.
#
# Trust root: the minisign PUBLIC key embedded below (NOT downloaded). An attacker who swaps
# the release tarball cannot forge a signature for it without the matching PRIVATE key.
#
# Install on a new PC (prereqs: curl + minisign):
#   curl -fsSL https://github.com/Kaidera-AI/homebrew-kaidera/releases/latest/download/bootstrap.sh -o bootstrap.sh && bash bootstrap.sh
set -euo pipefail

DEFAULT_REPO="Kaidera-AI/homebrew-kaidera"
REPO="${KAIDERA_REPO:-$DEFAULT_REPO}"
TAG="${KAIDERA_RELEASE:-latest}"          # or pin a specific vX.Y.Z
DEFAULT_DEST="$HOME/kaidera-os"
DEST="${KAIDERA_DEST:-$DEFAULT_DEST}"        # where the app is installed
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# The minisign PUBLIC key — the root of trust. Populated by dist/setup-signing.sh.
MINISIGN_PUBKEY="RWT3GqtwZl9yMMzCsenpPRoefIRB67QCXcF3SC3Y4YJ3eE7KEXXmnIs6"

say(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------------------
[ -n "$REPO" ] || die "KAIDERA_REPO is required (for example: Kaidera-AI/homebrew-kaidera)."
command -v curl >/dev/null 2>&1     || die "curl is required to download public release assets."
command -v minisign >/dev/null 2>&1 || die "minisign required: 'brew install minisign' (macOS) or your package manager (Linux)."
case "$MINISIGN_PUBKEY" in RWQ__RUN*) die "bootstrap not configured — the publisher must run dist/setup-signing.sh to embed the public key." ;; esac

# --- 1. download the signed release ------------------------------------------------------
if [ "$TAG" = "latest" ]; then
  RELEASE_URL="$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest")" \
    || die "could not resolve the latest public release."
  TAG="${RELEASE_URL##*/}"
fi
case "$TAG" in
  v[0-9]*) ;;
  *) die "invalid release tag '$TAG' (expected vX.Y.Z)." ;;
esac

say "Downloading signed release ($TAG) from $REPO"
BASE_URL="https://github.com/$REPO/releases/download/$TAG"
TARBALL="$WORK/kaidera-os-$TAG.tar.gz"
for ASSET in "$(basename "$TARBALL")" "$(basename "$TARBALL").minisig" "$(basename "$TARBALL").sha256"; do
  curl -fsSL "$BASE_URL/$ASSET" -o "$WORK/$ASSET" \
    || die "release download failed for $TAG asset $ASSET."
done
[ -f "$TARBALL" ] || die "no release tarball found in the download."
ok "got $(basename "$TARBALL")"

# --- 2. verify INTEGRITY (SHA-256) -------------------------------------------------------
say "Verifying integrity (SHA-256)"
( cd "$WORK" && {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum -c "$(basename "$TARBALL").sha256";
    else shasum -a 256 -c "$(basename "$TARBALL").sha256"; fi
  } ) >/dev/null || die "SHA-256 MISMATCH — the download is corrupt or tampered. Aborting."
ok "checksum matches"

# --- 3. verify AUTHENTICITY (minisign signature against the embedded key) -----------------
say "Verifying authenticity (minisign signature)"
minisign -V -P "$MINISIGN_PUBKEY" -m "$TARBALL" >/dev/null \
  || die "SIGNATURE INVALID — this release is NOT authentic (or was tampered). Aborting."
ok "signature valid — authentic release"

# --- 4. extract + install ----------------------------------------------------------------
say "Installing to $DEST"
if [ -d "$DEST/.git" ] && [ "${KAIDERA_ALLOW_GIT_DEST:-0}" != "1" ]; then
  die "refusing to install a redistributable into a Git checkout: $DEST
Set KAIDERA_DEST to a dedicated install directory, or use ./install.sh inside development checkouts.
Override only for one-off recovery with KAIDERA_ALLOW_GIT_DEST=1."
fi
EXTRACT="$WORK/extract"
mkdir -p "$DEST" "$EXTRACT"

tar -xzf "$TARBALL" -C "$EXTRACT"
SRCROOT="$(find "$EXTRACT" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -n "$SRCROOT" ] && [ -d "$SRCROOT" ] || die "release archive did not contain a top-level directory."

if command -v rsync >/dev/null 2>&1; then
  # Synchronize the signed release with delete semantics so removed project/demo
  # files do not survive forever on upgraded VMs. Preserve only deployment-local
  # state that install.sh/runtime own.
  rsync -a --delete \
    --exclude='.git/' \
    --exclude='.env' \
    --exclude='.envrc' \
    --exclude='.dogfood-backup/' \
    --exclude='.kaidera-os/' \
    --exclude='.local/' \
    --exclude='.playwright-cli/' \
    --exclude='.agents/agents/' \
    --exclude='.agents/backups/' \
    --exclude='.agents/config/autonomy-policy.json' \
    --exclude='.agents/config/beat.env' \
    --exclude='.agents/config/runtime.yaml' \
    --exclude='.agents/config/sync.yaml' \
    --exclude='.agents/config/workspace.json' \
    --exclude='beat/logs/' \
    --exclude='beat/state/' \
    --exclude='local-cortex/.console-host' \
    --exclude='local-cortex/.env' \
    --exclude='local-cortex/console/.venv/' \
    --exclude='local-cortex/logs/' \
    --exclude='output/' \
    "$SRCROOT"/ "$DEST"/
else
  printf '\033[33m! rsync not found — overlaying release without pruning stale files. Install rsync for clean updates.\033[0m\n' >&2
  tar -xzf "$TARBALL" -C "$DEST" --strip-components=1
fi

cd "$DEST"
chmod +x ./install.sh 2>/dev/null || true
[ -f ./install.sh ] || die "install.sh missing from the release."
exec ./install.sh
