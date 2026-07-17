"""kaidera-os — the Kaidera OS deployment CLI (packaging Inc 1).

Productizes the manual `tar app+spa/dist → restart kaidera-os-console` upgrade we ran by
hand, adding an automatic **rollback** when the new build fails its health check. See
`local-cortex/docs/2026-06-19-kaidera-os-packaging-design.md`.

Inc 1 scope (this file): manage an ALREADY-installed native console —
  kaidera-os upgrade <artifact>   swap app/ + spa/dist, migrate, restart, health-check, rollback-on-fail
  kaidera-os status | restart | start | stop
  kaidera-os version              installed console version
  kaidera-os install [-- args…]   delegate to install.sh (first-time bootstrap)

`<artifact>` is a release tarball (`kaidera-os-<v>.tgz` containing `app/` + `spa/dist`), a
directory holding those, or an `https://` URL to a tarball. The console + Cortex layout
(CONSOLE_DIR, the `kaidera-os-console` systemd unit, the `harness-appdb-migrate` compose
one-shot) matches install.sh.

The upgrade ORCHESTRATION (`do_upgrade`) takes injectable `run`/`http_status_version` callables
so the backup→restart→rollback money-path is unit-testable without a real systemd/Docker.
Deferred to later increments: brew tap, npm launcher, `kaidera-os rollback <version>` history,
release-digest Cortex reconcile, launchd (macOS) service control.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

SERVICE = os.environ.get("KAIDERA_OS_SERVICE", "kaidera-os-console")  # fitness:allow-literal infrastructure-constant
DEFAULT_COMPOSE_PROJECT = "kaidera-os-cortex"  # fitness:allow-literal product stack default, overridable per install
# The two directories that make up a console release (everything that changes per version).
RELEASE_DIRS = ("app", "spa/dist")


def console_dir() -> Path:
    """The installed console root. This module lives at console/app/kaidera_os_cli.py, so the
    console is two parents up; env-overridable for tests / non-standard installs."""
    env = os.environ.get("KAIDERA_OS_CONSOLE_DIR", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def console_port() -> int:
    return int(os.environ.get("CONSOLE_PORT", "8765"))


# ── small process / http seams (injectable so do_* is testable) ─────────────
def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, inheriting stdio. argv list → no shell."""
    return subprocess.run(cmd, **kw)


def _http_status_version(url: str, timeout: float = 5.0) -> Optional[str]:
    """GET {url} → the JSON `version`, or None on any failure (the health probe)."""
    try:
        import json

        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — localhost health
            return (json.loads(resp.read().decode()) or {}).get("version")
    except Exception:
        return None


# ── service control (systemd; sudo fallback) ────────────────────────────────
def _systemctl(action: str, run: Callable = _run) -> bool:
    """`systemctl <action> kaidera-os-console`, falling back to sudo. True on success.
    Returns False (with a hint) when systemd isn't present — the caller decides."""
    if not shutil.which("systemctl"):
        print(f"  (no systemd — restart the console manually: it serves :{console_port()})")
        return False
    for argv in ([
        "systemctl", action, SERVICE,
    ], ["sudo", "systemctl", action, SERVICE]):
        try:
            if run(argv).returncode == 0:
                return True
        except Exception as exc:
            print(f"  ({' '.join(argv)} failed: {exc})", file=sys.stderr)
            continue
    return False


# ── artifact resolution + integrity (a release becomes the live process) ─────
def _verify_digest(data: bytes, expected: str) -> None:
    """Abort unless sha256(data) == expected. The out-of-band integrity pin: the operator
    gets the digest from the release page, NOT from the same channel as the bytes."""
    got = hashlib.sha256(data).hexdigest()
    if got.lower() != expected.strip().lower():
        raise SystemExit(
            f"kaidera-os: sha256 mismatch — got {got}, expected {expected.strip()} (refusing to install)"
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so a pinned release URL resolves DIRECTLY and can't be
    bounced to an attacker-controlled host after the operator pinned + digested it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit(f"kaidera-os: refusing redirect to {newurl} — pin the final release URL")


def _download(url: str, timeout: float = 60.0) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(url, timeout=timeout) as resp:  # noqa: S310 — https-only + sha256-verified by caller
        return resp.read()


def _has_release_dirs(src: Path) -> bool:
    return all((src / d).exists() for d in RELEASE_DIRS)


def _release_console_root(src: Path) -> Path:
    """Return the console directory inside a materialized release.

    Older ``kaidera-os upgrade`` artifacts contained ``app/`` + ``spa/dist`` at the
    artifact root. The canonical signed release created by ``dist/release.sh`` is
    a full repository archive, so those directories live under
    ``local-cortex/console`` and usually one top-level ``kaidera-os-vX.Y.Z``
    directory. Accept both layouts so the rollback-capable upgrader can consume
    the exact artifact the publisher signs.
    """
    candidates: list[Path] = []

    def add(path: Path) -> None:
        if path not in candidates:
            candidates.append(path)

    add(src)
    try:
        top_dirs = [p for p in src.iterdir() if p.is_dir()]
    except OSError:
        top_dirs = []
    if len(top_dirs) == 1:
        add(top_dirs[0])

    for base in list(candidates):
        add(base / "console")
        add(base / "local-cortex" / "console")

    for candidate in candidates:
        if _has_release_dirs(candidate):
            return candidate
    return src


def _materialize(artifact: str, workdir: Path, sha256: Optional[str] = None) -> Path:
    """Resolve an artifact (tarball path / directory / https URL) to a directory holding
    `app/` + `spa/dist`.

    SECURITY (an installed release becomes the live console process → RCE surface):
      1. refuse insecure `http://`;
      2. REQUIRE an out-of-band `sha256` for any `https://` download and verify the bytes
         BEFORE writing/extracting (https proves you reached a server, not that the bytes
         are yours);
      3. refuse redirects (the pinned URL must resolve directly);
      4. extract with `filter='data'` so no member escapes the destination.
    A local tarball is verified too when `sha256` is supplied."""
    if artifact.startswith("http://"):
        raise SystemExit("kaidera-os: refusing insecure http:// artifact — use https:// with --sha256")
    if artifact.startswith("https://"):
        if not sha256:
            raise SystemExit(
                "kaidera-os: a remote (https) artifact requires --sha256 <digest> (integrity pin)"
            )
        data = _download(artifact)
        _verify_digest(data, sha256)
        dl = workdir / "release.tgz"
        dl.write_bytes(data)
        artifact = str(dl)
        sha256 = None  # the downloaded bytes are already verified
    p = Path(artifact)
    if p.is_dir():
        return _release_console_root(p)
    if p.is_file() and (p.name.endswith((".tgz", ".tar.gz", ".tar"))):
        if sha256:
            _verify_digest(p.read_bytes(), sha256)
        dest = workdir / "extracted"
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(p) as tf:
            tf.extractall(dest, filter="data")  # filter='data' — refuse path-escaping members
        return _release_console_root(dest)
    raise SystemExit(f"kaidera-os: artifact not found or unsupported: {artifact}")


# ── the upgrade money-path (injectable seams → unit-testable) ───────────────
def do_upgrade(
    artifact: str,
    *,
    cdir: Optional[Path] = None,
    run: Callable = _run,
    http_status_version: Callable[[str], Optional[str]] = _http_status_version,
    expected_version: Optional[str] = None,
    skip_migrate: bool = False,
    sha256: Optional[str] = None,
    health_attempts: int = 30,
    health_sleep: Callable = time.sleep,
) -> int:
    """Swap in a new console release with automatic rollback.

    Steps: materialize artifact → BACKUP current app/+spa/dist → replace → app-DB
    migrate (compose one-shot) → restart service → health-check /console/version →
    on failure, RESTORE the backup + restart (rollback). Returns a process exit code."""
    cdir = cdir or console_dir()
    if not (cdir / "app").is_dir():
        print(f"kaidera-os: no console at {cdir} (run `kaidera-os install` first)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="kaidera-os-") as tmp:
        work = Path(tmp)
        src = _materialize(artifact, work, sha256=sha256)
        if not _has_release_dirs(src):
            print(f"kaidera-os: artifact missing {RELEASE_DIRS} (incomplete release)", file=sys.stderr)
            return 2

        # 1. BACKUP the live dirs (for rollback) into a sibling temp tree.
        backup = work / "backup"
        for d in RELEASE_DIRS:
            live = cdir / d
            if live.exists():
                dst = backup / d
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(live, dst)
        print(f"kaidera-os: backed up {', '.join(RELEASE_DIRS)}")

        def _restore() -> None:
            for d in RELEASE_DIRS:
                b = backup / d
                if b.exists():
                    live = cdir / d
                    if live.exists():
                        shutil.rmtree(live)
                    shutil.copytree(b, cdir / d)

        # 2. Replace the live dirs with the new release.
        try:
            for d in RELEASE_DIRS:
                live = cdir / d
                if live.exists():
                    shutil.rmtree(live)
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src / d, live)
            print("kaidera-os: installed new app/ + spa/dist")

            # 3. App-DB migrations (idempotent, forward-only). Best-effort: a missing
            #    compose / Docker doesn't abort an otherwise-valid console swap.
            if not skip_migrate:
                _migrate(cdir, run)

            # 4. Restart + 5. health-check.
            if not _systemctl("restart", run):
                raise RuntimeError("console restart failed — restart manually / check systemd+sudo")
            # `systemctl restart` returns once systemd SPAWNS uvicorn, NOT once it has bound
            # the port + finished its lifespan startup — so a single immediate poll races the
            # boot and gets connection-refused → None → a false rollback. Poll with a bounded
            # retry (health_sleep is injectable so tests don't actually sleep).
            url = f"http://127.0.0.1:{console_port()}/console/version"
            ok_version = None
            for attempt in range(max(1, health_attempts)):
                ok_version = http_status_version(url)
                if ok_version is not None and (
                    expected_version is None or ok_version == expected_version
                ):
                    break
                if attempt < health_attempts - 1:
                    health_sleep(1.0)
            healthy = ok_version is not None and (
                expected_version is None or ok_version == expected_version
            )
            if not healthy:
                raise RuntimeError(
                    f"health check failed (got version={ok_version!r}, expected={expected_version!r})"
                )
        except Exception as exc:
            print(f"kaidera-os: upgrade FAILED ({exc}) — rolling back", file=sys.stderr)
            _restore()
            _systemctl("restart", run)
            return 1

    print(f"kaidera-os: upgraded ✓ (console reports {ok_version})")
    return 0


def _migrate(cdir: Path, run: Callable = _run) -> None:
    """Run the app-DB migration one-shot (the `harness-appdb-migrate` compose service).
    Best-effort — logs and continues if compose/Docker isn't reachable."""
    compose = cdir.parent.parent / ".agents" / "docker-compose.cortex.yml"
    if not shutil.which("docker") or not compose.exists():
        print("  (skipping migrate — docker/compose not found; run it manually if schema changed)")
        return
    compose_project = os.environ.get(
        "KAIDERA_OS_COMPOSE_PROJECT",
        os.environ.get("KAIDERA_COMPOSE_PROJECT", DEFAULT_COMPOSE_PROJECT),
    )
    try:
        result = run([
            "docker", "compose", "-p", compose_project, "-f", str(compose),
            "up", "--no-deps", "harness-appdb-migrate",
        ])
    except Exception as exc:
        raise RuntimeError(f"app-db migration failed: {exc}") from exc
    rc = getattr(result, "returncode", 0)
    if rc != 0:
        raise RuntimeError(f"app-db migration failed (exit {rc})")


# ── thin command wrappers ────────────────────────────────────────────────────
def do_status() -> int:
    v = _http_status_version(f"http://127.0.0.1:{console_port()}/console/version")
    print(f"console: {'up' if v else 'down'} (version={v or '?'}) on :{console_port()}")
    if shutil.which("systemctl"):
        _run(["systemctl", "--no-pager", "status", SERVICE], check=False)
    return 0 if v else 1


def do_version() -> int:
    try:
        from app.version import __version__

        print(__version__)
        return 0
    except Exception:
        v = _http_status_version(f"http://127.0.0.1:{console_port()}/console/version")
        print(v or "unknown")
        return 0 if v else 1


def do_install(extra: list[str]) -> int:
    """Delegate first-time install to install.sh (it bootstraps deps + Cortex + the venv)."""
    script = console_dir().parent.parent / "install.sh"
    if not script.exists():
        print(f"kaidera-os: install.sh not found at {script}", file=sys.stderr)
        return 2
    return _run(["bash", str(script), *extra]).returncode


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="kaidera-os", description="Kaidera OS deployment CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upgrade", help="swap in a new console release (rollback on failure)")
    up.add_argument("artifact", help="release .tgz path, a directory, or an https:// URL")
    up.add_argument("--sha256", dest="sha256", default=None,
                    help="REQUIRED for an https:// artifact: the expected sha256 of the tarball "
                         "(integrity pin; also verifies a local .tgz when given)")
    up.add_argument("--expect", dest="expected_version", default=None,
                    help="health gate (NOT a security control): the version the console must "
                         "report after restart, else roll back")
    up.add_argument("--skip-migrate", action="store_true", help="don't run app-DB migrations")

    sub.add_parser("status", help="is the console up + which version")
    sub.add_parser("version", help="print the installed console version")
    for a in ("restart", "start", "stop"):
        sub.add_parser(a, help=f"{a} the kaidera-os-console service")
    ins = sub.add_parser("install", help="first-time install (delegates to install.sh)")
    ins.add_argument("extra", nargs=argparse.REMAINDER, help="args passed through to install.sh")
    ns = p.parse_args(argv)
    if ns.cmd == "upgrade":
        return do_upgrade(ns.artifact, expected_version=ns.expected_version,
                          skip_migrate=ns.skip_migrate, sha256=ns.sha256)
    if ns.cmd == "status":
        return do_status()
    if ns.cmd == "version":
        return do_version()
    if ns.cmd in ("restart", "start", "stop"):
        return 0 if _systemctl(ns.cmd) else 1
    if ns.cmd == "install":
        return do_install(ns.extra)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
