#!/usr/bin/env bash
# Install/verify the external Herdr runtime prerequisite.
#
# Herdr is not bundled with Kaidera OS. This script installs it from upstream
# only when a pinned installer checksum is provided, and prints the resolved
# binary path for launch scripts to export as KAIDERA_OS_HERDR_BIN.

set -euo pipefail

PRINT_PATH=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print-path)
      PRINT_PATH=1
      shift
      ;;
    --help|-h)
      cat <<'EOF'
Usage:
  install-herdr-runtime.sh [--print-path]

Environment:
  KAIDERA_OS_HERDR_BIN     Use this Herdr binary if executable.
  KAIDERA_INSTALL_HERDR=0  Do not auto-install; fail if Herdr is missing.
  HERDR_INSTALL_DIR        Install dir for Herdr upstream installer.
  HERDR_INSTALL_URL        Override Herdr upstream installer URL.
  HERDR_INSTALL_SHA256     Required SHA-256 for the installer script.
EOF
      exit 0
      ;;
    *)
      printf 'ERROR: unknown option: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

say() { [ "$PRINT_PATH" = "1" ] || printf '\n\033[1;36m== %s ==\033[0m\n' "$*" >&2; }
ok()  { [ "$PRINT_PATH" = "1" ] || printf '  \033[32m✓\033[0m %s\n' "$*" >&2; }
die() { printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

is_executable() {
  [ -n "${1:-}" ] && [ -f "$1" ] && [ -x "$1" ]
}

brew_herdr_bin() {
  command -v brew >/dev/null 2>&1 || return 1
  prefix="$(brew --prefix herdr 2>/dev/null || true)"
  [ -n "$prefix" ] && is_executable "$prefix/bin/herdr" && printf '%s\n' "$prefix/bin/herdr"
}

find_herdr_bin() {
  if is_executable "${KAIDERA_OS_HERDR_BIN:-}"; then
    printf '%s\n' "$KAIDERA_OS_HERDR_BIN"
    return 0
  fi
  if command -v herdr >/dev/null 2>&1; then
    command -v herdr
    return 0
  fi
  if bin="$(brew_herdr_bin)"; then
    printf '%s\n' "$bin"
    return 0
  fi
  install_dir="${HERDR_INSTALL_DIR:-$HOME/.local/bin}"
  if is_executable "$install_dir/herdr"; then
    printf '%s\n' "$install_dir/herdr"
    return 0
  fi
  return 1
}

install_with_upstream_script() {
  command -v curl >/dev/null 2>&1 || die "curl not found; install curl or install Herdr manually and set KAIDERA_OS_HERDR_BIN."
  install_dir="${HERDR_INSTALL_DIR:-$HOME/.local/bin}"
  mkdir -p "$install_dir"
  url="${HERDR_INSTALL_URL:-https://herdr.dev/install.sh}"
  expected_sha="${HERDR_INSTALL_SHA256:-}"
  [ -n "$expected_sha" ] || die "Herdr auto-install requires HERDR_INSTALL_SHA256. Install Herdr manually or provide a pinned installer checksum."
  case "$url" in
    https://*) ;;
    *) die "HERDR_INSTALL_URL must use https:// when auto-installing Herdr." ;;
  esac

  tmp="$(mktemp)"
  curl -fsSL "$url" -o "$tmp"
  if command -v sha256sum >/dev/null 2>&1; then
    actual_sha="$(sha256sum "$tmp" | awk '{print $1}')"
  else
    actual_sha="$(shasum -a 256 "$tmp" | awk '{print $1}')"
  fi
  if [ "$actual_sha" != "$expected_sha" ]; then
    rm -f "$tmp"
    die "Herdr installer checksum mismatch: expected $expected_sha, got $actual_sha."
  fi
  HERDR_INSTALL_DIR="$install_dir" sh "$tmp" >&2
  rm -f "$tmp"
}

say "Herdr runtime"
HERDR_BIN="$(find_herdr_bin || true)"
if [ -z "$HERDR_BIN" ]; then
  if [ "${KAIDERA_INSTALL_HERDR:-1}" = "0" ]; then
    die "Herdr is required. Install it, or set KAIDERA_OS_HERDR_BIN to the herdr executable."
  fi

  install_with_upstream_script
  HERDR_BIN="$(find_herdr_bin || true)"
fi

[ -n "$HERDR_BIN" ] || die "Herdr install completed but the herdr binary was not found. Set KAIDERA_OS_HERDR_BIN."
VERSION="$("$HERDR_BIN" --version 2>&1 | head -1 || true)"
[ -n "$VERSION" ] || die "Herdr binary found at $HERDR_BIN but --version failed."

ok "herdr available: $VERSION ($HERDR_BIN)"
ok "Herdr remains an external upstream dependency; Kaidera OS does not bundle its source or binary."

if [ "$PRINT_PATH" = "1" ]; then
  printf '%s\n' "$HERDR_BIN"
fi
