# Local Cortex Quickstart

Cortex is the shared memory, graph, event, and coordination component included
with Kaidera OS Community. Its name is permanent and independent of product names.

## Install the Stack

From the Kaidera OS repository root:

```sh
./install.sh
```

The installer starts Cortex, Postgres, graph and PDF workers, and the operational
database with Docker Compose. It then creates the native Python console environment
and starts the console as a background service.

Required prerequisites are Docker with Compose v2 and Python 3.11 or newer. AI
execution additionally needs a separately installed and authenticated Claude Code,
Codex, or PI CLI.

## Create a Project Configuration

Validate the blank example:

```sh
python3 redistributable/scripts/validate-cortex-project-config.py \
  redistributable/examples/blank.project.json
```

Preview startup changes:

```sh
python3 redistributable/scripts/cortex_startup_wizard.py \
  --config redistributable/examples/blank.project.json \
  --root /absolute/path/to/project \
  --dry-run --diff
```

Apply and register:

```sh
python3 redistributable/scripts/cortex_startup_wizard.py \
  --config redistributable/examples/blank.project.json \
  --root /absolute/path/to/project \
  --apply
```

The wizard writes project runtime and workspace configuration, registers the
project and workers through the Cortex API, and verifies lead bootstrapping. It
does not request or store model-provider credentials.

## Verify

```sh
curl -fsS http://127.0.0.1:8501/health
curl -fsS http://127.0.0.1:8765/console/version
cortex-boot lead
```

Open <http://127.0.0.1:8765> and select the project. The Dashboard, Graph,
History, and Cortex settings views should all show project-scoped data.

## Optional Workers

Local semantic indexing:

```sh
KAIDERA_CORTEX_LOCAL_EMBED=1 ./install.sh
```

Audio and vision enrichment:

```sh
KAIDERA_CORTEX_PROFILE=full ./install.sh
```

The full profile requires substantially more disk and memory.

## Local State

- `local-cortex/.env` stores generated local service secrets and is gitignored.
- `.agents/config/` stores generated project runtime configuration.
- Cortex data lives in Docker named volumes.
- Project source remains in the canonical workspace selected during registration.

Never commit local state, credentials, generated identities, chat histories, or
customer project content to the Kaidera OS source repository.
