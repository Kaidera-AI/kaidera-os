from __future__ import annotations

import importlib.util
import plistlib
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "macos" / "verify_installed_operator.py"
    spec = importlib.util.spec_from_file_location("verify_installed_operator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_app(root: Path) -> Path:
    app = root / "Kaidera OS Operator.app"
    contents = app / "Contents"
    binary = contents / "MacOS" / "Kaidera OS Operator"
    icon = contents / "Resources" / "kaidera-os-operator.icns"
    template = contents / "Resources" / "KaideraOSOperator_KaideraOSOperator.bundle" / "kaidera-icon-template.png"
    binary.parent.mkdir(parents=True)
    icon.parent.mkdir(parents=True)
    template.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    icon.write_bytes(b"icns")
    template.write_bytes(b"png")
    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(
            {
                "CFBundleIdentifier": "ai.kaidera.kaidera-os.operator",
                "CFBundleIconFile": "kaidera-os-operator",
                "LSUIElement": True,
            },
            fh,
        )
    return app


def test_verify_installed_operator_accepts_expected_bundle(tmp_path, monkeypatch):
    mod = _load_module()
    app = _write_app(tmp_path)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    checks = mod.verify_installed_app(app)
    report = mod.build_report(app, checks)

    assert checks
    assert all(check.status == "ok" for check in checks)
    assert report["ready"] is True


def test_verify_installed_operator_rejects_missing_icon(tmp_path, monkeypatch):
    mod = _load_module()
    app = _write_app(tmp_path)
    (app / "Contents" / "Resources" / "kaidera-os-operator.icns").unlink()
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    checks = mod.verify_installed_app(app)

    failed = {check.name for check in checks if check.status == "fail"}
    assert "bundle_icon" in failed
