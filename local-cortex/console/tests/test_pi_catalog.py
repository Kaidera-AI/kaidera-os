from __future__ import annotations

from app.pi_catalog import parse_pi_list_models, parse_pi_thinking_levels


def test_parse_pi_thinking_levels_reads_installed_cli_choices():
    help_text = "--thinking <level>  Set thinking level: off, minimal, low, medium, high, xhigh"
    assert parse_pi_thinking_levels(help_text) == [
        "off", "minimal", "low", "medium", "high", "xhigh"
    ]


def test_parse_pi_list_models_groups_dynamic_providers_and_preserves_codex_values():
    text = """provider      model                                        context  max-out  thinking  images
fireworks     accounts/fireworks/models/kimi-k2p6          262K     262K     yes       yes
ollama-cloud  qwen3-coder:480b                             128K     8.2K     no        no
openai-codex  gpt-5.5                                      272K     128K     yes       yes
"""

    levels = ["off", "minimal", "low", "medium", "high", "xhigh"]
    groups = parse_pi_list_models(text, levels)

    by_provider = {g["provider"]: g for g in groups}
    assert set(by_provider) == {"fireworks", "ollama-cloud", "openai-codex"}
    assert by_provider["fireworks"]["rows"][0]["id"] == (
        "fireworks/accounts/fireworks/models/kimi-k2p6"
    )
    assert by_provider["fireworks"]["rows"][0]["image"] is True
    assert by_provider["fireworks"]["rows"][0]["reasoning_levels"] == levels
    assert by_provider["ollama-cloud"]["rows"][0]["id"] == "ollama-cloud/qwen3-coder:480b"
    assert by_provider["ollama-cloud"]["rows"][0]["reasoning_levels"] == []
    # OpenAI-Codex stays bare so existing PI overrides remain selected.
    assert by_provider["openai-codex"]["rows"][0]["id"] == "gpt-5.5"


def test_parse_pi_list_models_skips_non_table_noise():
    text = """
some warning
provider model context max-out thinking images
bad
openai-codex  gpt-5.4  272K  128K  yes  yes
"""

    groups = parse_pi_list_models(text)

    assert groups == [
        {
            "provider": "openai-codex",
            "label": "OpenAI Codex",
            "count": 1,
            "configured": True,
            "rows": [
                {
                    "id": "gpt-5.4",
                    "display_name": "gpt-5.4",
                    "type": "chat",
                    "context_window": "272K",
                    "max_output": "128K",
                    "reasoning": True,
                    "image": True,
                }
            ],
        }
    ]
