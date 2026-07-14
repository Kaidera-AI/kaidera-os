#!/usr/bin/env bash
# Kaidera OS local-deployment installer - NATIVE console + Cortex/DB containers.
#
# After this runs, the app is self-contained: a native console (uvicorn) on this host +
# the Cortex 6-layer + app-DB in containers. Works on macOS or a fresh Linux VM with no
# Mac-host dependency - the kaidera harness calls provider APIs directly with keys you
# enter in Settings. Herdr is installed as an external runtime prerequisite; it is
# not bundled in this repository or redistributable package.
# Design: docs/2026-06-13-selfcontained-redesign-plan.md
set -euo pipefail

# Resolve paths RELATIVE to this script — never hardcode a home dir (runs anywhere).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONSOLE_DIR="$REPO_ROOT/local-cortex/console"
CORTEX_COMPOSE="$REPO_ROOT/.agents/docker-compose.cortex.yml"
HERDR_INSTALLER="$REPO_ROOT/redistributable/scripts/install-herdr-runtime.sh"

# Tunables (env-overridable so the script carries no environment-specific literals).
COMPOSE_PROJECT="${KAIDERA_COMPOSE_PROJECT:-kaidera-os-cortex}"   # fitness:allow-literal product stack name
CONSOLE_PORT="${KAIDERA_CONSOLE_PORT:-8765}"
# Compose services that inspect local project files need a host path Docker can
# actually bind-mount. Project roots are intentionally user-selected and may live
# in DevVault, Google Drive, Dropbox, or another folder under the operator's home.
# The graph worker mount is read-only, so default to $HOME instead of the install
# parent; operators can still narrow/widen it with HOST_PROJECTS_ROOT.
HOST_PROJECTS_ROOT="${HOST_PROJECTS_ROOT:-${HOME:-$(dirname "$REPO_ROOT")}}"
CLAUDE_STATE_ROOT="${CLAUDE_STATE_ROOT:-${HOME:-$REPO_ROOT}/.claude/projects}"
export HOST_PROJECTS_ROOT CLAUDE_STATE_ROOT
# SAFE DEFAULT: 127.0.0.1 (localhost only). Kaidera OS local installs are open by default, so they
# must not be exposed on the network unless the operator also enables auth/TLS or uses a
# private network / VPN (e.g. Tailscale). To bind beyond localhost, OPT IN explicitly:
#     KAIDERA_CONSOLE_HOST=0.0.0.0 ./install.sh
# and firewall the port from the public internet.
#
# PERSISTED: the choice is remembered in local-cortex/.console-host so a LATER plain re-run (e.g.
# after an update) doesn't silently revert you to localhost and take the app off the network — the
# bug that kept biting on the VM. An explicit KAIDERA_CONSOLE_HOST always wins + updates the memo.
HOST_STATE="$REPO_ROOT/local-cortex/.console-host"
if [ -n "${KAIDERA_CONSOLE_HOST:-}" ]; then
  CONSOLE_HOST="$KAIDERA_CONSOLE_HOST"
  # Persist ONLY an EXPLICIT choice. We must NOT write the bare default below: a plain re-run with
  # no memo would otherwise persist 127.0.0.1 and LOCK the install to localhost (the revert that
  # kept taking the app off Tailscale). The memo now only ever holds a host the operator chose.
  printf '%s\n' "$CONSOLE_HOST" > "$HOST_STATE" 2>/dev/null || true
elif [ -f "$HOST_STATE" ]; then
  CONSOLE_HOST="$(tr -d '[:space:]' < "$HOST_STATE" 2>/dev/null || true)"
fi
CONSOLE_HOST="${CONSOLE_HOST:-127.0.0.1}"   # bare default — deliberately NOT written to the memo
# REVERSE-PROXY public URL: when the console runs BEHIND a TLS terminator (Caddy/nginx/Cloudflare),
# it must build login magic-links + post-login redirects against the PUBLIC origin, not the
# 127.0.0.1:PORT it actually binds. Without this the console saw the proxy as localhost and bounced
# operators to their own machine after login (a real reverse-proxy origin-mismatch we hit). OPT IN per deployment.
# TWO knobs (either or both):
#   KAIDERA_PUBLIC_HOST=app.example.com      # bare hostname; install.sh STANDS UP a Caddy TLS
#                                            # reverse proxy for it (Step 4c) + derives the base URL
#   KAIDERA_PUBLIC_BASE_URL=https://app.example.com   # full origin; sets console public-link origin
#                                            # only (you supply your own proxy)
# Empty defaults = direct/localhost install (unchanged behavior). When only PUBLIC_HOST is given we
# derive PUBLIC_BASE_URL=https://<host>; when only PUBLIC_BASE_URL is given we derive the host from it
# for the proxy step. We also pin KAIDERA_AUTH_ORIGIN to the base URL (passkey/WebAuthn origin) and
# always pass uvicorn --proxy-headers (harmless with no proxy: it only trusts X-Forwarded-* from
# FORWARDED_ALLOW_IPS, default the loopback proxy).
PUBLIC_HOST="${KAIDERA_PUBLIC_HOST:-}"
PUBLIC_BASE_URL="${KAIDERA_PUBLIC_BASE_URL:-}"
# Cross-derive the two so the operator only has to set one. Strip scheme + any path from a base URL
# to get the bare host; build an https base URL from a bare host.
if [ -z "$PUBLIC_HOST" ] && [ -n "$PUBLIC_BASE_URL" ]; then
  PUBLIC_HOST="${PUBLIC_BASE_URL#*://}"; PUBLIC_HOST="${PUBLIC_HOST%%/*}"
fi
if [ -z "$PUBLIC_BASE_URL" ] && [ -n "$PUBLIC_HOST" ]; then
  PUBLIC_BASE_URL="https://$PUBLIC_HOST"
fi
# TLS knobs for the Step 4c Caddy reverse proxy (precedence: CERT+KEY > EMAIL > `tls internal`).
#   KAIDERA_TLS_CERT + KAIDERA_TLS_KEY  -> serve YOUR cert (e.g. a Cloudflare origin cert at
#                                         /etc/caddy/origin.{crt,key}) for Cloudflare Full(strict).
#   KAIDERA_TLS_EMAIL                   -> Let's Encrypt auto-HTTPS (needs public :80 + :443 here).
#   neither (DEFAULT)                   → `tls internal` (self-signed; correct behind an upstream TLS).
TLS_EMAIL="${KAIDERA_TLS_EMAIL:-}"   # optional ACME contact email for the Caddy `tls` directive
TLS_CERT="${KAIDERA_TLS_CERT:-}"     # optional path to a TLS cert file (pair with KAIDERA_TLS_KEY)
TLS_KEY="${KAIDERA_TLS_KEY:-}"       # optional path to the matching private key file
FORWARDED_ALLOW_IPS="${KAIDERA_FORWARDED_ALLOW_IPS:-127.0.0.1}"
CORTEX_API_URL="${CORTEX_API_URL:-http://localhost:8501}"
APPDB_DSN="${HARNESS_APPDB_DSN:-postgresql://harness:harness@localhost:5500/harness_app}"
# Cortex + app-DB services only — the console runs NATIVE, so it is NOT in this list.
CORTEX_SERVICES=(cortex-pg cortex-api harness-appdb harness-appdb-migrate \
                 cortex-graph-worker cortex-pdf-worker)
# Provider-backed embeddings are the default path. The local sentence-transformer
# embed worker is a heavyweight fallback because it builds PyTorch; keep it
# opt-in so ordinary installs/updates do not export that large image.
CORTEX_LOCAL_EMBED="${KAIDERA_CORTEX_LOCAL_EMBED:-}"
case "$(printf '%s' "$CORTEX_LOCAL_EMBED" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) CORTEX_LOCAL_EMBED="1" ;;
  *) CORTEX_LOCAL_EMBED="0" ;;
esac
# Multimodal L5 enrichment (audio + vision) is OPT-IN: it adds heavy images
# (vision pulls an ~8 GB model) and is OFF by default. Set
# KAIDERA_CORTEX_PROFILE=full to start them - we both pass `--profile full`
# (so compose "sees" the profiled services) AND add the explicit service names
# below (so they actually start). Full profile also starts the local embed worker.
CORTEX_PROFILE="${KAIDERA_CORTEX_PROFILE:-}"
COMPOSE_PROFILE_ARGS=()
if [ "$CORTEX_PROFILE" = "full" ]; then
  CORTEX_LOCAL_EMBED="1"
fi
if [ "$CORTEX_LOCAL_EMBED" = "1" ]; then
  CORTEX_SERVICES+=(cortex-embed-worker)
fi
if [ "$CORTEX_PROFILE" = "full" ]; then
  CORTEX_SERVICES+=(cortex-audio-worker cortex-vision-worker)
  COMPOSE_PROFILE_ARGS=(--profile full)
fi

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
skip(){ printf '  \033[33m⏭\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. OS + dependency checks ------------------------------------------------------------
say "1/7 Dependencies"
OS="$(uname -s)"
case "$OS" in
  Linux)  ok "OS: Linux" ;;
  Darwin) ok "OS: macOS" ;;
  *)      die "Unsupported OS: $OS (Linux/macOS only)" ;;
esac
if [ -n "${KAIDERA_AUTH_ENABLED:-}" ]; then
  AUTH_ENABLED="$KAIDERA_AUTH_ENABLED"
elif [ "$OS" = "Darwin" ] && [ "$CONSOLE_HOST" = "127.0.0.1" ] && [ -z "$PUBLIC_BASE_URL" ]; then
  # Local Mac operator packages are private desktop apps, not public web apps.
  AUTH_ENABLED="0"
else
  # Hosted/shared installs stay fail-closed unless the operator explicitly opts out.
  AUTH_ENABLED="1"
fi
case "$(printf '%s' "$AUTH_ENABLED" | tr '[:upper:]' '[:lower:]')" in
  0|false|no|off) AUTH_ENABLED="0" ;;
  *) AUTH_ENABLED="1" ;;
esac
EDITION="${KAIDERA_OS_EDITION:-}"
EDITION_SOURCE="environment"
if [ -z "$EDITION" ]; then
  EDITION_MARKER="$REPO_ROOT/.kaidera-os-edition"
  if [ -f "$EDITION_MARKER" ]; then
    EDITION="$(tr -d '[:space:]' < "$EDITION_MARKER")"
    EDITION_SOURCE="release marker"
  elif [ -d "$REPO_ROOT/.git" ]; then
    EDITION="dev"
    EDITION_SOURCE="source checkout"
  else
    # An unpackaged, non-source tree must fail toward the redistributable posture.
    EDITION="public"
    EDITION_SOURCE="unmarked archive"
  fi
fi
case "$(printf '%s' "$EDITION" | tr '[:upper:]' '[:lower:]')" in
  public) EDITION="public" ;;
  dev) EDITION="dev" ;;
  "") die "Kaidera OS edition marker is empty (expected public or dev)" ;;
  *) die "Invalid KAIDERA_OS_EDITION='$EDITION' (expected public or dev)" ;;
esac
command -v docker >/dev/null 2>&1 || die "Docker not found — install Docker Engine (Linux) / Docker Desktop (macOS) first."
docker compose version >/dev/null 2>&1 || die "'docker compose' v2 plugin not found."
docker info >/dev/null 2>&1 || die "Docker daemon not running — start it and re-run."
ok "Docker + compose available"
if [ -z "${KAIDERA_COMPOSE_PROJECT:-}" ]; then
  _EXISTING_COMPOSE_PROJECT=""
  for _container in cortex-api cortex-pg harness-appdb cortex-graph-worker cortex-embed-worker cortex-pdf-worker; do
    _label="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$_container" 2>/dev/null || true)"
    if [ -n "$_label" ] && [ "$_label" != "<no value>" ]; then
      _EXISTING_COMPOSE_PROJECT="$_label"
      break
    fi
  done
  if [ -n "$_EXISTING_COMPOSE_PROJECT" ] && [ "$_EXISTING_COMPOSE_PROJECT" != "$COMPOSE_PROJECT" ]; then
    COMPOSE_PROJECT="$_EXISTING_COMPOSE_PROJECT"
    ok "adopting explicitly named existing Docker Compose project: $COMPOSE_PROJECT"
  fi
fi
ok "project bind root: $HOST_PROJECTS_ROOT"
if [ "$AUTH_ENABLED" = "0" ]; then
  ok "auth disabled for local/private console (set KAIDERA_AUTH_ENABLED=1 to require email sign-in)"
else
  ok "auth enabled (set KAIDERA_AUTH_ENABLED=0 only for private/local installs)"
fi
ok "edition: $EDITION ($EDITION_SOURCE)"
# Disk preflight. The default provider-backed stack is intentionally lighter
# because it no longer builds the PyTorch-based local embed worker. Warn loudly
# when operators opt into local embed/full multimodal mode because those images
# can fail HALFWAY on small disks (Errno 28). Non-fatal: df parsing varies and
# small DB/API deploys are valid.
FREE_GB="$(df -k / 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}')"
if [ -n "$FREE_GB" ] && [ "$CORTEX_LOCAL_EMBED" = "1" ] && [ "$FREE_GB" -lt 15 ]; then
  printf '\033[1;33m  ⚠ only %s GB free on / - local embedding/full multimodal mode needs ~15 GB+ headroom.\n     Resize the disk, or run the default provider-backed stack without KAIDERA_CORTEX_LOCAL_EMBED / KAIDERA_CORTEX_PROFILE=full.\033[0m\n' \
    "$FREE_GB"
elif [ -n "$FREE_GB" ] && [ "$FREE_GB" -lt 5 ]; then
  printf '\033[1;33m  ⚠ only %s GB free on / — even the default lightweight stack may run out of disk during image pulls/builds.\033[0m\n' "$FREE_GB"
else
  [ -n "$FREE_GB" ] && ok "disk: ${FREE_GB} GB free on /"
fi
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found — install Python 3.11+."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11+ required (found $("$PY" --version 2>&1)). Install a newer python3 and re-run."
ok "python3: $("$PY" --version 2>&1)"
if command -v claude >/dev/null 2>&1; then
  ok "claude-code CLI present — the Claude-subscription harness is available"
else
  skip "claude-code CLI not found - only needed for the Claude subscription; the kaidera API-key harness works regardless"
fi
# Herdr is the OPTIONAL "herdr-visible" runtime backend (a prototype for visible, pane-based
# execution). The console + the DEFAULT kaidera harness (provider APIs) work fully WITHOUT it, so a
# missing/unpinned Herdr must NOT block the install — best-effort, warn + continue. (Enable later:
# install Herdr + set KAIDERA_OS_HERDR_BIN; it auto-installs here only if HERDR_INSTALL_SHA256 is set.)
HERDR_BIN=""
if [ -x "$HERDR_INSTALLER" ]; then
  HERDR_BIN="$("$HERDR_INSTALLER" --print-path 2>/dev/null || true)"
fi
if [ -n "$HERDR_BIN" ]; then
  ok "Herdr runtime backend (optional): $HERDR_BIN"
else
  skip "Herdr runtime backend not installed - OPTIONAL (only the 'herdr-visible' execution mode needs it). The console + kaidera/claude harnesses work without it."
fi

# --- 2. Cortex + app-DB containers --------------------------------------------------------
say "2/7 Cortex + app-DB containers"
[ -f "$CORTEX_COMPOSE" ] || die "compose file not found: $CORTEX_COMPOSE"
# local-cortex/.env carries operator-local config the compose loads via env_file. Provider
# keys go in Settings (the app-DB), NOT here — BUT the Cortex ADMIN token is the one secret
# that MUST be provisioned at install time: cortex-api fails CLOSED without it (no project can
# be created), and the native console must send the SAME value. Generate a strong token ONCE,
# persist it here (cortex-api reads it via env_file on the up below), and reuse it for the
# console's systemd unit + runner so both sides of the wire agree. Idempotent on re-run; a
# weak/well-known 'cortex-local-admin' is treated as unset and rotated.
ENV_LOCAL="$REPO_ROOT/local-cortex/.env"
[ -f "$ENV_LOCAL" ] || { : > "$ENV_LOCAL"; ok "created local-cortex/.env (provider keys go in Settings, not here)"; }
# `|| true` is LOAD-BEARING: an empty / token-less .env (the post-wipe greenfield state) makes grep exit
# 1, which under `set -euo pipefail` would kill the install SILENTLY right here — before the regenerate
# below ever runs. Swallow the no-match so an absent token falls through to the `[ -z ]` regenerate.
ADMIN_TOKEN="$(grep -E '^CORTEX_ADMIN_TOKEN=' "$ENV_LOCAL" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
if [ -z "$ADMIN_TOKEN" ] || [ "$ADMIN_TOKEN" = "cortex-local-admin" ]; then
  ADMIN_TOKEN="$( (openssl rand -hex 32 2>/dev/null) || "$PY" -c 'import secrets;print(secrets.token_hex(32))' )"
  grep -vE '^CORTEX_ADMIN_TOKEN=' "$ENV_LOCAL" > "$ENV_LOCAL.tmp" 2>/dev/null && mv "$ENV_LOCAL.tmp" "$ENV_LOCAL" || true
  printf 'CORTEX_ADMIN_TOKEN=%s\n' "$ADMIN_TOKEN" >> "$ENV_LOCAL"
  ok "generated a Cortex admin token (persisted in local-cortex/.env — shared by cortex-api + console)"
else
  ok "reusing the Cortex admin token from local-cortex/.env"
fi
# The console's auth-session HMAC key. selfcontained deploy mode enables auth (auth_enabled()),
# and auth.py's _auth_secret() raises AuthConfigError — so /auth/email/request + /auth/email/verify
# 500 and NOBODY CAN LOG IN - unless KAIDERA_AUTH_SECRET is set. Generate a strong key ONCE, persist
# it here (idempotent: a re-run reuses the existing one so live sessions survive), and inject it into
# the console runner + systemd unit below so the wire agrees. Same pattern as CORTEX_ADMIN_TOKEN above.
# `|| true` on the grep is load-bearing for the same reason: a token-less .env makes grep exit 1, which
# under `set -euo pipefail` would kill the install SILENTLY before the `[ -z ]` regenerate runs.
AUTH_SECRET="$(grep -E '^KAIDERA_AUTH_SECRET=' "$ENV_LOCAL" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
if [ -z "$AUTH_SECRET" ]; then
  AUTH_SECRET="$( (openssl rand -base64 32 2>/dev/null) || "$PY" -c 'import secrets;print(secrets.token_urlsafe(32))' )"
  grep -vE '^KAIDERA_AUTH_SECRET=' "$ENV_LOCAL" > "$ENV_LOCAL.tmp" 2>/dev/null && mv "$ENV_LOCAL.tmp" "$ENV_LOCAL" || true
  printf 'KAIDERA_AUTH_SECRET=%s\n' "$AUTH_SECRET" >> "$ENV_LOCAL"
  ok "generated the console auth secret (persisted in local-cortex/.env — a fresh install can now log in)"
else
  ok "reusing the console auth secret from local-cortex/.env"
fi
# Pre-stage NON-SECRET Google Workspace SMTP relay examples as a ready-to-fill
# COMMENTED block so an operator only adds the mailbox and relay credential, then
# uncomments. We must NOT activate SMTP without the password — that fails CLOSED
# and blocks the first login — so these ship commented and 'log' delivery still
# bootstraps the first admin. Idempotent: the grep matches the commented key, so
# a re-run never re-appends.
if ! grep -q 'KAIDERA_SMTP_HOST' "$ENV_LOCAL" 2>/dev/null; then
  cat >> "$ENV_LOCAL" <<'SMTP_DEFAULTS'

# --- Auth email - optional Google Workspace SMTP relay integration ---
# Keep commented until the relay credential is present. Never commit the password.
# KAIDERA_AUTH_EMAIL_DELIVERY=smtp
# KAIDERA_SMTP_HOST=smtp-relay.gmail.com
# KAIDERA_SMTP_PORT=587
# KAIDERA_SMTP_TLS=1
# KAIDERA_SMTP_FROM=noreply@your-domain.example
# KAIDERA_SMTP_USER=noreply@your-domain.example
# KAIDERA_SMTP_PASSWORD=<relay-password>                 # SECRET - never commit
SMTP_DEFAULTS
  ok "staged optional SMTP relay examples (commented) in local-cortex/.env"
fi

# Cortex install mode detection. A re-run must converge the existing stack
# in-place, not overwrite Cortex state. Docker's named volumes and the env file
# are the durable boundary; compose `up` is allowed, `down -v`/volume deletion is
# not. This signal is also checked by scripts/install/verify-cortex-install-contract.py.
_CORTEX_EXISTING_SIGNALS=""
_add_cortex_signal() {
  if [ -z "$_CORTEX_EXISTING_SIGNALS" ]; then
    _CORTEX_EXISTING_SIGNALS="$1"
  else
    _CORTEX_EXISTING_SIGNALS="$_CORTEX_EXISTING_SIGNALS, $1"
  fi
}
docker container inspect cortex-pg >/dev/null 2>&1 && _add_cortex_signal "container:cortex-pg"
docker container inspect cortex-api >/dev/null 2>&1 && _add_cortex_signal "container:cortex-api"
docker container inspect harness-appdb >/dev/null 2>&1 && _add_cortex_signal "container:harness-appdb"
for _vol in \
  "${COMPOSE_PROJECT}_cortex-pg-data" "kaidera-os-cortex_cortex-pg-data" \
  "cortex-pg-data" \
  "${COMPOSE_PROJECT}_harness-appdb-data" "kaidera-os-cortex_harness-appdb-data" \
  "harness-appdb-data"; do
  if docker volume inspect "$_vol" >/dev/null 2>&1; then
    _add_cortex_signal "volume:$_vol"
  fi
done
if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "$CORTEX_API_URL/health" >/dev/null 2>&1; then
  _add_cortex_signal "health:$CORTEX_API_URL"
fi
if [ -n "$_CORTEX_EXISTING_SIGNALS" ]; then
  ok "existing Cortex detected ($_CORTEX_EXISTING_SIGNALS) — preserving env secrets and named data volumes; converging services in place"
else
  ok "no existing Cortex detected — provisioning a fresh local Cortex/app-DB stack"
fi
# NOTE: KAIDERA_AUTH_SECRET only enables the login MACHINERY. For REAL email delivery of the sign-in
# code/link, also set KAIDERA_SMTP_HOST / KAIDERA_SMTP_FROM (+ _USER/_PASSWORD) in local-cortex/.env.
# Without SMTP the console runs in 'log' delivery: the one-time code is written to the console log
# (journalctl -u kaidera-os-console / the foreground runner) - enough to bootstrap the FIRST admin. Do
# NOT block the install on SMTP; it is a post-install convenience, not a prerequisite to log in.
# --build so a re-install / update rebuilds the api+worker images from the (possibly newer) committed
# Dockerfiles — otherwise `up -d` reuses a STALE cached image and the clean package's new code never
# takes effect. Cache-hits + is fast when nothing changed; only the changed layers rebuild.
# NOTE the `${arr[@]+...}` guard around COMPOSE_PROFILE_ARGS: expanding an EMPTY array as
# "${arr[@]}" under `set -u` is an "unbound variable" error on bash 3.2 (macOS's /bin/bash) — the
# guard makes the default (no-profile) path expand to nothing safely. CORTEX_SERVICES is never empty.
docker compose -p "$COMPOSE_PROJECT" -f "$CORTEX_COMPOSE" \
  ${COMPOSE_PROFILE_ARGS[@]+"${COMPOSE_PROFILE_ARGS[@]}"} up -d --build "${CORTEX_SERVICES[@]}" \
  || die "Cortex/app-DB containers failed to build/start — check network (image pulls) + free disk, then re-run. Logs: docker compose -p $COMPOSE_PROJECT -f $CORTEX_COMPOSE logs"
ok "started ${#CORTEX_SERVICES[@]} services (project: $COMPOSE_PROJECT) — console excluded (runs native)"
if [ "$CORTEX_LOCAL_EMBED" = "1" ]; then
  ok "local embed worker enabled (KAIDERA_CORTEX_LOCAL_EMBED=1 or KAIDERA_CORTEX_PROFILE=full)"
else
  skip "local embed worker OFF - provider-backed Cortex embeddings stay active; re-run with KAIDERA_CORTEX_LOCAL_EMBED=1 ./install.sh for the local sentence-transformer fallback"
fi
if [ "$CORTEX_PROFILE" = "full" ]; then
  ok "multimodal L5: audio + vision workers started (KAIDERA_CORTEX_PROFILE=full)"
else
  skip "audio/vision L5 enrichment OFF - re-run with KAIDERA_CORTEX_PROFILE=full ./install.sh to enable (vision pulls an ~8 GB model)"
fi
# A re-install may have cortex-api already running with the OLD/empty token; recreate it so it
# reloads the .env we just wrote and the admin surface (create-project) unlocks.
docker compose -p "$COMPOSE_PROJECT" -f "$CORTEX_COMPOSE" up -d --force-recreate --no-deps cortex-api >/dev/null 2>&1 || true
for _ in $(seq 1 30); do
  [ "$(docker inspect harness-appdb --format '{{.State.Health.Status}}' 2>/dev/null || true)" = "healthy" ] && break
  sleep 2
done
if [ "$(docker inspect harness-appdb --format '{{.State.Health.Status}}' 2>/dev/null || true)" = "healthy" ]; then
  ok "app-DB healthy (localhost:5500)"
else
  skip "app-DB health not confirmed yet — it may still be starting"
fi

# --- 3. Native console venv ---------------------------------------------------------------
say "3/7 Native console (Python venv)"
VENV="$CONSOLE_DIR/.venv"
if [ -d "$VENV" ] && { [ ! -x "$VENV/bin/python" ] || [ ! -x "$VENV/bin/pip" ] \
  || ! "$VENV/bin/pip" --version >/dev/null 2>&1; }; then
  rm -rf -- "$VENV"
  ok "discarded relocated or broken console virtual environment"
fi
[ -d "$VENV" ] || "$PY" -m venv "$VENV" \
  || die "python venv creation failed — on Debian/Ubuntu install the venv module: apt install python3-venv, then re-run."
"$VENV/bin/python" -m pip install -q --upgrade pip \
  || die "pip upgrade failed — check network / PyPI access, then re-run."
if [ -f "$CONSOLE_DIR/requirements.txt" ]; then
  "$VENV/bin/python" -m pip install -q -r "$CONSOLE_DIR/requirements.txt" \
    || die "console dependency install failed — check network / PyPI access, then re-run."
else
  "$VENV/bin/python" -m pip install -q "uvicorn[standard]" fastapi jinja2 httpx sse-starlette asyncpg psycopg2-binary \
    || die "console dependency install failed — check network / PyPI access, then re-run."
fi
ok "console venv ready ($VENV)"
# The SPA bundle (spa/dist) IS the runtime UI — without it /app is blank. A release ships it
# prebuilt; if it's missing OR stale versus source/config, build it when npm is available, else
# FAIL LOUD. A merely-present dist is not enough during local promotion: otherwise install.sh can
# restart the console while still serving an older UI bundle.
SPA_INDEX="$CONSOLE_DIR/spa/dist/index.html"
SPA_READY="0"
SPA_STALE="0"
if [ -f "$SPA_INDEX" ] && ls "$CONSOLE_DIR"/spa/dist/assets/*.js >/dev/null 2>&1; then
  SPA_READY="1"
  if find \
      "$CONSOLE_DIR/spa/src" \
      "$CONSOLE_DIR/spa/package.json" \
      "$CONSOLE_DIR/spa/package-lock.json" \
      "$CONSOLE_DIR/spa/vite.config.ts" \
      "$CONSOLE_DIR/spa/tsconfig.json" \
      "$CONSOLE_DIR/spa/tsconfig.app.json" \
      "$CONSOLE_DIR/spa/tsconfig.node.json" \
      -type f -newer "$SPA_INDEX" 2>/dev/null | grep -q .; then
    SPA_STALE="1"
  fi
fi
if [ "$SPA_READY" = "1" ] && [ "$SPA_STALE" = "0" ]; then
  ok "SPA bundle present"
elif command -v npm >/dev/null 2>&1; then
  if [ "$SPA_READY" = "1" ]; then
    echo "  rebuilding stale SPA bundle (source/config is newer than dist)…"
  else
    echo "  building the SPA bundle (npm — first build can take a minute)…"
  fi
  if bash "$CONSOLE_DIR/scripts/build-spa.sh"; then
    ok "SPA built"
  else
    die "SPA build failed — see output above."
  fi
else
  if [ "$SPA_STALE" = "1" ]; then
    die "SPA bundle is stale and npm not found. Install Node.js/npm or deploy a release with a fresh spa/dist."
  fi
  die "SPA bundle missing (spa/dist) and npm not found. Re-download a complete release, or install Node.js so install.sh can build it."
fi

# --- 4. Console runner (portable script + Linux systemd unit) -----------------------------
say "4/7 Console runner"

# Login-email delivery wiring (BUG #2 fix). auth.py reads the delivery mode + Graph/SMTP creds
# from os.environ ONLY (same as KAIDERA_AUTH_SECRET), but the generated runner/unit pass a FIXED
# env allowlist and don't source local-cortex/.env — so before this, Graph/SMTP delivery needed a
# hand-authored systemd dropin. Now: collect whichever email vars are set (from the install ENV or
# from local-cortex/.env, which install.sh already owns) and INJECT them into both the runner and
# the unit, exactly the way KAIDERA_AUTH_SECRET is injected. Unset vars are simply omitted (the
# default `log` delivery — code to the journal — still bootstraps the first admin). Secrets are
# read into the rendered files but never echoed to the console.
AUTH_EMAIL_KEYS=(
  KAIDERA_AUTH_EMAIL_DELIVERY
  KAIDERA_AUTH_GRAPH_TENANT_ID KAIDERA_AUTH_GRAPH_CLIENT_ID
  KAIDERA_AUTH_GRAPH_CLIENT_SECRET KAIDERA_AUTH_GRAPH_SENDER
  KAIDERA_SMTP_HOST KAIDERA_SMTP_FROM KAIDERA_SMTP_PORT
  KAIDERA_SMTP_USER KAIDERA_SMTP_PASSWORD KAIDERA_SMTP_TLS
)
# Resolve each key: prefer an explicit value in the install ENV; else fall back to local-cortex/.env
# (so an operator can drop the block in the one file install.sh manages). `|| true` keeps a no-match
# from killing the script under `set -euo pipefail`.
_env_or_dotenv() {  # $1 = key → prints the value (env wins, then .env), or nothing
  local _k="$1" _v
  _v="$(eval "printf '%s' \"\${$_k:-}\"")"
  if [ -z "$_v" ] && [ -f "$ENV_LOCAL" ]; then
    _v="$(grep -E "^${_k}=" "$ENV_LOCAL" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  fi
  printf '%s' "$_v"
}
AUTH_EMAIL_RUNNER_LINES=""   # `KEY='VALUE' \` lines for the runner's `exec env` block
AUTH_EMAIL_UNIT_LINES=""     # `Environment="KEY=VALUE"` lines for the systemd unit
_auth_email_set_count=0
_auth_email_delivery_desc=""
if [ "$AUTH_ENABLED" = "0" ]; then
  skip "login-email delivery not wired — auth is disabled for this local/private install"
else
  for _k in "${AUTH_EMAIL_KEYS[@]}"; do
    _v="$(_env_or_dotenv "$_k")"
    [ -n "$_v" ] || continue
    _auth_email_set_count=$((_auth_email_set_count + 1))
    [ "$_k" = "KAIDERA_AUTH_EMAIL_DELIVERY" ] && _auth_email_delivery_desc="$_v"
    # Runner: single-quote the value (escape any embedded single quote) so secrets with shell
    # metacharacters survive verbatim inside the generated `exec env … \` continuation.
    _q="${_v//\'/\'\\\'\'}"
    AUTH_EMAIL_RUNNER_LINES="${AUTH_EMAIL_RUNNER_LINES}         ${_k}='${_q}' \\
"
    # systemd: double-quote so spaces are kept (values are single-line — env can't hold newlines).
    # C-style-escape backslash then double-quote so a secret containing either survives intact
    # inside the quoted Environment= value (real Graph/SMTP secrets rarely have them, but be safe).
    _sv="${_v//\\/\\\\}"; _sv="${_sv//\"/\\\"}"
    AUTH_EMAIL_UNIT_LINES="${AUTH_EMAIL_UNIT_LINES}Environment=\"${_k}=${_sv}\"
"
  done
fi
if [ "$_auth_email_set_count" -gt 0 ]; then
  ok "login-email delivery wired into the runner + unit (${_auth_email_delivery_desc:-auto-select}, $_auth_email_set_count var(s) — secrets not shown)"
fi

# Hosted auth/proxy deployment knobs. These are not secrets, but they are runtime-critical
# for public domains behind Caddy/Cloudflare: re-running install.sh must not silently drop
# cookie domain, passkey RP ID, secure-cookie, or trusted-proxy settings.
AUTH_DEPLOY_KEYS=(
  KAIDERA_PUBLIC_BASE_URL KAIDERA_AUTH_ORIGIN
  KAIDERA_AUTH_COOKIE_DOMAIN KAIDERA_AUTH_RP_ID
  KAIDERA_AUTH_COOKIE_SECURE KAIDERA_AUTH_TRUSTED_PROXY
)
AUTH_DEPLOY_RUNNER_LINES=""
AUTH_DEPLOY_UNIT_LINES=""
_auth_deploy_set_count=0
for _k in "${AUTH_DEPLOY_KEYS[@]}"; do
  _v="$(_env_or_dotenv "$_k")"
  [ -n "$_v" ] || continue
  _auth_deploy_set_count=$((_auth_deploy_set_count + 1))
  _q="${_v//\'/\'\\\'\'}"
  AUTH_DEPLOY_RUNNER_LINES="${AUTH_DEPLOY_RUNNER_LINES}         ${_k}='${_q}' \\
"
  _sv="${_v//\\/\\\\}"; _sv="${_sv//\"/\\\"}"
  AUTH_DEPLOY_UNIT_LINES="${AUTH_DEPLOY_UNIT_LINES}Environment=\"${_k}=${_sv}\"
"
done
if [ "$_auth_deploy_set_count" -gt 0 ]; then
  ok "hosted auth deployment options wired into the runner + unit ($_auth_deploy_set_count var(s))"
fi

# Project-pack extension wiring. The pack installer writes an extensions.env helper with
# KAIDERA_OS_EXTENSION_MODULES and KAIDERA_OS_EXTENSION_PATHS; persist those explicit operator
# choices into the generated runner/unit so installed packs actually load after restart.
EXTENSION_KEYS=(KAIDERA_OS_EXTENSION_MODULES KAIDERA_OS_EXTENSION_PATHS)
EXTENSION_RUNNER_LINES=""
EXTENSION_UNIT_LINES=""
_extension_set_count=0
for _k in "${EXTENSION_KEYS[@]}"; do
  _v="$(_env_or_dotenv "$_k")"
  [ -n "$_v" ] || continue
  _extension_set_count=$((_extension_set_count + 1))
  _q="${_v//\'/\'\\\'\'}"
  EXTENSION_RUNNER_LINES="${EXTENSION_RUNNER_LINES}         ${_k}='${_q}' \\
"
  _sv="${_v//\\/\\\\}"; _sv="${_sv//\"/\\\"}"
  EXTENSION_UNIT_LINES="${EXTENSION_UNIT_LINES}Environment=\"${_k}=${_sv}\"
"
done
if [ "$_extension_set_count" -gt 0 ]; then
  ok "project-pack extensions wired into the runner + unit ($_extension_set_count var(s))"
fi
EDITION_RUNNER_LINE=""
EDITION_UNIT_LINE=""
if [ -n "$EDITION" ]; then
  EDITION_RUNNER_LINE="         KAIDERA_OS_EDITION=\"$EDITION\" \\
"
  EDITION_UNIT_LINE="Environment=KAIDERA_OS_EDITION=$EDITION
"
fi

RUNNER="$REPO_ROOT/run-kaidera-os-console.sh"
cat > "$RUNNER" <<EOF
#!/usr/bin/env bash
# Run the native Kaidera OS console (self-contained mode). Generated by install.sh.
cd "$CONSOLE_DIR"
# CORTEX_ADMIN_TOKEN is intentionally NOT injected — the console reads it from
# local-cortex/.env at request time (the SINGLE source it shares with cortex-api's
# env_file), so the two can never drift. Baking a copy into the process env (which
# resolve_admin_token reads BEFORE .env) was the recurring "admin token / can't
# register project" mismatch.
# KAIDERA_AUTH_SECRET, in contrast, MUST be injected: auth.py's _auth_secret() reads
# it from os.environ ONLY (no .env fallback like the admin token has), so without it
# in the process env the login endpoints 500 and the operator can't sign in. We inject
# the SAME value install.sh just persisted to local-cortex/.env, so there is no drift.
# ponytail: reclaim the port so a stale listener can't shadow a restart.
# Fail loud, not silent: if the port is held, take it; never crash-loop against a ghost.
# launchctl kickstart -k otherwise reports rc=0 while the old listener keeps serving and
# the fresh process dies on bind (EADDRINUSE) under set -e + exec. Default to the port
# install.sh baked into the exec below; an env override still wins at runtime.
_PORT="\${KAIDERA_CONSOLE_PORT:-$CONSOLE_PORT}"
if lsof -ti tcp:"\$_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "console launcher: reclaiming port \$_PORT from a stale listener" >&2
    lsof -ti tcp:"\$_PORT" -sTCP:LISTEN | xargs kill 2>/dev/null || true
    # give the old listener a moment to release the socket
    for _i in 1 2 3 4 5; do lsof -ti tcp:"\$_PORT" -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 1; done
fi
exec env KAIDERA_DEPLOY_MODE=selfcontained \\
         KAIDERA_AUTH_ENABLED="$AUTH_ENABLED" \\
${EDITION_RUNNER_LINE}         PATH="\$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" \\
         CORTEX_API_URL="$CORTEX_API_URL" \\
         KAIDERA_OS_HERDR_BIN="$HERDR_BIN" \\
         HARNESS_APPDB_DSN="$APPDB_DSN" \\
         KAIDERA_AUTH_SECRET="$AUTH_SECRET" \\${PUBLIC_BASE_URL:+
         KAIDERA_PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \\
         KAIDERA_AUTH_ORIGIN="$PUBLIC_BASE_URL" \\}
${AUTH_EMAIL_RUNNER_LINES}${AUTH_DEPLOY_RUNNER_LINES}${EXTENSION_RUNNER_LINES}  "$VENV/bin/uvicorn" app.main:app --host "$CONSOLE_HOST" --port "$CONSOLE_PORT" \\
    --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS" \\
    --timeout-graceful-shutdown 5
EOF
chmod +x "$RUNNER"
ok "run script: $RUNNER"
# Persist the install root for packaged native shells. A dragged-to-Applications
# menu-bar app cannot derive the source checkout path from __file__, so it reads
# this non-secret pointer (or KAIDERA_OS_HOME) before falling back to source paths.
OPERATOR_CONFIG_DIR="$HOME/.kaidera-os"
OPERATOR_CONFIG="$OPERATOR_CONFIG_DIR/operator.json"
mkdir -p "$OPERATOR_CONFIG_DIR" 2>/dev/null || true
if command -v python3 >/dev/null 2>&1; then
  python3 - "$REPO_ROOT" "$OPERATOR_CONFIG" <<'PY' || true
import json
import sys
from pathlib import Path

repo_root = str(Path(sys.argv[1]).resolve())
path = Path(sys.argv[2])
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps({"repo_root": repo_root}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  ok "native operator install root recorded in $OPERATOR_CONFIG"
else
  skip "python3 unavailable for operator root config - set KAIDERA_OS_HOME=$REPO_ROOT before launching the native operator"
fi
# Loud warning when the operator opted into network exposure.
case "$CONSOLE_HOST" in
  127.0.0.1|localhost|::1) ;;
  *) printf '\033[1;33m  ⚠ EXPOSED: binding %s:%s - enable KAIDERA_AUTH_ENABLED=1 + HTTPS for shared use.\n     Firewall this port from the public internet; reach it only over a private network / VPN (e.g. Tailscale).\033[0m\n' "$CONSOLE_HOST" "$CONSOLE_PORT" ;;
esac
if [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
  # Always write a ready-to-install unit (runs as the installing user; auto-starts at boot).
  SYSTEMD_SERVICE="kaidera-os-console"
  UNIT_SRC="$REPO_ROOT/$SYSTEMD_SERVICE.service"
  # Optional reverse-proxy env block (only when KAIDERA_PUBLIC_BASE_URL is set) - pins the public
  # origin so login links/redirects use it instead of the localhost the proxy connects from.
  PUBLIC_ENV_LINES=""
  if [ -n "$PUBLIC_BASE_URL" ]; then
    PUBLIC_ENV_LINES="Environment=KAIDERA_PUBLIC_BASE_URL=$PUBLIC_BASE_URL
Environment=KAIDERA_AUTH_ORIGIN=$PUBLIC_BASE_URL
"
  fi
  cat > "$UNIT_SRC" <<EOF
[Unit]
Description=Kaidera OS native console (self-contained)
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$CONSOLE_DIR
Environment=KAIDERA_DEPLOY_MODE=selfcontained
Environment=KAIDERA_AUTH_ENABLED=$AUTH_ENABLED
${EDITION_UNIT_LINE}Environment=CORTEX_API_URL=$CORTEX_API_URL
Environment=HARNESS_APPDB_DSN=$APPDB_DSN
Environment=KAIDERA_OS_HERDR_BIN=$HERDR_BIN
Environment=KAIDERA_AUTH_SECRET=$AUTH_SECRET
# CORTEX_ADMIN_TOKEN is NOT set here on purpose — the console reads it from
# local-cortex/.env (the single source shared with cortex-api), so they can't drift.
# KAIDERA_AUTH_SECRET IS set here (unlike the admin token): auth.py reads it from the
# process env only, with no .env fallback, so selfcontained-mode login 500s without it.
# Login-email delivery (Graph/SMTP) and hosted auth/proxy deployment options are injected the
# same way (auth.py reads those from os.environ) — only vars set in the install ENV /
# local-cortex/.env appear.
# Project-pack extensions are injected only when explicitly configured with
# KAIDERA_OS_EXTENSION_MODULES / KAIDERA_OS_EXTENSION_PATHS.
${PUBLIC_ENV_LINES}${AUTH_EMAIL_UNIT_LINES}${AUTH_DEPLOY_UNIT_LINES}${EXTENSION_UNIT_LINES}# uvicorn --proxy-headers is ALWAYS passed (safe with no proxy: X-Forwarded-* is only trusted
# from --forwarded-allow-ips, default loopback). Behind Caddy/nginx it makes request.base_url /
# scheme / host reflect the public origin so the console never emits localhost links/redirects.
ExecStart=$VENV/bin/uvicorn app.main:app --host $CONSOLE_HOST --port $CONSOLE_PORT --proxy-headers --forwarded-allow-ips=$FORWARDED_ALLOW_IPS --timeout-graceful-shutdown 5
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  # `enable` (boot) + `restart` (apply NOW) — NOT `enable --now`, which only STARTS an inactive
  # service and would leave an already-running console on its OLD config after a re-run (e.g. a
  # host-binding change wouldn't take effect — the bug that left the console stuck on 127.0.0.1).
  MANUAL="sudo cp $UNIT_SRC /etc/systemd/system/$SYSTEMD_SERVICE.service && sudo systemctl daemon-reload && sudo systemctl enable $SYSTEMD_SERVICE && sudo systemctl restart $SYSTEMD_SERVICE"
  if [ "$(id -u)" = "0" ] || [ -w /etc/systemd/system ]; then
    if cp "$UNIT_SRC" "/etc/systemd/system/$SYSTEMD_SERVICE.service" \
         && systemctl daemon-reload && systemctl enable "$SYSTEMD_SERVICE" \
         && systemctl restart "$SYSTEMD_SERVICE"; then
      ok "auto-start enabled + console (re)started on $CONSOLE_HOST:$CONSOLE_PORT"
    else
      skip "systemd install failed; run: $MANUAL"
    fi
  elif command -v sudo >/dev/null 2>&1; then
    echo "  installing the background service (you may be asked for your sudo password)…"
    if sudo cp "$UNIT_SRC" "/etc/systemd/system/$SYSTEMD_SERVICE.service" \
         && sudo systemctl daemon-reload && sudo systemctl enable "$SYSTEMD_SERVICE" \
         && sudo systemctl restart "$SYSTEMD_SERVICE"; then
      ok "auto-start enabled + console (re)started on $CONSOLE_HOST:$CONSOLE_PORT"
      # Grant THIS (non-root) user passwordless control of ONLY this unit, so later
      # `kaidera-os upgrade` / start / stop / restart run non-interactively. Without it an
      # account lacking passwordless sudo can't restart the console → every upgrade fails
      # at the restart + rolls back. Scoped to the exact verbs + the one unit; syntax-
      # validated with visudo BEFORE install so a malformed drop-in is ignored, never a lockout.
      _SC="$(command -v systemctl || echo /usr/bin/systemctl)"
      _SUDOERS="$(id -un) ALL=(root) NOPASSWD: $_SC start $SYSTEMD_SERVICE, $_SC stop $SYSTEMD_SERVICE, $_SC restart $SYSTEMD_SERVICE, $_SC status $SYSTEMD_SERVICE"
      _STMP="$(mktemp)"; printf '%s\n' "$_SUDOERS" > "$_STMP"
      if sudo visudo -cf "$_STMP" >/dev/null 2>&1 && sudo install -m 0440 "$_STMP" /etc/sudoers.d/kaidera-os-console; then
        ok "passwordless service control granted to $(id -un) - later 'kaidera-os upgrade' won't prompt"
      else
        skip "couldn't grant passwordless service control; later 'kaidera-os upgrade' may prompt for sudo"
      fi
      rm -f "$_STMP"
    else
      skip "couldn't enable automatically — run: $MANUAL"
    fi
  else
    skip "no sudo found — to enable auto-start run: $MANUAL"
  fi
elif [ "$OS" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  # macOS E011 path: install a per-user LaunchAgent that runs the SAME generated
  # runner. The native/menu-bar app controls this LaunchAgent; it does not own a
  # second service implementation.
  LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
  LAUNCH_AGENT_LABEL="ai.kaidera.kaidera-os.console"
  LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/$LAUNCH_AGENT_LABEL.plist"
  mkdir -p "$LAUNCH_AGENT_DIR" "$REPO_ROOT/local-cortex/logs"
  _LAUNCH_DOMAIN="gui/$(id -u)"

  cat > "$LAUNCH_AGENT_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LAUNCH_AGENT_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$RUNNER</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_ROOT</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>StandardOutPath</key>
  <string>$REPO_ROOT/local-cortex/logs/kaidera-os-console.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$REPO_ROOT/local-cortex/logs/kaidera-os-console.launchd.err.log</string>
</dict>
</plist>
EOF
  launchctl bootout "$_LAUNCH_DOMAIN" "$LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
  if launchctl bootstrap "$_LAUNCH_DOMAIN" "$LAUNCH_AGENT_PLIST" >/dev/null 2>&1 \
       && launchctl enable "$_LAUNCH_DOMAIN/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 \
       && launchctl kickstart -k "$_LAUNCH_DOMAIN/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1; then
    ok "macOS LaunchAgent installed + console (re)started on $CONSOLE_HOST:$CONSOLE_PORT"
  else
    skip "LaunchAgent written but launchctl did not start it — start manually: launchctl bootstrap $_LAUNCH_DOMAIN $LAUNCH_AGENT_PLIST && launchctl kickstart -k $_LAUNCH_DOMAIN/$LAUNCH_AGENT_LABEL"
  fi
fi

# --- 4c. Public reverse proxy (opt-in) ----------------------------------------------------
# When KAIDERA_PUBLIC_HOST (or _BASE_URL) is set, OWN the public path: render the generic,
# project-agnostic Caddy template (redistributable/deploy/Caddyfile.template) for this host and
# install it so the app serves publicly over HTTPS out-of-the-box — instead of leaving the TLS
# terminator as a manual out-of-band step. No host set ⇒ localhost-only (this step is skipped,
# behavior unchanged). Linux + systemd only (Caddy is a Linux service); on macOS / no Caddy we
# print the one-liners so the operator can finish by hand.
if [ -n "$PUBLIC_HOST" ]; then
  say "4c/7 Public reverse proxy ($PUBLIC_HOST)"
  PROXY_TEMPLATE="$REPO_ROOT/redistributable/deploy/Caddyfile.template"
  if [ ! -f "$PROXY_TEMPLATE" ]; then
    skip "reverse-proxy template missing ($PROXY_TEMPLATE) — skipping public proxy setup"
  else
    # Render: substitute the Caddy host + TLS placeholders with concrete values so the installed
    # file is self-contained (no reliance on Caddy's process env). Pick the TLS directive in a
    # strict precedence so we NEVER emit a bare `tls` (invalid — `wrong argument count after tls`,
    # which is what silently downed a working Caddy in the v0.1.119 clean-deploy test):
    #   1. KAIDERA_TLS_CERT + KAIDERA_TLS_KEY -> `tls <cert> <key>` (origin cert; Cloudflare Full-strict)
    #   2. KAIDERA_TLS_EMAIL                  -> `tls <email>`      (Let's Encrypt auto-HTTPS)
    #   3. neither                             → `tls internal`     (self-signed; correct behind an
    #                                            upstream terminator like Cloudflare). The SAFE default.
    RENDERED_CADDY="$REPO_ROOT/Caddyfile"
    if [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ]; then
      _TLS_LINE="tls $TLS_CERT $TLS_KEY"
      _TLS_DESC="origin cert ($TLS_CERT)"
    elif [ -n "$TLS_EMAIL" ]; then
      _TLS_LINE="tls $TLS_EMAIL"
      _TLS_DESC="Let's Encrypt auto-HTTPS ($TLS_EMAIL)"
    else
      _TLS_LINE="tls internal"
      _TLS_DESC="tls internal (self-signed - set KAIDERA_TLS_CERT/KEY for an origin cert, or KAIDERA_TLS_EMAIL for Let's Encrypt)"
    fi
    # Use awk for a literal, delimiter-safe substitution (cert/key paths contain '/'). The template
    # carries a single `__KAIDERA_TLS_LINE__` marker on its own (indented) line; replace it wholesale.
    awk -v host="$PUBLIC_HOST" -v tls="$_TLS_LINE" '
      { gsub(/\{\$KAIDERA_PUBLIC_HOST\}/, host); gsub(/__KAIDERA_TLS_LINE__/, tls); print }
    ' "$PROXY_TEMPLATE" > "$RENDERED_CADDY"
    ok "rendered Caddyfile for $PUBLIC_HOST → $RENDERED_CADDY (TLS: $_TLS_DESC)"
    CADDY_MANUAL="sudo cp $RENDERED_CADDY /etc/caddy/Caddyfile && sudo systemctl enable caddy && sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy"
    if [ "$OS" = "Linux" ] && command -v caddy >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
      # `caddy validate` PROVISIONS the config — it actually LOADS the TLS cert + key files (the failure
      # is `provision tls: loading certificates`, not a parse error). An origin key (KAIDERA_TLS_CERT/KEY,
      # e.g. /etc/caddy/*.key) is typically root/caddy-owned 0600, so validate must run with the SAME
      # privilege that installs + runs Caddy below — else it fails `permission denied` on a perfectly
      # VALID config. (Caught in the v0.1.120 clean-deploy re-test: validate ran as the unprivileged
      # install user and could not read the caddy-owned origin key.) Mirror the install's escalation.
      if [ "$(id -u)" = "0" ]; then _CADDY_SUDO=""
      elif command -v sudo >/dev/null 2>&1; then _CADDY_SUDO="sudo"
      else _CADDY_SUDO=""; fi
      # VALIDATE FIRST — never overwrite/reload /etc/caddy/Caddyfile with a config that won't parse.
      # In v0.1.119 a bare `tls` rendered an UNPARSEABLE file, install.sh cp-ed it over the live
      # config, the reload failed, and a WORKING Caddy went `failed` (public path DOWN) behind a soft
      # "skip" — the BLOCKER this gate closes. `caddy validate` the RENDERED file (not the live one)
      # and FAIL LOUD on error, leaving the running Caddy + its current /etc/caddy/Caddyfile untouched.
      if ! _CADDY_VALIDATE_OUT="$($_CADDY_SUDO caddy validate --config "$RENDERED_CADDY" --adapter caddyfile 2>&1)"; then
        printf '%s\n' "$_CADDY_VALIDATE_OUT" | sed 's/^/      /' >&2
        die "rendered Caddyfile failed 'caddy validate' (see above) - NOT installing it (the running Caddy + /etc/caddy/Caddyfile are untouched). The rendered file is at $RENDERED_CADDY; fix the TLS knobs (KAIDERA_TLS_CERT/KEY or KAIDERA_TLS_EMAIL) and re-run, or install by hand once valid: $CADDY_MANUAL"
      fi
      ok "rendered Caddyfile passed 'caddy validate'"
      # Only now (config proven valid) do we install + reload. A reload failure here is a REAL
      # problem (valid config, but Caddy wouldn't apply it), so it is loud, not a soft skip.
      _install_caddy() { cp "$RENDERED_CADDY" /etc/caddy/Caddyfile 2>/dev/null; }
      if [ "$(id -u)" = "0" ] || [ -w /etc/caddy ]; then
        if _install_caddy && systemctl enable caddy >/dev/null 2>&1 && { systemctl reload caddy >/dev/null 2>&1 || systemctl restart caddy >/dev/null 2>&1; }; then
          ok "Caddy serving https://$PUBLIC_HOST → 127.0.0.1:$CONSOLE_PORT ($_TLS_DESC)"
        else
          die "Caddy config is valid but reload/restart FAILED — check 'systemctl status caddy' and 'journalctl -u caddy'. The config was already copied to /etc/caddy/Caddyfile; once the service is healthy: sudo systemctl reload caddy"
        fi
      elif command -v sudo >/dev/null 2>&1; then
        echo "  installing the reverse proxy (you may be asked for your sudo password)…"
        if sudo cp "$RENDERED_CADDY" /etc/caddy/Caddyfile && sudo systemctl enable caddy >/dev/null 2>&1 \
             && { sudo systemctl reload caddy >/dev/null 2>&1 || sudo systemctl restart caddy >/dev/null 2>&1; }; then
          ok "Caddy serving https://$PUBLIC_HOST → 127.0.0.1:$CONSOLE_PORT ($_TLS_DESC)"
        else
          die "Caddy config is valid but the sudo install/reload FAILED — run it yourself and check 'systemctl status caddy': $CADDY_MANUAL"
        fi
      else
        skip "no sudo found — the rendered config is VALID; to serve publicly run: $CADDY_MANUAL"
      fi
    elif [ "$OS" = "Linux" ]; then
      skip "Caddy not installed — install it (https://caddyserver.com/docs/install), then: $CADDY_MANUAL"
    else
      skip "public proxy auto-setup is Linux-only — the rendered Caddyfile is at $RENDERED_CADDY (or use your own terminator → 127.0.0.1:$CONSOLE_PORT)"
    fi
    # Remind that the console must bind beyond loopback for the proxy to reach it across hosts (it
    # reaches 127.0.0.1 fine when co-located; the warning is for split setups).
    case "$CONSOLE_HOST" in
      127.0.0.1|localhost|::1) printf '  ℹ console binds %s - fine for a co-located proxy; for a remote proxy set KAIDERA_CONSOLE_HOST=0.0.0.0.\n' "$CONSOLE_HOST" ;;
    esac
  fi
fi

# --- 4b. Cortex CLI on PATH ---------------------------------------------------------------
say "4b/7 Cortex CLI on PATH"
# The cortex-* CLI lives in $REPO_ROOT/.agents/scripts and is how an operator boots agents,
# files handoffs, and logs (cortex-boot / cortex-handoff / cortex-log …). Make it reachable in
# NEW shells persistently. Preferred: a system-wide /etc/profile.d drop-in (login + most
# interactive shells source it) — same sudo-or-bare pattern Step 4 uses for the systemd unit.
# Fallback (no sudo / no profile.d): append an idempotent, grep-guarded block to THIS user's
# shell rc so it isn't duplicated on re-run.
CLI_DIR="$REPO_ROOT/.agents/scripts"
CLI_COUNT="$(find "$CLI_DIR" -maxdepth 1 -type f -name 'cortex-*' 2>/dev/null | wc -l | tr -d ' ')"
PROFILE_D="/etc/profile.d/kaidera-cortex.sh"
# shellcheck disable=SC2016  # $PATH is emitted literally for the target shell.
PROFILE_BODY="$(printf '# Cortex CLI - generated by the Kaidera OS installer\nexport PATH="%s:$PATH"\nexport CORTEX_API_URL="%s"\n' "$CLI_DIR" "$CORTEX_API_URL")"
_cli_installed=""
if [ -d "$CLI_DIR" ]; then
  if [ "$(id -u)" = "0" ] || [ -w /etc/profile.d ]; then
    printf '%s\n' "$PROFILE_BODY" > "$PROFILE_D" 2>/dev/null \
      && { chmod 644 "$PROFILE_D" 2>/dev/null || true; _cli_installed="$PROFILE_D"; }
  elif command -v sudo >/dev/null 2>&1 && [ -d /etc/profile.d ]; then
    printf '%s\n' "$PROFILE_BODY" | sudo tee "$PROFILE_D" >/dev/null 2>&1 \
      && { sudo chmod 644 "$PROFILE_D" 2>/dev/null || true; _cli_installed="$PROFILE_D"; }
  fi
  if [ -n "$_cli_installed" ]; then
    ok "Cortex CLI on PATH via $PROFILE_D ($CLI_COUNT cortex-* commands) — open a new shell, or: source $PROFILE_D"
  else
    # Fallback to the invoking user's shell rc (zsh → ~/.zshrc, else ~/.bashrc).
    case "${SHELL:-}" in */zsh) RC="$HOME/.zshrc" ;; *) RC="$HOME/.bashrc" ;; esac
    GUARD="# >>> kaidera-cortex-cli >>>"
    _cli_rc_action="added"
    if [ -f "$RC" ] && grep -qF "$GUARD" "$RC" 2>/dev/null; then
      RC_BACKUP="$RC.kaidera-path-pre-refresh-$(date +%Y%m%dT%H%M%S)"
      cp -p "$RC" "$RC_BACKUP"
      sed -i.bak '/# >>> kaidera-cortex-cli >>>/,/# <<< kaidera-cortex-cli <<</d' "$RC" \
        && rm -f "$RC.bak"
      _cli_rc_action="updated"
    fi
    if {
      printf '\n%s\n' "$GUARD"
      # shellcheck disable=SC2016  # $PATH is emitted literally for the target shell.
      printf 'export PATH="%s:$PATH"\n' "$CLI_DIR"
      printf 'export CORTEX_API_URL="%s"\n' "$CORTEX_API_URL"
      printf '# <<< kaidera-cortex-cli <<<\n'
    } >> "$RC" 2>/dev/null; then
      ok "Cortex CLI ${_cli_rc_action} PATH block in $RC ($CLI_COUNT cortex-* commands) — open a new shell, or: source $RC"
    else
      skip "couldn't persist the Cortex CLI PATH — add manually: export PATH=\"$CLI_DIR:\$PATH\""
    fi
  fi
else
  skip "Cortex CLI dir not found ($CLI_DIR) — skipping PATH setup"
fi

# --- 5. Seed defaults ---------------------------------------------------------------------
say "5/7 Defaults"
# No agents are pre-seeded — the operator names their first worker from the console (Inc 6
# onboarding). A future seeder may pre-stage a standard roster; for now this is by design.
ok "No agents are pre-seeded — open the console → Get Started to create your project and name your first worker."

# --- 6. Ready -----------------------------------------------------------------------------
say "6/7 Ready"

# Cortex admin SMOKE-TEST — prove the EXACT admin path 'create project' uses works NOW. A token
# or connectivity break fails HERE (loud, with the one-line fix) instead of surfacing later as a
# cryptic "admin token not configured" the first time you create a project. cortex-api's
# require-admin compares the X-Cortex-Admin-Token header to its own CORTEX_ADMIN_TOKEN: 403 means
# the two sides disagree (cortex-api wasn't recreated after the token landed in local-cortex/.env);
# 000 means cortex-api is unreachable. Any other code = auth accepted.
if command -v curl >/dev/null 2>&1; then
  _SMOKE_TOK="${CORTEX_ADMIN_TOKEN:-$ADMIN_TOKEN}"
  _SMOKE_CODE="$( curl -s -o /dev/null -w '%{http_code}' --max-time 6 \
                    -H "X-Cortex-Admin-Token: $_SMOKE_TOK" "$CORTEX_API_URL/beat/roles" 2>/dev/null || echo 000 )"
  case "$_SMOKE_CODE" in
    403) printf '\033[1;31m  ✗ Cortex admin token MISMATCH - the console and cortex-api disagree on CORTEX_ADMIN_TOKEN,\n     so CREATING A PROJECT WILL FAIL. Fix - recreate cortex-api to re-read local-cortex/.env, then\n     restart the console:\n       docker compose -p %s -f %s up -d --force-recreate --no-deps cortex-api\n       sudo systemctl restart kaidera-os-console\033[0m\n' "$COMPOSE_PROJECT" "$CORTEX_COMPOSE" ;;
    000) printf '\033[1;31m  ✗ Cortex UNREACHABLE at %s — creating a project WILL fail. Check it is Up:\n       docker compose -p %s -f %s ps\033[0m\n' "$CORTEX_API_URL" "$COMPOSE_PROJECT" "$CORTEX_COMPOSE" ;;
    *)   ok "Cortex admin auth OK — token accepted, project creation will work (the probe's HTTP $_SMOKE_CODE from /beat/roles is the EXPECTED success here: any non-403/000 means auth passed)" ;;
  esac
else
  skip "curl not found — skipping the Cortex admin smoke-test (install otherwise complete)"
fi

# Prefer the Tailscale/VPN IP — `hostname -I`'s FIRST address is usually the cloud-INTERNAL
# IP (e.g. GCP 10.x), which a laptop can't reach. Surface the VPN IP so the operator opens
# the right URL instead of guessing.
_IPS="$( (hostname -I 2>/dev/null || true) | tr -s ' ')"
_TSIP="$( (command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1) || true )"

# First-run auth hint. Hosted/shared installs run first-party auth, where the
# first email that signs in while the user table is empty becomes the admin.
# Local Mac packages are private desktop apps, so they default auth OFF and skip
# login-email delivery entirely. When auth is enabled, derive the effective
# delivery the same way auth.py does: explicit KAIDERA_AUTH_EMAIL_DELIVERY wins,
# else `graph` when a Graph client secret is set, else `smtp` when an SMTP host
# is set, else `log`.
_DELIVERY="$(_env_or_dotenv KAIDERA_AUTH_EMAIL_DELIVERY)"
if [ -z "$_DELIVERY" ]; then
  if [ -n "$(_env_or_dotenv KAIDERA_AUTH_GRAPH_CLIENT_SECRET)" ]; then _DELIVERY="graph"
  elif [ -n "$(_env_or_dotenv KAIDERA_SMTP_HOST)" ]; then _DELIVERY="smtp"
  else _DELIVERY="log"; fi
fi
if [ "$_DELIVERY" = "log" ]; then
  _DELIVERY_HINT="$(printf 'delivery is "log" (no email configured): after you request a code, read it from the console journal:\n       journalctl -u kaidera-os-console | grep -i "sign-in code"\n     (or watch live: journalctl -u kaidera-os-console -f). Configure SMTP/Graph email later - see redistributable/docs/LOCAL_CORTEX_QUICKSTART.md.')"
else
  _DELIVERY_HINT="delivery is \"$_DELIVERY\": the sign-in code is emailed to you."
fi
if [ "$AUTH_ENABLED" = "0" ]; then
  _AUTH_HINT="$(printf '  AUTH - disabled for this local/private install. No email sign-in is required.\n  To require sign-in later, re-run with KAIDERA_AUTH_ENABLED=1 ./install.sh and configure SMTP/Graph if you want emailed codes.')"
else
  _AUTH_HINT="$(printf '  SIGN IN — the FIRST email you sign in with becomes the ADMIN (it is created on first login\n  while the user table is empty; everyone after is added by that admin). On the login page,\n  enter your email and request a code. %s' "$_DELIVERY_HINT")"
fi
cat <<EOF

  The console is running as a background service (auto-starts on boot).
$( [ "$OS" = "Darwin" ] && echo "  Manage it:  $CONSOLE_DIR/scripts/kaidera-os operator status|restart|stop|start" )
$( [ "$OS" = "Darwin" ] && echo "  Logs:       tail -f $REPO_ROOT/local-cortex/logs/kaidera-os-console.launchd.err.log" )
$( [ "$OS" != "Darwin" ] && echo "  Manage it:  systemctl status|restart|stop kaidera-os-console   ·   logs: journalctl -u kaidera-os-console -f" )
  (or run it in the foreground for debugging: $RUNNER)

  Open it in a browser:
    On this machine:        http://127.0.0.1:$CONSOLE_PORT
$( [ "$CONSOLE_HOST" = "0.0.0.0" ] && [ -n "$_TSIP" ] && echo "    Over Tailscale (VPN):   http://$_TSIP:$CONSOLE_PORT   ← use this from your laptop" )
$( [ "$CONSOLE_HOST" = "0.0.0.0" ] && echo "    Bound to 0.0.0.0 — reachable on ANY of this VM's IPs:$_IPS(pick your VPN/LAN one, NOT the cloud-internal 10.x)." )
$( [ "$CONSOLE_HOST" = "0.0.0.0" ] && echo "    ⚠ enable KAIDERA_AUTH_ENABLED=1 + HTTPS for shared use; keep port $CONSOLE_PORT off the public internet." )

${_AUTH_HINT}

  FIRST RUN — open Settings → Providers and add at least ONE provider API key
  (e.g. Ollama Cloud for kimi, or OpenAI / Anthropic). The default 'kaidera' harness
  needs no CLI: it calls providers directly with the key you store in Settings.

  Cortex CLI: open a new shell (or \`source $PROFILE_D\`), then \`cortex-boot <name>\`.
  Herdr runtime: $HERDR_BIN
  Local embedding fallback: re-run with \`KAIDERA_CORTEX_LOCAL_EMBED=1 ./install.sh\` to start the PyTorch sentence-transformer worker.
  Multimodal audio/vision (L5): re-run with \`KAIDERA_CORTEX_PROFILE=full ./install.sh\` (also starts local embed; vision pulls an ~8 GB model).
  Clean wipe for a fresh re-deploy: \`./uninstall.sh\`.

EOF
ok "Install complete."
