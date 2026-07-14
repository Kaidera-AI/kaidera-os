#!/usr/bin/env python3
"""Prove the installed Kaidera OS Mac operator lifecycle contract.

The DMG smoke proves the artifact shape. This script proves the next layer:
an installed operator app can coexist with the canonical local Kaidera OS install,
and the canonical operator CLI can reach, stop, start, restart, and query update
state for that install.

Potentially mutating checks are explicit:
- update apply runs only with --apply-update
- opening the browser runs only with --open-console
- reboot survival is verified only when re-run after reboot with --post-reboot
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


APP_NAME = "Kaidera OS Operator.app"
READY_STATUSES = {"running"}
OPTIONAL_STATUSES = {"ok", "skip", "manual"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    required: bool = True


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def read_version(root: Path) -> str:
    text = (root / "local-cortex" / "console" / "app" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise RuntimeError("could not read Kaidera OS version")
    return match.group(1)


def cli_path(root: Path) -> Path:
    return root / "local-cortex" / "console" / "scripts" / "kaidera-os"


def default_artifact(root: Path, version: str) -> Path:
    return root / "dist" / "macos" / f"kaidera-os-operator-v{version}.dmg"


def default_output(root: Path, version: str) -> Path:
    return root / "output" / "release" / "kaidera-os-operator-macos" / "evidence" / (
        f"operator-lifecycle-v{version}.json"
    )


def parse_operator_json(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            "_parse_error": str(exc),
            "_returncode": result.returncode,
            "_stdout": (result.stdout or "").strip(),
            "_stderr": (result.stderr or "").strip(),
        }
    if isinstance(payload, dict):
        return payload
    return {"value": payload, "_returncode": result.returncode}


def run_operator(root: Path, args: Sequence[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["KAIDERA_OS_HOME"] = str(root.resolve())
    return run_command(
        ["bash", str(cli_path(root)), "operator", *args],
        cwd=root,
        env=env,
        timeout=timeout,
    )


def status_is_ready(payload: dict[str, object], *, allow_degraded: bool = False) -> bool:
    status = str(payload.get("status") or "")
    if status in READY_STATUSES:
        return True
    return allow_degraded and status == "degraded"


def status_is_stopped(payload: dict[str, object]) -> bool:
    return payload.get("status") == "stopped"


def result_ok(result: subprocess.CompletedProcess[str], payload: dict[str, object]) -> bool:
    return result.returncode == 0 and payload.get("ok", True) is not False


def compact_payload(payload: dict[str, object]) -> str:
    keys = ("status", "version", "console_url", "repo_root", "repo_root_found", "ok", "code", "error")
    small = {key: payload[key] for key in keys if key in payload}
    if "payload" in payload and isinstance(payload["payload"], dict):
        nested = payload["payload"]
        small["payload"] = {key: nested[key] for key in ("status", "version", "update_available", "job_id") if key in nested}
    if "_parse_error" in payload:
        small["_parse_error"] = payload["_parse_error"]
    return json.dumps(small, sort_keys=True)


def poll_status(
    run_status: Callable[[], subprocess.CompletedProcess[str]],
    predicate: Callable[[dict[str, object]], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, dict[str, object], int]:
    deadline = monotonic() + timeout_seconds
    attempts = 0
    last_payload: dict[str, object] = {}
    while True:
        attempts += 1
        result = run_status()
        last_payload = parse_operator_json(result)
        if result.returncode == 0 and predicate(last_payload):
            return True, last_payload, attempts
        if monotonic() >= deadline:
            return False, last_payload, attempts
        sleep(interval_seconds)


def attach_dmg(dmg_path: Path, mount_dir: Path) -> str:
    result = run_command(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount_dir), str(dmg_path)]
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or "hdiutil attach failed")
    for line in result.stdout.splitlines():
        if line.startswith("/dev/"):
            return line.split()[0]
    return str(mount_dir)


def detach_dmg(device_or_mount: str) -> None:
    if device_or_mount:
        run_command(["hdiutil", "detach", device_or_mount, "-quiet"])


def install_app_from_dmg(
    *,
    artifact: Path,
    install_dir: Path,
    replace_existing: bool,
) -> list[Check]:
    checks: list[Check] = []
    if shutil.which("hdiutil"):
        checks.append(Check("hdiutil_tool", "ok", shutil.which("hdiutil") or "hdiutil"))
    else:
        return [Check("hdiutil_tool", "fail", "hdiutil not found on PATH")]

    if not artifact.exists():
        return [*checks, Check("artifact_exists", "fail", f"missing {artifact}")]
    checks.append(Check("artifact_exists", "ok", str(artifact)))

    device = ""
    mounted = False
    with tempfile.TemporaryDirectory(prefix="kaidera-os-lifecycle-dmg.") as tmp:
        mount_dir = Path(tmp) / "mount"
        mount_dir.mkdir()
        try:
            device = attach_dmg(artifact, mount_dir)
            mounted = True
            checks.append(Check("dmg_mount", "ok", str(mount_dir)))
            source_app = mount_dir / APP_NAME
            if not source_app.is_dir():
                checks.append(Check("dmg_app_bundle", "fail", f"missing {source_app}"))
                return checks
            checks.append(Check("dmg_app_bundle", "ok", str(source_app)))

            install_dir.mkdir(parents=True, exist_ok=True)
            installed_app = install_dir / APP_NAME
            if installed_app.exists():
                if not replace_existing:
                    checks.append(
                        Check(
                            "app_install_destination",
                            "fail",
                            f"{installed_app} already exists; pass --replace-installed-app to overwrite",
                        )
                    )
                    return checks
                shutil.rmtree(installed_app)
            shutil.copytree(source_app, installed_app, symlinks=True)
            checks.append(Check("app_copy_install", "ok", str(installed_app)))
            binary = installed_app / "Contents" / "MacOS" / "Kaidera OS Operator"
            checks.append(
                Check(
                    "app_installed_binary",
                    "ok" if binary.is_file() and os.access(binary, os.X_OK) else "fail",
                    str(binary),
                )
            )
        except Exception as exc:
            checks.append(Check("dmg_install", "fail", str(exc)))
        finally:
            if mounted:
                detach_dmg(device)
                checks.append(Check("dmg_detach", "ok", device or str(mount_dir)))
    return checks


def add_command_check(
    checks: list[Check],
    *,
    name: str,
    result: subprocess.CompletedProcess[str],
    payload: dict[str, object],
    required: bool = True,
) -> None:
    ok = result_ok(result, payload)
    detail = compact_payload(payload)
    if not ok and result.stderr:
        detail = f"{detail}; stderr={result.stderr.strip()}"
    checks.append(Check(name, "ok" if ok else "fail", detail, required=required))


def run_lifecycle(
    *,
    root: Path,
    timeout_seconds: float,
    interval_seconds: float,
    allow_degraded: bool,
    apply_update_requested: bool,
    open_console_requested: bool,
    post_reboot: bool,
) -> list[Check]:
    checks: list[Check] = []
    cli = cli_path(root)
    if not cli.exists():
        return [Check("operator_cli", "fail", f"missing {cli}")]
    checks.append(Check("operator_cli", "ok", str(cli)))

    home_result = run_operator(root, ["home"])
    home_payload = parse_operator_json(home_result)
    add_command_check(checks, name="operator_home", result=home_result, payload=home_payload)

    def run_status() -> subprocess.CompletedProcess[str]:
        return run_operator(root, ["status"])

    initial = run_status()
    initial_payload = parse_operator_json(initial)
    if status_is_ready(initial_payload, allow_degraded=allow_degraded):
        checks.append(Check("initial_console_reachable", "ok", compact_payload(initial_payload)))
    else:
        checks.append(
            Check(
                "initial_console_reachable",
                "manual",
                f"not initially reachable; attempting start: {compact_payload(initial_payload)}",
                required=False,
            )
        )
        start_result = run_operator(root, ["start"])
        start_payload = parse_operator_json(start_result)
        add_command_check(checks, name="initial_start_command", result=start_result, payload=start_payload)
        ready, payload, attempts = poll_status(
            run_status,
            lambda data: status_is_ready(data, allow_degraded=allow_degraded),
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )
        checks.append(
            Check(
                "initial_start_reaches_console",
                "ok" if ready else "fail",
                f"attempts={attempts}; {compact_payload(payload)}",
            )
        )

    latest_status = run_status()
    latest_payload = parse_operator_json(latest_status)
    checks.append(
        Check(
            "console_url",
            "ok" if str(latest_payload.get("console_url") or "").startswith("http://127.0.0.1:") else "fail",
            compact_payload(latest_payload),
        )
    )

    if open_console_requested:
        open_result = run_operator(root, ["open"])
        open_payload = parse_operator_json(open_result)
        add_command_check(checks, name="open_console", result=open_result, payload=open_payload)
    else:
        checks.append(Check("open_console", "skip", "not requested; pass --open-console to launch browser", required=False))

    stop_result = run_operator(root, ["stop"])
    stop_payload = parse_operator_json(stop_result)
    add_command_check(checks, name="stop_command", result=stop_result, payload=stop_payload)
    stopped, stopped_payload, stopped_attempts = poll_status(
        run_status,
        status_is_stopped,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )
    checks.append(
        Check(
            "stop_reaches_stopped",
            "ok" if stopped else "fail",
            f"attempts={stopped_attempts}; {compact_payload(stopped_payload)}",
        )
    )

    start_result = run_operator(root, ["start"])
    start_payload = parse_operator_json(start_result)
    add_command_check(checks, name="start_command", result=start_result, payload=start_payload)
    started, started_payload, started_attempts = poll_status(
        run_status,
        lambda data: status_is_ready(data, allow_degraded=allow_degraded),
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )
    checks.append(
        Check(
            "start_reaches_console",
            "ok" if started else "fail",
            f"attempts={started_attempts}; {compact_payload(started_payload)}",
        )
    )

    restart_result = run_operator(root, ["restart"])
    restart_payload = parse_operator_json(restart_result)
    add_command_check(checks, name="restart_command", result=restart_result, payload=restart_payload)
    restarted, restarted_payload, restarted_attempts = poll_status(
        run_status,
        lambda data: status_is_ready(data, allow_degraded=allow_degraded),
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )
    checks.append(
        Check(
            "restart_reaches_console",
            "ok" if restarted else "fail",
            f"attempts={restarted_attempts}; {compact_payload(restarted_payload)}",
        )
    )

    update_result = run_operator(root, ["check-update"])
    update_payload = parse_operator_json(update_result)
    add_command_check(checks, name="check_update_status", result=update_result, payload=update_payload)

    if apply_update_requested:
        apply_result = run_operator(root, ["apply-update"])
        apply_payload = parse_operator_json(apply_result)
        add_command_check(checks, name="apply_update", result=apply_result, payload=apply_payload)
        update_ready, update_ready_payload, update_attempts = poll_status(
            run_status,
            lambda data: status_is_ready(data, allow_degraded=allow_degraded),
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )
        checks.append(
            Check(
                "apply_update_returns_to_console",
                "ok" if update_ready else "fail",
                f"attempts={update_attempts}; {compact_payload(update_ready_payload)}",
            )
        )
    else:
        checks.append(
            Check(
                "apply_update",
                "skip",
                "not requested; pass --apply-update when an update should actually be applied",
                required=False,
            )
        )

    if post_reboot:
        reboot_ready, reboot_payload, reboot_attempts = poll_status(
            run_status,
            lambda data: status_is_ready(data, allow_degraded=allow_degraded),
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )
        checks.append(
            Check(
                "post_reboot_console_reachable",
                "ok" if reboot_ready else "fail",
                f"attempts={reboot_attempts}; {compact_payload(reboot_payload)}",
            )
        )
    else:
        checks.append(
            Check(
                "post_reboot_console_reachable",
                "manual",
                "re-run this script with --post-reboot after rebooting the Mac",
                required=False,
            )
        )

    return checks


def build_report(
    *,
    root: Path,
    artifact: Path | None,
    version: str,
    checks: Sequence[Check],
    install_dir: Path | None,
) -> dict[str, object]:
    ready = all(check.status == "ok" if check.required else check.status in OPTIONAL_STATUSES for check in checks)
    return {
        "product": "Kaidera OS Operator",
        "channel": "macos",
        "version": version,
        "artifact": artifact.name if artifact else None,
        "artifact_path": str(artifact.resolve()) if artifact else None,
        "repo_root": str(root.resolve()),
        "install_dir": str(install_dir.resolve()) if install_dir else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
        },
        "ready": ready,
        "checks": [asdict(check) for check in checks],
    }


def render_text(report: dict[str, object]) -> str:
    lines = [
        "Kaidera OS Operator lifecycle proof",
        f"version: {report['version']}",
        f"artifact: {report.get('artifact')}",
        f"install_dir: {report.get('install_dir')}",
    ]
    for check in report["checks"]:  # type: ignore[index]
        requirement = "required" if check["required"] else "optional"
        lines.append(f"- {check['status'].upper()} {check['name']} [{requirement}]: {check['detail']}")
    lines.append(f"ready: {str(report['ready']).lower()}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="prove the Kaidera OS Mac operator lifecycle contract")
    parser.add_argument("artifact", nargs="?", type=Path, help="DMG path; defaults to current version in dist/macos")
    parser.add_argument("--output", type=Path, help="write JSON proof report")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for each lifecycle state")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between status polls")
    parser.add_argument("--install-dir", type=Path, help="copy the app here; defaults to a temporary Applications dir")
    parser.add_argument("--replace-installed-app", action="store_true", help="replace an existing copied app at install-dir")
    parser.add_argument("--skip-dmg-install", action="store_true", help="skip DMG mount/copy and only prove service lifecycle")
    parser.add_argument("--allow-degraded", action="store_true", help="accept degraded console status as reachable")
    parser.add_argument("--apply-update", action="store_true", help="actually call the update apply endpoint")
    parser.add_argument("--open-console", action="store_true", help="actually open the console in the default browser")
    parser.add_argument("--post-reboot", action="store_true", help="verify service reachability after a manual reboot")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    version = read_version(root)
    artifact = (args.artifact or default_artifact(root, version)).resolve()
    checks: list[Check] = []

    if platform.system() != "Darwin":
        checks.append(Check("macos_host", "fail", f"requires macOS; current host is {platform.system()}"))
    else:
        checks.append(Check("macos_host", "ok", f"Darwin {platform.machine()}"))

    temp_install: tempfile.TemporaryDirectory[str] | None = None
    install_dir: Path | None = None
    try:
        if args.skip_dmg_install:
            checks.append(Check("dmg_install", "skip", "not requested", required=False))
        else:
            if args.install_dir:
                install_dir = args.install_dir.expanduser().resolve()
                replace_existing = args.replace_installed_app
            else:
                temp_install = tempfile.TemporaryDirectory(prefix="kaidera-os-operator-install.")
                install_dir = Path(temp_install.name) / "Applications"
                replace_existing = True
            if platform.system() == "Darwin":
                checks.extend(
                    install_app_from_dmg(
                        artifact=artifact,
                        install_dir=install_dir,
                        replace_existing=replace_existing,
                    )
                )

        if platform.system() == "Darwin":
            checks.extend(
                run_lifecycle(
                    root=root,
                    timeout_seconds=args.timeout,
                    interval_seconds=args.interval,
                    allow_degraded=args.allow_degraded,
                    apply_update_requested=args.apply_update,
                    open_console_requested=args.open_console,
                    post_reboot=args.post_reboot,
                )
            )

        report = build_report(root=root, artifact=None if args.skip_dmg_install else artifact, version=version, checks=checks, install_dir=install_dir)
        output = args.output or default_output(root, version)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(render_text(report))
            print(f"report: {output}")
        return 0 if report["ready"] else 1
    finally:
        if temp_install is not None:
            temp_install.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
