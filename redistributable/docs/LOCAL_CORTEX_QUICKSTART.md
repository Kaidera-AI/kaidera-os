# Kaidera OS — Fresh-Install Quickstart

Status: current for the signed-release install path
Audience: operator standing up a fresh self-contained Kaidera OS deployment (a laptop or a fresh Linux VM)
Scope: prerequisites → install → what gets deployed → first run → using the Cortex CLI → re-deploy/cleanup.

This is the Kaidera OS redistributable: a native console (uvicorn) on the host plus the
Cortex 6-layer memory stack and the app-DB in containers. The default `kaidera` harness calls
provider APIs directly with keys you enter in the console's Settings — no per-harness CLI required.

---

## Prerequisites

- **Docker + Compose v2** — Docker Engine (Linux) or Docker Desktop (macOS), with the
  `docker compose` v2 plugin. The daemon must be running.
- **Python 3.11+** — for the native console venv.
- **~5 GB free disk for the default stack** — provider-backed Cortex embeddings are used by
  default. Keep **~15 GB+** if you opt into the local sentence-transformer embed worker or the
  full multimodal profile.
- **`herdr` CLI — OPTIONAL** — external terminal/runtime dependency for the explicit
  `herdr-visible` backend. It is not bundled in the Kaidera OS package.
- **`claude` CLI — OPTIONAL** — only needed if you want the Claude-subscription harness. The
  default `kaidera` API-key harness works without it.

The installer checks required prerequisites and fails loud with a one-line fix if one is missing.

---

## Install

Install from the signed release artifact. This is the canonical deployment path:
all deployments receive the same verified package and `bootstrap.sh` prunes stale
release-managed files while preserving local runtime state.

```bash
KAIDERA_OS_GITHUB_REPOSITORY=owner/repository
gh release download -R "$KAIDERA_OS_GITHUB_REPOSITORY" -p bootstrap.sh -O bootstrap.sh
bash bootstrap.sh
```

Useful options (all env-prefixed, all optional):

```bash
# Expose the console on a VPN / LAN (e.g. Tailscale) instead of localhost-only.
# Hosted/shared installs should also enable first-party auth and TLS.
KAIDERA_CONSOLE_HOST=0.0.0.0 ./install.sh

# Enable first-party passwordless auth for hosted/shared installs.
KAIDERA_AUTH_ENABLED=1 KAIDERA_AUTH_SECRET="$(openssl rand -base64 32)" ./install.sh

# Serve publicly over HTTPS: install.sh stands up a Caddy reverse proxy for the host (Step 4c).
# It picks the TLS mode for you and `caddy validate`s the rendered config BEFORE touching the
# live one (it never installs an unparseable Caddyfile or takes a working Caddy down). Precedence:
KAIDERA_PUBLIC_HOST=app.example.com ./install.sh                       # → tls internal (DEFAULT,
                                                                        #   self-signed; correct
                                                                        #   behind Cloudflare "Full")
KAIDERA_PUBLIC_HOST=app.example.com KAIDERA_TLS_EMAIL=you@example.com ./install.sh   # → Let's Encrypt
KAIDERA_PUBLIC_HOST=app.example.com \
  KAIDERA_TLS_CERT=/etc/caddy/origin.crt KAIDERA_TLS_KEY=/etc/caddy/origin.key ./install.sh  # → your
                                                                        #   origin cert (Cloudflare
                                                                        #   Full-strict)

# Start the local sentence-transformer embed fallback (OFF by default; provider embeddings are default).
KAIDERA_CORTEX_LOCAL_EMBED=1 ./install.sh

# Also start the multimodal audio + vision L5 workers (OFF by default; vision pulls an ~8 GB model).
KAIDERA_CORTEX_PROFILE=full ./install.sh
```

The host choice is remembered in `local-cortex/.console-host`, so a later plain re-run won't
silently revert a VPN-exposed install back to localhost.

### Login-email delivery (hosted/shared installs)

Local macOS localhost installs run without first-party email auth by default. This is intentional:
the Mac operator package is a private desktop app, not a public web deployment. To require login on
Mac, or for any hosted/shared install, set `KAIDERA_AUTH_ENABLED=1`.

When first-party auth is on, the console emails a one-time sign-in **code + link** to the user.
`KAIDERA_AUTH_EMAIL_DELIVERY` picks how that email goes out:

- `log` (default when auth is on) — the code is written to the console journal only; no email is sent.
- `smtp` — classic SMTP (`KAIDERA_SMTP_HOST` / `KAIDERA_SMTP_FROM`, optional
  `KAIDERA_SMTP_PORT` / `KAIDERA_SMTP_USER` / `KAIDERA_SMTP_PASSWORD` / `KAIDERA_SMTP_TLS`).
- `graph` — **Microsoft 365 Graph, app-only (client-credentials)**, for tenants that lock down
  SMTP AUTH. It is auto-selected when `KAIDERA_AUTH_GRAPH_CLIENT_SECRET` is set, or force it with
  `KAIDERA_AUTH_EMAIL_DELIVERY=graph`. Required env:

  ```bash
  KAIDERA_AUTH_EMAIL_DELIVERY=graph
  KAIDERA_AUTH_GRAPH_TENANT_ID=<azure-ad-tenant-id>
  KAIDERA_AUTH_GRAPH_CLIENT_ID=<app-registration-client-id>
  KAIDERA_AUTH_GRAPH_CLIENT_SECRET=<app-registration-secret-value>   # secret — never commit
  KAIDERA_AUTH_GRAPH_SENDER=<mailbox-the-app-sends-as>               # e.g. noreply@yourdomain
  ```

  The Azure AD app registration needs the **Mail.Send** *application* permission **with admin
  consent**. To restrict which mailbox the app may send as, attach an Exchange
  `ApplicationAccessPolicy` scoped to the sender. The console acquires an app token from
  `login.microsoftonline.com/<tenant>/oauth2/v2.0/token` and sends via Graph `sendMail`
  (expects HTTP 202). Missing config or a Graph error fails loud with a secret-free message in
  the journal — the secret and token are never logged.

**`install.sh` wires these for you.** Set the SMTP **or** Graph vars in the install environment, or
drop them into `local-cortex/.env` (the file `install.sh` already manages), and the installer injects
them into the generated systemd unit + runner — exactly as it does `KAIDERA_AUTH_SECRET` — so no
hand-authored dropin is needed. Only the vars you set are injected; unset ones are omitted.

Packaged SMTP defaults are staged in `local-cortex/.env` as a commented, non-secret template.
They are intentionally disabled until the operator supplies a mailbox and credential:

```bash
KAIDERA_AUTH_EMAIL_DELIVERY=smtp
KAIDERA_SMTP_HOST=smtp-relay.gmail.com
KAIDERA_SMTP_PORT=587
KAIDERA_SMTP_TLS=1
KAIDERA_SMTP_FROM=noreply@yourdomain.example
KAIDERA_SMTP_USER=noreply@yourdomain.example
KAIDERA_SMTP_PASSWORD=<relay-password> # secret — never commit or ship in the package
```

Graph example:

```bash
KAIDERA_AUTH_ENABLED=1 \
KAIDERA_AUTH_EMAIL_DELIVERY=graph \
KAIDERA_AUTH_GRAPH_TENANT_ID=… KAIDERA_AUTH_GRAPH_CLIENT_ID=… \
KAIDERA_AUTH_GRAPH_CLIENT_SECRET=… KAIDERA_AUTH_GRAPH_SENDER=wren@yourdomain \
./install.sh
```

Secrets are read into the unit/runner but never echoed to the console. With no email configured the
default `log` delivery writes the code to the journal (`journalctl -u kaidera-os-console | grep -i
'sign-in code'`) — enough to bootstrap the **first admin** (the first email to sign in becomes admin).

---

## What gets deployed

| Piece | Where | What it is |
|---|---|---|
| **Native console** | host (uvicorn, systemd on Linux) | the web UI + API on `:8765` |
| **Cortex 6-layer stack** | containers | agent memory + coordination (`cortex-pg`, `cortex-api`, graph/pdf workers, provider-backed embeddings) |
| **App-DB** | container | the operational store (`harness-appdb` on `:5500`) |
| **Cortex CLI** | host PATH | the `cortex-*` commands (`cortex-boot`, `cortex-handoff`, `cortex-log`, …) |
| **Herdr runtime** | host PATH or `KAIDERA_OS_HERDR_BIN` | optional external upstream runtime for the `herdr-visible` backend |

A fresh Cortex DB bootstraps its schema from the committed `cortex-schema-full.sql` (it is born
with the normalized identity, no legacy data, and a freshly generated admin token). The local
sentence-transformer embed worker is opt-in (`KAIDERA_CORTEX_LOCAL_EMBED=1`) because the default
Cortex embedding path uses the configured provider model. The audio + vision workers are opt-in
(`KAIDERA_CORTEX_PROFILE=full`, which also starts the local embed fallback); everything else starts
by default.

On Linux the console installs as a `systemd` service (`kaidera-os-console`) that auto-starts on boot:

```bash
systemctl status|restart|stop kaidera-os-console
journalctl -u kaidera-os-console -f          # logs
```

On macOS run it in the foreground with the generated `./run-kaidera-os-console.sh`.

Herdr is an optional external prerequisite, not vendored source or a bundled binary. The package verifier checks
`redistributable/scripts/verify-herdr-runtime.py`, and generated runners export the resolved
binary path as `KAIDERA_OS_HERDR_BIN`. Direct runtime remains the default until the product
cutover gate; to opt into the visible runtime explicitly:

```bash
KAIDERA_OS_RUNTIME_BACKEND=herdr-visible KAIDERA_OS_ENABLE_HERDR_VISIBLE=1 ./run-kaidera-os-console.sh
```

---

## First run

1. **Open the console.**
   - On this machine: `http://127.0.0.1:8765`
   - If you installed with `KAIDERA_CONSOLE_HOST=0.0.0.0`, the installer prints the VPN/LAN URL.
2. **Add at least one provider key** — Settings → Providers, add an API key (e.g. Ollama Cloud
   for kimi, or OpenAI / Anthropic). The default `kaidera` harness needs no CLI; it calls the
   provider directly with this key.
3. **Get Started** — create your project and **name your first worker**. No agents are pre-seeded;
   you name your roster from the console.

---

## Drop in a turnkey project (a profile)

A *profile* is one small DATA file — `<your_project_key>.profile.json` — that the harness LOADS to
configure + run a dropped-in project (designations, default-project hint, and an optional
single-agent **portal persona**). The harness itself names no project and no worker; the profile
supplies all of that.

**Author it from the shipped template.** The generic template is
`redistributable/examples/example.profile.json`. The loader resolves the profile
**by the active project's key** — `load_profile("<key>")` reads
`<key>.profile.json` — so the file MUST be named for your live project key:

```bash
# Use the exact key you registered for the project.
cd redistributable/examples
cp example.profile.json your-project-key.profile.json
# then edit your-project-key.profile.json: set "project", optional "default_project",
# and optional portal settings to your project key and package-owned persona file.
```

A profile named for the wrong key will **not** resolve for the active project.
That key mismatch is the single most common turnkey snag.

**The persona is materialized automatically.** When the profile is named for the active project and
its `portal` block carries a `persona` (inline) or `persona_file`, the chat path picks it up via
`build_agent_persona()` — so the package worker chats **in-persona** with **no**
hand-authored `agents/<NAME>_IDENTITY.md`. A hand-authored identity file, if
present, still takes precedence; otherwise the profile persona is the source.
(Override the profiles dir with `KAIDERA_PROFILES_DIR`; it defaults to the
shipped `redistributable/examples/`.)

---

## Using the Cortex CLI

`install.sh` puts the `cortex-*` CLI on your PATH (system-wide via `/etc/profile.d/kaidera-cortex.sh`,
or appended to your shell rc as a fallback). Open a **new shell** (or `source` the drop-in the
installer names), then:

```bash
cortex-boot <name>                 # session start — identity + context for an agent
cortex-handoff --mine <name>       # your pending handoffs
cortex-log <name> decision "..."   # log a decision / lesson
```

---

## Re-deploy / cleanup

`./uninstall.sh` is a **greenfield wipe** — it removes the console service, the Cortex + app-DB
containers **and their data volumes** (all agent memory + app-DB rows), the venv, and every
install-generated file (admin token, host memo, run script, systemd unit, the CLI PATH entry). It
does NOT touch git.

```bash
./uninstall.sh            # interactive — type "wipe" to confirm
./uninstall.sh --yes      # non-interactive (scripted / CI)
```

For a born-clean reinstall of the latest code:

```bash
./uninstall.sh --yes
KAIDERA_OS_GITHUB_REPOSITORY=owner/repository
gh release download -R "$KAIDERA_OS_GITHUB_REPOSITORY" -p bootstrap.sh -O bootstrap.sh
bash bootstrap.sh         # or: KAIDERA_CONSOLE_HOST=0.0.0.0 bash bootstrap.sh
```

A fresh install rebuilds the container images so the new code takes effect and bootstraps a fresh
Cortex DB from the committed schema.

---

## Redist follow-ups (known gaps — not yet turnkey)

These are captured from the 2026-06-18 clean-slate dogfood
(`docs/2026-06-18-redist-dogfood-rebuild.md`). They do not block a working install; they are the
remaining hand-steps a turnkey drop-in still needs:

- **Project pack application hardening (GAP #5 remainder).** The redist now ships a project-pack
  manifest contract (`redistributable/schema/cortex-project-pack.schema.json`), validator
  (`redistributable/scripts/validate-cortex-project-pack.py`), generic example pack
  (`redistributable/examples/project-pack-basic/project-pack.json`), and dry-run-by-default
  installer (`redistributable/scripts/cortex-project-pack install <pack> --target <project-root>
  [--apply]`). Use this to validate and copy brand/voice/connector/seed DATA into a project-owned
  `.kaidera-os/project-packs/<pack>/` directory. Add Project can now scan that folder, select an
  installed pack, and import its Cortex seed files into the new project scope. Settings →
  Extensions shows declared modules, enable/disable helper state, loaded state, and restart-required
  drift. Customer/project DATA still does not ship in the Kaidera OS core.
- **Chat-history → Cortex LTM ingest (the LTM chat-history gap).** Authenticated chat turns persist
  durably to **run_state** (the app-DB; readable via `GET /runs/run/{run_id}` and the project run
  board) — but they are **not** auto-ingested into Cortex long-term memory, so a `cortex-search` over
  the chat content comes back empty. run_state is the authoritative persistence for now; wiring chat
  turns into Cortex LTM is a separate open item.

---

## Guardrails

- Local Mac installs are open by default because the macOS account is the local auth boundary.
  Hosted/shared installs must enable `KAIDERA_AUTH_ENABLED=1`, set `KAIDERA_AUTH_SECRET`, and put
  the console behind HTTPS.
- Provider keys live in the console's Settings (the app-DB), **never** in git, chat, or docs.
- Don't `psql` / `docker exec` into the Cortex DB for normal work — use the console or the CLI.
