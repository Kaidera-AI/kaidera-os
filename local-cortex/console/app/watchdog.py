"""Runtime watchdog — deterministic failure-supervisor for autonomous work.

The watchdog is a DETERMINISTIC supervisor, not an LLM. Its job: classify each
in-flight (claimed) handoff as healthy / recover / stuck based on purely
observable signals, take the correct safe action, and escalate when needed.

Design principles
-----------------
* Pure classifier at the heart — ``classify_run`` has no I/O at all and is
  fully unit-testable in isolation.
* Injected ops adapter (``CortexWatchdogOps`` / ``FakeOps`` in tests) — all side
  effects are behind a duck-typed interface so the logic and the I/O are
  independently testable.
* Durable escalation dedup — before filing a ``[WATCHDOG-SIGNAL]`` for handoff X,
  the watchdog checks live open escalations (via ``get_open_escalations``) for an
  existing one that references ``X[:8]``. This survives process restarts. The
  in-memory ``_escalated`` set is kept as a fast-path to avoid redundant live checks
  within a single process session.
* Clear-on-resolve (reconcile) — each ``scan_once`` starts with
  ``reconcile_escalations``: it lists open ``[WATCHDOG-SIGNAL]`` escalations, checks
  whether the referenced stuck handoff has since resolved (completed / no longer
  claimed), and auto-completes stale escalation noise.
* Never crashes the loop — ``run_forever`` wraps each scan in contextlib.suppress
  so a transient Cortex outage or CLI failure degrades gracefully.
* Auto-requeue with a retry cap — a stuck mid-run handoff is REQUEUED (released
  ``claimed`` → ``pending`` via ``POST /handoffs/{id}/release``) so the dispatcher
  re-picks it. Each release increments the row's ``retry_count``; once that count
  reaches ``WATCHDOG_MAX_RETRIES`` (default 3, env-overridable) the watchdog
  ESCALATES to the project's lead worker instead of requeuing again — so a
  permanently-failing run can never requeue-loop forever. The lead worker is
  RESOLVED from the running project's role policy (the registry interactive lead) —
  the harness names no worker (§ pure-runtime).
* Bounded auto-actions — the only automatic mutations are:
    1. ``complete`` a handoff whose worker logged SUCCESS but the row stayed
       claimed (safe: idempotent on Cortex's end).
    2. ``release`` (requeue) a stuck mid-run handoff back to ``pending`` so the
       dispatcher re-picks it — but ONLY while ``retry_count < WATCHDOG_MAX_RETRIES``
       (the release endpoint increments the count, so requeues are strictly bounded).
    3. ``escalate`` (create a handoff to the project's lead worker) for a stuck run
       AT/OVER the retry cap, or any orphaned/silent/timed-out run that can't be
       safely requeued.
    4. ``complete`` a stale ``[WATCHDOG-SIGNAL]`` escalation whose referenced run
       has since resolved (reconcile/clear-on-resolve — keeps queue clean).
  All are conservative. The watchdog never kills a run, and never requeues one past
  the retry cap (it escalates instead).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import time
from datetime import datetime
from typing import Any

from .domain import roles as _roles


# ---------------------------------------------------------------------------
#  Tunables — bounded env-overridable constants
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


# Claimed longer than this with no success marker → stuck.
WATCHDOG_STALE_S: float = _env_float("WATCHDOG_STALE_SECONDS", 900.0, 30.0, 7200.0)
# How often the heartbeat loop sweeps (seconds).
WATCHDOG_INTERVAL_S: float = _env_float("WATCHDOG_INTERVAL_SECONDS", 60.0, 10.0, 600.0)
# How many times a stuck handoff may be auto-REQUEUED (released → pending) before the
# watchdog gives up and ESCALATES to the lead instead of requeuing forever. Each
# release increments the row's ``retry_count`` (Cortex side), which is what this cap
# is compared against: ``retry_count < cap`` → requeue; ``>= cap`` → escalate. The
# orchestrator's orphan-reclaim path honors the SAME env var so no path requeues
# without a ceiling.
WATCHDOG_MAX_RETRIES: int = _env_int("WATCHDOG_MAX_RETRIES", 3, 0, 100)
# The escalation ROLE — the project's lead worker is who a stuck run escalates TO,
# and acts AS. The harness names NO worker (it is a pure runtime; "agents" are AI
# Workers owned by the running PROJECT — CTO 2026-06-18). So both the from-actor and
# the to-agent are RESOLVED per-scan from the project's role policy (the registry
# interactive lead). ``WATCHDOG_AGENT`` is an optional per-deployment OVERRIDE for the
# from-actor; empty (the default) means "derive from the project role policy". The
# role label itself is a config-general string, never a worker name.
ESCALATION_ROLE: str = (os.environ.get("WATCHDOG_ESCALATION_ROLE", "lead") or "lead").strip().lower()
WATCHDOG_AGENT: str = (os.environ.get("WATCHDOG_AGENT", "") or "").strip().lower()

# REQUEST-LIVED lease owners (Milestone 1 T10/T11): these runs execute IN-PROCESS in
# the console request that started them — Approve & Run (``approve_run``) and the
# interactive chat (``chat``). They have NO separate worker PID, so they NEVER write a
# heartbeat. Their TERMINAL STATUS (the store row reaching ok/error) is the completion
# signal — NOT heartbeat age. The watchdog must therefore NOT apply the
# heartbeat-staleness axis to them (a null/old heartbeat_at is expected, not death).
# A DETACHED worker (any other lease owner, e.g. ``worker``/``orchestrator``) DOES
# heartbeat, so it is judged on the heartbeat axis.
REQUEST_LIVED_LEASES: frozenset[str] = frozenset({"approve_run", "chat"})

# ---------------------------------------------------------------------------
#  Pure classifier — the heart of the watchdog (no I/O)
# ---------------------------------------------------------------------------

def _retry_count(handoff: dict) -> int:
    """The handoff row's ``retry_count`` (how many times it has been requeued so far),
    coerced to a non-negative int. The Cortex ``/handoffs?status=claimed`` list carries
    it (exposed by 68e55a0); a missing/garbage value reads as 0 so a fresh run always
    qualifies for its first requeue. Pure + total (never raises)."""
    try:
        n = int(handoff.get("retry_count") or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def classify_run(
    status: str,
    has_success_marker: bool,
    claimed_age_s: float | None,
    stale_threshold_s: float = WATCHDOG_STALE_S,
    *,
    heartbeat_age_s: float | None = None,
    lease_owner: str | None = None,
) -> str:
    """Deterministic verdict for one in-flight handoff.

    Returns one of:
      ``'completed'`` — already done; not our concern.
      ``'recover'``   — claimed but the worker logged its SUCCESS marker →
                        re-complete (a silent complete-failure left it claimed).
      ``'stuck'``     — claimed, no success marker, and provably stale → escalate.
      ``'healthy'``   — claimed recently / alive / age unknown → leave it.

    Staleness has TWO axes (Milestone 1 T11 adds the heartbeat axis):

    1. HEARTBEAT axis (preferred — REAL liveness) — for a DETACHED worker (a
       lease owner NOT in ``REQUEST_LIVED_LEASES``), the store row carries a
       ``heartbeat_at`` the worker bumps on a cadence; ``heartbeat_age_s`` is how
       long since that beat. A LIVE beat (age ≤ threshold) means the process is
       alive → ``healthy`` even if it was claimed long ago (live liveness beats the
       old inferred "claimed age" heuristic). A STALE beat (age > threshold) means
       the process is gone → ``stuck`` even if it was claimed only moments ago.

       REQUEST-LIVED EXEMPTION (load-bearing, per the T9/T10 notes): an in-process
       run (``approve_run`` / ``chat``) has no separate PID and NEVER heartbeats, so
       a null/old ``heartbeat_at`` is EXPECTED, not death. The heartbeat axis does
       NOT apply to it; its terminal status (the row reaching ok/error, surfaced via
       ``has_success_marker``) is the completion signal. A still-claimed request-lived
       run with no marker is ``healthy`` (in-flight), never ``stuck``-on-heartbeat.

    2. CLAIMED-AGE axis (fallback — inferred) — when we have NO heartbeat reading
       (``heartbeat_age_s is None``) for a non-request-lived run (the Cortex fallback,
       or a row the store never wrote), fall back to the original behavior: a claimed
       handoff with no marker, claimed longer than the threshold, is ``stuck``.

    Design notes (unchanged):
    - Recover takes priority over everything: if the marker is present, the worker
      finished successfully, so we just need to re-complete the row.
    - claimed_age_s / heartbeat_age_s of None means "no evidence of staleness" on
      that axis — we never call a run stuck without evidence.
    - Both threshold comparisons are strict (>) so a run exactly at the threshold is
      still considered in-progress.
    """
    s = (status or "").strip().lower()

    if s == "completed":
        return "completed"

    # Only claimed handoffs are in-flight runs we supervise.
    if s != "claimed":
        return "healthy"

    # Recover wins regardless of axis — the run finished, the row just stayed claimed.
    if has_success_marker:
        return "recover"

    is_request_lived = (lease_owner or "").strip().lower() in REQUEST_LIVED_LEASES

    # HEARTBEAT axis — only for DETACHED workers that actually heartbeat, and only
    # when we have a reading. A request-lived (in-process) run is EXEMPT: it never
    # heartbeats, so we never judge it on heartbeat age.
    if not is_request_lived and heartbeat_age_s is not None:
        if heartbeat_age_s > stale_threshold_s:
            return "stuck"   # dead: heartbeat went stale
        return "healthy"     # alive: a recent beat supersedes a stale claimed-age

    # REQUEST-LIVED with no marker → still in-flight (terminal status is the signal).
    # We must NOT fall through to the claimed-age axis for it (a long in-process run
    # is normal, not stuck).
    if is_request_lived:
        return "healthy"

    # CLAIMED-AGE fallback — no heartbeat reading for a detached run: the original
    # inferred-staleness behavior.
    if claimed_age_s is not None and claimed_age_s > stale_threshold_s:
        return "stuck"

    return "healthy"


# ---------------------------------------------------------------------------
#  Ops interface — duck-typed (no ABC) so tests inject FakeOps freely
# ---------------------------------------------------------------------------

class CortexWatchdogOps:
    """Project-scoped watchdog I/O over the shared Cortex HTTP client.

    Runtime supervision can target any registered project. Cortex's CLI isolation
    guard intentionally rejects cross-project shell contexts, so the watchdog uses
    the existing API client and explicit ``X-Project`` scoping throughout.
    """

    def __init__(self, project: str, client: object, agent: str = WATCHDOG_AGENT) -> None:
        self._project = project
        self._client = client
        # The from-actor OVERRIDE only (a per-deployment env, empty by default). The
        # actual escalation worker is resolved per-scan from the project role policy —
        # the harness hardcodes no worker name here (§ pure-runtime / zero AI Workers).
        self._agent = (agent or "").strip().lower()

    async def _pm_agent(self, project: str) -> str | None:
        """The project's lead worker name for escalation — resolved from role policy.

        Reads the project's live roster and picks its interactive lead
        worker) via the shared, project-general resolver ``domain.roles.interactive_lead``
        (the same one the dispatcher uses, so escalation and dispatch never name a
        different lead). Returns the lower-cased worker name, or None when the roster
        has no interactive lead / the lookup fails — the caller then falls back to the
        escalation ROLE only, never to a hardcoded worker. Best-effort: never raises."""
        try:
            agents = await self._client.get_agents(project)
        except Exception:
            return None
        lead = _roles.interactive_lead(agents or [])
        name = ((lead or {}).get("name") or "").strip().lower()
        return name or None

    async def get_handoffs(self, project: str) -> list[dict]:
        # The watchdog supervises IN-FLIGHT runs, which are CLAIMED handoffs. The
        # /handoffs list is pending-only by default (that's the orchestrator's
        # dispatch queue), so explicitly ask for claimed — otherwise the watchdog
        # sees nothing to supervise and silently no-ops.
        return await self._client.get_handoffs(project, status="claimed")

    async def get_open_escalations(self, project: str) -> list[dict]:
        """Return all open (pending) handoffs that carry ``[WATCHDOG-SIGNAL]``.

        Used for:
        * Durable dedup — skip filing if one already exists for the same run.
        * Reconcile — complete stale escalations whose referenced run resolved.

        The client's ``get_handoffs`` without a status arg returns the pending
        queue (the default). We filter client-side for the marker so we don't
        need an extra API surface.
        """
        try:
            all_pending = await self._client.get_handoffs(project)
        except Exception:
            return []
        return [
            h for h in all_pending
            if "[WATCHDOG-SIGNAL]" in (h.get("summary") or "").upper()
        ]

    async def has_success_marker(self, project: str, handoff_id: str) -> bool:
        needle = f"COMPLETED {handoff_id}"
        try:
            results = await self._client.search(
                project, needle, limit=12, rerank=False
            )
        except Exception:
            return False
        return any(
            needle
            in " ".join(
                str(row.get(key, "")) for key in ("summary", "text", "content")
            )
            for row in (results or [])
        )

    async def claimed_age_seconds(self, project: str, handoff_id: str) -> float | None:
        try:
            handoff = await self._client.get_handoff(project, handoff_id)
        except Exception:
            return None
        if not handoff:
            return None
        return _age_seconds_from_iso(
            handoff.get("claimed_at") or handoff.get("created_at")
        )

    async def complete(self, project: str, handoff_id: str) -> bool:
        return await self._client.complete_handoff(project, handoff_id)

    async def release(self, project: str, handoff_id: str, reason: str = "") -> bool:
        return await self._client.release_handoff(
            project, handoff_id, reason=reason
        )

    async def escalate(self, project: str, handoff: dict, reason: str) -> bool:
        hid = handoff.get("id", "?")
        summary = (
            f"[WATCHDOG-SIGNAL] stuck run {hid[:8]}: {reason} — "
            f"Lead assess + decide (retry/reassign/escalate)"
        )
        # Resolve this project's lead worker from its role policy (never hardcoded).
        # The escalation is addressed to it and filed as it. Fallbacks, in order:
        # deployment actor override -> resolved lead -> role label.
        pm_agent = await self._pm_agent(project)
        from_actor = self._agent or pm_agent or ESCALATION_ROLE
        # Address the escalation to the resolved lead worker when known; otherwise to the
        # ROLE alone (`--to pm`) so the dispatcher routes it to the project's lead.
        to_agent = pm_agent

        result = await self._client.create_handoff(
            project,
            from_actor,
            {
                "from_role": ESCALATION_ROLE,
                "to_role": ESCALATION_ROLE,
                "to_agent": to_agent,
                "summary": summary,
                "priority": "high",
            },
        )
        return isinstance(result, dict) and bool(
            result.get("id") or result.get("handoff_id") or result.get("ok")
        )


# ---------------------------------------------------------------------------
#  Store-backed ops — OBSERVATION reads the RunState SSOT store (Milestone 1 T11)
# ---------------------------------------------------------------------------

def _age_seconds_from_iso(ts: Any) -> float | None:
    """Elapsed seconds since an ISO-8601 timestamp (the store's ``heartbeat_at`` /
    ``started_at`` strings), or None if it can't be parsed. Tolerates a trailing
    ``+00`` (Postgres short tz) and a naive (tz-less) stamp (assumed UTC)."""
    if ts is None:
        return None
    if not isinstance(ts, str):
        # A datetime (or anything with .timestamp()) — best-effort.
        try:
            return time.time() - ts.timestamp()
        except Exception:
            return None
    s = ts.strip()
    if not s:
        return None
    # Normalize "…+00" → "…+00:00" so datetime.fromisoformat accepts it.
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive stamp → treat as UTC (the store writes UTC timestamptz).
        from datetime import timezone as _tz
        dt = dt.replace(tzinfo=_tz.utc)
    return time.time() - dt.timestamp()


class StoreWatchdogOps:
    """Observation-reads-the-store ops adapter (Milestone 1 T11).

    The watchdog used to GREP Cortex CLI text to GUESS liveness (``has_success_marker``
    ran ``cortex-search "COMPLETED <id>"``; ``claimed_age_seconds`` parsed the
    ``"Claimed at:"`` line). Now it reads the durable RunState SSOT store — REAL
    signals the worker / Approve&Run / chat write:

      * ``has_success_marker`` → ``store.by_handoff(hid).status == "ok"``;
      * ``claimed_age_seconds`` → age of the row's ``heartbeat_at`` (fallback
        ``started_at``);
      * ``heartbeat_age_seconds`` / ``lease_owner`` → the NEW signals the
        heartbeat-staleness classifier axis needs (a request-lived in-process run
        never heartbeats, so the classifier exempts it).

    Cortex handoff reads/mutations delegate to ``CortexWatchdogOps``. The store also
    owns stale active-run reconciliation: a detached run whose heartbeat is past the
    watchdog threshold is terminalized so it cannot remain "live" after its process
    has exited or its handoff has already completed.

    GRACEFUL-DEGRADE (house law): the store is an OPTIONAL signal source. A store that
    returns None (no row / run predates the store / row pruned) or RAISES (app-DB down)
    FALLS BACK to the injected Cortex ops for the marker + age, so a dead app-DB can
    never blind or crash the supervisor.
    """

    def __init__(self, store: object, cortex_ops: object, *, project: str = "") -> None:
        self._store = store
        self._cortex = cortex_ops
        self._project = project

    async def _row(self, handoff_id: str) -> Any | None:
        """The latest store run row for a handoff (or None / on any failure). The
        store's ``by_handoff`` already graceful-degrades, but we guard anyway so a
        store that's None or raises never escapes."""
        store = self._store
        if store is None:
            return None
        try:
            return await store.by_handoff(handoff_id)
        except Exception:
            return None

    async def reconcile_stale_runs(
        self, project: str, stale_threshold_s: float
    ) -> int:
        """Terminalize provably stale detached rows in the run-state read model."""
        store = self._store
        if store is None:
            return 0
        try:
            rows = await store.list_active(project)
        except Exception:
            return 0

        reconciled = 0
        for row in rows or []:
            status = (getattr(row, "status", "") or "").strip().lower()
            lease_owner = (getattr(row, "lease_owner", "") or "").strip().lower()
            if status not in {"queued", "running"} or lease_owner in REQUEST_LIVED_LEASES:
                continue
            age = _age_seconds_from_iso(getattr(row, "heartbeat_at", None))
            if age is None:
                age = _age_seconds_from_iso(getattr(row, "started_at", None))
            if age is None or age <= stale_threshold_s:
                continue
            run_id = (getattr(row, "run_id", "") or "").strip()
            if not run_id:
                continue
            try:
                await store.set_status(
                    run_id,
                    "error",
                    error=f"watchdog reconciled stale detached run after {age:.0f}s without heartbeat",
                )
                reconciled += 1
            except Exception:
                continue
        return reconciled

    # -- delegated Cortex handoff operations -----------------------------------

    async def get_handoffs(self, project: str) -> list[dict]:
        return await self._cortex.get_handoffs(project)

    async def get_open_escalations(self, project: str) -> list[dict]:
        return await self._cortex.get_open_escalations(project)

    async def complete(self, project: str, handoff_id: str) -> bool | None:
        return await self._cortex.complete(project, handoff_id)

    async def release(self, project: str, handoff_id: str, reason: str = "") -> bool | None:
        return await self._cortex.release(project, handoff_id, reason)

    async def escalate(self, project: str, handoff: dict, reason: str) -> bool | None:
        return await self._cortex.escalate(project, handoff, reason)

    # -- observation reads the store (with Cortex API fallback) -----------------

    async def has_success_marker(self, project: str, handoff_id: str) -> bool:
        """The run's store row reaching ``status == "ok"`` IS the success marker —
        the durable terminal status the worker / Approve&Run / chat write. A store
        MISS (None row) or a down store falls back to Cortex search."""
        row = await self._row(handoff_id)
        if row is not None:
            return (getattr(row, "status", "") or "").strip().lower() == "ok"
        return await self._cortex.has_success_marker(project, handoff_id)

    async def claimed_age_seconds(self, project: str, handoff_id: str) -> float | None:
        """Liveness age from the store row's ``heartbeat_at`` (the live stamp), falling
        back to ``started_at`` when the run hasn't beaten yet. A store MISS / down
        store falls back to the handoff API's ``claimed_at`` metadata. None when nothing is
        readable (the classifier treats unknown age as healthy)."""
        row = await self._row(handoff_id)
        if row is not None:
            age = _age_seconds_from_iso(getattr(row, "heartbeat_at", None))
            if age is None:
                age = _age_seconds_from_iso(getattr(row, "started_at", None))
            if age is not None:
                return age
            # Row exists but carries no usable stamp → fall through to Cortex as a last
            # resort (still better than blindly None).
        return await self._cortex.claimed_age_seconds(project, handoff_id)

    async def heartbeat_age_seconds(self, project: str, handoff_id: str) -> float | None:
        """Seconds since the store row's ``heartbeat_at`` (the REAL liveness signal),
        or None when the run never heartbeats (an in-process request-lived run) / no
        row / down store. Feeds the classifier's heartbeat-staleness axis."""
        row = await self._row(handoff_id)
        if row is None:
            return None
        return _age_seconds_from_iso(getattr(row, "heartbeat_at", None))

    async def lease_owner(self, project: str, handoff_id: str) -> str | None:
        """The store row's ``lease_owner`` (e.g. ``approve_run`` / ``chat`` =
        request-lived in-process; anything else = detached worker), or None when no
        row / down store. Tells the classifier whether the heartbeat axis applies."""
        row = await self._row(handoff_id)
        if row is None:
            return None
        return getattr(row, "lease_owner", None)


# ---------------------------------------------------------------------------
#  Watchdog — the supervision loop
# ---------------------------------------------------------------------------

class Watchdog:
    """Failure-supervisor. One instance per app, driven by the console lifespan.

    The watchdog ONLY acts on ``claimed`` handoffs. For each:
      * ``recover``  → call ``ops.complete`` (re-complete a silent-complete-failure).
      * ``stuck``    → if ``retry_count < max_retries`` REQUEUE it (``ops.release`` →
                       back to pending for the dispatcher); else ESCALATE once
                       (``ops.escalate``, deduped durably + in-memory). The release
                       endpoint increments ``retry_count``, so each requeue moves the
                       run one step closer to the cap — it can never loop forever.
      * ``healthy``  → no action.
      * ``completed``/anything else → skip.

    Safety properties:
    - Bounded automatic actions: ``complete`` (idempotent), ``release`` (requeue,
      strictly capped by ``retry_count``), ``escalate`` (creates a lead handoff — no
      mutations to the stuck run itself), and auto-completing stale
      ``[WATCHDOG-SIGNAL]`` escalations (reconcile).
    - Durable escalation dedup (survives restarts): before filing a
      ``[WATCHDOG-SIGNAL]`` for run X, the watchdog checks live open escalations
      for an existing one that already references X. ``_escalated`` is kept as a
      fast-path that avoids the live check within a single process session.
    - Clear-on-resolve (reconcile): each scan starts by listing all open
      ``[WATCHDOG-SIGNAL]`` escalations and auto-completing any whose referenced
      run is no longer stuck/claimed (resolved, abandoned, completed, or missing).
    - ``run_forever`` never raises out of the loop — the scan is wrapped in
        ``contextlib.suppress(Exception)`` so a transient Cortex outage
      degrades gracefully to a missed scan, not a crash.
    """

    # Marker embedded in every watchdog escalation summary so they can be
    # identified and deduped / reconciled against live Cortex state.
    ESCALATION_MARKER: str = "[WATCHDOG-SIGNAL]"

    def __init__(
        self,
        ops: object,
        *,
        stale_threshold_s: float = WATCHDOG_STALE_S,
        max_retries: int = WATCHDOG_MAX_RETRIES,
    ) -> None:
        self._ops = ops
        self._stale = stale_threshold_s
        # Requeue ceiling: a stuck handoff is RELEASED (requeued) while its
        # ``retry_count`` is below this; at/over it the watchdog escalates instead
        # (never a requeue-loop forever). Clamped non-negative so a 0 cap means
        # "never requeue — escalate on the first stuck detection".
        self._max_retries = max(0, int(max_retries))
        # In-memory fast-path dedup: handoff IDs already escalated this session.
        # Kept so we don't do the live get_open_escalations check every scan for
        # runs we already escalated in this process lifetime. The durable dedup
        # (via live Cortex check) is the source of truth across restarts.
        self._escalated: set[str] = set()

    @staticmethod
    def _extract_referenced_id(summary: str) -> str | None:
        """Extract the 8-char stuck-run ID from a ``[WATCHDOG-SIGNAL]`` summary.

        The escalate() method writes:
          ``[WATCHDOG-SIGNAL] stuck run <hid[:8]>: <reason>``
        Returns the 8-char prefix, or None if parsing fails.
        """
        marker = "stuck run "
        upper = summary.lower()
        idx = upper.find(marker)
        if idx == -1:
            return None
        fragment = summary[idx + len(marker):]
        # The 8-char id ends at the next ":" or whitespace.
        end = len(fragment)
        for sep in (":", " "):
            pos = fragment.find(sep)
            if pos != -1:
                end = min(end, pos)
        candidate = fragment[:end].strip()
        # Must be exactly 8 hex chars.
        if len(candidate) == 8 and all(c in "0123456789abcdefABCDEF-" for c in candidate):
            return candidate
        return None

    async def reconcile_escalations(self, project: str, claimed_hids: set[str]) -> int:
        """Auto-complete stale ``[WATCHDOG-SIGNAL]`` escalations.

        For each open ``[WATCHDOG-SIGNAL]`` escalation, extract the 8-char
        referenced stuck-run ID. If that run is no longer in the claimed set
        (resolved, completed, abandoned, or simply gone), the escalation is
        stale noise — complete it to keep the lead queue clean.

        Returns the number of escalations auto-completed.
        """
        try:
            open_escalations = await self._ops.get_open_escalations(project)
        except Exception:
            return 0

        completed = 0
        for esc in open_escalations:
            esc_id = esc.get("id")
            if not esc_id:
                continue
            summary = esc.get("summary") or ""
            ref_id = self._extract_referenced_id(summary)
            if ref_id is None:
                continue
            # Check if the referenced run is still in the current claimed set.
            # claimed_hids contains the SHORT (8-char) id of every still-claimed run.
            still_stuck = any(hid.startswith(ref_id) or hid[:8] == ref_id for hid in claimed_hids)
            if not still_stuck:
                try:
                    result = await self._ops.complete(project, esc_id)
                    if result is not False:
                        completed += 1
                except Exception:
                    pass
        return completed

    async def _optional_signal(self, method: str, project: str, hid: str):
        """Read an OPTIONAL observation signal (``heartbeat_age_seconds`` /
        ``lease_owner``, the T11 store-backed additions) from the ops adapter.

        The base ``CortexWatchdogOps`` does not implement these — only
        ``StoreWatchdogOps`` does. So we duck-type: a missing method (or any failure)
        returns None, and ``classify_run`` falls back to the original claimed-age axis.
        This keeps the base ops and every existing FakeOps test working unchanged."""
        fn = getattr(self._ops, method, None)
        if fn is None:
            return None
        try:
            return await fn(project, hid)
        except Exception:
            return None

    @staticmethod
    def _stuck_reason(claimed_age_s: float | None, heartbeat_age_s: float | None) -> str:
        """Human reason for a stuck-run escalation, robust to either staleness axis.

        Prefers the heartbeat signal (real liveness) when present; otherwise reports
        the claimed age. Both can be None (e.g. a detached run flagged on a stale
        heartbeat we then couldn't re-read) — never format None with ``%f``."""
        if heartbeat_age_s is not None:
            return f"no heartbeat for {heartbeat_age_s:.0f}s, no success marker"
        if claimed_age_s is not None:
            return f"claimed {claimed_age_s:.0f}s, no success marker"
        return "stale run, no success marker"

    async def scan_once(self, project: str) -> dict:
        """One supervision pass over a project's CLAIMED handoffs.

        Steps:
          1. Fetch all claimed handoffs.
          2. Reconcile open ``[WATCHDOG-SIGNAL]`` escalations (clear-on-resolve).
          3. For each claimed handoff: classify and act (recover / stuck / healthy).
             * Durable dedup for ``stuck``: skip if a live escalation already
               references this run (even after a process restart).

        Returns a counts dict:
          ``scanned``    — number of claimed handoffs inspected.
          ``recovered``  — complete() calls made.
          ``requeued``   — release() calls made (stuck + under the retry cap).
          ``escalated``  — new escalate() calls made (stuck + at/over the cap; deduped).
          ``healthy``    — claimed + still working (no action taken).
          ``reconciled`` — stale escalations auto-completed.
          ``runs_reconciled`` — stale detached run-state rows terminalized.
        """
        counts = {
            "healthy": 0,
            "recovered": 0,
            "requeued": 0,
            "escalated": 0,
            "scanned": 0,
            "reconciled": 0,
            "runs_reconciled": 0,
        }

        handoffs = await self._ops.get_handoffs(project)

        # Build a set of the short (8-char) ids of all currently claimed runs
        # for the reconcile step.
        claimed_hids: set[str] = {
            (h.get("id") or "")
            for h in handoffs
            if (h.get("status") or "").strip().lower() == "claimed"
        }

        reconcile_runs = getattr(self._ops, "reconcile_stale_runs", None)
        if callable(reconcile_runs):
            try:
                counts["runs_reconciled"] = await reconcile_runs(project, self._stale)
            except Exception:
                counts["runs_reconciled"] = 0

        # Step 2: reconcile — complete stale escalations whose run resolved.
        counts["reconciled"] = await self.reconcile_escalations(project, claimed_hids)

        # Step 3: fetch live open escalations for durable dedup. Reuse the data
        # already cached in reconcile where possible — but reconcile may not have
        # been called (e.g. get_open_escalations raised), so fetch fresh here.
        try:
            open_escalations = await self._ops.get_open_escalations(project)
        except Exception:
            open_escalations = []

        # Build a set of the 8-char run IDs that already have a live escalation.
        # This is the durable dedup source-of-truth (survives restarts).
        already_escalated_live: set[str] = set()
        for esc in open_escalations:
            ref = self._extract_referenced_id(esc.get("summary") or "")
            if ref:
                already_escalated_live.add(ref)

        for h in handoffs:
            if (h.get("status") or "").strip().lower() != "claimed":
                continue

            counts["scanned"] += 1
            hid = h.get("id")
            marker = await self._ops.has_success_marker(project, hid)
            age = await self._ops.claimed_age_seconds(project, hid)
            # NEW T11 signals (optional on the ops adapter): the store-backed ops
            # expose heartbeat age + lease owner; the base Cortex ops do NOT, so read
            # them defensively (absent → None) and the classifier falls back to the
            # original claimed-age axis. This keeps CortexWatchdogOps + the existing
            # FakeOps tests working untouched while the store ops add real liveness.
            heartbeat_age = await self._optional_signal("heartbeat_age_seconds", project, hid)
            lease_owner = await self._optional_signal("lease_owner", project, hid)
            verdict = classify_run(
                h.get("status"), marker, age, self._stale,
                heartbeat_age_s=heartbeat_age, lease_owner=lease_owner,
            )

            if verdict == "recover":
                result = await self._ops.complete(project, hid)
                if result is not False:
                    counts["recovered"] += 1

            elif verdict == "stuck":
                reason = self._stuck_reason(age, heartbeat_age)
                retry_count = _retry_count(h)
                if retry_count < self._max_retries:
                    # UNDER the cap → REQUEUE: release the stuck run back to pending so
                    # the dispatcher re-picks it. The release endpoint increments
                    # retry_count, so the NEXT time this run gets stuck it is one step
                    # closer to the cap — bounded, never a requeue-loop forever. Requeue
                    # is NOT deduped: a released row leaves the claimed set immediately,
                    # so it can't be re-released in the same or next sweep until it is
                    # re-dispatched, re-claimed, and stuck again (with a higher count).
                    result = await self._ops.release(project, hid, reason)
                    if result is not False:
                        counts["requeued"] += 1
                else:
                    # AT/OVER the cap → ESCALATE to the lead instead of requeuing again.
                    short_id = (hid or "")[:8]
                    # Fast-path: in-memory check (avoids re-checking live Cortex within
                    # a single process session for runs we already escalated).
                    in_memory = hid in self._escalated
                    # Durable check: live Cortex (survives restarts — source of truth).
                    live_exists = short_id in already_escalated_live
                    if not in_memory and not live_exists:
                        result = await self._ops.escalate(project, h, reason)
                        if result is not False:
                            self._escalated.add(hid)
                            already_escalated_live.add(short_id)
                            counts["escalated"] += 1
                    # Already escalated (in-memory or live): skip, don't increment.

            else:
                # "healthy" (or any unexpected verdict) — leave it alone
                counts["healthy"] += 1

        return counts

    async def run_forever(
        self,
        projects_fn: object,
        *,
        interval_s: float = WATCHDOG_INTERVAL_S,
        stop: "asyncio.Event | None" = None,
    ) -> None:
        """Heartbeat loop: every ``interval_s``, scan each autonomous project.

        ``projects_fn()`` is called each iteration to get the current list of
        autonomous project keys — so a project toggled OFF mid-session is
        skipped on the next sweep without restarting the loop. It may be sync OR
        return an awaitable (which is awaited): the console passes the
        orchestrator's OFF-loop, serialized autonomous-projects reader so the
        watchdog never races the orchestrator on the sync settings DB connection.

        ``stop`` is an optional ``asyncio.Event``; when set the loop exits after
        the current sweep completes. When None the loop runs indefinitely.

        This method NEVER raises out of the loop. Every scan is wrapped in
        ``contextlib.suppress(Exception)`` — a Cortex outage or CLI failure
        degrades to a missed sweep, not a crash.
        """
        while stop is None or not stop.is_set():
            with contextlib.suppress(Exception):
                projects = projects_fn()
                if inspect.isawaitable(projects):
                    projects = await projects
                for project in (projects or []):
                    await self.scan_once(project)

            # Sleep for interval_s, but wake early if the stop event fires.
            try:
                if stop is not None:
                    await asyncio.wait_for(stop.wait(), timeout=interval_s)
                else:
                    await asyncio.sleep(interval_s)
            except (asyncio.TimeoutError, Exception):
                pass
