"""Autonomous Dispatch scheduler — legacy module name ``app.orchestrator`` (E007).

THE MODEL (CTO-confirmed, AGENT_PERSONAS_AND_ORG_MODEL.md §4 + §5.1)
-------------------------------------------------------------------
Dispatch is deterministic code, not an agent/LLM persona. Its loop: a handoff is
created → an event or heartbeat wakes the scheduler → Dispatch verifies the row is
ready → it spawns the target worker as an isolated ``run-agent`` process. A
project may optionally configure a lead/PM beat script to decompose and
prioritize work, but this scheduler itself names no project or worker. Escalation
to the lead remains outside this scheduler: when a worker errors/blocks, this file
logs the outcome and leaves judgment to the Watchdog + configured lead path.

CONSEQUENTIAL — so it ships DARK and SAFE
-----------------------------------------
This module auto-runs agents on the user's subscription. The hard safeguards are
enforced HERE and are non-negotiable:

  * OFF BY DEFAULT for every project. The loop reconciles the ON set from the
    app-DB ``project_autonomy`` table (``settings.autonomous_projects()``). No row
    is ever seeded, and a degraded app-DB reads as EMPTY → the loop idles. When
    ALL projects are OFF (the default) the loop spawns NOTHING: it is a clean
    no-op that watches nobody.
  * KILL-SWITCH. Flipping a project OFF stops new dispatches for it immediately
    (the next reconcile drops it; in-flight runs are not force-killed but no new
    ones start). Flipping the loop OFF everywhere returns it to the idle no-op.
  * IDEMPOTENCY. A handoff is dispatched AT MOST once: this process keeps a
    per-(project,id) seen-set, while the spawned worker performs the durable atomic
    Cortex claim before doing work. A second console / restart cannot run the same
    handoff twice.
  * CONCURRENCY CAP. At most ``MAX_CONCURRENT`` (default 3) auto-runs per project
    are in flight; over the cap, the handoff is deferred (re-seen on the next
    sweep, not dropped).
  * NEVER CRASH THE CONSOLE. Every iteration is wrapped; a Cortex/app-DB/runner
    failure is caught + logged and the loop continues. The legacy kickoff-framing
    hook is best-effort and NEVER blocks a dispatch.

ARCHITECTURE — event-driven, with a poll fallback (BOTH, by design)
-------------------------------------------------------------------
Cortex ``GET /events`` is the primary trigger: it parks on the Postgres
``cortex_events`` NOTIFY condition and pushes a frame the instant a row lands
(zero cost while idle). A handoff create emits a ``handoff_created`` event whose
``detail`` JSON (schema ``cortex.handoff_lifecycle.v1``) carries the full target
(``to_agent``/``to_role``), ``status``, ``priority`` and ``handoff_id`` — enough to
dispatch directly off the frame. We consume that stream per ON-project.

We ALSO run a short reconcile/sweep poll (~``POLL_INTERVAL`` s) as a robust
fallback: it (a) re-reads which projects are ON (so a toggle flip is honoured even
if no event fires), (b) opens/closes per-project event watchers to match, and
(c) re-scans ``/handoffs`` for any pending row the event stream might have missed
(a missed NOTIFY, a row created before the watcher connected, an SSE reconnect
gap). Event-driven keeps latency ~instant and cost ~zero; the poll guarantees we
never strand a pending handoff. Idempotency makes the overlap safe — a handoff
seen by both paths is dispatched once.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import automation_feed
from . import settings as settings_store
from .dispatch.command import DispatchWorkerSpec, dispatch_worker, terminalize_unstarted_run
from .domain import designation as _designation
from .domain import roles as _roles
from .harness_runner import _apply_project_workspace

log = logging.getLogger("console.orchestrator")


# ---------------------------------------------------------------------------
#  Off-loop settings reads — SERIALIZED.
#
#  `settings_store.*` is a SYNCHRONOUS API backed by psycopg2 (app/appdb.py →
#  SettingsDB). Two layered hazards, both surfacing as the autonomous-dispatch
#  stall ("pi shows 1 tool segment, then freezes, then 'did not respond within
#  120s — turn aborted'", with the file never written):
#
#   1. ON-LOOP BLOCKING. A sync psycopg2 round-trip on the event loop blocks it
#      for the call's duration. While blocked the loop can't run the subprocess
#      stdout-reader callback, so the child's OS pipe fills, the child blocks on
#      write right after its first tool frame, and the turn cap aborts it. The
#      prior fix moved the *in-module* `settings_store.*` reads off-loop via
#      asyncio.to_thread — but MISSED the injected `chat_routing_for`, which also
#      does a sync `get_agent_override` read on the dispatch hot path (start of
#      every server-side run, while the subprocess streams). `_chat_routing_async`
#      closes that gap.
#
#   2. CONCURRENT USE OF ONE psycopg2 CONNECTION (the residual stall). SettingsDB
#      holds a SINGLE shared psycopg2 connection with NO lock. The loop issues
#      these reads from MANY places that run as independent tasks: `_watch_project`
#      on every SSE chunk, `_reconcile_once`/`_scan_pending` on every poll, and
#      each `_dispatch_run`. Routed through asyncio.to_thread, those land on
#      DIFFERENT worker threads — so two reads can hit the same connection at once.
#      A psycopg2 connection is NOT safe for concurrent use: overlapping queries
#      desync the libpq protocol and a thread can block in recv() indefinitely
#      waiting for a reply another thread consumed. That hung worker (and the
#      corrupted connection behind it) is the 120s "freeze" — it only shows under
#      the live server where SSE + poll + dispatch genuinely overlap, never in a
#      single-threaded direct run.
#
#  Fix: a single module-level asyncio.Lock SERIALIZES every loop settings read, so
#  the shared connection is only ever touched by one worker thread at a time (off
#  the loop, never concurrently). These four helpers are the SINGLE funnel the
#  loop's hot path uses; the lock makes the funnel both non-blocking AND race-free.
#  Reads are sub-millisecond, so serializing them costs nothing measurable.
# ---------------------------------------------------------------------------

# Serializes the loop's sync settings reads (see above). Module-level so every
# Dispatch scheduler task shares it — the shared psycopg2 connection demands it.
_settings_lock = asyncio.Lock()


async def _autonomous_projects_async() -> list[str]:
    """`settings_store.autonomous_projects()` OFF the loop AND serialized (the
    shared psycopg2 connection must never be used by two worker threads at once)."""
    async with _settings_lock:
        return await asyncio.to_thread(settings_store.autonomous_projects)


async def _agent_designation_async(project_key: str, agent_name: str) -> str:
    """`settings_store.get_agent_designation()` OFF the loop AND serialized."""
    async with _settings_lock:
        return await asyncio.to_thread(
            settings_store.get_agent_designation, project_key, agent_name
        )


async def _agent_role_aliases_async(project_key: str, agent_name: str) -> str:
    """Read an agent's configured role aliases OFF the loop AND serialized."""
    async with _settings_lock:
        override = await asyncio.to_thread(
            settings_store.get_agent_override, project_key, agent_name
        )
        return str((override or {}).get("role_aliases") or "")


async def _agent_auto_dispatch_async(project_key: str, agent_name: str) -> str:
    """Configured auto-dispatch tri-state for an agent: "true", "false", or ""."""
    async with _settings_lock:
        override = await asyncio.to_thread(
            settings_store.get_agent_override, project_key, agent_name
        )
        return _designation.normalize_boolish((override or {}).get("auto_dispatch"))


async def _chat_routing_async(
    chat_routing_for: Callable[[dict, str], tuple[str, str | None, str | None]],
    agent: dict,
    project_key: str,
) -> tuple[str, str | None, str | None]:
    """Resolve an agent's (harness, model, reasoning) OFF the loop AND serialized.

    The injected ``chat_routing_for`` (main._chat_routing_for) reads the per-agent
    console override via ``settings_store.get_agent_override`` — the SAME sync
    psycopg2 read the helpers above guard, on the dispatch hot path (start of every
    server-side run, while a harness subprocess streams). Hazard #1 (on-loop block)
    AND hazard #2 (concurrent connection use) both apply, so it goes through the
    same off-loop + serialized funnel."""
    async with _settings_lock:
        return await asyncio.to_thread(chat_routing_for, agent, project_key)


async def _is_propose_mode_async(project_key: str) -> bool:
    """The AUTONOMOUS propose-mode gate, OFF the loop AND serialized (same psycopg2
    connection safety contract as the other helpers above). Uses the FAIL-CLOSED
    `is_propose_mode_gate`: an UNREADABLE propose state (app-DB down) GATES the spawn
    (require approval) rather than auto-spawning unapproved work on a DB hiccup."""
    async with _settings_lock:
        return await asyncio.to_thread(settings_store.is_propose_mode_gate, project_key)


async def _set_awaiting_approval_async(project_key: str, handoff_id: str) -> None:
    """`settings_store.set_approval_status(..., 'awaiting')` OFF the loop AND
    serialized. Best-effort: a write failure is logged but never raises into
    the dispatch loop (the gate must never crash the loop — if the record can't
    be written, handoff stays None in the DB and re-evaluates next sweep)."""
    async with _settings_lock:
        await asyncio.to_thread(
            settings_store.set_approval_status, project_key, handoff_id, "awaiting"
        )


async def _approval_status_async(project_key: str, handoff_id: str) -> str | None:
    """`settings_store.get_approval_status()` OFF the loop AND serialized.
    Returns 'awaiting', 'approved', or None (no row / DB unavailable).
    None means the gate should write 'awaiting' on the next sweep."""
    async with _settings_lock:
        return await asyncio.to_thread(
            settings_store.get_approval_status, project_key, handoff_id
        )


# ---------------------------------------------------------------------------
#  Tunables (env-overridable; safe defaults). All bounded so a bad env value
#  can't make the loop hot-spin or remove a safeguard.
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a truthy env flag (1/true/yes) with a safe default."""
    val = (os.environ.get(name) or "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


# Per-project ceiling on simultaneous auto-runs (the concurrency cap safeguard).
MAX_CONCURRENT = _env_int("ORCH_MAX_CONCURRENT", 3, 1, 20)
# Reconcile/sweep cadence (the poll-fallback + toggle re-check). Short enough to
# feel live if an event is ever missed, long enough to cost ~nothing.
POLL_INTERVAL = _env_float("ORCH_POLL_INTERVAL", 4.0, 1.0, 60.0)
# Global emergency override: by default Dispatch does not auto-spawn an
# interactive-designated agent unless that agent has explicit per-agent
# auto_dispatch enabled. Setting ORCH_DISPATCH_INTERACTIVE=1 bypasses the guard
# for compatibility/debug only; normal projects should use the per-agent setting.
DISPATCH_INTERACTIVE = _env_bool("ORCH_DISPATCH_INTERACTIVE", False)
# Actor label for deterministic Dispatch telemetry. The legacy `ORCH_AGENT` env
# name is still accepted for compatibility, but the default actor is Dispatch code,
# not a roster persona.
DISPATCH_ACTOR = (
    os.environ.get("DISPATCH_ACTOR")
    or os.environ.get("ORCH_AGENT")
    or "dispatch"
).strip().lower() or "dispatch"
ORCH_AGENT = DISPATCH_ACTOR  # compatibility alias for older callers/tests.
# The spawnable worker unit (E007 Autonomy v2). Dispatch SPAWNS this as its OWN OS
# process per dispatch and only AWAITS its exit code — it never hosts the harness
# stream itself (hosting the child's stdout inline, with sync DB reads interleaved
# on the same loop, was the v1 dispatch stall). Absolute path derived from this
# file so it resolves regardless of the process CWD; override with ORCH_RUN_AGENT
# for tests / packaging.
RUN_AGENT_SCRIPT = os.environ.get(
    "ORCH_RUN_AGENT",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "run-agent"
    ),
)
# Hard ceiling on a single spawned run (belt-and-braces — the worker + harness have
# their own timeouts, and the separate Watchdog + lease catch stuck runs). On
# expiry Dispatch kills the child and marks the run failed for the Watchdog path.
RUN_TIMEOUT_S = _env_float("ORCH_RUN_TIMEOUT", 900.0, 30.0, 3600.0)
# Cap on the in-memory activity ring buffer surfaced in the Dispatch view.
ACTIVITY_MAX = _env_int("ORCH_ACTIVITY_MAX", 50, 10, 500)
# Run-state store bounds (the "watch the crew work" view, now backed by the durable
# RunState SSOT store — the in-memory transcript store was removed at T12). These
# stay as the canonical caps the store layer mirrors:
#   * TRANSCRIPT_MAX_RUNS   — recent-runs rail cap PER PROJECT (the read path's
#                             ``store.recent(limit=…)`` window; see main._CREW_RUNS_MAX).
#   * TRANSCRIPT_MAX_BYTES  — per-run total-chars cap (the SQL twin in
#                             adapters/runstate_pg enforces it so one run can't grow
#                             unbounded).
TRANSCRIPT_MAX_RUNS = _env_int("ORCH_TRANSCRIPT_MAX_RUNS", 20, 2, 100)
TRANSCRIPT_MAX_BYTES = _env_int("ORCH_TRANSCRIPT_MAX_BYTES", 32 * 1024, 4 * 1024, 256 * 1024)
# Legacy PM beat (pre-scheduler compatibility) — optional lead/PM assessment
# spawned after each worker completion and on the poll path. The productized PM
# planning beat is now a scheduled handoff in the app-DB. This subprocess seam is
# disabled unless ORCH_PM_BEAT_SCRIPT is explicitly set.
#
# CANONICAL PATH (E007 AV-5, hybrid PM planner): the scheduled `pm-planning-beat`
# job (automation_feed.pm_planning_schedule_payload) emits an epic-decomposition
# handoff to the resolved PM/lead, which the SAME dispatch funnel spawns through
# `run-agent` under every gate (propose_mode, auto_dispatch, wave, cap,
# sole-claimer). That replaces this seam. Keep ORCH_PM_BEAT_SCRIPT empty in
# normal operation so there is exactly ONE planner and no double-planning; this
# Popen hook is compat-only for deployments that still pin a script launcher.
# * PM_BEAT_SCRIPT  — project/deployment supplied one-shot launcher.
# * PM_BEAT_MIN_INTERVAL_S — minimum gap between poll-triggered beats per project;
#   a completion-triggered beat is never rate-limited (an event happened). Default
#   300s (5 min) keeps the poll path from spamming beats on every 4s sweep.
# * PM_BEAT_TIMEOUT_S — hard ceiling on a single PM-beat subprocess (assessment +
#   one handoff create, not a long worker run; 300s is generous).
PM_BEAT_SCRIPT = os.environ.get("ORCH_PM_BEAT_SCRIPT", "").strip()
PM_BEAT_MIN_INTERVAL_S = _env_float("ORCH_PM_BEAT_MIN_INTERVAL", 300.0, 30.0, 3600.0)
PM_BEAT_TIMEOUT_S = _env_float("ORCH_PM_BEAT_TIMEOUT", 300.0, 30.0, 900.0)
# RECLAIM ORPHANED CLAIMS (dispatch Bug A) — a handoff CLAIMED by an agent that never
# ran it (e.g. an agent whose loop was disabled) stays stuck forever: the dispatch
# path only picks up PENDING rows (claimed ones are skipped as in-flight) and the
# watchdog only ESCALATES, never requeues. The conservative reclaim pass RELEASES
# such a claim (→ pending) so the next reconcile re-dispatches it. The bar is
# DELIBERATELY high — we reclaim ONLY a claim that is BOTH older than this threshold
# AND has NO run ever started for it (truly orphaned, no partial work to lose):
#   * Default 3600s (1h) — a slow-to-start dispatch is NOT an orphan; the reported
#     case was claimed days ago and never ran.
#   * Bounded [300s, 7d] so a bad env value can't make it reclaim aggressively (too
#     low) nor effectively disable it without intent (too high).
# This is read straight from the env (not via _env_float's float-only bounds) so an
# operator can express the threshold in plain seconds; bounds are applied below.
RECLAIM_ORPHAN_S = _env_float("RECLAIM_ORPHAN_S", 3600.0, 300.0, 604800.0)
# Requeue ceiling for the orphan-reclaim path — the SAME cap the watchdog applies to
# its stuck-run requeue (env ``WATCHDOG_MAX_RETRIES``, default 3). A handoff already
# requeued this many times is NOT released again here; it is left claimed for the
# watchdog to escalate to the lead. This guarantees NO path (reclaim OR watchdog)
# requeues a handoff without a ceiling.
RECLAIM_MAX_RETRIES = _env_int("WATCHDOG_MAX_RETRIES", 3, 0, 100)
# Handoff statuses that count as a fresh, dispatchable PENDING row (claimed/closed
# rows are skipped). Mirrors the Dispatch view's open-status set.
_PENDING_STATUSES = ("pending", "open", "new", "unclaimed", "")
# Handoff statuses that count as CLAIMED / in-flight (the reclaim pass scans these).
# A row is treated as claimed if its status is here OR it simply carries a claimed_by
# (the live list marks both); the reclaim guard re-checks no-run before releasing.
_CLAIMED_STATUSES = ("claimed", "in_progress", "in-progress", "working", "assigned")

# Handoff statuses that count as COMPLETE / terminal for WAVE gating (E007 Phase
# 1.5). A wave is "done" only when EVERY handoff in it has a status here. This set
# is the union of Cortex's closed-handoff vocab; it MUST match the legacy/path-safe
# wave-plan helper's `--show` terminal set so the printed DAG agrees with what the
# loop gates on. A claimed-but-still-open handoff is NOT terminal — it's in-flight, so a
# later wave keeps waiting until that run actually completes the handoff in Cortex.
_TERMINAL_STATUSES = (
    "completed", "complete", "done", "closed", "cancelled", "canceled", "resolved",
)


# ---------------------------------------------------------------------------
#  HarnessPort factory (Track B / harness-service I1) — pick the worker-spawn
#  mechanism from HARNESS_SPAWN_MODE. The composition root (main.py lifespan)
#  calls this and passes the result as Orchestrator(harness_port=…).
#
#    unset / "legacy" / anything-unknown → None
#        → the orchestrator's EXISTING inline subprocess.Popen path (zero change).
#    "local"  → LocalHarnessAdapter (HarnessPort over the SAME host subprocess spawn,
#        now behind the port — the seam a remote adapter slots into later).
#    "remote" → RemoteHarnessAdapter (POST to the host harness-service). NOT built
#        until I2 — the lazy import is wrapped so an ImportError fails CLOSED
#        (returns None → legacy path), so selecting "remote" in I1 never breaks boot.
#
#  Imports are LAZY (inside the function) so this module never eagerly pulls in the
#  adapter package (which imports subprocess/httpx) at import time — and so the I2
#  remote module can be absent in I1 without a top-level ImportError.
# ---------------------------------------------------------------------------

def _make_harness_port() -> Any | None:
    """Resolve the HarnessPort for this process from HARNESS_SPAWN_MODE (see above).
    Returns None for the default/legacy/unknown modes (→ the inline spawn path) and
    NEVER raises — a misconfigured mode or a not-yet-built remote adapter degrades to
    None (legacy), so autonomy is never blocked by harness-mode config."""
    mode = os.environ.get("HARNESS_SPAWN_MODE", "legacy").strip().lower()
    if mode == "local":
        try:
            from app.adapters.harness_local import LocalHarnessAdapter

            return LocalHarnessAdapter()
        except Exception as exc:  # never let harness-mode config break boot
            log.warning(
                "HARNESS_SPAWN_MODE=local but LocalHarnessAdapter unavailable "
                "(falling back to legacy inline spawn): %s", exc,
            )
            return None
    if mode == "remote":
        # I2 (BUILT): the remote harness-service adapter — httpx over POST /spawn to
        # the HOST-resident harness-service (app/harness_service.py). Lazy-import +
        # fail CLOSED to the legacy path is KEPT (defensive): if the adapter module is
        # ever unavailable (a partial checkout / a packaging slice that omits httpx),
        # selecting 'remote' degrades to the inline spawn instead of crashing boot.
        try:
            from app.adapters.harness_remote import RemoteHarnessAdapter

            return RemoteHarnessAdapter()
        except ImportError:
            log.warning(
                "HARNESS_SPAWN_MODE=remote but RemoteHarnessAdapter could not be "
                "imported — falling back to the legacy inline spawn path."
            )
            return None
        except Exception as exc:  # any other failure also degrades to legacy
            log.warning(
                "HARNESS_SPAWN_MODE=remote adapter failed to construct "
                "(falling back to legacy inline spawn): %s", exc,
            )
            return None
    # "legacy" / unset / anything unrecognised → the existing inline spawn path.
    return None


# ---------------------------------------------------------------------------
#  Activity feed — an in-memory ring buffer (newest-first on read). Visible in
#  the Dispatch view so the operator can see what Dispatch picked up / ran.
#  Cleared on process restart by design (it is live telemetry, not an audit log —
#  the durable record is the Cortex handoff lifecycle + the app-DB usage rows).
# ---------------------------------------------------------------------------

class ActivityFeed:
    """Bounded, thread-safe-enough (single event loop) recent-activity ring."""

    def __init__(self, maxlen: int = ACTIVITY_MAX) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0

    def add(
        self,
        project: str,
        kind: str,
        text: str,
        *,
        agent: str | None = None,
        handoff_id: str | None = None,
        level: str = "info",
    ) -> None:
        self._seq += 1
        self._items.appendleft(
            {
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "project": (project or "").strip().lower(),
                "kind": kind,        # picked_up | dispatched | completed | error | skipped | info
                "level": level,      # info | warn | error | success
                "text": text,
                "agent": agent,
                "handoff_id": handoff_id,
                "handoff_short": (handoff_id or "")[:8] if handoff_id else None,
            }
        )
        # Mirror to the log so a headless run is still observable.
        getattr(log, "warning" if level in ("warn", "error") else "info")(
            "[dispatch·%s] %s", project, text
        )

    def recent(self, project: str | None = None, limit: int = ACTIVITY_MAX) -> list[dict[str, Any]]:
        key = (project or "").strip().lower()
        out: list[dict[str, Any]] = []
        for item in self._items:  # already newest-first
            if key and item["project"] != key:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
#  Live run-state is the durable RunState SSOT store (Milestone 1).
#
#  The in-memory ``RunTranscript`` / ``TranscriptStore`` that used to back the
#  "watch the crew work" view were REMOVED at T12: the orchestrator pre-creates a
#  ``run_state`` row in the store (``self._runstate.start_run`` in ``_dispatch_run``)
#  and the detached worker writes spans + heartbeat + terminal status to that SAME
#  row (T6). The agent-detail pane reads the store (T7) and is pushed live off the
#  store's NOTIFY bus (T8 ``/runstate/stream``). The store is the ONE live-state
#  path — restart-safe, not process memory. ``ActivityFeed`` below stays: it is the
#  Dispatch view's lifecycle ring (picked up / completed / errored), not a transcript.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
#  Server-side agent-run primitive — "kick off an agent" (no browser).
# ---------------------------------------------------------------------------

async def _safe_repo_root(cortex: Any, project_key: str | None) -> str | None:
    """The project's on-disk repo_root, for spawning a worker in the RIGHT folder.

    Returns None — a safe no-op that leaves the legacy cwd — on ANY failure:
    unknown project, no repo_root, or a configured root that isn't mounted /
    doesn't exist (e.g. an offline GoogleDrive path). Never raises, so a bad
    project root degrades a dispatch to the old behaviour instead of crashing it."""
    if not project_key:
        return None
    try:
        proj = await cortex.get_project(project_key)
    except Exception:
        return None
    root = ((proj or {}).get("repo_root") or "").strip()  # fitness:allow-literal "repo_root" is a wire/dict key, not a project (false match on 'root')
    return root if root and os.path.isdir(root) else None  # fitness:allow-literal "root" path var, not the agent/project (false match on 'root')


async def run_agent_server_side(
    *,
    cortex: Any,
    harness_runner: Any,
    chat_routing_for: Callable[[dict, str], tuple[str, str | None, str | None]],
    record_usage: Callable[..., Awaitable[None]],
    project_key: str,
    agent: dict,
    task_prompt: str,
    system: str | None = None,
) -> dict[str, Any]:
    """Run ONE agent server-side and CONSUME its stream here (no SSE to a browser).

    Resolves the agent's harness/model via ``chat_routing_for`` (override-first,
    same as the chat/Approve&Run path), spawns it through
    ``harness_runner.stream_chat``, accumulates the reply, records the run's usage
    to the app-DB (via ``record_usage`` — the same logic the routes use), and
    returns a small summary. Only the OUTPUT text (``delta``/``result``) is
    accumulated into the returned reply ``text``; ``thinking``/``tool`` spans are
    ignored here (this seam is the kickoff-framing helper, which wants only the
    reply — the live transcript is written to the RunState store by the worker).

    Returns ``{status, text, error, category, tokens_in, tokens_out, cost_usd}``
    where ``status`` is one of ``ok`` | ``error``. NEVER raises — a runner/spawn
    failure is captured into ``status='error'`` so the loop just logs it (Phase 2
    escalation is out of scope). This is the autonomous twin of the dispatch route's
    streaming branch, minus the browser."""
    # Resolve routing OFF the loop: chat_routing_for does a sync psycopg2 settings
    # read (per-agent override), and this runs while the subprocess below streams —
    # a blocked loop here is the residual dispatch stall (see _chat_routing_async).
    harness, model, _reasoning = await _chat_routing_async(
        chat_routing_for, agent, project_key
    )
    # Run the inline server-side turn in the SELECTED project's folder + scope (not
    # the console's own). None when the project has no usable root → legacy behaviour.
    workspace = await _safe_repo_root(cortex, project_key)
    assembled: list[str] = []
    result_text = ""
    status = "ok"
    error_msg: str | None = None
    error_cat: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None

    try:
        async for ev in harness_runner.stream_chat(
            task_prompt, model=model, system=system, harness=harness,
            workspace=workspace, project_key=project_key, run_context="autonomous",
        ):
            kind = ev.get("type")
            if kind == "delta":
                assembled.append(ev.get("text", ""))
            elif kind == "result":
                result_text = ev.get("text") or ""
                tokens_in = ev.get("tokens_in")
                tokens_out = ev.get("tokens_out")
                cost_usd = ev.get("cost_usd")
                # Capture usage to the app-DB exactly like the routes do.
                with contextlib.suppress(Exception):
                    await record_usage(project_key, agent, model, ev)
            elif kind == "error":
                status = "error"
                error_msg = ev.get("message", "harness error")
                error_cat = ev.get("category", "error")
            elif kind in ("thinking", "tool", "done"):
                # thinking/tool spans are display-only and not part of the reply;
                # this server-side seam only needs the OUTPUT text.
                pass
    except Exception as exc:  # the runner should not raise, but never crash the loop
        status = "error"
        error_msg = f"agent run failed: {exc}"
        error_cat = "run_exception"

    text = result_text or "".join(assembled)
    return {
        "status": status,
        "text": text,
        "error": error_msg,
        "category": error_cat,
        "harness": harness,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
    }


# ---------------------------------------------------------------------------
#  The Dispatch scheduler loop (class name retained for API compatibility).
# ---------------------------------------------------------------------------

class Orchestrator:
    """Dispatch scheduler background loop. One instance per app, started/stopped
    in the FastAPI lifespan. Holds the dispatched-set (idempotency), the
    per-project in-flight counters (concurrency cap), the per-project event
    watchers, and the activity feed."""

    def __init__(
        self,
        *,
        cortex: Any,
        appdb: Any,
        harness_runner: Any,
        chat_routing_for: Callable[[dict, str], tuple[str, str | None, str | None]],
        record_usage: Callable[..., Awaitable[None]],
        find_agent: Callable[[list[dict], str], dict | None],
        resolve_target: Callable[..., dict | None],
        classify_interactive: Callable[[dict, str], bool],
        project_identity: Callable[
            [Any, str], str | None | Awaitable[str | None]
        ],
        agent_view: Callable[[dict], dict],
        runstate: Any | None = None,
        harness_port: Any | None = None,
    ) -> None:
        self._cortex = cortex
        self._appdb = appdb
        self._runner = harness_runner
        self._chat_routing_for = chat_routing_for
        self._record_usage = record_usage
        # RunStatePort (Milestone 1) — the run-state SSOT store, the ONE live-state
        # path. Injected from the lifespan (RunStatePgStore over the shared AppDB
        # pool). OPTIONAL + graceful-degrade: when None (or a write fails) the
        # orchestrator falls back to the legacy spawn argv (no run_id) and the run
        # simply writes no store state — a down store never blocks a dispatch.
        self._runstate = runstate
        # HarnessPort (Track B / harness-service I1) — the worker-SPAWN seam. When
        # injected (built by _make_harness_port from HARNESS_SPAWN_MODE), _dispatch_run
        # routes the spawn through the port (the local adapter is byte-for-byte the
        # existing host subprocess.Popen; a remote adapter lands in I2). OPTIONAL +
        # additive: when None (the default, HARNESS_SPAWN_MODE unset), _dispatch_run
        # takes the EXISTING inline subprocess.Popen path unchanged — zero behaviour
        # change. A down/failing port can only fail a single dispatch (reported as an
        # error feed line + slot released), never crash the loop.
        self._harness = harness_port
        self._find_agent = find_agent
        self._resolve_target = resolve_target
        # Override-first interactive classifier (mirrors main._classify_interactive):
        # given (agent, designation_override) returns True for an INTERACTIVE agent
        # (an interactive lead/human you talk to). Injected — this module can't import main.py.
        self._classify_interactive = classify_interactive
        self._project_identity = project_identity
        self._agent_view = agent_view

        self.feed = ActivityFeed()
        # Per-run live transcripts now live in the durable RunState SSOT store
        # (``self._runstate``) — the in-memory transcript store was removed at T12.

        # idempotency: (project, handoff_id) already dispatched (or in flight).
        self._dispatched: set[tuple[str, str]] = set()
        # REQUEUE RECONCILIATION (AV-3): the ``retry_count`` we observed when each key
        # was ACTUALLY dispatched (the line-6 spawn path only — NOT the held-pending
        # adds). The watchdog/reclaim REQUEUE a stuck mid-run handoff by RELEASING it
        # back to pending, which increments the row's ``retry_count`` (server-side, via
        # POST /handoffs/{id}/release). On the next sweep the idempotency gate compares
        # the candidate's live ``retry_count`` against this recorded value: a rise means
        # the run was requeued, so we DROP the stale marker and re-dispatch — closing the
        # requeue → re-dispatch loop that the append-only ``_dispatched`` set otherwise
        # strands (the run would sit pending forever, blocked here yet never re-claimed,
        # so it could never reach the watchdog's retry cap). Held-pending handoffs
        # (unresolved/interactive/deterministic) are NEVER recorded here, so they keep
        # their "held until restart" semantics (no entry → no re-dispatch, no re-log).
        self._dispatched_retry: dict[tuple[str, str], int] = {}
        # concurrency cap: live auto-run count per project.
        self._inflight: dict[str, int] = {}
        # wave-status feed de-dupe: project -> {epic -> last-reported snapshot}.
        # Keeps _report_wave_status from spamming the activity feed every poll.
        self._wave_status_seen: dict[str, dict[str | None, tuple]] = {}
        # (project, handoff_id) we've already logged a 'wave-blocked, holding' line
        # for — so a held handoff logs once, not every poll while it waits.
        self._wave_blocked_logged: set[tuple[str, str]] = set()
        # handoff ids whose UNROUTABLE target (unknown role/agent) we've already
        # escalated to the lead — single-shot per process; the Cortex byte-identical
        # create-dedupe keeps it single across restarts too.
        self._unroutable_escalated: set[str] = set()
        # Retry-capped orphan claims remain claimed by design while the watchdog
        # escalates them. Log each one once per process instead of every 4s sweep.
        self._reclaim_cap_logged: set[tuple[str, str]] = set()
        # Handoffs waiting behind the concurrency cap stay pending and are revisited
        # every sweep. Emit one wait-state line per handoff, then clear it at dispatch.
        self._cap_deferred_logged: set[tuple[str, str]] = set()
        # last-computed per-project wave summary (for the Dispatch view header).
        # project -> {"epics": [{epic, active_wave, running, waiting}], "any": bool}.
        self._wave_summary: dict[str, dict[str, Any]] = {}
        # per-project event-watcher tasks (open while the project is ON).
        self._watchers: dict[str, asyncio.Task] = {}
        # spawned dispatch tasks (kept so shutdown can cancel cleanly).
        self._runs: set[asyncio.Task] = set()

        # Project beat state.
        # _pm_beat_inflight: projects with a beat subprocess currently running.
        #   Used to debounce: only ONE beat per project at a time.
        # _pm_beat_last_ts: monotonic timestamp of the last completed (or started)
        #   beat per project. Poll-triggered beats check this against
        #   PM_BEAT_MIN_INTERVAL_S; completion-triggered beats bypass the rate-limit.
        self._pm_beat_inflight: set[str] = set()
        self._pm_beat_last_ts: dict[str, float] = {}
        # Strong refs to in-flight pm-beat tasks — prevents CPython GC from
        # collecting an untracked task before it executes (mirrors _runs pattern).
        self._pm_beat_tasks: set[asyncio.Task] = set()
        # Autonomy owns the canonical PM planning heartbeat. Track projects whose
        # durable beat has been verified this process; failed checks are retried on
        # the next reconcile and turning autonomy OFF clears the marker.
        self._planning_beats_ensured: set[str] = set()

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()  # toggle route fires this for a fast reconcile
        self._ticks = 0

    # -- lifecycle ----------------------------------------------------------

    async def _resolve_target_detail(
        self, project_key: str, handoff: dict, agents: list[dict]
    ) -> dict[str, Any]:
        """Resolve a handoff target with the shared role-routing detail contract.

        Keeps the scheduler feed aligned with the Dispatch board: unresolved work
        is no longer just "no roster match"; it carries the same reason code and
        human-readable reason operators see in the UI. If the detail resolver ever
        fails, fall back to the injected legacy resolver so existing integrations
        stay compatible.
        """
        try:
            names = [
                (a.get("name") or "").strip()
                for a in agents
                if (a.get("name") or "").strip()
            ]
            designations: dict[str, str] = {}
            aliases: dict[str, str] = {}
            for name in names:
                key = name.lower()
                with contextlib.suppress(Exception):
                    designations[key] = await _agent_designation_async(project_key, name)
                with contextlib.suppress(Exception):
                    aliases[key] = await _agent_role_aliases_async(project_key, name)
            return _roles.resolve_target_detail(
                handoff,
                agents,
                designation_of=lambda name: designations.get((name or "").lower(), ""),
                classify_interactive=self._classify_interactive,
                aliases_of=lambda name: aliases.get((name or "").lower(), ""),
            )
        except Exception as exc:  # pragma: no cover - defensive compatibility path
            log.warning(
                "[dispatch·%s] target detail resolution failed; falling back: %s",
                project_key, exc,
            )
            try:
                target = self._resolve_target(handoff, agents or [], project_key)
            except TypeError:
                target = self._resolve_target(handoff, agents or [])
            if target is not None:
                return {
                    "agent": target,
                    "status": "resolved",
                    "reason_code": "legacy_resolver",
                    "reason": "Legacy target resolver returned an agent.",
                    "target_type": "legacy",
                    "target": "",
                    "matched_on": "",
                }
            return {
                "agent": None,
                "status": "unresolved",
                "reason_code": "legacy_unresolved",
                "reason": "Legacy target resolver did not return an agent.",
                "target_type": "legacy",
                "target": "",
                "matched_on": "",
            }

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="dispatch-scheduler")
            log.info(
                "dispatch scheduler started (OFF by default; reconcile=%ss, cap=%d/project)",
                POLL_INTERVAL, MAX_CONCURRENT,
            )

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        # Cancel watchers + in-flight runs + the main loop.
        for t in list(self._watchers.values()):
            t.cancel()
        for t in list(self._runs):
            t.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._watchers.clear()
        self._runs.clear()

    def notify_toggle_changed(self) -> None:
        """The autonomous-toggle route calls this so the loop reconciles NOW
        (open/close watchers, pick up backlog) instead of waiting for the poll."""
        self._wake.set()

    # -- introspection for the UI ------------------------------------------

    def status(self, project_key: str | None = None) -> dict[str, Any]:
        """Snapshot for the Dispatch view: whether the loop is active anywhere,
        and this project's ON state + in-flight count."""
        on = settings_store.autonomous_projects()
        key = (project_key or "").strip().lower()
        return {
            "loop_running": self._task is not None and not self._task.done(),
            "any_on": bool(on),
            "on_projects": on,
            "project_on": key in on if key else False,
            "inflight": self._inflight.get(key, 0) if key else 0,
            "max_concurrent": MAX_CONCURRENT,
            "ticks": self._ticks,
            # Wave plan summary for this project (E007 Phase 1.5): the active epic +
            # wave per epic, with running/waiting counts. Empty 'epics' (and
            # any=False) means no wave plan → flat Phase-1 dispatch.
            "waves": self._wave_summary.get(key, {"epics": [], "any": False}) if key else {"epics": [], "any": False},
        }

    # NOTE: the live-transcript getters (recent_runs / transcript / latest_run) were
    # removed at T12 along with the in-memory transcript store. The agent-detail pane
    # now reads run state from the durable RunState SSOT store directly
    # (main._agent_runs_view_store over app.state.runstate), pushed live by the T8
    # /runstate/stream channel — the orchestrator no longer holds a read model.

    # -- the main reconcile loop -------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._ticks += 1
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one bad sweep kill the loop
                log.warning("dispatch scheduler sweep error (continuing): %s", exc)
            # Wait POLL_INTERVAL, but wake early on a toggle change.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_INTERVAL)
            self._wake.clear()

    async def _reconcile_once(self) -> None:
        """One sweep: re-read the ON set, match watchers to it, and poll-scan each
        ON project's pending handoffs (the fallback path). When the ON set is
        empty this does effectively nothing — the loop's idle no-op."""
        try:
            on = set(await _autonomous_projects_async())
        except Exception as exc:
            log.warning("dispatch scheduler could not read ON set (idling): %s", exc)
            on = set()

        # Close watchers for projects that turned OFF (kill-switch path).
        for proj in list(self._watchers):
            if proj not in on:
                t = self._watchers.pop(proj)
                t.cancel()
                self.feed.add(proj, "info", "autonomy OFF — Dispatch stopped watching", agent=ORCH_AGENT)
                self._planning_beats_ensured.discard(proj)

        # Open watchers for newly-ON projects (event-driven path).
        for proj in on:
            watcher = self._watchers.get(proj)
            if watcher is None or watcher.done():
                self._watchers[proj] = asyncio.create_task(
                    self._watch_project(proj), name=f"dispatch-watch-{proj}"
                )
                self.feed.add(proj, "info", "autonomy ON — Dispatch watching for handoffs", agent=ORCH_AGENT)

        # A project-level autonomy switch is a complete operating mode, not only a
        # handoff watcher. Ensure its proactive PM/lead planning heartbeat exists.
        # A newly repaired job is due immediately and is emitted by the scheduled-job
        # pass below in this same reconcile cycle.
        for proj in on:
            if proj in self._planning_beats_ensured:
                continue
            result = await automation_feed.ensure_pm_planning_schedule(
                appdb=self._appdb,
                cortex=self._cortex,
                project=proj,
            )
            if result.get("ok"):
                self._planning_beats_ensured.add(proj)
                if result.get("created") or result.get("repaired"):
                    self.feed.add(
                        proj,
                        "info",
                        "autonomy ON — PM planning heartbeat enabled and due now",
                        agent=ORCH_AGENT,
                    )

        # Poll-fallback scan: catch pending handoffs the event stream missed.
        for proj in on:
            with contextlib.suppress(Exception):
                await self._scan_pending(proj)

        # Durable scheduled-job feeders. These emit ordinary Cortex handoffs and
        # let the existing dispatch/propose/approval path handle execution.
        for proj in on:
            with contextlib.suppress(Exception):
                await automation_feed.run_due_scheduled_jobs(
                    appdb=self._appdb,
                    cortex=self._cortex,
                    project=proj,
                    feed=self.feed,
                )

        # Legacy script PM-beat poll safety net. Normal PM planning now runs as a
        # scheduled handoff above, so this only wakes when an operator explicitly
        # configures the old ORCH_PM_BEAT_SCRIPT compatibility hook.
        if PM_BEAT_SCRIPT:
            for proj in on:
                self._schedule_pm_beat(proj, reason="poll")

    def _schedule_pm_beat(self, project_key: str, *, reason: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _bt = loop.create_task(
            self._pm_beat(project_key, reason=reason),
            name=f"pm-beat-{reason}-{project_key}",
        )
        self._pm_beat_tasks.add(_bt)
        _bt.add_done_callback(self._pm_beat_tasks.discard)

    # -- event-driven watcher (primary path) -------------------------------

    async def _watch_project(self, project_key: str) -> None:
        """Consume Cortex /events for one ON project and dispatch on each new
        ``handoff_created`` frame. Re-checks the ON state on every frame so a
        toggle-OFF stops dispatching even before the watcher is torn down. The
        stream is idle/cost-free between events; on disconnect it simply returns
        and the next reconcile re-opens it."""
        buf = b""
        try:
            async for chunk in self._cortex.stream_events(project_key):
                # The kill-switch must bite mid-stream too. Read the ON set OFF the
                # event loop (sync psycopg2) — this fires on ~every SSE chunk, so a
                # blocking read here is exactly what starves a concurrent harness
                # subprocess's stdout drain and stalls its run.
                if project_key not in set(await _autonomous_projects_async()):
                    return
                buf += chunk
                # SSE frames are separated by a blank line.
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    frame = _parse_sse_frame(raw)
                    if frame is None:
                        continue
                    event_name, payload = frame
                    if event_name != "handoff_created":
                        continue
                    handoff = _handoff_from_event(payload)
                    if handoff is None:
                        continue
                    with contextlib.suppress(Exception):
                        await self._maybe_dispatch(project_key, handoff, source="event")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("dispatch watcher for %s ended: %s", project_key, exc)

    # -- wave plan context (E007 Phase 1.5) --------------------------------

    async def _load_wave_context(
        self, project_key: str, handoffs: list[dict] | None = None
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Load the wave-gating context for a project: ``(plan, handoffs_by_id)``.

        ``plan`` is the app-DB ``handoff_orchestration`` map {id: {epic, wave}} —
        EMPTY when the app-DB is down OR nothing is planned, in which case every
        handoff is wave 0 (Phase-1 behaviour preserved — a degraded app-DB can only
        fall back to ungated dispatch, never strand a handoff). ``handoffs_by_id``
        indexes the project's live Cortex handoffs (re-used from the poll scan when
        passed, else fetched) so the gate can test which earlier-wave handoffs are
        complete. Both reads are best-effort; on any failure we degrade to empty
        (→ wave 0 / Phase 1), never raising into the loop."""
        plan: dict[str, dict[str, Any]] = {}
        try:
            plan = await self._appdb.orchestration_plan(project_key)
        except Exception as exc:  # app-DB layer already swallows; belt + braces
            log.info("wave plan read failed for %s (treating as wave 0): %s",
                     project_key, exc)
            plan = {}
        # Fast path: no plan at all → no need to index handoffs (every row wave 0).
        if not plan:
            return {}, {}
        if handoffs is None:
            with contextlib.suppress(Exception):
                handoffs = await self._cortex.get_handoffs(project_key)
        by_id: dict[str, dict[str, Any]] = {}
        for h in handoffs or []:
            hid = str(h.get("id") or "").strip()
            if hid:
                by_id[hid] = h
        return plan, by_id

    # -- poll fallback (catch-up scan) -------------------------------------

    async def _scan_pending(self, project_key: str) -> None:
        """Re-scan /handoffs for pending rows the event path may have missed. The
        seen-set + claim make a row already handled here a cheap no-op.

        Wave-aware (E007 Phase 1.5): we load the project's wave plan + index the
        live handoffs ONCE here, then pass that context to each candidate so only
        the active wave dispatches. With no plan the context is empty and every
        pending row is wave 0 (Phase-1 behaviour). Also surfaces a compact per-epic
        'active wave' line to the activity feed so the operator can see what
        Dispatch is working through and what is waiting."""
        handoffs = await self._cortex.get_handoffs(project_key)
        plan, by_id = await self._load_wave_context(project_key, handoffs)
        wave_ctx = (plan, by_id)

        # Visibility: when a plan exists, report each epic's active wave + how many
        # of its handoffs are running / waiting (best-effort, never blocks dispatch).
        if plan:
            with contextlib.suppress(Exception):
                self._report_wave_status(project_key, plan, by_id)

        # RECLAIM ORPHANED CLAIMS (dispatch Bug A) — BEFORE the pending dispatch scan:
        # release any genuinely-orphaned claim (claimed long ago, no run ever started)
        # back to pending so this SAME sweep's dispatch loop (and the next) can re-pick
        # it. Best-effort + fully guarded — a reclaim failure logs + continues, never
        # crashing the reconcile loop or blocking dispatch.
        with contextlib.suppress(Exception):
            await self._reclaim_orphaned_claims(project_key, handoffs)

        for h in handoffs or []:
            if not _is_pending(h):
                continue
            await self._maybe_dispatch(project_key, h, source="poll", wave_ctx=wave_ctx)

    async def _reclaim_orphaned_claims(
        self, project_key: str, pending_handoffs: list[dict] | None
    ) -> None:
        """CONSERVATIVE reclaim pass (dispatch Bug A). For each CLAIMED handoff in the
        project, RELEASE it back to pending — ONLY when it is GENUINELY orphaned:

          1. status == claimed (``_is_claimed``), AND
          2. ``claimed_age`` is known AND > ``RECLAIM_ORPHAN_S`` (a slow-to-start
             dispatch is NOT an orphan; an unknown age is NEVER reclaimed), AND
          3. ``retry_count`` is BELOW ``RECLAIM_MAX_RETRIES`` (the same requeue ceiling
             the watchdog applies — a row already requeued to the cap is left claimed
             for the watchdog to escalate, never requeue-looped forever), AND
          4. NO run was EVER started for it (truly orphaned — no partial work to lose),
             determined from the run-state store. If the store is None / unreachable we
             SKIP every row (never reclaim blind — a down store must not look like
             "no run" and trigger a release of in-flight work).

        Released rows become PENDING → the dispatch path re-picks them with no other
        change. Every safety bar must hold; any single failure logs + continues. This
        method NEVER raises into the reconcile loop.

        ``pending_handoffs`` is the poll scan's already-fetched PENDING list, re-used
        only to skip a redundant claimed-fetch when there's nothing to do offline; the
        claimed rows themselves are fetched here via ``get_handoffs(status='claimed')``
        (the pending list does NOT contain claimed rows)."""
        # GUARD 0 — no store means we cannot prove "no run", so we must not reclaim
        # anything (the spec's "store None/down → SKIP"). This also covers a stripped
        # deployment where run-state isn't wired.
        if self._runstate is None:
            return
        # GUARD 0b — the store must be REACHABLE. A down store makes by_handoff()
        # return None (its degrade signal) for EVERY handoff, which would look like
        # "no run exists" and could release genuinely in-flight claims. Probe once per
        # pass; on an unreachable store, SKIP the whole pass.
        if not await self._runstate_reachable():
            return

        # Fetch the CLAIMED rows (the pending list the caller passed does NOT include
        # them — the /handoffs list is pending-only by default). Best-effort: a failed
        # fetch skips this pass.
        try:
            claimed = await self._cortex.get_handoffs(project_key, status="claimed")
        except Exception as exc:
            log.info("[reclaim·%s] could not list claimed handoffs (skipping): %s",
                     project_key, exc)
            return

        for h in claimed or []:
            with contextlib.suppress(Exception):
                await self._maybe_reclaim_one(project_key, h)

    async def _maybe_reclaim_one(self, project_key: str, handoff: dict) -> None:
        """Evaluate ONE claimed handoff against the four reclaim bars and release it
        iff ALL hold. Isolated per-row so one bad row never aborts the pass."""
        hid = str(handoff.get("id") or "").strip()
        if not hid:
            return
        runstate = self._runstate
        if runstate is None:
            return

        # BAR 1 — must be genuinely CLAIMED (not pending/terminal). Defence in depth:
        # we asked for status='claimed', but re-check so a race / odd status is safe.
        if not _is_claimed(handoff):
            return

        # BAR 2 — must be aged. An unknown age is NEVER reclaimed (a row we can't date
        # is treated as fresh). A recently-claimed row (< threshold) is a slow start,
        # not an orphan.
        age = _claimed_age_seconds(handoff)
        if age is None or age <= RECLAIM_ORPHAN_S:
            return

        # BAR 3 — RETRY CEILING. A handoff already requeued RECLAIM_MAX_RETRIES times
        # must NOT be released again (no requeue-loop forever — the same cap the
        # watchdog applies). Over the ceiling we LEAVE it claimed: the watchdog then
        # sees a stuck, at-cap run and escalates it to the lead instead of requeuing.
        retries = _retry_count_of(handoff)
        if retries >= RECLAIM_MAX_RETRIES:
            key = (project_key, hid)
            if key not in self._reclaim_cap_logged:
                self._reclaim_cap_logged.add(key)
                log.info(
                    "[reclaim·%s] orphaned claim %s at retry cap (%d ≥ %d) — leaving "
                    "claimed for watchdog escalation (no requeue-loop)",
                    project_key, hid[:8], retries, RECLAIM_MAX_RETRIES,
                )
            return

        # BAR 4 — NO run was ever started for it (truly orphaned). by_handoff returns
        # the latest run for the handoff, or None when none exists. We already proved
        # the store reachable (GUARD 0b), so a None here genuinely means "no run".
        # A row that DOES have a run is in-flight (or has partial work) → leave it for
        # the watchdog, never reclaim.
        try:
            run = await runstate.by_handoff(hid)
        except Exception as exc:
            # A per-row store hiccup → skip this row (never reclaim blind).
            log.info("[reclaim·%s] run-state lookup failed for %s (skipping): %s",
                     project_key, hid[:8], exc)
            return
        if run is not None:
            return

        # ALL bars passed → release the orphaned claim back to pending. The cortex
        # release is idempotent; a failure logs + leaves the row claimed (re-evaluated
        # next sweep). On success the next reconcile dispatches it via the normal path.
        released = await self._cortex.release_handoff(project_key, hid)
        if released:
            self.feed.add(
                project_key, "info",
                f"released orphaned claim {hid[:8]} (claimed {age:.0f}s ago, no run) → pending",
                agent=ORCH_AGENT, handoff_id=hid, level="warn",
            )
            log.info(
                "[reclaim·%s] released orphaned claim %s (claimed %.0fs ago, no run) → pending",
                project_key, hid[:8], age,
            )
        else:
            log.info(
                "[reclaim·%s] release of orphaned claim %s did not take "
                "(will re-evaluate next sweep)", project_key, hid[:8],
            )

    async def _runstate_reachable(self) -> bool:
        """Best-effort probe that the run-state store is REACHABLE, so a None from
        ``by_handoff`` can be trusted to mean "no run" rather than "store down".

        Uses the adapter's own degrade signal when present: the Pg store exposes a
        private ``_pool()`` that returns None when asyncpg is missing or the DB is
        unreachable. We duck-type it (the Protocol doesn't define it) and treat a
        non-None pool as reachable. When the store doesn't expose ``_pool`` (a stub /
        a different adapter), we fall back to a light ``list_active`` round-trip and
        treat a clean return (even []) as reachable, any raise as down. NEVER raises —
        an exception here means "treat as unreachable" → the caller skips the pass."""
        store = self._runstate
        if store is None:
            return False
        pool_probe = getattr(store, "_pool", None)
        if callable(pool_probe):
            try:
                return await pool_probe() is not None
            except Exception:
                return False
        # No private pool probe — fall back to a real (cheap) read. A clean return
        # (including an empty list) proves the store answered; any raise → unreachable.
        try:
            await store.list_active()
            return True
        except Exception:
            return False

    def _report_wave_status(
        self,
        project_key: str,
        plan: dict[str, dict[str, Any]],
        by_id: dict[str, dict[str, Any]],
    ) -> None:
        """Emit a compact per-epic active-wave status to the activity feed.

        For each planned epic: the active (lowest-incomplete) wave, how many of
        that wave's handoffs are currently in-flight (dispatched this process) vs
        still waiting to start, plus a note for later (blocked) waves. De-duped per
        (project, epic, active_wave, counts) so it logs only on a real change — no
        feed spam every poll. A stalled wave (a handoff whose deps never complete,
        or whose run keeps failing) simply keeps showing as the active wave; that is
        the 'it just waits, never crash' behaviour, surfaced for the operator."""
        # Collect epics present in the plan.
        epics: set[str | None] = set()
        for row in plan.values():
            e = row.get("epic")
            epics.add(e if (isinstance(e, str) and e.strip()) else None)

        summary_epics: list[dict[str, Any]] = []
        for epic in sorted(epics, key=lambda e: (e is None, e or "")):
            active = _active_wave_for_epic(epic, plan, by_id)
            epic_label = epic or "(no epic)"
            snapshot: tuple[str | None, int | None, int, int]
            if active is None:
                summary_epics.append(
                    {"epic": epic_label, "active_wave": None, "running": 0, "waiting": 0}
                )
                snapshot = (epic, None, 0, 0)
                if self._wave_status_seen.get(project_key, {}).get(epic) == snapshot:
                    continue
                self._wave_status_seen.setdefault(project_key, {})[epic] = snapshot
                self.feed.add(
                    project_key, "info",
                    f"Epic {epic_label} · all waves complete",
                    agent=ORCH_AGENT, level="info",
                )
                continue

            # Count this active wave's handoffs: running (in our dispatched set) vs
            # waiting (pending, not yet dispatched).
            running = 0
            waiting = 0
            for hid, row in plan.items():
                row_epic = row.get("epic")
                row_epic = row_epic if (isinstance(row_epic, str) and row_epic.strip()) else None
                if row_epic != epic or _plan_wave(plan, hid) != active:
                    continue
                live = by_id.get(hid)
                if live is not None and _is_handoff_complete(live):
                    continue
                if (project_key, hid) in self._dispatched:
                    running += 1
                else:
                    waiting += 1

            summary_epics.append(
                {"epic": epic_label, "active_wave": active,
                 "running": running, "waiting": waiting}
            )
            snapshot = (epic, active, running, waiting)
            if self._wave_status_seen.get(project_key, {}).get(epic) == snapshot:
                continue
            self._wave_status_seen.setdefault(project_key, {})[epic] = snapshot
            self.feed.add(
                project_key, "info",
                f"Epic {epic_label} · wave {active} · {running} running · {waiting} waiting",
                agent=ORCH_AGENT, level="info",
            )

        # Stash the latest summary for the Dispatch view header (status()).
        self._wave_summary[project_key] = {
            "epics": summary_epics,
            "any": any(e["active_wave"] is not None for e in summary_epics),
        }

    # -- the gate: idempotency + cap + claim, then dispatch ----------------

    def _forget_dispatch(self, key: tuple[str, str]) -> None:
        """Drop ALL per-(project, handoff) dispatch bookkeeping for ``key`` so the next
        sweep treats it as fresh. Used when a requeue is detected (AV-3): the
        watchdog/reclaim released a stuck run back to pending, so its stale
        idempotency marker, recorded retry baseline, and one-shot wave-blocked log
        flag must all be cleared for it to re-dispatch cleanly. Total + idempotent —
        ``discard``/``pop(..., None)`` never raise on a missing key."""
        self._dispatched.discard(key)
        self._dispatched_retry.pop(key, None)
        self._wave_blocked_logged.discard(key)

    async def _maybe_dispatch(
        self,
        project_key: str,
        handoff: dict,
        *,
        source: str,
        wave_ctx: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        """The single funnel every candidate handoff (event OR poll) passes
        through. Enforces, in order: OFF-gate, idempotency, WAVE gate, target
        resolution, INTERACTIVE guard, concurrency cap, Cortex claim. Only if ALL
        pass does it kick off.

        INTERACTIVE GUARD: designation means chat capability, not dispatch
        capability. After resolving the target we look up its designation
        override-first and, if it is interactive, require either explicit per-agent
        `auto_dispatch=true` or the global compatibility override. Without that
        opt-in, the handoff is left UNCLAIMED for the human. This guard sits
        BEFORE the cap + claim so a held handoff never consumes a slot or claims
        the row.

        `wave_ctx` is the pre-loaded ``(plan, handoffs_by_id)`` from the poll scan;
        the event path passes None and we load it lazily. The wave gate (E007 Phase
        1.5) blocks a wave > 0 handoff until all its prior waves complete; a wave-0
        / unplanned handoff is always allowed (Phase-1 behaviour). A wave-blocked
        handoff is left UNTOUCHED (not marked dispatched, not claimed) so it is
        re-evaluated on the next sweep once its wave opens — it simply waits."""
        # 1. OFF-gate (defence in depth — the watcher/scan only run for ON
        #    projects, but re-check so a flip between read + dispatch is honoured).
        #    Read OFF the event loop (sync psycopg2 must never block the loop while
        #    a harness subprocess streams).
        if project_key not in set(await _autonomous_projects_async()):
            return

        hid = str(handoff.get("id") or "").strip()
        if not hid:
            return
        key = (project_key, hid)

        # 2. Idempotency — never dispatch the same handoff twice. EXCEPTION (AV-3): if
        #    the handoff has since been REQUEUED — its live ``retry_count`` rose above
        #    what we recorded when we dispatched it — the watchdog/reclaim released it
        #    back to pending and it MUST be re-dispatched. Reconcile the append-only set
        #    against the live count so the requeue → re-dispatch loop actually closes
        #    (and the run can keep cycling until the watchdog's retry cap escalates it).
        #    A key with NO recorded retry (the held-pending adds below) is left as-is —
        #    those stay held without re-dispatch/re-log, exactly as before.
        if key in self._dispatched:
            prior_retry = self._dispatched_retry.get(key)
            if prior_retry is None or _retry_count_of(handoff) <= prior_retry:
                return
            # retry_count rose → requeued since our dispatch: drop the stale marker and
            # fall through to re-dispatch this released run.
            self._forget_dispatch(key)
            self.feed.add(
                project_key, "info",
                f"{hid[:8]} requeued (retry {_retry_count_of(handoff)}) — re-dispatching",
                agent=ORCH_AGENT, handoff_id=hid, level="info",
            )

        if not _is_pending(handoff):
            return

        # 2.5 WAVE gate (E007 Phase 1.5) — dependency sequencing. A wave > 0
        #     handoff dispatches ONLY when all its prior waves (same epic) are
        #     complete; a wave-0 / unplanned handoff is always allowed (Phase 1).
        #     Load the plan lazily for the event path (the poll path passes it in).
        if wave_ctx is None:
            wave_ctx = await self._load_wave_context(project_key)
        plan, by_id = wave_ctx
        if plan:  # no plan → every handoff is wave 0 → skip the gate entirely
            # The live row from the index is freshest; fall back to the candidate.
            live = by_id.get(hid, handoff)
            allowed, wave, epic, active = _wave_gate_ok(live, plan, by_id)
            if not allowed:
                # Wave-blocked: leave it for a later sweep (do NOT mark dispatched
                # or claim it). Log once per (project, handoff) so we don't spam.
                if key not in self._wave_blocked_logged:
                    self._wave_blocked_logged.add(key)
                    self.feed.add(
                        project_key, "skipped",
                        f"holding {hid[:8]} — epic {epic or '(none)'} wave {wave} "
                        f"waits for wave {active if active is not None else '?'} to finish",
                        agent=ORCH_AGENT, handoff_id=hid, level="info",
                    )
                return

        # 3. Resolve the target agent (to_agent, else to_role → roster agent).
        #    Done BEFORE the cap/claim so an interactive target (next step) can be
        #    skipped without ever consuming a concurrency slot or claiming the row.
        #    `project_key` is threaded so the resolver's lead-alias step (cpo →
        #    the project's interactive lead) reads THIS project's designation
        #    overrides (designation-driven, not a baked-in agent). The injected
        #    resolver may be a legacy 2-arg callable (older test stubs) — fall back
        #    to the 2-arg call so the contract stays backward-compatible.
        agents = await self._cortex.get_agents(project_key)
        resolution = await self._resolve_target_detail(project_key, handoff, agents or [])
        target = resolution.get("agent")
        if target is None:
            # No roster match → mark seen (so we don't re-evaluate forever) + log.
            self._dispatched.add(key)
            reason = resolution.get("reason") or "No roster agent matches this handoff."
            code = resolution.get("reason_code") or "unresolved"
            level = "info" if code == "human_target" else "warn"
            self.feed.add(
                project_key, "skipped",
                f"handoff {hid[:8]} not auto-dispatched ({code}) — {reason}",
                handoff_id=hid, level=level,
            )
            # UNROUTABLE SAFETY NET: a handoff to a role/agent that matches NOBODY is a
            # permanent config error (e.g. an agent escalating to an invented role like
            # 'scmo') — before this, such consult-backs/alarms died silently as pending
            # rows forever (ultrareview 2026-07-02). File ONE escalation to the LEAD so a
            # human-visible agent re-routes it. Human targets (cto/operator) are
            # intentional and never escalated. Single-shot: in-memory guard + the
            # Cortex byte-identical create-dedupe (vs open rows) across restarts.
            if code in ("unknown_role", "unknown_agent") and hid not in self._unroutable_escalated:
                self._unroutable_escalated.add(hid)
                from_agent = str(handoff.get("from_agent") or "").split("@", 1)[0].strip()
                if from_agent:
                    tgt = resolution.get("target") or handoff.get("to_agent") or handoff.get("to_role") or "?"
                    body = {
                        "to_role": "lead",
                        "priority": "high",
                        "summary": (f"[UNROUTABLE] handoff {hid[:8]} targets '{tgt}' which matches "
                                    "no roster agent — re-route it or fix the roster/aliases"),
                        "context": (f"Original handoff {hid}: {str(handoff.get('summary') or '')[:300]}\n"
                                    f"Resolution: {reason}\n"
                                    "Action: re-file the work to a real role/agent (or add a role alias), "
                                    "then complete both this escalation and the stranded handoff."),
                    }
                    with contextlib.suppress(Exception):
                        await self._cortex.create_handoff(project_key, from_agent, body)
                        self.feed.add(
                            project_key, "info",
                            f"{hid[:8]} unroutable → escalated to lead for re-routing",
                            handoff_id=hid, level="info",
                        )
            return

        target_name = (target.get("name") or "").strip()

        # 4. INTERACTIVE GUARD — Interactive means "chat-capable lead", not "never
        #    runnable". A real project can mark that lead as dispatch-capable with
        #    auto_dispatch=true while preserving the chat UI. Without explicit
        #    per-agent permission (or the global compatibility escape hatch), hold
        #    the handoff before cap/claim so it stays human-visible and unclaimed.
        designation = await _agent_designation_async(project_key, target_name)
        target_caps = target.get("capabilities") or {}
        local_auto_dispatch = await _agent_auto_dispatch_async(project_key, target_name)
        capability_auto_dispatch = _designation.normalize_boolish(
            target_caps.get("auto_dispatch")
        )
        auto_dispatch_flag = local_auto_dispatch or capability_auto_dispatch
        target_auto_dispatch = _designation.is_auto_dispatch_enabled(auto_dispatch_flag)
        target_is_interactive = self._classify_interactive(target, designation)
        if not DISPATCH_INTERACTIVE and target_is_interactive and not target_auto_dispatch:
            self._dispatched.add(key)
            self.feed.add(
                project_key, "skipped",
                f"{hid[:8]} → {target.get('display_name') or target_name} is interactive "
                f"(chat-capable lead) without auto-dispatch — left for human review",
                agent=ORCH_AGENT, handoff_id=hid, level="info",
            )
            log.info(
                "[dispatch·%s] %s → %s is interactive without auto_dispatch — left for human review",
                project_key, hid, target_name,
            )
            return
        interactive_dispatch_reason: str | None = None
        if target_is_interactive and (target_auto_dispatch or DISPATCH_INTERACTIVE):
            interactive_dispatch_reason = (
                "auto_dispatch=true"
                if target_auto_dispatch
                else "ORCH_DISPATCH_INTERACTIVE=1"
            )

        # 4b. DETERMINISTIC GUARD — a deterministic agent is a pure-code "mini" agent with
        #     NO model (app.domain.designation: not is_ai_worker). It runs on its own
        #     schedule/trigger (managed by the lead), so Dispatch must never auto-spawn an
        #     LLM run for it. Only AUTONOMOUS agents (AI workers) are auto-dispatched —
        #     interactive (above) + deterministic (here) are the two tiers Dispatch leaves
        #     alone. Skipped BEFORE the cap/claim, exactly like the interactive guard.
        if designation == "deterministic":
            self._dispatched.add(key)
            self.feed.add(
                project_key, "skipped",
                f"{hid[:8]} → {target.get('display_name') or target_name} is a deterministic "
                f"agent (no model) — runs on its own schedule, not auto-dispatched",
                agent=ORCH_AGENT, handoff_id=hid, level="info",
            )
            log.info(
                "[dispatch·%s] %s → %s is deterministic (no LLM) — not auto-dispatched",
                project_key, hid, target_name,
            )
            return

        # 4.5 PROPOSE-MODE gate (PM Relentless Beat Inc 1 — training-wheels safety).
        #     When the operator has enabled propose_mode for this project, the gate
        #     checks the handoff's persistent approval STATUS instead of relying on
        #     the in-memory _dispatched set. This is the CRITICAL fix for the
        #     approve→spawn re-gate bug: gated handoffs are NOT added to _dispatched,
        #     so every sweep re-evaluates the status from the DB:
        #       None     → first time seen; write 'awaiting', log the feed line, GATE.
        #       'awaiting' → still waiting for operator; GATE silently (no re-log).
        #       'approved' → operator clicked Approve; fall through to normal spawn.
        #     A DB write failure on None→awaiting is best-effort: status stays None
        #     and the gate retries next sweep (handoff is never stranded in _dispatched).
        #
        #     OPERATOR NOTE: when the app-DB is unreachable the propose gate now FAILS
        #     CLOSED (is_propose_mode_gate() returns True → hold), so a DB hiccup can only
        #     ever GATE autonomous work, never auto-spawn it unapproved. (Autonomy itself
        #     is also fail-safe-OFF on a down DB, so in practice the loop simply pauses.)
        if await _is_propose_mode_async(project_key):
            status = await _approval_status_async(project_key, hid)
            if status != "approved":
                if status is None:
                    # First time the gate has seen this handoff — write 'awaiting'
                    # and emit the feed line ONCE. DB failure stays None; retries
                    # next sweep.
                    with contextlib.suppress(Exception):
                        await _set_awaiting_approval_async(project_key, hid)
                    self.feed.add(
                        project_key, "awaiting_approval",
                        f"propose-mode: handoff {hid[:8]} → "
                        f"{target.get('display_name') or target_name} "
                        f"parked awaiting human approval",
                        agent=ORCH_AGENT, handoff_id=hid, level="info",
                    )
                    log.info(
                        "[dispatch·%s] propose-mode: %s → %s parked awaiting approval",
                        project_key, hid, target_name,
                    )
                # GATE — do NOT add to _dispatched; must re-evaluate every sweep
                # so it can detect when status flips to 'approved'.
                return
            # status == 'approved' → fall through to the normal spawn path below

        # 5. Concurrency cap — defer (do NOT mark dispatched) if at the ceiling.
        if self._inflight.get(project_key, 0) >= MAX_CONCURRENT:
            if key not in self._cap_deferred_logged:
                self._cap_deferred_logged.add(key)
                self.feed.add(
                    project_key, "skipped",
                    f"at cap ({MAX_CONCURRENT}) — deferring handoff {hid[:8]}",
                    handoff_id=hid, level="warn",
                )
            return

        self._cap_deferred_logged.discard(key)
        if interactive_dispatch_reason:
            self.feed.add(
                project_key, "info",
                f"{hid[:8]} → {target.get('display_name') or target_name} is interactive "
                f"and {interactive_dispatch_reason} — dispatching queued work while keeping chat enabled",
                agent=ORCH_AGENT, handoff_id=hid, level="info",
            )
            log.info(
                "[dispatch·%s] %s → %s is interactive but %s — dispatching",
                project_key, hid, target_name, interactive_dispatch_reason,
            )

        # 6. Reserve + dispatch. We do NOT claim here (E007 Autonomy v2): the spawned
        #    run-agent worker is the SOLE claimer. Its atomic Cortex claim is the
        #    durable idempotency lock — if two consoles (or two trigger paths across
        #    processes) spawn for the same handoff, exactly one worker's claim wins
        #    (PENDING→claimed) and the others exit "skipped" (rc=2), doing no work.
        #    The in-memory _dispatched set stops THIS console re-spawning the same id.
        #    (Pre-v2 this scheduler pre-claimed here; with spawn-per-task that
        #    DOUBLE-claimed — the worker's own claim then 404'd and it skipped without
        #    running. The worker is now the single claimer, consistent with running
        #    run-agent standalone.)
        self._dispatched.add(key)
        # Record the retry_count at dispatch time so a later requeue (which increments
        # it server-side via the release endpoint) is detected at the idempotency gate
        # above and re-dispatched (AV-3: close the requeue → re-dispatch loop).
        self._dispatched_retry[key] = _retry_count_of(handoff)
        self._inflight[project_key] = self._inflight.get(project_key, 0) + 1
        self.feed.add(
            project_key, "picked_up",
            f"Dispatch picked up {hid[:8]} ({source}) → dispatching {target.get('display_name') or target_name}",
            agent=ORCH_AGENT, handoff_id=hid, level="info",
        )
        log.info(
            "Dispatch picked up %s:%s → dispatching %s",
            hid[:8], project_key, target_name,
        )

        task = asyncio.create_task(
            self._dispatch_run(project_key, handoff, target),
            name=f"dispatch-run-{hid[:8]}",
        )
        self._runs.add(task)
        task.add_done_callback(self._runs.discard)

    async def _dispatch_run(self, project_key: str, handoff: dict, target: dict) -> None:
        """Kick off the target agent on the handoff (consuming the run server-side),
        then log the outcome. Releases the concurrency slot in `finally`. On error
        we ONLY log (Phase 2 escalation-to-Lead is out of scope).

        Pre-creates the run's durable RunState SSOT row (``self._runstate.start_run``)
        BEFORE spawning, so the detached worker writes spans + heartbeat + terminal
        status to that SAME row (T6) and the agent-detail pane sees real, restart-safe
        live state (T7/T8). The store is the ONE live-state path now — the in-memory
        transcript store was removed at T12."""
        hid = str(handoff.get("id") or "").strip()
        target_name = (target.get("name") or "").strip()
        target_display = target.get("display_name") or (target.get("capabilities") or {}).get("display_name") or target_name
        started = time.monotonic()
        # Resolve the harness/model up front so the run_state header shows what the
        # agent runs on (best-effort — never blocks the run). OFF the loop: this is a
        # sync psycopg2 settings read and other runs may be streaming concurrently.
        run_harness = run_model = None
        with contextlib.suppress(Exception):
            run_harness, run_model, _ = await _chat_routing_async(
                self._chat_routing_for, target, project_key
            )
        run_error: str | None = None
        try:
            repo_root = await _safe_repo_root(self._cortex, project_key)
            harness_port = self._harness
            require_run_id = True
            completed_label = "[harness-port]"
            if harness_port is None:
                # Shared command path, local mechanism: LocalHarnessAdapter preserves
                # the existing host subprocess spawn, including the legacy no-run_id
                # degrade when run-state is unavailable.
                from app.adapters.harness_local import LocalHarnessAdapter

                harness_port = LocalHarnessAdapter(
                    run_agent_script=RUN_AGENT_SCRIPT,
                    run_timeout_s=RUN_TIMEOUT_S,
                    popen=subprocess.Popen,
                )
                require_run_id = False
                completed_label = "[spawned]"

            outcome = await dispatch_worker(
                DispatchWorkerSpec(
                    project=project_key,
                    agent=target_name,
                    agent_display=target_display,
                    handoff_id=hid,
                    harness=run_harness,
                    model=run_model,
                    repo_root=repo_root,
                    lease_owner="orchestrator",
                    run_timeout_s=RUN_TIMEOUT_S,
                    require_run_id=require_run_id,
                ),
                runstate=self._runstate,
                harness_port=harness_port,
            )
            dt = time.monotonic() - started
            if not outcome.accepted:
                # The worker never started (script missing / harness-service down).
                # dispatch_worker already terminalized the pre-created run_state row.
                run_error = (outcome.error or "").strip()[-300:] or "spawn rejected"
                self.feed.add(
                    project_key, "error",
                    f"{target.get('display_name') or target_name} on {hid[:8]} "
                    f"could not be spawned: {run_error} (Watchdog supervises)",
                    agent=target_name, handoff_id=hid, level="error",
                )
            elif outcome.exit_code is None:
                # ACCEPTED but not-yet-terminal — the async "dispatched" shape (a
                # remote/async adapter). The worker reports terminal state later via
                # run-state; we do NOT mark it completed here.
                self.feed.add(
                    project_key, "dispatched",
                    f"{target.get('display_name') or target_name} dispatched on "
                    f"{hid[:8]} ({run_model or 'model'}) [harness-service]",
                    agent=target_name, handoff_id=hid, level="info",
                )
            else:
                # ACCEPTED + a terminal exit code — map it with the SAME 0/2/else
                # rules as the prior inline path (LocalHarnessAdapter awaited the worker).
                rc = outcome.exit_code
                if rc == 0:
                    self.feed.add(
                        project_key, "completed",
                        f"{target.get('display_name') or target_name} completed {hid[:8]} "
                        f"({run_model or 'model'}, {dt:.0f}s) {completed_label}",
                        agent=target_name, handoff_id=hid, level="success",
                    )
                elif rc == 2:
                    # The worker could not claim the row (already taken — a race with the
                    # other trigger path). Not a failure; nothing to do.
                    runstate = self._runstate
                    if runstate is not None:
                        with contextlib.suppress(Exception):
                            await runstate.set_status(
                            outcome.run_id,
                            "ok",
                            metadata={
                                "dispatch_outcome": "skipped",
                                "reason": "worker could not claim handoff",
                            },
                        )
                    self.feed.add(
                        project_key, "skipped",
                        f"{target.get('display_name') or target_name} could not claim "
                        f"{hid[:8]} (already taken) — skipped",
                        agent=target_name, handoff_id=hid, level="info",
                    )
                else:
                    stderr_tail = (outcome.stderr_tail or outcome.error or "").strip()
                    run_error = stderr_tail[-300:] or f"run-agent exited {rc}"
                    self.feed.add(
                        project_key, "error",
                        f"{target.get('display_name') or target_name} on {hid[:8]} "
                        f"failed (run-agent exit {rc}): {run_error}",
                        agent=target_name, handoff_id=hid, level="error",
                    )
                    # IMMEDIATE RELEASE (bounded): a failed spawn used to leave the row
                    # claimed for the Watchdog — but reclaim's no-run bar skips rows with
                    # a terminal-failed run, so recovery degraded to prune timing (a live
                    # [EMAIL] handoff sat claimed 9.9h — ultrareview 2026-07-02). Release
                    # NOW so the next reconcile can re-dispatch; the server increments
                    # retry_count, and at the same requeue ceiling as reclaim we stop
                    # releasing and leave the row claimed for the Watchdog to escalate.
                    retries = _retry_count_of(handoff)
                    if retries >= RECLAIM_MAX_RETRIES:
                        self.feed.add(
                            project_key, "error",
                            f"{hid[:8]} at retry cap ({retries} ≥ {RECLAIM_MAX_RETRIES}) "
                            "— left claimed for Watchdog escalation",
                            agent=target_name, handoff_id=hid, level="error",
                        )
                    else:
                        with contextlib.suppress(Exception):
                            released = await self._cortex.release_handoff(project_key, hid)
                            if released:
                                self.feed.add(
                                    project_key, "info",
                                    f"{hid[:8]} released after failed spawn "
                                    f"(retry {retries + 1}/{RECLAIM_MAX_RETRIES}) — will re-dispatch",
                                    agent=target_name, handoff_id=hid, level="info",
                                )
        except asyncio.CancelledError:
            run_error = "cancelled"
            raise
        except Exception as exc:
            run_error = str(exc)
            self.feed.add(
                project_key, "error",
                f"spawn of {hid[:8]} to {target_name} failed: {exc} (logged only)",
                agent=target_name, handoff_id=hid, level="error",
            )
        finally:
            # The worker writes its normal terminal status. The shared dispatch
            # command reinforces a synchronous adapter's observed process exit so a
            # killed/timed-out worker cannot leave a live row behind.
            self._inflight[project_key] = max(0, self._inflight.get(project_key, 1) - 1)
            # Legacy script PM-beat fast path. Product PM planning beats are
            # scheduled handoffs; keep this only for deployments that explicitly
            # set ORCH_PM_BEAT_SCRIPT.
            if PM_BEAT_SCRIPT:
                self._schedule_pm_beat(project_key, reason="completion")

    async def _terminalize_stranded_run(self, run_id: str | None, error: str | None) -> None:
        """Best-effort: mark a PRE-CREATED run_state row terminal ('error') when the
        WORKER never started — a rejected spawn or a spawn exception. Without this the
        row strands at 'queued' forever (no worker exists to write its terminal status),
        the run looks perpetually live in the pane, and the concurrency picture lies.

        Safe by construction: only called on the never-started paths, so it can never
        race a live worker's own terminal write. A None run_id / down store is a no-op."""
        await terminalize_unstarted_run(self._runstate, run_id, error)

    # -- PM-beat (Increment 2) — optional lead/PM assessment loop ------------

    async def _pm_beat(self, project_key: str, *, reason: str) -> None:
        """Spawn a single one-shot lead/PM beat subprocess for ``project_key``.

        COMPAT-ONLY (E007 AV-5). The canonical PM planner is the hybrid scheduled
        `pm-planning-beat` handoff (see automation_feed), which spawns the PM/lead
        through the normal dispatch funnel and gates. This script-launcher seam is
        dormant unless ORCH_PM_BEAT_SCRIPT is set; keeping it empty guarantees a
        single planner and avoids a double-planner.


        The configured beat: boot → assess active epic (handoffs done/in-flight/
        pending/blocked, waves, signals) → act (file next safe handoff OR
        escalate to lead OR log epic-done) → STOP. The beat re-triggers on the
        next completion or poll interval without requiring the harness to loop
        inside this process.

        Spawn mechanics (spawn-don't-host constraint, E007 Autonomy v2 principle):
          * We Popen ``PM_BEAT_SCRIPT <project_key>`` as an OS subprocess — exactly
            like ``_dispatch_run`` spawns ``run-agent`` workers. We do NOT call
            ``harness_runner.stream_chat`` here. No harness stream in this loop.
          * We wait off the event loop via ``asyncio.to_thread`` so the wait never
            blocks the event loop.
          * stdout is DEVNULL (beat output goes to Cortex + beat log, not here).
          * The beat runs in its own session (``start_new_session=True``).

        Rate-limiting and debounce:
          * ``_pm_beat_inflight`` (set[str]): one beat per project at a time. If a
            beat is already running for this project, we skip silently (debounce).
          * ``_pm_beat_last_ts`` (dict[str, float]): monotonic timestamp of last
            beat start. ``reason="poll"`` beats are skipped when the last beat was
            < ``PM_BEAT_MIN_INTERVAL_S`` ago. ``reason="completion"`` beats bypass
            the rate-limit (an event happened and the configured beat should re-assess) but are
            still debounced (``_pm_beat_inflight``).
        """
        # 1. Debounce — skip if a beat is already running for this project.
        if project_key in self._pm_beat_inflight:
            log.debug("[pm-beat·%s] skip: beat already in-flight (%s)", project_key, reason)
            return

        # 2. Rate-limit — poll beats only fire after PM_BEAT_MIN_INTERVAL_S.
        if reason == "poll":
            last = self._pm_beat_last_ts.get(project_key, 0.0)
            elapsed = time.monotonic() - last
            if elapsed < PM_BEAT_MIN_INTERVAL_S:
                log.debug(
                    "[pm-beat·%s] skip: poll rate-limited (%.0fs < %.0fs)",
                    project_key, elapsed, PM_BEAT_MIN_INTERVAL_S,
                )
                return

        # 3. Guard: only fire if the PM_BEAT_SCRIPT exists; degrade gracefully if
        #    the beat/ directory is absent (e.g. a stripped redistributable).
        if not os.path.exists(PM_BEAT_SCRIPT):
            log.debug("[pm-beat·%s] skip: PM_BEAT_SCRIPT not found: %s", project_key, PM_BEAT_SCRIPT)
            return

        # 4. Reserve the inflight slot and record start timestamp.
        self._pm_beat_inflight.add(project_key)
        self._pm_beat_last_ts[project_key] = time.monotonic()
        # Resolve THIS project's PM worker for the feed attribution (role-policy driven,
        # never a hardcoded worker name). pm_name = the feed `agent`; pm_label = display.
        pm_name, pm_label = await self._pm_beat_label(project_key)
        self.feed.add(
            project_key, "info",
            f"PM-beat triggered (reason={reason}) — spawning {pm_label} one-shot assessment",
            agent=pm_name, level="info",
        )
        log.info("[pm-beat·%s] spawning PM-beat (reason=%s)", project_key, reason)

        proc: subprocess.Popen | None = None
        try:
            # PM-beat (the project's PM worker) also runs in the project's folder + scope.
            pm_repo_root = await _safe_repo_root(self._cortex, project_key)
            proc = subprocess.Popen(
                [PM_BEAT_SCRIPT, project_key],
                cwd=pm_repo_root or None,
                env=_apply_project_workspace(dict(os.environ), project_key, pm_repo_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # own process group: true detachment
            )
            try:
                _out, err = await asyncio.to_thread(proc.communicate, timeout=PM_BEAT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(proc.wait)
                err = f"pm-beat timed out after {PM_BEAT_TIMEOUT_S:.0f}s"
                self.feed.add(
                    project_key, "error",
                    f"PM-beat timed out ({PM_BEAT_TIMEOUT_S:.0f}s); {pm_label}'s assessment aborted",
                    agent=pm_name, level="error",
                )
                log.warning("[pm-beat·%s] timed out", project_key)
                return
            rc = proc.returncode
            if rc == 0:
                self.feed.add(
                    project_key, "info",
                    f"PM-beat complete — {pm_label} assessed + acted",
                    agent=pm_name, level="info",
                )
                log.info("[pm-beat·%s] completed (rc=0)", project_key)
            else:
                stderr_tail = (err or "").strip()[-200:]
                self.feed.add(
                    project_key, "error",
                    f"PM-beat exited {rc}: {stderr_tail or 'no stderr'}",
                    agent=pm_name, level="error",
                )
                log.warning("[pm-beat·%s] exited %d: %s", project_key, rc, stderr_tail)
        except asyncio.CancelledError:
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
            raise
        except Exception as exc:
            self.feed.add(
                project_key, "error",
                f"PM-beat spawn failed: {exc}",
                agent=pm_name, level="error",
            )
            log.warning("[pm-beat·%s] spawn error: %s", project_key, exc)
        finally:
            self._pm_beat_inflight.discard(project_key)

    async def _pm_beat_label(self, project_key: str) -> tuple[str, str]:
        """The running PROJECT's PM worker — (feed_agent_name, display_label).

        Resolved from the project's own role policy (the registry interactive lead, via
        the shared ``domain.roles.interactive_lead``), so the PM-beat feed attributes
        the beat to whichever worker the project configured as its lead/coordinator — the harness
        names NO worker (§ pure-runtime / zero AI Workers). Falls back to the generic
        ``("pm", "PM")`` when the roster has no interactive lead / the lookup fails;
        never raises. The display label prefers the lead's ``display_name``."""
        with contextlib.suppress(Exception):
            agents = await self._cortex.get_agents(project_key)
            lead = _roles.interactive_lead(agents or [])
            if lead:
                name = (lead.get("name") or "").strip()
                label = (lead.get("display_name") or "").strip() or name or "PM"
                if name:
                    return name.lower(), label
        return "pm", "PM"

    async def _safe_project_identity(self, project_key: str) -> str | None:
        with contextlib.suppress(Exception):
            value = self._project_identity(self._cortex, project_key)
            if inspect.isawaitable(value):
                return await value
            return value
        return None


# ---------------------------------------------------------------------------
#  SSE frame + handoff-event parsing helpers.
# ---------------------------------------------------------------------------

def _parse_sse_frame(raw: bytes) -> tuple[str, dict[str, Any]] | None:
    """Parse one raw SSE frame (the bytes between blank lines) into
    (event_name, data_dict). Comment-only frames (``: ping``) and frames without
    a JSON ``data:`` payload return None. Tolerant of multi-line data."""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None
    event_name = "message"
    data_lines: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if not data_lines:
        return None
    try:
        payload = json.loads("".join(data_lines))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return event_name, payload


def _handoff_from_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Reconstruct a handoff-shaped dict from a ``handoff_created`` event frame.

    The Cortex event stream entry is ``{"id", "fields": {...}}`` where
    ``fields.detail`` is the JSON ``cortex.handoff_lifecycle.v1`` blob carrying
    ``handoff_id``, ``to_agent``, ``to_role``, ``status``, ``priority``, etc. We map
    that onto the same key shape ``get_handoffs`` returns so the dispatch funnel is
    source-agnostic. Returns None if the detail can't be read."""
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return None
    detail_raw = fields.get("detail")
    detail: dict[str, Any] = {}
    if isinstance(detail_raw, str):
        with contextlib.suppress(ValueError):
            parsed = json.loads(detail_raw)
            if isinstance(parsed, dict):
                detail = parsed
    elif isinstance(detail_raw, dict):
        detail = detail_raw

    hid = str(detail.get("handoff_id") or "").strip()
    if not hid:
        return None
    return {
        "id": hid,
        "summary": fields.get("summary") or "",
        "from_agent": detail.get("from_agent"),
        "from_role": detail.get("from_role"),
        "to_agent": detail.get("to_agent"),
        "to_role": detail.get("to_role"),
        "status": detail.get("status") or "pending",
        "priority": detail.get("priority"),
        "claimed_by": detail.get("claimed_by"),
        "project_id": None,
    }


def _is_pending(handoff: dict[str, Any]) -> bool:
    """True if a handoff is a fresh, dispatchable PENDING row (unclaimed, not
    closed). A claimed/completed/cancelled row is skipped."""
    status = (handoff.get("status") or "").strip().lower()
    if status and status not in _PENDING_STATUSES:
        return False
    if handoff.get("claimed_by"):
        return False
    return True


def _is_claimed(handoff: dict[str, Any]) -> bool:
    """True if a handoff is CLAIMED / in-flight (a reclaim candidate, before the
    age + no-run guards). A row counts as claimed when its status is in the claimed
    set OR it carries a ``claimed_by`` (the live list marks both). A terminal row
    (completed/cancelled/…) is NEVER claimed — never a reclaim candidate."""
    status = (handoff.get("status") or "").strip().lower()
    if status in _TERMINAL_STATUSES:
        return False
    if status in _CLAIMED_STATUSES:
        return True
    # No explicit claimed status but a claimed_by is set → treat as claimed
    # (defensive: the list marks claimed rows with claimed_by even when the status
    # string varies across Cortex versions).
    return bool(handoff.get("claimed_by"))


def _claimed_age_seconds(handoff: dict[str, Any]) -> float | None:
    """Seconds since the handoff was claimed, from whatever timestamp the dict
    carries — ``claimed_at`` first (the real claim time, present on the single-GET
    shape), then ``updated_at`` (last mutation, the claim), then ``created_at`` (the
    only stamp on the LIST shape — a fair lower bound, since a row created days ago
    and still claimed is at least that old). Returns None when NO usable timestamp is
    present OR none parses — and the reclaim guard treats None as "no evidence of
    age" → it NEVER reclaims without an age. Pure + total (never raises)."""
    for field in ("claimed_at", "updated_at", "created_at"):
        raw = handoff.get(field)
        if not raw:
            continue
        age = _age_seconds_from_ts(raw)
        if age is not None:
            return age
    return None


def _retry_count_of(handoff: dict[str, Any]) -> int:
    """The handoff's ``retry_count`` (how many times it has already been requeued),
    coerced to a non-negative int. The Cortex claimed list carries it (exposed by
    68e55a0); a missing/garbage value reads as 0. Mirrors ``watchdog._retry_count`` —
    both the reclaim path and the watchdog compare against the same ceiling. Pure +
    total (never raises)."""
    try:
        n = int(handoff.get("retry_count") or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _age_seconds_from_ts(raw: Any) -> float | None:
    """Parse a Cortex timestamp (ISO-8601 or the ``YYYY-MM-DD HH:MM:SS.ssss+00``
    Postgres form) and return seconds elapsed since then. None on any parse failure
    (mirrors the watchdog's tolerant ``Claimed at:`` normalization). Never raises."""
    if not isinstance(raw, str):
        return None
    ts = raw.strip()
    if not ts:
        return None
    # Normalize the Postgres "2026-06-21 23:23:55.737699+00" form to ISO-8601.
    ts = ts.replace(" ", "T", 1)
    if ts.endswith("+00"):
        ts = ts[:-3] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # A naive timestamp is assumed UTC (the Cortex convention) so the subtraction is
    # always tz-aware vs tz-aware.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


# ---------------------------------------------------------------------------
#  WAVE gating (E007 Phase 1.5) — dependency-sequenced dispatch.
#
#  The plan (app-DB `handoff_orchestration`) tags some handoffs with an epic + a
#  wave. Dispatch permits only the LOWEST wave (per epic) that still has incomplete
#  handoffs; a wave > 0 handoff NEVER dispatches before its prior waves complete.
#  A handoff with NO plan row is wave 0 → dispatched immediately (Phase-1 behaviour
#  preserved). These pure helpers compute the gate from a plan map + a handoff list.
# ---------------------------------------------------------------------------

def _is_handoff_complete(handoff: dict[str, Any]) -> bool:
    """True if a handoff is COMPLETE/terminal for wave gating.

    A wave advances only when every one of its handoffs is complete by THIS test.
    Terminal status (completed/closed/cancelled/…) counts as complete. A still-open
    or claimed-but-in-flight handoff is NOT complete (a later wave keeps waiting).
    A handoff we can't see in the live list at all is handled by the caller (a
    planned id with no live row is treated as not-yet-complete → its wave waits,
    never crashes)."""
    status = (handoff.get("status") or "").strip().lower()
    return status in _TERMINAL_STATUSES


def _plan_wave(plan: dict[str, dict[str, Any]], hid: str) -> int:
    """The planned wave for a handoff id — 0 when it has no plan row (Phase-1
    dispatch-immediately). Defensive against a malformed row (→ 0)."""
    row = plan.get(hid) or {}
    try:
        return max(0, int(row.get("wave", 0)))
    except (TypeError, ValueError):
        return 0


def _plan_epic(plan: dict[str, dict[str, Any]], hid: str) -> str | None:
    """The planned epic for a handoff id — None when it has no plan row / no epic."""
    row = plan.get(hid) or {}
    epic = row.get("epic")
    return epic if (isinstance(epic, str) and epic.strip()) else None


def _active_wave_for_epic(
    epic: str | None,
    plan: dict[str, dict[str, Any]],
    handoffs_by_id: dict[str, dict[str, Any]],
) -> int | None:
    """The LOWEST wave of `epic` that still has an INCOMPLETE handoff (the only
    wave allowed to dispatch right now), or None when every wave of the epic is
    complete (nothing left to dispatch for it).

    Gaps in wave numbers are fine — we walk the waves that actually exist in
    ascending order and return the first one that isn't fully complete. A planned
    handoff whose live row is missing (deleted/not-yet-visible) counts as
    incomplete, so its wave WAITS rather than the loop skipping ahead (fail-safe:
    never run a later wave on incomplete-but-invisible earlier work). NEVER raises.
    """
    # Collect this epic's waves → list of handoff ids, from the plan.
    waves: dict[int, list[str]] = {}
    for hid, row in plan.items():
        row_epic = row.get("epic")
        row_epic = row_epic if (isinstance(row_epic, str) and row_epic.strip()) else None
        if row_epic != epic:
            continue
        w = _plan_wave(plan, hid)
        waves.setdefault(w, []).append(hid)

    if not waves:
        return None  # nothing planned for this epic

    for w in sorted(waves):
        for hid in waves[w]:
            live = handoffs_by_id.get(hid)
            # A planned handoff with no live row → treat as incomplete (its wave
            # waits). A live row that isn't terminal → incomplete.
            if live is None or not _is_handoff_complete(live):
                return w  # this is the active (lowest-incomplete) wave
    return None  # every wave of this epic is fully complete


def _wave_gate_ok(
    handoff: dict[str, Any],
    plan: dict[str, dict[str, Any]],
    handoffs_by_id: dict[str, dict[str, Any]],
) -> tuple[bool, int, str | None, int | None]:
    """Decide whether a candidate handoff may dispatch under wave gating.

    Returns ``(allowed, wave, epic, active_wave)``:
      * ``allowed``     — True iff the handoff is in its epic's currently-active
                          wave (or is wave 0 / unplanned → always allowed, Phase 1).
      * ``wave``        — the handoff's own planned wave (0 if unplanned).
      * ``epic``        — the handoff's planned epic (None if unplanned).
      * ``active_wave`` — the epic's active wave right now (None if no plan / all
                          complete), for the activity-feed framing.

    A wave-0 / unplanned handoff is ALWAYS allowed (preserves Phase-1 behaviour).
    A wave > 0 handoff is allowed ONLY when it equals its epic's active wave, i.e.
    all lower waves of that epic are complete. Pure + total (never raises)."""
    hid = str(handoff.get("id") or "").strip()
    wave = _plan_wave(plan, hid)
    epic = _plan_epic(plan, hid)

    # Wave 0 (or unplanned) → Phase-1 dispatch-immediately. No epic gating.
    if wave <= 0:
        return True, 0, epic, None

    active = _active_wave_for_epic(epic, plan, handoffs_by_id)
    # Allowed only if THIS handoff's wave is the epic's active (lowest-incomplete)
    # wave. If active is a LOWER wave, prior waves are not done → not allowed yet.
    allowed = (active is not None) and (wave == active)
    return allowed, wave, epic, active
