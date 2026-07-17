#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "usage: $0 X.Y.Z" >&2
  exit 2
}

cd "$ROOT"
ACTUAL="$(sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([0-9]+[.][0-9]+[.][0-9]+)"/\1/p' local-cortex/console/app/version.py)"
[ "$ACTUAL" = "$VERSION" ] || {
  echo "version mismatch: source=$ACTUAL requested=$VERSION" >&2
  exit 1
}
git diff --quiet
git diff --cached --quiet
[ -z "$(git ls-files --others --exclude-standard)" ] || {
  echo "refusing to archive an untracked working tree" >&2
  exit 1
}

bash scripts/fitness/check-community-source-boundary.sh
bash scripts/fitness/check-version-changelog-sync.sh

OUT="$ROOT/output/release/community"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ARCHIVE="$OUT/kaidera-os-v${VERSION}.tar.gz"
mkdir -p "$OUT"
rm -f "$ARCHIVE" "$ARCHIVE.sha256" "$ARCHIVE.minisig"

git archive --format=tar --prefix="kaidera-os-v${VERSION}/" HEAD > "$TMP/source.tar"
gzip -n -9 < "$TMP/source.tar" > "$ARCHIVE"

mkdir "$TMP/extracted"
tar -xzf "$ARCHIVE" -C "$TMP/extracted"
STAGED="$TMP/extracted/kaidera-os-v${VERSION}"
FITNESS_ROOT="$STAGED" bash "$STAGED/scripts/fitness/check-community-source-boundary.sh"
FITNESS_OSS_SCAN_ROOT="$STAGED" bash "$STAGED/scripts/fitness/check-oss-package-hygiene.sh"
python3 -m compileall -q "$STAGED/local-cortex/console/app" "$STAGED/redistributable/scripts"
bash -n "$STAGED/install.sh" "$STAGED/update.sh"

(
  cd "$OUT"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256"
  else
    shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256"
  fi
)

if [ -n "${MINISIGN_SECKEY:-}" ]; then
  command -v minisign >/dev/null 2>&1 || {
    echo "MINISIGN_SECKEY is set but minisign is unavailable" >&2
    exit 1
  }
  minisign -S -s "$MINISIGN_SECKEY" -m "$ARCHIVE" -x "$ARCHIVE.minisig"
elif [ "${KAIDERA_REQUIRE_MINISIGN:-0}" = "1" ]; then
  echo "KAIDERA_REQUIRE_MINISIGN=1 but MINISIGN_SECKEY is unset" >&2
  exit 1
fi

printf 'community archive: %s\n' "$ARCHIVE"
cat "$ARCHIVE.sha256"
