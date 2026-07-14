"""Never-forget memory for interactive chat — persist a completed chat turn to Cortex
LTM (feature-gap step 5; closes the long-standing `project_ltm_chat_history_gap`).

WHY: the autonomous worker already persists its transcript to Cortex LTM
(`run_agent.run_one` -> `cortex.log(name, "decision", f"{name} TRANSCRIPT …")`). Interactive
chat did NOT — both the detached host runner (`chat_run.chat_one`) and the in-process
route (`main.agent_chat`) wrote ONLY to the app-DB run-state store, so a conversation was
lost if the app-DB was wiped and the agent kept no durable memory of it. This module is
the shared writer BOTH chat paths call after a turn completes, so chat lands in the SAME
L1 verbatim + L2 embedding memory the autonomous run does (recallable via cortex-search
and surfaced at L6 boot).

THE CAP / NEVER-FORGET DECISION: the autonomous path caps the transcript at 8000 chars
(`result.text[:8000]`) — it silently drops the tail of a long run. "Never-forget" chat must
not. So we persist the FULL turn: when a turn exceeds a GENEROUS per-entry budget
(`CHAT_LTM_CHUNK_CHARS`) we CHUNK it into sequential `CHAT {run_id} [i/N]: …` log entries
rather than truncating — every character survives and the entries reassemble in order.

GRACEFUL-DEGRADE (house law): every write is best-effort. A Cortex-down / raising log
collaborator (or a None one) must NOT break the chat — the reply already returned and the
run-state SSOT write is unchanged; only the durable LTM write is lost. Failures are
logged-and-swallowed, exactly like `run_agent`'s degrade.

The write mechanism is INJECTED as `log(agent, event_type, summary, project)` — the SAME
async interface `run_agent.run_one` calls. The detached host runner passes the real
`run_agent.WorkerCortex.log` (which shells `cortex-log`, like the autonomous worker); the
in-process route passes `cli_log` (a non-blocking `cortex-log` subprocess, so it never
stalls the FastAPI event loop). Injecting the writer keeps this module pure + unit-testable
(the tests pass a fake) and free of any per-project literal (the gate scans `app/*.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import shutil
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger("console.chat_ltm")

# Per-entry character budget for a single Cortex LTM `cortex-log` write. Chosen GENEROUS
# (2x the autonomous transcript's 8000-char cap) so the overwhelming majority of chat
# turns persist as ONE entry, while staying comfortably under both the OS argv limit
# (`cortex-log` passes the summary as a single argv; ARG_MAX is ~256KB+) and any practical
# Cortex summary size. A turn larger than this is CHUNKED (never truncated) — see
# `build_chat_summaries`.
CHAT_LTM_CHUNK_CHARS = 16000

# Reserve inside the budget for the per-entry header (`<agent> CHAT <run_id> [i/N]: `).
# Run ids are short (uuid4 = 36 chars) and the [i/N] marker is tiny, but we keep a roomy
# fixed reserve so a chunk's header + payload never exceeds the budget regardless of N's
# digit count. The payload capacity per chunk is `CHAT_LTM_CHUNK_CHARS - _HEADER_RESERVE`.
_HEADER_RESERVE = 200

# The event marker — the chat twin of the autonomous `TRANSCRIPT` marker. Distinct so a
# cortex-search can find conversations specifically (`CHAT <run_id>`), separate from
# autonomous run transcripts.
_MARKER = "CHAT"

LogFn = Callable[..., Awaitable[None]]


def build_chat_summaries(agent: str, run_id: str, message: str, reply: str) -> list[str]:
    """Build the Cortex-LTM summary line(s) for one completed chat turn.

    Returns a LIST: one entry for a normal turn, or several `… [i/N]: <slice>` entries
    for a turn whose `<message> -> <reply>` body exceeds the per-entry budget. The FULL
    body is preserved across the entries (never truncated) and the entries reassemble in
    order. `agent` is the identity to stamp into the text (compound where the caller has
    it, e.g. `agent@project`; bare name otherwise — matching the autonomous path)."""
    msg = (message or "").strip()
    rep = (reply or "").strip()
    if not msg and not rep:
        return []
    # The turn body: the user's message and the agent's reply, the way the autonomous
    # TRANSCRIPT row carries the reply — but here we carry BOTH sides of the exchange.
    body = f"{msg} -> {rep}" if msg else rep

    head = f"{agent} {_MARKER} {run_id}"
    single = f"{head}: {body}"
    if len(single) <= CHAT_LTM_CHUNK_CHARS:
        return [single]

    # Chunk the body so each `<head> [i/N]: <slice>` entry stays within the budget.
    capacity = max(1, CHAT_LTM_CHUNK_CHARS - len(head) - _HEADER_RESERVE)
    total = max(1, math.ceil(len(body) / capacity))
    out: list[str] = []
    for i in range(total):
        slice_ = body[i * capacity : (i + 1) * capacity]
        out.append(f"{head} [{i + 1}/{total}]: {slice_}")
    return out


async def persist_chat_turn(
    log_fn: Optional[LogFn],
    agent: str,
    run_id: str,
    message: str,
    reply: str,
    project: Optional[str] = None,
    *,
    event_type: str = "decision",
) -> None:
    """Persist ONE completed chat turn to Cortex LTM via the injected `log_fn`.

    Writes one `cortex.log(agent, event_type, summary, project)` per chunk (usually just
    one). NEVER-FORGET: the full turn is persisted (chunked, not truncated). The agent arg
    passed to `log_fn` is the BARE agent name segment (the cortex-log CLI normalises it);
    the compound identity, when the caller supplies one, is stamped into the summary TEXT.

    GRACEFUL-DEGRADE: a None `log_fn` is a clean no-op, and EVERY write is wrapped so a
    raising / down Cortex never propagates — the chat reply already returned and the
    run-state SSOT write is unchanged; only the durable LTM write is lost."""
    if log_fn is None:
        return
    summaries = build_chat_summaries(agent, run_id, message, reply)
    if not summaries:
        return
    # The cortex-log AGENT arg must be the bare name (the CLI lowercases/normalises it).
    agent_arg = (agent or "").split("@", 1)[0].strip()
    for summary in summaries:
        try:
            await log_fn(agent_arg, event_type, summary, project)
        except Exception as exc:  # never break the chat on an LTM write failure
            log.warning("chat to LTM write failed (degraded, chat unaffected): %s", exc)


def _cortex_log_program(workspace: str | None = None) -> str:
    """Resolve the API-backed writer without relying on launchd's minimal PATH."""
    candidates = []
    if workspace:
        candidates.append(Path(workspace).expanduser() / ".agents" / "scripts" / "cortex-log")
    candidates.append(Path(__file__).resolve().parents[3] / ".agents" / "scripts" / "cortex-log")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("cortex-log") or "cortex-log"


async def cli_log(
    agent: str,
    event_type: str,
    summary: str,
    project: Optional[str] = None,
    *,
    workspace: str | None = None,
) -> None:
    """A `cortex-log` writer for the IN-PROCESS chat route — the SAME CLI the autonomous
    worker shells, but via `asyncio.create_subprocess_exec` (args as a LIST, no shell — so
    no injection vector) so it NEVER blocks the FastAPI event loop (the worker's
    `run_agent.WorkerCortex.log` uses a blocking subprocess because it runs in a dedicated
    single-task process; the in-process route must stay async).

    Best-effort + total: the process is spawned and awaited with a short timeout; ANY
    failure (CLI absent, non-zero exit, timeout) propagates to `persist_chat_turn`'s
    wrapper which swallows it, so a down Cortex never breaks the chat. `event_type` maps to
    a cortex-log valid type (decision/lesson); the marker is already in the summary text."""
    et = "lesson" if event_type == "lesson" else "decision"
    env = dict(os.environ)
    workdir = (workspace or "").strip()
    if project:
        env["CORTEX_PROJECT"] = project
    env.setdefault("CORTEX_API_URL", "http://127.0.0.1:8501")
    if workdir:
        env["KAIDERA_AGENT_WORKSPACE"] = workdir
        env["PATH"] = str(Path(workdir) / ".agents" / "scripts") + os.pathsep + env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(
        _cortex_log_program(workdir), agent, et, summary,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=workdir if workdir and os.path.isdir(workdir) else None,
    )
    try:
        return_code = await asyncio.wait_for(proc.wait(), timeout=20)
        if return_code != 0:
            raise RuntimeError(f"cortex-log exited {return_code}")
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        raise


__all__ = [
    "CHAT_LTM_CHUNK_CHARS",
    "build_chat_summaries",
    "persist_chat_turn",
    "cli_log",
]
