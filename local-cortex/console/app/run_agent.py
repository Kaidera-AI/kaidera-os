"""run-agent: run ONE agent on ONE handoff in its own process, then exit.

A spawnable, self-contained unit (E007 Autonomy v2). Composes the existing
building blocks (routing + harness_runner + CortexClient); all durable state
goes through Cortex. Exit code: 0 = completed, 1 = failed, 2 = could-not-claim.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

# Live-trail cap: a spawned worker narrates its thinking + tool calls to Cortex as it
# runs (so the console pane shows the work live, not just a status). Each step is a
# blocking cortex-log subprocess, so we bound the count — beyond this the run still
# finishes and the FULL reply is persisted in the TRANSCRIPT row; only the live
# play-by-play is truncated (logged once, not silently).
MAX_LIVE_STEPS = 40

# Run-state heartbeat cadence (Milestone 1, T6). While the harness streams, a
# background task bumps `runstate.heartbeat(run_id, pid=…)` every this-many seconds
# — the LIVENESS signal the watchdog will read (real liveness, not CLI-text
# grepping). Env-overridable; bounded so a bad value can't hot-spin or stall.
def _hb_interval() -> float:
    try:
        return max(1.0, min(60.0, float(os.environ.get("RUNSTATE_HEARTBEAT_S", "").strip() or 10.0)))
    except (TypeError, ValueError):
        return 10.0


HEARTBEAT_INTERVAL_S = _hb_interval()


@dataclass
class RunResult:
    status: str                       # "completed" | "failed" | "skipped"
    text: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    harness: str | None = None
    model: str | None = None


async def run_one(
    name: str,
    handoff_id: str,
    project: str,
    *,
    cortex: Any,
    runner: Any,
    routing: Callable[[dict, str], tuple[str, str | None, str | None]],
    task_summary: str,
    system: str | None = None,
    runstate: Optional[Any] = None,
    run_id: Optional[str] = None,
) -> RunResult:
    """Claim → run harness → complete. Pure-ish: collaborators injected.

    RUN-STATE WRITES (Milestone 1, T6 — the live-state path): when ``runstate``
    (a RunStatePort) AND ``run_id`` are supplied, this writes the run's live state to
    the app-DB store ALONGSIDE the durable ``cortex.log`` audit (STARTED/STEP/
    TRANSCRIPT/COMPLETED — UNCHANGED). The ``~/.cortex-feed`` display feed was removed
    at T12; the store IS the live display surface now (read via T7, pushed via T8):
      * at start              → ``set_status(run_id, "running")``
      * per thinking/tool/output span → ``append_output(run_id, kind=…, text=…)``
      * a background heartbeat → ``heartbeat(run_id, pid=…)`` every
        ``HEARTBEAT_INTERVAL_S`` while streaming (the new liveness signal)
      * on success → stamp final totals via ``heartbeat(run_id, tokens…/cost…)`` then
        ``set_status(run_id, "ok")``; on failure → ``set_status(run_id, "error", error=…)``
        (tokens/cost live on the header via ``heartbeat`` — ``set_status`` takes only
        status/error/metadata)

    GRACEFUL-DEGRADE: every store call is wrapped — a store that RAISES (down DB) or
    is ``None`` (or no ``run_id``: the legacy spawn argv) is a clean no-op; the run +
    the Cortex audit proceed regardless. The detached worker must NEVER be broken by
    the store."""
    agent = {"name": name}

    # Run-state store helpers (T6). All best-effort: a None store, a missing run_id,
    # or a raising store is a silent no-op — the store must never break the run.
    _rs_on = runstate is not None and bool(run_id)

    async def _rs_status(status: str, **kw: Any) -> None:
        if not _rs_on:
            return
        with contextlib.suppress(Exception):
            await runstate.set_status(run_id, status, **kw)

    async def _rs_totals(*, tokens_in: Any, tokens_out: Any, cost_est_usd: Any) -> None:
        """Persist the run's FINAL token/cost totals on the run header. These live on
        the run_state header via ``heartbeat`` (the RunStatePort contract — ``set_status``
        takes ONLY status/error/metadata, NOT tokens). Calling ``set_status('ok',
        tokens_in=…)`` raises a TypeError that the suppress below swallows, which left
        the terminal write un-applied and the run pinned at 'running' forever (the #1
        autonomy blocker). Best-effort, like every other store write."""
        if not _rs_on:
            return
        with contextlib.suppress(Exception):
            await runstate.heartbeat(
                run_id, tokens_in=tokens_in, tokens_out=tokens_out,
                cost_est_usd=cost_est_usd, pid=os.getpid(),
            )

    rs_seq = 0  # writer-chosen monotonic span order ((run_id, seq) is idempotent in the store).

    async def _rs_span(kind: str, text: str) -> None:
        nonlocal rs_seq
        if not _rs_on or not text:
            return
        rs_seq += 1
        with contextlib.suppress(Exception):
            await runstate.append_output(run_id, seq=rs_seq, kind=kind, text=text)

    claimed = await cortex.claim_handoff(handoff_id, name)
    if not claimed:
        # Never started — do NOT write a 'running' run_state row for a run that
        # didn't run (the orchestrator pre-created it 'queued' in T5; a failed claim
        # leaves it queued for the watchdog to reconcile, not falsely 'running').
        return RunResult(status="skipped", error="could not claim")

    harness, model, reasoning = routing(agent, project)
    sys_prompt = system or f"You are {name}, a {project} agent. Do the work and reply concisely."

    assembled: list[str] = []        # the REPLY (the agent's answer) — delta + result text
    think_buf: list[str] = []        # current thinking block; flushed as one step on the next action
    step_seq = 0                     # ordered live-trail counter (thinking + tool steps)
    result = RunResult(status="completed", harness=harness, model=model)
    started = False

    # Background heartbeat (T6) — bumps run_state.heartbeat_at on a cadence while the
    # harness streams, so the watchdog reads REAL liveness. Best-effort + cancelled
    # in `finally`; only runs when the store + run_id are present.
    async def _heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            with contextlib.suppress(Exception):
                await runstate.heartbeat(run_id, pid=os.getpid())

    hb_task: asyncio.Task | None = None

    async def _flush_thought() -> None:
        """Persist the accumulated thinking as ONE ordered live step (thinking streams
        token-by-token, but each cortex-log is a subprocess, so we log per-thought not
        per-token). Capped by MAX_LIVE_STEPS; the full reply still lands in TRANSCRIPT."""
        nonlocal step_seq
        if not think_buf:
            return
        thought = "".join(think_buf).strip()
        think_buf.clear()
        if thought and step_seq < MAX_LIVE_STEPS:
            step_seq += 1
            await cortex.log(name, "checkin", f"{name} STEP {handoff_id} #{step_seq:03d} think {thought[:400]}", project)
        # Store: capture the thinking span (the live display surface). NOT bounded by
        # MAX_LIVE_STEPS — every thought goes to the store (bounded in the adapter).
        if thought:
            await _rs_span("thinking", thought)

    try:
        async for ev in runner.stream_chat(
            task_summary,
            model=model,
            system=sys_prompt,
            harness=harness,
            reasoning=reasoning,
            run_context="autonomous",
        ):
            kind = ev.get("type")
            if kind in ("thinking", "tool", "delta") and not started:
                started = True
                await cortex.log(name, "checkin", f"{name} STARTED {handoff_id}", project)
                # Store: mark the run RUNNING + start the heartbeat (T6).
                await _rs_status("running")
                if _rs_on and hb_task is None:
                    hb_task = asyncio.create_task(_heartbeat_loop())
            if kind == "thinking":
                think_buf.append(ev.get("text", ""))
            elif kind == "tool":
                await _flush_thought()                       # the thought that led to this action
                tool_txt = (ev.get("text") or ev.get("name") or "tool").strip()
                if step_seq < MAX_LIVE_STEPS:
                    step_seq += 1
                    await cortex.log(name, "checkin", f"{name} STEP {handoff_id} #{step_seq:03d} tool {tool_txt[:400]}", project)
                # Store: every tool call goes to the store (NOT bounded by MAX_LIVE_STEPS).
                await _rs_span("tool", tool_txt + "\n")
            elif kind == "delta":
                await _flush_thought()                       # thinking before the reply
                delta_text = ev.get("text", "")
                assembled.append(delta_text)
                if delta_text:
                    await _rs_span("output", delta_text)
            elif kind == "result":
                result.tokens_in = ev.get("tokens_in")
                result.tokens_out = ev.get("tokens_out")
                result.cost_usd = ev.get("cost_usd")
                txt = ev.get("text") or ""
                if txt and txt.strip() != "".join(assembled).strip():
                    assembled.append(txt)
                    await _rs_span("output", txt)
            elif kind == "error":
                result.status = "failed"
                result.error = ev.get("message", "harness error")
        await _flush_thought()                               # trailing thought with no following action
    except Exception as exc:
        result.status = "failed"
        result.error = f"run crashed: {exc}"
    finally:
        # Stop the heartbeat before the terminal status write (best-effort).
        if hb_task is not None:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hb_task

    result.text = "".join(assembled)
    # Full reply persisted to Cortex LTM (raised cap — this is the never-forget transcript,
    # paired with the live STEP trail above so the console can replay the whole run).
    await cortex.log(name, "decision", f"{name} TRANSCRIPT {handoff_id}: {result.text[:8000]}", project)
    if result.status == "completed":
        await cortex.complete_handoff(handoff_id)
        # Distinct SUCCESS marker. The transcript above is logged on failure too, so
        # it cannot prove success. This marker is written ONLY on the completed path,
        # so the PM watchdog can reliably tell a worker that SUCCEEDED (but may have
        # hit a silent complete-failure, leaving the handoff still "claimed") apart
        # from one that errored — and re-complete the former without guessing.
        await cortex.log(name, "decision", f"{name} COMPLETED {handoff_id}", project)
        # App-side reliable filing: file any handoffs the agent EMITTED in its reply.
        # The PARENT process files them deterministically via the Cortex API, so a
        # planner's decomposition lands even if the model confabulated that its own
        # shell filing was blocked. Best-effort; gated downstream by propose_mode.
        with contextlib.suppress(Exception):
            filed = await file_emitted_handoffs(cortex, name, result.text)
            if filed:
                await cortex.log(
                    name, "decision",
                    f"{name} FILED {len(filed)} handoff(s) from {handoff_id}: {', '.join(filed)}",
                    project,
                )
        # Store: stamp the run's FINAL telemetry totals on the header (via heartbeat —
        # the RunStatePort home for tokens/cost), THEN flip the terminal status. Keeping
        # tokens off set_status matches the adapter contract; passing them there raised a
        # swallowed TypeError that pinned the run at 'running' (T6).
        await _rs_totals(tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                         cost_est_usd=result.cost_usd)
        await _rs_status("ok")
    else:
        # Store: terminal ERROR with the failure detail (T6).
        await _rs_status("error", error=result.error)
    return result


# ---------------------------------------------------------------------------
#  WorkerCortex — real cortex collaborator for the dedicated run-agent process
# ---------------------------------------------------------------------------

class WorkerCortex:
    """Worker's Cortex collaborator: claim via CortexClient (HTTP), complete + log
    via the cortex-* CLIs (CortexClient has no complete/log).

    EVENT-LOOP SAFETY: ``complete_handoff`` and ``log`` are AWAITED from the streaming
    ``run_one`` loop (per-thought ``STEP`` logging is the hot path), so the blocking
    ``subprocess.run`` is OFFLOADED to a worker thread via ``asyncio.to_thread`` — a
    synchronous CLI call on the loop thread stalls the harness stream's pipe drain
    (the v1 pipe-drain freeze under load). The CLI call + its timeout/return are
    otherwise byte-for-byte unchanged; only the thread it runs on moved."""

    def __init__(self, project: str, client: Any) -> None:
        self._project = project
        self._client = client

    async def claim_handoff(self, handoff_id: str, agent: str) -> bool:
        return await self._client.claim_handoff(self._project, handoff_id, agent)

    async def complete_handoff(self, handoff_id: str) -> None:
        # Offload the blocking CLI to a thread so it never stalls the event loop
        # (the streaming run awaits this; a sync subprocess here blocks pipe drain).
        await asyncio.to_thread(
            subprocess.run,
            ["cortex-handoff", "--complete", handoff_id],
            capture_output=True, text=True, timeout=20,
            env=_cortex_cli_env(self._project), cwd=_cortex_cli_cwd(),
        )

    async def log(self, agent: str, event_type: str, summary: str, project: str | None = None) -> None:
        # cortex-log supports decision/lesson; map worker check-ins to "decision"
        # (the STARTED/TRANSCRIPT marker is already in the summary text).
        et = "lesson" if event_type == "lesson" else "decision"
        # Offload the blocking CLI to a thread — per-thought STEP logging runs inside
        # the harness stream loop, so a sync subprocess here is the worst offender for
        # blocking the event loop / stalling the stream pipe drain.
        await asyncio.to_thread(
            subprocess.run,
            ["cortex-log", agent, et, summary],
            capture_output=True, text=True, timeout=20,
            env=_cortex_cli_env(project or self._project), cwd=_cortex_cli_cwd(),
        )

    async def create_handoff(self, from_agent: str, body: dict[str, Any]) -> dict[str, Any]:
        """File a handoff via the Cortex HTTP API (registered project writer).

        Used by the app-side reliable-filing path: the PARENT worker process (this,
        unsandboxed + deterministic) files the handoffs the AGENT decomposed, so
        autonomous planning works regardless of whether the agent's own shell filing
        succeeds. Returns the Cortex response (`{ok:false,error}` on failure)."""
        return await self._client.create_handoff(self._project, from_agent, body)


# ---------------------------------------------------------------------------
#  App-side reliable handoff filing — the agent EMITS, the app FILES
# ---------------------------------------------------------------------------
#  An autonomous planner (PM) decomposes work into handoffs, but a model can
#  confabulate that its own shell filing is "sandboxed" and give up (observed:
#  gpt-5.5 hallucinated a read-only sandbox 3x and filed nothing, though writes
#  actually work). Contract: the agent EMITS its decomposition as ONE block of
#  JSON handoff specs; this PARENT process (unsandboxed, deterministic) FILES each
#  via the Cortex API. Robust regardless of the model's shell behavior. Filed
#  handoffs are gated downstream by propose_mode at dispatch — never a gate bypass.

MAX_EMITTED_HANDOFFS = int(os.environ.get("WORKER_MAX_EMITTED_HANDOFFS", "12"))
_EMIT_HANDOFFS_RE = re.compile(
    r"===\s*FILE-HANDOFFS\s*===\s*(.*?)\s*===\s*END-FILE-HANDOFFS\s*===",
    re.DOTALL | re.IGNORECASE,
)
# Body keys the Cortex POST /handoffs writer accepts (mirrors automation_feed).
_HANDOFF_BODY_KEYS = (
    "from_role", "to_role", "to_agent", "priority", "summary",
    "branch", "files_changed", "verification", "next_steps", "context",
)


def parse_emitted_handoffs(text: str) -> list[dict[str, Any]]:
    """Extract handoff specs from an agent reply's ``===FILE-HANDOFFS=== [...] ===END-FILE-HANDOFFS===``
    block. Tolerates a fenced ```json inside. Returns [] if absent/malformed (never raises).
    A spec needs a non-empty ``summary`` AND ``to_role`` to be dispatchable."""
    m = _EMIT_HANDOFFS_RE.search(text or "")
    if not m:
        return []
    blob = m.group(1).strip()
    blob = re.sub(r"\A```[a-zA-Z0-9]*\s*|\s*```\Z", "", blob).strip()  # strip a code fence
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    specs: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("summary") or "").strip() and str(item.get("to_role") or "").strip():
            specs.append(item)
        if len(specs) >= MAX_EMITTED_HANDOFFS:
            break
    return specs


def _handoff_body_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Map an agent handoff spec to the Cortex POST /handoffs body (accepted keys only)."""
    body = {k: spec[k] for k in _HANDOFF_BODY_KEYS if spec.get(k) not in (None, "")}
    body["summary"] = str(spec.get("summary") or "").strip()[:500]
    body["to_role"] = str(spec.get("to_role") or "").strip().lower()
    body.setdefault("priority", str(spec.get("priority") or "medium").strip().lower())
    # Fold the planner's acceptance + wave sequencing into context/verification so the
    # downstream worker sees them even though they aren't first-class handoff columns.
    accept = str(spec.get("acceptance") or "").strip()
    if accept and not body.get("verification"):
        body["verification"] = accept[:1000]
    if spec.get("wave") is not None:
        body["context"] = (f"[wave {spec.get('wave')}] " + str(body.get("context") or "")).strip()
    return body


async def file_emitted_handoffs(cortex: Any, from_agent: str, text: str) -> list[str]:
    """File every handoff the agent emitted in ``text``; return the created ids.
    Best-effort: a single failed create is skipped, never aborts the rest."""
    filed: list[str] = []
    for spec in parse_emitted_handoffs(text):
        try:
            resp = await cortex.create_handoff(from_agent, _handoff_body_from_spec(spec))
        except Exception:
            continue
        if isinstance(resp, dict) and resp.get("ok") is not False:
            hid = resp.get("id") or resp.get("handoff_id") or resp.get("handoffId")
            if hid:
                filed.append(str(hid))
    return filed


# ---------------------------------------------------------------------------
#  Bootstrap helpers
# ---------------------------------------------------------------------------

def _cortex_cli_env(project: str, workspace: str | None = None) -> dict[str, str]:
    """Env for cortex-* subprocesses launched by this detached worker.

    The parent spawn already scopes the worker process, but every cortex-* call should
    carry that scope explicitly so a CLI invocation cannot fall back to the console
    project or shell cwd when the worker is running inside a project workspace.
    """
    env = dict(os.environ)
    if project:
        env["CORTEX_PROJECT"] = project
    env.setdefault("CORTEX_API_URL", "http://127.0.0.1:8501")
    ws = (workspace or env.get("KAIDERA_AGENT_WORKSPACE") or "").strip()
    if ws:
        env["KAIDERA_AGENT_WORKSPACE"] = ws
        env["PATH"] = os.path.join(ws, ".agents", "scripts") + os.pathsep + env.get("PATH", "")
    return env


def _cortex_cli_cwd(workspace: str | None = None) -> str | None:
    ws = (workspace or os.environ.get("KAIDERA_AGENT_WORKSPACE") or "").strip()
    return ws if ws and os.path.isdir(ws) else None

def _identity_dirs() -> list[str]:
    """The directories searched for an agent's persona file, in priority order.

    With ``AGENT_IDENTITY_DIR`` set, ONLY that dir is used (explicit override wins).
    Otherwise the PROJECT WORKSPACE (``KAIDERA_AGENT_WORKSPACE`` — set by the dispatch
    spawn for every project worker) is searched FIRST, then this console's own repo:

      1. ``<workspace>/.agents/agents/``   (generated canonical identities)
      2. ``<workspace>/agents/``           (turnkey layout — per-agent subdirs)
      3. ``<workspace>/docs/agents/``      (turnkey persona docs)
      4. ``<console repo>/.agents/agents/`` + ``<console repo>/agents/`` (fallback)

    Workspace-first matters: a marketing/turnkey worker (saul, gem, cole) must boot
    with the TURNKEY's persona, not a generic prompt — without this every
    console-dispatched project worker ran identity-less (ultrareview 2026-07-02)."""
    override = os.environ.get("AGENT_IDENTITY_DIR")
    if override:
        return [override]
    dirs: list[str] = []
    ws = (os.environ.get("KAIDERA_AGENT_WORKSPACE") or "").strip()
    if ws and os.path.isdir(ws):
        dirs += [
            os.path.join(ws, ".agents", "agents"),
            os.path.join(ws, "agents"),
            os.path.join(ws, "docs", "agents"),
        ]
    here = os.path.dirname(os.path.abspath(__file__))                # .../console/app
    repo = os.path.join(here, "..", "..", "..")                      # -> repo root
    dirs += [
        os.path.join(repo, ".agents", "agents"),                     # shipped/generated dir
        os.path.join(repo, "agents"),                                # legacy code default
    ]
    return dirs


def _identity_candidates(base: str, name: str, *, missions: bool = False) -> list[str]:
    """Persona file candidates under ``base`` for agent ``name``, most-specific first.

    Pass 1 (``missions=False``): exact PERSONA names only — the generated canonical
    ``<NAME>_IDENTITY.md``, ``<name>.md``, and the turnkey ``<Title>/<name>.md``.
    Pass 2 (``missions=True``): loop/tick MISSION files (``<Title>/<name>*.md``) as a
    last resort — better than bootless, but a persona doc always wins first (marlow's
    hourly-loop mission must not shadow his real charter in ``docs/agents/marlow.md``)."""
    low = name.lower()
    title = low.capitalize()
    if not missions:
        return [
            os.path.join(base, f"{name.upper()}_IDENTITY.md"),
            os.path.join(base, f"{low}.md"),
            os.path.join(base, title, f"{low}.md"),
        ]
    try:
        import glob as _glob
        cands = sorted(_glob.glob(os.path.join(base, title, f"{low}*mission*.md")))
        cands += [p for p in sorted(_glob.glob(os.path.join(base, title, f"{low}*.md")))
                  if p not in cands]
        return cands
    except Exception:
        return []


def _agent_identity(name: str) -> str | None:
    """The agent's RICH persona — searched WORKSPACE-FIRST (see ``_identity_dirs``),
    exact persona files across ALL dirs first, then turnkey mission files as a
    fallback (``_identity_candidates``). Without this the worker runs with only the
    minimal cortex-boot line and cannot actually act in its role. Returns the file
    text, or None when no persona exists in any searched location."""
    text: str | None = None
    dirs = _identity_dirs()
    for missions in (False, True):
        for base in dirs:
            for path in _identity_candidates(base, name, missions=missions):
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                    break
                except OSError:
                    continue
            if text is not None:
                break
        if text is not None:
            break
    if text is None:
        return None
    # Strip leading YAML frontmatter ("---\n...\n---"): it is file metadata, not
    # persona. CRITICAL — a system prompt that STARTS with "---" makes the harness
    # CLIs' arg parser treat it as a flags block, so the child exits in ~5s with an
    # empty reply (this silently broke identity-bearing agents on BOTH pi and
    # claude-code, while bodyless agents appeared to run fine).
    # Keep only the persona body.
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL).strip()
    return text or None


def _core_repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))              # .../console/app
    return os.path.realpath(os.path.join(here, "..", "..", ".."))  # -> repo root


def _skill_search_roots() -> list[tuple[str, str]]:
    """Skill roots, workspace-first then Kaidera OS core.

    A dispatched worker may run inside a turnkey/project workspace whose Cortex boot
    manifest points at workspace-local skills. Keep the same confinement guarantee
    per root: body refs can only resolve under that root's .agents/skills tree.
    """
    roots: list[str] = []
    ws = (os.environ.get("KAIDERA_AGENT_WORKSPACE") or "").strip()
    if ws:
        roots.append(os.path.realpath(ws))
    roots.append(_core_repo_root())
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        if not root or root in seen:
            continue
        seen.add(root)
        out.append((root, os.path.realpath(os.path.join(root, ".agents", "skills"))))
    return out


def _resolve_skill_ref(body_ref: str | None) -> str | None:
    if not body_ref:
        return None
    if os.path.isabs(body_ref):
        return None
    if ".." in body_ref.replace("\\", "/").split("/"):
        return None
    for root, skills_root in _skill_search_roots():
        resolved = os.path.realpath(os.path.join(root, body_ref))
        try:
            if os.path.commonpath([resolved, skills_root]) != skills_root:
                continue
        except ValueError:
            continue
        if os.path.isfile(resolved):
            return resolved
    return None


def _skill_body(body_ref: str | None) -> str:
    """Read a skill's SKILL.md from its body_ref, CONFINED to ``.agents/skills/``,
    frontmatter-stripped + capped. Returns '' on any miss — injection is best-effort.

    SECURITY (path traversal): ``body_ref`` comes from a DB row that a project writer
    can set, and this body is spliced verbatim into the worker's SYSTEM prompt. Without
    confinement a malicious row (``/etc/passwd``, ``../../secret``) would read arbitrary
    host files into the agent's instructions. We therefore reject absolute paths and
    any ``..`` component up front, then realpath the candidate and require it to live
    under a workspace/core skills root (symlink-safe). Anything outside ⇒ ''."""
    resolved = _resolve_skill_ref(body_ref)
    if not resolved:
        return ""
    try:
        with open(resolved, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL).strip()
    return text[:6000]


# Skill-selection tuning. The cap is env-overridable (the orchestrator may widen it
# for a heavy task), but the _select_skills `max_n` PARAM always wins (testability).
def _max_skills() -> int:
    """How many on-demand skills to inject per worker (default 3). Bounded so a bad
    env value can't blow the context budget or zero-out selection."""
    try:
        return max(1, min(20, int(os.environ.get("KAIDERA_MAX_SKILLS", "").strip() or 3)))
    except (TypeError, ValueError):
        return 3


# Stopwords dropped before keyword overlap — generic words that carry no routing
# signal (every handoff says "the"/"need"/"this"). Kept tiny on purpose: the goal is
# to strip noise, not to do real NLP. Tokens <3 chars are also dropped (see _tokenize).
_SKILL_STOPWORDS = frozenset({
    "the", "and", "for", "you", "your", "with", "this", "that", "from", "into",
    "need", "want", "use", "using", "have", "has", "are", "was", "were", "will",
    "can", "should", "must", "please", "let", "its", "but", "not", "all", "any",
    "out", "get", "got", "via", "per", "run", "task", "work", "skill", "skills",
})


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on any non-alphanumeric run, drop tokens <3 chars + stopwords.
    Returns a SET (we score on presence/overlap, not frequency). Total: bad input → {}."""
    if not text:
        return set()
    toks = re.split(r"[^a-z0-9]+", text.lower())
    return {t for t in toks if len(t) >= 3 and t not in _SKILL_STOPWORDS}


def _skill_frontmatter(body_ref: str | None) -> dict[str, Any]:
    """Read + parse ONLY the YAML frontmatter of a skill's SKILL.md (reusing _skill_body's
    confined path resolution for body_ref). Returns the parsed mapping, or {} on any miss
    (no body_ref, path escapes the skills root, file gone, no frontmatter, parse error).

    Used by the on-demand selector to read the routing fields (`tags`, `when_to_load`,
    `when_not_to_load`). Tolerates missing PyYAML via a minimal `key: value` +
    `tags: [a, b]` / `- item` parser. PURE + TOTAL — never raises."""
    resolved = _resolve_skill_ref(body_ref)
    if not resolved:
        return {}
    try:
        with open(resolved, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    m = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    # Prefer PyYAML when present; fall back to a minimal hand-parser otherwise (the spec
    # tolerates missing PyYAML). Either path is wrapped so a parse error → {}.
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        return _parse_frontmatter_min(block)


def _parse_frontmatter_min(block: str) -> dict[str, Any]:
    """Minimal `key: value` frontmatter parser for the no-PyYAML path. Handles scalars,
    inline lists (`tags: [a, b]`), and dash-item lists following a bare `key:`. Good
    enough for the selector's routing fields; never raises (total)."""
    out: dict[str, Any] = {}
    cur_list_key: str | None = None
    try:
        for raw in block.splitlines():
            line = raw.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            stripped = line.strip()
            # A dash-item continues the most recent bare-key list.
            if stripped.startswith("- ") and cur_list_key is not None:
                out.setdefault(cur_list_key, [])
                if isinstance(out[cur_list_key], list):
                    out[cur_list_key].append(stripped[2:].strip().strip("'\""))
                continue
            if ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if not val:
                # Bare `key:` — a dash-item list (or empty value) may follow.
                cur_list_key = key
                out[key] = []
                continue
            cur_list_key = None
            if val.startswith("[") and val.endswith("]"):
                items = [v.strip().strip("'\"") for v in val[1:-1].split(",")]
                out[key] = [v for v in items if v]
            else:
                out[key] = val.strip("'\"")
    except Exception:  # pragma: no cover - defensive: parser must never raise
        return out
    return out


def _select_skills(task_text: str, skills: list[dict[str, Any]], max_n: int) -> list[dict[str, Any]]:
    """Deterministic on-demand skill selector (no model call on the hot path, per
    SKILLS_ON_DEMAND.md §5.3). Picks the <=max_n skills most relevant to ``task_text``,
    so a worker's system prompt carries only task-relevant skills instead of every
    globally-delivered skill.

    SCORING
    -------
    - Small set (``len(skills) <= max_n``) → returned UNCHANGED (nothing to select).
    - KEYWORD score (always computed): token overlap between the (tokenized, stopword-
      stripped) task and the skill's matchable text. ``when_to_load`` + ``tags`` count
      ~2x name/description (the routing fields). Normalized to 0..1 by the max keyword
      score seen (guarded for div-0).
    - Top max_n by score descending, tie-broken by skill_slug (stable, deterministic).
    PURE + TOTAL: any read/parse error gives that skill a zero score, never raises."""
    skills = skills or []
    if len(skills) <= max_n:
        return skills

    task_tokens = _tokenize(task_text)

    def _keyword_score(sk: dict[str, Any]) -> int:
        """Token-overlap score: light text (name+description) 1x, routing text
        (when_to_load+tags) 2x. Total: any miss → 0."""
        try:
            if not task_tokens:
                return 0
            fm = _skill_frontmatter(sk.get("body_ref"))
            light = " ".join(str(sk.get(k) or "") for k in ("name", "description"))
            tags = fm.get("tags")
            tags_txt = " ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags or "")
            routing = f"{fm.get('when_to_load') or ''} {tags_txt}"
            light_hits = len(task_tokens & _tokenize(light))
            routing_hits = len(task_tokens & _tokenize(routing))
            return light_hits + 2 * routing_hits
        except Exception:  # pragma: no cover - defensive: scoring must never raise
            return 0

    def _slug(sk: dict[str, Any]) -> str:
        return str(sk.get("skill_slug") or "")

    # --- keyword pass (always; the proven path + the fallback) ---
    kw_scores: dict[int, int] = {id(sk): _keyword_score(sk) for sk in skills}
    scored = sorted(skills, key=lambda sk: (-kw_scores[id(sk)], _slug(sk)))
    nonzero = [sk for sk in scored if kw_scores[id(sk)] > 0]
    result = (nonzero or scored)[:max_n]

    # Env-gated debug: which slugs + which mode (semantic vs keyword fallback). The
    # caller logs the full slug list; this just records the routing MODE for diagnosis.
    if os.environ.get("KAIDERA_SKILL_SELECT_DEBUG"):
        with contextlib.suppress(Exception):
            picked = ",".join(_slug(sk) or str(sk.get("name") or "?") for sk in result)
            sys.stderr.write(f"[run-agent] _select_skills mode=keyword picked=[{picked}]\n")
    return result


def _skills_section(skills: list[dict[str, Any]]) -> str:
    """Render the agent's delivered skills as a system-prompt section. Each skill's
    SKILL.md body is injected; the agent runs any scripts a skill names through its
    run_bash tool (a skill grants guidance + scripts, not new tool permissions)."""
    blocks: list[str] = []
    for sk in skills or []:
        body = _skill_body(sk.get("body_ref"))
        if not body:
            continue
        title = sk.get("name") or sk.get("skill_slug") or "skill"
        blocks.append(f"### Skill: {title}\n{body}")
    if not blocks:
        return ""
    return (
        "\n\n---\n## Your installed skills\n"
        "You have the skills below. Follow each SKILL.md. When a skill names a script "
        "or command, RUN it with your run_bash tool (install prerequisites via run_bash "
        "if needed) — don't just describe it.\n\n" + "\n\n".join(blocks)
    )


def _profile_persona(project: str) -> str | None:
    """The active project's PROFILE persona text — the turnkey's declared persona from
    ``<project>.profile.json`` ``portal.persona``/``persona_file`` (redist dogfood GAP #4).

    This makes a dropped-in turnkey chat AS its persona WITHOUT a hand-authored
    ``<NAME>_IDENTITY.md``: the profile IS the persona source. Best-effort + total — a
    missing/blank project, no profile, or no portal persona yields None (the caller then
    falls back to the boot line). Never raises (project_profile accessors are tolerant)."""
    if not (project or "").strip():
        return None
    try:
        from . import project_profile
        text = (project_profile.portal_persona(project) or "").strip()
    except Exception:
        return None
    return text or None


def _parse_cortex_boot(raw: str) -> tuple[str, list[dict[str, Any]]]:
    """Accept both current JSON and legacy plain-text ``cortex-boot`` output.

    Registered workspaces can carry different generated Cortex CLI generations.
    Current API-backed scripts print a structured boot/persona document; older
    project scripts print the boot text directly even when their help mentions a
    JSON option. Plain text still provides valid identity/context, while delivered
    skills are available only from the structured form.
    """
    text = (raw or "").strip()
    if not text:
        return "", []
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text, []
    if not isinstance(data, dict):
        return text, []
    boot = str(data.get("boot") or "").strip()
    persona = data.get("persona") if isinstance(data.get("persona"), dict) else {}
    skills = [row for row in persona.get("skills") or [] if isinstance(row, dict)]
    return boot, skills


def build_agent_persona(name: str, project: str, workspace: str | None = None) -> str:
    """The agent's SYSTEM persona — its identity file (who it IS), ELSE the active
    project's profile persona, + current cortex-boot context — the SAME framing the
    autonomous worker builds in `_load_system_and_task`, REUSED by the interactive chat +
    dispatch so a chatted agent acts as ITS persona on ITS project (e.g. a
    domain expert persona on its own project), not a generic line.

    PERSONA PRECEDENCE (redist dogfood GAP #4):
      1. a hand-authored ``<NAME>_IDENTITY.md`` (the explicit per-agent override), ELSE
      2. the dropped-in project's PROFILE persona (``project_profile.portal_persona`` —
         the turnkey becomes its OWN persona source, no hand-authored file needed), ELSE
      3. the cortex-boot context alone (the generic identity line), ELSE
      4. '' (the caller then builds a PROJECT-AWARE one-liner from its `project` variable).

    NO hardcoded project name: the identity is per-agent, the profile + boot are scoped to
    the passed-in `project`. Best-effort — returns '' when nothing resolves."""
    boot = ""
    try:
        env = _cortex_cli_env(project, workspace)
        proc = subprocess.run(
            ["cortex-boot", name], capture_output=True, text=True, timeout=30, env=env,
            cwd=_cortex_cli_cwd(workspace),
        )
        raw = (proc.stdout or "").strip()
        if raw:
            boot, _ = _parse_cortex_boot(raw)
    except Exception:
        boot = ""
    # Persona source: the per-agent identity file wins; else the project's profile persona
    # (the turnkey-materialization fallback). Either is framed with the boot context the
    # same way, so the chat path reads a rich persona with NO hand-authored identity file.
    persona = _agent_identity(name) or _profile_persona(project)
    if persona:
        return persona if not boot else f"{persona}\n\n---\n## Current Cortex context\n{boot[:3000]}"
    if boot:
        return boot[:6000]
    return ""


def _load_system_and_task(name: str, handoff_id: str, project: str) -> tuple[str, str]:
    """Build the worker's SYSTEM prompt (rich identity + Cortex context + installed
    skills) and the TASK text (the handoff).

    SYSTEM = the agent's identity file (who it IS — role + discipline) PLUS the
    cortex-boot context (current state) PLUS any skills delivered in the boot persona
    manifest (global + bound). The identity makes the agent act as its declared
    role; the skills give the agent task capabilities (e.g. a web-reader skill).
    Falls back gracefully. TASK comes from ``cortex-handoff --show <id>``. Best-effort."""
    # --- current Cortex context + delivered skills (boot) ---
    boot = ""
    skills: list[dict[str, Any]] = []
    cli_env = _cortex_cli_env(project)
    cli_cwd = _cortex_cli_cwd()
    try:
        proc = subprocess.run(
            ["cortex-boot", name],
            capture_output=True, text=True, timeout=30, env=cli_env, cwd=cli_cwd,
        )
        raw = (proc.stdout or "").strip()
        if raw:
            boot, skills = _parse_cortex_boot(raw)
    except Exception:
        boot = ""

    # --- system: identity (preferred) + boot context, with graceful fallback ---
    identity = _agent_identity(name)
    if identity:
        system = identity if not boot else f"{identity}\n\n---\n## Current Cortex context\n{boot[:3000]}"
    elif boot:
        system = boot[:6000]
    else:
        system = f"You are {name}, a {project} agent. Do the work and reply concisely."

    # --- task: handoff summary from cortex-handoff --show ---
    # Fetched BEFORE the skills section: the on-demand selector (below) needs the task
    # text to score skills by relevance, so this MUST run first.
    task = f"(handoff {handoff_id})"
    try:
        proc = subprocess.run(
            ["cortex-handoff", "--show", handoff_id],
            capture_output=True, text=True, timeout=20, env=cli_env, cwd=cli_cwd,
        )
        output = (proc.stdout or "").strip()
        if output:
            task = output
    except Exception:
        pass  # fall back to the default above

    # --- installed skills (global + bound), injected from the boot persona manifest ---
    # DETERMINISTIC on-demand selection: with many global skills delivered, inject only
    # the ones relevant to THIS task (relevance scored against the task text above) rather
    # than every skill — keeps the worker's system prompt lean. Best-effort: if selection
    # raises, fall back to the prior behavior, just capped at max_n so a large set can't
    # bloat the prompt.
    max_n = _max_skills()
    try:
        selected = _select_skills(task, skills, max_n)
    except Exception:
        selected = (skills or [])[:max_n]
    # Lightweight visibility (a full cortex.log here is overkill): an env-gated line of
    # which slugs were picked, matching the file's stderr-diagnostic style.
    if os.environ.get("KAIDERA_SKILL_SELECT_DEBUG"):
        slugs = ",".join(str(sk.get("skill_slug") or sk.get("name") or "?") for sk in selected)
        with contextlib.suppress(Exception):
            sys.stderr.write(f"[run-agent] skills selected for {name}/{handoff_id}: [{slugs}]\n")
    system += _skills_section(selected)

    # The worker process identity is authoritative. Codex/Claude receive the system
    # text prepended to the task as plain prompt text, so make placeholder expansion
    # unambiguous for skills that say "cortex-boot <you>".
    runtime_identity = (
        "## Runtime identity (authoritative)\n"
        f"You are executing as `{name}@{project}`.\n"
        f"When instructions say `<you>`, use `{name}`. When they say `<project>`, use `{project}`.\n"
        f"Mandatory Cortex self-check commands for this run: `cortex-boot {name}` and "
        f"`cortex-handoff --mine {name}`. Do not run boot/mine for another agent unless "
        "the handoff explicitly asks you to inspect that agent.\n"
    )
    system = f"{runtime_identity}\n{system}"

    return system, task


def _build_collaborators(project: str) -> tuple[Any, Any, Any]:
    """Build the real (cortex, runner, routing) collaborators for the live run."""
    from . import harness_runner
    from .cortex_client import CortexClient
    from .main import _chat_routing_for
    return WorkerCortex(project, CortexClient()), harness_runner, _chat_routing_for


def _build_runstate() -> Optional[Any]:
    """Build the worker's OWN RunStatePort store (Milestone 1, T6).

    The worker is a DETACHED subprocess — it must NOT depend on the console, so it
    constructs its own ``RunStatePgStore`` over its own ``AppDB``. The DSN is resolved
    via ``host_appdb_dsn()`` (the HOST resolver): the worker runs on the HOST, so if it
    inherited the in-CONTAINER DSN (``harness-appdb:5432``) it would silently fail every
    run-state write — the resolver rewrites that to the loopback host port so the writes
    LAND. The store itself graceful-degrades (a down DB → every method is a no-op), and
    we additionally wrap CONSTRUCTION so an import/setup failure can never raise into the
    worker — a None store just means run_one skips all store writes cleanly."""
    try:
        from .adapters.runstate_pg import RunStatePgStore
        from .appdb import AppDB, host_appdb_dsn
        return RunStatePgStore(AppDB(host_appdb_dsn()))
    except Exception:  # pragma: no cover - defensive: store is optional, never fatal
        return None


def _redact_dsn(dsn: str) -> str:
    """Redact the password from a DSN for a log line (postgresql://user:***@host/db)."""
    try:
        if "://" in dsn and "@" in dsn:
            scheme, rest = dsn.split("://", 1)
            creds, tail = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{tail}"
    except Exception:  # pragma: no cover - defensive
        pass
    return dsn


async def _probe_runstate_appdb(runstate: Optional[Any]) -> None:
    """Emit ONE diagnostic line to STDERR stating the resolved app-DB DSN and whether a
    connect-probe succeeded — so a run-state write failure is no longer invisible.

    WHY: run-state writes are best-effort + swallowed (graceful-degrade), and the host
    service routes the worker's stderr to a per-run logfile now — so this single line is
    the operator's signal for why a run might stick at ``queued`` (e.g. the worker
    resolved a container DSN the host can't reach). Best-effort + time-bounded; NEVER
    raises and NEVER blocks the run (a None store / a down DB / a raising probe just logs
    and returns). The password is redacted."""
    if runstate is None:
        return
    appdb = getattr(runstate, "_appdb", None)
    dsn = _redact_dsn(str(getattr(appdb, "dsn", "") or ""))
    ok = False
    if appdb is not None:
        with contextlib.suppress(Exception):
            ok = bool(await appdb.ping())
    if ok:
        sys.stderr.write(f"[run-agent] app-DB OK ({dsn}) — run-state writes will land\n")
    else:
        sys.stderr.write(
            f"[run-agent] app-DB UNREACHABLE ({dsn}) — run-state writes will NOT land "
            "(run will appear stuck at queued; check the DSN is the HOST loopback, "
            "not the in-container harness-appdb host)\n"
        )
    with contextlib.suppress(Exception):
        sys.stderr.flush()


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

async def _amain(
    name: str, handoff_id: str, project: str, run_id: Optional[str] = None
) -> int:
    cortex, runner, routing = _build_collaborators(project)
    # The worker's OWN run-state store (T6). Only used when a run_id was passed
    # (argv[4]); with no run_id (legacy spawn) run_one skips all store writes.
    runstate = _build_runstate() if run_id else None
    # Diagnosability: emit ONE app-DB connect line to stderr (→ the per-run logfile the
    # host service now captures) so a run-state write failure is visible, not silent.
    await _probe_runstate_appdb(runstate)
    system, task = _load_system_and_task(name, handoff_id, project)
    res = await run_one(
        name, handoff_id, project,
        cortex=cortex, runner=runner,
        routing=routing, task_summary=task, system=system,
        runstate=runstate, run_id=run_id,
    )
    try:
        await cortex._client.aclose()  # tidy the httpx client
    except Exception:
        pass
    # Close the run-state store's asyncpg pool (best-effort).
    if runstate is not None:
        with contextlib.suppress(Exception):
            await runstate._appdb.aclose()
    return {"completed": 0, "failed": 1, "skipped": 2}.get(res.status, 1)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("usage: run-agent <name> <handoff_id> <project> [run_id]\n")
        return 64
    # argv[4] (run_id) is OPTIONAL — the orchestrator passes a pre-created uuid4 so
    # the worker writes the SAME run_state row (T5/T6); a standalone invocation
    # without it runs exactly as before (no store writes).
    run_id = argv[3] if len(argv) > 3 else None
    return asyncio.run(_amain(argv[0], argv[1], argv[2], run_id))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
