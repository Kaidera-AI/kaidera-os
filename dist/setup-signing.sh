#!/usr/bin/env bash
# ONE-TIME: create the minisign signing keypair (your root of trust) and embed the PUBLIC
# key into dist/bootstrap.sh. Run this once on the publishing machine, then commit the
# updated bootstrap.sh.
#
# SECURITY: the PRIVATE key (~/.minisign/minisign.key) is what proves a release is genuinely
# yours. Keep it SECRET, password-protected, and backed up offline. If it leaks, anyone can
# forge releases that pass verification — rotate it (new keypair → re-embed → re-release).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

command -v minisign >/dev/null 2>&1 || {
  echo "Install minisign first:"
  echo "  macOS:  brew install minisign"
  echo "  Debian: sudo apt-get install minisign     Fedora: sudo dnf install minisign"
  exit 1
}

KEYDIR="${MINISIGN_DIR:-$HOME/.minisign}"; mkdir -p "$KEYDIR"
SECKEY="$KEYDIR/minisign.key"; PUBKEY="$KEYDIR/minisign.pub"

if [ -f "$SECKEY" ]; then
  echo "Signing key already exists at $SECKEY — reusing it (not regenerating)."
else
  if [ "${MINISIGN_NO_PASSWORD:-0}" = "1" ]; then
    echo "Generating an unencrypted minisign keypair for headless release automation."
    echo "Keep $SECKEY secret and rotate back to a password-protected key when practical."
    minisign -G -W -s "$SECKEY" -p "$PUBKEY"
  else
    echo "Generating a minisign keypair. You'll be asked for a password — choose a strong one"
    echo "and remember it; it encrypts the private key."
    minisign -G -s "$SECKEY" -p "$PUBKEY"
  fi
fi

# The public key is the LAST line of the .pub file (the base64 string).
PUB="$(tail -1 "$PUBKEY")"
[ -n "$PUB" ] || { echo "could not read public key from $PUBKEY"; exit 1; }
echo ""
echo "Public key: $PUB"

# Embed it into bootstrap.sh (the verifier ships its own trust anchor).
if command -v sed >/dev/null 2>&1; then
  sed -i.bak "s|^MINISIGN_PUBKEY=.*|MINISIGN_PUBKEY=\"$PUB\"|" bootstrap.sh && rm -f bootstrap.sh.bak
  echo "Embedded the public key into dist/bootstrap.sh."
fi
echo ""
echo "Next:"
echo "  1. Commit dist/bootstrap.sh (now carries your public key)."
echo "  2. Keep $SECKEY SECRET + backed up."
echo "  3. Publish a release:  dist/release.sh"
