var e=`# Getting started with Kaidera OS

Kaidera OS is the console + Cortex platform that runs AI workers for your projects.
This guide gets you from a fresh machine to a working console in minutes.

## What you are running
- **Console** — the FastAPI app (\`app.main:app\`) served by uvicorn. The UI is a
  React/Vite SPA plus legacy HTML surfaces. It hosts settings, AI workers,
  dispatch, runs, and the orchestrator.
- **Cortex API** — the memory backend (Postgres-backed) the console reads/writes.
- **App-DB** — the console's operational store (settings, agent overrides,
  run-state, project flags). On a Mac dev setup it runs in Docker on \`localhost:5500\`.
- **Harnesses** — Claude Code, Codex, and PI run as host subscription CLIs;
  Kaidera OS runs connected provider models through the in-process API lane.

## 1. Install
- Clone the repo and run the canonical installer:
  \`\`\`bash
  cd kaidera-os
  ./install.sh
  \`\`\`
- The installer creates the Python venv, preserves an existing Cortex/app-DB,
  builds or verifies the bundled SPA, writes \`run-kaidera-os-console.sh\`, and
  installs the canonical \`ai.kaidera.kaidera-os.console\` LaunchAgent.
- To refresh the bundled UI during development:
  \`\`\`bash
  cd local-cortex/console/spa
  npm install
  npm run build
  \`\`\`
- On macOS, \`install.sh\` also disables the old
  \`ai.adaptech.kaidera.console\` LaunchAgent if present, so a stale legacy
  runner cannot restart after the Kaidera OS cutover.

## 2. Start the console
- Mac (managed):
  \`\`\`bash
  launchctl kickstart -k gui/$(id -u)/ai.kaidera.kaidera-os.console
  \`\`\`
- Manual:
  \`\`\`bash
  ./run-kaidera-os-console.sh
  \`\`\`
- Confirm it is up:
  \`\`\`bash
  curl -s http://localhost:8765/healthz      # {"status":"ok","version":"..."}
  curl -s http://localhost:8765/console/version
  \`\`\`

## 3. Sign in
- In local dev mode (\`KAIDERA_DEPLOY_MODE=dev\`) auth is OFF —
  the console is a single-operator tool and you are admitted automatically.
- In hosted mode auth is ON. Sign in with the email magic-link
  (\`/auth/email/request\` + \`/auth/email/verify\`).
- The first admin can be bootstrapped with \`KAIDERA_AUTH_BOOTSTRAP_TOKEN\`.

## 4. The layout
- **Dashboard** — project status, repo root, active epic, recent activity, Cortex health.
- **Agents column** — the roster grouped Interactive vs AI Workers; pick an agent to
  open its pane (chat for interactive, run transcript for non-interactive workers).
- **Dispatch** — the open-handoff queue with proposed agents + Approve & Run.
- **Runs** — live run-state pane (the worker pane) streamed over \`/runstate/stream\`.
- **Settings** — System, Cortex, Workspace, and Extensions.
- **Help** — this guide.

## 5. Before you configure anything
1. Confirm Cortex is healthy (Settings → Cortex, or Dashboard). A red Cortex read
   breaks graph, history, explain, and dashboard counts — fix it first.
2. Confirm the app-DB is connected (Settings → System shows \`store_connected\`).
3. Confirm the model/harness route you plan to use is available in your edition.

## Prerequisites checklist
- [ ] Console reachable on \`:8765\`
- [ ] Cortex API reachable on \`:8501\` (\`/health\` → \`healthy\`)
- [ ] App-DB reachable on \`:5500\` (Settings → System \`store_connected: true\`)
- [ ] At least one allowed harness/model route available for your edition
- [ ] A project folder to bring online (see the First Project guide)

## Gotchas
- **Auth is fail-closed by default.** A console with no \`KAIDERA_DEPLOY_MODE\` signal
  is treated as untrusted (auth ON). Local dev must declare \`KAIDERA_DEPLOY_MODE=dev\`.
- **The console must use its generated runner.** A bare \`uvicorn\` start without the
  generated environment leaves auth ON and the app-DB DSN unset, so every read
  401s / degrades.
- **Harness CLIs are host-side.** The slim console container does not ship claude-code
  or pi; it shells the host. Project dispatch needs them on PATH (or the remote
  harness-service with \`HARNESS_SPAWN_MODE=remote\`).

## Search keywords
install, run, start, login, layout, console, first run, setup, launchd, uvicorn, healthz
`;export{e as default};