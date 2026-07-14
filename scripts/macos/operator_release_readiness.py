#!/usr/bin/env python3
"""Check readiness for a public Kaidera OS Operator macOS release."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


RunResult = subprocess.CompletedProcess[str]
Runner = Callable[[Sequence[str]], RunResult]
Which = Callable[[str], str | None]


def default_runner(args: Sequence[str]) -> RunResult:
    return subprocess.run(
        list(args),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _check_command(name: str, which: Which) -> Check:
    path = which(name)
    if path:
        return Check(f"{name}_tool", "ok", path)
    return Check(f"{name}_tool", "fail", f"{name} not found on PATH")


def _check_swift_package(root: Path) -> Check:
    package = root / "native" / "macos" / "KaideraOSOperator" / "Package.swift"
    if package.exists():
        return Check("swift_operator_package", "ok", str(package))
    return Check("swift_operator_package", "fail", f"missing Swift operator package: {package}")


def _check_codesign_identity(
    env: Mapping[str, str],
    *,
    which: Which,
    runner: Runner,
) -> Check:
    identity = env.get("KAIDERA_OS_CODESIGN_IDENTITY", "").strip()
    if not identity:
        return Check(
            "codesign_identity",
            "warn",
            "KAIDERA_OS_CODESIGN_IDENTITY is not set; public DMGs will be unsigned/ad-hoc signed",
        )
    if not which("security"):
        return Check("codesign_identity", "fail", "security tool not found; cannot inspect keychain identities")

    result = runner(["security", "find-identity", "-v", "-p", "codesigning"])
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode == 0 and identity in output:
        return Check("codesign_identity", "ok", identity)
    return Check("codesign_identity", "fail", f"Developer ID identity not found in keychain: {identity}")


def _check_notary_profile(
    env: Mapping[str, str],
    *,
    which: Which,
    runner: Runner,
    verify_notary_profile: bool,
) -> Check:
    profile = env.get("KAIDERA_OS_NOTARY_PROFILE", "").strip()
    if not profile:
        return Check(
            "notary_profile",
            "warn",
            "KAIDERA_OS_NOTARY_PROFILE is not set; public DMGs will not be notarized",
        )
    if not which("xcrun"):
        return Check("notary_profile", "fail", "xcrun not found; install Xcode command line tools")
    if not verify_notary_profile:
        return Check(
            "notary_profile",
            "ok",
            f"{profile} configured (not verified; pass --verify-notary-profile to test keychain access)",
        )

    result = runner(["xcrun", "notarytool", "history", "--keychain-profile", profile])
    if result.returncode == 0:
        return Check("notary_profile", "ok", f"{profile} verified with notarytool history")
    detail = (result.stderr or result.stdout or "notarytool history failed").strip()
    return Check("notary_profile", "fail", detail)


def run_checks(
    *,
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
    system_name: str | None = None,
    machine: str | None = None,
    which: Which = shutil.which,
    runner: Runner = default_runner,
    verify_notary_profile: bool = False,
) -> list[Check]:
    env = env or os.environ
    root = root or Path(__file__).resolve().parents[2]
    system_name = system_name or platform.system()
    machine = machine or platform.machine()

    checks: list[Check] = []
    if system_name == "Darwin":
        checks.append(Check("macos_host", "ok", f"{system_name} {machine}"))
    else:
        checks.append(Check("macos_host", "fail", f"DMG release builds require macOS; current host is {system_name}"))

    checks.extend(
        [
            _check_command("hdiutil", which),
            _check_command("swift", which),
            _check_swift_package(root),
            _check_command("codesign", which),
            _check_command("xcrun", which),
            _check_codesign_identity(env, which=which, runner=runner),
            _check_notary_profile(
                env,
                which=which,
                runner=runner,
                verify_notary_profile=verify_notary_profile,
            ),
        ]
    )
    return checks


def has_failures(checks: Sequence[Check], *, strict: bool) -> bool:
    bad_statuses = {"fail", "warn"} if strict else {"fail"}
    return any(check.status in bad_statuses for check in checks)


def render_text(checks: Sequence[Check], *, strict: bool) -> str:
    icons = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    lines = ["Kaidera OS Operator macOS release readiness"]
    for check in checks:
        lines.append(f"- {icons.get(check.status, check.status.upper())} {check.name}: {check.detail}")
    mode = "strict" if strict else "soft"
    lines.append(f"mode: {mode}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check Mac operator public release readiness")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--verify-notary-profile",
        action="store_true",
        help="call notarytool history to verify KAIDERA_OS_NOTARY_PROFILE keychain access",
    )
    args = parser.parse_args(argv)

    checks = run_checks(verify_notary_profile=args.verify_notary_profile)
    if args.json:
        payload = {
            "strict": args.strict,
            "ready": not has_failures(checks, strict=args.strict),
            "checks": [asdict(check) for check in checks],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(checks, strict=args.strict))
    return 1 if has_failures(checks, strict=args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
