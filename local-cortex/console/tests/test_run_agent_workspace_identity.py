"""Workspace-first identity resolution for project workers.

Ultrareview 2026-07-02 found every console-dispatched marketing worker booted with a
generic one-line prompt: `_identity_dirs` only searched the kaidera-os repo, and
`_agent_identity` only tried `<NAME>_IDENTITY.md` — while turnkeys keep personas as
`agents/<Title>/<name>.md`, `agents/<Title>/<name>-*.mission.md`, `docs/agents/<name>.md`.
"""
import os

import app.run_agent as r


def _mk(path, text="---\nname: x\n---\nPERSONA BODY"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_workspace_dirs_searched_first(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _mk(str(ws / ".agents" / "agents" / "SAUL_IDENTITY.md"), "---\nx: y\n---\nWS CANONICAL")
    monkeypatch.setenv("KAIDERA_AGENT_WORKSPACE", str(ws))
    monkeypatch.delenv("AGENT_IDENTITY_DIR", raising=False)
    dirs = r._identity_dirs()
    assert dirs[0] == str(ws / ".agents" / "agents")
    assert r._agent_identity("saul") == "WS CANONICAL"


def test_turnkey_subdir_layout_resolves(tmp_path, monkeypatch):
    # agents/Saul/saul.md — the marlow-turnkey layout
    ws = tmp_path / "ws"
    _mk(str(ws / "agents" / "Saul" / "saul.md"), "SAUL TURNKEY PERSONA")
    monkeypatch.setenv("KAIDERA_AGENT_WORKSPACE", str(ws))
    monkeypatch.delenv("AGENT_IDENTITY_DIR", raising=False)
    assert r._agent_identity("saul") == "SAUL TURNKEY PERSONA"


def test_mission_file_fallback_resolves(tmp_path, monkeypatch):
    # agents/Gem/gem-marketing-multimedia.mission.md — gem's ONLY persona file
    ws = tmp_path / "ws"
    _mk(str(ws / "agents" / "Gem" / "gem-marketing-multimedia.mission.md"), "GEM MISSION")
    monkeypatch.setenv("KAIDERA_AGENT_WORKSPACE", str(ws))
    monkeypatch.delenv("AGENT_IDENTITY_DIR", raising=False)
    assert r._agent_identity("gem") == "GEM MISSION"


def test_docs_agents_layout_resolves(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _mk(str(ws / "docs" / "agents" / "marlow.md"), "MARLOW DOC PERSONA")
    monkeypatch.setenv("KAIDERA_AGENT_WORKSPACE", str(ws))
    monkeypatch.delenv("AGENT_IDENTITY_DIR", raising=False)
    assert r._agent_identity("marlow") == "MARLOW DOC PERSONA"


def test_explicit_override_still_wins(tmp_path, monkeypatch):
    ov = tmp_path / "ov"
    ws = tmp_path / "ws"
    _mk(str(ov / "BOB_IDENTITY.md"), "OVERRIDE WINS")
    _mk(str(ws / ".agents" / "agents" / "BOB_IDENTITY.md"), "WS SHOULD LOSE")
    monkeypatch.setenv("AGENT_IDENTITY_DIR", str(ov))
    monkeypatch.setenv("KAIDERA_AGENT_WORKSPACE", str(ws))
    assert r._identity_dirs() == [str(ov)]
    assert r._agent_identity("bob") == "OVERRIDE WINS"


def test_no_workspace_falls_back_to_repo(monkeypatch):
    monkeypatch.delenv("KAIDERA_AGENT_WORKSPACE", raising=False)
    monkeypatch.delenv("AGENT_IDENTITY_DIR", raising=False)
    dirs = r._identity_dirs()
    assert len(dirs) == 2 and dirs[0].endswith(os.path.join(".agents", "agents"))


def test_frontmatter_still_stripped(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _mk(str(ws / "agents" / "Zed" / "zed.md"), "---\nname: zed\n---\nBODY ONLY")
    monkeypatch.setenv("KAIDERA_AGENT_WORKSPACE", str(ws))
    monkeypatch.delenv("AGENT_IDENTITY_DIR", raising=False)
    out = r._agent_identity("zed")
    assert out == "BODY ONLY" and not out.startswith("---")
