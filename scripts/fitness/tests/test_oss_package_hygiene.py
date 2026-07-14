from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts" / "fitness" / "check-oss-package-hygiene.sh"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_gate(scan_root: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "FITNESS_OSS_SCAN_ROOT": str(scan_root),
        **extra_env,
    }
    return subprocess.run(
        ["bash", str(GATE)],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_generated_workspace_state_fails(tmp_path: Path) -> None:
    _write(tmp_path / ".agents" / "config" / "workspace.json", "{}\n")

    completed = _run_gate(tmp_path)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "workspace.json" in completed.stdout + completed.stderr


def test_project_pack_payload_path_fails(tmp_path: Path) -> None:
    _write(tmp_path / "local-cortex" / "console" / "app" / "talib" / "worker.py", "x = 1\n")

    completed = _run_gate(tmp_path)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "talib/worker.py" in completed.stdout + completed.stderr


def test_secret_file_fails_but_env_example_passes(tmp_path: Path) -> None:
    _write(tmp_path / "local-cortex" / ".env.example", "OPENROUTER_API_KEY=\n")
    assert _run_gate(tmp_path).returncode == 0

    _write(tmp_path / "local-cortex" / ".env", "OPENROUTER_API_KEY=real\n")
    completed = _run_gate(tmp_path)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "local-cortex/.env" in completed.stdout + completed.stderr


def test_personal_path_fails(tmp_path: Path) -> None:
    _write(
        tmp_path / "install.sh",
        'ROOT="/Users/example/DevVault/sample-project"\n',
    )

    completed = _run_gate(tmp_path)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "personal host paths" in completed.stdout + completed.stderr


def test_private_key_payload_fails(tmp_path: Path) -> None:
    _write(
        tmp_path / "local-cortex" / "console" / "app" / "key_fixture.py",
        'KEY = "-----BEGIN PRIVATE KEY-----"\n',
    )

    completed = _run_gate(tmp_path)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "credential-looking payloads" in completed.stdout + completed.stderr


def test_generic_project_pack_example_passes(tmp_path: Path) -> None:
    _write(
        tmp_path / "redistributable" / "examples" / "project-pack-basic" / "project-pack.json",
        '{"kind": "kaidera-os.project-pack"}\n',
    )
    _write(
        tmp_path / "redistributable" / "examples" / "project-pack-basic" / "portal" / "index.html",
        "<main>Generic portal</main>\n",
    )

    completed = _run_gate(tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_deployment_forbidden_pattern_is_configurable(tmp_path: Path) -> None:
    _write(tmp_path / "redistributable" / "docs" / "guide.md", "Forbidden customer name\n")

    completed = _run_gate(
        tmp_path,
        KAIDERA_OS_OSS_FORBIDDEN_PATTERNS="Forbidden customer name",
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "deployment-forbidden patterns" in completed.stdout + completed.stderr
