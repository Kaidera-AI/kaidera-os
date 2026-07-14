# Secure distribution and install

Kaidera OS public releases are published through
[`Kaidera-AI/homebrew-kaidera`](https://github.com/Kaidera-AI/homebrew-kaidera).
Each release carries a versioned source archive, SHA-256 sidecar, and minisign
signature. macOS disk images are also Developer ID signed and notarized.

## Trust model

The signed bootstrap verifies two properties before extracting or running code:

1. **Authenticity:** minisign validates the archive against the public key embedded
   in `bootstrap.sh`.
2. **Integrity:** SHA-256 detects corruption or replacement in transit.

The release assets are public. GitHub authentication is not required for install.
Provider credentials, license-session tokens, and customer data are never release
assets.

## Install a public release

Homebrew, npm, and the standard curl installer are documented in the
[distribution repository](https://github.com/Kaidera-AI/homebrew-kaidera).

For explicit minisign verification, install `curl` and `minisign`, then run:

```sh
curl -fsSL \
  https://github.com/Kaidera-AI/homebrew-kaidera/releases/latest/download/bootstrap.sh \
  -o bootstrap.sh
bash bootstrap.sh
```

The bootstrap resolves the latest public tag, downloads the archive plus both
verification sidecars, verifies them, synchronizes the application into
`~/kaidera-os`, and runs `install.sh`.

Pin a release or change the install destination when needed:

```sh
KAIDERA_RELEASE=v0.1.231 KAIDERA_DEST="$HOME/kaidera-os-0.1.231" \
  bash bootstrap.sh
```

## Publish a release

Publishing requires maintainer access, GitHub CLI authentication, and the protected
minisign private key:

```sh
brew install minisign gh
gh auth login
bash dist/setup-signing.sh
dist/release.sh
```

`dist/release.sh` refuses a dirty worktree, runs package completeness and clean-room
checks, builds from committed source, bakes the public edition, writes the generated
edition marker and manifest, scans for credentials, verifies the archive, signs it,
and publishes to the repository selected by `KAIDERA_REPO`.

Never expose the minisign private key to untrusted pull-request code. Keep it
password-protected and backed up offline. For automation, use a protected release
environment with explicit owner approval.

## Source correspondence

Every object release must identify the exact public-source commit used to build it
and offer equivalent access to the corresponding source. Public source lives at
[`Kaidera-AI/kaidera-os`](https://github.com/Kaidera-AI/kaidera-os) under
`AGPL-3.0-only`.
