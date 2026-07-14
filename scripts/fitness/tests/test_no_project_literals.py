"""Tests for the `check-no-project-literals` fitness gate (the SDK-realignment ratchet).

The gate (`scripts/fitness/check-no-project-literals.sh`) scans the harness code
paths for hardcoded project keys, hexes, agent-name string literals, and personal
paths — the literals that shadow the config-as-data path and break drop-in
portability (see docs/2026-06-05-sdk-realignment-audit-and-plan.md §4).

It is a RATCHET, not an absolute ban: a baseline file
(`scripts/fitness/.project-literals-baseline`) pins today's known per-file counts.
The gate FAILS only when a file EXCEEDS its baseline or a brand-new offending file
appears — so it is green at baseline and red on any NEW drift. Each realignment
phase shrinks the baseline; an empty baseline means fully enforcing.

These tests shell out exactly like test_pre_push.py and drive the gate against a
THROWAWAY fixture tree via two env overrides — so they NEVER touch the real
codebase or the real baseline:
  FITNESS_SCAN_ROOT     root the gate scans (instead of the repo)
  FITNESS_BASELINE_FILE baseline file the gate reads (instead of the shipped one)

Assertions:
  1. a planted NEW literal (no baseline)        -> gate exits 1
  2. that same literal recorded in the baseline -> gate exits 0
  3. a `# fitness:allow-literal` inline escape   -> the hit is suppressed (exit 0)
  4. an allowed path (`*.env.example`)           -> its literals are skipped (exit 0)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts" / "fitness" / "check-no-project-literals.sh"


def _run_gate(
    scan_root: Path,
    *,
    baseline: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the gate against `scan_root`, optionally with a custom baseline file.

    With no baseline given, point the gate at a guaranteed-absent path so it
    treats every hit as new (baseline = empty).
    """
    env = {
        **os.environ,
        "FITNESS_SCAN_ROOT": str(scan_root),
        "FITNESS_BASELINE_FILE": str(baseline) if baseline else str(scan_root / "__no_such_baseline__"),
        "FITNESS_PROJECT_KEYS": "sample-project",
        "FITNESS_PROJECT_HEXES": "5872|aba3",
        "FITNESS_AGENT_NAMES": "kai",
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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# A scanned-shape fixture file: lives under one of the gate's scan globs so it is
# actually inspected. `beat/<name>.sh` is in scope (`beat/**.{sh,py}`).
def _planted_beat_script(scan_root: Path, body: str, name: str = "run-thing.sh") -> Path:
    p = scan_root / "beat" / name
    _write(p, "#!/usr/bin/env bash\n" + body)
    return p


# ── 1. A NEW literal, no baseline → gate fails ───────────────────────────────
def test_new_literal_fails(tmp_path: Path) -> None:
    # A hardcoded project hex in a beat script — classic Theme-B drift.
    _planted_beat_script(tmp_path, 'PROJECT_HEX="5872"\n')
    completed = _run_gate(tmp_path)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 1, f"expected fail on new literal, got rc={completed.returncode}\n{out}"
    assert "no-project-literals" in out


def test_new_project_key_literal_fails(tmp_path: Path) -> None:
    # Hardcoded project KEY (case label) in a beat script.
    _planted_beat_script(tmp_path, 'case "$p" in\n  sample-project) echo hi ;;\nesac\n')
    completed = _run_gate(tmp_path)
    assert completed.returncode == 1, completed.stdout + completed.stderr


def test_new_agent_name_literal_fails(tmp_path: Path) -> None:
    # Hardcoded agent-name string literal — Theme C "hardwired to kai".
    _planted_beat_script(tmp_path, 'AGENT="kai"\n')
    completed = _run_gate(tmp_path)
    assert completed.returncode == 1, completed.stdout + completed.stderr


def test_new_personal_path_literal_fails(tmp_path: Path) -> None:
    # Personal absolute path — Theme D portability blocker.
    _planted_beat_script(tmp_path, 'CONSOLE="/Users/example/DevVault/sample-project"\n')
    completed = _run_gate(tmp_path)
    assert completed.returncode == 1, completed.stdout + completed.stderr


# ── 2. The same literal recorded in the baseline → gate passes ───────────────
def test_baselined_literal_passes(tmp_path: Path) -> None:
    f = _planted_beat_script(tmp_path, 'PROJECT_HEX="5872"\n')
    rel = f.relative_to(tmp_path)
    # Baseline format: "count<TAB>relpath", sorted. One hit on this file.
    baseline = tmp_path / "baseline"
    baseline.write_text(f"1\t{rel}\n", encoding="utf-8")
    completed = _run_gate(tmp_path, baseline=baseline)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 0, f"baseline should absorb the hit, got rc={completed.returncode}\n{out}"


def test_exceeding_baseline_fails(tmp_path: Path) -> None:
    # Two hits on a file whose baseline only allows one → EXCEEDS baseline → fail.
    f = _planted_beat_script(tmp_path, 'A="5872"\nB="aba3"\n')
    rel = f.relative_to(tmp_path)
    baseline = tmp_path / "baseline"
    baseline.write_text(f"1\t{rel}\n", encoding="utf-8")
    completed = _run_gate(tmp_path, baseline=baseline)
    assert completed.returncode == 1, completed.stdout + completed.stderr


def test_below_baseline_passes(tmp_path: Path) -> None:
    # One hit on a file whose baseline allows two → does NOT exceed → pass
    # (a phase removed a literal; the ratchet must not punish improvement).
    f = _planted_beat_script(tmp_path, 'A="5872"\n')
    rel = f.relative_to(tmp_path)
    baseline = tmp_path / "baseline"
    baseline.write_text(f"2\t{rel}\n", encoding="utf-8")
    completed = _run_gate(tmp_path, baseline=baseline)
    assert completed.returncode == 0, completed.stdout + completed.stderr


# ── 3. The `# fitness:allow-literal` inline escape suppresses a hit ──────────
def test_allow_literal_escape_suppresses(tmp_path: Path) -> None:
    # No baseline, but the offending line carries the inline escape → not counted.
    _planted_beat_script(
        tmp_path,
        'PROJECT_HEX="5872"  # fitness:allow-literal seed default for the wizard\n',
    )
    completed = _run_gate(tmp_path)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 0, f"allow-literal escape should suppress the hit\n{out}"


# ── 4. Allowed paths are skipped entirely ───────────────────────────────────
def test_env_example_path_skipped(tmp_path: Path) -> None:
    # A *.env.example file is config-as-data: its literals are NEVER flagged,
    # even with no baseline.
    p = tmp_path / "beat" / "sample.env.example"
    _write(p, 'CORTEX_PROJECT=sample-project\nPROJECT_HEX=5872\n')
    completed = _run_gate(tmp_path)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 0, f"*.env.example must be skipped\n{out}"


def test_tests_fixture_path_skipped(tmp_path: Path) -> None:
    # Anything under a tests/ dir is a fixture and must not be flagged.
    p = tmp_path / "beat" / "tests" / "test_fixture.py"
    _write(p, 'AGENT = "kai"\nHEX = "5872"\n')
    completed = _run_gate(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_out_of_scope_path_skipped(tmp_path: Path) -> None:
    # A file outside every scan glob (e.g. docs/) is not scanned at all.
    p = tmp_path / "docs" / "notes.py"
    _write(p, 'AGENT = "kai"\nHEX = "5872"\n')
    completed = _run_gate(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr


# ── 5. A totally clean tree passes ──────────────────────────────────────────
def test_clean_tree_passes(tmp_path: Path) -> None:
    _planted_beat_script(tmp_path, 'PROJECT="${CORTEX_PROJECT:?must be set}"\n')
    completed = _run_gate(tmp_path)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 0, out
    assert "✅" in out


# ── 6. A brand-new offending FILE (not in baseline) fails ───────────────────
def test_new_file_not_in_baseline_fails(tmp_path: Path) -> None:
    # Baseline knows file A; a NEW file B with a literal appears → fail.
    a = _planted_beat_script(tmp_path, 'A="5872"\n', name="known.sh")
    rel_a = a.relative_to(tmp_path)
    _planted_beat_script(tmp_path, 'B="aba3"\n', name="brand-new.sh")
    baseline = tmp_path / "baseline"
    baseline.write_text(f"1\t{rel_a}\n", encoding="utf-8")
    completed = _run_gate(tmp_path, baseline=baseline)
    assert completed.returncode == 1, completed.stdout + completed.stderr
