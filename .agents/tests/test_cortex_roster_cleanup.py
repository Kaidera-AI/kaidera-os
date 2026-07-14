from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents" / "scripts" / "cortex-roster-cleanup"


def test_cortex_roster_cleanup_shell_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_cortex_roster_cleanup_delegates_to_safe_tools():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "cortex-maintain-agents" in text
    assert "cortex-harness-doctor" in text
    assert "rm -rf" not in text
    assert "DELETE FROM" not in text
