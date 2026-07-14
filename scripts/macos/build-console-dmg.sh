#!/usr/bin/env bash
# Build the Kaidera OS macOS console redist DMG.
#
# This is the HEAVY runtime payload (console + Cortex + install.sh), packaged as a
# macOS disk image so it can ship alongside the thin operator DMG.
# Together they are the "two DMG" split: Kaidera OS Operator (menu-bar app, app-only)
# + Kaidera OS Console (this — the full runtime the operator drives).
#
# The payload is `git archive HEAD` — the same committed tree dist/release.sh
# tarballs — so this DMG and the GitHub-Releases tarball can never drift. It
# deliberately mirrors build-operator-dmg.sh's OUTPUT contract (versioned DMG +
# .sha256 + .metadata.json + latest alias + staged publication bundle) so the
# configured publish step treats both channels identically.
#
#   scripts/macos/build-console-dmg.sh
#
# Optional env:
#   KAIDERA_OS_CODESIGN_IDENTITY   Developer ID to sign the DMG (else unsigned)
#   KAIDERA_OS_NOTARY_PROFILE      notarytool keychain profile (else skip notarize)
#   KAIDERA_OS_CONSOLE_PUBLISH_DIR staging output dir (default output/release/kaidera-os-console-macos)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONSOLE_DIR="$ROOT/local-cortex/console"
VERSION_FILE="$CONSOLE_DIR/app/version.py"
DIST_DIR="$ROOT/dist/macos"
WORK_DIR="$ROOT/.build/macos-console-dmg"
VOL_NAME="Kaidera OS Console"

say(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }
die(){ printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "DMG builds must run on macOS."
command -v hdiutil >/dev/null 2>&1 || die "hdiutil not found."
command -v git >/dev/null 2>&1 || die "git not found."

VERSION="$(
  python3 - <<PY
import re
from pathlib import Path
text = Path("$VERSION_FILE").read_text(encoding="utf-8")
match = re.search(r'__version__\\s*=\\s*"([^"]+)"', text)
if not match:
    raise SystemExit(1)
print(match.group(1))
PY
)"
[ -n "$VERSION" ] || die "could not read version from $VERSION_FILE"

PREFIX="kaidera-os-v${VERSION}"
DMG_NAME="kaidera-os-console-v${VERSION}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"
COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

say "1/6 Redist completeness — HEAD must carry every install/runtime file"
bash "$ROOT/scripts/fitness/check-redist-complete.sh" \
  || die "redist INCOMPLETE — a fresh install would break. Fix + commit. NOTHING built."

say "2/6 Stage redist payload (git archive HEAD @ $COMMIT)"
rm -rf "$WORK_DIR"; mkdir -p "$WORK_DIR/stage" "$DIST_DIR"
git -C "$ROOT" archive --format=tar --prefix="$PREFIX/" HEAD | tar -xf - -C "$WORK_DIR/stage"
printf 'public\n' > "$WORK_DIR/stage/$PREFIX/.kaidera-os-edition"
python3 "$WORK_DIR/stage/$PREFIX/scripts/release/bake-public-edition.py" \
  "$WORK_DIR/stage/$PREFIX/local-cortex/console/app/edition.py"
# MANIFEST for parity with the release tarball (self-listing, deterministic order).
(
  cd "$WORK_DIR/stage/$PREFIX"
  { find . -type f -print | sed 's#^\./##'; printf 'MANIFEST.txt\n'; } \
    | LC_ALL=C sort -u > MANIFEST.txt
)
ok "staged $PREFIX ($(find "$WORK_DIR/stage/$PREFIX" -type f | wc -l | tr -d ' ') files)"

say "3/6 Secret scan — zero credentials may ship (defense past export-ignore)"
if grep -aIqrE "(sk-[A-Za-z0-9]{20,}|fw_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xai-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)" "$WORK_DIR/stage/$PREFIX"; then
  die "SECRET DETECTED in the payload — ABORTING. Add the offending path to .gitattributes (export-ignore)."
fi
ok "no credential patterns in the payload"

say "4/6 Install README + create DMG"
cat > "$WORK_DIR/stage/README.txt" <<EOF
Kaidera OS Console v$VERSION  (redist payload, build commit $COMMIT)

This disk image contains the full Kaidera OS console + Cortex runtime installer.
It is the heavy payload — the companion to the thin "Kaidera OS Operator" app DMG.

Install:
1. Copy the "$PREFIX" folder from this disk image to your Mac (e.g. into your home folder).
2. Open Terminal, then:
     cd ~/$PREFIX
     ./install.sh
3. install.sh brings up the Cortex + app-DB containers and the console service.
4. (Optional) Install the "Kaidera OS Operator" app DMG to drive it from the menu bar.

Requires: macOS 14+, Docker, Python 3. See $PREFIX/INSTALL.md for full details.
EOF

# Notarization requires every Mach-O in the payload to carry a Developer ID signature and
# secure timestamp. Detect binaries by file type so executables and extensionless payloads
# cannot bypass signing.
if [ -n "${KAIDERA_OS_CODESIGN_IDENTITY:-}" ]; then
  command -v codesign >/dev/null 2>&1 || die "codesign not found but KAIDERA_OS_CODESIGN_IDENTITY set."
  _MACHO_LIST="$WORK_DIR/macho-files"
  : > "$_MACHO_LIST"
  while IFS= read -r -d '' _bin; do
    if file -b "$_bin" 2>/dev/null | grep -q 'Mach-O'; then
      printf '%s\0' "$_bin" >> "$_MACHO_LIST"
    fi
  done < <(find "$WORK_DIR/stage" -type f -print0)
  _MACHO_COUNT="$(tr -cd '\000' < "$_MACHO_LIST" | wc -c | tr -d ' ')"
  while IFS= read -r -d '' _bin; do
    codesign --force --timestamp --options runtime --sign "$KAIDERA_OS_CODESIGN_IDENTITY" "$_bin"
    codesign --verify --strict "$_bin"
  done < "$_MACHO_LIST"
  ok "signed ${_MACHO_COUNT:-0} inner Mach-O binaries (notarization prerequisite)"
fi
rm -f "$DMG_PATH"
hdiutil create -volname "$VOL_NAME" -srcfolder "$WORK_DIR/stage" -ov -format UDZO "$DMG_PATH" >/dev/null
ok "created $DMG_PATH"

say "5/6 Sign + notarize (optional)"
if [ -n "${KAIDERA_OS_CODESIGN_IDENTITY:-}" ]; then
  codesign --force --timestamp --sign "$KAIDERA_OS_CODESIGN_IDENTITY" "$DMG_PATH"
  ok "signed DMG with $KAIDERA_OS_CODESIGN_IDENTITY"
  if [ -n "${KAIDERA_OS_NOTARY_PROFILE:-}" ]; then
    command -v xcrun >/dev/null 2>&1 || die "xcrun not found but KAIDERA_OS_NOTARY_PROFILE set."
    xcrun notarytool submit "$DMG_PATH" --keychain-profile "$KAIDERA_OS_NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG_PATH"
    ok "notarized + stapled"
  fi
else
  ok "unsigned DMG (set KAIDERA_OS_CODESIGN_IDENTITY + KAIDERA_OS_NOTARY_PROFILE for a public-trusted build)"
fi

say "6/6 Checksums, metadata, publication staging"
(cd "$DIST_DIR" && shasum -a 256 "$DMG_NAME") | tee "$DMG_PATH.sha256" >/dev/null
ok "sha256: $(awk '{print $1}' "$DMG_PATH.sha256")"

# Inline metadata (parity with operator_release_metadata.py fields; product = Console).
python3 - "$DMG_PATH" "$VERSION" "$COMMIT" "$DMG_PATH.metadata.json" "${KAIDERA_OS_CODESIGN_IDENTITY:-}" "${KAIDERA_OS_NOTARY_PROFILE:-}" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

dmg, version, commit, out = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
identity, notary = sys.argv[5].strip(), sys.argv[6].strip()
h = hashlib.sha256()
with dmg.open("rb") as fh:
    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
        h.update(chunk)
notarized = bool(identity and notary)
meta = {
    "product": "Kaidera OS Console",
    "channel": "macos",
    "version": version,
    "artifact": dmg.name,
    "artifact_url": dmg.name,
    "sha256": h.hexdigest(),
    "size_bytes": dmg.stat().st_size,
    "commit": commit,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "signing": {
        "kind": "developer_id" if identity else "unsigned",
        "identity": identity or None,
        "notarized": notarized,
        "stapled": notarized,
    },
    "public_release_ready": bool(identity and notarized),
    "install_notes": [
        "Copy the kaidera-os-v<version> folder from the disk image to your Mac.",
        "Run ./install.sh from that folder to bring up Cortex + the console.",
        "This is the full runtime payload, not the thin operator app.",
        "Install the Kaidera OS Operator app DMG to control it from the menu bar.",
        "Requires macOS 14+, Docker, and Python 3.",
    ],
}
out.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
cp "$DMG_PATH.metadata.json" "$DIST_DIR/latest-console-macos.json"
ok "metadata + latest alias ready"

# Verify the DMG mounts and actually carries the installer before we stage it.
MNT="$(mktemp -d)"
hdiutil attach "$DMG_PATH" -nobrowse -readonly -mountpoint "$MNT" >/dev/null
if [ ! -f "$MNT/$PREFIX/install.sh" ] || [ ! -f "$MNT/README.txt" ]; then
  hdiutil detach "$MNT" >/dev/null 2>&1 || true; rm -rf "$MNT"
  die "DMG self-check failed: $PREFIX/install.sh or README.txt missing inside the image."
fi
hdiutil detach "$MNT" >/dev/null 2>&1 || true; rm -rf "$MNT"
ok "DMG mounts; $PREFIX/install.sh present"

# Stage the publication bundle for the configured deployment target.
STAGE_OUT="${KAIDERA_OS_CONSOLE_PUBLISH_DIR:-$ROOT/output/release/kaidera-os-console-macos}"
mkdir -p "$STAGE_OUT"
find "$STAGE_OUT" -maxdepth 1 -type f \( \
  -name 'kaidera-os-console-v*.dmg' -o -name 'kaidera-os-console-v*.dmg.sha256' -o \
  -name 'kaidera-os-console-v*.dmg.metadata.json' -o -name 'latest-macos.json' -o \
  -name 'publication-manifest.json' -o -name 'README.md' \
\) -exec rm -f {} +
cp "$DMG_PATH" "$DMG_PATH.sha256" "$DMG_PATH.metadata.json" "$STAGE_OUT/"
cp "$DMG_PATH.metadata.json" "$STAGE_OUT/latest-macos.json"
python3 - "$STAGE_OUT" "$VERSION" "$DMG_NAME" <<'PY'
import json, sys
from pathlib import Path
out, version, dmg = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
files = [dmg, f"{dmg}.sha256", f"{dmg}.metadata.json", "latest-macos.json", "README.md"]
(out / "publication-manifest.json").write_text(
    json.dumps({
        "product": "Kaidera OS Console",
        "channel": "macos",
        "version": version,
        "files": files,
        "publish_contract": "copy all files to the configured static Mac console download path",
    }, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
(out / "README.md").write_text(
    f"# Kaidera OS Console Mac Release\n\n"
    f"Staged from the canonical Kaidera OS repository.\n\n"
    f"- Version: {version}\n- DMG: {dmg}\n- SHA-256: {dmg}.sha256\n"
    f"- Metadata: {dmg}.metadata.json\n- Latest metadata alias: latest-macos.json\n\n"
    f"Publish these files together under the Mac **console** download path, next to the\n"
    f"operator channel. The versioned filenames are immutable; update `latest-macos.json`\n"
    f"to point customers at the current console payload.\n",
    encoding="utf-8",
)
PY
ok "staged publication bundle: $STAGE_OUT"
ok "DMG ready: $DMG_PATH"
