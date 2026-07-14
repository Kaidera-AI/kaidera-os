#!/usr/bin/env bash
# Build the Kaidera OS macOS operator DMG.
#
# This packages the E011 menu-bar controller. It does not implement a second
# installer or updater: the app controls the existing LaunchAgent/runner and
# delegates updates to the E010 endpoints.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONSOLE_DIR="$ROOT/local-cortex/console"
SWIFT_PACKAGE_DIR="$ROOT/native/macos/KaideraOSOperator"
VERSION_FILE="$CONSOLE_DIR/app/version.py"
DIST_DIR="$ROOT/dist/macos"
WORK_DIR="$ROOT/.build/macos-operator-dmg"
APP_BUILD_DIR="$ROOT/.build/macos-operator-app"
APP_NAME="Kaidera OS Operator.app"
VOL_NAME="Kaidera OS"

say(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }
die(){ printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "DMG builds must run on macOS."
command -v hdiutil >/dev/null 2>&1 || die "hdiutil not found."
command -v swift >/dev/null 2>&1 || die "swift not found. Install Xcode or Xcode Command Line Tools."
command -v codesign >/dev/null 2>&1 || die "codesign not found."
[ -z "${KAIDERA_OS_NOTARY_PROFILE:-}" ] || command -v xcrun >/dev/null 2>&1 || die "xcrun not found; install Xcode command line tools."

if [ "${KAIDERA_OS_REQUIRE_RELEASE_SIGNING:-}" = "1" ]; then
  READINESS_ARGS=(--strict)
  if [ "${KAIDERA_OS_VERIFY_NOTARY_PROFILE:-}" = "1" ]; then
    READINESS_ARGS+=(--verify-notary-profile)
  fi
  python3 "$ROOT/scripts/macos/operator_release_readiness.py" "${READINESS_ARGS[@]}"
fi

[ -f "$SWIFT_PACKAGE_DIR/Package.swift" ] || die "Swift operator package missing: $SWIFT_PACKAGE_DIR/Package.swift"

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
DMG_NAME="kaidera-os-operator-v${VERSION}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"
COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

say "1/5 Build operator app"
swift build -c release --package-path "$SWIFT_PACKAGE_DIR"
SWIFT_BIN_DIR="$(swift build -c release --package-path "$SWIFT_PACKAGE_DIR" --show-bin-path)"
SWIFT_BIN="$SWIFT_BIN_DIR/KaideraOSOperator"
SWIFT_RESOURCE_BUNDLE="$SWIFT_BIN_DIR/KaideraOSOperator_KaideraOSOperator.bundle"
OPERATOR_ICON="$SWIFT_PACKAGE_DIR/Sources/KaideraOSOperator/Resources/kaidera-os-operator.icns"
[ -x "$SWIFT_BIN" ] || die "SwiftPM did not produce $SWIFT_BIN"
[ -f "$OPERATOR_ICON" ] || die "operator app icon missing: $OPERATOR_ICON"
rm -rf "$APP_BUILD_DIR"
mkdir -p "$APP_BUILD_DIR/$APP_NAME/Contents/MacOS" "$APP_BUILD_DIR/$APP_NAME/Contents/Resources"
cp "$SWIFT_BIN" "$APP_BUILD_DIR/$APP_NAME/Contents/MacOS/Kaidera OS Operator"
chmod 755 "$APP_BUILD_DIR/$APP_NAME/Contents/MacOS/Kaidera OS Operator"
cp "$OPERATOR_ICON" "$APP_BUILD_DIR/$APP_NAME/Contents/Resources/kaidera-os-operator.icns"
if [ -d "$SWIFT_RESOURCE_BUNDLE" ]; then
  cp -R "$SWIFT_RESOURCE_BUNDLE" "$APP_BUILD_DIR/$APP_NAME/Contents/Resources/"
else
  die "SwiftPM did not produce resource bundle $SWIFT_RESOURCE_BUNDLE"
fi
cat > "$APP_BUILD_DIR/$APP_NAME/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>Kaidera OS Operator</string>
  <key>CFBundleExecutable</key>
  <string>Kaidera OS Operator</string>
  <key>CFBundleIdentifier</key>
  <string>ai.kaidera.kaidera-os.operator</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleIconFile</key>
  <string>kaidera-os-operator</string>
  <key>CFBundleName</key>
  <string>Kaidera OS Operator</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>$VERSION</string>
  <key>CFBundleVersion</key>
  <string>$VERSION</string>
  <key>LSMinimumSystemVersion</key>
  <string>14.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
EOF
xattr -cr "$APP_BUILD_DIR/$APP_NAME" 2>/dev/null || true
ok "built $APP_BUILD_DIR/$APP_NAME"

say "2/5 Code signing"
if [ -n "${KAIDERA_OS_CODESIGN_IDENTITY:-}" ]; then
  codesign --force --deep --timestamp --options runtime --sign "$KAIDERA_OS_CODESIGN_IDENTITY" "$APP_BUILD_DIR/$APP_NAME"
  ok "signed app with $KAIDERA_OS_CODESIGN_IDENTITY"
else
  codesign --force --deep --sign - "$APP_BUILD_DIR/$APP_NAME"
  ok "ad-hoc signed app (set KAIDERA_OS_CODESIGN_IDENTITY for Developer ID signing)"
fi
codesign --verify --deep --strict "$APP_BUILD_DIR/$APP_NAME"

say "3/5 Stage DMG contents"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/stage" "$DIST_DIR"
cp -R "$APP_BUILD_DIR/$APP_NAME" "$WORK_DIR/stage/$APP_NAME"
ln -s /Applications "$WORK_DIR/stage/Applications"
cat > "$WORK_DIR/stage/README.txt" <<EOF
Kaidera OS Operator v$VERSION

Drag "Kaidera OS Operator.app" to Applications, then open it from the menu bar.

First run:
1. This DMG installs only the operator app; it does not install Cortex or the Kaidera OS runtime.
2. Use it on a Mac where Kaidera OS/Cortex is already installed.
3. The operator controls the existing Kaidera OS LaunchAgent and opens the browser console.
4. Updates are delegated to the Kaidera OS update endpoints; no second updater is bundled here.
5. Use "Preflight" to check install-root, Python, Docker, runner, and Cortex readiness.
6. If the operator cannot find the existing install, set KAIDERA_OS_HOME or run:
   local-cortex/console/scripts/kaidera-os operator set-home /path/to/kaidera-os
7. Use "Run Install / Repair" only to repair an existing Kaidera OS install; the DMG itself stays app-only.

Build commit: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
EOF
ok "staged app, Applications symlink, README"

say "4/5 Create DMG"
rm -f "$DMG_PATH"
hdiutil create -volname "$VOL_NAME" -srcfolder "$WORK_DIR/stage" -ov -format UDZO "$DMG_PATH" >/dev/null
ok "created $DMG_PATH"
if [ -n "${KAIDERA_OS_CODESIGN_IDENTITY:-}" ]; then
  codesign --force --timestamp --sign "$KAIDERA_OS_CODESIGN_IDENTITY" "$DMG_PATH"
  ok "signed DMG with $KAIDERA_OS_CODESIGN_IDENTITY"
fi

say "5/5 Optional notarization"
if [ -n "${KAIDERA_OS_NOTARY_PROFILE:-}" ]; then
  xcrun notarytool submit "$DMG_PATH" --keychain-profile "$KAIDERA_OS_NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG_PATH"
  ok "notarized + stapled $DMG_NAME"
else
  ok "skipped notarization (set KAIDERA_OS_NOTARY_PROFILE to notarize)"
fi

(cd "$DIST_DIR" && shasum -a 256 "$DMG_NAME") | tee "$DMG_PATH.sha256"
METADATA_CMD=(python3 "$ROOT/scripts/macos/operator_release_metadata.py" \
  "$DMG_PATH" \
  --version "$VERSION" \
  --commit "$COMMIT" \
  --output "$DMG_PATH.metadata.json")
if [ -n "${KAIDERA_OS_CODESIGN_IDENTITY:-}" ]; then
  METADATA_CMD+=(--codesign-identity "$KAIDERA_OS_CODESIGN_IDENTITY")
fi
if [ -n "${KAIDERA_OS_NOTARY_PROFILE:-}" ]; then
  METADATA_CMD+=(--notarized --stapled)
fi
"${METADATA_CMD[@]}" >/dev/null
ok "metadata ready: $DMG_PATH.metadata.json"
cp "$DMG_PATH.metadata.json" "$DIST_DIR/latest-macos.json"
ok "latest metadata alias ready: $DIST_DIR/latest-macos.json"
ok "DMG ready: $DMG_PATH"
