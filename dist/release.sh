#!/usr/bin/env bash
# Build + SIGN + publish a release to the public distribution repository. Repeatable — run this
# for every version. Produces a tarball + SHA-256 + minisign signature, and uploads them
# (plus bootstrap.sh) as release assets the verifying installer pulls.
#
#   dist/release.sh [version]        # version defaults to app/version.py
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

DEFAULT_REPO="Kaidera-AI/homebrew-kaidera"
REPO="${KAIDERA_REPO:-$DEFAULT_REPO}"
VERSION="${1:-$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' local-cortex/console/app/version.py | head -1)}"
[ -n "$VERSION" ] || { echo "could not determine version"; exit 1; }
TAG="v$VERSION"
NAME="kaidera-os-$TAG"
SECKEY="${MINISIGN_SECKEY:-$HOME/.minisign/minisign.key}"
OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT

die() { echo "$*" >&2; exit 1; }

[ -n "$REPO" ] || die "KAIDERA_REPO is required (for example: Kaidera-AI/homebrew-kaidera)."
command -v minisign >/dev/null 2>&1 || die "minisign required (brew install minisign)"
command -v gh >/dev/null 2>&1       || die "gh required + 'gh auth login'"
[ -f "$SECKEY" ] || die "no signing key at $SECKEY — run dist/setup-signing.sh first"

# The release artifact is built from HEAD. Refuse a dirty tree so uncommitted fixes, generated
# assets, or export-ignore changes cannot be silently omitted from the package.
if ! git diff --quiet -- . || ! git diff --cached --quiet -- .; then
  die "working tree has uncommitted changes; commit or stash before release so HEAD matches the package"
fi
UNTRACKED="$(git ls-files --others --exclude-standard)"
if [ -n "$UNTRACKED" ]; then
  printf '%s\n' "$UNTRACKED" | sed 's/^/  untracked: /' >&2
  die "working tree has untracked files; commit, ignore, or remove them before release"
fi

# COMPLETENESS — a release tarball is `git archive HEAD`, so it carries ONLY committed files. Assert
# the commit has every artifact install.sh + the running app need (the SPA bundle, compose, schema,
# requirements…) so we can never publish the "SPA bundle missing"-class of redist again.
echo "== Redist completeness — every install/runtime artifact must be in the commit =="
bash scripts/fitness/check-redist-complete.sh \
  || { echo "✗ redist INCOMPLETE — a fresh install would break. Fix + commit. NOTHING published."; exit 1; }

# RUNTIME PROOF — boot the console from the committed tree in a bare container (the gold standard).
echo "== Clean-room boot proof — the committed tree must boot + serve the SPA on a blank machine =="
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  bash scripts/test-clean-install.sh \
    || { echo "✗ clean-room boot FAILED — a fresh install would not come up. NOTHING published."; exit 1; }
else
  echo "  ⚠ docker unavailable — SKIPPING the clean-room boot proof; run it before trusting this release."
fi

echo "== Building $NAME.tar.gz (git archive @ $(git rev-parse --short HEAD); export-ignore applies) =="
STAGE="$OUT/stage"
mkdir -p "$STAGE"
git archive --format=tar --prefix="$NAME/" HEAD | tar -xf - -C "$STAGE"
printf 'public\n' > "$STAGE/$NAME/.kaidera-os-edition"
python3 "$STAGE/$NAME/scripts/release/bake-public-edition.py" \
  "$STAGE/$NAME/local-cortex/console/app/edition.py"
(
  cd "$STAGE/$NAME"
  { find . -type f -print | sed 's#^\./##'; printf 'MANIFEST.txt\n'; } \
    | LC_ALL=C sort -u > MANIFEST.txt
)
COPYFILE_DISABLE=1 tar -czf "$OUT/$NAME.tar.gz" -C "$STAGE" "$NAME"

echo "== Secret scan — ZERO credentials may ship (defense in depth past export-ignore) =="
if tar -xzOf "$OUT/$NAME.tar.gz" 2>/dev/null \
   | grep -aIqE "(sk-[A-Za-z0-9]{20,}|fw_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xai-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"; then
  echo "✗ SECRET DETECTED in the release tarball — ABORTING."
  echo "  Add the offending path to .gitattributes (export-ignore) and re-run. NOTHING is published."
  exit 1
fi
echo "  ✓ no credential patterns in the tarball"

echo "== SHA-256 =="
( cd "$OUT" && {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$NAME.tar.gz";
    else shasum -a 256 "$NAME.tar.gz"; fi
  } > "$NAME.tar.gz.sha256" )

echo "== Redistributable verifier =="
python3 redistributable/scripts/verify-cortex-package.py \
  "$OUT/$NAME.tar.gz" \
  --report "$OUT/$NAME.verify.json" \
  || die "redistributable verifier failed; NOTHING published"

echo "== Signing (minisign) =="
minisign -S -s "$SECKEY" -m "$OUT/$NAME.tar.gz" -t "Kaidera OS console $TAG"

echo "== Publishing GitHub release $TAG to $REPO =="
gh release create "$TAG" \
  "$OUT/$NAME.tar.gz" "$OUT/$NAME.tar.gz.minisig" "$OUT/$NAME.tar.gz.sha256" "dist/bootstrap.sh" \
  -R "$REPO" --title "$TAG" --target "$(git rev-parse HEAD)" \
  --notes "Kaidera OS console $TAG - signed release (minisign + SHA-256). Install: dist/README.md."

echo ""
echo "Done. Install on a new PC:"
echo "  gh release download -R $REPO -p bootstrap.sh -O bootstrap.sh && bash bootstrap.sh"
