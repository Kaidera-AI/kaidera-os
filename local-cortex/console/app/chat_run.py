"""run-chat: run ONE interactive-chat turn on the HOST, then exit (harness-service I4).

The chat twin of `run_agent.py`. The interactive chat route (`POST /agents/{p}/{a}/
chat`) is MODEL (a): it pre-creates a `run_id`, drives `harness_runner.stream_chat`,
writes spans + status to the RunState SSOT store under that id, and the UI reads the
reply via `GET /runstate/stream`. To let a CONTAINERIZED console serve chat, that
"run one chat turn" must be runnable on the HOST (which has the harness CLIs +
their OAuth login) — exactly as `run_agent.py` made the autonomous worker host-runnable.

`chat_one(...)` is the reusable core: GIVEN a `(project, agent, message)` and a
pre-created `run_id`, it drives `runner.stream_chat(...)` and writes the SAME
run-state cycle the in-process route writes — MINUS the handoff lifecycle (a chat has
NO handoff: nothing to claim/complete; `lease_owner='chat'`, `handoff_id=None`). All
durable state goes through the RunState store (the app-DB) DIRECTLY, like the worker,
so the container never needs the CLI — it POSTs `/chat` to the host harness-service,
which shells THIS runner.

Exit code: 0 = ok, 1 = error.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .chat_history import compose_contextual_prompt, load_session_history
from . import harness as harness_cfg
from .local_run_tasks import LOCAL_RUN_CANCELLED_ERROR


@dataclass
class ChatResult:
    status: str                       # "ok" | "error"
    text: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    harness: str | None = None
    model: str | None = None


async def chat_one(
    name: str,
    message: str,
    project: str,
    *,
    run_id: str,
    runner: Any,
    runstate: Optional[Any] = None,
    cortex: Optional[Any] = None,
    harness: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    system: str | None = None,
    session_id: str | None = None,
    attachment_paths: list[str] | None = None,
    start_run: bool = True,
    pid: int | None = None,
    workspace: str | None = None,
    project_key: str | None = None,
    ltm_log_fn: Callable[..., Any] | None = None,
    ltm_agent: str | None = None,
    on_result: Callable[[dict[str, Any]], Any] | None = None,
) -> ChatResult:
    """Run ONE chat turn → write run-state spans + terminal status under `run_id`, then
    persist the COMPLETED turn to Cortex LTM (never-forget; step 5).

    MIRRORS the in-process chat route (`main.agent_chat`) and the worker
    (`run_agent.run_one`), MINUS the handoff lifecycle — a chat is free-standing:
      * `start_run(run_id, handoff_id=None, lease_owner='chat')` opens the row;
      * `set_status(run_id, 'running')` at start;
      * `append_output(run_id, seq, kind, text)` per streamed delta/result span;
      * on done → `set_status(run_id, 'ok', tokens…/cost…)`; on a harness error →
        `set_status(run_id, 'error', error=…)`.

    NEVER-FORGET LTM (step 5): when a `cortex` collaborator is supplied AND the turn
    completes OK, the turn (user message + reply) is persisted to Cortex LTM via
    `chat_ltm.persist_chat_turn` using the SAME `cortex.log(...)` mechanism the autonomous
    worker uses (`run_agent.run_one`'s TRANSCRIPT write) — so the conversation survives an
    app-DB wipe and the agent can recall it. `cortex` is used ONLY for this write; a chat
    has NO handoff, so it is NEVER claimed/completed.

    GRACEFUL-DEGRADE (house law): EVERY store call is best-effort — a None / raising
    store (down app-DB) is a clean no-op; the turn still runs and the reply is still
    assembled (only the durable run-state is lost). The LTM write is likewise best-effort
    (a None / raising `cortex` never breaks the turn). The detached chat runner must NEVER
    be broken by the store OR the LTM write."""
    # Run-state store helpers (best-effort; mirror run_agent.run_one's _rs_*).
    _rs_on = runstate is not None

    async def _rs_status(status: str, **kw: Any) -> None:
        if not _rs_on:
            return
        with contextlib.suppress(Exception):
            await runstate.set_status(run_id, status, **kw)

    async def _rs_totals(*, tokens_in: Any, tokens_out: Any, cost_est_usd: Any) -> None:
        """Stamp the turn's FINAL token/cost totals on the run header via ``heartbeat``
        (the RunStatePort home for tokens/cost — ``set_status`` takes only status/error/
        metadata). Passing tokens to ``set_status`` raised a swallowed TypeError that
        left the row pinned at 'running'. Best-effort, like every store write."""
        if not _rs_on:
            return
        with contextlib.suppress(Exception):
            await runstate.heartbeat(
                run_id, tokens_in=tokens_in, tokens_out=tokens_out,
                cost_est_usd=cost_est_usd, pid=os.getpid(),
            )

    seq = 0

    async def _rs_span(kind: str, text: str) -> None:
        nonlocal seq
        if not _rs_on or not text:
            return
        seq += 1
        with contextlib.suppress(Exception):
            await runstate.append_output(run_id, seq=seq, kind=kind, text=text)

    # Open the run_state row (lease_owner='chat', NO handoff). Best-effort: a down
    # store leaves the row unwritten but the turn proceeds. `session_id` (when set)
    # groups this turn into its conversation so a later turn can thread it back.
    if _rs_on and start_run:
        with contextlib.suppress(Exception):
            await runstate.start_run(
                run_id=run_id,
                project=project,
                agent=name,
                agent_display=name,
                handoff_id=None,
                harness=harness,
                model=model,
                pid=os.getpid() if pid is None else pid,
                lease_owner="chat",
                session_id=session_id,
            )

    # MULTI-TURN CONTEXT (feature-gap step 6, Inc B): store the USER MESSAGE as an
    # `input` span FIRST (seq 1) so a later turn in this session can rebuild this turn's
    # user side, then thread the conversation's prior turns into the prompt. Both are
    # best-effort + additive: with no session_id (or a down store) there are no prior
    # turns, `load_session_history` returns [], and the prompt is UNCHANGED (single-shot
    # — byte-for-byte as before). The `input` span is written regardless (it is harmless
    # for a single-shot turn and is what makes the NEXT turn's history work).
    await _rs_span("input", message)
    # CHAT FILE-ATTACHMENTS (feature-gap step 6, Inc A): the HOST chat runner's twin of
    # the in-process route's attachment path. `attachment_paths` are the HOST paths the
    # harness-service wrote (it forwarded the container's uploaded bytes to the host and
    # passed the resulting paths here). We persist ONE `attachment` span per file (the
    # basename → a transcript chip) and weave the files into the CURRENT message via
    # `inline_attachments` BEFORE history wraps it. Both are additive + degrade-safe:
    # `inline_attachments([], msg)` returns the message unchanged (the no-attachment path
    # is byte-for-byte), and a missing path is skipped inside it (never raises).
    paths = list(attachment_paths or [])
    if paths:
        from .attachments import inline_attachments
        import os as _os
        for _p in paths:
            with contextlib.suppress(Exception):
                await _rs_span("attachment", _os.path.basename(_p))
        image_readable = harness_cfg.supports_vision_attachments(harness, model)
        enriched_message = inline_attachments(
            paths, message, image_readable=image_readable
        )
    else:
        enriched_message = message
    history: list[tuple[str, str]] = []
    with contextlib.suppress(Exception):
        history = await load_session_history(runstate, project, name, session_id)
    threaded_prompt = compose_contextual_prompt(enriched_message, system, history)

    sys_prompt = system or f"You are {name}, a {project} agent. Reply concisely."
    assembled: list[str] = []
    result = ChatResult(status="ok", harness=harness, model=model)

    await _rs_status("running")
    try:
        async for ev in runner.stream_chat(
            threaded_prompt,
            model=model,
            system=sys_prompt,
            harness=harness,
            reasoning=reasoning,
            workspace=workspace,
            project_key=project_key or project,
            run_context="chat",
        ):
            kind = ev.get("type")
            if kind == "delta":
                delta_text = ev.get("text", "")
                assembled.append(delta_text)
                await _rs_span("output", delta_text)
            elif kind == "thinking":
                await _rs_span("thinking", ev.get("text", ""))
            elif kind == "tool":
                tool_text = ev.get("text") or ev.get("name") or ""
                await _rs_span("tool", tool_text)
            elif kind == "tasks":
                items = ev.get("items") or []
                await _rs_span("tasks", json.dumps(items, ensure_ascii=False))
            elif kind == "subagent":
                label = ev.get("label") or ev.get("text") or "sub-agent"
                await _rs_span("subagent", str(label))
            elif kind == "result":
                if on_result is not None:
                    maybe = on_result(ev)
                    if inspect.isawaitable(maybe):
                        await maybe
                result.tokens_in = ev.get("tokens_in")
                result.tokens_out = ev.get("tokens_out")
                result.cost_usd = ev.get("cost_usd")
                txt = ev.get("text") or ""
                # De-dup the result ECHO: streaming harnesses (claude-code/pi) emit
                # the full reply as deltas AND echo it in the terminal `result` —
                # appending an exact echo DOUBLES the reply ("HEAL OKHEAL OK"). Skip
                # only when `txt` exactly equals what already streamed; genuinely
                # different/additional result text (or a result-only harness with no
                # deltas) is still captured.
                if txt and txt.strip() != "".join(assembled).strip():
                    assembled.append(txt)
                    await _rs_span("output", txt)
            elif kind == "error":
                result.status = "error"
                result.error = ev.get("message", "harness error")
            # session / done frames are not separately surfaced.
    except (asyncio.CancelledError, GeneratorExit):
        result.status = "error"
        result.error = LOCAL_RUN_CANCELLED_ERROR
        await _rs_status("error", error=result.error)
        raise
    except Exception as exc:  # a runner crash → error terminal (never propagates)
        result.status = "error"
        result.error = f"chat crashed: {exc}"

    result.text = "".join(assembled)
    if result.status == "ok":
        await _rs_totals(
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_est_usd=result.cost_usd,
        )
        await _rs_status("ok")
        # NEVER-FORGET (step 5): persist the completed turn to Cortex LTM via the SAME
        # mechanism the autonomous worker uses (cortex.log). Best-effort — a None/raising
        # cortex never breaks the turn (the reply + run-state are already done). Only a
        # COMPLETED turn is persisted (an error turn produced no reply worth remembering).
        log_fn = ltm_log_fn or getattr(cortex, "log", None)
        if log_fn is not None:
            from .chat_ltm import persist_chat_turn
            with contextlib.suppress(Exception):
                await persist_chat_turn(log_fn, ltm_agent or name, run_id, message, result.text, project)
    else:
        await _rs_status("error", error=result.error)
    # CHAT FILE-ATTACHMENTS (step 6, Inc A): the turn reached a terminal state →
    # best-effort cleanup of the HOST attachment files the harness-service wrote (under
    # HARNESS_ATTACHMENT_DIR/<attachment_id>/, a different layout from the run-keyed
    # sandbox — so we clean the exact paths we were given, not a run dir). Never raises;
    # a no-op when no attachment was passed. The bytes were already woven into the prompt
    # + the `attachment` span is the durable record; the host service's 24h sweep backs
    # this up.
    if paths:
        from .attachments import cleanup_paths
        with contextlib.suppress(Exception):
            cleanup_paths(paths)
    return result


# ---------------------------------------------------------------------------
#  Bootstrap helpers (the real collaborators for the live host run)
# ---------------------------------------------------------------------------

def _build_runner_and_routing() -> tuple[Any, Callable[[dict, str], tuple[str, str | None, str | None]]]:
    """The real (runner, routing) collaborators — the SAME harness_runner +
    _chat_routing_for the in-process chat route uses (so a host chat turn routes
    identically to the in-process one)."""
    from . import harness_runner
    from .main import _chat_routing_for
    return harness_runner, _chat_routing_for


def _build_runstate() -> Optional[Any]:
    """Build the chat runner's OWN RunStatePort store (mirrors run_agent._build_runstate).

    The runner is a DETACHED host subprocess — it must NOT depend on the console, so it
    constructs its own `RunStatePgStore` over its own `AppDB` (the same asyncpg layer +
    the `HARNESS_APPDB_DSN`/`APPDB_DSN` env the console uses). The store graceful-
    degrades (a down DB → every method is a no-op), and construction is wrapped so an
    import/setup failure can never raise into the runner (None → chat_one skips store
    writes cleanly)."""
    try:
        from .adapters.runstate_pg import RunStatePgStore
        from .appdb import AppDB
        return RunStatePgStore(AppDB())
    except Exception:  # pragma: no cover - defensive: store is optional, never fatal
        return None


def _build_cortex(project: str) -> Optional[Any]:
    """Build the chat runner's Cortex collaborator for the never-forget LTM write (step 5).

    Reuses the autonomous worker's `run_agent.WorkerCortex` over a `CortexClient` — so a
    host chat turn persists its transcript to Cortex via the EXACT SAME path the autonomous
    worker uses (`WorkerCortex.log` shells `cortex-log`; blocking subprocess is fine here —
    the chat runner, like run-agent, is a dedicated single-turn host process). Construction
    is wrapped so an import/setup failure can never raise into the runner (None → chat_one
    skips the LTM write cleanly; the run-state write is unaffected)."""
    try:
        from .cortex_client import CortexClient
        from .run_agent import WorkerCortex
        return WorkerCortex(project, CortexClient())
    except Exception:  # pragma: no cover - defensive: the LTM write is optional, never fatal
        return None


def _load_chat_system(name: str, project: str) -> str | None:
    """Build the chat turn's SYSTEM framing on the host (best-effort).

    Reuses the worker's identity loader (`run_agent._agent_identity` — the rich
    persona file) so a host chat turn frames the agent the same way the worker does;
    falls back to None (chat_one then uses its minimal default). Kept best-effort so a
    missing identity file never breaks the turn."""
    try:
        from .run_agent import _agent_identity
        return _agent_identity(name)
    except Exception:  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

async def _amain(
    name: str, project: str, run_id: str, message: str,
    session_id: str | None = None,
    attachment_paths: list[str] | None = None,
) -> int:
    runner, routing = _build_runner_and_routing()
    runstate = _build_runstate()
    # Cortex collaborator for the never-forget LTM write (step 5) — the SAME WorkerCortex
    # the autonomous worker uses. Best-effort: a None one just skips the LTM write.
    cortex = _build_cortex(project)
    # Resolve the agent's configured routing (override-first) the SAME way the
    # in-process route does — via _chat_routing_for on a minimal agent dict.
    harness = model = reasoning = None
    with contextlib.suppress(Exception):
        harness, model, reasoning = routing({"name": name}, project)
    system = _load_chat_system(name, project)
    res = await chat_one(
        name, message, project,
        run_id=run_id, runner=runner, runstate=runstate, cortex=cortex,
        harness=harness, model=model, reasoning=reasoning, system=system,
        session_id=session_id, attachment_paths=attachment_paths,
    )
    # Close the run-state store's asyncpg pool (best-effort).
    if runstate is not None:
        with contextlib.suppress(Exception):
            await runstate._appdb.aclose()
    # Close the Cortex HTTP client (best-effort) — mirrors run_agent._amain's aclose.
    if cortex is not None:
        with contextlib.suppress(Exception):
            await cortex._client.aclose()
    return 0 if res.status == "ok" else 1


def main(argv: list[str]) -> int:
    # argv: <agent> <project> <run_id> [--session-id <id>] [--attachment-paths a,b]
    #       <message...>. The message is the REMAINING argv joined (so a multi-word
    # message survives the shell-less argv list). `--session-id <id>` (multi-turn chat)
    # and `--attachment-paths <csv>` (file-attachments, step 6) are OPTIONAL — when
    # present anywhere after run_id, each flag + its value are consumed and the rest is
    # the message; absent → None (single-shot / no-attachment, the existing contract).
    usage = (
        "usage: run-chat <agent> <project> <run_id> [--session-id <id>] "
        "[--attachment-paths a,b] <message...>\n"
    )
    if len(argv) < 4:
        sys.stderr.write(usage)
        return 64
    name, project, run_id = argv[0], argv[1], argv[2]
    rest = list(argv[3:])

    def _consume_flag(flag: str) -> str | None:
        """Pull `flag <value>` out of `rest` (if present) and return the value."""
        if flag in rest:
            i = rest.index(flag)
            if i + 1 < len(rest):
                val = rest[i + 1]
                del rest[i : i + 2]
                return val
            del rest[i : i + 1]
        return None

    session_id = _consume_flag("--session-id")
    attach_csv = _consume_flag("--attachment-paths")
    attachment_paths = (
        [a for a in attach_csv.split(",") if a] if attach_csv else None
    )
    message = " ".join(rest)
    if not message:
        sys.stderr.write(usage)
        return 64
    return asyncio.run(
        _amain(name, project, run_id, message, session_id, attachment_paths)
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
