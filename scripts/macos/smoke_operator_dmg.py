#!/usr/bin/env python3
"""Smoke-test the Kaidera OS Operator DMG install contract.

This is not a substitute for a human clean-Mac reboot test. It verifies the DMG
itself: mountability, expected app bundle shape, Applications symlink, README
first-run contract, bundle metadata, code-signature validity, architecture, and
release metadata consistency.
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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


APP_NAME = "Kaidera OS Operator.app"
EXPECTED_TOP_LEVEL = {APP_NAME, "Applications", "README.txt"}
FORBIDDEN_TOP_LEVEL = {"install.sh", "update.sh", "local-cortex", ".agents", "docker-compose.yml"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def read_version(root: Path) -> str:
    text = (root / "local-cortex" / "console" / "app" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise RuntimeError("could not read Kaidera OS version")
    return match.group(1)


def load_metadata(dmg_path: Path) -> dict[str, object]:
    metadata_path = Path(f"{dmg_path}.metadata.json")
    if not metadata_path.exists():
        return {}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def check_tool(name: str) -> Check:
    path = shutil.which(name)
    if path:
        return Check(f"{name}_tool", "ok", path)
    return Check(f"{name}_tool", "fail", f"{name} not found on PATH")


def plist_value(plist: Path, key: str) -> str:
    result = run_command(["/usr/libexec/PlistBuddy", "-c", f"Print :{key}", str(plist)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"could not read {key}")
    return result.stdout.strip()


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
    if not device_or_mount:
        return
    run_command(["hdiutil", "detach", device_or_mount, "-quiet"])


def verify_mounted_contract(
    *,
    mount_dir: Path,
    version: str,
    metadata: dict[str, object],
) -> list[Check]:
    checks: list[Check] = []
    app = mount_dir / APP_NAME
    applications = mount_dir / "Applications"
    readme = mount_dir / "README.txt"
    plist = app / "Contents" / "Info.plist"
    binary = app / "Contents" / "MacOS" / "Kaidera OS Operator"
    bundle_icon = app / "Contents" / "Resources" / "kaidera-os-operator.icns"
    resource_bundle = app / "Contents" / "Resources" / "KaideraOSOperator_KaideraOSOperator.bundle"
    template_icon = resource_bundle / "kaidera-icon-template.png"

    def add(condition: bool, name: str, ok_detail: str, fail_detail: str) -> None:
        checks.append(Check(name, "ok" if condition else "fail", ok_detail if condition else fail_detail))

    add(app.is_dir(), "app_bundle", str(app), f"missing {app}")
    top_level = sorted(item.name for item in mount_dir.iterdir() if not item.name.startswith("."))
    unexpected = sorted(name for name in top_level if name not in EXPECTED_TOP_LEVEL)
    forbidden = sorted(name for name in top_level if name in FORBIDDEN_TOP_LEVEL)
    add(
        not unexpected,
        "app_only_top_level_payload",
        f"top-level payload is app-only: {top_level}",
        f"unexpected top-level payload entries: {unexpected}",
    )
    add(
        not forbidden,
        "app_only_no_runtime_payload",
        "no Kaidera OS/Cortex runtime payload is bundled",
        f"forbidden runtime payload entries found: {forbidden}",
    )
    add(applications.is_symlink(), "applications_symlink", str(applications), "missing Applications symlink")
    if applications.is_symlink():
        target = os.readlink(applications)
        add(target == "/Applications", "applications_symlink_target", target, f"expected /Applications, got {target}")

    add(readme.is_file(), "readme", str(readme), "missing README.txt")
    add(plist.is_file(), "info_plist", str(plist), "missing Info.plist")
    add(binary.is_file() and os.access(binary, os.X_OK), "operator_binary", str(binary), "missing executable")
    add(bundle_icon.is_file(), "operator_bundle_icon", str(bundle_icon), "missing app bundle icon")
    add(resource_bundle.is_dir(), "swift_resource_bundle", str(resource_bundle), "missing Swift resource bundle")
    add(template_icon.is_file(), "operator_template_icon", str(template_icon), "missing template icon resource")

    if plist.is_file():
        try:
            bundle_id = plist_value(plist, "CFBundleIdentifier")
            add(
                bundle_id == "ai.kaidera.kaidera-os.operator",
                "bundle_identifier",
                bundle_id,
                f"unexpected bundle id: {bundle_id}",
            )
            bundle_name = plist_value(plist, "CFBundleName")
            add(bundle_name == "Kaidera OS Operator", "bundle_name", bundle_name, f"unexpected bundle name: {bundle_name}")
            icon_file = plist_value(plist, "CFBundleIconFile")
            add(
                icon_file == "kaidera-os-operator",
                "bundle_icon_file",
                icon_file,
                f"expected kaidera-os-operator, got {icon_file}",
            )
            lsui = plist_value(plist, "LSUIElement").lower()
            add(lsui == "true", "lsui_element", lsui, f"expected true, got {lsui}")
        except RuntimeError as exc:
            checks.append(Check("info_plist_values", "fail", str(exc)))

    if binary.exists():
        file_result = run_command(["file", str(binary)])
        output = file_result.stdout.strip()
        arch_ok = file_result.returncode == 0 and "Mach-O" in output and ("arm64" in output or "universal" in output)
        add(arch_ok, "binary_architecture", output, output or "file command failed")

    if app.exists():
        codesign = run_command(["codesign", "--verify", "--deep", "--strict", str(app)])
        detail = (codesign.stderr or codesign.stdout or "codesign verify passed").strip()
        add(codesign.returncode == 0, "codesign_verify", detail, detail or "codesign verify failed")

    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        add(
            "installs only the operator app" in text,
            "readme_app_only_contract",
            "README documents app-only install",
            "README does not document app-only install",
        )
        add(
            "Kaidera OS/Cortex is already installed" in text,
            "readme_existing_cortex_contract",
            "README documents existing Kaidera OS/Cortex prerequisite",
            "README does not document existing Kaidera OS/Cortex prerequisite",
        )
        add(
            f"Kaidera OS Operator v{version}" in text,
            "readme_version",
            f"README names v{version}",
            f"README does not name v{version}",
        )
        add(
            'Drag "Kaidera OS Operator.app" to Applications' in text,
            "readme_drag_install",
            "README documents drag install",
            "README missing drag install instruction",
        )
        add(
            "operator set-home" in text,
            "readme_set_home_repair",
            "README documents set-home repair",
            "README missing set-home repair path",
        )
        commit = str(metadata.get("commit") or "")
        if commit:
            add(
                f"Build commit: {commit}" in text,
                "readme_build_commit",
                f"README names build commit {commit}",
                f"README missing build commit {commit}",
            )

    if app.is_dir():
        with tempfile.TemporaryDirectory(prefix="kaidera-os-app-only-install.") as tmp:
            install_root = Path(tmp) / "Applications"
            installed_app = install_root / APP_NAME
            install_root.mkdir()
            shutil.copytree(app, installed_app, symlinks=True)
            add(
                installed_app.is_dir(),
                "app_only_copy_install",
                str(installed_app),
                "copying the app bundle failed",
            )
            installed_binary = installed_app / "Contents" / "MacOS" / "Kaidera OS Operator"
            add(
                installed_binary.is_file() and os.access(installed_binary, os.X_OK),
                "app_only_copy_binary",
                str(installed_binary),
                "copied app is missing executable binary",
            )

    return checks


def build_report(
    *,
    artifact: Path,
    version: str,
    metadata: dict[str, object],
    checks: Sequence[Check],
) -> dict[str, object]:
    return {
        "product": "Kaidera OS Operator",
        "channel": "macos",
        "artifact": artifact.name,
        "artifact_path": str(artifact.resolve()),
        "version": version,
        "commit": metadata.get("commit"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
        },
        "ready": all(check.status == "ok" for check in checks),
        "checks": [asdict(check) for check in checks],
    }


def render_text(report: dict[str, object]) -> str:
    lines = [
        "Kaidera OS Operator DMG smoke",
        f"artifact: {report['artifact']}",
        f"version: {report['version']}",
        f"commit: {report.get('commit')}",
    ]
    for check in report["checks"]:  # type: ignore[index]
        status = check["status"].upper()
        lines.append(f"- {status} {check['name']}: {check['detail']}")
    lines.append(f"ready: {str(report['ready']).lower()}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the Kaidera OS Operator DMG")
    parser.add_argument("artifact", nargs="?", type=Path, help="DMG path; defaults to current version in dist/macos")
    parser.add_argument("--output", type=Path, help="write JSON smoke report")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    version = read_version(root)
    artifact = args.artifact or root / "dist" / "macos" / f"kaidera-os-operator-v{version}.dmg"
    artifact = artifact.resolve()
    metadata = load_metadata(artifact)
    checks: list[Check] = []

    if platform.system() != "Darwin":
        checks.append(Check("macos_host", "fail", f"DMG smoke requires macOS; current host is {platform.system()}"))
    else:
        checks.append(Check("macos_host", "ok", f"Darwin {platform.machine()}"))

    checks.extend([check_tool("hdiutil"), check_tool("codesign"), check_tool("file")])
    if Path("/usr/libexec/PlistBuddy").exists():
        checks.append(Check("plistbuddy_tool", "ok", "/usr/libexec/PlistBuddy"))
    else:
        checks.append(Check("plistbuddy_tool", "fail", "missing /usr/libexec/PlistBuddy"))

    if not artifact.exists():
        checks.append(Check("artifact_exists", "fail", f"missing {artifact}"))
    else:
        checks.append(Check("artifact_exists", "ok", str(artifact)))
        if metadata:
            expected = {
                "version": version,
                "artifact": artifact.name,
                "size_bytes": artifact.stat().st_size,
            }
            for key, value in expected.items():
                checks.append(
                    Check(
                        f"metadata_{key}",
                        "ok" if metadata.get(key) == value else "fail",
                        f"{metadata.get(key)!r}",
                    )
                )
            signing = metadata.get("signing")
            signing_ok = (
                isinstance(signing, dict)
                and signing.get("kind") in {"ad_hoc", "developer_id"}
                and isinstance(signing.get("notarized"), bool)
                and isinstance(signing.get("stapled"), bool)
                and isinstance(metadata.get("public_release_ready"), bool)
            )
            checks.append(
                Check(
                    "metadata_signing_state",
                    "ok" if signing_ok else "fail",
                    repr(signing),
                )
            )
        else:
            checks.append(Check("metadata_sidecar", "fail", f"missing {artifact}.metadata.json"))

    mounted = False
    device = ""
    with tempfile.TemporaryDirectory(prefix="kaidera-os-dmg-smoke.") as tmp:
        mount_dir = Path(tmp) / "mount"
        mount_dir.mkdir()
        if all(check.status == "ok" for check in checks):
            try:
                device = attach_dmg(artifact, mount_dir)
                mounted = True
                checks.append(Check("dmg_mount", "ok", str(mount_dir)))
                checks.extend(verify_mounted_contract(mount_dir=mount_dir, version=version, metadata=metadata))
            except RuntimeError as exc:
                checks.append(Check("dmg_mount", "fail", str(exc)))
            finally:
                if mounted:
                    detach_dmg(device)
                    checks.append(Check("dmg_detach", "ok", device or str(mount_dir)))

    report = build_report(artifact=artifact, version=version, metadata=metadata, checks=checks)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
