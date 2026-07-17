# Building the Kaidera OS Community Console

The console is a FastAPI backend with a React SPA. Community builds run in a
browser and do not include the private native macOS operator or installer sources.

## Backend

```sh
cd local-cortex/console
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

The Cortex API is expected at `http://localhost:8501`. The console remains usable
with degraded empty states when Cortex is unavailable.

## SPA

```sh
cd local-cortex/console/spa
npm ci
npm test -- --run
npm run typecheck
npm run lint
npm run build
```

The production bundle is written to `spa/dist`. Release archives include the
committed bundle so installation does not require Node.js.

## Full QA

From the repository root:

```sh
make qa
```

This runs Python, shell, Cortex, console, SPA, privacy, and community-boundary
checks.

## Release Archive

```sh
scripts/release/build-community-release.sh 0.1.233
```

The builder accepts an exact semantic version, requires a clean committed tree,
runs release gates, and writes a versioned source archive plus SHA-256 sidecar.
When `MINISIGN_SECKEY` is configured it also writes a detached minisign signature.

Commercial macOS packages are built from the private engineering repository and
published through kaidera.ai.
