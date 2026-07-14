from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / ".agents/scripts/cortex-harness-doctor"


def _load_doctor_module():
    loader = importlib.machinery.SourceFileLoader("cortex_harness_doctor", str(DOCTOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


doctor = _load_doctor_module()


def _write_generated_project(root: Path, project: str = "dxb") -> None:
    (root / ".agents/config").mkdir(parents=True)
    (root / ".agents/agents").mkdir(parents=True)
    (root / ".agents/roles").mkdir(parents=True)
    (root / ".agents/rules").mkdir(parents=True)
    canonical_scripts = root.parent / "canonical-scripts"
    canonical_scripts.mkdir()
    (canonical_scripts / "cortex-boot").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / ".agents/scripts").symlink_to(canonical_scripts, target_is_directory=True)

    (root / ".agents/config/runtime.yaml").write_text(
        f"# GENERATED FROM CORTEX - DO NOT EDIT\nruntime: docker\nproject:\n  name: {project}\napi:\n  url: http://localhost:8501\npostgres:\n  port: 5499\n",
        encoding="utf-8",
    )
    (root / ".agents/config/workspace.json").write_text(
        json.dumps(
            {
                "_generated": {"note": "GENERATED FROM CORTEX - DO NOT EDIT"},
                "program": {"key": project, "root": str(root)},
                "projects": [
                    {
                        "key": project,
                        "repo_root": str(root),
                        "roots": [{"path": str(root), "kind": "primary"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / ".agents/agents/TALIB_IDENTITY.md").write_text(
        "# GENERATED FROM CORTEX - DO NOT EDIT\n\n---\nname: talib\n",
        encoding="utf-8",
    )
    (root / ".agents/roles/tech-lead.md").write_text(
        "# GENERATED FROM CORTEX - DO NOT EDIT\n",
        encoding="utf-8",
    )
    (root / ".agents/rules/dxb.md").write_text(
        "# GENERATED FROM CORTEX - DO NOT EDIT\n",
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(
        "# GENERATED FROM CORTEX - DO NOT EDIT\n",
        encoding="utf-8",
    )


def test_generated_project_mirror_passes(tmp_path):
    root = tmp_path / "dxb"
    _write_generated_project(root)

    report = doctor.scan_root(root, mode="project", expected_project="dxb")

    assert report.issues == []


def test_project_mirror_fails_on_copied_scripts_tree(tmp_path):
    root = tmp_path / "dxb"
    _write_generated_project(root)
    (root / ".agents/scripts").unlink()
    (root / ".agents/scripts").mkdir()
    (root / ".agents/scripts/cortex-boot").write_text("# stale copy\n", encoding="utf-8")

    report = doctor.scan_root(root, mode="project", expected_project="dxb")

    assert "copied_scripts_tree" in {issue.code for issue in report.issues}


def test_project_mirror_fails_on_redis_and_legacy_project_runtime(tmp_path):
    root = tmp_path / "dxb"
    _write_generated_project(root)
    (root / ".agents/config/runtime.yaml").write_text(
        "# GENERATED FROM CORTEX - DO NOT EDIT\nproject:\n  name: localdev\n  project_hex: deadbeef\nredis:\n  container_name: cortex-redis\n",
        encoding="utf-8",
    )

    report = doctor.scan_root(root, mode="project", expected_project="dxb")
    codes = {issue.code for issue in report.issues}

    assert "redis_yaml_block" in codes
    assert "redis_container" in codes
    assert "legacy_project_hex" in codes
    assert "legacy_runtime_project_key" in codes
    assert "runtime_project_mismatch" in codes


def test_project_mirror_fails_on_multi_project_workspace(tmp_path):
    root = tmp_path / "dxb"
    _write_generated_project(root)
    (root / ".agents/config/workspace.json").write_text(
        json.dumps(
            {
                "program": {"key": "localdev", "root": "/Users/example/localdev"},
                "projects": [{"key": "localdev"}, {"key": "dxb"}],
            }
        ),
        encoding="utf-8",
    )

    report = doctor.scan_root(root, mode="project", expected_project="dxb")
    codes = {issue.code for issue in report.issues}

    assert "workspace_not_generated" in codes
    assert "workspace_multi_project_mirror" in codes
    assert "legacy_workspace_project_key" in codes
    assert "workspace_project_mismatch" in codes
    assert "workspace_root_mismatch" in codes


def test_project_mirror_warns_on_manual_identity_file(tmp_path):
    root = tmp_path / "dxb"
    _write_generated_project(root)
    (root / ".agents/agents/TALIB_IDENTITY.md").write_text("name: talib\n", encoding="utf-8")

    report = doctor.scan_root(root, mode="project", expected_project="dxb")

    assert "manual_mirror_markdown" in {issue.code for issue in report.issues}


def test_advisory_exits_zero_with_issues(tmp_path, capsys):
    root = tmp_path / "dxb"
    _write_generated_project(root)
    (root / ".agents/scripts").unlink()
    (root / ".agents/scripts").mkdir()

    code = doctor.main(["--root", str(root), "--mode", "project", "--advisory"])
    captured = capsys.readouterr()

    assert code == 0
    assert "copied_scripts_tree" in captured.out


def test_json_output_reports_issue_codes(tmp_path, capsys):
    root = tmp_path / "dxb"
    _write_generated_project(root)
    (root / ".agents/config/runtime.yaml").write_text(
        "# GENERATED FROM CORTEX - DO NOT EDIT\nredis:\n  url: redis://example\n",
        encoding="utf-8",
    )

    code = doctor.main(["--root", str(root), "--mode", "project", "--json"])
    captured = capsys.readouterr()
    body = json.loads(captured.out)

    assert code == 1
    assert body["reports"][0]["issues"][0]["code"] == "redis_yaml_block"


def test_current_product_source_root_passes_auto_mode():
    # Live generated mirrors retain their registered key until the coordinated
    # Cortex migration. Source-root fitness must not force that migration.
    report = doctor.scan_root(ROOT, mode="auto", expected_project=None)

    assert report.mode == "product"
    assert report.issues == []
