# Community Release Artifacts

Kaidera OS Community releases are source archives published from
[`Kaidera-AI/kaidera-os`](https://github.com/Kaidera-AI/kaidera-os).

Each release contains:

- `kaidera-os-vX.Y.Z.tar.gz`
- `kaidera-os-vX.Y.Z.tar.gz.sha256`
- `kaidera-os-vX.Y.Z.tar.gz.minisig` when the release signing key is available
- release notes generated from the matching changelog entry

The archive is built with `git archive` from an exact commit after the community
source-boundary, privacy, tests, and package fitness gates pass. Local state,
credentials, customer data, provider integrations, licensing code, and native
commercial packaging are excluded.

The Homebrew, npm, and curl launchers download this same archive and verify its
SHA-256. The supported commercial macOS installer is distributed separately at
[kaidera.ai](https://kaidera.ai/downloads/kaidera-os/macos).
