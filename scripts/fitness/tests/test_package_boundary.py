from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts" / "fitness" / "check-package-boundary.sh"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_gate(
    scan_root: Path, *, marker_file: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "FITNESS_SCAN_ROOT": str(scan_root),
        "FITNESS_BASELINE_FILE": str(scan_root / "__missing_baseline__"),
    }
    if marker_file is not None:
        env.pop("FITNESS_PACKAGE_MARKERS", None)
        env["FITNESS_PACKAGE_MARKERS_FILE"] = str(marker_file)
    else:
        env["FITNESS_PACKAGE_MARKERS"] = "samplepkg|customerpkg|devsuitepkg"
    return subprocess.run(
        ["bash", str(GATE)],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_package_marker_in_runtime_source_fails(tmp_path: Path) -> None:
    _write(
        tmp_path / "local-cortex" / "console" / "app" / "samplepkg_worker.py",
        "AGENT = 'samplepkg'\n",
    )
    completed = _run_gate(tmp_path)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "samplepkg_worker.py" in completed.stdout + completed.stderr


def test_local_marker_file_extends_boundary_policy(tmp_path: Path) -> None:
    markers = tmp_path / "markers.txt"
    markers.write_text("externalpkg\n", encoding="utf-8")
    _write(
        tmp_path / "local-cortex" / "console" / "app" / "externalpkg_worker.py",
        "AGENT = 'externalpkg'\n",
    )
    completed = _run_gate(tmp_path, marker_file=markers)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "externalpkg_worker.py" in completed.stdout + completed.stderr


def test_package_marker_in_test_fixture_is_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path / "local-cortex" / "console" / "spa" / "src" / "features" / "DashboardView.test.tsx",
        "const agent = 'samplepkg'\n",
    )
    completed = _run_gate(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_generic_package_contract_passes(tmp_path: Path) -> None:
    _write(
        tmp_path / "redistributable" / "examples" / "project-pack-basic" / "project-pack.json",
        '{"kind":"kaidera-os.project-pack"}\n',
    )
    completed = _run_gate(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
