from __future__ import annotations

from app import claude_catalog


HELP = """
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --model <model>                       Provide an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a full
                                        name (e.g. 'claude-fable-5').
  --no-session-persistence              Disable persistence
Commands:
"""


def test_parse_claude_help_discovers_aliases_and_current_efforts():
    rows = claude_catalog.parse_claude_help(HELP)
    assert [row["value"] for row in rows] == [
        "fable", "opus", "sonnet", "claude-fable-5"
    ]
    assert all(
        row["reasoning_levels"] == ["low", "medium", "high", "xhigh", "max"]
        for row in rows
    )


def test_harness_merges_discovered_claude_rows_with_fallback_and_custom(monkeypatch):
    from app import harness, settings

    monkeypatch.setattr(
        claude_catalog,
        "_catalog_cache",
        {"models": claude_catalog.parse_claude_help(HELP), "expires": float("inf")},
    )
    monkeypatch.setattr(
        settings,
        "load_harness_model_overrides",
        lambda: {"claude-code": [{"value": "claude-future", "label": "Future"}]},
    )

    rows = harness.harness_model_options("claude-code")
    values = [row["value"] for row in rows]
    assert values[:4] == ["fable", "opus", "sonnet", "claude-fable-5"]
    assert "haiku" in values
    assert "claude-future" in values
    assert len(values) == len(set(values))
