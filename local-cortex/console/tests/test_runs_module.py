"""Track A step 5 — the FIFTH and FINAL feature-module carve: `app/runs/`.

The runs READ feature (the run-state read side — the agent-detail LIVE-WORK
TRANSCRIPT rail + selected-run body, plus the active/recent/by-handoff run reads)
is lifted out of `app/main.py`'s blob into a clean vertical module behind the SDK
`RunStatePort`. It follows the PATTERN the analytics → agents → settings → dispatch
carves established; this is the LAST module carve, so Track A's five feature modules
are then all behind ports.

SCOPE — READ ONLY (the established carve shape). The orchestrator's IMPERATIVE core
stays in `main.py` / `orchestrator.py`: `_dispatch_run` / spawn, `_pm_beat`, Approve
& Run, the autonomy toggle, and the SSE `/runstate/stream` WRITER side. This module
owns only the run-READ logic — the run rail + transcript view-model (lifted 1:1 from
`main._agent_runs_view_store` + its `_store_run_row` / `_store_transcript_view` render
mappers) and thin pass-throughs to the port's `list_active` / `get_run` / `recent` /
`by_handoff` reads. It spawns nothing and writes nothing.

The module has three parts and these tests pin each:

  * `app/runs/service.py` — the run-read LOGIC (`RunsService`). It depends ONLY on
    `domain.runstate.RunStatePort` (the run-state SSOT) + an INJECTED relative-time
    formatter (the analytics/dispatch callable-injection pattern — so the service
    stays free of the concrete `main._activity_relative` / `datetime` shaping). It
    imports NOTHING outward (no fastapi / httpx / subprocess / psycopg2 / asyncpg)
    and never reaches back into `app.main`, the concrete `appdb` / `adapters`, or the
    `app.orchestrator` imperative core. The rail/transcript shaping moved 1:1 from
    `main._store_run_row` / `_store_transcript_view` / `_agent_runs_view_store`.
    → tested against a FAKE `RunStatePort` (no DB).

  * `app/runs/api.py` — a FastAPI `APIRouter` (the imperative shell — MAY import
    fastapi) whose `GET /runs/{project}` (active+recent) / `GET /runs/{project}/by-
    handoff/{handoff_id}` / `GET /runs/run/{run_id}` construct the service over the
    port (resolved from `app.state.runstate` via `Depends`) and return JSON. → tested
    by driving the route functions directly with a fake port (no ASGI / live DB).

These tests are written BEFORE the implementation (strict TDD) and match the existing
fake-driven, no-DB style (`test_dispatch_module.py` / `test_agents_module.py`).
"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.runstate import RunRecord, RunSpan, RunStatePort


# ---------------------------------------------------------------------------
#  Fake RunStatePort — serves scripted run records (no DB).
# ---------------------------------------------------------------------------


class FakeRunStore:
    """Structural `RunStatePort` stand-in for the runs read service (no app-DB).

    Serves scripted `RunRecord`s for the READ methods the module uses (`recent`,
    `get_run`, `list_active`, `by_handoff`). The WRITER side (`start_run` /
    `append_output` / `set_status` / `heartbeat` / `subscribe`) is intentionally
    NOT implemented — the runs READ carve never touches it. `down` simulates a
    degraded store (every read returns the empty value — the house-law graceful
    degrade); `raises` makes the reads throw (the service must still degrade)."""

    def __init__(self, *, runs=None, down=False, raises=False):
        # `runs` is keyed by run_id; `recent`/`list_active` return them newest-first
        # in insertion order (the caller scripts the order).
        self._runs: dict[str, RunRecord] = {r.run_id: r for r in (runs or [])}
        self._order: list[str] = [r.run_id for r in (runs or [])]
        self._down = down
        self._raises = raises
        self.calls: list[str] = []

    def _all(self) -> list[RunRecord]:
        return [self._runs[rid] for rid in self._order]

    async def recent(self, project=None, limit: int = 20, *, lease_owner=None):
        # `lease_owner` (OPTIONAL, additive — the explain gallery enumerates explain
        # runs through it) scopes the read to runs holding that lease; None keeps the
        # project-wide recent behaviour. The runs READ service never passes it.
        self.calls.append(f"recent:{project}:{limit}:{lease_owner or ''}")
        if self._raises:
            raise RuntimeError("store down")
        if self._down:
            return []
        rows = [
            r for r in self._all()
            if (project is None or (r.project or "").lower() == project.lower())
            and (lease_owner is None or (r.lease_owner or "") == lease_owner)
        ]
        return rows[:limit]

    async def list_active(self, project=None):
        self.calls.append(f"list_active:{project}")
        if self._raises:
            raise RuntimeError("store down")
        if self._down:
            return []
        return [
            r for r in self._all()
            if r.status in ("queued", "running")
            and (project is None or (r.project or "").lower() == project.lower())
        ]

    async def get_run(self, run_id: str):
        self.calls.append(f"get_run:{run_id}")
        if self._raises:
            raise RuntimeError("store down")
        if self._down:
            return None
        return self._runs.get(run_id)

    async def by_handoff(self, handoff_id: str):
        self.calls.append(f"by_handoff:{handoff_id}")
        if self._raises:
            raise RuntimeError("store down")
        if self._down:
            return None
        # newest run for the handoff wins (the store's contract); _order is oldest→
        # newest as scripted, so scan in reverse.
        for rid in reversed(self._order):
            r = self._runs[rid]
            if r.handoff_id == handoff_id:
                return r
        return None


class CancelRunStore:
    def __init__(self, *, run_id: str = "run-cancel", status: str | None = "running"):
        self.run_id = run_id
        self.record = (
            RunRecord(run_id=run_id, project="kaidera-os", agent="ren", status=status)
            if status is not None
            else None
        )
        self.statuses: list[dict] = []

    async def get_run(self, run_id: str):
        return self.record if run_id == self.run_id else None

    async def set_status(self, run_id: str, status: str, *, error=None, metadata=None):
        self.statuses.append({"run_id": run_id, "status": status, "error": error})
        if self.record is not None:
            self.record.status = status
            self.record.error = error


class CancelReq:
    def __init__(self, *, harness_port=None):
        self.app = type("App", (), {})()
        self.app.state = type("State", (), {})()
        self.app.state.local_run_tasks = {}
        self.app.state.harness_port = harness_port


class CancelHarnessPort:
    def __init__(self, *, result: bool = False):
        self.result = result
        self.cancelled: list[str] = []

    async def cancel_run(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return self.result


def _stub_relative(ts):
    """A relative-time formatter stand-in (the real one is `main._activity_relative`,
    which the service takes as an INJECTED callable so it stays free of the concrete
    datetime shaping). Deterministic: a present ts → 'ago', else ''."""
    return "ago" if ts else ""


# Two agents' runs in ONE project. ren has a RUNNING run (with a body) + an older OK
# run; quill has one OK run for a handoff. The store keys runs by the plain (lower-
# cased) agent name, exactly as `start_run` lowers it.
REN_RUNNING = RunRecord(
    run_id="run-ren-1",
    project="kaidera-os",
    agent="ren",
    agent_display="Ren",
    handoff_id="h-1",
    harness="claude-code",
    model="claude-opus-4-8[1m]",
    status="running",
    started_at="2026-06-06T10:00:00",
    updated_at="2026-06-06T10:05:00",
    spans=[
        RunSpan(seq=0, kind="think", text="planning…"),
        RunSpan(seq=1, kind="tool", text="grep foo"),
        RunSpan(seq=2, kind="output", text="done."),
    ],
)
REN_OLD_OK = RunRecord(
    run_id="run-ren-0",
    project="kaidera-os",
    agent="ren",
    agent_display="Ren",
    handoff_id="h-0",
    status="ok",
    started_at="2026-06-06T09:00:00",
    updated_at="2026-06-06T09:10:00",
    ended_at="2026-06-06T09:10:00",
)
QUILL_OK = RunRecord(
    run_id="run-quill-0",
    project="kaidera-os",
    agent="quill",
    agent_display="Quill",
    handoff_id="h-q",
    status="ok",
    started_at="2026-06-06T08:00:00",
    updated_at="2026-06-06T08:30:00",
    ended_at="2026-06-06T08:30:00",
)

# Newest-first scripted order (the real store returns newest-first); ren's running
# run leads, then ren's old ok, then quill's.
SAMPLE_RUNS = [REN_RUNNING, REN_OLD_OK, QUILL_OK]


# ---------------------------------------------------------------------------
#  service.py — the run-read logic moved out of main.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_runs_view_lists_agent_rail_and_selects_running_body():
    """`RunsService.agent_runs_view` builds the agent-detail LIVE-WORK-TRANSCRIPT
    context for ONE agent from the store: the recent-run rail filtered to THAT agent
    (newest-first), with the agent's RUNNING run auto-selected and its body hydrated
    — the substance lifted 1:1 from `main._agent_runs_view_store`."""
    from app.runs.service import RunsService

    store = FakeRunStore(runs=SAMPLE_RUNS)
    svc = RunsService(store=store, relative=_stub_relative)
    ctx = await svc.agent_runs_view("kaidera-os", "ren")

    # the rail is filtered to ren's two runs (quill's run is excluded), newest-first
    assert [r["run_id"] for r in ctx["agent_runs"]] == ["run-ren-1", "run-ren-0"]
    assert ctx["agent_run_count"] == 2
    assert ctx["agent_run_running"] == 1
    # no explicit run pinned → the RUNNING run is auto-selected, body hydrated
    assert ctx["agent_run_selected_id"] == "run-ren-1"
    sel = ctx["agent_run_selected"]
    assert sel is not None
    # the body re-read carries the spans (seg-typed 1:1) + the joined body
    assert [s["kind"] for s in sel["segments"]] == ["think", "tool", "output"]
    assert sel["body"] == "planning…grep foodone."
    assert sel["truncated"] is False
    # the running-dot flag is on (a running run is selected)
    assert ctx["agent_run_active"] is True
    # the store is present → not the 'no run-state SSOT' degrade
    assert ctx["agent_run_no_orch"] is False
    # it actually read the recent rail + the selected body from the port
    assert any(c.startswith("recent:") for c in store.calls)
    assert "get_run:run-ren-1" in store.calls


@pytest.mark.asyncio
async def test_agent_runs_view_honors_pinned_run_id():
    """A pinned `run_id` (the live SSE pane carries it) selects THAT run's body —
    but only when it belongs to the agent (the `_agent_runs_view_store` guard)."""
    from app.runs.service import RunsService

    store = FakeRunStore(runs=SAMPLE_RUNS)
    svc = RunsService(store=store, relative=_stub_relative)

    # pin ren's OLD ok run → it's selected even though a newer running run exists
    ctx = await svc.agent_runs_view("kaidera-os", "ren", run_id="run-ren-0")
    assert ctx["agent_run_selected_id"] == "run-ren-0"
    assert "get_run:run-ren-0" in store.calls

    # pin a run that belongs to ANOTHER agent → ignored, falls back to ren's running
    ctx = await svc.agent_runs_view("kaidera-os", "ren", run_id="run-quill-0")
    assert ctx["agent_run_selected_id"] == "run-ren-1"


@pytest.mark.asyncio
async def test_store_run_row_and_transcript_view_shapes():
    """The header + body render mappers (lifted 1:1 from `main._store_run_row` /
    `_store_transcript_view`): the header carries the run identity + the friendly
    status chip + the relative-age labels (via the injected formatter); the
    transcript adds the error/ended-age + seg-typed segments + the joined body."""
    from app.runs.service import RunsService

    svc = RunsService(store=FakeRunStore(), relative=_stub_relative)

    row = svc.store_run_row(REN_RUNNING)
    assert row["run_id"] == "run-ren-1"
    assert row["agent_display"] == "Ren"
    assert row["handoff_id"] == "h-1"
    assert row["handoff_short"] == "h-1"
    assert row["status"] == "running"
    assert row["running"] is True
    assert row["status_label"] == "running"
    # relative-age labels come through the INJECTED formatter (present ts → 'ago')
    assert row["started_ago"] == "ago"
    assert row["updated_ago"] == "ago"

    # the friendly status chip maps ok→completed, error→errored, queued→queued
    assert svc.store_run_row(REN_OLD_OK)["status_label"] == "completed"

    view = svc.store_transcript_view(REN_RUNNING)
    assert view["body"] == "planning…grep foodone."
    assert [s["kind"] for s in view["segments"]] == ["think", "tool", "output"]
    assert view["truncated"] is False


@pytest.mark.asyncio
async def test_run_row_and_transcript_surface_metadata_sidecar():
    """ISSUE A — the run read-model surfaces the `metadata` JSONB sidecar.

    An Explain run stamps `run_state.metadata = {"capability": "explain",
    "artifact_id": …}` on terminal success; the adapter already carries it onto the
    `RunRecord` (JSONB → dict). The READ shaping must pass it through so a reader (the
    SPA, the explain gallery) can jump from the run to its persisted L5 artifact via
    `GET /runs/run/{id}`. A run with no sidecar surfaces `metadata: None` (the safe
    default — autonomous runs / chat turns are byte-for-byte unchanged)."""
    from app.runs.service import RunsService

    svc = RunsService(store=FakeRunStore(), relative=_stub_relative)

    explain_run = RunRecord(
        run_id="run-explain-1",
        project="kaidera-os",
        agent="console",
        status="ok",
        lease_owner="explain",
        metadata={"capability": "explain", "artifact_id": "art-xyz"},
        started_at="2026-06-07T10:00:00",
        spans=[RunSpan(seq=0, kind="output", text="<!DOCTYPE html><html></html>")],
    )

    # The HEADER row carries the sidecar.
    row = svc.store_run_row(explain_run)
    assert row["metadata"] == {"capability": "explain", "artifact_id": "art-xyz"}

    # The TRANSCRIPT view (extends the header) carries it too.
    view = svc.store_transcript_view(explain_run)
    assert view["metadata"] == {"capability": "explain", "artifact_id": "art-xyz"}

    # A run with no sidecar surfaces None (the additive default).
    assert svc.store_run_row(REN_RUNNING)["metadata"] is None


@pytest.mark.asyncio
async def test_get_run_endpoint_returns_metadata():
    """`GET /runs/run/{id}` (the route the SPA reads) includes the run's `metadata`
    sidecar in its JSON — so the explain gallery can read `artifact_id` from the run."""
    from app.runs import api as runs_api

    explain_run = RunRecord(
        run_id="run-explain-1",
        project="kaidera-os",
        agent="console",
        status="ok",
        lease_owner="explain",
        metadata={"capability": "explain", "artifact_id": "art-xyz"},
        started_at="2026-06-07T10:00:00",
        spans=[RunSpan(seq=0, kind="output", text="<!DOCTYPE html><html></html>")],
    )
    store = FakeRunStore(runs=[explain_run])

    detail = await runs_api.run_detail_endpoint("run-explain-1", store=store)
    assert detail["run_id"] == "run-explain-1"
    assert detail["metadata"] == {"capability": "explain", "artifact_id": "art-xyz"}


@pytest.mark.asyncio
async def test_active_and_recent_passthrough():
    """`RunsService.active` / `recent` pass through to the port's `list_active` /
    `recent` and shape each header row (the run-board view-model). Active returns
    only queued|running runs; recent returns the headers newest-first."""
    from app.runs.service import RunsService

    store = FakeRunStore(runs=SAMPLE_RUNS)
    svc = RunsService(store=store, relative=_stub_relative)

    active = await svc.active("kaidera-os")
    assert [r["run_id"] for r in active] == ["run-ren-1"]  # only the running one
    assert active[0]["running"] is True

    recent = await svc.recent("kaidera-os", limit=10)
    assert [r["run_id"] for r in recent] == ["run-ren-1", "run-ren-0", "run-quill-0"]
    # board view-model: a run header row carries the friendly status label
    assert recent[1]["status_label"] == "completed"

    assert any(c.startswith("list_active:") for c in store.calls)
    assert any(c.startswith("recent:") for c in store.calls)


@pytest.mark.asyncio
async def test_board_combines_active_and_recent():
    """`RunsService.board` is the run-board view-model: active runs + recent headers
    + the counts, one shaped payload (active reads + recent reads through the port)."""
    from app.runs.service import RunsService

    store = FakeRunStore(runs=SAMPLE_RUNS)
    svc = RunsService(store=store, relative=_stub_relative)
    board = await svc.board("kaidera-os")

    assert [r["run_id"] for r in board["active"]] == ["run-ren-1"]
    assert board["active_count"] == 1
    assert [r["run_id"] for r in board["recent"]] == [
        "run-ren-1", "run-ren-0", "run-quill-0"
    ]
    assert board["recent_count"] == 3


@pytest.mark.asyncio
async def test_get_run_and_by_handoff_shape_full_body():
    """`RunsService.get_run` / `by_handoff` return ONE run WITH its hydrated body
    (the transcript view-model), or None when unknown — the read pass-throughs over
    the port's `get_run` / `by_handoff`."""
    from app.runs.service import RunsService

    store = FakeRunStore(runs=SAMPLE_RUNS)
    svc = RunsService(store=store, relative=_stub_relative)

    run = await svc.get_run("run-ren-1")
    assert run is not None
    assert run["run_id"] == "run-ren-1"
    assert run["body"] == "planning…grep foodone."

    by_h = await svc.by_handoff("h-1")
    assert by_h is not None and by_h["run_id"] == "run-ren-1"

    assert await svc.get_run("nope") is None
    assert await svc.by_handoff("nope") is None


@pytest.mark.asyncio
async def test_graceful_degrade_when_store_none_or_down():
    """GRACEFUL-DEGRADE (house law): a None store, a down store, or reads that RAISE
    all degrade to the clean empty state (empty rail/active/recent, None selected,
    None get_run/by_handoff) — never a 500. `agent_run_no_orch` is True only when the
    store itself is absent (run-state SSOT down)."""
    from app.runs.service import RunsService

    # 1) None store — the SSOT failed to construct.
    svc_none = RunsService(store=None, relative=_stub_relative)
    ctx = await svc_none.agent_runs_view("kaidera-os", "ren")
    assert ctx["agent_runs"] == [] and ctx["agent_run_selected"] is None
    assert ctx["agent_run_no_orch"] is True  # store absent → the degrade copy shows
    assert await svc_none.active("kaidera-os") == []
    assert await svc_none.recent("kaidera-os") == []
    assert await svc_none.get_run("run-ren-1") is None
    assert await svc_none.by_handoff("h-1") is None
    board = await svc_none.board("kaidera-os")
    assert board["active"] == [] and board["recent"] == []

    # 2) A store whose reads RAISE — every read is wrapped; a hiccup never blanks.
    svc_raise = RunsService(
        store=FakeRunStore(runs=SAMPLE_RUNS, raises=True), relative=_stub_relative
    )
    ctx = await svc_raise.agent_runs_view("kaidera-os", "ren")
    assert ctx["agent_runs"] == [] and ctx["agent_run_selected"] is None
    # the store EXISTS (it just errored) → not the 'no SSOT' degrade
    assert ctx["agent_run_no_orch"] is False
    assert await svc_raise.active("kaidera-os") == []
    assert await svc_raise.get_run("run-ren-1") is None
    assert await svc_raise.by_handoff("h-1") is None

    # 3) A down store — empty reads, the clean empty state.
    svc_down = RunsService(
        store=FakeRunStore(runs=SAMPLE_RUNS, down=True), relative=_stub_relative
    )
    assert (await svc_down.agent_runs_view("kaidera-os", "ren"))["agent_runs"] == []
    assert await svc_down.recent("kaidera-os") == []


def test_service_depends_only_on_port_not_outward():
    """GUARD: `app/runs/service.py` imports NOTHING outward (no fastapi / httpx /
    subprocess / psycopg2 / asyncpg) and does NOT reach for `app.main`, the concrete
    `app.appdb` / `app.adapters`, or the `app.orchestrator` imperative core — only the
    domain `RunStatePort` (+ the injected relative-time callable).

    Parsed via `ast` (a name in a comment/docstring can't fool it), mirroring
    `test_ports_purity.py` / the dispatch guard. This is the module-isolation rule the
    `.importlinter` independence contract also enforces at the graph level."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "app" / "runs" / "service.py"
    ).read_text()
    tree = ast.parse(src)
    top: set[str] = set()
    dotted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top.add(a.name.split(".")[0])
                dotted.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top.add(node.module.split(".")[0])
                dotted.add(node.module)

    forbidden = {"fastapi", "starlette", "httpx", "subprocess", "psycopg2", "asyncpg"}
    assert not (top & forbidden), (
        f"service.py must not import outward I/O libs, got: {sorted(top & forbidden)}"
    )
    # No reaching back into the blob, the concrete adapters/db, or the orchestrator.
    assert "app.main" not in dotted, "service.py must not import app.main"
    assert not any(
        m == "app.appdb"
        or m == "app.orchestrator"
        or m.startswith("app.adapters")
        for m in dotted
    ), "service.py must depend on the domain RunStatePort + the injected callable"


# ---------------------------------------------------------------------------
#  api.py — the FastAPI router (imperative shell; builds svc over the port)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_runs_board_endpoint_returns_board():
    """Driving the `GET /runs/{project}` handler directly returns the service's run
    board (active + recent + counts), no ASGI / live DB — a fake port."""
    from app.runs import api as runs_api

    store = FakeRunStore(runs=SAMPLE_RUNS)
    result = await runs_api.runs_board_endpoint("kaidera-os", store=store)

    assert result["project"] == "kaidera-os"
    assert result["active_count"] == 1
    assert [r["run_id"] for r in result["active"]] == ["run-ren-1"]
    assert result["recent_count"] == 3


@pytest.mark.asyncio
async def test_router_run_detail_and_by_handoff_endpoints():
    """`GET /runs/run/{run_id}` returns one run WITH its body; `GET /runs/{project}/by-
    handoff/{handoff_id}` returns the latest run for a handoff — both driven directly
    with a fake port (no ASGI)."""
    from app.runs import api as runs_api

    store = FakeRunStore(runs=SAMPLE_RUNS)

    detail = await runs_api.run_detail_endpoint("run-ren-1", store=store)
    assert detail["run_id"] == "run-ren-1"
    assert detail["body"] == "planning…grep foodone."

    by_h = await runs_api.run_by_handoff_endpoint("kaidera-os", "h-1", store=store)
    assert by_h["run_id"] == "run-ren-1"


@pytest.mark.asyncio
async def test_router_run_detail_404_when_unknown():
    """An unknown run id raises a 404 (the read surface's not-found), never a 500 —
    the body read degraded to None and the route surfaces it as not-found."""
    from fastapi import HTTPException

    from app.runs import api as runs_api

    store = FakeRunStore(runs=SAMPLE_RUNS)
    with pytest.raises(HTTPException) as ei:
        await runs_api.run_detail_endpoint("nope", store=store)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_run_endpoint_cancels_local_task_harness_and_marks_active_run():
    from app import local_run_tasks
    from app.runs import api as runs_api

    async def _forever():
        await asyncio.Event().wait()

    port = CancelHarnessPort(result=True)
    req = CancelReq(harness_port=port)
    store = CancelRunStore(status="running")
    task = asyncio.create_task(_forever())
    local_run_tasks.register_local_run_task(req.app.state, "run-cancel", task)

    result = await runs_api.cancel_run_endpoint(req, "run-cancel", store=store)

    assert result["cancelled"] is True
    assert result["local_task_cancelled"] is True
    assert result["harness_cancelled"] is True
    assert result["marked"] is True
    assert store.statuses == [{
        "run_id": "run-cancel",
        "status": "error",
        "error": local_run_tasks.LOCAL_RUN_CANCELLED_ERROR,
    }]
    assert port.cancelled == ["run-cancel"]
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_run_endpoint_is_noop_for_unknown_or_terminal_runs():
    from app.runs import api as runs_api

    unknown_req = CancelReq(harness_port=CancelHarnessPort(result=False))
    unknown = await runs_api.cancel_run_endpoint(
        unknown_req, "missing", store=CancelRunStore(status=None)
    )
    assert unknown["cancelled"] is False
    assert unknown["marked"] is False
    assert unknown["status"] is None

    terminal_store = CancelRunStore(status="ok")
    terminal = await runs_api.cancel_run_endpoint(
        CancelReq(harness_port=CancelHarnessPort(result=False)),
        "run-cancel",
        store=terminal_store,
    )
    assert terminal["cancelled"] is False
    assert terminal["marked"] is False
    assert terminal["status"] == "ok"
    assert terminal_store.statuses == []


def test_router_runs_path_does_not_collide():
    """The runs JSON routes live under the distinct `/runs/...` root — so mounting the
    router additively can NEVER shadow the existing `/runstate/stream` SSE writer
    route (a different `/runstate` root), the `/dispatch/...` routes, or the
    `/agents/...` routes. All GET (read surfaces, never mutations)."""
    from app.runs.api import router

    routes = [(sorted(r.methods or []), r.path) for r in router.routes]
    paths = {p for _, p in routes}

    # the three distinct /runs leaves
    assert "/runs/{project}" in paths
    assert "/runs/{project}/by-handoff/{handoff_id}" in paths
    assert "/runs/run/{run_id}" in paths
    assert "/runs/run/{run_id}/cancel" in paths
    # it claims NEITHER the live /runstate SSE route NOR the dispatch/agents roots
    assert "/runstate/stream" not in paths
    assert not any(p.startswith("/dispatch") for p in paths)
    assert not any(p.startswith("/agents") for p in paths)
    # the read routes are GET-only; the explicit cancel leaf is the only mutation.
    by_path = {_path: methods for methods, _path in routes}
    assert by_path["/runs/{project}"] == ["GET"]
    assert by_path["/runs/{project}/by-handoff/{handoff_id}"] == ["GET"]
    assert by_path["/runs/run/{run_id}"] == ["GET"]
    assert by_path["/runs/run/{run_id}/cancel"] == ["POST"]


def test_router_is_apirouter_with_routes():
    """`app.runs.api.router` is a FastAPI APIRouter exposing the runs paths under the
    module's `/runs` prefix (so `main` can `include_router` it additively)."""
    from fastapi import APIRouter

    from app.runs.api import router

    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    assert "/runs/{project}" in paths


def test_module_exports_service_and_router():
    """`app.runs` re-exports the service + router (the module's public face)."""
    import app.runs as runs

    assert hasattr(runs, "RunsService")
    assert hasattr(runs, "router")
