"""T7 — the agent-detail LIVE-WORK-TRANSCRIPT reads from the RunState STORE.

Milestone 1 (RunState SSOT) read-cutover. T5+T6 made every autonomous dispatch
populate the durable store (`run_state` row + `run_span` spans + heartbeat +
terminal status on the worker-shared run_id). But the UI still read the in-memory
`TranscriptStore`, so the durable state was invisible. T7 repoints the crew/agent
read path to the store (`app.state.runstate`): `recent(project)` for the run rail,
`get_run(run_id)` / `by_handoff(hid)` for the selected run's body.

These tests pin the new async store-backed view builder
(`app.main._agent_runs_view_store`) against a FAKE store (a minimal RunStatePort
writer/reader holding RunRecord/RunSpan in memory — no DB needed, deterministic):

  * a seeded run + spans renders into the template context (rail row + selected
    body with seg-typed segments mapped 1:1 from RunSpan.kind),
  * pinning a run_id selects that run,
  * a missing run / down (None) store DEGRADES cleanly — empty state, never a
    raise, never a 500,
  * the span kinds (thinking/tool/output) pass straight through to the template
    segment kinds,
  * `_enrich_run_from_cortex` is GONE (the ~2s Cortex re-grep — its reason to
    exist ended when the worker writes spans to the store).
"""

from __future__ import annotations

import pytest

import app.main as main
from app.domain.runstate import RunRecord, RunSpan


class FakeStore:
    """In-memory RunStatePort reader: holds pre-built RunRecords keyed by run_id
    (with hydrated spans) and answers the read methods the view uses
    (recent / get_run / by_handoff). `fail=True` makes every read raise, to prove
    the view degrades on a half-up store the same as on a None store."""

    def __init__(self, records: list[RunRecord] | None = None, fail: bool = False):
        self._records = list(records or [])
        self._fail = fail

    async def recent(self, project=None, limit=20):
        if self._fail:
            raise RuntimeError("store down")
        key = (project or "").strip().lower() or None
        out = [r for r in self._records if key is None or r.project == key]
        # newest-first is the store's contract; the fixtures are already ordered.
        return out[:limit]

    async def get_run(self, run_id):
        if self._fail:
            raise RuntimeError("store down")
        for r in self._records:
            if r.run_id == run_id:
                return r
        return None

    async def by_handoff(self, handoff_id):
        if self._fail:
            raise RuntimeError("store down")
        cands = [r for r in self._records if r.handoff_id == handoff_id]
        return cands[0] if cands else None


def _run(
    run_id,
    *,
    project="kaidera-os",
    agent="bob",
    status="running",
    handoff_id="h-abc12345",
    spans=None,
    **kw,
):
    return RunRecord(
        run_id=run_id,
        project=project,
        agent=agent,
        agent_display=kw.get("agent_display", agent.capitalize()),
        handoff_id=handoff_id,
        harness=kw.get("harness", "claude-code"),
        model=kw.get("model", "opus"),
        status=status,
        error=kw.get("error"),
        started_at=kw.get("started_at", "2026-06-05T10:00:00+00:00"),
        updated_at=kw.get("updated_at", "2026-06-05T10:00:05+00:00"),
        ended_at=kw.get("ended_at"),
        spans=spans or [],
    )


# ── the store feeds the agent-detail transcript ──────────────────────────────


async def test_store_run_renders_into_agent_view():
    """A seeded run + spans from the STORE populates the agent-detail context:
    the rail lists the run, and the selected body carries the spans as seg-typed
    segments (thinking/tool/output map 1:1 to the template segment kinds)."""
    spans = [
        RunSpan(seq=1, kind="thinking", text="I should edit the doc first"),
        RunSpan(seq=2, kind="tool", text="Edit(doc.md)"),
        RunSpan(seq=3, kind="output", text="Updated the doc."),
    ]
    store = FakeStore([_run("run-1", spans=spans)])

    ctx = await main._agent_runs_view_store(store, "kaidera-os", "bob")

    # rail row present, scoped to this agent
    assert ctx["agent_run_count"] == 1
    assert ctx["agent_runs"][0]["run_id"] == "run-1"
    assert ctx["agent_runs"][0]["status_label"] == "running"
    assert ctx["agent_run_running"] == 1
    assert ctx["agent_run_active"] is True
    assert ctx["agent_run_no_orch"] is False

    # the selected body carries the spans, mapped to the template's segment shape
    sel = ctx["agent_run_selected"]
    assert sel is not None
    assert sel["run_id"] == "run-1"
    assert sel["agent_display"] == "Bob"
    assert sel["handoff_short"] == "h-abc123"  # handoff_id[:8]
    assert sel["status_label"] == "running"
    kinds = [s["kind"] for s in sel["segments"]]
    assert kinds == ["thinking", "tool", "output"]
    body = "".join(s["text"] for s in sel["segments"])
    assert "I should edit the doc first" in body
    assert "Edit(doc.md)" in body
    assert "Updated the doc." in body


async def test_store_filters_runs_to_this_agent():
    """The rail shows ONLY this agent's runs (another agent's run is excluded)."""
    store = FakeStore([
        _run("run-bob", agent="bob", handoff_id="h-bob00001"),
        _run("run-ren", agent="ren", handoff_id="h-ren00001"),
    ])
    ctx = await main._agent_runs_view_store(store, "kaidera-os", "bob")
    ids = [r["run_id"] for r in ctx["agent_runs"]]
    assert ids == ["run-bob"]


async def test_store_pins_run_id_when_given():
    """A pinned run_id selects that specific run (the self-poll carries it so a
    running run keeps filling in without jumping to the newest each tick)."""
    store = FakeStore([
        _run("run-new", status="running", handoff_id="h-new00001",
             spans=[RunSpan(seq=1, kind="output", text="newest")]),
        _run("run-old", status="ok", handoff_id="h-old00001",
             spans=[RunSpan(seq=1, kind="output", text="pinned older run")]),
    ])
    ctx = await main._agent_runs_view_store(store, "kaidera-os", "bob", run_id="run-old")
    assert ctx["agent_run_selected"]["run_id"] == "run-old"
    body = "".join(s["text"] for s in ctx["agent_run_selected"]["segments"])
    assert "pinned older run" in body


async def test_store_prefers_running_run_when_unpinned():
    """With no pin, a RUNNING run is selected over a finished one."""
    store = FakeStore([
        _run("run-done", status="ok", handoff_id="h-done0001",
             spans=[RunSpan(seq=1, kind="output", text="done body")]),
        _run("run-live", status="running", handoff_id="h-live0001",
             spans=[RunSpan(seq=1, kind="output", text="live body")]),
    ])
    ctx = await main._agent_runs_view_store(store, "kaidera-os", "bob")
    assert ctx["agent_run_selected"]["run_id"] == "run-live"


async def test_terminal_run_maps_status_label_and_ended():
    """A finished run maps status → friendly label; an errored run carries error."""
    store = FakeStore([
        _run("run-err", status="error", error="boom", handoff_id="h-err00001",
             ended_at="2026-06-05T10:00:09+00:00",
             spans=[RunSpan(seq=1, kind="output", text="partial")]),
    ])
    ctx = await main._agent_runs_view_store(store, "kaidera-os", "bob")
    sel = ctx["agent_run_selected"]
    assert sel["status"] == "error"
    assert sel["status_label"] == "errored"
    assert sel["error"] == "boom"
    assert sel["running"] is False
    assert ctx["agent_run_running"] == 0


# ── graceful degrade — never a 500 ───────────────────────────────────────────


async def test_missing_run_degrades_to_empty_state():
    """An agent with NO runs in the store → empty list + None selected (the clean
    empty state the template shows), never a raise."""
    store = FakeStore([])  # store is up but holds nothing
    ctx = await main._agent_runs_view_store(store, "kaidera-os", "bob")
    assert ctx["agent_runs"] == []
    assert ctx["agent_run_count"] == 0
    assert ctx["agent_run_selected"] is None
    assert ctx["agent_run_active"] is False


async def test_none_store_degrades_to_empty_state():
    """A None store (run-state SSOT failed to construct / app-DB down) degrades to
    the empty state — never a raise, never a 500."""
    ctx = await main._agent_runs_view_store(None, "kaidera-os", "bob")
    assert ctx["agent_runs"] == []
    assert ctx["agent_run_selected"] is None
    assert ctx["agent_run_active"] is False


async def test_failing_store_reads_degrade_to_empty_state():
    """A half-up store whose reads RAISE degrades to the empty state (each read is
    guarded) — the pane never blanks the whole page with a 500."""
    store = FakeStore([_run("run-x")], fail=True)
    ctx = await main._agent_runs_view_store(store, "kaidera-os", "bob")
    assert ctx["agent_runs"] == []
    assert ctx["agent_run_selected"] is None


async def test_blank_project_or_agent_degrades():
    """Missing project/agent (e.g. the inline-config POST path) → empty, no-poll."""
    store = FakeStore([_run("run-1")])
    assert (await main._agent_runs_view_store(store, "", "bob"))["agent_runs"] == []
    assert (await main._agent_runs_view_store(store, "kaidera-os", ""))["agent_runs"] == []


# ── _enrich_run_from_cortex is gone ──────────────────────────────────────────


def test_enrich_run_from_cortex_is_removed():
    """T7 deletes the ~2s Cortex re-grep enrichment — the worker now writes spans
    to the store, so the function (and its name) must no longer exist in main."""
    assert not hasattr(main, "_enrich_run_from_cortex")
