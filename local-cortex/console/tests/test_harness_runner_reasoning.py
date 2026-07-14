"""Routed reasoning level → claude-code (`--effort`) + codex (`-c
model_reasoning_effort`) command construction (AV-4).

Before this, only the pi + kaidera lanes honoured the configured reasoning level;
claude-code (the DEFAULT lane) and codex dropped it. These tests pin that a
configured level provably reaches both command builders, and that OFF / default /
unrecognized values stay safe (no flag → the CLI's own default).

CLI flags VERIFIED live (2026-07-10):
  * Claude Code 2.1.206 — `--effort <level>`: low|medium|high|xhigh|max.
  * codex-cli 0.144.1 — `model/list` advertises exact per-model ladders;
    GPT-5.6 Sol reaches max/ultra while GPT-5.5 stops at xhigh.
"""

from __future__ import annotations

import pytest

from app import harness_runner as hr


@pytest.fixture(autouse=True)
def _no_command_override(monkeypatch):
    """Force the REAL claude argv path (no HARNESS_CMD_OVERRIDE mock)."""
    monkeypatch.delenv("HARNESS_CMD_OVERRIDE", raising=False)
    monkeypatch.setattr(hr, "_claude_program", lambda: hr.CLAUDE_PROGRAM)
    monkeypatch.setattr(hr, "_codex_program", lambda: hr.CODEX_PROGRAM)
    hr.set_command_override(None)
    yield
    hr.set_command_override(None)


# --------------------------------------------------------------------------- #
#  claude-code → --effort                                                      #
# --------------------------------------------------------------------------- #

def test_build_command_forwards_configured_effort():
    argv = hr._build_command("hello", "sonnet", reasoning="high")
    assert ["--effort", "high"] == argv[argv.index("--effort"): argv.index("--effort") + 2]
    # real lane (claude binary + base flags), not the mock override path.
    assert argv[0] == hr.CLAUDE_PROGRAM


def test_build_command_effort_max_passes_through():
    argv = hr._build_command("hello", "sonnet", reasoning="max")
    assert ["--effort", "max"] == argv[argv.index("--effort"): argv.index("--effort") + 2]


def test_build_command_effort_alias_normalized():
    # "med" is a stored alias for "medium" (app.reasoning.normalize_level).
    argv = hr._build_command("hello", "sonnet", reasoning="med")
    assert ["--effort", "medium"] == argv[argv.index("--effort"): argv.index("--effort") + 2]


def test_build_command_minimal_clamped_to_low():
    # claude's effort floor is "low"; our "minimal" tier clamps up.
    argv = hr._build_command("hello", "sonnet", reasoning="minimal")
    assert ["--effort", "low"] == argv[argv.index("--effort"): argv.index("--effort") + 2]


def test_build_command_uses_new_live_claude_effort_without_release_change(monkeypatch):
    from app import harness

    monkeypatch.setattr(
        harness,
        "harness_model_options",
        lambda _harness: [{
            "value": "claude-future",
            "label": "Claude Future",
            "reasoning_levels": ["minimal", "low", "medium"],
        }],
    )
    argv = hr._build_command("hello", "claude-future", reasoning="minimal")
    assert ["--effort", "minimal"] == argv[
        argv.index("--effort"): argv.index("--effort") + 2
    ]


@pytest.mark.parametrize("level", [None, "", "off", "none", "false"])
def test_build_command_off_or_default_omits_effort(level):
    argv = hr._build_command("hello", "sonnet", reasoning=level)
    assert "--effort" not in argv


def test_build_command_unrecognized_level_omits_effort():
    # An unknown token must not become an invalid --effort value (would 400 the CLI).
    argv = hr._build_command("hello", "sonnet", reasoning="ludicrous")
    assert "--effort" not in argv


def test_build_command_default_is_byte_for_byte_legacy():
    # reasoning=None → identical to the historical no-reasoning argv.
    assert hr._build_command("hello", "sonnet") == hr._build_command("hello", "sonnet", reasoning=None)
    assert "--effort" not in hr._build_command("hello", "sonnet")


# --------------------------------------------------------------------------- #
#  codex → -c model_reasoning_effort="<level>"                                 #
# --------------------------------------------------------------------------- #

def test_build_codex_command_forwards_configured_effort():
    argv = hr._build_codex_command("hello", "gpt-5.6-sol", reasoning="high")
    i = argv.index("-c")
    assert argv[i + 1] == 'model_reasoning_effort="high"'


def test_build_codex_command_uses_same_resolved_cli_as_catalog(monkeypatch):
    monkeypatch.setattr(hr, "_codex_program", lambda: "/latest/codex")

    assert hr._build_codex_command("hello", "gpt-5.6-sol")[0] == "/latest/codex"


def test_build_codex_command_omits_effort_the_model_does_not_support():
    argv = hr._build_codex_command("hello", "gpt-5.6-sol", reasoning="minimal")
    assert "-c" not in argv


def test_build_codex_command_forwards_new_max_and_ultra_levels():
    argv = hr._build_codex_command("hello", "gpt-5.6-sol", reasoning="max")
    i = argv.index("-c")
    assert argv[i + 1] == 'model_reasoning_effort="max"'

    argv = hr._build_codex_command("hello", "gpt-5.6-sol", reasoning="ultra")
    i = argv.index("-c")
    assert argv[i + 1] == 'model_reasoning_effort="ultra"'


def test_build_codex_command_rejects_max_for_xhigh_only_model():
    argv = hr._build_codex_command("hello", "gpt-5.5", reasoning="max")
    assert "-c" not in argv


def test_build_codex_command_omits_effort_for_unknown_model():
    argv = hr._build_codex_command("hello", "gpt-not-in-catalog", reasoning="ultra")
    assert "-c" not in argv


@pytest.mark.parametrize("level", [None, "", "off", "none", "false"])
def test_build_codex_command_off_or_default_omits_override(level):
    argv = hr._build_codex_command("hello", "gpt-5.6-sol", reasoning=level)
    assert "-c" not in argv
    assert not any("model_reasoning_effort" in tok for tok in argv)


def test_build_codex_command_default_is_byte_for_byte_legacy():
    assert hr._build_codex_command("hello", "gpt-5.6-sol") == hr._build_codex_command(
        "hello", "gpt-5.6-sol", reasoning=None
    )


@pytest.mark.parametrize(
    "run_context",
    ["chat", "autonomous", "approve", "approve-run", "manual", "interactive"],
)
def test_codex_work_contexts_use_workspace_write(run_context):
    argv = hr._build_codex_command(
        "do the work",
        "gpt-5.6-sol",
        run_context=run_context,
    )
    sandbox_index = argv.index("-s")
    assert argv[sandbox_index + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in argv
    assert "danger-full-access" not in argv


@pytest.mark.parametrize("run_context", [None, "", "explain", "inspection", "pm_beat"])
def test_codex_inspection_contexts_remain_read_only(run_context):
    argv = hr._build_codex_command(
        "inspect only",
        "gpt-5.6-sol",
        run_context=run_context,
    )
    sandbox_index = argv.index("-s")
    assert argv[sandbox_index + 1] == "read-only"
    assert "sandbox_workspace_write.network_access=true" not in argv


def test_composed_prompt_forbids_claiming_unperformed_actions():
    prompt = hr._compose_prompt("open the connector", "You are the project lead.")

    assert "never claim an external action" in prompt
    assert "If the required tool or connector is unavailable" in prompt


# --------------------------------------------------------------------------- #
#  Router forwards the routed level into both lanes (end-to-end)               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_router_forwards_reasoning_to_claude(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake(prompt, model=None, system=None, reasoning=None, **kw):
        captured["reasoning"] = reasoning
        yield {"type": "done"}

    monkeypatch.setattr(hr, "_stream_claude", _fake)
    async for _ in hr.stream_chat("hi", harness="claude-code", reasoning="high"):
        pass
    assert captured["reasoning"] == "high"


@pytest.mark.asyncio
async def test_router_forwards_reasoning_to_codex(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake(prompt, model=None, system=None, reasoning=None, **kw):
        captured["reasoning"] = reasoning
        yield {"type": "done"}

    monkeypatch.setattr(hr, "_stream_codex", _fake)
    async for _ in hr.stream_chat("hi", harness="codex", reasoning="high"):
        pass
    assert captured["reasoning"] == "high"
