"""Tests for app/watchdog.py — the deterministic PM failure-supervisor.

TDD order:
  1. classify_run — all four verdict paths
  2. scan_once — correct actions for a mixed list of handoffs
  3. escalation dedup — same stuck handoff escalated only once across two scans
  4. durable dedup — fresh _escalated set still skips if live escalation exists
  5. clear-on-resolve — reconcile auto-completes stale escalations
"""
import pytest

from app.watchdog import classify_run, Watchdog, WATCHDOG_STALE_S, WATCHDOG_MAX_RETRIES


# ---------------------------------------------------------------------------
#  FakeOps — a scriptable duck-type stand-in for CliWatchdogOps.
#  It records every action the Watchdog takes so tests can assert precisely.
# ---------------------------------------------------------------------------

class FakeOps:
    """Scriptable per-handoff ops adapter.

    ``scripts`` maps handoff_id -> dict:
      {
        "has_success_marker": bool,
        "claimed_age_seconds":  float | None,
      }

    Actions recorded: list of ("complete", id) | ("release", id, reason) |
    ("escalate", id, reason) and get_handoffs returns whatever was set via
    ``.set_handoffs(...)``.

    ``set_open_escalations`` sets the list returned by ``get_open_escalations``
    so tests can pre-populate live Cortex state without needing real I/O.
    """

    def __init__(self):
        self._handoffs: list[dict] = []
        self._scripts: dict[str, dict] = {}
        self._open_escalations: list[dict] = []
        self.actions: list[tuple] = []

    def set_handoffs(self, handoffs: list[dict]) -> None:
        self._handoffs = handoffs

    def set_open_escalations(self, escalations: list[dict]) -> None:
        """Pre-populate the live Cortex open-escalations state for testing."""
        self._open_escalations = escalations

    def script(self, handoff_id: str, *, has_success_marker: bool, claimed_age_seconds) -> None:
        self._scripts[handoff_id] = {
            "has_success_marker": has_success_marker,
            "claimed_age_seconds": claimed_age_seconds,
        }

    async def get_handoffs(self, project) -> list[dict]:
        return list(self._handoffs)

    async def get_open_escalations(self, project) -> list[dict]:
        return list(self._open_escalations)

    async def has_success_marker(self, project, handoff_id) -> bool:
        entry = self._scripts.get(handoff_id, {})
        return bool(entry.get("has_success_marker", False))

    async def claimed_age_seconds(self, project, handoff_id):
        entry = self._scripts.get(handoff_id, {})
        return entry.get("claimed_age_seconds", None)

    async def complete(self, project, handoff_id) -> None:
        self.actions.append(("complete", handoff_id))

    async def release(self, project, handoff_id, reason: str = "") -> None:
        self.actions.append(("release", handoff_id, reason))

    async def escalate(self, project, handoff: dict, reason: str) -> None:
        self.actions.append(("escalate", handoff.get("id"), reason))


# ---------------------------------------------------------------------------
#  classify_run tests — all four deterministic verdict paths
# ---------------------------------------------------------------------------

class TestClassifyRun:
    """Pure function: no I/O, fully deterministic."""

    def test_completed_status_returns_completed(self):
        """Already closed — we don't care about age or marker."""
        assert classify_run("completed", False, 9999.0) == "completed"

    def test_completed_status_ignores_marker(self):
        assert classify_run("completed", True, 100.0) == "completed"

    def test_completed_case_insensitive(self):
        assert classify_run("  COMPLETED  ", False, None) == "completed"

    def test_claimed_with_success_marker_is_recover(self):
        """Worker logged COMPLETED but handoff stayed claimed (silent-complete failure)."""
        assert classify_run("claimed", True, 500.0) == "recover"

    def test_claimed_with_marker_age_none_still_recover(self):
        """Age is unknown but the marker is present → recover regardless."""
        assert classify_run("claimed", True, None) == "recover"

    def test_claimed_stale_no_marker_is_stuck(self):
        """Stale run with no success marker → stuck (escalate)."""
        threshold = 900.0
        assert classify_run("claimed", False, threshold + 1.0, threshold) == "stuck"

    def test_claimed_exactly_at_threshold_is_not_stuck(self):
        """Exactly at threshold is NOT stuck (> not >=)."""
        threshold = 900.0
        assert classify_run("claimed", False, threshold, threshold) == "healthy"

    def test_claimed_recent_no_marker_is_healthy(self):
        """Run is young — still working."""
        threshold = 900.0
        assert classify_run("claimed", False, threshold - 1.0, threshold) == "healthy"

    def test_claimed_age_none_no_marker_is_healthy(self):
        """No age info available — can't call it stuck; leave it alone."""
        assert classify_run("claimed", False, None) == "healthy"

    def test_non_claimed_non_completed_is_healthy(self):
        """Pending, open, etc. — not our concern."""
        assert classify_run("pending", False, 9999.0) == "healthy"
        assert classify_run("", False, 9999.0) == "healthy"
        assert classify_run("open", True, 9999.0) == "healthy"

    def test_uses_default_threshold_constant(self):
        """With no explicit threshold the module constant is used."""
        # Just past the default threshold → stuck.
        assert classify_run("claimed", False, WATCHDOG_STALE_S + 1.0) == "stuck"
        # Just under → healthy.
        assert classify_run("claimed", False, WATCHDOG_STALE_S - 1.0) == "healthy"


# ---------------------------------------------------------------------------
#  scan_once tests — one supervision pass with a mixed handoff list
# ---------------------------------------------------------------------------

class TestScanOnce:
    """Tests that scan_once calls the right ops and returns correct counts."""

    @pytest.mark.asyncio
    async def test_completed_handoff_is_ignored(self):
        """A completed handoff doesn't trigger any action."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-done", "status": "completed"}])
        # No script needed — it should never ask about it.
        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")
        assert counts["scanned"] == 0
        assert ops.actions == []

    @pytest.mark.asyncio
    async def test_pending_handoff_is_ignored(self):
        """Non-claimed handoffs (pending / open) are skipped — we only watch claimed."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-pend", "status": "pending"}])
        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")
        assert counts["scanned"] == 0
        assert ops.actions == []

    @pytest.mark.asyncio
    async def test_recover_path_calls_complete(self):
        """Claimed + success marker → complete() is called, recovered++ ."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-rec", "status": "claimed"}])
        ops.script("h-rec", has_success_marker=True, claimed_age_seconds=500.0)
        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")
        assert counts["recovered"] == 1
        assert counts["escalated"] == 0
        assert ("complete", "h-rec") in ops.actions

    @pytest.mark.asyncio
    async def test_stuck_path_at_cap_calls_escalate(self):
        """Claimed + no marker + stale AND at the retry cap → escalate() is called,
        escalated++ (requeue is exhausted, so the run escalates to the lead)."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-stuck", "status": "claimed", "retry_count": 3}])
        ops.script("h-stuck", has_success_marker=False, claimed_age_seconds=1200.0)
        w = Watchdog(ops, stale_threshold_s=900.0)  # default max_retries=3
        counts = await w.scan_once("kaidera-os")
        assert counts["escalated"] == 1
        assert counts["recovered"] == 0
        assert counts["requeued"] == 0
        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]
        assert len(escalate_calls) == 1
        assert escalate_calls[0][1] == "h-stuck"

    @pytest.mark.asyncio
    async def test_healthy_path_no_actions(self):
        """Claimed + no marker + young → no action, healthy++ ."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-ok", "status": "claimed"}])
        ops.script("h-ok", has_success_marker=False, claimed_age_seconds=100.0)
        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")
        assert counts["healthy"] == 1
        assert counts["recovered"] == 0
        assert counts["escalated"] == 0
        assert ops.actions == []

    @pytest.mark.asyncio
    async def test_healthy_age_none_no_actions(self):
        """Claimed + no marker + age unknown → leave alone (healthy)."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-unk", "status": "claimed"}])
        ops.script("h-unk", has_success_marker=False, claimed_age_seconds=None)
        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")
        assert counts["healthy"] == 1
        assert ops.actions == []

    @pytest.mark.asyncio
    async def test_mixed_handoff_list(self):
        """Four handoffs: 1 completed (ignored), 1 recover, 1 stuck, 1 healthy.

        Asserts counts and the exact set of ops actions recorded.
        """
        ops = FakeOps()
        ops.set_handoffs([
            {"id": "h-done",  "status": "completed"},                      # ignored
            {"id": "h-rec",   "status": "claimed"},                         # recover (marker present)
            {"id": "h-stuck", "status": "claimed", "retry_count": 3},       # stuck, at cap → escalate
            {"id": "h-young", "status": "claimed"},                         # healthy (young, no marker)
        ])
        ops.script("h-rec",   has_success_marker=True,  claimed_age_seconds=500.0)
        ops.script("h-stuck", has_success_marker=False, claimed_age_seconds=1800.0)
        ops.script("h-young", has_success_marker=False, claimed_age_seconds=60.0)

        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")

        assert counts["scanned"]   == 3  # only claimed rows
        assert counts["recovered"] == 1
        assert counts["escalated"] == 1
        assert counts["healthy"]   == 1

        # complete called exactly once, for h-rec
        complete_calls = [a for a in ops.actions if a[0] == "complete"]
        assert complete_calls == [("complete", "h-rec")]

        # escalate called exactly once, for h-stuck
        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]
        assert len(escalate_calls) == 1
        assert escalate_calls[0][1] == "h-stuck"


# ---------------------------------------------------------------------------
#  Auto-requeue + retry cap tests (AV-3)
#  A stuck mid-run handoff is REQUEUED (released → pending) while its retry_count
#  is below the cap; AT/OVER the cap it ESCALATES instead (never loops forever).
# ---------------------------------------------------------------------------

class TestRequeueAndRetryCap:
    """The stuck-run verdict now branches on retry_count: requeue under the cap,
    escalate at/over it. This is the core AV-3 behavior."""

    @pytest.mark.asyncio
    async def test_stuck_under_cap_requeues_not_escalates(self):
        """A stuck run with retry_count below the cap is RELEASED (requeued) back to
        pending — NOT escalated. Asserts the REAL release call + the handoff id + the
        stuck reason (watchdog bugs hide behind mocks that accept anything)."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-req", "status": "claimed", "retry_count": 0}])
        ops.script("h-req", has_success_marker=False, claimed_age_seconds=1800.0)

        w = Watchdog(ops, stale_threshold_s=900.0, max_retries=3)
        counts = await w.scan_once("kaidera-os")

        assert counts["requeued"] == 1
        assert counts["escalated"] == 0
        assert counts["recovered"] == 0
        release_calls = [a for a in ops.actions if a[0] == "release"]
        assert len(release_calls) == 1, "a stuck run under the cap must be released exactly once"
        assert release_calls[0][1] == "h-req", "release must target the real stuck handoff id"
        # The stuck reason is forwarded (real argument, not a placeholder).
        assert "no success marker" in release_calls[0][2]
        # It must NOT escalate while requeues remain.
        assert [a for a in ops.actions if a[0] == "escalate"] == []

    @pytest.mark.asyncio
    async def test_missing_retry_count_treated_as_zero_requeues(self):
        """A handoff with NO retry_count field reads as 0 → first stuck detection
        requeues (the common case for a freshly-stuck run)."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-fresh", "status": "claimed"}])  # no retry_count
        ops.script("h-fresh", has_success_marker=False, claimed_age_seconds=1800.0)

        w = Watchdog(ops, stale_threshold_s=900.0, max_retries=3)
        counts = await w.scan_once("kaidera-os")

        assert counts["requeued"] == 1
        assert counts["escalated"] == 0
        assert ("release", "h-fresh", ops.actions[-1][2]) == ops.actions[-1]

    @pytest.mark.asyncio
    async def test_stuck_at_cap_escalates_not_requeues(self):
        """At retry_count == cap, requeue is EXHAUSTED → escalate, never release."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-cap", "status": "claimed", "retry_count": 3}])
        ops.script("h-cap", has_success_marker=False, claimed_age_seconds=1800.0)

        w = Watchdog(ops, stale_threshold_s=900.0, max_retries=3)
        counts = await w.scan_once("kaidera-os")

        assert counts["escalated"] == 1
        assert counts["requeued"] == 0
        assert [a for a in ops.actions if a[0] == "release"] == []
        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]
        assert len(escalate_calls) == 1 and escalate_calls[0][1] == "h-cap"

    @pytest.mark.asyncio
    async def test_stuck_over_cap_escalates(self):
        """Over the cap (retry_count > cap) also escalates — defensive (a run that
        somehow exceeded the cap must not requeue)."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-over", "status": "claimed", "retry_count": 7}])
        ops.script("h-over", has_success_marker=False, claimed_age_seconds=1800.0)

        w = Watchdog(ops, stale_threshold_s=900.0, max_retries=3)
        counts = await w.scan_once("kaidera-os")

        assert counts["escalated"] == 1
        assert counts["requeued"] == 0
        assert [a for a in ops.actions if a[0] == "release"] == []

    @pytest.mark.asyncio
    async def test_requeue_progression_to_cap(self):
        """The boundary walk: retry_count 0,1,2 requeue; 3 escalates (cap=3). This is
        the bounded ladder — requeues are exhausted exactly at the cap."""
        for rc in (0, 1, 2):
            ops = FakeOps()
            ops.set_handoffs([{"id": "h", "status": "claimed", "retry_count": rc}])
            ops.script("h", has_success_marker=False, claimed_age_seconds=1800.0)
            w = Watchdog(ops, stale_threshold_s=900.0, max_retries=3)
            counts = await w.scan_once("kaidera-os")
            assert counts["requeued"] == 1 and counts["escalated"] == 0, (
                f"retry_count={rc} (< cap) must requeue"
            )

        ops = FakeOps()
        ops.set_handoffs([{"id": "h", "status": "claimed", "retry_count": 3}])
        ops.script("h", has_success_marker=False, claimed_age_seconds=1800.0)
        w = Watchdog(ops, stale_threshold_s=900.0, max_retries=3)
        counts = await w.scan_once("kaidera-os")
        assert counts["escalated"] == 1 and counts["requeued"] == 0, (
            "retry_count == cap must escalate"
        )

    @pytest.mark.asyncio
    async def test_zero_cap_escalates_on_first_stuck(self):
        """max_retries=0 means 'never requeue' — escalate on the first stuck detection
        (retry_count 0 is NOT < 0)."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-zero", "status": "claimed", "retry_count": 0}])
        ops.script("h-zero", has_success_marker=False, claimed_age_seconds=1800.0)

        w = Watchdog(ops, stale_threshold_s=900.0, max_retries=0)
        counts = await w.scan_once("kaidera-os")

        assert counts["escalated"] == 1
        assert counts["requeued"] == 0

    @pytest.mark.asyncio
    async def test_requeued_key_present_in_scan_result(self):
        """scan_once result dict must include the 'requeued' key (even when 0)."""
        ops = FakeOps()
        ops.set_handoffs([])
        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")
        assert "requeued" in counts

    def test_default_cap_constant_is_three(self):
        """The module default cap is 3 (env WATCHDOG_MAX_RETRIES); a Watchdog built
        with no explicit max_retries uses it."""
        assert WATCHDOG_MAX_RETRIES == 3
        w = Watchdog(FakeOps(), stale_threshold_s=900.0)
        assert w._max_retries == 3


# ---------------------------------------------------------------------------
#  Escalation dedup tests — same stuck handoff must only escalate once
# ---------------------------------------------------------------------------

class TestEscalationDedup:
    """The Watchdog must NOT spam the PM (Kai) with repeated escalations for the same handoff."""

    @pytest.mark.asyncio
    async def test_same_stuck_handoff_escalated_only_once(self):
        """Running scan_once twice on the same stuck handoff → escalate called once."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-stuck2", "status": "claimed", "retry_count": 3}])
        ops.script("h-stuck2", has_success_marker=False, claimed_age_seconds=2000.0)

        w = Watchdog(ops, stale_threshold_s=900.0)

        counts1 = await w.scan_once("kaidera-os")
        counts2 = await w.scan_once("kaidera-os")

        assert counts1["escalated"] == 1
        # Second pass: handoff is already in the dedup set → not escalated again.
        assert counts2["escalated"] == 0

        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]
        assert len(escalate_calls) == 1

    @pytest.mark.asyncio
    async def test_different_handoffs_each_escalated_once(self):
        """Two distinct stuck handoffs each get escalated exactly once."""
        ops = FakeOps()
        ops.set_handoffs([
            {"id": "h-s1", "status": "claimed", "retry_count": 3},
            {"id": "h-s2", "status": "claimed", "retry_count": 3},
        ])
        ops.script("h-s1", has_success_marker=False, claimed_age_seconds=2000.0)
        ops.script("h-s2", has_success_marker=False, claimed_age_seconds=2000.0)

        w = Watchdog(ops, stale_threshold_s=900.0)

        await w.scan_once("kaidera-os")
        await w.scan_once("kaidera-os")

        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]
        ids_escalated = [a[1] for a in escalate_calls]
        # Each was escalated once — dedup is per-id, not a global "only one"
        assert ids_escalated.count("h-s1") == 1
        assert ids_escalated.count("h-s2") == 1
        assert len(escalate_calls) == 2

    @pytest.mark.asyncio
    async def test_recover_happens_every_scan_until_gone(self):
        """Recover is NOT deduped — if a handoff is still claimed+marker each pass,
        we try to complete it each pass (idempotent on Cortex's end)."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "h-rec2", "status": "claimed"}])
        ops.script("h-rec2", has_success_marker=True, claimed_age_seconds=500.0)

        w = Watchdog(ops, stale_threshold_s=900.0)

        await w.scan_once("kaidera-os")
        await w.scan_once("kaidera-os")

        complete_calls = [a for a in ops.actions if a[0] == "complete"]
        # complete is safe to re-issue (idempotent), and the watchdog does it both passes
        assert len(complete_calls) == 2


# ---------------------------------------------------------------------------
#  run_forever safety — verifies it never raises out of the loop and the
#  stop event terminates it cleanly
# ---------------------------------------------------------------------------

class TestRunForever:
    @pytest.mark.asyncio
    async def test_stop_event_terminates_loop(self):
        """run_forever exits when stop is set — no hang, no exception."""
        import asyncio

        ops = FakeOps()
        ops.set_handoffs([])
        w = Watchdog(ops, stale_threshold_s=900.0)
        stop = asyncio.Event()
        stop.set()  # already set → exits after first iteration

        # Must not raise and must complete within a reasonable time.
        await asyncio.wait_for(
            w.run_forever(lambda: ["kaidera-os"], interval_s=0.01, stop=stop),
            timeout=2.0,
        )

    @pytest.mark.asyncio
    async def test_scan_exception_does_not_raise_out_of_loop(self):
        """If scan_once raises, run_forever swallows it and keeps going."""
        import asyncio

        class BustedOps(FakeOps):
            async def get_handoffs(self, project):
                raise RuntimeError("cortex unavailable")

        ops = BustedOps()
        w = Watchdog(ops, stale_threshold_s=900.0)
        stop = asyncio.Event()

        # Let it run one iteration, which will trigger the exception, then stop.
        async def _stop_soon():
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.create_task(_stop_soon())
        # Must complete without raising RuntimeError
        await asyncio.wait_for(
            w.run_forever(lambda: ["kaidera-os"], interval_s=0.01, stop=stop),
            timeout=2.0,
        )


# ---------------------------------------------------------------------------
#  Durable dedup tests (E007 flood-bug fix)
#  The in-memory _escalated set is wiped between Watchdog instances to simulate
#  a process restart — the live Cortex check must prevent re-filing.
# ---------------------------------------------------------------------------

class TestDurableDedup:
    """Escalation dedup MUST survive process restarts via live Cortex check."""

    @pytest.mark.asyncio
    async def test_fresh_escalated_set_skips_if_live_escalation_exists(self):
        """A fresh Watchdog instance (empty _escalated) still skips if a
        [WATCHDOG-SIGNAL] for the same stuck handoff already exists in Cortex.

        This is the core durable-dedup regression guard: previously a process
        restart reset _escalated and re-filed duplicate escalations.
        """
        ops = FakeOps()
        ops.set_handoffs([{"id": "abcdef1234567890", "status": "claimed", "retry_count": 3}])
        ops.script("abcdef1234567890", has_success_marker=False, claimed_age_seconds=2000.0)

        # Pre-populate live Cortex with an existing [WATCHDOG-SIGNAL] for "abcdef12"
        ops.set_open_escalations([
            {
                "id": "esc-existing-01",
                "summary": "[WATCHDOG-SIGNAL] stuck run abcdef12: claimed 1800s, no success marker — PM assess + decide (retry/reassign/escalate)",
                "status": "pending",
            }
        ])

        # Fresh Watchdog instance — _escalated is empty (simulates restart).
        w = Watchdog(ops, stale_threshold_s=900.0)
        assert len(w._escalated) == 0

        counts = await w.scan_once("kaidera-os")

        # No new escalation filed — the live check caught the duplicate.
        assert counts["escalated"] == 0
        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]
        assert len(escalate_calls) == 0, (
            "Must NOT re-file escalation when a live [WATCHDOG-SIGNAL] already exists "
            "for the same handoff — even with a fresh (empty) _escalated set"
        )

    @pytest.mark.asyncio
    async def test_no_live_escalation_still_escalates(self):
        """If no live escalation exists and the handoff is stuck, escalate as usual."""
        ops = FakeOps()
        ops.set_handoffs([{"id": "deadbeef12345678", "status": "claimed", "retry_count": 3}])
        ops.script("deadbeef12345678", has_success_marker=False, claimed_age_seconds=2000.0)
        ops.set_open_escalations([])  # no live escalations

        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")

        assert counts["escalated"] == 1
        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]
        assert len(escalate_calls) == 1
        assert escalate_calls[0][1] == "deadbeef12345678"

    @pytest.mark.asyncio
    async def test_two_restarts_only_one_escalation_total(self):
        """Simulate two process restarts: each creates a new Watchdog with empty
        _escalated, but the live check prevents duplicate escalations.

        Restart 1: no live escalation → escalate fires, adds to live Cortex.
        Restart 2: live escalation found → dedup blocks the duplicate.
        """
        ops = FakeOps()
        ops.set_handoffs([{"id": "c0ffee1234567890", "status": "claimed", "retry_count": 3}])
        ops.script("c0ffee1234567890", has_success_marker=False, claimed_age_seconds=2000.0)
        ops.set_open_escalations([])

        # Restart 1: fresh Watchdog — no live escalation yet → fires.
        w1 = Watchdog(ops, stale_threshold_s=900.0)
        counts1 = await w1.scan_once("kaidera-os")
        assert counts1["escalated"] == 1

        # After the first escalation, simulate that live Cortex now has it.
        ops.set_open_escalations([
            {
                "id": "esc-restart-01",
                "summary": "[WATCHDOG-SIGNAL] stuck run c0ffee12: claimed 2000s, no success marker — PM assess + decide (retry/reassign/escalate)",
                "status": "pending",
            }
        ])

        # Restart 2: a brand-new Watchdog instance — empty _escalated.
        w2 = Watchdog(ops, stale_threshold_s=900.0)
        assert len(w2._escalated) == 0
        counts2 = await w2.scan_once("kaidera-os")

        assert counts2["escalated"] == 0, (
            "Second Watchdog instance (simulating restart) must NOT re-file the "
            "escalation when the live check finds an existing one"
        )
        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]
        assert len(escalate_calls) == 1, "Total escalations across both instances must be exactly 1"


# ---------------------------------------------------------------------------
#  Clear-on-resolve (reconcile) tests (E007 flood-bug fix)
#  Stale [WATCHDOG-SIGNAL] escalations must be auto-completed when the
#  referenced stuck handoff is no longer claimed.
# ---------------------------------------------------------------------------

class TestReconcileEscalations:
    """Stale escalation noise must be cleaned up when the underlying run resolves."""

    @pytest.mark.asyncio
    async def test_reconcile_completes_escalation_when_run_resolved(self):
        """If the run referenced by a [WATCHDOG-SIGNAL] is no longer claimed,
        the escalation is auto-completed (clear-on-resolve).
        """
        ops = FakeOps()
        # The stuck run is now completed/gone — not in the claimed set.
        ops.set_handoffs([])  # no claimed handoffs
        ops.set_open_escalations([
            {
                "id": "esc-stale-01",
                "summary": "[WATCHDOG-SIGNAL] stuck run feed1234: claimed 2000s, no success marker — PM assess + decide (retry/reassign/escalate)",
                "status": "pending",
            }
        ])

        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")

        # The stale escalation must be auto-completed.
        assert counts["reconciled"] == 1
        complete_calls = [a for a in ops.actions if a[0] == "complete"]
        assert len(complete_calls) == 1
        assert complete_calls[0][1] == "esc-stale-01"

    @pytest.mark.asyncio
    async def test_reconcile_does_not_complete_escalation_when_run_still_stuck(self):
        """If the referenced run is STILL stuck (still claimed), the escalation
        stays open — reconcile must not complete it prematurely.
        """
        ops = FakeOps()
        # The stuck run is still claimed (and at the retry cap → escalation path).
        ops.set_handoffs([{"id": "babe5678abcdef01", "status": "claimed", "retry_count": 3}])
        ops.script("babe5678abcdef01", has_success_marker=False, claimed_age_seconds=2000.0)
        ops.set_open_escalations([
            {
                "id": "esc-still-active-01",
                "summary": "[WATCHDOG-SIGNAL] stuck run babe5678: claimed 2000s, no success marker — PM assess + decide (retry/reassign/escalate)",
                "status": "pending",
            }
        ])

        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")

        assert counts["reconciled"] == 0
        # The escalation itself must NOT be completed.
        complete_calls = [a for a in ops.actions if a[0] == "complete"]
        assert len(complete_calls) == 0

    @pytest.mark.asyncio
    async def test_reconcile_and_dedup_together(self):
        """Combined scenario: one run just resolved (reconcile cleans up) and one
        run is newly stuck (escalate fires — exactly once).
        """
        ops = FakeOps()
        # "old-stuck" has resolved; "new-stuck" is freshly stuck (at cap → escalates).
        ops.set_handoffs([{"id": "cafe9999abcdef01", "status": "claimed", "retry_count": 3}])
        ops.script("cafe9999abcdef01", has_success_marker=False, claimed_age_seconds=1800.0)

        ops.set_open_escalations([
            {
                "id": "esc-old-01",
                # References "d3adb3ef" which is no longer claimed.
                "summary": "[WATCHDOG-SIGNAL] stuck run d3adb3ef: claimed 3600s, no success marker — PM assess + decide (retry/reassign/escalate)",
                "status": "pending",
            }
        ])

        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")

        # Old escalation reconciled (auto-completed).
        assert counts["reconciled"] == 1
        # New escalation filed for cafe9999.
        assert counts["escalated"] == 1

        complete_calls = [a for a in ops.actions if a[0] == "complete"]
        escalate_calls = [a for a in ops.actions if a[0] == "escalate"]

        # Only "esc-old-01" was completed (reconcile), not the new stuck run.
        assert len(complete_calls) == 1
        assert complete_calls[0][1] == "esc-old-01"
        assert len(escalate_calls) == 1
        assert escalate_calls[0][1] == "cafe9999abcdef01"

    @pytest.mark.asyncio
    async def test_reconcile_count_in_scan_once_result(self):
        """scan_once result dict must include 'reconciled' key."""
        ops = FakeOps()
        ops.set_handoffs([])
        ops.set_open_escalations([])
        w = Watchdog(ops, stale_threshold_s=900.0)
        counts = await w.scan_once("kaidera-os")
        assert "reconciled" in counts


# ---------------------------------------------------------------------------
#  _extract_referenced_id unit tests
# ---------------------------------------------------------------------------

class TestExtractReferencedId:
    """Unit tests for the static method that parses stuck-run IDs from summaries."""

    def test_parses_standard_escalation_summary(self):
        summary = "[WATCHDOG-SIGNAL] stuck run abcdef12: claimed 1800s, no success marker — PM assess"
        result = Watchdog._extract_referenced_id(summary)
        assert result == "abcdef12"

    def test_returns_none_for_missing_marker(self):
        assert Watchdog._extract_referenced_id("some other summary") is None

    def test_returns_none_for_empty_string(self):
        assert Watchdog._extract_referenced_id("") is None

    def test_handles_uppercase_signal_marker(self):
        summary = "[WATCHDOG-SIGNAL] STUCK RUN dead1234: claimed 900s, no success marker"
        # The method lowercases before searching.
        result = Watchdog._extract_referenced_id(summary)
        assert result == "dead1234"
