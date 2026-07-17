"""PI harness runner behavior.

These tests keep the console chat path from regressing to a live row that has
output spans but never reaches a terminal run_state status because the pi CLI kept
its process open after producing text.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import harness_runner as hr
from app import pi_catalog


@pytest.fixture(autouse=True)
def _stable_pi_program(monkeypatch):
    monkeypatch.setattr(hr, "_pi_program", lambda: hr.PI_PROGRAM)


def test_build_pi_command_keeps_bare_models_on_openai_codex_provider():
    argv = hr._build_pi_command("hello", "gpt-5.5", thinking="high")

    assert argv[:3] == [hr.PI_PROGRAM, "--provider", hr.PI_PROVIDER]
    assert ["--model", "gpt-5.5"] == argv[argv.index("--model"): argv.index("--model") + 2]
    assert ["--thinking", "high"] == argv[argv.index("--thinking"): argv.index("--thinking") + 2]


def test_build_pi_command_provider_prefixed_model_does_not_force_openai_codex():
    model = "fireworks/accounts/fireworks/models/kimi-k2p6"

    argv = hr._build_pi_command("hello", model)

    assert "--provider" not in argv
    assert ["--model", model] == argv[argv.index("--model"): argv.index("--model") + 2]


def test_pi_effort_is_validated_against_selected_model_catalog(monkeypatch):
    monkeypatch.setattr(
        pi_catalog,
        "_pi_catalog_cache",
        {
            "groups": [{
                "provider": "openai-codex",
                "rows": [
                    {"id": "reasoner", "reasoning_levels": ["off", "low", "high"]},
                    {"id": "plain", "reasoning_levels": []},
                ],
            }],
            "expires": float("inf"),
        },
    )

    reasoner = hr._build_pi_command("hello", "reasoner", thinking="high")
    assert ["--thinking", "high"] == reasoner[
        reasoner.index("--thinking"): reasoner.index("--thinking") + 2
    ]
    assert "--thinking" not in hr._build_pi_command("hello", "reasoner", thinking="max")
    assert "--thinking" not in hr._build_pi_command("hello", "plain", thinking="high")
    # Explicit off remains valid even for a model that has no reasoning ladder.
    plain_off = hr._build_pi_command("hello", "plain", thinking="off")
    assert ["--thinking", "off"] == plain_off[
        plain_off.index("--thinking"): plain_off.index("--thinking") + 2
    ]


def test_pi_child_env_strips_provider_keys_for_every_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "metered-openai")
    monkeypatch.setenv("FIREWORKS_API_KEY", "fireworks-key")

    codex_env = hr._pi_child_env("gpt-5.5")
    provider_env = hr._pi_child_env("fireworks/accounts/fireworks/models/kimi-k2p6")

    assert "OPENAI_API_KEY" not in codex_env
    assert "FIREWORKS_API_KEY" not in provider_env
    assert "OPENAI_API_KEY" not in provider_env


def test_pi_idle_after_text_default_is_short_tail():
    """The PI post-text idle guard is a short debounce, not the old 30s latency tail."""
    assert hr.PI_IDLE_AFTER_TEXT_TIMEOUT_S <= 3.0


class FakePiProc:
    def __init__(self) -> None:
        self.stdout = object()
        self.stderr = FakeStderr()
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeStderr:
    async def read(self) -> bytes:
        return b""


class IdleAfterTextReader:
    timeouts: list[float] = []

    def __init__(self, stdout: object) -> None:
        self.calls = 0
        self.closed = False

    def start(self) -> None:
        return None

    async def readline(self, timeout: float) -> bytes:
        self.timeouts.append(timeout)
        self.calls += 1
        if self.calls == 1:
            return (
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "delta": "pong",
                        },
                    }
                ).encode("utf-8")
                + b"\n"
            )
        raise asyncio.TimeoutError

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pi_idle_after_text_emits_result_and_kills_child(monkeypatch):
    proc = FakePiProc()
    IdleAfterTextReader.timeouts = []

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(hr.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(hr, "_PipeLineReader", IdleAfterTextReader)
    monkeypatch.setattr(hr, "PI_IDLE_AFTER_TEXT_TIMEOUT_S", 0.25)

    events = [event async for event in hr._stream_pi("ping", model="gpt-5.5")]

    assert events[0] == {"type": "delta", "text": "pong"}
    assert events[1]["type"] == "result"
    assert events[-1] == {"type": "done"}
    assert proc.killed is True
    assert IdleAfterTextReader.timeouts == [hr.TURN_TIMEOUT_S, 0.25]
