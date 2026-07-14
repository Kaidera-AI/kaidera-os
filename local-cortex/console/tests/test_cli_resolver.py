from __future__ import annotations

from app import cli_resolver


def test_resolver_selects_highest_version_and_preserves_path_order_on_tie(monkeypatch):
    monkeypatch.setattr(
        cli_resolver,
        "executable_candidates",
        lambda _program, *, env=None: ["/first/tool", "/old/tool", "/same/tool"],
    )
    versions = {
        "/first/tool": (2, 1, 206),
        "/old/tool": (2, 0, 76),
        "/same/tool": (2, 1, 206),
    }
    monkeypatch.setattr(cli_resolver, "executable_version", versions.get)

    assert cli_resolver.resolve_latest_executable("tool") == "/first/tool"
