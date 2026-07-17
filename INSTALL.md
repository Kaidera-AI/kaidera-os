# Install Kaidera OS Community

This guide installs the AGPL community source. The commercial macOS package is a
separate distribution available from
[kaidera.ai](https://kaidera.ai/downloads/kaidera-os/macos).

## Prerequisites

- macOS or Linux
- Docker Engine with Docker Compose v2, or Docker Desktop
- Python 3.11 or newer
- `curl`, `tar`, and either `sha256sum` or `shasum`
- At least one separately installed and authenticated AI CLI harness for AI work:
  Claude Code, Codex, or PI

The installer checks Docker, Compose, Python, disk space, and available harnesses
before provisioning services. Kaidera OS can start without an AI harness; workers
remain unavailable until one is installed.

## Homebrew

```sh
brew install kaidera-ai/kaidera/kaidera-os
kaidera-os install
kaidera-os start
```

## npm

```sh
npm install --global @kaidera/kaidera-os
kaidera-os install
```

## curl

```sh
curl -fsSL https://raw.githubusercontent.com/Kaidera-AI/homebrew-kaidera/main/install.sh | bash
```

The launcher downloads a versioned archive from the public Kaidera OS repository,
verifies its SHA-256 sidecar, extracts it under `~/kaidera-os` by default, and runs
`install.sh`.

## Source Checkout

```sh
git clone https://github.com/Kaidera-AI/kaidera-os.git
cd kaidera-os
./install.sh
```

The installation provisions Cortex and the operational database in Docker, creates
the native Python console environment, builds or uses the committed SPA bundle,
and configures a background service. Existing named data volumes and local secrets
are preserved on repeated runs.

## Open the Console

The default address is <http://127.0.0.1:8765>. Local macOS installs do not require
email sign-in by default. Hosted or shared installs enable authentication and should
be placed behind HTTPS.

Install and sign in to a supported external harness before assigning it to a worker.
Model and effort selectors are populated from each installed harness at runtime.

## Update

```sh
cd ~/kaidera-os
./update.sh
```

The updater verifies the release, applies it without deleting the database, restarts
the console, and checks the running version and Cortex admin-token wiring.

## Optional Local Workers

Enable local semantic indexing:

```sh
KAIDERA_CORTEX_LOCAL_EMBED=1 ./install.sh
```

Enable the larger audio and vision profile:

```sh
KAIDERA_CORTEX_PROFILE=full ./install.sh
```

The full profile requires substantial disk and memory headroom.

## Remove

```sh
./uninstall.sh
```

Read the script prompts carefully before removing persistent Cortex volumes.
