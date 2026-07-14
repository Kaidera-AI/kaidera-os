#!/usr/bin/env bash
# Stage the Mac operator release files for a static website or deploy pipeline.
#
# This intentionally does not know about a specific website repo. Kaidera OS owns
# artifact production and verification; deployment targets copy the staged files.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION_FILE="$ROOT/local-cortex/console/app/version.py"
SRC_DIR="${KAIDERA_OS_OPERATOR_DIST:-$ROOT/dist/macos}"
DEFAULT_OUT_DIR="$ROOT/output/release/kaidera-os-operator-macos"
OUT_DIR="${KAIDERA_OS_OPERATOR_PUBLISH_DIR:-$DEFAULT_OUT_DIR}"
CLEAN_MODE="auto"

usage(){
  cat <<'EOF'
Usage: scripts/macos/stage-operator-publication.sh [--clean|--no-clean] [output-dir]

Stages the verified Kaidera OS Operator Mac release files for publication.

Cleaning defaults:
  - default output dir: clean old staged operator files first
  - custom output dir: do not clean unless --clean is passed
EOF
}

say(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }
die(){ printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --clean)
      CLEAN_MODE="yes"
      shift
      ;;
    --no-clean)
      CLEAN_MODE="no"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      die "unknown option: $1"
      ;;
    *)
      OUT_DIR="$1"
      shift
      [ "$#" -eq 0 ] || die "only one output directory may be provided"
      ;;
  esac
done

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

DMG_NAME="kaidera-os-operator-v${VERSION}.dmg"
DMG_PATH="$SRC_DIR/$DMG_NAME"
SHA_PATH="$DMG_PATH.sha256"
META_PATH="$DMG_PATH.metadata.json"
LATEST_PATH="$SRC_DIR/latest-macos.json"

say "1/3 Verify release files"
[ -f "$DMG_PATH" ] || die "missing DMG: $DMG_PATH; run scripts/macos/build-operator-dmg.sh first"
[ -f "$SHA_PATH" ] || die "missing SHA file: $SHA_PATH"
[ -f "$META_PATH" ] || die "missing metadata: $META_PATH"
[ -f "$LATEST_PATH" ] || die "missing latest alias: $LATEST_PATH"

python3 - "$VERSION" "$DMG_PATH" "$SHA_PATH" "$META_PATH" "$LATEST_PATH" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

version = sys.argv[1]
dmg = Path(sys.argv[2])
sha_file = Path(sys.argv[3])
metadata_file = Path(sys.argv[4])
latest_file = Path(sys.argv[5])

digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
recorded_sha = sha_file.read_text(encoding="utf-8").split()[0]
if recorded_sha != digest:
    raise SystemExit(f"SHA mismatch: {sha_file} has {recorded_sha}, computed {digest}")

metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
latest = json.loads(latest_file.read_text(encoding="utf-8"))

expected = {
    "version": version,
    "artifact": dmg.name,
    "sha256": digest,
    "size_bytes": dmg.stat().st_size,
}
for field, value in expected.items():
    if metadata.get(field) != value:
        raise SystemExit(f"metadata {field} mismatch: expected {value!r}, got {metadata.get(field)!r}")
    if latest.get(field) != value:
        raise SystemExit(f"latest {field} mismatch: expected {value!r}, got {latest.get(field)!r}")

if latest != metadata:
    raise SystemExit("latest-macos.json must match the versioned metadata sidecar")
PY
ok "artifact, SHA, metadata, and latest alias agree"

say "2/3 Stage publication bundle"
mkdir -p "$OUT_DIR"
SHOULD_CLEAN=0
if [ "$CLEAN_MODE" = "yes" ]; then
  SHOULD_CLEAN=1
elif [ "$CLEAN_MODE" = "auto" ] && [ "$OUT_DIR" = "$DEFAULT_OUT_DIR" ]; then
  SHOULD_CLEAN=1
fi

if [ "$SHOULD_CLEAN" = "1" ]; then
  find "$OUT_DIR" -maxdepth 1 -type f \( \
    -name 'kaidera-os-operator-v*.dmg' -o \
    -name 'kaidera-os-operator-v*.dmg.sha256' -o \
    -name 'kaidera-os-operator-v*.dmg.metadata.json' -o \
    -name 'latest-macos.json' -o \
    -name 'publication-manifest.json' -o \
    -name 'README.md' \
  \) -exec rm -f {} +
  ok "removed stale staged operator files from $OUT_DIR"
fi
cp "$DMG_PATH" "$OUT_DIR/"
RECORDED_SHA="$(awk '{print $1}' "$SHA_PATH")"
printf '%s  %s\n' "$RECORDED_SHA" "$DMG_NAME" > "$OUT_DIR/$DMG_NAME.sha256"
cp "$META_PATH" "$OUT_DIR/"
cp "$LATEST_PATH" "$OUT_DIR/"

cat > "$OUT_DIR/README.md" <<EOF
# Kaidera OS Operator Mac Release

Staged from the canonical Kaidera OS repository.

- Version: $VERSION
- DMG: $DMG_NAME
- SHA-256: $DMG_NAME.sha256
- Metadata: $DMG_NAME.metadata.json
- Latest metadata alias: latest-macos.json

Publish these files together under the Mac operator download path. The
versioned filenames are immutable release artifacts; update
\`latest-macos.json\` to point customers at the current Mac operator.
EOF
ok "staged files in $OUT_DIR"

say "3/3 Publication manifest"
python3 - "$OUT_DIR" "$VERSION" "$DMG_NAME" <<'PY'
import json
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
version = sys.argv[2]
dmg_name = sys.argv[3]
files = [
    dmg_name,
    f"{dmg_name}.sha256",
    f"{dmg_name}.metadata.json",
    "latest-macos.json",
    "README.md",
]
manifest = {
    "product": "Kaidera OS Operator",
    "channel": "macos",
    "version": version,
    "files": files,
    "publish_contract": "copy all files to the static Mac operator download path",
}
(out_dir / "publication-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
ok "wrote $OUT_DIR/publication-manifest.json"
ok "publication bundle ready"
