import importlib.util
import json
import sys
from pathlib import Path


HELPER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "_cortex_beat_session_ingest.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("cortex_beat_session_ingest_test", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_pi_parser_preserves_chat_and_redacts_thinking(tmp_path):
    helper = load_helper()
    session_path = (
        tmp_path
        / "beat"
        / "state"
        / "pi-sessions"
        / "kaidera-os"
        / "bob"
        / "2026-06-13T00-00-00-000Z_11111111-1111-4111-8111-111111111111.jsonl"
    )
    write_jsonl(
        session_path,
        [
            {
                "type": "session",
                "id": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-06-13T00:00:00Z",
                "cwd": "/repo",
            },
            {
                "type": "model_change",
                "provider": "ollama-cloud",
                "modelId": "deepseek-v4-pro",
            },
            {
                "type": "message",
                "id": "u1",
                "timestamp": "2026-06-13T00:00:01Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "run the tick"}],
                },
            },
            {
                "type": "message",
                "id": "a1",
                "timestamp": "2026-06-13T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private reasoning"},
                        {"type": "text", "text": "I will run the check."},
                        {"type": "toolCall", "name": "bash", "arguments": {"command": "cortex-boot bob"}},
                        {"type": "toolResult", "toolCallId": "call-1", "output": "ok"},
                    ],
                },
            },
        ],
    )

    parsed = helper.parse_pi_session(
        session_path,
        project="kaidera-os",
        ingest_agent="kai",
        registered_writers={"kai", "ren"},
    )

    assert parsed is not None
    assert parsed.payload["session_uuid"] == "11111111-1111-4111-8111-111111111111"
    assert parsed.payload["agent"] == "kai"
    assert parsed.payload["metadata"]["original_agent"] == "bob"
    assert parsed.payload["metadata"]["skipped_thinking_parts"] == 1
    assert parsed.messages == 2
    combined = "\n".join(message["content"] for message in parsed.payload["messages"])
    assert "private reasoning" not in combined
    assert "run the tick" in combined
    assert "I will run the check." in combined
    assert "[tool call: bash]" in combined
    assert "[tool result: call-1]" in combined


def test_cli_requires_project_when_env_missing(monkeypatch, tmp_path, capsys):
    helper = load_helper()
    monkeypatch.delenv("CORTEX_PROJECT", raising=False)

    result = helper.main(["--root", str(tmp_path), "--limit", "1"])

    assert result == 2
    assert "--project or CORTEX_PROJECT is required" in capsys.readouterr().err


def test_harness_parser_uses_registered_writer_and_visible_work(tmp_path):
    helper = load_helper()
    directory = tmp_path / "beat" / "state" / "harness-sessions" / "kaidera-os" / "ren"
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(
        json.dumps(
            {
                "agent": "ren",
                "harness": "codex",
                "model": "gpt-5.5",
                "session_id": "22222222-2222-4222-8222-222222222222",
                "updated_at": "2026-06-13T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    write_jsonl(
        directory / "prompts.jsonl",
        [
            {
                "ts": "2026-06-13T00:00:01Z",
                "summary": "wake card",
                "card": "# Cortex Wake Card\n\nAgent: ren",
            }
        ],
    )
    (directory / "tool-summary.jsonl").write_text("", encoding="utf-8")
    (directory / "visible-thinking.md").write_text(
        "# Visible Thinking / Work History\n\n## Wake Card\n\nVisible work summary: checked queue.\n",
        encoding="utf-8",
    )

    parsed = helper.parse_harness_session(
        directory,
        project="kaidera-os",
        ingest_agent="kai",
        registered_writers={"kai", "ren"},
    )

    assert parsed is not None
    assert parsed.payload["agent"] == "ren"
    assert parsed.payload["metadata"]["original_agent"] == "ren"
    assert parsed.messages == 2
    combined = "\n".join(message["content"] for message in parsed.payload["messages"])
    assert "Cortex Wake Card" in combined
    assert "checked queue" in combined
