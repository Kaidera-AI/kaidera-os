"""Explain generation driver tests (`app/explain_run.py`).

`explain_one(target, *, run_id, runner, runstate, cortex, project, agent, harness, model)`
is the Explain twin of `chat_run.chat_one`: it opens a run_state row (lease_owner='explain',
NO handoff), assembles the source context, streams the generation, VALIDATES the result is
a self-contained HTML document, BEST-EFFORT persists it to Cortex L5, and walks the run
status running → ok | error (stamping `{artifact_id}` in metadata on success).

These run against a FakeRunner (scripted `stream_chat`) + a fake RunStatePort + a fake
Cortex (records `post_artifact`) — NO live CLI, no DB, no network, no real file reads
(`assemble` is monkeypatched). They assert: the row opens with the explain lease, spans
stream the generated HTML, `post_artifact` is called with the right fields + the `explains`
edge, status ends ok on valid HTML with the artifact_id in metadata, status ends error on
empty / non-HTML / oversized output, and — critically — a RAISING `post_artifact` leaves
the run OK (best-effort L5; artifact_id None).
"""

from __future__ import annotations

import pytest

import app.explain_run as er
from tests.conftest import FakeRunner


_VALID_HTML = (
    "<!DOCTYPE html>\n<html><head><title>Add helper</title></head>"
    "<body><h1>add()</h1></body></html>"
)


class FakeExplainRunState:
    """Records the RunStatePort calls explain_one makes. `raising=True` forces every
    method to raise (the graceful-degrade path)."""

    def __init__(self, *, raising: bool = False) -> None:
        self.raising = raising
        self.started: list[dict] = []
        self.statuses: list[dict] = []
        self.spans: list[dict] = []

    async def start_run(self, *, run_id, project, agent, agent_display=None,
                        handoff_id=None, harness=None, model=None, pid=None,
                        lease_owner=None, session_id=None):
        if self.raising:
            raise RuntimeError("store down")
        self.started.append({
            "run_id": run_id, "project": project, "agent": agent,
            "handoff_id": handoff_id, "lease_owner": lease_owner,
            "harness": harness, "model": model,
        })
        return type("Rec", (), {"run_id": run_id})()

    async def set_status(self, run_id, status, *, error=None, metadata=None):
        if self.raising:
            raise RuntimeError("store down")
        self.statuses.append({
            "run_id": run_id, "status": status, "error": error, "metadata": metadata,
        })

    async def append_output(self, run_id, *, seq, kind, text):
        if self.raising:
            raise RuntimeError("store down")
        self.spans.append({"run_id": run_id, "seq": seq, "kind": kind, "text": text})


class FakeExplainCortex:
    """Records `post_artifact` calls; scriptable return id / raising."""

    def __init__(self, *, artifact_id="art-1", raising=False):
        self.agent = "ren"  # fitness:allow-literal test fixture (the console reader)
        self._artifact_id = artifact_id
        self.raising = raising
        self.posts: list[dict] = []

    async def post_artifact(self, project, agent, **kwargs):
        self.posts.append({"project": project, "agent": agent, **kwargs})
        if self.raising:
            raise RuntimeError("cortex down")
        return self._artifact_id


def _events_html(html=_VALID_HTML):
    return [
        {"type": "delta", "text": html[: len(html) // 2]},
        {"type": "delta", "text": html[len(html) // 2:]},
        {"type": "result", "text": "", "tokens_in": 5, "tokens_out": 9},
        {"type": "done"},
    ]


@pytest.fixture(autouse=True)
def _stub_assemble(monkeypatch):
    """Stub the host context assembler so no real file/CLI/git is touched — explain_one
    just gets canned context + provenance."""
    monkeypatch.setattr(
        er, "assemble", lambda target: ("# canned context\nsource here", {"kind": target.get("kind"), "ok": True})
    )


# ---------------------------------------------------------------------------
#  Happy path: open (explain lease, NO handoff) → spans → validate → L5 → ok.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_one_streams_persists_and_ends_ok():
    runner = FakeRunner(_events_html())
    store = FakeExplainRunState()
    cortex = FakeExplainCortex(artifact_id="art-42")

    result = await er.explain_one(
        {"kind": "file", "repo": "/repo", "path": "mod.py"},
        run_id="rid-1", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai", harness="claude-code", model="opus",
    )

    # 1. Row opened ONCE with the explain lease + NO handoff.
    assert len(store.started) == 1
    started = store.started[0]
    assert started["lease_owner"] == "explain"
    assert started["handoff_id"] is None
    assert started["project"] == "kaidera-os"

    # 2. The generated HTML streamed as output spans.
    out = "".join(s["text"] for s in store.spans if s["kind"] == "output")
    assert "<!DOCTYPE html>" in out and "add()" in out

    # 3. post_artifact called with the verified fields + the explains edge.
    assert len(cortex.posts) == 1
    post = cortex.posts[0]
    assert post["project"] == "kaidera-os"
    assert post["source_file"] == "explain/rid-1.html"
    assert post["modality"] == "html"
    assert len(post["content_hash"]) == 64  # sha256 hex
    assert post["caption"] == "Add helper"  # from <title>
    assert "Explain: file mod.py" in post["neighborhood_text"]
    assert post["edge_type"] == "explains"
    assert post["target_type"] == "file"
    assert post["target_ref"] == "mod.py"
    assert post["raw_content"].startswith("<!DOCTYPE html>")

    # 4. Status walked running → ok, with the artifact_id + the TARGET stamped in
    #    metadata (so the gallery enumerating run_state can label the run + jump to its
    #    artifact WITHOUT parsing the input span or re-reading the artifact).
    statuses = [(s["status"]) for s in store.statuses]
    assert statuses[0] == "running"
    assert statuses[-1] == "ok"
    ok = [s for s in store.statuses if s["status"] == "ok"][0]
    assert ok["metadata"] and ok["metadata"].get("artifact_id") == "art-42"
    assert ok["metadata"].get("capability") == "explain"
    assert ok["metadata"].get("target_kind") == "file"
    assert ok["metadata"].get("target_path") == "mod.py"
    assert ok["metadata"].get("caption") == "Add helper"  # the <title>, for the gallery

    # 5. The result reports ok + the artifact id + the caption.
    assert result.status == "ok"
    assert result.artifact_id == "art-42"
    assert result.caption == "Add helper"


@pytest.mark.asyncio
async def test_explain_one_writes_input_span_describing_target():
    runner = FakeRunner(_events_html())
    store = FakeExplainRunState()
    await er.explain_one(
        {"kind": "blast", "repo": "/r", "fn": "do_thing"},
        run_id="rid-in", runner=runner, runstate=store, cortex=None,
        project="kaidera-os", agent="kai",
    )
    inputs = [s for s in store.spans if s["kind"] == "input"]
    assert inputs and "Explain blast: do_thing" in inputs[0]["text"]


# ---------------------------------------------------------------------------
#  Validation: empty / non-HTML / oversized → error (no persist).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_one_empty_generation_is_error():
    runner = FakeRunner([{"type": "result", "text": "", "tokens_in": 1, "tokens_out": 0},
                         {"type": "done"}])
    store = FakeExplainRunState()
    cortex = FakeExplainCortex()
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-empty", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "error"
    assert store.statuses[-1]["status"] == "error"
    assert not cortex.posts, "an empty generation must NOT be persisted"


@pytest.mark.asyncio
async def test_explain_one_non_html_generation_is_error():
    runner = FakeRunner([{"type": "delta", "text": "Sure! Here is an explanation: ..."},
                         {"type": "done"}])
    store = FakeExplainRunState()
    cortex = FakeExplainCortex()
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-nonhtml", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "error"
    assert "not a self-contained HTML document" in (result.error or "")
    assert not cortex.posts


@pytest.mark.asyncio
async def test_explain_one_salvages_html_after_harness_progress_text():
    """Harness status chatter must not turn a complete HTML payload into an error."""
    runner = FakeRunner(
        [
            {"type": "delta", "text": "Using the HTML artifact skill for this output.\n"},
            {"type": "delta", "text": _VALID_HTML},
            {"type": "done"},
        ]
    )
    store = FakeExplainRunState()
    cortex = FakeExplainCortex()
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-preamble", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "ok"
    assert result.html == _VALID_HTML
    assert store.statuses[-1]["status"] == "ok"
    assert cortex.posts[0]["raw_content"] == _VALID_HTML


@pytest.mark.asyncio
async def test_explain_one_recovers_complete_html_after_terminal_harness_error():
    runner = FakeRunner(
        [
            {"type": "delta", "text": _VALID_HTML},
            {"type": "error", "message": "transport closed after response"},
        ]
    )
    store = FakeExplainRunState()
    cortex = FakeExplainCortex()

    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-recovered", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai",
    )

    assert result.status == "ok"
    assert result.error is None
    assert result.html == _VALID_HTML
    assert store.statuses[-1]["status"] == "ok"
    assert cortex.posts[0]["raw_content"] == _VALID_HTML


@pytest.mark.asyncio
async def test_explain_one_oversized_generation_is_error(monkeypatch):
    monkeypatch.setattr(er, "MAX_HTML_BYTES", 200)
    big = "<!DOCTYPE html><html>" + ("x" * 5000) + "</html>"
    runner = FakeRunner([{"type": "delta", "text": big}, {"type": "done"}])
    store = FakeExplainRunState()
    cortex = FakeExplainCortex()
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-big", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "error"
    assert "cap" in (result.error or "")
    assert not cortex.posts


@pytest.mark.asyncio
async def test_explain_one_harness_error_is_error_no_persist():
    runner = FakeRunner([{"type": "delta", "text": "<!DOCTYPE html><html>"},
                         {"type": "error", "message": "model unavailable"},
                         {"type": "done"}])
    store = FakeExplainRunState()
    cortex = FakeExplainCortex()
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-herr", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "error"
    assert "model unavailable" in (result.error or "")
    assert not cortex.posts


# ---------------------------------------------------------------------------
#  BEST-EFFORT L5: a failed post_artifact does NOT abort — run stays ok.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_one_degrades_when_post_artifact_raises():
    runner = FakeRunner(_events_html())
    store = FakeExplainRunState()
    cortex = FakeExplainCortex(raising=True)  # post_artifact raises
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-degrade", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai",
    )
    # The L5 write blew up, but the run is STILL ok (best-effort persistence).
    assert result.status == "ok", "a failed L5 write must NOT fail the run"
    assert result.artifact_id is None
    assert store.statuses[-1]["status"] == "ok"
    ok = store.statuses[-1]
    assert ok["metadata"].get("artifact_id") is None


@pytest.mark.asyncio
async def test_explain_one_ok_when_post_artifact_returns_none():
    """post_artifact returning None (a graceful Cortex degrade) keeps the run ok with
    artifact_id None — the document is the deliverable."""
    runner = FakeRunner(_events_html())
    store = FakeExplainRunState()
    cortex = FakeExplainCortex(artifact_id=None)
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-none-art", runner=runner, runstate=store, cortex=cortex,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "ok"
    assert result.artifact_id is None


@pytest.mark.asyncio
async def test_explain_one_no_cortex_is_ok_no_persist():
    """No cortex collaborator → no L5 write, run still ok (the document is produced)."""
    runner = FakeRunner(_events_html())
    store = FakeExplainRunState()
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-no-cx", runner=runner, runstate=store, cortex=None,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "ok"
    assert result.artifact_id is None


# ---------------------------------------------------------------------------
#  Graceful-degrade: a RAISING / None store never crashes the run.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_one_survives_raising_store():
    runner = FakeRunner(_events_html())
    store = FakeExplainRunState(raising=True)
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-rs", runner=runner, runstate=store, cortex=None,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_explain_one_survives_none_store():
    runner = FakeRunner(_events_html())
    result = await er.explain_one(
        {"kind": "file", "repo": "/r", "path": "x.py"},
        run_id="rid-ns", runner=runner, runstate=None, cortex=None,
        project="kaidera-os", agent="kai",
    )
    assert result.status == "ok"
    assert "<!DOCTYPE html>" in result.html


# ---------------------------------------------------------------------------
#  Validation helpers + the system prompt contract.
# ---------------------------------------------------------------------------

def test_validate_html_accepts_doctype_and_html_prefix():
    assert er._validate_html("<!DOCTYPE html><html></html>") is None
    assert er._validate_html("  <html></html>") is None
    assert er._validate_html("<!doctype HTML><HTML></HTML>") is None  # case-insensitive


def test_validate_html_rejects_empty_and_non_html():
    assert er._validate_html("") is not None
    assert er._validate_html("   ") is not None
    assert er._validate_html("Here is the explanation") is not None


def test_extract_html_document_trims_harness_preamble_and_suffix():
    raw = f"Working on it.\n{_VALID_HTML}\nGeneration complete."
    assert er.extract_html_document(raw) == _VALID_HTML


def test_extract_html_document_accepts_html_root_without_doctype():
    assert er.extract_html_document("Status\n<html><body>ok</body></html>") == (
        "<html><body>ok</body></html>"
    )


def test_extract_html_document_leaves_non_html_for_validator():
    raw = "Here is a prose-only explanation"
    assert er.extract_html_document(raw) == raw


def test_extract_title_pulls_caption():
    assert er._extract_title("<html><head><title>My Page</title></head></html>") == "My Page"
    assert er._extract_title("<html></html>") is None


def test_system_prompt_demands_self_contained_html():
    p = er.EXPLAIN_SYSTEM_PROMPT.lower()
    assert "<!doctype html>" in p
    assert "mermaid" in p
    assert "noscript" in p or "fallback" in p
    assert "title" in p


# ---------------------------------------------------------------------------
#  CLI argv parsing.
# ---------------------------------------------------------------------------

def test_main_usage_on_too_few_args(capsys):
    rc = er.main(["kai", "kaidera-os"])  # missing run_id + flags
    assert rc != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_parse_argv_builds_target():
    parsed = er._parse_argv(
        ["--kind", "file", "--repo", "/r", "--path", "a/b.py", "--harness", "pi", "--model", "gpt"]
    )
    assert parsed is not None
    target, harness, model = parsed
    assert target == {"kind": "file", "repo": "/r", "path": "a/b.py"}
    assert harness == "pi" and model == "gpt"


def test_parse_argv_maps_git_rev_and_fn():
    parsed = er._parse_argv(["--kind", "diff", "--repo", "/r", "--git-rev", "abc"])
    assert parsed is not None
    target, _, _ = parsed
    assert target["git_rev"] == "abc"
    parsed2 = er._parse_argv(["--kind", "blast", "--repo", "/r", "--fn", "do_it"])
    assert parsed2 is not None
    assert parsed2[0]["fn"] == "do_it"


def test_parse_argv_accepts_project_kind_without_path():
    parsed = er._parse_argv(["--kind", "project", "--repo", "/r"])
    assert parsed is not None
    target, harness, model = parsed
    assert target == {"kind": "project", "repo": "/r"}
    assert harness is None and model is None


def test_parse_argv_rejects_bad_kind():
    assert er._parse_argv(["--kind", "nope", "--repo", "/r"]) is None


def test_main_drives_explain_one(monkeypatch):
    seen: dict = {}

    async def _fake_explain_one(target, *, run_id, runner, runstate, cortex,
                                project, agent, harness=None, model=None):
        seen.update({"target": target, "run_id": run_id, "project": project,
                     "agent": agent, "harness": harness, "model": model})
        return er.ExplainResult(status="ok", html="<html></html>")

    monkeypatch.setattr(er, "explain_one", _fake_explain_one)
    monkeypatch.setattr(er, "_build_runner", lambda: object())
    monkeypatch.setattr(er, "_build_runstate", lambda: None)
    monkeypatch.setattr(er, "_build_cortex", lambda: None)

    rc = er.main(["kai", "kaidera-os", "rid-cli", "--kind", "file", "--repo", "/r", "--path", "m.py"])
    assert rc == 0
    assert seen["agent"] == "kai"
    assert seen["project"] == "kaidera-os"
    assert seen["run_id"] == "rid-cli"
    assert seen["target"] == {"kind": "file", "repo": "/r", "path": "m.py"}
