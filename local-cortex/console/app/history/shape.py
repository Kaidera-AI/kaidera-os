"""Pure shaping: Cortex /history + /search + /roster → a clean activity-timeline payload.

NO I/O — stdlib only. The Cortex `/history` stream is the same noisy tool-call JSON the
agent-detail feed parses; this module turns it into a clean reverse-chronological
`events` timeline (one readable line per row), a recent-`decisions` feed (from `/search`),
and the roster `agent_count`. The api layer wires the live `CortexClient` reads around it.

The summariser (`summarize_row`) + the relative-age formatter (`relative_age`) are PORTED
1:1 from `main._summarize_history_row` / `runs.service._default_relative` so this module is
fully self-contained (no reach into `app.main` — the transitive-import trap the Track-A
carves flagged) and the readable lines match the legacy HTML history view exactly.

  - `events`     — `[{ts, ts_ago, agent, role, summary, kind}]`, newest-first, capped at
                   `HISTORY_EVENT_CAP`. `kind` ∈ say | tool | think (drives the row tint).
                   token_count / empty frames are DROPPED (they do not count as rows).
  - `decisions`  — `[{ts, ts_ago, agent, summary, source, category}]` from `/search`,
                   capped at `HISTORY_DECISIONS_CAP`. `ts`/`ts_ago`/`agent` are best-effort
                   ('' when the search row carries no timestamp/agent).
  - `agent_count`— the distinct agents on the project roster (the headline count).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

# Cap on timeline rows shown in the History view (newest kept). PORTED from the legacy
# `main._HISTORY_MAX` so the bounded window matches the HTML view.
HISTORY_EVENT_CAP = 60
# Cap on recent-decisions rows in the side feed (the legacy `main._HISTORY_DECISIONS_MAX`).
HISTORY_DECISIONS_CAP = 14

# Max readable-summary / decision-text lengths (kept compact for the timeline rows).
_SUMMARY_MAX = 220
_DETAIL_MAX = 80
_DECISION_MAX = 200

# --- regexes lifted from main (parse a possibly-truncated JSON content blob) -----------
# Pull "type":"..." out of a (possibly truncated) JSON content blob.
_TYPE_RE = re.compile(r'"type":\s*"([^"]+)"')
# Pull "name":"..." (the tool/function name) out of a function_call blob.
_NAME_RE = re.compile(r'"name":\s*"([^"]+)"')
# Pull the first cmd value out of an exec_command arguments blob (escaped or plain form).
_CMD_RE = re.compile(r'\\?"cmd\\?":\s*\\?"((?:[^"\\]|\\.)*)')


def _short(text: str, n: int) -> str:
    """Collapse whitespace and clip to n chars with an ellipsis (a local copy of
    main._short, so this module carries no console dependency)."""
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def relative_age(ts: str | None) -> str:
    """A compact 'how long ago' label for an ISO-UTC timestamp (pure stdlib `datetime`).

    'now' (<5s) · 'Ns' · 'Nm' · 'Nh' · else 'Nd'. Best-effort — an unparseable or absent
    timestamp degrades to '' (the row just omits the age). PORTED 1:1 from
    `runs.service._default_relative` so every console surface renders identical age labels."""
    if not ts:
        return ""
    raw = ts.strip()
    # Accept a trailing 'Z' (UTC) which fromisoformat rejects on older pythons.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - when
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 5:
        return "now"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _kind_label(kind: str) -> str:
    """Human noun for a summarised row's kind (drives the timeline group tint)."""
    return {"say": "message", "tool": "action", "think": "reasoning"}.get(kind, "activity")


def summarize_row(content: str) -> dict | None:
    """Turn one raw /history `content` blob into a clean, readable feed row.

    PORTED 1:1 from `main._summarize_history_row`. The history stream is noisy: most rows
    are tool-call JSON (often truncated mid-string by the API), with occasional
    token_count / reasoning frames. We classify the row by its "type" and render a short
    human line — never the raw JSON. Returns `{kind, label, detail}` or None for rows we
    deliberately drop (token_count frames surface in the header readout, not timeline rows).

    kind drives the row styling: 'say' (a plain agent message), 'tool' (an action it took),
    'think' (a reasoning step)."""
    raw = (content or "").strip()
    if not raw:
        return None

    # Plain (non-JSON) text → treat as something the agent said.
    if not raw.startswith("{"):
        return {"kind": "say", "label": _short(raw, _SUMMARY_MAX), "detail": None}

    # Try a full parse first (most rows are truncated, so this usually fails; we fall back
    # to regex field extraction below).
    obj: dict | None = None
    if raw.count("{") == raw.count("}"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                obj = parsed
        except ValueError:
            obj = None

    type_m = _TYPE_RE.search(raw)
    ctype = (obj or {}).get("type") if obj else (type_m.group(1) if type_m else None)

    if ctype == "token_count":
        # Surfaced in the header token readout, not as a timeline row.
        return None

    if ctype == "reasoning":
        return {"kind": "think", "label": "reasoned about the next step", "detail": None}

    if ctype == "function_call":
        name_m = _NAME_RE.search(raw)
        fn = (obj or {}).get("name") if obj else (name_m.group(1) if name_m else "tool")
        fn = fn or "tool"
        # For exec_command, surface the shell cmd as the detail. Prefer a clean parse of the
        # (escaped JSON) `arguments` string when the row parsed fully; otherwise regex the
        # cmd out of the raw (handles truncation).
        detail = None
        cmd = None
        if obj and isinstance(obj.get("arguments"), str):
            try:
                args = json.loads(obj["arguments"])
                if isinstance(args, dict) and isinstance(args.get("cmd"), str):
                    cmd = args["cmd"]
            except ValueError:
                cmd = None
        if cmd is None:
            cmd_m = _CMD_RE.search(raw)
            if cmd_m:
                cmd = cmd_m.group(1).encode().decode("unicode_escape", "ignore")
        if cmd:
            detail = _short(cmd, _DETAIL_MAX)
        return {"kind": "tool", "label": f"ran {fn}", "detail": detail}

    if ctype == "function_call_output":
        return {"kind": "tool", "label": "tool output", "detail": None}

    if ctype == "message":
        # An assistant/user message frame: pull readable text if present.
        text = ""
        if obj:
            c = obj.get("content")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                text = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        return {"kind": "say", "label": _short(text, _SUMMARY_MAX) or "(message)", "detail": None}

    # Unknown structured frame — label it by its type, never dump the JSON.
    return {"kind": "tool", "label": (ctype or "activity").replace("_", " "), "detail": None}


def shape_events(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Shape the raw `/history` messages into the reverse-chronological `events` timeline.

    `history` is the raw `messages` list (each {when, agent_name, role, content}) — newest
    first, as the live API returns it. Each row is run through `summarize_row` (dropping the
    None rows, e.g. token_count frames), the cap is applied to the NEWEST window, and the
    result stays newest-first. Each event carries the readable summary (the label folded with
    its detail), the agent, role, kind, and the timestamp + a relative-age label. Always
    returns a valid (possibly empty) list; never raises on malformed input."""
    rows = history if isinstance(history, list) else []
    events: list[dict[str, Any]] = []
    for m in rows:
        if not isinstance(m, dict):
            continue
        summary = summarize_row(m.get("content", ""))
        if summary is None:
            continue
        label = summary.get("label") or ""
        detail = summary.get("detail")
        # Fold the detail (e.g. the shell cmd) into the readable line so one string carries
        # the whole row — the SPA renders `summary` as the line + can split on the marker.
        text = f"{label} · {detail}" if detail else label
        ts = m.get("when") or ""
        events.append(
            {
                "ts": ts,
                "ts_ago": relative_age(ts),
                "agent": m.get("agent_name") or "—",
                "role": m.get("role") or "",
                "kind": summary.get("kind") or "say",
                "kind_label": _kind_label(summary.get("kind") or "say"),
                "summary": text,
            }
        )
        if len(events) >= HISTORY_EVENT_CAP:
            # The window arrives newest-first; once we've kept the newest CAP rows the rest
            # are older still, so we can stop (keeps the bound + stays newest-first).
            break
    return events


def shape_decisions(search: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Shape the raw `/search` results into the recent-`decisions` feed.

    `search` is the results list (each {text, source, category, when?, agent?, ...}). We keep
    the readable text + its source layer (decisions/lessons/graph/...) + category, plus a
    best-effort ts/ts_ago/agent ('' when the search row carries none). Capped at
    `HISTORY_DECISIONS_CAP`. Always returns a valid (possibly empty) list; never raises."""
    rows = search if isinstance(search, list) else []
    out: list[dict[str, Any]] = []
    for d in rows:
        if not isinstance(d, dict):
            continue
        text = _short(d.get("text") or "", _DECISION_MAX)
        if not text:
            continue
        ts = d.get("when") or d.get("ts") or d.get("created_at") or ""
        out.append(
            {
                "ts": ts,
                "ts_ago": relative_age(ts),
                "agent": d.get("agent") or d.get("agent_name") or "",
                "summary": text,
                "source": d.get("source") or "memory",
                "category": d.get("category") or "",
            }
        )
        if len(out) >= HISTORY_DECISIONS_CAP:
            break
    return out


def roster_agent_count(roster: list[dict[str, Any]] | None) -> int:
    """The distinct-agent count from a `/roster` agents list (the headline count).

    Counts distinct agent names (case-insensitive); falls back to the raw row count when no
    rows carry a name. 0 on an empty/malformed roster. Never raises."""
    rows = roster if isinstance(roster, list) else []
    names = {
        str(r.get("name") or r.get("agent_name") or "").strip().lower()
        for r in rows
        if isinstance(r, dict) and (r.get("name") or r.get("agent_name"))
    }
    names.discard("")
    if names:
        return len(names)
    return len(rows)


__all__ = [
    "summarize_row",
    "relative_age",
    "shape_events",
    "shape_decisions",
    "roster_agent_count",
    "HISTORY_EVENT_CAP",
    "HISTORY_DECISIONS_CAP",
]
