from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "install" / "verify-cortex-install-contract.py"
    spec = importlib.util.spec_from_file_location("verify_cortex_install_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_static_checks_pass_for_current_installer():
    mod = _load_module()
    root = Path(__file__).resolve().parents[3]

    checks = mod.static_checks(root)

    assert checks
    assert all(check.status == "ok" for check in checks), checks


def test_local_embed_worker_is_opt_in_for_default_installs():
    root = Path(__file__).resolve().parents[3]
    install = (root / "install.sh").read_text(encoding="utf-8")
    compose = (root / ".agents" / "docker-compose.cortex.yml").read_text(encoding="utf-8")

    default_services = re.search(r"CORTEX_SERVICES=\((.*?)\)", install, re.S)
    assert default_services is not None
    assert "cortex-embed-worker" not in default_services.group(1)
    assert "KAIDERA_CORTEX_LOCAL_EMBED" in install
    assert "CORTEX_SERVICES+=(cortex-embed-worker)" in install
    assert re.search(r"cortex-embed-worker:\n\s+profiles:\n\s+- local-embed", compose)


def test_static_checks_catch_destructive_volume_delete(tmp_path):
    mod = _load_module()
    (tmp_path / ".agents").mkdir()
    (tmp_path / "install.sh").write_text("docker compose down -v\n", encoding="utf-8")
    (tmp_path / ".agents" / "docker-compose.cortex.yml").write_text(
        "volumes:\n  cortex-pg-data:\n  harness-appdb-data:\n# runs ONCE on an empty data volume\n",
        encoding="utf-8",
    )

    checks = mod.static_checks(tmp_path)

    assert any(check.status == "fail" and "destructive" in check.detail for check in checks)


def test_compare_preserves_existing_secret_and_volume():
    mod = _load_module()
    before = {
        "existing_cortex_detected": True,
        "env_file": {
            "secrets": {
                "CORTEX_ADMIN_TOKEN": {"present": True, "sha256": "abc"},
                "KAIDERA_AUTH_SECRET": {"present": True, "sha256": "def"},
            }
        },
        "volumes": {
            "cortex-pg-data": [
                {"name": "kaidera_cortex-pg-data", "created_at": "t1", "mountpoint": "/v/cortex"}
            ],
            "harness-appdb-data": [
                {"name": "kaidera_harness-appdb-data", "created_at": "t2", "mountpoint": "/v/appdb"}
            ],
        },
        "containers": {"cortex-pg": {"exists": True}},
    }
    after = json.loads(json.dumps(before))

    checks = mod.compare_snapshots(before, after)

    assert checks
    assert all(check.status == "ok" for check in checks), checks


def test_compare_fails_when_existing_volume_recreated():
    mod = _load_module()
    before = {
        "existing_cortex_detected": True,
        "env_file": {
            "secrets": {
                "CORTEX_ADMIN_TOKEN": {"present": True, "sha256": "abc"},
                "KAIDERA_AUTH_SECRET": {"present": True, "sha256": "def"},
            }
        },
        "volumes": {
            "cortex-pg-data": [{"name": "kaidera_cortex-pg-data", "created_at": "old"}],
            "harness-appdb-data": [],
        },
        "containers": {},
    }
    after = {
        "existing_cortex_detected": True,
        "env_file": before["env_file"],
        "volumes": {
            "cortex-pg-data": [{"name": "kaidera_cortex-pg-data", "created_at": "new"}],
            "harness-appdb-data": [{"name": "kaidera_harness-appdb-data", "created_at": "fresh"}],
        },
        "containers": {},
    }

    checks = mod.compare_snapshots(before, after)

    assert any(check.name == "preserve_volume_cortex-pg-data" and check.status == "fail" for check in checks)


def test_snapshot_redacts_env_secret_values(tmp_path):
    mod = _load_module()
    env_file = tmp_path / "local-cortex" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("CORTEX_ADMIN_TOKEN=secret-token\nKAIDERA_AUTH_SECRET=auth-secret\n", encoding="utf-8")

    def fake_run(args):
        if args == ["docker", "info"]:
            return _result(returncode=1)
        if args and str(args[0]).endswith("curl"):
            return _result(returncode=7, stderr="connection refused")
        raise AssertionError(args)

    snapshot = mod.build_snapshot(tmp_path, run=fake_run, env={})

    secrets = snapshot["env_file"]["secrets"]
    assert secrets["CORTEX_ADMIN_TOKEN"]["present"] is True
    assert secrets["CORTEX_ADMIN_TOKEN"]["sha256"] != "secret-token"
    assert "secret-token" not in json.dumps(snapshot)
