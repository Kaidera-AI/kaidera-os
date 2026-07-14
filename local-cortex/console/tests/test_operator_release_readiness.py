from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "macos" / "operator_release_readiness.py"
    spec = importlib.util.spec_from_file_location("operator_release_readiness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _write_swift_operator_package(root: Path) -> None:
    package = root / "native" / "macos" / "KaideraOSOperator" / "Package.swift"
    package.parent.mkdir(parents=True)
    package.write_text("// swift-tools-version: 6.0\n", encoding="utf-8")


def test_soft_mode_allows_missing_public_release_credentials(tmp_path):
    mod = _load_module()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_swift_operator_package(tmp_path)

    checks = mod.run_checks(
        env={},
        root=tmp_path,
        system_name="Darwin",
        machine="arm64",
        which=lambda name: str(bin_dir / name),
        runner=lambda args: _result(),
    )

    assert not mod.has_failures(checks, strict=False)
    assert mod.has_failures(checks, strict=True)
    assert {check.name for check in checks if check.status == "warn"} == {
        "codesign_identity",
        "notary_profile",
    }


def test_codesign_identity_must_exist_when_configured(tmp_path):
    mod = _load_module()
    _write_swift_operator_package(tmp_path)

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}"

    checks = mod.run_checks(
        env={
            "KAIDERA_OS_CODESIGN_IDENTITY": "Developer ID Application: Kaidera AI",
            "KAIDERA_OS_NOTARY_PROFILE": "kaidera-os-notary",
        },
        root=tmp_path,
        system_name="Darwin",
        machine="arm64",
        which=which,
        runner=lambda args: _result(stdout='1) "Developer ID Application: Someone Else"'),
    )

    identity = next(check for check in checks if check.name == "codesign_identity")
    assert identity.status == "fail"
    assert mod.has_failures(checks, strict=False)


def test_public_release_credentials_pass_when_identity_and_profile_are_valid(tmp_path):
    mod = _load_module()
    _write_swift_operator_package(tmp_path)

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}"

    def runner(args):
        if args[:3] == ["security", "find-identity", "-v"]:
            return _result(stdout='1) "Developer ID Application: Kaidera AI"')
        if args[:3] == ["xcrun", "notarytool", "history"]:
            return _result(stdout="ok")
        raise AssertionError(args)

    checks = mod.run_checks(
        env={
            "KAIDERA_OS_CODESIGN_IDENTITY": "Developer ID Application: Kaidera AI",
            "KAIDERA_OS_NOTARY_PROFILE": "kaidera-os-notary",
        },
        root=tmp_path,
        system_name="Darwin",
        machine="arm64",
        which=which,
        runner=runner,
        verify_notary_profile=True,
    )

    assert all(check.status == "ok" for check in checks)
    assert not mod.has_failures(checks, strict=True)
