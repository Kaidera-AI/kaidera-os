#!/usr/bin/env python3
"""Verify the Kaidera OS installer Cortex preservation contract.

The contract:
- If Cortex already exists, `install.sh` converges it in place and preserves
  named data volumes plus local env secrets.
- If Cortex does not exist, `install.sh` can provision a fresh local Cortex.
- The installer must not contain destructive volume/database wipe commands.

Usage:
  scripts/install/verify-cortex-install-contract.py static
  scripts/install/verify-cortex-install-contract.py snapshot --output before.json
  ./install.sh
  scripts/install/verify-cortex-install-contract.py snapshot --output after.json
  scripts/install/verify-cortex-install-contract.py compare before.json after.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


CORTEX_CONTAINERS = ("cortex-pg", "cortex-api", "harness-appdb")
CORTEX_VOLUME_SUFFIXES = ("cortex-pg-data", "harness-appdb-data")
ENV_SECRET_KEYS = ("CORTEX_ADMIN_TOKEN", "KAIDERA_AUTH_SECRET")

DESTRUCTIVE_PATTERNS = (
    (r"\bdocker\s+compose\b[^\n]*\bdown\b[^\n]*(?:-v|--volumes)\b", "docker compose down with volume deletion"),
    (r"\bdocker\s+volume\s+rm\b", "docker volume rm"),
    (r"\bdocker\s+compose\b[^\n]*\brm\b", "docker compose rm"),
    (r"\brm\s+-rf\b[^\n]*(?:local-cortex|\.agents|cortex-pg-data|harness-appdb-data)", "rm -rf against Cortex/runtime state"),
)

REQUIRED_STATIC_MARKERS = (
    ("reusing the Cortex admin token", "installer reuses an existing Cortex admin token"),
    ("generated a Cortex admin token", "installer can generate a fresh Cortex admin token"),
    ("reusing the console auth secret", "installer reuses the console auth secret"),
    ("docker compose -p \"$COMPOSE_PROJECT\" -f \"$CORTEX_COMPOSE\"", "installer uses canonical compose project/file"),
    ("up -d --build", "installer converges services with compose up, not destructive recreate"),
    ("--force-recreate --no-deps cortex-api", "installer only force-recreates the stateless API after env changes"),
    ("existing Cortex detected", "installer detects an existing Cortex before convergence"),
    ("no existing Cortex detected", "installer reports fresh Cortex provisioning"),
    ("adopting explicitly named existing Docker Compose project", "installer adopts an explicitly named existing compose project"),
    ("project bind root", "installer uses a host-shareable projects bind root"),
    ("KAIDERA_CORTEX_LOCAL_EMBED", "installer exposes local embed worker as an opt-in fallback"),
    ("local embed worker enabled", "installer reports when the local embed worker is enabled"),
    ("for _harness in claude codex pi; do", "installer probes the supported external AI harnesses"),
    ("no AI CLI harness detected - the app will install", "installer degrades cleanly when no AI harness is installed"),
    ("Kaidera OS discovers each installed harness's models", "installer reports dynamic external harness discovery"),
    ("KAIDERA_SMTP_FROM=", "installer supports a configurable sender mailbox"),
    ("KAIDERA_SMTP_PASSWORD=<relay-password>", "installer never ships an SMTP password"),
    ("auth disabled for local/private console", "installer disables auth for local/private Mac installs"),
    ("KAIDERA_AUTH_ENABLED=\"$AUTH_ENABLED\"", "installer injects the resolved auth mode into the runner"),
    ("discarded relocated or broken console virtual environment", "installer rebuilds a virtual environment after the repository moves"),
    ("kaidera-path-pre-refresh", "installer refreshes an existing Cortex CLI path after the repository moves"),
    ("KAIDERA_PUBLIC_BASE_URL", "installer carries hosted public base URL into the runner/unit"),
    ("KAIDERA_AUTH_ORIGIN", "installer carries hosted auth origin into the runner/unit"),
    ("KAIDERA_AUTH_COOKIE_DOMAIN", "installer carries hosted auth cookie domain into the runner/unit"),
    ("KAIDERA_AUTH_RP_ID", "installer carries hosted auth relying-party id into the runner/unit"),
    ("KAIDERA_AUTH_TRUSTED_PROXY", "installer carries hosted trusted-proxy mode into the runner/unit"),
    ("hosted auth deployment options wired into the runner + unit", "installer reports hosted auth deployment wiring"),
    ("KAIDERA_OS_EXTENSION_MODULES", "installer carries project-pack extension modules into the runner/unit"),
    ("KAIDERA_OS_EXTENSION_PATHS", "installer carries project-pack extension paths into the runner/unit"),
    ("project-pack extensions wired into the runner + unit", "installer reports project-pack extension wiring"),
    ("PATH=\"\\$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin\"", "installer preserves PATH for subscription harness CLIs"),
    ("--timeout-graceful-shutdown 5", "installer bounds uvicorn graceful shutdown for reliable operator stop/start"),
    ("ai.kaidera.kaidera-os.console", "installer writes the canonical Kaidera OS LaunchAgent"),
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


RunFn = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def static_checks(root: Path) -> list[Check]:
    install = root / "install.sh"
    compose = root / ".agents" / "docker-compose.cortex.yml"
    checks: list[Check] = []
    if not install.exists():
        return [Check("install_script", "fail", f"missing {install}")]
    text = install.read_text(encoding="utf-8", errors="replace")

    checks.append(Check("install_script", "ok", str(install)))
    for pattern, label in DESTRUCTIVE_PATTERNS:
        match = re.search(pattern, text)
        checks.append(
            Check(
                f"no_{label.replace(' ', '_')}",
                "fail" if match else "ok",
                f"found destructive command: {match.group(0)}" if match else f"no {label}",
            )
        )
    for marker, label in REQUIRED_STATIC_MARKERS:
        checks.append(
            Check(
                f"marker_{label.replace(' ', '_')}",
                "ok" if marker in text else "fail",
                label if marker in text else f"missing marker: {marker}",
            )
        )

    if compose.exists():
        compose_text = compose.read_text(encoding="utf-8", errors="replace")
        for suffix in CORTEX_VOLUME_SUFFIXES:
            checks.append(
                Check(
                    f"compose_named_volume_{suffix}",
                    "ok" if suffix in compose_text else "fail",
                    f"{suffix} present" if suffix in compose_text else f"{suffix} missing",
                )
            )
        checks.append(
            Check(
                "compose_initdb_once",
                "ok" if "runs ONCE on an empty data volume" in compose_text else "fail",
                "Postgres initdb is documented as empty-volume-only",
            )
        )
    else:
        checks.append(Check("compose_file", "fail", f"missing {compose}"))
    return checks


def _docker_json(args: Sequence[str], *, run: RunFn) -> Any | None:
    result = run(args)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def inspect_container(name: str, *, run: RunFn) -> dict[str, Any]:
    data = _docker_json(["docker", "inspect", name], run=run)
    if not data:
        return {"exists": False}
    item = data[0]
    state = item.get("State") or {}
    health = state.get("Health") or {}
    mounts = item.get("Mounts") or []
    return {
        "exists": True,
        "id": item.get("Id"),
        "name": str(item.get("Name") or "").lstrip("/"),
        "image": item.get("Image"),
        "created": item.get("Created"),
        "state": state.get("Status"),
        "health": health.get("Status"),
        "mounts": [
            {
                "type": mount.get("Type"),
                "name": mount.get("Name"),
                "source": mount.get("Source"),
                "destination": mount.get("Destination"),
            }
            for mount in mounts
        ],
    }


def list_matching_volumes(*, run: RunFn) -> dict[str, list[dict[str, Any]]]:
    result = run(["docker", "volume", "ls", "--format", "{{.Name}}"])
    volume_names = result.stdout.splitlines() if result.returncode == 0 else []
    matched: dict[str, list[dict[str, Any]]] = {suffix: [] for suffix in CORTEX_VOLUME_SUFFIXES}
    for name in volume_names:
        for suffix in CORTEX_VOLUME_SUFFIXES:
            if name == suffix or name.endswith(f"_{suffix}"):
                data = _docker_json(["docker", "volume", "inspect", name], run=run)
                if data:
                    item = data[0]
                    matched[suffix].append(
                        {
                            "name": item.get("Name"),
                            "driver": item.get("Driver"),
                            "mountpoint": item.get("Mountpoint"),
                            "created_at": item.get("CreatedAt"),
                            "labels": item.get("Labels") or {},
                        }
                    )
    return matched


def cortex_health(url: str, *, run: RunFn) -> dict[str, Any]:
    curl = shutil.which("curl")
    if not curl:
        return {"checked": False, "ok": False, "detail": "curl not found"}
    result = run([curl, "-fsS", "--max-time", "3", f"{url.rstrip('/')}/health"])
    return {
        "checked": True,
        "ok": result.returncode == 0,
        "detail": (result.stdout or result.stderr).strip(),
    }


def build_snapshot(root: Path, *, run: RunFn = run_command, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    compose_project = env.get("KAIDERA_COMPOSE_PROJECT", "kaidera-os-cortex")
    cortex_url = env.get("CORTEX_API_URL", "http://localhost:8501")
    dotenv_path = root / "local-cortex" / ".env"
    dotenv = read_dotenv(dotenv_path)
    secrets = {
        key: {
            "present": bool(dotenv.get(key)),
            "sha256": hash_value(dotenv[key]) if dotenv.get(key) else None,
        }
        for key in ENV_SECRET_KEYS
    }
    docker_ok = run(["docker", "info"]).returncode == 0
    containers = {name: inspect_container(name, run=run) for name in CORTEX_CONTAINERS} if docker_ok else {}
    volumes = list_matching_volumes(run=run) if docker_ok else {suffix: [] for suffix in CORTEX_VOLUME_SUFFIXES}
    health = cortex_health(cortex_url, run=run)
    signals: list[str] = []
    signals.extend(f"container:{name}" for name, item in containers.items() if item.get("exists"))
    for suffix, items in volumes.items():
        signals.extend(f"volume:{item.get('name')}" for item in items)
    if any(secret["present"] for secret in secrets.values()):
        signals.append("env:local-cortex/.env")
    if health.get("ok"):
        signals.append(f"health:{cortex_url}")
    return {
        "kind": "cortex_install_contract_snapshot",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(root),
        "compose_project": compose_project,
        "cortex_api_url": cortex_url,
        "docker_available": docker_ok,
        "existing_cortex_detected": bool(signals),
        "signals": signals,
        "env_file": {
            "path": str(dotenv_path),
            "exists": dotenv_path.exists(),
            "secrets": secrets,
        },
        "containers": containers,
        "volumes": volumes,
        "health": health,
    }


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    before_existing = bool(before.get("existing_cortex_detected"))
    after_existing = bool(after.get("existing_cortex_detected"))
    checks.append(
        Check(
            "after_cortex_detected",
            "ok" if after_existing else "fail",
            "Cortex signals exist after install" if after_existing else "no Cortex signals after install",
        )
    )

    before_secrets = (((before.get("env_file") or {}).get("secrets")) or {})
    after_secrets = (((after.get("env_file") or {}).get("secrets")) or {})
    for key in ENV_SECRET_KEYS:
        old = before_secrets.get(key) or {}
        new = after_secrets.get(key) or {}
        if old.get("present"):
            ok = old.get("sha256") == new.get("sha256")
            checks.append(
                Check(
                    f"preserve_secret_{key}",
                    "ok" if ok else "fail",
                    "secret hash preserved" if ok else "secret hash changed",
                )
            )
        else:
            checks.append(
                Check(
                    f"fresh_secret_{key}",
                    "ok" if new.get("present") else "fail",
                    "secret present after fresh install" if new.get("present") else "secret missing after install",
                )
            )

    before_volumes = before.get("volumes") or {}
    after_volumes = after.get("volumes") or {}
    for suffix in CORTEX_VOLUME_SUFFIXES:
        old_items = before_volumes.get(suffix) or []
        new_items = after_volumes.get(suffix) or []
        if old_items:
            old_by_name = {item.get("name"): item for item in old_items}
            new_by_name = {item.get("name"): item for item in new_items}
            missing = sorted(name for name in old_by_name if name not in new_by_name)
            changed = sorted(
                name
                for name in old_by_name
                if name in new_by_name
                and old_by_name[name].get("created_at")
                and new_by_name[name].get("created_at")
                and old_by_name[name].get("created_at") != new_by_name[name].get("created_at")
            )
            ok = not missing and not changed
            detail = "existing volume(s) preserved" if ok else f"missing={missing}, recreated={changed}"
            checks.append(Check(f"preserve_volume_{suffix}", "ok" if ok else "fail", detail))
        else:
            checks.append(
                Check(
                    f"fresh_volume_{suffix}",
                    "ok" if new_items else "fail",
                    "volume present after fresh install" if new_items else "volume missing after install",
                )
            )

    before_containers = before.get("containers") or {}
    after_containers = after.get("containers") or {}
    if before_existing:
        for name in CORTEX_CONTAINERS:
            if (before_containers.get(name) or {}).get("exists"):
                exists_after = bool((after_containers.get(name) or {}).get("exists"))
                checks.append(
                    Check(
                        f"preserve_container_presence_{name}",
                        "ok" if exists_after else "fail",
                        "container still exists" if exists_after else "container missing after install",
                    )
                )
    return checks


def report(checks: Sequence[Check], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ready": all(check.status == "ok" for check in checks),
        "checks": [asdict(check) for check in checks],
        **(extra or {}),
    }


def print_report(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for check in payload.get("checks", []):
        print(f"{check['status'].upper()} {check['name']}: {check['detail']}")
    print(f"ready: {str(payload.get('ready')).lower()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify Cortex install preservation contract")
    parser.add_argument("--root", type=Path, default=repo_root_from_script())
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("static")
    snap = sub.add_parser("snapshot")
    snap.add_argument("--output", type=Path)
    cmp_parser = sub.add_parser("compare")
    cmp_parser.add_argument("before", type=Path)
    cmp_parser.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    cmd = args.cmd or "static"
    root = args.root.resolve()

    if cmd == "static":
        payload = report(static_checks(root), extra={"mode": "static"})
        print_report(payload, as_json=args.json)
        return 0 if payload["ready"] else 1
    if cmd == "snapshot":
        payload = build_snapshot(root)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else f"snapshot: {payload['generated_at']} existing={payload['existing_cortex_detected']}")
        return 0
    if cmd == "compare":
        before = json.loads(args.before.read_text(encoding="utf-8"))
        after = json.loads(args.after.read_text(encoding="utf-8"))
        payload = report(compare_snapshots(before, after), extra={"mode": "compare"})
        print_report(payload, as_json=args.json)
        return 0 if payload["ready"] else 1
    parser.error(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
