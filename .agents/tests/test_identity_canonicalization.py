"""Regression guard: deployment identity is generated, not checked in."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def is_tracked(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(relative)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def test_generated_workspace_config_is_not_checked_in():
    assert not is_tracked(ROOT / ".agents" / "config" / "workspace.json")
    assert not is_tracked(ROOT / ".agents" / "config" / "runtime.yaml")


def test_generated_identity_state_is_not_checked_in():
    generated_roots = [
        ROOT / ".agents" / "agents",
        ROOT / ".agents" / "bootstrap",
        ROOT / ".agents" / "rules",
        ROOT / ".agents" / "prompts",
    ]

    for root in generated_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:256]
            if "GENERATED FROM CORTEX" in head:
                assert not is_tracked(path), f"{path} is generated deployment state"


def test_root_boot_pointer_is_product_neutral():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "GENERATED FROM CORTEX" in text
    assert "has exactly two registered agents" not in text
    assert "kai" not in text.lower()
    assert "ren" not in text.lower()
