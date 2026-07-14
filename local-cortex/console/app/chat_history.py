"""Multi-turn conversation context for interactive chat (feature-gap step 6, Inc B).

WHY: a chat turn is a RunState row (`lease_owner='chat'`); each turn is a SINGLE-SHOT
harness call today — claude-code `-p` and pi each treat every call as a NEW session, so
the agent has NO memory of earlier turns in the same conversation. This module threads a
conversation's prior turns into the PROMPT (the only channel that survives the
new-session-per-call harnesses), so a follow-up like "and the second one?" resolves
against what was already said.

THE SHAPE (two pure-ish functions, both ADDITIVE + degrade-safe):

  * `load_session_history(store, project, agent, session_id, …)` reads the recent
    `lease_owner='chat'` runs for a `session_id` (via the RunStatePort's
    `recent(session_id=…)` + `get_run` hydration), rebuilds each turn's
    `(user_message, assistant_reply)` — the user message from the NEW `input` span, the
    reply from the concatenated `output` spans — OLDEST-FIRST, capped by BOTH a turn
    count and a character budget (dropping the OLDEST beyond the cap). It returns `[]`
    for a None / down store or a blank session — a chat must DEGRADE to single-shot,
    never crash.

  * `compose_contextual_prompt(prompt, system, history)` is PURE: it prepends a
    `[Previous conversation] … [Current message] <prompt>` block when there's history,
    else returns the prompt UNCHANGED (the single-shot path — byte-for-byte today).

DEPENDENCY DISCIPLINE: this module depends ONLY on the `RunStatePort` Protocol (the
pure domain port) — NEVER a concrete adapter — so it stays unit-testable with a fake
store and free of any per-project literal (the no-project-literals gate scans
`app/*.py`). It performs READS only; it never writes run-state.

CAPS are config-as-data: `HISTORY_MAX_TURNS` / `HISTORY_MAX_CHARS` constants,
env-overridable via `CHAT_HISTORY_MAX_TURNS` / `CHAT_HISTORY_MAX_CHARS` — a tunable
bound, not a hardcoded literal.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Optional

log = logging.getLogger("console.chat_history")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """A clamped int from the environment (mirrors orchestrator/runstate_pg
    `_env_int`). Keeps the caps config-driven — a tunable bound, no project literal."""
    try:
        return max(lo, min(hi, int(os.environ.get(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


# How many PRIOR turns (user+assistant pairs) at most we thread into the prompt. The
# NEWEST turns win when a conversation is longer (the cap drops the oldest). 8 keeps
# recent context rich while bounding the prompt; env-overridable for tuning.
HISTORY_MAX_TURNS = _env_int("CHAT_HISTORY_MAX_TURNS", 8, 1, 100)

# The total character budget for the threaded history block (user+assistant text,
# summed across kept turns). We drop the OLDEST turns until the kept set fits. 12000
# chars (~3k tokens) is a generous-but-bounded slice; env-overridable.
HISTORY_MAX_CHARS = _env_int("CHAT_HISTORY_MAX_CHARS", 12000, 200, 200000)

# The span kind the chat path now writes for the USER message (the reply stays
# `output`). One source of truth for the kind string, shared with the writers + the
# transcript renderers.
INPUT_SPAN_KIND = "input"
OUTPUT_SPAN_KIND = "output"


def _turn_from_run(run: Any) -> Optional[tuple[str, str]]:
    """Rebuild ONE `(user_message, assistant_reply)` turn from a hydrated RunRecord, or
    None if it isn't a complete exchange.

    The user message is the concatenation of the run's `input` span(s) (seq-ordered);
    the reply is the concatenation of its `output` span(s). A run missing EITHER side
    (e.g. the in-flight turn before its reply, or a malformed row) is skipped — only
    complete pairs are threaded as context. Tolerant of any odd shape (→ None)."""
    spans = getattr(run, "spans", None) or []
    user_parts: list[str] = []
    reply_parts: list[str] = []
    for sp in spans:
        kind = getattr(sp, "kind", None)
        text = getattr(sp, "text", "") or ""
        if kind == INPUT_SPAN_KIND:
            user_parts.append(text)
        elif kind == OUTPUT_SPAN_KIND:
            reply_parts.append(text)
    user = "".join(user_parts).strip()
    reply = "".join(reply_parts).strip()
    if not user or not reply:
        return None
    return (user, reply)


async def load_session_history(
    store: Any,
    project: str,
    agent: str,
    session_id: Optional[str],
    *,
    max_turns: int = HISTORY_MAX_TURNS,
    max_chars: int = HISTORY_MAX_CHARS,
) -> list[tuple[str, str]]:
    """Load a chat conversation's prior turns as `(user_message, assistant_reply)`
    tuples, OLDEST-FIRST, capped by turns AND chars.

    Reads the recent `lease_owner='chat'` runs for `session_id` through the
    RunStatePort: `recent(project, session_id=…)` for the (newest-first) headers, then
    `get_run` to hydrate each run's spans. Rebuilds each complete turn, reverses to
    oldest-first, keeps the NEWEST `max_turns`, then drops the OLDEST until the kept
    set's char total fits `max_chars`.

    GRACEFUL-DEGRADE (house law): a None / down store, a blank session, or ANY read
    failure yields `[]` — the chat then runs single-shot, exactly as before. Nothing
    here raises into the caller."""
    sess = (session_id or "").strip()
    if store is None or not sess:
        return []

    # Cap the header fetch generously above max_turns (some runs may be incomplete /
    # skipped), but bounded so a huge conversation can't pull an unbounded page.
    fetch_limit = max(1, min(int(max_turns) * 3, 200))

    try:
        # Newest-first headers for THIS session. The adapter filters in SQL; a fake
        # store returns the scripted set.
        headers = await store.recent(project, limit=fetch_limit, session_id=sess)
    except Exception as exc:  # down store → single-shot fallback (never crash)
        log.warning("chat history recent() failed (degraded, single-shot): %s", exc)
        return []

    turns_newest_first: list[tuple[str, str]] = []
    for header in headers or []:
        if len(turns_newest_first) >= int(max_turns):
            break  # already have enough NEWEST complete turns
        run_id = getattr(header, "run_id", None)
        if not run_id:
            continue
        run = header
        # recent() returns HEADERS (no body); hydrate the spans via get_run. If the
        # header already carries spans (a fake / future store), use it as-is.
        if not (getattr(header, "spans", None)):
            with contextlib.suppress(Exception):
                run = await store.get_run(run_id)
        if run is None:
            continue
        turn = _turn_from_run(run)
        if turn is not None:
            turns_newest_first.append(turn)

    # Oldest-first for prompt assembly (the conversation reads top-to-bottom).
    turns = list(reversed(turns_newest_first))

    # Char cap: drop the OLDEST turns until the kept set fits the budget. We keep the
    # newest contiguous window (most relevant context), oldest-first within it.
    budget = int(max_chars)
    total = sum(len(u) + len(r) for u, r in turns)
    while turns and total > budget:
        u, r = turns.pop(0)  # drop the oldest
        total -= len(u) + len(r)
    return turns


def compose_contextual_prompt(
    prompt: str,
    system: Optional[str],
    history: list[tuple[str, str]],
) -> str:
    """Build the prompt the harness sees, threading prior turns as context (PURE).

    With history: a `[Previous conversation]` block (each prior turn as
    `User: …` / `Assistant: …`, oldest-first) followed by a `[Current message]` marker
    and the current `prompt`. With NO history: the `prompt` is returned UNCHANGED (the
    single-shot path — byte-for-byte identical to today, so the no-session case is a
    pure no-op).

    `system` is accepted for symmetry with the harness's prompt/system seam but is NOT
    embedded here — the harness composes the system separately (`_compose_prompt`); this
    function only threads the conversation history into the user-visible prompt."""
    if not history:
        return prompt
    lines: list[str] = ["[Previous conversation]"]
    for user, reply in history:
        lines.append(f"User: {user}")
        lines.append(f"Assistant: {reply}")
    lines.append("")
    lines.append("[Current message]")
    lines.append(prompt)
    return "\n".join(lines)


__all__ = [
    "HISTORY_MAX_TURNS",
    "HISTORY_MAX_CHARS",
    "INPUT_SPAN_KIND",
    "OUTPUT_SPAN_KIND",
    "load_session_history",
    "compose_contextual_prompt",
]
