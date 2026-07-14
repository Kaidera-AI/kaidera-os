# Cortex Runtime Profile

Date: 2026-05-17

## Purpose

The Cortex project registry is the runtime source of truth for project identity,
roots, default worker, roster, heartbeat settings, launchd metadata, setup hints,
and the local Postgres-only Cortex service shape. Local JSON files are
import/export bootstrap surfaces only.

Runtime consumers should call:

```bash
curl -s http://localhost:8501/projects/<project-key>/runtime | python3 -m json.tool
```

The response is consumed by heartbeat control, launchd rendering,
redistributable setup, and fresh-machine verification. Do not add project keys,
worker IDs, repo roots, launchd labels, or Redis setup constants as script
constants. The current runtime profile describes a Postgres-backed local Cortex
stack.

## Postgres-Only Stack Rule

After E75 Phase 2, the active local Cortex core stack is `cortex-pg` plus
`cortex-api`, with optional internal worker containers. Redis is absent from
the normal setup path: no runtime consumer should require `cortex-redis`, port
`6399`, `CORTEX_REDIS_URL`, Redis Python dependencies, `redis-cli`, or
`/admin/redis`. Historical Redis stream/cache state was explicitly accepted as
ephemeral during Inc 28.

## Runtime Fields

The runtime profile is expected to carry:

- Project key, display name, status, default worker, and roots.
- Worker roster records with names, roles, models, and capabilities.
- Heartbeat worker ID, cadence, launchd label, plist name, progress provider,
  and exported environment values.
- Warp/setup hints such as window prefix and which launch surfaces are enabled.

## Safe Import Rule

`cortex-sync-workspace` updates configured projects but no longer prunes projects
missing from the local JSON by default. Destructive cleanup requires the explicit
operator flag:

```bash
cortex-sync-workspace --prune-missing
```

This prevents one workspace's stale `workspace.json` from unregistering unrelated
projects.

## Heartbeat Runtime Contract

Heartbeat control resolves its runtime through `beat/runtime-profile.py`.

```bash
python3 beat/runtime-profile.py --json
python3 beat/runtime-profile.py --shell
```

The API profile wins when available. Runtime YAML and workspace JSON are only
bootstrap fallbacks for the period before the API container is reachable.

## Regression Proof

Use two separately registered test projects and confirm each runtime response
returns only its own project key, roster, default worker, and heartbeat metadata:

```bash
curl -s http://localhost:8501/projects/<project-a>/runtime | python3 -m json.tool
curl -s http://localhost:8501/projects/<project-b>/runtime | python3 -m json.tool
```

These profiles must remain separate. A runtime response for one project must not
include another project's root, roster, default worker, or heartbeat metadata.

## Open Consumers

Dashboard and startup wizard code should read this runtime endpoint directly.
Package setup consumes runtime identity and heartbeat launch metadata without
shipping any project-specific worker loops.
