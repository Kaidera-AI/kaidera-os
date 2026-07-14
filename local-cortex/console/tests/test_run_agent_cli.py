"""Tests for run-agent CLI wiring: main() exit-code mapping + WorkerCortex adapter.

All tests monkeypatch real I/O (subprocess, HTTP) so no network/CLI is needed.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.run_agent as ra
from app.run_agent import RunResult, WorkerCortex


# ---------------------------------------------------------------------------
#  main() exit-code mapping tests
# ---------------------------------------------------------------------------

def test_main_completed_exit_0(monkeypatch):
    async def fake_run_one(*a, **k):
        return RunResult(status="completed")

    monkeypatch.setattr(ra, "run_one", fake_run_one)
    monkeypatch.setattr(
        ra, "_build_collaborators",
        lambda project: (object(), object(), (lambda ag, pr: ("pi", "m", "high")))
    )
    monkeypatch.setattr(ra, "_load_system_and_task", lambda n, h, p: ("sys", "task"))
    assert ra.main(["bob", "h-1", "kaidera-os"]) == 0


def test_main_failed_exit_1(monkeypatch):
    async def fake_run_one(*a, **k):
        return RunResult(status="failed", error="x")

    monkeypatch.setattr(ra, "run_one", fake_run_one)
    monkeypatch.setattr(
        ra, "_build_collaborators",
        lambda project: (object(), object(), (lambda ag, pr: ("pi", "m", "high")))
    )
    monkeypatch.setattr(ra, "_load_system_and_task", lambda n, h, p: ("sys", "task"))
    assert ra.main(["bob", "h-1", "kaidera-os"]) == 1


def test_main_skipped_exit_2(monkeypatch):
    async def fake_run_one(*a, **k):
        return RunResult(status="skipped", error="could not claim")

    monkeypatch.setattr(ra, "run_one", fake_run_one)
    monkeypatch.setattr(
        ra, "_build_collaborators",
        lambda project: (object(), object(), (lambda ag, pr: ("pi", "m", "high")))
    )
    monkeypatch.setattr(ra, "_load_system_and_task", lambda n, h, p: ("sys", "task"))
    assert ra.main(["bob", "h-1", "kaidera-os"]) == 2


def test_main_usage_exit_64():
    assert ra.main(["bob"]) == 64


def test_main_usage_exit_64_no_args():
    assert ra.main([]) == 64


# ---------------------------------------------------------------------------
#  _amain passes the right args and handles aclose gracefully
# ---------------------------------------------------------------------------

def test_amain_passes_args_to_run_one(monkeypatch):
    """_amain calls run_one with the built collaborators + loaded task/system."""
    captured = {}

    async def fake_run_one(name, handoff_id, project, *, cortex, runner, routing,
                           task_summary, system, runstate=None, run_id=None):
        captured["name"] = name
        captured["handoff_id"] = handoff_id
        captured["project"] = project
        captured["task_summary"] = task_summary
        captured["system"] = system
        captured["run_id"] = run_id
        return RunResult(status="completed")

    fake_cortex = MagicMock()
    fake_cortex._client = MagicMock()
    fake_cortex._client.aclose = AsyncMock()

    monkeypatch.setattr(ra, "run_one", fake_run_one)
    monkeypatch.setattr(
        ra, "_build_collaborators",
        lambda project: (fake_cortex, object(), (lambda ag, pr: ("pi", "m", "high")))
    )
    monkeypatch.setattr(ra, "_load_system_and_task", lambda n, h, p: ("my-system", "my-task"))

    exit_code = asyncio.run(ra._amain("ren", "h-42", "kaidera-os"))
    assert exit_code == 0
    assert captured["name"] == "ren"
    assert captured["handoff_id"] == "h-42"
    assert captured["project"] == "kaidera-os"
    assert captured["task_summary"] == "my-task"
    assert captured["system"] == "my-system"
    fake_cortex._client.aclose.assert_awaited_once()


def test_main_threads_run_id_argv4_to_amain(monkeypatch):
    """Milestone 1 (T5/T6): the optional argv[4] (run_id) the orchestrator passes is
    parsed and threaded into _amain → run_one (so the worker writes the SAME row)."""
    captured = {}

    async def fake_run_one(name, handoff_id, project, *, cortex, runner, routing,
                           task_summary, system, runstate=None, run_id=None):
        captured["run_id"] = run_id
        captured["runstate_is_set"] = runstate is not None
        return RunResult(status="completed")

    monkeypatch.setattr(ra, "run_one", fake_run_one)
    monkeypatch.setattr(
        ra, "_build_collaborators",
        lambda project: (object(), object(), (lambda ag, pr: ("pi", "m", "high")))
    )
    monkeypatch.setattr(ra, "_load_system_and_task", lambda n, h, p: ("sys", "task"))
    # A sentinel runstate so we can confirm _amain built + passed one for a run_id.
    monkeypatch.setattr(ra, "_build_runstate", lambda: object())

    assert ra.main(["bob", "h-1", "kaidera-os", "run-uuid-9"]) == 0
    assert captured["run_id"] == "run-uuid-9"
    assert captured["runstate_is_set"] is True


def test_main_no_run_id_argv_skips_runstate(monkeypatch):
    """BACK-COMPAT: a 3-arg invocation (no run_id) builds NO store and passes
    run_id=None — the worker runs exactly as before (no store writes)."""
    captured = {}

    async def fake_run_one(name, handoff_id, project, *, cortex, runner, routing,
                           task_summary, system, runstate=None, run_id=None):
        captured["run_id"] = run_id
        captured["runstate_is_set"] = runstate is not None
        return RunResult(status="completed")

    monkeypatch.setattr(ra, "run_one", fake_run_one)
    monkeypatch.setattr(
        ra, "_build_collaborators",
        lambda project: (object(), object(), (lambda ag, pr: ("pi", "m", "high")))
    )
    monkeypatch.setattr(ra, "_load_system_and_task", lambda n, h, p: ("sys", "task"))
    # If _build_runstate were called it would error — assert it is NOT for legacy argv.
    monkeypatch.setattr(ra, "_build_runstate", lambda: (_ for _ in ()).throw(AssertionError("must not build store for legacy argv")))

    assert ra.main(["bob", "h-1", "kaidera-os"]) == 0
    assert captured["run_id"] is None
    assert captured["runstate_is_set"] is False


def test_amain_aclose_exception_is_swallowed(monkeypatch):
    """_amain survives even when aclose() raises (try/except in _amain)."""
    async def fake_run_one(*a, **k):
        return RunResult(status="completed")

    fake_cortex = MagicMock()
    fake_cortex._client = MagicMock()
    fake_cortex._client.aclose = AsyncMock(side_effect=RuntimeError("closed already"))

    monkeypatch.setattr(ra, "run_one", fake_run_one)
    monkeypatch.setattr(
        ra, "_build_collaborators",
        lambda project: (fake_cortex, object(), (lambda ag, pr: ("pi", "m", "high")))
    )
    monkeypatch.setattr(ra, "_load_system_and_task", lambda n, h, p: ("sys", "task"))

    # Should not raise; aclose exception is swallowed
    exit_code = asyncio.run(ra._amain("ren", "h-99", "kaidera-os"))
    assert exit_code == 0


# ---------------------------------------------------------------------------
#  WorkerCortex adapter unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_cortex_claim_delegates_to_client():
    fake_client = MagicMock()
    fake_client.claim_handoff = AsyncMock(return_value=True)
    wc = WorkerCortex("kaidera-os", fake_client)
    result = await wc.claim_handoff("h-55", "ren")
    assert result is True
    fake_client.claim_handoff.assert_awaited_once_with("kaidera-os", "h-55", "ren")


@pytest.mark.asyncio
async def test_worker_cortex_complete_runs_subprocess():
    fake_client = MagicMock()
    wc = WorkerCortex("kaidera-os", fake_client)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        await wc.complete_handoff("h-77")
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == ["cortex-handoff", "--complete", "h-77"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 20
    assert kwargs["env"]["CORTEX_PROJECT"] == "kaidera-os"


@pytest.mark.asyncio
async def test_worker_cortex_log_decision():
    fake_client = MagicMock()
    wc = WorkerCortex("kaidera-os", fake_client)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        await wc.log("ren", "checkin", "ren STARTED h-77", "kaidera-os")
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == ["cortex-log", "ren", "decision", "ren STARTED h-77"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 20
    assert kwargs["env"]["CORTEX_PROJECT"] == "kaidera-os"


@pytest.mark.asyncio
async def test_worker_cortex_log_lesson():
    fake_client = MagicMock()
    wc = WorkerCortex("kaidera-os", fake_client)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        await wc.log("ren", "lesson", "always test first", "kaidera-os")
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == ["cortex-log", "ren", "lesson", "always test first"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 20
    assert kwargs["env"]["CORTEX_PROJECT"] == "kaidera-os"


# ---------------------------------------------------------------------------
#  AV-2 (b): the blocking cortex-* CLI runs OFF the event loop thread.
#  These are AWAITED from the streaming run_one loop (per-thought STEP logging
#  is the hot path), so a synchronous subprocess.run on the loop thread stalls
#  the harness stream pipe drain (the v1 freeze). asyncio.to_thread offloads it.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_cortex_complete_runs_off_event_loop():
    """complete_handoff's blocking subprocess executes on a WORKER thread, not the
    event-loop thread — proven by capturing the running thread inside the call."""
    fake_client = MagicMock()
    wc = WorkerCortex("kaidera-os", fake_client)
    loop_thread = threading.get_ident()
    ran_on: dict[str, int] = {}

    def _capture(*args, **kwargs):
        ran_on["tid"] = threading.get_ident()
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=_capture):
        await wc.complete_handoff("h-77")

    assert ran_on["tid"] != loop_thread  # offloaded, did not block the loop


@pytest.mark.asyncio
async def test_worker_cortex_log_runs_off_event_loop():
    """log's blocking subprocess (the per-thought STEP hot path) executes on a
    WORKER thread, not the event-loop thread."""
    fake_client = MagicMock()
    wc = WorkerCortex("kaidera-os", fake_client)
    loop_thread = threading.get_ident()
    ran_on: dict[str, int] = {}

    def _capture(*args, **kwargs):
        ran_on["tid"] = threading.get_ident()
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=_capture):
        await wc.log("ren", "checkin", "ren STEP h-77 #001 think hi", "kaidera-os")

    assert ran_on["tid"] != loop_thread  # offloaded, did not block the loop


# ---------------------------------------------------------------------------
#  _load_system_and_task — subprocess-mocked unit tests
# ---------------------------------------------------------------------------

def test_load_system_and_task_happy_path():
    """Parses cortex-boot JSON and cortex-handoff --show text output."""
    boot_json = '{"boot": "You are ren:5872, a kaidera-os agent.", "surface_version": "v1"}'
    handoff_text = "Handoff h-42: do the thing.\nContext: fix the widget."

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if cmd[0] == "cortex-boot":
            result.stdout = boot_json
            result.returncode = 0
        elif cmd[0] == "cortex-handoff":
            result.stdout = handoff_text
            result.returncode = 0
        else:
            result.stdout = ""
            result.returncode = 1
        return result

    with patch("subprocess.run", side_effect=fake_run):
        system, task = ra._load_system_and_task("ren", "h-42", "kaidera-os")

    assert "Runtime identity (authoritative)" in system
    assert "ren@kaidera-os" in system
    assert "cortex-boot ren" in system
    assert "cortex-handoff --mine ren" in system
    assert "ren:5872" in system
    assert "do the thing" in task


def test_load_system_and_task_scopes_cortex_cli_env_to_worker_workspace(tmp_path, monkeypatch):
    """The detached worker's own cortex-boot/show subprocesses must run with the
    project/workspace env, not an accidental console shell scope."""
    scripts = tmp_path / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setenv("KAIDERA_AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("CORTEX_API_URL", raising=False)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        result = MagicMock()
        result.stdout = '{"boot": "ctx"}' if cmd[0] == "cortex-boot" else "handoff text"
        result.returncode = 0
        return result

    with patch("subprocess.run", side_effect=fake_run):
        system, task = ra._load_system_and_task("kai", "h-42", "kaidera-os")

    assert "ctx" in system
    assert task == "handoff text"
    assert [c[0][0] for c in calls[:2]] == ["cortex-boot", "cortex-handoff"]
    assert calls[0][0] == ["cortex-boot", "kai"]
    for _cmd, kwargs in calls[:2]:
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["env"]["CORTEX_PROJECT"] == "kaidera-os"
        assert kwargs["env"]["CORTEX_API_URL"] == "http://127.0.0.1:8501"
        assert kwargs["env"]["KAIDERA_AGENT_WORKSPACE"] == str(tmp_path)
        assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(scripts)


def test_load_system_and_task_boot_fails_fallback():
    """Falls back to default system prompt when cortex-boot fails."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if cmd[0] == "cortex-boot":
            raise FileNotFoundError("cortex-boot not found")
        result.stdout = "handoff text"
        result.returncode = 0
        return result

    with patch("subprocess.run", side_effect=fake_run):
        system, task = ra._load_system_and_task("ren", "h-42", "kaidera-os")

    assert "ren" in system
    assert "kaidera-os" in system
    assert "handoff text" in task


def test_load_system_and_task_both_fail_fallback():
    """Falls back to defaults when both cortex-boot and cortex-handoff fail."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("not found")

    with patch("subprocess.run", side_effect=fake_run):
        system, task = ra._load_system_and_task("ren", "h-42", "kaidera-os")

    assert "ren" in system
    assert "(handoff h-42)" in task


def test_load_system_and_task_boot_invalid_json_fallback():
    """Legacy plain-text cortex-boot output remains usable as context."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if cmd[0] == "cortex-boot":
            result.stdout = "this is not json"
            result.returncode = 0
        else:
            result.stdout = "handoff summary text"
            result.returncode = 0
        return result

    with patch("subprocess.run", side_effect=fake_run):
        system, task = ra._load_system_and_task("ren", "h-42", "kaidera-os")

    assert "this is not json" in system
    assert "handoff summary text" in task


def test_parse_cortex_boot_preserves_plain_text_and_has_no_structured_skills():
    boot, skills = ra._parse_cortex_boot("You are marlow, cmo for marketing.")

    assert boot == "You are marlow, cmo for marketing."
    assert skills == []


def test_load_system_truncates_long_boot():
    """Boot text is truncated to 6000 chars."""
    long_boot = "X" * 10000
    boot_json = f'{{"boot": "{long_boot}"}}'

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if cmd[0] == "cortex-boot":
            result.stdout = boot_json
        else:
            result.stdout = "task"
        result.returncode = 0
        return result

    with patch("subprocess.run", side_effect=fake_run), patch.object(ra, "_agent_identity", return_value=None):
        system, _ = ra._load_system_and_task("ren", "h-1", "kaidera-os")

    assert system.count("X") == 6000
    assert "Runtime identity (authoritative)" in system


def test_load_system_uses_identity_file():
    """An agent with an identity file gets it as the system prompt (so Kai runs as the
    PM and Quill as the knowledge keeper, not a generic agent); the cortex-boot context
    is appended after it."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = '{"boot": "current ctx"}' if cmd[0] == "cortex-boot" else "task"
        result.returncode = 0
        return result

    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(ra, "_agent_identity", return_value="# I am the PM\nDog with the bone."):
        system, _ = ra._load_system_and_task("kai", "h-1", "kaidera-os")

    assert "Runtime identity (authoritative)" in system
    assert "kai@kaidera-os" in system
    assert "cortex-boot kai" in system
    assert "cortex-handoff --mine kai" in system
    assert "I am the PM" in system
    assert "current ctx" in system  # boot context appended after the identity


def test_agent_identity_strips_yaml_frontmatter(tmp_path, monkeypatch):
    """REGRESSION: an identity file's leading YAML frontmatter must be stripped. A
    system prompt that STARTS with "---" makes the harness CLIs treat it as a flags
    block, so the worker exits in ~5s with an empty reply — this silently broke every
    identity-bearing agent (Kai, Quill) on both pi and claude-code."""
    (tmp_path / "ZED_IDENTITY.md").write_text(
        "---\nname: zed\nrole: pm\n---\n# Zed — the PM\nDoes the work.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_IDENTITY_DIR", str(tmp_path))

    text = ra._agent_identity("zed")

    assert text is not None
    assert not text.startswith("---")          # frontmatter gone
    assert text.startswith("# Zed — the PM")    # body preserved, from its heading
    assert "name: zed" not in text             # metadata not leaked into the prompt
    assert "Does the work." in text


# ---------------------------------------------------------------------------
#  build_agent_persona — profile-persona materialization (redist dogfood GAP #4)
# ---------------------------------------------------------------------------

def test_build_agent_persona_falls_back_to_profile_persona(monkeypatch):
    """GAP #4 (headline): with NO hand-authored ``<NAME>_IDENTITY.md``, the persona comes
    from the active project's PROFILE (``project_profile.portal_persona``). This is what
    makes a dropped-in turnkey chat IN-PERSONA with no
    hand-authored identity file — the whole point of the materialization fix."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = '{"boot": "current ctx"}' if cmd[0] == "cortex-boot" else ""
        result.returncode = 0
        return result

    # No identity file resolves for this agent...
    monkeypatch.setattr(ra, "_agent_identity", lambda name: None)
    # ...but the project's profile DOES carry a portal persona.
    from app import project_profile as pp
    monkeypatch.setattr(
        pp, "portal_persona",
        lambda project: "# Example Portal Lead\nOwns the package-specific workflow.",
    )

    with patch("subprocess.run", side_effect=fake_run):
        persona = ra.build_agent_persona("wren", "marketing")

    assert "Example Portal Lead" in persona                            # from the PROFILE
    assert "current ctx" in persona                                    # boot context appended
    # NOT the generic project-aware one-liner the caller would build on an empty return.
    assert persona != ""


def test_build_agent_persona_identity_file_beats_profile_persona(monkeypatch):
    """Precedence: a hand-authored identity file STILL wins over the profile persona
    (identity file → profile persona → boot → "")."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = '{"boot": "ctx"}' if cmd[0] == "cortex-boot" else ""
        result.returncode = 0
        return result

    monkeypatch.setattr(ra, "_agent_identity", lambda name: "# Hand-authored identity")
    from app import project_profile as pp
    # If the profile persona were (wrongly) preferred, this string would appear.
    monkeypatch.setattr(pp, "portal_persona", lambda project: "PROFILE PERSONA SHOULD NOT WIN")

    with patch("subprocess.run", side_effect=fake_run):
        persona = ra.build_agent_persona("wren", "marketing")

    assert "Hand-authored identity" in persona
    assert "PROFILE PERSONA SHOULD NOT WIN" not in persona


def test_build_agent_persona_no_identity_no_profile_falls_back_to_boot(monkeypatch):
    """With neither an identity file NOR a profile persona, the boot line is used (so the
    caller does NOT have to build the project-aware one-liner)."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = '{"boot": "just the boot line"}' if cmd[0] == "cortex-boot" else ""
        result.returncode = 0
        return result

    monkeypatch.setattr(ra, "_agent_identity", lambda name: None)
    from app import project_profile as pp
    monkeypatch.setattr(pp, "portal_persona", lambda project: "")  # no profile persona

    with patch("subprocess.run", side_effect=fake_run):
        persona = ra.build_agent_persona("wren", "marketing")

    assert persona == "just the boot line"


def test_agent_identity_resolves_from_dot_agents_agents_dir(tmp_path, monkeypatch):
    """GAP #4b: with no ``AGENT_IDENTITY_DIR`` override, ``_agent_identity`` finds the
    SHIPPED identity files under ``<repo>/.agents/agents/`` (where KAI/QUILL/REN live),
    not only the legacy ``<repo>/agents/``. Faked here by pointing ``_identity_dirs`` at a
    ``.agents/agents`` dir under a tmp repo root."""
    shipped = tmp_path / ".agents" / "agents"
    shipped.mkdir(parents=True)
    (shipped / "WREN_IDENTITY.md").write_text(
        "# Wren — CMO\nOwns marketing.\n", encoding="utf-8"
    )
    legacy = tmp_path / "agents"  # legacy dir exists but does NOT carry the file
    legacy.mkdir()

    # No override → the search order is [.agents/agents, agents] (relative to the repo).
    monkeypatch.delenv("AGENT_IDENTITY_DIR", raising=False)
    monkeypatch.setattr(ra, "_identity_dirs", lambda: [str(shipped), str(legacy)])

    text = ra._agent_identity("wren")

    assert text is not None
    assert text.startswith("# Wren — CMO")
