from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "macos" / "smoke_operator_dmg.py"
    spec = importlib.util.spec_from_file_location("smoke_operator_dmg", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_verify_mounted_contract_accepts_expected_operator_bundle(tmp_path, monkeypatch):
    mod = _load_module()
    mount = tmp_path / "mount"
    app = mount / "Kaidera OS Operator.app"
    contents = app / "Contents"
    binary = contents / "MacOS" / "Kaidera OS Operator"
    bundle_icon = contents / "Resources" / "kaidera-os-operator.icns"
    template_icon = contents / "Resources" / "KaideraOSOperator_KaideraOSOperator.bundle" / "kaidera-icon-template.png"
    contents.mkdir(parents=True)
    binary.parent.mkdir(parents=True)
    bundle_icon.parent.mkdir(parents=True)
    template_icon.parent.mkdir(parents=True)
    (contents / "Info.plist").write_text("<plist />", encoding="utf-8")
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    bundle_icon.write_bytes(b"icns")
    template_icon.write_bytes(b"png")
    (mount / "README.txt").write_text(
        "\n".join(
            [
                "Kaidera OS Operator v0.1.187",
                'Drag "Kaidera OS Operator.app" to Applications',
                "This DMG installs only the operator app",
                "Use it on a Mac where Kaidera OS/Cortex is already installed",
                "local-cortex/console/scripts/kaidera-os operator set-home /path/to/kaidera-os",
                "Build commit: abc123",
            ]
        ),
        encoding="utf-8",
    )
    os.symlink("/Applications", mount / "Applications")

    def fake_run(args):
        if args[0] == "/usr/libexec/PlistBuddy":
            key = args[2].split(":", 1)[1]
            values = {
                "CFBundleIdentifier": "ai.kaidera.kaidera-os.operator",
                "CFBundleName": "Kaidera OS Operator",
                "CFBundleIconFile": "kaidera-os-operator",
                "LSUIElement": "true",
            }
            return _result(stdout=values[key])
        if args[0] == "file":
            return _result(stdout="Mach-O 64-bit executable arm64")
        if args[0] == "codesign":
            return _result(stderr="valid on disk")
        raise AssertionError(args)

    monkeypatch.setattr(mod, "run_command", fake_run)

    checks = mod.verify_mounted_contract(
        mount_dir=mount,
        version="0.1.187",
        metadata={"commit": "abc123"},
    )

    assert checks
    assert all(check.status == "ok" for check in checks)


def test_build_report_marks_failed_checks_not_ready(tmp_path):
    mod = _load_module()

    report = mod.build_report(
        artifact=tmp_path / "operator.dmg",
        version="0.1.187",
        metadata={"commit": "abc123"},
        checks=[mod.Check("example", "fail", "broken")],
    )

    assert report["ready"] is False
