"""Tests for the `check-import-linter` fitness gate (the SDK boundary gate).

The gate (`scripts/fitness/check-import-linter.sh`) runs import-linter's Forbidden
contract (`local-cortex/console/.importlinter`) to enforce the SDK's one law: the
domain core imports NOTHING outward — `app.domain.*` must not import httpx /
fastapi / starlette / subprocess / psycopg2 / asyncpg / app.adapters / app.main
(docs/sdk/README.md §"The layer rule"). A red contract blocks the push.

These tests shell out exactly like test_no_project_literals.py / test_pre_push.py
and drive the gate against a THROWAWAY console fixture via the gate's own env
overrides — so they NEVER mutate the real domain files:
  IMPORT_LINTER_CONSOLE_DIR  the dir the gate runs lint-imports from (a tmp copy)
  IMPORT_LINTER_BIN          the lint-imports binary (the real console .venv's)
  IMPORT_LINTER_CONFIG       the contract file (default `.importlinter`)

The fixture is a real copy of the console's `app/` package + the shipped
`.importlinter`, so import-linter graphs the SAME code the real gate sees. The
planted-violation test appends `import httpx` to the COPY's domain module only.

Assertions:
  1. the gate PASSES on the clean fixture (mirrors the real tree)            -> exit 0
  2. a planted `import httpx` in the COPY's app/domain/ breaks the contract  -> exit 1
  3. a missing contract file                                                 -> exit 1
  4. import-linter not installed (a fake non-exec bin, no PATH fallback)     -> exit 1
     — a boundary gate must FAIL LOUD, never silently skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Absolute bash so subprocess can launch the gate even when we blank PATH (case 4);
# fall back to "bash" (PATH-resolved) if it's not at the usual location.
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"

# tests[0] → fitness[1] → scripts[2] → repo-root[3].
ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts" / "fitness" / "check-import-linter.sh"
CONSOLE = ROOT / "local-cortex" / "console"
REAL_LINT_BIN = CONSOLE / ".venv" / "bin" / "lint-imports"
REAL_CONFIG = CONSOLE / ".importlinter"

# The whole suite is meaningless without the linter actually installed in the
# console .venv — skip (don't fail) if a checkout hasn't run the dev install yet.
pytestmark = pytest.mark.skipif(
    not REAL_LINT_BIN.exists(),
    reason=f"import-linter not installed in the console .venv ({REAL_LINT_BIN}); "
    "run: .venv/bin/python -m pip install import-linter",
)


def _make_console_fixture(tmp_path: Path) -> Path:
    """Build a throwaway console dir: a real copy of `app/` + the shipped
    `.importlinter`. import-linter graphs the copy, never the real tree."""
    fixture = tmp_path / "console"
    fixture.mkdir(parents=True)
    shutil.copytree(
        CONSOLE / "app",
        fixture / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(REAL_CONFIG, fixture / ".importlinter")
    return fixture


def _run_gate(
    console_dir: Path,
    *,
    lint_bin: Path | str = REAL_LINT_BIN,
    config: str = ".importlinter",
    drop_path: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the gate against `console_dir` using the gate's env overrides."""
    env = {
        **os.environ,
        "IMPORT_LINTER_CONSOLE_DIR": str(console_dir),
        "IMPORT_LINTER_BIN": str(lint_bin),
        "IMPORT_LINTER_CONFIG": config,
    }
    if drop_path:
        # Empty PATH so the gate's `command -v lint-imports` fallback can't find a
        # system install — isolates the "not installed" branch. We launch via the
        # ABSOLUTE BASH below, so blanking PATH doesn't stop the gate from starting.
        env["PATH"] = ""
    return subprocess.run(
        [BASH, str(GATE)],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


# ── 1. Clean fixture (mirrors the real tree) → gate passes ───────────────────
def test_clean_domain_passes(tmp_path: Path) -> None:
    fixture = _make_console_fixture(tmp_path)
    completed = _run_gate(fixture)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 0, f"clean domain should pass, got rc={completed.returncode}\n{out}"
    assert "✅ import-linter — domain core imports nothing outward" in out, out


# ── 2. Planted violation in the COPY → contract breaks → gate fails ──────────
def test_planted_httpx_import_fails(tmp_path: Path) -> None:
    fixture = _make_console_fixture(tmp_path)
    # Plant the leak in the THROWAWAY copy's domain only — never the real file.
    runstate = fixture / "app" / "domain" / "runstate.py"
    runstate.write_text(
        runstate.read_text(encoding="utf-8")
        + "\nimport httpx  # PLANTED VIOLATION (test fixture only)\n",
        encoding="utf-8",
    )
    completed = _run_gate(fixture)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 1, f"planted httpx import should fail the gate\n{out}"
    assert "❌ import-linter" in out, out
    # The offending import must be surfaced (the arrow import-linter reports).
    assert "httpx" in out, out
    assert "app.domain.runstate" in out, out


def test_planted_adapter_import_fails(tmp_path: Path) -> None:
    # The contract also forbids the OUTER app layers, not just 3rd-party I/O:
    # a domain → app.adapters import is the architecture inversion we guard against.
    fixture = _make_console_fixture(tmp_path)
    ports = fixture / "app" / "domain" / "ports.py"
    ports.write_text(
        ports.read_text(encoding="utf-8")
        + "\nimport app.adapters  # PLANTED VIOLATION (test fixture only)\n",
        encoding="utf-8",
    )
    completed = _run_gate(fixture)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 1, f"planted app.adapters import should fail the gate\n{out}"
    assert "app.adapters" in out, out


# ── 3. Missing contract file → gate fails (not a silent skip) ────────────────
def test_missing_contract_fails(tmp_path: Path) -> None:
    fixture = _make_console_fixture(tmp_path)
    (fixture / ".importlinter").unlink()
    completed = _run_gate(fixture)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 1, out
    assert "contract file not found" in out, out


# ── 4. import-linter not installed → gate fails LOUD (never silently skips) ──
def test_linter_not_installed_fails_loud(tmp_path: Path) -> None:
    fixture = _make_console_fixture(tmp_path)
    # Point at a non-existent binary AND drop PATH so the fallback can't find one.
    completed = _run_gate(
        fixture,
        lint_bin=tmp_path / "no_such_lint_imports",
        drop_path=True,
    )
    out = completed.stdout + completed.stderr
    assert completed.returncode != 0, f"a missing linter must FAIL (not skip)\n{out}"
    assert "not installed" in out, out
