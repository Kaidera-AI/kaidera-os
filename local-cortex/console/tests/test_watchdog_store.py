"""Milestone 1 T11 — the PM watchdog reads the RunState SSOT store for OBSERVATION,
instead of parsing Cortex CLI text. Supervision now runs on REAL signals:

  * ``has_success_marker(hid)``  → ``store.by_handoff(hid)`` + ``status == "ok"``
    (the durable terminal status the worker / Approve&Run / chat writes), NOT a
    ``cortex-search "COMPLETED <id>"`` grep.
  * ``claimed_age_seconds(hid)`` → computed from the store row's ``heartbeat_at``
    (falling back to ``started_at``), NOT a parse of the CLI's ``"Claimed at:"`` line.
  * ``classify_run`` gains a HEARTBEAT-STALENESS axis: a claimed/running DETACHED
    worker whose ``heartbeat_at`` is older than the threshold is ``stuck`` even if it
    was claimed recently (real liveness, not inferred). CRITICAL: ``lease_owner`` in
    the REQUEST-LIVED set (``approve_run`` / ``chat``) runs IN-PROCESS and never
    heartbeats (no separate PID) — its TERMINAL STATUS is the completion signal, so a
    long-running request-lived run with a null/old ``heartbeat_at`` is NOT stuck.

The store-backed ops graceful-degrade: a down store (``by_handoff`` returns None /
raises) falls back to the injected Cortex API ops so a dead app-DB can never blind or
crash the supervisor. It also terminalizes stale detached run rows so the read model
cannot show a dead process as live forever.

Style mirrors ``tests/test_watchdog.py`` (pure classifier table-tests + a scriptable
fake) and ``tests/test_watchdog_claimed.py``.
"""

from __future__ import annotations

import pytest

from app.watchdog import (
    classify_run,
    StoreWatchdogOps,
    WATCHDOG_STALE_S,
)


# ===========================================================================
#  classify_run — the new heartbeat-staleness axis (table-tests)
#
#  Signature is BACKWARD-COMPATIBLE: the new inputs are keyword-only with
#  defaults, so every existing positional call in test_watchdog.py still holds.
#    classify_run(status, has_success_marker, claimed_age_s,
#                 stale_threshold_s=…, *, heartbeat_age_s=None, lease_owner=None)
# ===========================================================================

THRESH = 900.0


class TestClassifyRunHeartbeatAxis:
    """The heartbeat-staleness axis: detached workers are judged on heartbeat age;
    request-lived (approve_run / chat) runs are judged on TERMINAL STATUS only."""

    # -- detached worker: stale heartbeat → stuck, even if claimed recently --------

    def test_detached_stale_heartbeat_is_stuck_even_if_recently_claimed(self):
        """A DETACHED worker (lease_owner='worker') whose heartbeat is older than the
        threshold is STUCK — even though it was claimed only moments ago. This is the
        whole point of T11: real liveness beats inferred 'claimed age'."""
        verdict = classify_run(
            "claimed", False, 10.0, THRESH,            # claimed just 10s ago
            heartbeat_age_s=THRESH + 100.0,            # but heartbeat is stale
            lease_owner="worker",
        )
        assert verdict == "stuck"

    def test_detached_fresh_heartbeat_is_healthy_even_if_claimed_long_ago(self):
        """A DETACHED worker claimed long ago but still heart-beating is HEALTHY —
        the live heartbeat proves the process is alive, so old claimed-age must not
        flag it. (Liveness supersedes the stale claimed-age heuristic.)"""
        verdict = classify_run(
            "claimed", False, THRESH + 5000.0, THRESH,  # claimed ages ago
            heartbeat_age_s=5.0,                         # but beating right now
            lease_owner="worker",
        )
        assert verdict == "healthy"

    def test_detached_stale_heartbeat_with_marker_is_recover(self):
        """Recover still wins: a stale-heartbeat detached run that ALSO has its
        success marker (terminal status ok) just needs re-completing, not escalating."""
        verdict = classify_run(
            "claimed", True, 10.0, THRESH,
            heartbeat_age_s=THRESH + 100.0,
            lease_owner="worker",
        )
        assert verdict == "recover"

    # -- request-lived (approve_run / chat): NEVER stuck on heartbeat ---------------

    def test_long_approve_run_null_heartbeat_is_NOT_stuck(self):
        """THE critical case (per the T9 notes): an in-process approve_run never
        heartbeats (no separate PID). A long-running approve_run with a NULL
        heartbeat must NOT be classified dead — its terminal status is the signal."""
        verdict = classify_run(
            "claimed", False, THRESH + 100.0, THRESH,   # 'claimed age' is long…
            heartbeat_age_s=None,                        # …and it never heartbeats
            lease_owner="approve_run",
        )
        assert verdict != "stuck", (
            "an in-process approve_run is REQUEST-LIVED — a null/old heartbeat must "
            "NOT mark it dead; only its terminal status completes it"
        )
        assert verdict == "healthy"

    def test_long_approve_run_old_heartbeat_is_NOT_stuck(self):
        """Even a stale (non-null) heartbeat_age must not flag a request-lived
        approve_run — the heartbeat axis simply does not apply to in-process runs."""
        verdict = classify_run(
            "claimed", False, 10.0, THRESH,
            heartbeat_age_s=THRESH + 99999.0,
            lease_owner="approve_run",
        )
        assert verdict == "healthy"

    def test_chat_lease_is_request_lived_too(self):
        """An interactive chat run (lease_owner='chat', T10) is ALSO request-lived —
        in-process, never heartbeats — so the same exemption applies."""
        verdict = classify_run(
            "claimed", False, THRESH + 100.0, THRESH,
            heartbeat_age_s=None,
            lease_owner="chat",
        )
        assert verdict == "healthy"

    def test_request_lived_terminal_ok_recovers(self):
        """A request-lived run whose store row reached 'ok' (marker present) but whose
        handoff stayed claimed → recover (re-complete), regardless of heartbeat."""
        verdict = classify_run(
            "claimed", True, THRESH + 100.0, THRESH,
            heartbeat_age_s=None,
            lease_owner="approve_run",
        )
        assert verdict == "recover"

    # -- backward-compat: no heartbeat info → the original claimed-age behavior -----

    def test_no_heartbeat_info_falls_back_to_claimed_age_stuck(self):
        """With NO heartbeat info and NO lease_owner (e.g. the Cortex fallback, or a row
        the store never wrote), classify_run must behave EXACTLY as before: a stale
        claimed-age with no marker is stuck."""
        assert classify_run("claimed", False, THRESH + 1.0, THRESH) == "stuck"

    def test_no_heartbeat_info_recent_claimed_age_healthy(self):
        assert classify_run("claimed", False, THRESH - 1.0, THRESH) == "healthy"

    def test_detached_no_heartbeat_but_stale_claimed_age_still_stuck(self):
        """A detached worker we have NO heartbeat reading for (heartbeat_age_s=None)
        but a STALE claimed-age → still stuck via the original axis. (A worker that
        was supposed to heartbeat but we can't read one, and is also long-claimed,
        is genuinely suspect.)"""
        verdict = classify_run(
            "claimed", False, THRESH + 50.0, THRESH,
            heartbeat_age_s=None,
            lease_owner="worker",
        )
        assert verdict == "stuck"

    def test_completed_status_short_circuits_regardless_of_heartbeat(self):
        assert classify_run(
            "completed", False, 10.0, THRESH,
            heartbeat_age_s=THRESH + 100.0, lease_owner="worker",
        ) == "completed"

    def test_non_claimed_is_healthy_regardless_of_heartbeat(self):
        assert classify_run(
            "pending", False, 9999.0, THRESH,
            heartbeat_age_s=THRESH + 100.0, lease_owner="worker",
        ) == "healthy"


# ===========================================================================
#  StoreWatchdogOps — observation reads the store; everything else delegates
#  to injected Cortex ops. Graceful-degrade to Cortex when the store is down.
# ===========================================================================

class FakeRecord:
    """A minimal RunRecord-shaped stand-in (only the fields the watchdog reads)."""

    def __init__(self, *, status="running", heartbeat_at=None, started_at=None,
                 lease_owner=None, run_id=None):
        self.status = status
        self.heartbeat_at = heartbeat_at
        self.started_at = started_at
        self.lease_owner = lease_owner
        self.run_id = run_id


class FakeStore:
    """Scriptable RunStatePort stand-in: ``by_handoff`` returns a pre-set record (or
    None / raises) per handoff id. Records every lookup."""

    def __init__(self, *, raising=False):
        self.raising = raising
        self._rows: dict[str, FakeRecord] = {}
        self.lookups: list[str] = []
        self.status_updates: list[tuple[str, str, str | None]] = []

    def set_row(self, hid: str, rec: FakeRecord | None) -> None:
        if rec is not None and rec.run_id is None:
            rec.run_id = hid
        self._rows[hid] = rec

    async def by_handoff(self, handoff_id):
        self.lookups.append(handoff_id)
        if self.raising:
            raise RuntimeError("store down")
        return self._rows.get(handoff_id)

    async def list_active(self, project=None):
        if self.raising:
            raise RuntimeError("store down")
        return [
            row for row in self._rows.values()
            if row is not None and row.status in {"queued", "running"}
        ]

    async def set_status(self, run_id, status, *, error=None, metadata=None):
        if self.raising:
            raise RuntimeError("store down")
        self.status_updates.append((run_id, status, error))
        for row in self._rows.values():
            if row is not None and row.run_id == run_id:
                row.status = status


class FakeCliOps:
    """Stand-in for the injected CortexWatchdogOps fallback. Returns scripted values and
    records that it was consulted (so we can prove the store-down fallback path)."""

    def __init__(self, *, marker=False, age=None):
        self._marker = marker
        self._age = age
        self.marker_calls: list[str] = []
        self.age_calls: list[str] = []
        self.handoffs_calls = 0
        self.escalations_calls = 0
        self.complete_calls: list[str] = []
        self.release_calls: list[tuple] = []
        self.escalate_calls: list[tuple] = []

    async def get_handoffs(self, project):
        self.handoffs_calls += 1
        return []

    async def get_open_escalations(self, project):
        self.escalations_calls += 1
        return []

    async def has_success_marker(self, project, handoff_id):
        self.marker_calls.append(handoff_id)
        return self._marker

    async def claimed_age_seconds(self, project, handoff_id):
        self.age_calls.append(handoff_id)
        return self._age

    async def complete(self, project, handoff_id):
        self.complete_calls.append(handoff_id)

    async def release(self, project, handoff_id, reason=""):
        self.release_calls.append((handoff_id, reason))

    async def escalate(self, project, handoff, reason):
        self.escalate_calls.append((handoff.get("id"), reason))


def _now_iso(offset_s: float = 0.0) -> str:
    """An ISO-8601 UTC timestamp `offset_s` seconds in the PAST (offset>0 = older)."""
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=offset_s)
    return t.isoformat()


class TestStoreOpsSuccessMarker:
    @pytest.mark.asyncio
    async def test_has_success_marker_reads_store_status_ok(self):
        """A store row at status='ok' IS the success marker (no Cortex grep)."""
        store = FakeStore()
        store.set_row("h-ok", FakeRecord(status="ok"))
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        assert await ops.has_success_marker("kaidera-os", "h-ok") is True

    @pytest.mark.asyncio
    async def test_has_success_marker_false_for_running_row(self):
        store = FakeStore()
        store.set_row("h-run", FakeRecord(status="running"))
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        assert await ops.has_success_marker("kaidera-os", "h-run") is False

    @pytest.mark.asyncio
    async def test_has_success_marker_false_for_error_row(self):
        store = FakeStore()
        store.set_row("h-err", FakeRecord(status="error"))
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        assert await ops.has_success_marker("kaidera-os", "h-err") is False

    @pytest.mark.asyncio
    async def test_has_success_marker_store_miss_falls_back_to_cli(self):
        """No store row (None) → fall back to the CLI marker grep (the run may predate
        the store, or the row was pruned)."""
        store = FakeStore()
        store.set_row("h-miss", None)
        cli = FakeCliOps(marker=True)
        ops = StoreWatchdogOps(store, cli, project="kaidera-os")
        assert await ops.has_success_marker("kaidera-os", "h-miss") is True
        assert cli.marker_calls == ["h-miss"], "store miss must consult the Cortex fallback"

    @pytest.mark.asyncio
    async def test_has_success_marker_store_down_falls_back_to_cli(self):
        """A raising store (app-DB down) must fall back to the CLI, never crash."""
        store = FakeStore(raising=True)
        cli = FakeCliOps(marker=True)
        ops = StoreWatchdogOps(store, cli, project="kaidera-os")
        assert await ops.has_success_marker("kaidera-os", "h-x") is True
        assert cli.marker_calls == ["h-x"]


class TestStoreOpsClaimedAge:
    @pytest.mark.asyncio
    async def test_claimed_age_from_heartbeat_at(self):
        """Age is computed from heartbeat_at (the live liveness stamp)."""
        store = FakeStore()
        store.set_row("h1", FakeRecord(status="running", heartbeat_at=_now_iso(120.0)))
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        age = await ops.claimed_age_seconds("kaidera-os", "h1")
        assert age is not None and 100.0 < age < 200.0

    @pytest.mark.asyncio
    async def test_claimed_age_falls_back_to_started_at_when_no_heartbeat(self):
        """No heartbeat yet (e.g. just-started run) → fall back to started_at."""
        store = FakeStore()
        store.set_row("h2", FakeRecord(status="running", heartbeat_at=None,
                                       started_at=_now_iso(300.0)))
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        age = await ops.claimed_age_seconds("kaidera-os", "h2")
        assert age is not None and 250.0 < age < 360.0

    @pytest.mark.asyncio
    async def test_claimed_age_store_miss_falls_back_to_cli(self):
        store = FakeStore()
        store.set_row("h3", None)
        cli = FakeCliOps(age=42.0)
        ops = StoreWatchdogOps(store, cli, project="kaidera-os")
        age = await ops.claimed_age_seconds("kaidera-os", "h3")
        assert age == 42.0
        assert cli.age_calls == ["h3"]

    @pytest.mark.asyncio
    async def test_claimed_age_store_down_falls_back_to_cli(self):
        store = FakeStore(raising=True)
        cli = FakeCliOps(age=7.0)
        ops = StoreWatchdogOps(store, cli, project="kaidera-os")
        assert await ops.claimed_age_seconds("kaidera-os", "h4") == 7.0
        assert cli.age_calls == ["h4"]

    @pytest.mark.asyncio
    async def test_claimed_age_no_stamps_at_all_returns_none(self):
        """A row with neither heartbeat_at nor started_at and no CLI age → None
        (classifier treats unknown age as healthy)."""
        store = FakeStore()
        store.set_row("h5", FakeRecord(status="running", heartbeat_at=None, started_at=None))
        ops = StoreWatchdogOps(store, FakeCliOps(age=None), project="kaidera-os")
        assert await ops.claimed_age_seconds("kaidera-os", "h5") is None


class TestStoreOpsHeartbeatAndLease:
    @pytest.mark.asyncio
    async def test_heartbeat_age_seconds_from_row(self):
        store = FakeStore()
        store.set_row("h1", FakeRecord(heartbeat_at=_now_iso(50.0)))
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        age = await ops.heartbeat_age_seconds("kaidera-os", "h1")
        assert age is not None and 30.0 < age < 90.0

    @pytest.mark.asyncio
    async def test_heartbeat_age_none_when_never_beat(self):
        """A request-lived (in-process) run never heartbeats → heartbeat_age is None.
        This is the signal classify_run uses to apply the request-lived exemption."""
        store = FakeStore()
        store.set_row("h2", FakeRecord(heartbeat_at=None))
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        assert await ops.heartbeat_age_seconds("kaidera-os", "h2") is None

    @pytest.mark.asyncio
    async def test_heartbeat_age_none_on_store_down(self):
        ops = StoreWatchdogOps(FakeStore(raising=True), FakeCliOps(), project="kaidera-os")
        assert await ops.heartbeat_age_seconds("kaidera-os", "h") is None

    @pytest.mark.asyncio
    async def test_lease_owner_from_row(self):
        store = FakeStore()
        store.set_row("h1", FakeRecord(lease_owner="approve_run"))
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        assert await ops.lease_owner("kaidera-os", "h1") == "approve_run"

    @pytest.mark.asyncio
    async def test_lease_owner_none_on_miss_or_down(self):
        store = FakeStore()
        store.set_row("h2", None)
        ops = StoreWatchdogOps(store, FakeCliOps(), project="kaidera-os")
        assert await ops.lease_owner("kaidera-os", "h2") is None
        ops2 = StoreWatchdogOps(FakeStore(raising=True), FakeCliOps(), project="kaidera-os")
        assert await ops2.lease_owner("kaidera-os", "h") is None


class TestStoreRunReconciliation:
    @pytest.mark.asyncio
    async def test_stale_detached_active_run_is_terminalized(self):
        store = FakeStore()
        store.set_row(
            "stale-1",
            FakeRecord(
                heartbeat_at=_now_iso(WATCHDOG_STALE_S + 300),
                started_at=_now_iso(WATCHDOG_STALE_S + 600),
                lease_owner="orchestrator",
            ),
        )
        ops = StoreWatchdogOps(store, FakeCliOps(), project="marketing")

        reconciled = await ops.reconcile_stale_runs("marketing", WATCHDOG_STALE_S)

        assert reconciled == 1
        assert store.status_updates[0][0:2] == ("stale-1", "error")
        assert "without heartbeat" in (store.status_updates[0][2] or "")

    @pytest.mark.asyncio
    async def test_request_lived_active_run_is_not_terminalized_by_heartbeat(self):
        store = FakeStore()
        store.set_row(
            "chat-1",
            FakeRecord(
                heartbeat_at=None,
                started_at=_now_iso(WATCHDOG_STALE_S + 600),
                lease_owner="chat",
            ),
        )
        ops = StoreWatchdogOps(store, FakeCliOps(), project="marketing")

        reconciled = await ops.reconcile_stale_runs("marketing", WATCHDOG_STALE_S)

        assert reconciled == 0
        assert store.status_updates == []


class TestStoreOpsDelegation:
    """StoreWatchdogOps delegates Cortex handoff reads and mutations to its base ops."""

    @pytest.mark.asyncio
    async def test_get_handoffs_delegates_to_cli(self):
        cli = FakeCliOps()
        ops = StoreWatchdogOps(FakeStore(), cli, project="kaidera-os")
        await ops.get_handoffs("kaidera-os")
        assert cli.handoffs_calls == 1

    @pytest.mark.asyncio
    async def test_get_open_escalations_delegates_to_cli(self):
        cli = FakeCliOps()
        ops = StoreWatchdogOps(FakeStore(), cli, project="kaidera-os")
        await ops.get_open_escalations("kaidera-os")
        assert cli.escalations_calls == 1

    @pytest.mark.asyncio
    async def test_complete_delegates_to_cli(self):
        cli = FakeCliOps()
        ops = StoreWatchdogOps(FakeStore(), cli, project="kaidera-os")
        await ops.complete("kaidera-os", "h-done")
        assert cli.complete_calls == ["h-done"]

    @pytest.mark.asyncio
    async def test_release_delegates_to_cli(self):
        """The mutating requeue delegates to the Cortex ops."""
        cli = FakeCliOps()
        ops = StoreWatchdogOps(FakeStore(), cli, project="kaidera-os")
        await ops.release("kaidera-os", "h-stuck", "claimed 1800s, no marker")
        assert cli.release_calls == [("h-stuck", "claimed 1800s, no marker")]

    @pytest.mark.asyncio
    async def test_escalate_delegates_to_cli(self):
        cli = FakeCliOps()
        ops = StoreWatchdogOps(FakeStore(), cli, project="kaidera-os")
        await ops.escalate("kaidera-os", {"id": "h-stuck"}, "claimed 1800s, no marker")
        assert cli.escalate_calls == [("h-stuck", "claimed 1800s, no marker")]


# ===========================================================================
#  End-to-end through Watchdog.scan_once with StoreWatchdogOps:
#  the heartbeat-staleness verdict drives a real escalate, and the
#  request-lived exemption keeps a long approve_run healthy.
# ===========================================================================

class TestScanOnceWithStoreOps:
    @pytest.mark.asyncio
    async def test_stale_heartbeat_detached_run_at_cap_escalates(self):
        """A detached worker, recently CLAIMED but with a STALE heartbeat, is stuck.
        At the retry cap (requeues exhausted) → scan_once escalates it (real-liveness
        supervision, end to end). The escalate path delegates to Cortex ops."""
        from app.watchdog import Watchdog

        store = FakeStore()
        # Recently claimed (so the OLD claimed-age axis would call it healthy)…
        # …but heartbeat is stale → the new axis flags it stuck.
        store.set_row("detached-1", FakeRecord(
            status="running",
            heartbeat_at=_now_iso(WATCHDOG_STALE_S + 300.0),  # stale beat
            started_at=_now_iso(20.0),                         # claimed just now
            lease_owner="worker",
        ))
        cli = FakeCliOps()
        # retry_count at the cap → escalate (not requeue).
        cli.get_handoffs = _const_handoffs(  # type: ignore
            [{"id": "detached-1", "status": "claimed", "retry_count": 3}]
        )
        ops = StoreWatchdogOps(store, cli, project="kaidera-os")

        w = Watchdog(ops, stale_threshold_s=WATCHDOG_STALE_S, max_retries=3)
        counts = await w.scan_once("kaidera-os")

        assert counts["escalated"] == 1, "a stale-heartbeat detached run at cap must escalate"
        assert cli.escalate_calls and cli.escalate_calls[0][0] == "detached-1"
        assert cli.release_calls == [], "at the cap it escalates, never requeues"

    @pytest.mark.asyncio
    async def test_stale_heartbeat_detached_run_under_cap_requeues_via_cli(self):
        """A stale-heartbeat detached run UNDER the cap is REQUEUED end-to-end: the
        store-backed ops delegate release to the injected Cortex ops."""
        from app.watchdog import Watchdog

        store = FakeStore()
        store.set_row("detached-2", FakeRecord(
            status="running",
            heartbeat_at=_now_iso(WATCHDOG_STALE_S + 300.0),  # stale beat → stuck
            started_at=_now_iso(20.0),
            lease_owner="worker",
        ))
        cli = FakeCliOps()
        cli.get_handoffs = _const_handoffs(  # type: ignore
            [{"id": "detached-2", "status": "claimed", "retry_count": 0}]
        )
        ops = StoreWatchdogOps(store, cli, project="kaidera-os")

        w = Watchdog(ops, stale_threshold_s=WATCHDOG_STALE_S, max_retries=3)
        counts = await w.scan_once("kaidera-os")

        assert counts["requeued"] == 1, "a stale-heartbeat detached run under cap must requeue"
        assert counts["escalated"] == 0
        assert cli.release_calls and cli.release_calls[0][0] == "detached-2", (
            "release must delegate to Cortex ops with the real handoff id"
        )
        assert cli.escalate_calls == []

    @pytest.mark.asyncio
    async def test_long_approve_run_not_escalated(self):
        """A long-running approve_run (in-process, never heartbeats, NULL
        heartbeat_at) must NOT be escalated — it is request-lived; only its terminal
        status completes it."""
        from app.watchdog import Watchdog

        store = FakeStore()
        store.set_row("approve-1", FakeRecord(
            status="running",
            heartbeat_at=None,                          # never heartbeats
            started_at=_now_iso(WATCHDOG_STALE_S + 600.0),  # running a long time
            lease_owner="approve_run",
        ))
        cli = FakeCliOps()
        cli.get_handoffs = _const_handoffs([{"id": "approve-1", "status": "claimed"}])  # type: ignore
        ops = StoreWatchdogOps(store, cli, project="kaidera-os")

        w = Watchdog(ops, stale_threshold_s=WATCHDOG_STALE_S)
        counts = await w.scan_once("kaidera-os")

        assert counts["escalated"] == 0, (
            "a long-running in-process approve_run must NOT be escalated on a null/old "
            "heartbeat — it is request-lived (terminal status is the completion signal)"
        )
        assert counts["healthy"] == 1
        assert cli.escalate_calls == []

    @pytest.mark.asyncio
    async def test_store_ok_row_recovers(self):
        """A handoff still 'claimed' whose store row reached 'ok' → recover (the
        marker now comes from the store status, not a Cortex grep)."""
        from app.watchdog import Watchdog

        store = FakeStore()
        store.set_row("ok-1", FakeRecord(status="ok", heartbeat_at=_now_iso(5.0),
                                         lease_owner="worker"))
        cli = FakeCliOps()
        cli.get_handoffs = _const_handoffs([{"id": "ok-1", "status": "claimed"}])  # type: ignore
        ops = StoreWatchdogOps(store, cli, project="kaidera-os")

        w = Watchdog(ops, stale_threshold_s=WATCHDOG_STALE_S)
        counts = await w.scan_once("kaidera-os")

        assert counts["recovered"] == 1
        assert cli.complete_calls == ["ok-1"]


def _const_handoffs(handoffs):
    """Return an async get_handoffs that always yields `handoffs` (monkeypatch helper
    for FakeCliOps so scan_once sees a scripted claimed list)."""
    async def _gh(project):
        return list(handoffs)
    return _gh
