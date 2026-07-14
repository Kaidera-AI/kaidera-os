"""Dispatch Bug A — CONSERVATIVE reclaim of genuinely-orphaned claims.

A handoff CLAIMED by an agent that never ran it (e.g. an agent whose loop was
disabled) stays stuck forever: the dispatch path only picks up PENDING rows
(claimed ones are skipped as in-flight) and the watchdog only ESCALATES, never
requeues. The reclaim pass (``Orchestrator._reclaim_orphaned_claims`` →
``_maybe_reclaim_one``) RELEASES such a claim back to pending so the existing
dispatch path re-picks it — but ONLY when ALL three bars hold:

  1. status == claimed,
  2. ``claimed_age`` > ``RECLAIM_ORPHAN_S`` (a slow start is NOT an orphan), AND
  3. NO run was ever started for it (truly orphaned — no partial work to lose),
     proven against a REACHABLE run-state store (a down/None store → SKIP, never
     reclaim blind).

These pin the gate with the cortex client + run-state store MOCKED — we assert the
release decision, not a live Cortex round-trip. Async-test style mirrors
test_orchestrator_runstate.py / test_orchestrator_spawn.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.orchestrator as orch
from app.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
#  Fakes — a cortex client that serves claimed handoffs + records release calls,
#  and a run-state store that reports reachable + whether a run exists per handoff.
# ---------------------------------------------------------------------------

class FakeCortex:
    """Serves a fixed CLAIMED-handoff list and records every release_handoff call."""

    def __init__(self, claimed: list[dict] | None = None, *, list_raises: bool = False):
        self._claimed = claimed or []
        self._list_raises = list_raises
        self.released: list[tuple[str, str]] = []  # (project, handoff_id)

    async def get_handoffs(self, project_key: str, status: str | None = None):
        if self._list_raises:
            raise RuntimeError("cortex down")
        # The reclaim pass asks for status='claimed'; anything else → [] (pending).
        return list(self._claimed) if status == "claimed" else []

    async def release_handoff(self, project_key: str, handoff_id: str, agent: str = ""):
        self.released.append((project_key, handoff_id))
        return True


class FakeRunState:
    """Run-state stub. ``reachable`` controls the _pool() probe; ``runs`` maps a
    handoff_id → a truthy run record (its presence means a run EXISTS)."""

    def __init__(self, *, reachable: bool = True, runs: dict[str, object] | None = None,
                 by_handoff_raises: bool = False):
        self._reachable = reachable
        self._runs = runs or {}
        self._by_handoff_raises = by_handoff_raises
        self.by_handoff_calls: list[str] = []

    async def _pool(self):
        # Mirror RunStatePgStore._pool: None == down/unreachable.
        return object() if self._reachable else None

    async def by_handoff(self, handoff_id: str):
        self.by_handoff_calls.append(handoff_id)
        if self._by_handoff_raises:
            raise RuntimeError("store hiccup")
        return self._runs.get(handoff_id)

    async def list_active(self, project=None):  # fallback-probe path (no _pool use here)
        if not self._reachable:
            raise RuntimeError("store down")
        return []


def _make_orch(cortex, runstate):
    """An Orchestrator with inert stubs + injected cortex/runstate. The reclaim pass
    only touches self._cortex (get_handoffs/release_handoff) and self._runstate."""
    return Orchestrator(
        cortex=cortex,
        appdb=object(),
        harness_runner=object(),
        chat_routing_for=lambda agent, project: ("pi", "m", "high"),
        record_usage=None,
        find_agent=lambda agents, name: None,
        resolve_target=lambda handoff, agents: None,
        classify_interactive=lambda agent, desig: False,
        project_identity=lambda cortex, project: None,
        agent_view=lambda a: a,
        runstate=runstate,
    )


def _ts_ago(seconds: float) -> str:
    """A Cortex-style ISO timestamp ``seconds`` in the past (tz-aware UTC)."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _claimed(hid: str, *, age_s: float, field: str = "claimed_at") -> dict:
    """A claimed handoff aged ``age_s`` via the given timestamp field."""
    return {"id": hid, "status": "claimed", "claimed_by": "kai", field: _ts_ago(age_s)}


def _feed_texts(o, project="kaidera-os"):
    return [e.get("text") for e in o.feed.recent(project)]


# ---------------------------------------------------------------------------
#  THE ORPHAN CASE — claimed, aged, no run → released.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orphaned_claim_is_released():
    """Claimed > RECLAIM_ORPHAN_S ago AND no run ever started → release → pending."""
    hid = "orphan-abc12345"
    cortex = FakeCortex([_claimed(hid, age_s=orch.RECLAIM_ORPHAN_S + 600)])
    rs = FakeRunState(reachable=True, runs={})  # no run for any handoff
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == [("kaidera-os", hid)]
    assert rs.by_handoff_calls == [hid]  # the no-run check ran
    assert any("released orphaned claim" in t for t in _feed_texts(o))


# ---------------------------------------------------------------------------
#  SAFETY — recently-claimed is NOT an orphan.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recently_claimed_is_not_released():
    """A claim younger than RECLAIM_ORPHAN_S is a slow start, not an orphan → kept.
    The no-run lookup is short-circuited by the age bar (cheaper + safer)."""
    cortex = FakeCortex([_claimed("fresh-1", age_s=10.0)])
    rs = FakeRunState(reachable=True, runs={})
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == []
    assert rs.by_handoff_calls == []  # age bar failed before the run check


# ---------------------------------------------------------------------------
#  SAFETY — a claim WITH a run (in-flight / partial work) is NOT released.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claimed_with_run_is_not_released():
    """Aged past the threshold but a run EXISTS → in-flight / has partial work →
    left for the watchdog, never reclaimed."""
    hid = "running-1"
    cortex = FakeCortex([_claimed(hid, age_s=orch.RECLAIM_ORPHAN_S + 999)])
    rs = FakeRunState(reachable=True, runs={hid: object()})  # a run exists
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == []
    assert rs.by_handoff_calls == [hid]  # the run check ran and found a run


# ---------------------------------------------------------------------------
#  SAFETY — a down / unreachable run-state store skips the whole pass.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_down_runstate_skips_reclaim():
    """An unreachable store makes by_handoff() return None for EVERY handoff (its
    degrade signal), which would look like 'no run' — so the pass SKIPS entirely
    (never reclaim blind). No claimed fetch, no release, no run lookup."""
    cortex = FakeCortex([_claimed("aged-1", age_s=orch.RECLAIM_ORPHAN_S + 600)])
    rs = FakeRunState(reachable=False, runs={})
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == []
    assert rs.by_handoff_calls == []


@pytest.mark.asyncio
async def test_none_runstate_skips_reclaim():
    """No store injected at all → cannot prove 'no run' → SKIP (never reclaim blind)."""
    cortex = FakeCortex([_claimed("aged-2", age_s=orch.RECLAIM_ORPHAN_S + 600)])
    o = _make_orch(cortex, None)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == []


# ---------------------------------------------------------------------------
#  SAFETY — an undatable claim (no usable timestamp) is NEVER reclaimed.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_with_no_timestamp_is_not_released():
    """A claimed row carrying NO usable timestamp → unknown age → never reclaimed
    (we never reclaim without an age)."""
    cortex = FakeCortex([{"id": "nots-1", "status": "claimed", "claimed_by": "kai"}])
    rs = FakeRunState(reachable=True, runs={})
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == []
    assert rs.by_handoff_calls == []  # age unknown → short-circuit before run check


# ---------------------------------------------------------------------------
#  ROBUSTNESS — a per-row store hiccup skips that row, never reclaims blind.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_by_handoff_raise_skips_row():
    """If the per-row by_handoff lookup raises (a transient store error) we SKIP
    that row rather than releasing blind."""
    cortex = FakeCortex([_claimed("hiccup-1", age_s=orch.RECLAIM_ORPHAN_S + 600)])
    rs = FakeRunState(reachable=True, by_handoff_raises=True)
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == []


# ---------------------------------------------------------------------------
#  ROBUSTNESS — a failed claimed-list fetch is best-effort (no crash, no release).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claimed_list_fetch_failure_is_best_effort():
    """A failed get_handoffs(status='claimed') skips the pass without raising."""
    cortex = FakeCortex([], list_raises=True)
    rs = FakeRunState(reachable=True, runs={})
    o = _make_orch(cortex, rs)

    # Must not raise.
    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == []


# ---------------------------------------------------------------------------
#  The LIST shape only carries created_at — confirm it still ages + reclaims.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  RETRY CAP (AV-3) — no path requeues without a ceiling. A claim already
#  requeued to RECLAIM_MAX_RETRIES is NOT released again; it's left claimed for
#  the watchdog to escalate (mirrors the watchdog's stuck-run requeue cap).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_at_retry_cap_is_not_released():
    """An aged, no-run orphan whose retry_count has hit RECLAIM_MAX_RETRIES is NOT
    released again — the reclaim path honors the same requeue ceiling as the watchdog,
    leaving the row claimed for watchdog escalation (no requeue-loop forever). The
    cheaper cap bar short-circuits BEFORE the run-state lookup."""
    hid = "capped-1"
    h = {**_claimed(hid, age_s=orch.RECLAIM_ORPHAN_S + 600),
         "retry_count": orch.RECLAIM_MAX_RETRIES}
    cortex = FakeCortex([h])
    rs = FakeRunState(reachable=True, runs={})  # no run — would otherwise reclaim
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == [], "a claim at the retry cap must NOT be requeued again"
    assert rs.by_handoff_calls == [], "the cap bar short-circuits before the run check"


@pytest.mark.asyncio
async def test_retry_cap_is_deduped_across_reconcile_sweeps():
    hid = "capped-log-1"
    handoff = {
        **_claimed(hid, age_s=orch.RECLAIM_ORPHAN_S + 600),
        "retry_count": orch.RECLAIM_MAX_RETRIES,
    }
    instance = _make_orch(FakeCortex([handoff]), FakeRunState(reachable=True, runs={}))
    await instance._reclaim_orphaned_claims("kaidera-os", [])
    await instance._reclaim_orphaned_claims("kaidera-os", [])

    assert instance._reclaim_cap_logged == {("kaidera-os", hid)}


@pytest.mark.asyncio
async def test_claim_under_retry_cap_is_released():
    """An orphan still UNDER the retry cap (retry_count 0) is released (requeued) as
    before — the cap only blocks once the ceiling is reached."""
    hid = "under-cap-1"
    h = {**_claimed(hid, age_s=orch.RECLAIM_ORPHAN_S + 600), "retry_count": 0}
    cortex = FakeCortex([h])
    rs = FakeRunState(reachable=True, runs={})
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == [("kaidera-os", hid)]
    assert rs.by_handoff_calls == [hid]  # under cap → proceeds to the run check


@pytest.mark.asyncio
async def test_created_at_fallback_ages_and_reclaims():
    """The live /handoffs list carries created_at (not claimed_at). A row created
    long ago + still claimed + no run is reclaimed via the created_at fallback."""
    hid = "created-old-1"
    cortex = FakeCortex(
        [_claimed(hid, age_s=orch.RECLAIM_ORPHAN_S + 4000, field="created_at")]
    )
    rs = FakeRunState(reachable=True, runs={})
    o = _make_orch(cortex, rs)

    await o._reclaim_orphaned_claims("kaidera-os", [])

    assert cortex.released == [("kaidera-os", hid)]
