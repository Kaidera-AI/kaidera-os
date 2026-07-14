#!/usr/bin/env python3
"""Verify an installed Kaidera OS Operator.app bundle without mutating services."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


DEFAULT_APP = Path("/Applications/Kaidera OS Operator.app")
EXPECTED_BUNDLE_ID = "ai.kaidera.kaidera-os.operator"
EXPECTED_ICON_FILE = "kaidera-os-operator"


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


def _ok(name: str, detail: str) -> Check:
    return Check(name, "ok", detail)


def _fail(name: str, detail: str) -> Check:
    return Check(name, "fail", detail)


def _read_plist(path: Path) -> dict[str, object]:
    with path.open("rb") as fh:
        data = plistlib.load(fh)
    return data if isinstance(data, dict) else {}


def verify_installed_app(app: Path = DEFAULT_APP) -> list[Check]:
    checks: list[Check] = []
    contents = app / "Contents"
    plist_path = contents / "Info.plist"
    binary = contents / "MacOS" / "Kaidera OS Operator"
    icon = contents / "Resources" / "kaidera-os-operator.icns"
    template_icon = (
        contents
        / "Resources"
        / "KaideraOSOperator_KaideraOSOperator.bundle"
        / "kaidera-icon-template.png"
    )

    checks.append(_ok("app_bundle", str(app)) if app.is_dir() else _fail("app_bundle", f"missing {app}"))
    checks.append(_ok("info_plist", str(plist_path)) if plist_path.is_file() else _fail("info_plist", "missing Info.plist"))
    checks.append(
        _ok("operator_binary", str(binary))
        if binary.is_file() and os.access(binary, os.X_OK)
        else _fail("operator_binary", f"missing executable {binary}")
    )
    checks.append(_ok("bundle_icon", str(icon)) if icon.is_file() else _fail("bundle_icon", f"missing {icon}"))
    checks.append(
        _ok("menu_bar_template_icon", str(template_icon))
        if template_icon.is_file()
        else _fail("menu_bar_template_icon", f"missing {template_icon}")
    )

    if plist_path.is_file():
        try:
            plist = _read_plist(plist_path)
            bundle_id = str(plist.get("CFBundleIdentifier") or "")
            checks.append(
                _ok("bundle_identifier", bundle_id)
                if bundle_id == EXPECTED_BUNDLE_ID
                else _fail("bundle_identifier", f"expected {EXPECTED_BUNDLE_ID}, got {bundle_id}")
            )
            icon_file = str(plist.get("CFBundleIconFile") or "")
            checks.append(
                _ok("bundle_icon_file", icon_file)
                if icon_file == EXPECTED_ICON_FILE
                else _fail("bundle_icon_file", f"expected {EXPECTED_ICON_FILE}, got {icon_file}")
            )
            lsui = bool(plist.get("LSUIElement"))
            checks.append(_ok("lsui_element", "true") if lsui else _fail("lsui_element", "expected true"))
        except Exception as exc:
            checks.append(_fail("info_plist_values", str(exc)))

    if app.is_dir() and shutil.which("codesign"):
        result = run_command(["codesign", "--verify", "--deep", "--strict", str(app)])
        detail = (result.stderr or result.stdout or "").strip() or "codesign verify passed"
        checks.append(_ok("codesign_verify", detail) if result.returncode == 0 else _fail("codesign_verify", detail))

    return checks


def build_report(app: Path, checks: Sequence[Check]) -> dict[str, object]:
    return {
        "product": "Kaidera OS Operator",
        "app_path": str(app),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready": all(check.status == "ok" for check in checks),
        "checks": [asdict(check) for check in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify installed Kaidera OS Operator.app")
    parser.add_argument("--app", default=str(DEFAULT_APP), help="path to Kaidera OS Operator.app")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args(argv)

    app = Path(args.app)
    checks = verify_installed_app(app)
    report = build_report(app, checks)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"{check.status.upper()} {check.name}: {check.detail}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
