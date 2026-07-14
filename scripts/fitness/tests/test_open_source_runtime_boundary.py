from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts" / "fitness" / "check-open-source-runtime-boundary.sh"


def run_gate(scan_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(GATE)],
        cwd=str(ROOT),
        env={**os.environ, "FITNESS_ROOT": str(scan_root)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_empty_public_runtime_passes(tmp_path: Path) -> None:
    assert run_gate(tmp_path).returncode == 0


def test_commercial_license_runtime_fails(tmp_path: Path) -> None:
    write(tmp_path / "local-cortex" / "console" / "app" / "license.py")
    completed = run_gate(tmp_path)
    assert completed.returncode == 1
    assert "license.py" in completed.stdout


def test_direct_provider_credential_in_provider_seam_fails(tmp_path: Path) -> None:
    write(
        tmp_path / "local-cortex" / "console" / "app" / "providers.py",
        'FIELD = "anthropic_api_key"\n',
    )
    completed = run_gate(tmp_path)
    assert completed.returncode == 1
    assert "direct/custom provider implementation" in completed.stdout
