from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BAKER = ROOT / "scripts" / "release" / "bake-public-edition.py"
SOURCE_EDITION = ROOT / "local-cortex" / "console" / "app" / "edition.py"


def _baked_value(path: Path) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_BAKED_EDITION"
        and isinstance(node.value, ast.Constant)
    ]
    assert len(values) == 1
    return values[0]


def test_source_checkout_remains_unrestricted():
    assert _baked_value(SOURCE_EDITION) is None


def test_release_baker_changes_only_the_staged_copy(tmp_path: Path):
    staged = tmp_path / "edition.py"
    staged.write_bytes(SOURCE_EDITION.read_bytes())

    subprocess.run([sys.executable, str(BAKER), str(staged)], check=True)

    assert _baked_value(staged) == "public"
    assert _baked_value(SOURCE_EDITION) is None


def test_release_baker_fails_closed_when_assignment_shape_drifts(tmp_path: Path):
    staged = tmp_path / "edition.py"
    staged.write_text("_BAKED_EDITION = None\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(BAKER), str(staged)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "expected one unbaked _BAKED_EDITION assignment" in result.stderr
