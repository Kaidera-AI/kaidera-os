from __future__ import annotations

import json

import pytest

from app import codex_catalog


RAW_MODELS = {
    "data": [
        {
            "id": "gpt-5.6-sol",
            "model": "gpt-5.6-sol",
            "displayName": "GPT-5.6-Sol",
            "description": "Frontier coding model",
            "hidden": False,
            "isDefault": True,
            "defaultReasoningEffort": "low",
            "supportedReasoningEfforts": [
                {"reasoningEffort": value, "description": value}
                for value in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
            "inputModalities": ["text", "image"],
        },
        {
            "id": "hidden-old",
            "model": "hidden-old",
            "displayName": "Hidden",
            "description": "",
            "hidden": True,
            "isDefault": False,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [],
        },
    ],
    "nextCursor": None,
}


def test_parse_codex_model_list_preserves_model_specific_efforts():
    rows = codex_catalog.parse_codex_model_list(RAW_MODELS)
    assert [row["value"] for row in rows] == ["gpt-5.6-sol"]
    assert rows[0]["is_default"] is True
    assert rows[0]["default_reasoning"] == "low"
    assert rows[0]["reasoning_levels"] == [
        "low", "medium", "high", "xhigh", "max", "ultra"
    ]
    assert rows[0]["input_modalities"] == ["text", "image"]


def test_parse_codex_model_list_accepts_normalized_host_bridge_rows():
    rows = codex_catalog.parse_codex_model_list(
        [{
            "value": "gpt-next",
            "label": "GPT Next",
            "reasoning_levels": ["low", "future"],
            "default_reasoning": "future",
            "input_modalities": ["text"],
        }]
    )
    assert rows == [{
        "value": "gpt-next",
        "label": "GPT Next",
        "reasoning_levels": ["low", "future"],
        "default_reasoning": "future",
        "is_default": False,
        "input_modalities": ["text"],
        "description": "",
    }]


@pytest.mark.asyncio
async def test_app_server_discovery_performs_handshake_then_model_list(monkeypatch):
    class Reader:
        def __init__(self):
            self.lines = [
                json.dumps({"id": 1, "result": {"userAgent": "test"}}).encode() + b"\n",
                json.dumps({"method": "status", "params": {}}).encode() + b"\n",
                json.dumps({"id": 2, "result": RAW_MODELS}).encode() + b"\n",
            ]

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

    class Writer:
        def __init__(self):
            self.writes: list[bytes] = []

        def write(self, value: bytes):
            self.writes.append(value)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    class Process:
        def __init__(self):
            self.stdin = Writer()
            self.stdout = Reader()
            self.returncode = None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    proc = Process()

    async def fake_spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(codex_catalog, "_resolve_codex_program", lambda _program: "/latest/codex")
    monkeypatch.setattr(codex_catalog.asyncio, "create_subprocess_exec", fake_spawn)
    rows = await codex_catalog._list_via_cli("codex", 1)

    assert [row["value"] for row in rows] == ["gpt-5.6-sol"]
    sent = [json.loads(line) for line in proc.stdin.writes]
    assert [frame["method"] for frame in sent] == [
        "initialize", "initialized", "model/list"
    ]
    assert sent[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert sent[-1]["params"] == {"limit": 100, "includeHidden": False}


def test_resolver_selects_newest_installed_codex(monkeypatch):
    monkeypatch.setattr(
        codex_catalog,
        "resolve_latest_executable",
        lambda _program, *, env: "/new/codex",
    )

    assert codex_catalog._resolve_codex_program("codex") == "/new/codex"


def test_harness_prefers_live_codex_catalog_and_default(monkeypatch):
    from app import harness

    live = codex_catalog.parse_codex_model_list(RAW_MODELS)
    monkeypatch.setattr(
        codex_catalog,
        "_catalog_cache",
        {"models": live, "expires": float("inf")},
    )
    assert harness.harness_model_options("codex") == live
    assert harness.harness_default_model("codex") == "gpt-5.6-sol"
