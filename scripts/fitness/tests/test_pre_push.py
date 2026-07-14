"""Tests for the Kaidera OS fitness pre-push hook and installer.

The pre-push hook (`scripts/fitness/pre-push`) wires the fitness gates onto
`git push`: it runs the gate runner, blocks the push if a gate is red (unless
FITNESS_GATE_OVERRIDE=1), then CHAINS the existing protected-branch hook
(`local-cortex/git-hooks/pre-push`) and exits with ITS status — so
never-push-to-main keeps winning.

These tests shell out like the old loop tests did and use the hook's own
override env vars (FITNESS_RUN / PROTECTED_BRANCH_HOOK) to
inject STUB scripts, so we never run the real gates or touch the real
.git/hooks. The installer is exercised against throwaway tmp fixture repos.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PRE_PUSH = ROOT / "scripts" / "fitness" / "pre-push"
INSTALL = ROOT / "scripts" / "fitness" / "install-hooks.sh"

# git's pre-push contract feeds lines on stdin:
#   <local_ref> <local_sha> <remote_ref> <remote_sha>
PUSH_LINE = (
    "refs/heads/feat/bulletproof-dev-flow abc123 "
    "refs/heads/feat/bulletproof-dev-flow def456\n"
)
PUSH_ARGS = ["origin", "git@example.com:kaidera-os.git"]


def _make_stub(path: Path, exit_code: int, *, record_stdin: Path | None = None) -> None:
    """Write an executable stub script that exits `exit_code`.

    If `record_stdin` is given, the stub dumps everything it received on stdin
    to that file first — so a test can assert the saved push line was forwarded.
    """
    lines = ["#!/usr/bin/env bash", "set -euo pipefail"]
    if record_stdin is not None:
        # Quote the path so spaces in tmp paths are safe.
        lines.append(f'cat > "{record_stdin}"')
    lines.append(f"exit {exit_code}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _run_hook(
    tmp_path: Path,
    *,
    gate_exit: int,
    protected_exit: int,
    override: str | None = None,
    record_stdin: Path | None = None,
    stdin: str = PUSH_LINE,
) -> subprocess.CompletedProcess[str]:
    gate = tmp_path / "stub_run.sh"
    protected = tmp_path / "stub_protected.sh"
    _make_stub(gate, gate_exit)
    _make_stub(protected, protected_exit, record_stdin=record_stdin)

    env = {
        **os.environ,
        "FITNESS_RUN": str(gate),
        "PROTECTED_BRANCH_HOOK": str(protected),
    }
    if override is not None:
        env["FITNESS_GATE_OVERRIDE"] = override

    return subprocess.run(
        [str(PRE_PUSH), *PUSH_ARGS],
        cwd=str(ROOT),
        env=env,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


# ── 1. Red gate, no override → push blocked ──────────────────────────────────
def test_red_gate_no_override_blocks_push(tmp_path: Path) -> None:
    completed = _run_hook(tmp_path, gate_exit=1, protected_exit=0)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert "PUSH BLOCKED" in output
    assert "FITNESS_GATE_OVERRIDE=1" in output


# ── 2. Green gate + green protected hook → push allowed (exit 0) ──────────────
def test_green_gate_green_protected_allows_push(tmp_path: Path) -> None:
    completed = _run_hook(tmp_path, gate_exit=0, protected_exit=0)
    assert completed.returncode == 0, completed.stdout + completed.stderr


# ── 3. FITNESS_GATE_OVERRIDE=1 + red gate → proceeds past the gate ───────────
def test_override_lets_red_gate_through_to_chained_hook(tmp_path: Path) -> None:
    # Red gate but override set; chained protected hook is green -> overall 0,
    # proving the gate did NOT block and control reached the chained hook.
    completed = _run_hook(tmp_path, gate_exit=1, protected_exit=0, override="1")
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "PUSH BLOCKED" not in output
    assert "override" in output.lower()


def test_override_red_gate_still_obeys_protected_block(tmp_path: Path) -> None:
    # Override bypasses the GATE, but the chained protected hook must still win:
    # red protected hook -> overall non-zero even with the gate override.
    completed = _run_hook(tmp_path, gate_exit=1, protected_exit=1, override="1")
    assert completed.returncode != 0, completed.stdout + completed.stderr


# ── 4. Protected-branch delegation: gate green, hook decides ─────────────────
def test_green_gate_protected_hook_blocks_overall_blocks(tmp_path: Path) -> None:
    completed = _run_hook(tmp_path, gate_exit=0, protected_exit=1)
    assert completed.returncode != 0, completed.stdout + completed.stderr


def test_green_gate_protected_hook_allows_overall_allows(tmp_path: Path) -> None:
    completed = _run_hook(tmp_path, gate_exit=0, protected_exit=0)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_hook_exits_with_protected_hook_status(tmp_path: Path) -> None:
    # The hook must exit with the chained hook's EXACT status, not a generic 1.
    completed = _run_hook(tmp_path, gate_exit=0, protected_exit=42)
    assert completed.returncode == 42, completed.stdout + completed.stderr


# ── 5. Saved stdin is forwarded verbatim to the chained hook ─────────────────
def test_stdin_forwarded_to_chained_hook(tmp_path: Path) -> None:
    record = tmp_path / "received_stdin.txt"
    completed = _run_hook(
        tmp_path, gate_exit=0, protected_exit=0, record_stdin=record
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert record.exists(), "chained hook never received stdin"
    assert record.read_text(encoding="utf-8") == PUSH_LINE


# ── 6. install-hooks.sh: symlink / idempotent / uninstall / backup ───────────
def _init_tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    return repo


def _run_install(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INSTALL), *args],
        cwd=str(repo),
        env={**os.environ, "FITNESS_INSTALL_GIT_DIR": str(repo / ".git")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_install_creates_symlink(tmp_path: Path) -> None:
    repo = _init_tmp_repo(tmp_path)
    completed = _run_install(repo)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    hook = repo / ".git" / "hooks" / "pre-push"
    assert hook.is_symlink(), "pre-push was not installed as a symlink"
    assert hook.resolve() == PRE_PUSH.resolve()


def test_install_is_idempotent(tmp_path: Path) -> None:
    repo = _init_tmp_repo(tmp_path)
    first = _run_install(repo)
    assert first.returncode == 0, first.stdout + first.stderr
    second = _run_install(repo)
    assert second.returncode == 0, second.stdout + second.stderr
    hook = repo / ".git" / "hooks" / "pre-push"
    assert hook.is_symlink()
    assert hook.resolve() == PRE_PUSH.resolve()
    # No backups should be spawned by re-running on our own link.
    backups = list((repo / ".git" / "hooks").glob("pre-push.bak-*"))
    assert backups == [], f"idempotent re-run created backups: {backups}"


def test_install_backs_up_foreign_hook(tmp_path: Path) -> None:
    repo = _init_tmp_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho i-was-here\n", encoding="utf-8")
    hook.chmod(0o755)

    completed = _run_install(repo)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    # Our symlink is now in place...
    assert hook.is_symlink()
    assert hook.resolve() == PRE_PUSH.resolve()
    # ...and the foreign hook was preserved, not clobbered.
    backups = list((repo / ".git" / "hooks").glob("pre-push.bak-*"))
    assert len(backups) == 1, f"expected exactly one backup, got {backups}"
    assert "i-was-here" in backups[0].read_text(encoding="utf-8")


def test_uninstall_removes_symlink(tmp_path: Path) -> None:
    repo = _init_tmp_repo(tmp_path)
    _run_install(repo)
    hook = repo / ".git" / "hooks" / "pre-push"
    assert hook.is_symlink()

    completed = _run_install(repo, "--uninstall")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not hook.exists() and not hook.is_symlink(), "symlink not removed"


def test_uninstall_restores_backup(tmp_path: Path) -> None:
    repo = _init_tmp_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho original-hook\n", encoding="utf-8")
    hook.chmod(0o755)

    _run_install(repo)  # backs up the foreign hook, installs our symlink
    completed = _run_install(repo, "--uninstall")
    assert completed.returncode == 0, completed.stdout + completed.stderr

    assert not hook.is_symlink(), "our symlink should be gone after uninstall"
    assert hook.exists(), "original hook was not restored"
    assert "original-hook" in hook.read_text(encoding="utf-8")
    backups = list((repo / ".git" / "hooks").glob("pre-push.bak-*"))
    assert backups == [], f"backup not consumed on restore: {backups}"
