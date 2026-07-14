"""Live harness runner — drive a per-agent harness headlessly on the user's
**logged-in subscription** and stream the reply back as text chunks.

This is the backend half of the agent-detail live chat (R2b) and the dispatch
"Approve & Run". The composer/dispatch resolve the agent's CONFIGURED harness
(from the per-agent settings now in the app-DB) and pass it here; we spawn that
harness as a subprocess and stream its output into the chat feed.

Spawning is done with ``asyncio.create_subprocess_exec`` (argv list, NEVER a
shell string) — there is no shell, so no shell-injection surface: the user's
prompt is passed as a single argv element, not interpolated into a command line.

PER-HARNESS ROUTING
-------------------
`stream_chat(prompt, model=None, system=None, harness=None, run_context=None)` routes by the
agent's configured harness:

  * ``claude-code`` (default / proven path) — the real subscription spawn
    (``claude -p ... --output-format stream-json``); streams partial-message
    text deltas. This is the harness everything fell back to before routing.
  * ``codex`` — REAL best-effort: spawns the ``codex`` CLI non-interactively
    (``codex exec --json``) when it's on PATH, parsing its JSONL events; the
    final ``agent_message`` item is surfaced as the reply and ``turn.completed``
    carries the token usage. Subscription auth (ChatGPT login) is used; provider
    API keys are stripped from the child env exactly like claude-code. If the
    ``codex`` binary is absent we degrade to the same clear, non-crashing message
    as the unwired harnesses below.
  * ``pi`` — REAL best-effort: spawns the ``pi`` CLI non-interactively
    (``pi --mode json -p`` plus either ``--provider openai-codex`` for bare models
    or a provider-prefixed ``--model`` value) when it's on PATH, parsing its
    JSONL session/event stream; ``message_update`` ``text_delta`` frames stream the
    reply text and ``turn_end`` carries token usage + per-turn USD cost. Auth is
    the OpenAI Codex / ChatGPT subscription OAuth (``~/.pi/agent/auth.json``) — NOT
    an API key — so metered keys are stripped from the child env exactly like
    claude-code/codex (verified live). If the ``pi`` binary is absent we degrade
    to the same clear, non-crashing message as the unwired harnesses below.
  * ``kaidera`` (kaidera/API) — REAL API lane: calls the selected provider's
    chat-completions endpoint using keys from Settings / environment / custom
    providers. Chat NEVER crashes on a provider failure; it yields a clear error.

Every path yields the SAME typed event dicts (see EVENTS) and always terminates
with a single ``done`` event, so the caller (main.py → SSE) is harness-agnostic.

Forcing the SUBSCRIPTION (not the metered API): the child env inherits the parent
env but **strips ``ANTHROPIC_API_KEY``** (and the other metered keys) — per the
E007 harness-integration research, an API key present silently bills the metered
API instead of the logged-in Max/Pro (or ChatGPT) plan. We do NOT pass ``--bare``
(that skips OAuth).

EXACT COMMANDS (the real subscription spawns this wires)
--------------------------------------------------------
claude-code:
    claude -p "<prompt>" \
        --output-format stream-json \
        --verbose \
        --include-partial-messages \
        --model <model>

`<model>` is the agent's configured claude model alias (e.g. ``opus`` /
``sonnet`` / ``haiku``) when set, else ``DEFAULT_CLAUDE_MODEL`` (``sonnet`` — a
sensible, lower-cost claude default). `--verbose --include-partial-messages` are
REQUIRED for the partial `stream_event` text deltas (per the research). Auth is
the macOS Keychain OAuth token / `CLAUDE_CODE_OAUTH_TOKEN` already on the box
from the user's `claude` login — we never pass a token or an API key.

codex:
    codex exec --json --skip-git-repo-check -s <sandbox> \
        [-m <model>] "<prompt>"

Runs Codex non-interactively, printing JSONL events to stdout. The final
``item.completed`` (an ``agent_message`` item) carries the reply text; codex emits
the message as one completed item (not token-by-token deltas), so it surfaces as a
single ``result`` event. ``turn.completed.usage`` carries token counts
(``input_tokens`` + ``cached_input_tokens`` folded into tokens_in, ``output_tokens``
+ ``reasoning_output_tokens`` into tokens_out). Auth is the ChatGPT login already
on the box. Inspection-only turns use ``read-only``; operator-directed chat,
manual/approved runs, and autonomous work use ``workspace-write`` with network
access so project-local Cortex/API tools remain usable. Stdin is closed so
``codex exec`` never blocks waiting on piped input.

EVENTS yielded (each a dict with a ``type`` key)
------------------------------------------------
  * ``{"type": "session", "session_id": str, "model": str}``   — system/init frame
  * ``{"type": "delta",   "text": str}``                       — a streamed text chunk
  * ``{"type": "result",  "text": str, "cost_usd": float|None, "session_id": str|None,
       "tokens_in": int|None, "tokens_out": int|None}``
                                                                — the final result frame
                                                                  (token counts from the
                                                                  frame's ``usage`` block,
                                                                  for usage telemetry)
  * ``{"type": "error",   "message": str, "category": str}``   — a clear UI error
  * ``{"type": "done"}``                                        — stream finished cleanly
  * ``{"type": "tasks",   "items": [{"content": str, "status": str, "activeForm"?: str}]}``
                                                                — the agent's task list
                                                                  (claude-code ``TodoWrite``);
                                                                  emitted ALONGSIDE the
                                                                  ``tool`` event, drives the
                                                                  "N/total tasks" indicator
  * ``{"type": "subagent","label": str, "text": str}``         — a sub-agent spawn
                                                                  (claude-code ``Task``);
                                                                  emitted ALONGSIDE the
                                                                  ``tool`` event, drives the
                                                                  sub-agent count

The caller (main.py) maps these onto SSE frames for the browser.

TESTING — injectable command
-----------------------------
The argv is built by `_build_command()`, which honours a command OVERRIDE so a
test can point the runner at a MOCK script that emits a few sample `stream-json`
lines (instead of firing the real, billable `claude -p`):

  * module-level `set_command_override([...])` (highest precedence), or
  * the `HARNESS_CMD_OVERRIDE` env var — a shell-style token list parsed with
    `shlex.split` (e.g. ``"python3 /path/to/mock_claude.py"``).

When an override is set, the override tokens REPLACE the leading ``claude -p``
program+flags; the prompt and the ``--model`` pair are still appended so the
mock can echo them back. Set the override to the empty string / call
`set_command_override(None)` to restore the real `claude` invocation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from . import platform_config

# The real program + headless/streaming flags. The model is appended per-call.
# Kept as a constant so the one place the real subscription is fired is obvious.
CLAUDE_PROGRAM = "claude"
CLAUDE_BASE_FLAGS = (
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
)

# A sensible, lower-cost claude default when the agent has no model override.
# (claude-code accepts the short aliases opus|sonnet|haiku — see app.harness.)
DEFAULT_CLAUDE_MODEL = "sonnet"

# The DEFAULT harness for a new/unconfigured agent — the proven claude-code path.
# (main.py resolves an agent with no override to this + DEFAULT_CLAUDE_MODEL.)
DEFAULT_HARNESS = "claude-code"

# claude-code's `--effort <level>` accepts (VERIFIED `claude --help`, 2.1.206):
# low|medium|high|xhigh|max. Our canonical 'minimal' tier is below claude's floor
# (clamped to 'low' by _claude_effort_args). The configured reasoning level is
# forwarded to this flag so the DEFAULT lane honours it instead of dropping it.
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _claude_program() -> str:
    """Use the same newest-installed Claude CLI as capability discovery."""
    try:
        from app.claude_catalog import _resolve_claude_program

        return _resolve_claude_program(CLAUDE_PROGRAM)
    except Exception:
        return CLAUDE_PROGRAM

# codex non-interactive program + invariant flags. The sandbox is selected per run
# context by `_codex_sandbox_mode`; the prompt (and optional `-m <model>`) are
# appended per call. `--json` emits JSONL and `--skip-git-repo-check` supports
# registered workspaces that are not Git repositories.
CODEX_PROGRAM = "codex"
CODEX_BASE_FLAGS = (
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--color",
    "never",
)

# Contexts that represent real operator-approved or autonomous work. Codex's
# `workspace-write` remains confined to the selected project root; inspection-only
# contexts such as Explain keep the read-only default. We never select
# `danger-full-access` from Kaidera OS.
CODEX_WORKSPACE_WRITE_CONTEXTS = frozenset((
    "chat",
    "autonomous",
    "approve",
    "approve_run",
    "manual",
    "interactive",
    "foreground",
))

# codex exposes NO reasoning CLI flag (VERIFIED `codex --help` / `codex exec
# --help`, codex-cli 0.144.1). The documented lever is the config override
# `-c model_reasoning_effort="<level>"`. The current app-server catalog owns the
# exact per-model values; this union is only the compatibility fallback when model
# discovery is unavailable. It intentionally retains `minimal` for old saved rows.
CODEX_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _codex_program() -> str:
    """Use the same newest-installed Codex CLI as model discovery.

    Multiple package managers can leave different Codex versions on ``PATH``.
    Resolving here prevents a catalog from a new CLI being executed by an older
    binary whose position happens to win for the worker process.
    """
    try:
        from app.codex_catalog import _resolve_codex_program

        return _resolve_codex_program(CODEX_PROGRAM)
    except Exception:
        return CODEX_PROGRAM

# pi non-interactive program + flags (pi 0.80.3). The prompt (and the optional
# `--model`, `--thinking`, `--system-prompt` pairs) are appended per-call.
#   --provider openai-codex  → drive the OpenAI Codex / ChatGPT subscription for
#                              bare model ids; provider-prefixed model ids are
#                              passed directly via --model provider/id
#   --mode json              → JSONL session + event stream on stdout
#   -p                       → non-interactive: process the prompt and exit
#   --no-session             → ephemeral (don't persist a session)
#   --tools read,bash,edit,write → FULL DEV AGENT capability (CTO 2026-06-02):
#                              read files, run shell (bash), edit + write files.
#                              pi has NO sandbox flag — capability IS the explicit
#                              tool allowlist (was --no-tools = pure chat).
#   --no-context-files       → don't auto-load AGENTS.md / CLAUDE.md (deterministic
#                              prompt — only the harness-composed prompt/system)
# All flag names VERIFIED via `pi --help` on pi 0.80.3 (2026-07-10). Auth is the
# openai-codex OAuth in `~/.pi/agent/auth.json` (NOT an API key); the stripped
# child env keeps a metered key from ever overriding that — verified live.
PI_PROGRAM = "pi"
PI_PROVIDER = "openai-codex"
# The DEFAULT pi model — ollama-cloud deepseek-v4-pro (the operator's preferred
# provider/model). Provider-prefixed so _build_pi_command does NOT force
# --provider openai-codex for this default; the runner passes it as
# --model ollama-cloud/deepseek-v4-pro directly.
PI_DEFAULT_MODEL = "ollama-cloud/deepseek-v4-pro"
PI_BASE_FLAGS = (
    "--mode",
    "json",
    "-p",
    "--no-session",
    # FULL DEV AGENT tool policy: read + bash(shell) + edit + write (verified tool
    # names via `pi --help`). multi_tool_use.parallel omitted (sequential is fine).
    # NOTE: --no-context-files keeps the prompt deterministic (harness-composed only).
    # --no-extensions --no-skills are NOT forced here — the operator's pi extensions
    # (ollama-cloud, fireworks, etc.) must be active for provider-prefixed models.
    "--tools",
    "read,bash,edit,write",
    "--no-context-files",
)

# Per-turn wall-clock cap (seconds) for background/autonomous lanes. A hung or
# looping child is killed and surfaced as a clear timeout error rather than
# blocking a worker forever. Foreground/manual lanes opt out via
# _turn_read_timeout(); explicit user cancellation still closes the generator and
# kills the child in each lane's finally block.
TURN_TIMEOUT_S = float(os.environ.get("HARNESS_TURN_TIMEOUT_S", "120"))

_FOREGROUND_RUN_CONTEXTS = frozenset((
    "chat",
    "approve",
    "approve_run",
    "manual",
    "interactive",
    "foreground",
    "explain",
))


def _normalise_run_context(run_context: str | None) -> str:
    return (run_context or "").strip().lower().replace("-", "_")


def _turn_read_timeout(run_context: str | None = None) -> float | None:
    """Read timeout for one silent child-output interval.

    Foreground/manual work is operator-cancellable, so it must not be killed by the
    short background turn cap while a real agent is thinking or running a long tool.
    Background/autonomous/unknown contexts keep the finite watchdog timeout.
    """
    return None if _normalise_run_context(run_context) in _FOREGROUND_RUN_CONTEXTS else TURN_TIMEOUT_S


# Once pi has emitted assistant text, a missing terminal frame should not leave the
# chat row open for the full turn timeout. Some pi/provider builds keep the process
# alive after the answer text; treat a short post-output idle as a completed turn.
# This is intentionally only the AFTER-TEXT tail: first-token/tool silence still uses
# the full turn timeout above.
PI_IDLE_AFTER_TEXT_TIMEOUT_S = float(
    os.environ.get("HARNESS_PI_IDLE_AFTER_TEXT_TIMEOUT_S", "3")
)

# asyncio StreamReader buffer ceiling for the child's stdout. The default is 64KB
# (asyncio.streams._DEFAULT_LIMIT) — and `readline()` raises
# ``asyncio.LimitOverrunError`` ("Separator is not found, and chunk exceed the
# limit") if a single line (no newline yet) grows past it. Both claude-code and
# codex emit JSON events one-per-line, and a real task can emit a single
# ``--json`` event line WELL over 64KB (e.g. a long agent_message), which killed
# the whole run. Raise the ceiling to 16MB so large JSON frames fit in one line.
STREAM_BUFFER_LIMIT = 16 * 1024 * 1024

# Metered-API keys we MUST strip from the child env so the CLI uses the
# logged-in SUBSCRIPTION (Keychain OAuth) and never silently bills the API.
# (Research §"Cross-cutting backend rules" #1 — claude reads ANTHROPIC_API_KEY;
# the rest are stripped defensively so this stays correct if we add codex etc.)
_METERED_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
)

# Module-level command override (set by tests). None → use the real claude argv.
_CMD_OVERRIDE: list[str] | None = None


def set_command_override(tokens: list[str] | None) -> None:
    """Point the runner at a MOCK command (tests) or restore the real one.

    `tokens` replaces the leading ``claude`` program + base flags; the prompt and
    ``--model <model>`` are still appended (so a mock can echo them). Pass None
    (or []) to restore the real `claude -p ...` invocation."""
    global _CMD_OVERRIDE
    _CMD_OVERRIDE = list(tokens) if tokens else None


def _split_override_env(var_name: str) -> list[str] | None:
    """Return a shlex-split command override from an env var, or None.

    Used by the non-claude lanes so tests can point a runner at a mock executable
    without firing a real/billable harness. Invalid shell quoting falls back to a
    simple whitespace split, matching the legacy HARNESS_CMD_OVERRIDE behaviour."""
    env = os.environ.get(var_name, "").strip()
    if env:
        try:
            toks = shlex.split(env)
        except ValueError:
            toks = env.split()
        return toks or None
    return None


def _override_tokens() -> list[str] | None:
    """Resolve the active command override: the module-level setter wins, else
    the `HARNESS_CMD_OVERRIDE` env var (shlex-split), else None (real claude)."""
    if _CMD_OVERRIDE:
        return list(_CMD_OVERRIDE)
    return _split_override_env("HARNESS_CMD_OVERRIDE")


def resolve_model(model: str | None) -> str:
    """The claude model alias to spawn with: the agent's configured value when a
    non-blank string, else the lower-cost DEFAULT_CLAUDE_MODEL."""
    m = (model or "").strip()
    return m or DEFAULT_CLAUDE_MODEL


def _claude_effort_args(
    reasoning: str | None,
    model: str | None = None,
) -> list[str]:
    """Map a routed reasoning level to claude-code's ``--effort <level>`` flag.

    Mirrors pi's ``--thinking`` passthrough so the operator-configured reasoning
    level reaches the DEFAULT lane instead of being silently dropped. The stored
    value is normalized via ``app.reasoning`` (so "med"/"on"/"off" aliases resolve
    correctly), then:
      * OFF / empty / a bare "on" / an unrecognized token → NO flag (claude uses
        its own default — a correct, reasoning-default call).
      * "minimal" → "low" (claude's effort floor; it has no lower setting).
      * any value in ``CLAUDE_EFFORT_LEVELS`` → ``["--effort", <level>]``.
    Returns a (possibly empty) argv fragment, never raises."""
    from app import reasoning as _reasoning

    lvl = _reasoning.normalize_level(reasoning)
    if not lvl or lvl == "_on_":
        return []
    supported: list[str] | None = None
    if (model or "").strip():
        try:
            from app import harness as _harness

            option = next(
                (
                    row
                    for row in _harness.harness_model_options("claude-code")
                    if row.get("value") == (model or "").strip()
                ),
                None,
            )
            if option is not None and "reasoning_levels" in option:
                supported = list(option.get("reasoning_levels") or [])
        except Exception:
            supported = None
    if supported is not None:
        if lvl in supported:
            return ["--effort", lvl]
        # Older Claude versions have no minimal tier. Clamp only when the live
        # CLI does not advertise minimal; a future advertised tier passes through.
        if lvl == "minimal" and "low" in supported:
            return ["--effort", "low"]
        return []
    if lvl == "minimal":
        lvl = "low"
    if lvl in CLAUDE_EFFORT_LEVELS:
        return ["--effort", lvl]
    return []


def _build_command(prompt: str, model: str, reasoning: str | None = None) -> list[str]:
    """Assemble the subprocess argv (list, for exec — never a shell string).

    Real path:  ``claude -p <prompt> --output-format stream-json --verbose
                  --include-partial-messages --model <model> [--effort <level>]``
    Override path: ``<override tokens...> <prompt> --model <model> [--effort
                  <level>]`` so a mock sees the same trailing prompt + model it can
    echo back. The ``--effort`` pair is appended only when a real reasoning level
    is routed in (``reasoning=None`` → byte-for-byte the legacy argv)."""
    override = _override_tokens()
    effort = _claude_effort_args(reasoning, model)
    if override is not None:
        return [*override, prompt, "--model", model, *effort]
    return [_claude_program(), *CLAUDE_BASE_FLAGS, prompt, "--model", model, *effort]


def _child_env() -> dict[str, str]:
    """The child environment: inherit the parent env but STRIP the metered-API
    keys so the CLI authenticates via the logged-in subscription (Keychain
    OAuth), never the metered API. Returns a copy (parent env untouched)."""
    env = dict(os.environ)
    for key in _METERED_ENV_KEYS:
        env.pop(key, None)
    return env


def _pi_model_provider(model: str | None) -> str:
    """Provider PI will use for this model value.

    Bare values are OpenAI-Codex subscription models. Provider-prefixed values use
    their first path component, matching PI's documented `provider/id` model syntax.
    """
    m = (model or "").strip()
    if "/" in m:
        provider = m.split("/", 1)[0].strip()
        return provider or PI_PROVIDER
    return PI_PROVIDER


def _pi_program() -> str:
    """Use the same newest-installed PI CLI as model discovery."""
    try:
        from app.pi_catalog import _resolve_pi_program

        return _resolve_pi_program(PI_PROGRAM)
    except Exception:
        return PI_PROGRAM


def _pi_child_env(model: str | None = None) -> dict[str, str]:
    """The pi child env plus two pi-specific knobs.

    pi authenticates via the openai-codex OAuth in ``~/.pi/agent/auth.json``
    for bare OpenAI-Codex model ids, so those turns still strip metered provider
    keys. Provider-prefixed PI model ids (for example ``fireworks/...``) need the
    host's provider keys, so those turns preserve the service environment.
    ``PI_SKIP_VERSION_CHECK=1`` and ``PI_TELEMETRY=0`` quiet pi's startup version
    check / telemetry side-effects. We never read or log ``auth.json`` / tokens."""
    provider = _pi_model_provider(model)
    env = _child_env() if provider == PI_PROVIDER else dict(os.environ)
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["PI_TELEMETRY"] = "0"
    return env


def _apply_project_workspace(env: dict[str, str], project_key: str | None,
                             workspace: str | None) -> dict[str, str]:
    """Scope a harness child env to the SELECTED project's workspace.

    The console is a multi-project UI: when the operator chats with an agent in
    project X, the spawned harness (and any cortex-* CLI the agent shells) must
    run IN project X's repo_root and resolve project X's `.agents/scripts` FIRST
    on PATH — otherwise the Cortex CLI's workspace-path isolation guard boots the
    agent under the console's own workspace and every command runs in
    the wrong folder. Returns `env` mutated (PATH prepended with the project's
    `.agents/scripts`, CORTEX_PROJECT set to the project key). No-op when
    `workspace` is absent (legacy/single-project path, byte-for-byte unchanged).
    Also pins the agent-facing Cortex API URL when the parent did not provide one:
    some harness sandboxes fail on the `localhost` default while `127.0.0.1` works
    for the same host service."""
    ws = (workspace or "").strip()
    if ws:
        scripts = os.path.join(ws, ".agents", "scripts")
        env["PATH"] = (scripts + os.pathsep + env.get("PATH", ""))
        env["KAIDERA_AGENT_WORKSPACE"] = ws
    if project_key:
        env["CORTEX_PROJECT"] = project_key
    env.setdefault("CORTEX_API_URL", "http://127.0.0.1:8501")
    return env


def _compose_prompt(prompt: str, system: str | None) -> str:
    """Light system context: optionally prepend the agent's role as a one-line
    framing header above the user's message. Kept deliberately simple (a real
    per-agent system-prompt / persona wiring is a follow-up). Blank `system`
    leaves the prompt untouched."""
    sys_line = (system or "").strip()
    integrity = (
        "Execution integrity: never claim an external action, connector approval, "
        "file change, command, or handoff happened unless you actually performed it "
        "with an available tool in this turn. If the required tool or connector is "
        "unavailable, say so plainly and give the exact next action."
    )
    if not sys_line:
        return f"{integrity}\n\n{prompt}"
    return f"{sys_line}\n\n{integrity}\n\n{prompt}"


# ---------------------------------------------------------------------------
#  NDJSON frame → event mapping
# ---------------------------------------------------------------------------

# Error categories the research calls out on a system/api_retry frame. We map
# them to friendly, actionable UI copy.
_AUTH_CATEGORIES = ("authentication_failed", "authentication", "unauthorized")
_RATELIMIT_CATEGORIES = ("rate_limit", "rate_limited", "overloaded")

# Hard cap on the per-tool-use input summary we surface (the raw `input` blob of a
# tool_use can be huge — a whole file, a giant patch — so we NEVER carry it whole;
# the transcript only needs the tool name + a short arg preview). The orchestrator
# store has its own per-run byte cap on top of this; this just keeps any single
# tool/command line short and readable. Secrets are not expanded — we only ever
# stringify what the harness already put in the frame and truncate it.
_TOOL_SUMMARY_MAX = 200


def _summarize_tool_input(value: Any) -> str:
    """A SHORT, single-line preview of a tool_use/command argument blob.

    Bounds the output to ``_TOOL_SUMMARY_MAX`` characters (a tool's ``input`` can be
    an entire file or patch — we must never store the whole blob). A dict/list is
    compact-JSON-encoded then truncated; a string is collapsed to one line then
    truncated; anything else is ``str()``-ed. Best-effort + total: any failure
    yields ``""`` so a malformed input never breaks the run. We do NOT expand env
    vars or resolve anything — this is a verbatim, truncated echo of what the
    harness already emitted."""
    if value is None:
        return ""
    try:
        if isinstance(value, str):
            s = value
        else:
            s = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        try:
            s = str(value)
        except Exception:
            return ""
    # Collapse whitespace/newlines to keep the summary a single tidy line.
    s = " ".join(s.split())
    if len(s) > _TOOL_SUMMARY_MAX:
        s = s[:_TOOL_SUMMARY_MAX] + "…"
    return s


def _tool_use_event(name: Any, tool_input: Any) -> dict[str, Any]:
    """Build a ``tool`` runner event from a tool name + (bounded) input summary.

    The event carries both a structured ``name`` and a ready-to-render ``text``
    (``name(arg-preview)`` or just ``name`` when there are no args). The transcript
    stores ``text``; ``name`` is there for any future structured use."""
    nm = (str(name).strip() if name is not None else "") or "tool"
    summary = _summarize_tool_input(tool_input)
    text = f"{nm}({summary})" if summary else nm
    return {"type": "tool", "name": nm, "text": text}


# Canonical TodoWrite statuses (claude-code's task-list tool). An unrecognised
# value is kept verbatim (defensive) but the done-count below only credits
# "completed", so an unexpected status simply isn't counted as done.
_TODO_STATUSES = ("pending", "in_progress", "completed")
_TASK_CONTENT_MAX = 200  # bound each task label like a tool-arg preview


def _tool_extra_events(name: Any, tool_input: Any) -> list[dict[str, Any]]:
    """STRUCTURED side-events for the tasks/sub-agent indicator (in ADDITION to the
    normal ``tool`` event, which the caller still emits so the feed keeps showing the
    raw activity).

    * ``TodoWrite`` → ``{"type":"tasks","items":[{"content","status"}...]}`` parsed
      from ``tool_input["todos"]`` (claude-code's task-list tool). Each todo dict
      carries ``content`` + ``status`` ∈ {pending,in_progress,completed}; an
      ``activeForm`` (the in-progress phrasing) is forwarded when present so the UI
      can show the active label. The list may be empty (a cleared task list).
    * ``Task``      → ``{"type":"subagent","label":..,"text":..}`` (claude-code's
      sub-agent spawn tool). The label prefers the human ``description``, then the
      ``subagent_type``, then a generic fallback.

    TOTAL + defensive: ANY malformed shape (``tool_input`` not a dict, ``todos`` not
    a list, a todo that isn't a dict, a non-string field) is skipped, and the worst
    case is an empty list — this NEVER raises, so the chat stream can't break on an
    unexpected payload. Non-task/sub-agent tools yield ``[]``."""
    nm = (str(name).strip() if name is not None else "")
    if nm not in ("TodoWrite", "Task"):
        return []
    try:
        if not isinstance(tool_input, dict):
            return []
        if nm == "TodoWrite":
            todos = tool_input.get("todos")
            if not isinstance(todos, list):
                return []
            items: list[dict[str, Any]] = []
            for td in todos:
                if not isinstance(td, dict):
                    continue
                content = td.get("content")
                if not isinstance(content, str):
                    content = "" if content is None else str(content)
                content = " ".join(content.split())
                if len(content) > _TASK_CONTENT_MAX:
                    content = content[:_TASK_CONTENT_MAX] + "…"
                status = td.get("status")
                status = status if status in _TODO_STATUSES else (
                    str(status) if status is not None else "pending"
                )
                item: dict[str, Any] = {"content": content, "status": status}
                active = td.get("activeForm")
                if isinstance(active, str) and active.strip():
                    item["activeForm"] = active.strip()
                items.append(item)
            return [{"type": "tasks", "items": items}]
        # nm == "Task" — a sub-agent spawn.
        label = tool_input.get("description") or tool_input.get("subagent_type") or "sub-agent"
        if not isinstance(label, str) or not label.strip():
            label = "sub-agent"
        label = " ".join(label.split())
        if len(label) > _TASK_CONTENT_MAX:
            label = label[:_TASK_CONTENT_MAX] + "…"
        text = _summarize_tool_input(tool_input.get("prompt")) or label
        return [{"type": "subagent", "label": label, "text": text}]
    except Exception:
        # Belt-and-braces: a structured-event failure must never break the turn —
        # the normal tool event (emitted by the caller) still carries the activity.
        return []

_NOT_LOGGED_IN_MSG = (
    "claude-code is not logged in — run `claude /login` in a terminal "
    "(it uses your Claude subscription, no API key)."
)
_RATE_LIMIT_MSG = (
    "claude-code hit a rate / usage limit on your subscription — wait a moment "
    "and try again."
)


def _extract_delta_text(frame: dict[str, Any]) -> str | None:
    """Pull streamed text from a `stream_event` frame: `.event.delta.text_delta`
    (the partial-message text-delta shape). Tolerant of a missing path."""
    event = frame.get("event")
    if not isinstance(event, dict):
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    # Anthropic streaming uses {"type":"text_delta","text":"..."}; the research
    # names the field `text_delta`, so accept either key defensively.
    text = delta.get("text")
    if not isinstance(text, str):
        text = delta.get("text_delta")
    return text if isinstance(text, str) and text else None


def _extract_thinking_delta(frame: dict[str, Any]) -> str | None:
    """Pull streamed EXTENDED-THINKING text from a `stream_event` frame's
    ``content_block_delta`` of type ``thinking_delta``
    (``.event.delta = {"type":"thinking_delta","thinking":"..."}``).

    This is the live, token-by-token thinking stream that ``--include-partial-messages``
    emits, mirroring the text-delta path. ``signature_delta`` /
    ``input_json_delta`` deltas are NOT thinking text → ignored here (the tool name +
    full input come from the complete ``assistant`` content block instead). Tolerant
    of a missing path (→ None)."""
    event = frame.get("event")
    if not isinstance(event, dict):
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    if delta.get("type") not in (None, "thinking_delta"):
        # A non-thinking delta (text_delta / signature_delta / input_json_delta).
        return None
    text = delta.get("thinking")
    if not isinstance(text, str):
        text = delta.get("thinking_delta")
    return text if isinstance(text, str) and text else None


def _events_from_assistant_blocks(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a complete ``assistant`` message frame's content blocks to runner
    events for THINKING + TOOL-USE (NOT text).

    A claude-code stream-json ``assistant`` frame carries the final assembled
    message: ``frame.message.content`` is a list of typed blocks. We surface:
      * ``thinking``           → ``{"type":"thinking","text": <thinking>}``
      * ``redacted_thinking``  → a short ``[redacted thinking]`` thinking marker
      * ``tool_use``           → ``{"type":"tool", ...}`` (name + bounded arg summary)
    We deliberately SKIP ``text`` blocks here — the reply text already streamed via
    the ``stream_event`` text-deltas (and via the final ``result``); re-emitting it
    would DUPLICATE the output. Plain ``thinking`` blocks are returned too, but the
    caller (``_stream_claude``) drops them when thinking already streamed live via
    ``thinking_delta`` (so thinking is shown once, not twice). Tolerant of any
    missing/odd shape (→ ``[]``)."""
    msg = frame.get("message")
    if not isinstance(msg, dict):
        return []  # not the message-bearing shape
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            txt = block.get("thinking")
            if isinstance(txt, str) and txt:
                out.append({"type": "thinking", "text": txt})
        elif btype == "redacted_thinking":
            out.append({"type": "thinking", "text": "[redacted thinking]"})
        elif btype == "tool_use":
            # The normal tool event keeps the raw activity in the feed; the extra
            # events (tasks / subagent) drive the structured indicator. Both are
            # appended so the caller (SSE mapper) can surface each.
            out.append(_tool_use_event(block.get("name"), block.get("input")))
            out.extend(_tool_extra_events(block.get("name"), block.get("input")))
        # text / other block types: skipped (text already streamed; see docstring).
    return out


def _classify_error(category: str) -> tuple[str, str]:
    """Map a raw error category to (canonical_category, friendly_message)."""
    cat = (category or "").lower()
    if any(a in cat for a in _AUTH_CATEGORIES):
        return "authentication_failed", _NOT_LOGGED_IN_MSG
    if any(r in cat for r in _RATELIMIT_CATEGORIES):
        return "rate_limit", _RATE_LIMIT_MSG
    return (category or "error"), f"claude-code reported an error: {category or 'unknown'}"


def _extract_usage(frame: dict[str, Any]) -> tuple[int | None, int | None]:
    """Pull (tokens_in, tokens_out) from a claude-code `result` frame's usage.

    The stream-json `result` frame carries a `usage` object in the Anthropic SDK
    shape — `{input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens}`. We fold the cache fields into the input total
    (they ARE input tokens, billed at a cache rate) so the telemetry captures the
    full prompt size. Tolerant of a missing/partial `usage` block (→ (None, None)),
    so a frame without usage simply records no token counts."""
    usage = frame.get("usage")
    if not isinstance(usage, dict):
        return None, None

    def _g(*keys: str) -> int | None:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, int):
                return v
        return None

    inp = _g("input_tokens", "prompt_tokens")
    cache_read = _g("cache_read_input_tokens", "cache_read_tokens")
    cache_create = _g("cache_creation_input_tokens", "cache_creation_tokens")
    out = _g("output_tokens", "completion_tokens")

    # Combine the (possibly several) input-side counters into one total.
    in_parts = [v for v in (inp, cache_read, cache_create) if isinstance(v, int)]
    tokens_in = sum(in_parts) if in_parts else None
    return tokens_in, out


def _parse_frame(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """Map ONE parsed NDJSON frame to a LIST of runner event dicts (``[]`` to drop).

    Returns a list because a single frame can now yield several events: an
    ``assistant`` message frame may carry a thinking block PLUS one or more
    ``tool_use`` blocks. The pre-existing single-event frames still return a
    one-element list (callers iterate uniformly).

    Handles:
      * ``system``/init        → ``session``
      * ``system``/api_retry   → ``error`` (auth / rate-limit)
      * ``stream_event``       → ``delta`` (reply text) and/or ``thinking`` (live
                                 extended-thinking deltas)
      * ``assistant``          → ``thinking`` + ``tool`` (the complete content
                                 blocks; reply ``text`` is SKIPPED — it already
                                 streamed via the deltas, so re-emitting it would
                                 duplicate the output)
      * ``result``             → ``result`` (final text + cost + usage) or ``error``
    Anything else is dropped (we never surface raw harness JSON to the feed)."""
    ftype = frame.get("type")

    if ftype == "system":
        subtype = frame.get("subtype")
        if subtype == "init":
            return [{
                "type": "session",
                "session_id": frame.get("session_id"),
                "model": frame.get("model"),
            }]
        if subtype == "api_retry":
            # The error payload may be a dict ({"category": ...}) or a string.
            err = frame.get("error")
            category = ""
            if isinstance(err, dict):
                category = str(err.get("category") or err.get("type") or "")
            elif isinstance(err, str):
                category = err
            canon, message = _classify_error(category)
            return [{"type": "error", "category": canon, "message": message}]
        return []

    if ftype == "stream_event":
        out: list[dict[str, Any]] = []
        # Reply text deltas — the OUTPUT stream (unchanged behaviour).
        text = _extract_delta_text(frame)
        if text:
            out.append({"type": "delta", "text": text})
        # Extended-thinking deltas — live, token-by-token (new). A given delta is
        # EITHER text or thinking, never both, so these don't overlap.
        thinking = _extract_thinking_delta(frame)
        if thinking:
            out.append({"type": "thinking", "text": thinking})
        return out

    if ftype == "assistant":
        # The complete assembled assistant message — source of TOOL-USE blocks and
        # (as a fallback) full thinking blocks. Reply text is intentionally skipped
        # here (already streamed via the text-deltas above). Returns thinking + tool
        # events; _stream_claude drops the thinking ones if thinking already
        # streamed live (so it shows once).
        blocks = _events_from_assistant_blocks(frame)
        return blocks or []

    if ftype == "result":
        # Final assembled reply. `is_error` / `subtype` flag a failed turn.
        if frame.get("is_error") or frame.get("subtype") not in (None, "success"):
            raw = frame.get("result")
            msg = raw if isinstance(raw, str) and raw else "claude-code did not complete the turn."
            return [{"type": "error", "category": "result_error", "message": msg}]
        result = frame.get("result")
        tokens_in, tokens_out = _extract_usage(frame)
        return [{
            "type": "result",
            "text": result if isinstance(result, str) else "",
            "cost_usd": frame.get("total_cost_usd"),
            "session_id": frame.get("session_id"),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }]

    return []


def _looks_like_auth_failure(stderr: str, returncode: int | None) -> bool:
    """Heuristic: did a non-zero exit / stderr indicate a not-logged-in state?

    claude-code with no OAuth token (and no API key, since we strip it) exits
    non-zero and prints a login/auth hint to stderr. We surface that as the clear
    'run claude /login' message rather than a raw stderr dump."""
    low = (stderr or "").lower()
    auth_words = (
        "not logged in",
        "log in",
        "login",
        "/login",
        "authenticate",
        "authentication",
        "unauthorized",
        "oauth",
        "no api key",
        "credit balance",
        "setup-token",
    )
    return bool(returncode) and any(w in low for w in auth_words)


# ---------------------------------------------------------------------------
#  The harness ROUTER + per-harness async generators
# ---------------------------------------------------------------------------


def canonical_harness_key(harness: str | None) -> str:
    """Normalise a harness value to a runner key. Blank → the default harness
    (claude-code). Lower-cased, whitespace-stripped. Unknown values pass through
    (the router then sends them down the graceful path)."""
    h = (harness or "").strip().lower()
    return h or DEFAULT_HARNESS


async def stream_chat(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    harness: str | None = None,
    reasoning: str | None = None,
    workspace: str | None = None,
    project_key: str | None = None,
    run_context: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """ROUTE a chat/dispatch turn to the agent's configured harness and yield its
    streamed reply events.

    `harness` selects the lane (default ``claude-code``):
      * ``claude-code`` → the real subscription claude spawn (``_stream_claude``).
      * ``codex``       → the real codex non-interactive spawn (``_stream_codex``);
                          if the ``codex`` binary is absent, degrades gracefully.
      * ``pi``          → the real pi non-interactive spawn (``_stream_pi``,
                          ``pi --mode json``); if the
                          ``pi`` binary is absent, degrades gracefully.
      * ``kaidera`` / ``kaidera`` → configured provider API lane.
      * ``kaidera-no-tools`` / ``kaidera-singleshot`` / ``no-tools`` →
                          configured provider API lane without shell/file tools.
      * anything else   → graceful status message (unknown harness).

    Every lane yields the same event dicts (session / delta / result / error /
    done) and always terminates with a single ``done`` event, so the caller
    (main.py → SSE) is harness-agnostic. The HARNESS_CMD_OVERRIDE mock applies to
    the claude-code lane (the test surface)."""
    prompt = (prompt or "").strip()
    if not prompt:
        yield {"type": "error", "category": "empty_prompt", "message": "Empty message — nothing to send."}
        yield {"type": "done"}
        return

    key = canonical_harness_key(harness)

    if key == "claude-code":
        # claude-code's `--effort` levels (low/medium/high/xhigh/max) cover our
        # reasoning config, so forward it (mirrors the pi lane). Without this the
        # DEFAULT lane ran at claude's own default rather than the configured level.
        async for ev in _stream_claude(
            prompt, model, system, reasoning=reasoning, workspace=workspace,
            project_key=project_key, run_context=run_context
        ):
            yield ev
        return
    if key == "codex":
        # codex has no reasoning flag — forwarded as the documented
        # `-c model_reasoning_effort=<level>` config override (mirrors the pi lane).
        async for ev in _stream_codex(
            prompt, model, system, reasoning=reasoning, workspace=workspace,
            project_key=project_key, run_context=run_context
        ):
            yield ev
        return
    if key == "pi":
        # pi's --thinking levels (off/minimal/low/medium/high/xhigh) match our
        # reasoning config 1:1, so forward it straight through. Without this the PM
        # ran at pi's provider default (which can be a runaway-high level) instead of
        # the medium the operator configured.
        async for ev in _stream_pi(
            prompt, model, system, thinking=reasoning, workspace=workspace,
            project_key=project_key, run_context=run_context
        ):
            yield ev
        return
    if key == "kaidera":  # fitness:allow-literal canonical harness id routes the own-harness lane, not a per-project literal
        async for ev in _stream_kaidera(prompt, model, system, thinking=reasoning, workspace=workspace, project_key=project_key):
            yield ev
        return
    if key in ("kaidera-no-tools", "kaidera-singleshot", "no-tools"):  # fitness:allow-literal harness lane aliases, not project names
        async for ev in _stream_kaidera_singleshot(prompt, model, system, reasoning, workspace=workspace):
            yield ev
        return
    # Unknown value — graceful fallback.
    async for ev in _stream_graceful(key, model):
        yield ev


async def _stream_claude(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    reasoning: str | None = None,
    workspace: str | None = None,
    project_key: str | None = None,
    run_context: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Spawn claude-code on the subscription and yield streamed reply events.

    Yields the event dicts documented in the module docstring (session / delta /
    result / error / done). Always terminates with a single ``done`` event (even
    after an error) so the caller can close the SSE stream deterministically.

    Robust to: missing binary (FileNotFoundError → clear install message),
    not-logged-in (auth heuristic on a non-zero exit), per-turn timeout
    (TURN_TIMEOUT_S → kill + timeout error), malformed NDJSON lines (skipped),
    and a non-zero exit with no parsed error (raw stderr tail surfaced)."""
    chosen_model = resolve_model(model)
    argv = _build_command(_compose_prompt(prompt, system), chosen_model, reasoning=reasoning)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_apply_project_workspace(_child_env(), project_key, workspace),
            cwd=workspace or None,
            # Raise the StreamReader buffer ceiling above 64KB so a single large
            # stream-json line (a long partial-message/result frame) doesn't trip
            # asyncio's "Separator is not found, and chunk exceed the limit".
            limit=STREAM_BUFFER_LIMIT,
        )
    except FileNotFoundError:
        prog = argv[0] if argv else "claude"
        yield {
            "type": "error",
            "category": "binary_not_found",
            "message": (
                f"`{prog}` was not found on PATH. Install Claude Code "
                "(`npm i -g @anthropic-ai/claude-code`) and run `claude /login`."
            ),
        }
        yield {"type": "done"}
        return
    except OSError as exc:  # pragma: no cover - exec-layer failure
        yield {"type": "error", "category": "spawn_failed", "message": f"Failed to start the harness: {exc}"}
        yield {"type": "done"}
        return

    saw_error = False
    saw_text = False
    # Thinking de-dup: extended thinking arrives BOTH as live `thinking_delta`
    # `stream_event`s AND, again whole, inside the final `assistant` message block.
    # We stream the live deltas (best UX) and then DROP the assistant-frame thinking
    # so it isn't shown twice. If a build emits no thinking deltas but does carry the
    # assistant thinking block, this stays False and the block is surfaced — so
    # thinking is shown exactly once either way.
    streamed_thinking = False
    # Stall-proof stdout drain (see _PipeLineReader) — a busy/blocked loop can't
    # backpressure claude-code's OS pipe. Closed in `finally`.
    assert proc.stdout is not None
    reader = _PipeLineReader(proc.stdout)
    reader.start()
    turn_timeout = _turn_read_timeout(run_context)
    try:
        while True:
            try:
                line = await reader.readline(turn_timeout)
            except asyncio.TimeoutError:
                _kill(proc)
                yield {
                    "type": "error",
                    "category": "timeout",
                    "message": f"claude-code did not respond within {int(TURN_TIMEOUT_S)}s — turn aborted.",
                }
                saw_error = True
                break

            if line is _OVERSIZED_LINE:
                # One frame overran even the 16MB buffer — skip it (logged) and
                # keep streaming the rest of the turn instead of crashing.
                print(
                    "[harness_runner] claude-code emitted a stdout line over "
                    f"{STREAM_BUFFER_LIMIT} bytes — skipping that frame.",
                    flush=True,
                )
                continue

            assert isinstance(line, bytes)
            if not line:  # EOF
                break

            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                # Non-JSON noise (e.g. a stray log line) — skip, never surface raw.
                continue
            if not isinstance(frame, dict):
                continue

            is_assistant = frame.get("type") == "assistant"
            for event in _parse_frame(frame):
                etype = event.get("type")
                # Thinking de-dup: if the live deltas already streamed thinking,
                # drop the duplicate thinking carried in the assistant frame (keep
                # its tool_use events). The first thinking we see flips the flag.
                if etype == "thinking":
                    if is_assistant and streamed_thinking:
                        continue
                    if not is_assistant:
                        streamed_thinking = True
                if etype == "delta":
                    saw_text = True
                elif etype == "result" and event.get("text"):
                    saw_text = True
                elif etype == "error":
                    saw_error = True
                yield event

        # Reap the process + inspect the exit code / stderr for a clean finish.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            _kill(proc)

        stderr_text = ""
        if proc.stderr is not None:
            try:
                stderr_text = (await proc.stderr.read()).decode("utf-8", "replace")
            except (OSError, ValueError):  # pragma: no cover - defensive
                stderr_text = ""

        if not saw_error:
            rc = proc.returncode
            if _looks_like_auth_failure(stderr_text, rc):
                yield {"type": "error", "category": "authentication_failed", "message": _NOT_LOGGED_IN_MSG}
                saw_error = True
            elif rc not in (0, None) and not saw_text:
                tail = " ".join(stderr_text.split())[-300:]
                yield {
                    "type": "error",
                    "category": "nonzero_exit",
                    "message": (
                        f"claude-code exited with code {rc}"
                        + (f": {tail}" if tail else " (no output).")
                    ),
                }
                saw_error = True
    finally:
        # Never leak a child if the consumer stops iterating early (client
        # disconnect) or an unexpected error bubbles out. Close the drain task too.
        await reader.close()
        if proc.returncode is None:
            _kill(proc)

    yield {"type": "done"}


def _kill(proc: asyncio.subprocess.Process) -> None:
    """Best-effort terminate of a still-running child (no raise on a dead pid)."""
    try:
        proc.kill()
    except ProcessLookupError:  # already gone
        pass
    except OSError:  # pragma: no cover - defensive
        pass


# Sentinel returned by `_readline_resilient` when a single line overran the (now
# 16MB) stream buffer — we skip that one oversized frame rather than crash, and
# the read loop keeps going. Distinct from b"" (EOF) and a real line.
_OVERSIZED_LINE = object()


async def _readline_resilient(reader: asyncio.StreamReader) -> bytes | object:
    """Read one newline-terminated line, tolerating an oversized frame.

    Even with a raised ``limit=`` (STREAM_BUFFER_LIMIT), a pathologically huge
    single JSON line could still exceed the ceiling. Rather than let
    ``asyncio.LimitOverrunError`` kill the whole run (the original
    "Separator is not found, and chunk exceed the limit" bug), we DRAIN the
    consumed bytes and return the ``_OVERSIZED_LINE`` sentinel so the caller can
    log a warning and skip just that frame. ``IncompleteReadError`` (EOF mid-line)
    is treated as a partial final line (returned as-is) so its bytes are not lost.

    Returns: a ``bytes`` line (possibly empty b"" at EOF), or the
    ``_OVERSIZED_LINE`` sentinel when a frame overran the buffer."""
    try:
        return await reader.readline()
    except asyncio.LimitOverrunError as exc:
        # The separator (newline) wasn't found within `limit` bytes. Consume the
        # bytes already buffered (exc.consumed) so the reader can make progress to
        # the next line instead of re-raising on the same overrun forever.
        try:
            await reader.readexactly(exc.consumed)
        except (asyncio.IncompleteReadError, Exception):  # pragma: no cover - defensive
            pass
        return _OVERSIZED_LINE
    except asyncio.IncompleteReadError as exc:
        # EOF reached before a newline — return whatever partial bytes we got so a
        # final newline-less frame is still parsed (not silently dropped).
        return exc.partial
    except ValueError:  # pragma: no cover - defensive (older limit-overrun shape)
        return _OVERSIZED_LINE


# Sentinel a drained-pipe reader puts on its queue to mark clean EOF (distinct from
# b"" which a lane already reads as EOF, and from _OVERSIZED_LINE).
_EOF = object()


class _PipeLineReader:
    """A stall-proof line source over a child's stdout pipe.

    THE PROBLEM IT SOLVES (the autonomous-dispatch stall): the lanes below read
    the child one line at a time with ``await asyncio.wait_for(readline(...))``.
    A child's OS stdout pipe is a small fixed buffer (~64KB on macOS). ``readline``
    only drains that pipe WHILE it is being awaited — so if the consuming coroutine
    is not scheduled promptly (the orchestrator loop is busy elsewhere: a blocking
    psycopg2 settings read, a heavy reconcile sweep, another concurrent run), the
    pipe fills, the child BLOCKS on its next write right after a tool frame, and the
    per-turn timeout then aborts it ("did not respond within Ns — turn aborted").
    That is the "exactly 1 tool segment, then freeze, then timeout, no file written"
    symptom seen under the live loop but never on a direct/standalone run.

    THE FIX (structural + reusable): a dedicated background task does NOTHING but
    drain the pipe — ``_readline_resilient`` in a tight loop — pushing each line
    onto an in-process ``asyncio.Queue``. That decouples emptying the OS pipe from
    parsing/yielding: even a briefly-starved consumer can't backpressure the child,
    because the lightweight drain task is scheduled whenever the loop runs at all
    and keeps the kernel pipe empty (bytes buffer in-process, not in the 64KB pipe).
    ``readline(timeout)`` then awaits the QUEUE with the lane-selected timeout and
    returns the same values the lanes already handle (a ``bytes`` line, ``b""``/EOF,
    or ``_OVERSIZED_LINE``) — so each lane keeps its behaviour and event contract
    while the caller decides whether a short background cap applies.

    Lifecycle: ``start()`` spawns the drain task; ``readline(timeout)`` consumes;
    ``close()`` cancels + awaits the drain task (idempotent, never raises). Each
    lane creates one, ``start()``s it before the read loop, and ``close()``s it in
    a ``finally`` so cleanup is guaranteed on every exit path (EOF, timeout, error,
    cancellation)."""

    __slots__ = ("_stream", "_queue", "_task", "_eof")

    def __init__(self, stream: asyncio.StreamReader) -> None:
        self._stream = stream
        # Unbounded queue: the whole point is that the in-process buffer absorbs
        # bursts the 64KB OS pipe can't, so the child never blocks on write. Growth
        # is bounded in practice by the child's own output for one turn; the
        # 16MB-per-line ceiling caps any single frame.
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._eof = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        """Continuously move lines off the OS pipe onto the queue until EOF. This
        is the ONLY thing that reads the pipe, and it does the minimum work per
        line, so it stays schedulable even when the rest of the loop is busy."""
        try:
            while True:
                line = await _readline_resilient(self._stream)
                if line is _OVERSIZED_LINE:
                    await self._queue.put(_OVERSIZED_LINE)
                    continue
                if not line:  # b"" → real EOF
                    await self._queue.put(_EOF)
                    return
                await self._queue.put(line)
        except asyncio.CancelledError:  # close() during a pending read
            raise
        except Exception:  # pragma: no cover - defensive: surface EOF, never crash
            await self._queue.put(_EOF)

    async def readline(self, timeout: float | None) -> bytes | object:
        """Next line from the drained queue, or raise ``asyncio.TimeoutError`` if
        none arrives within a finite ``timeout``. ``None`` waits until output/EOF
        or caller cancellation. Returns ``b""`` at EOF (matching the lanes' existing
        EOF check), or ``_OVERSIZED_LINE`` for a skipped oversized frame — the same
        value space ``_readline_resilient`` returned, so callers are unchanged. Once
        EOF is seen we keep returning ``b""`` (idempotent)."""
        if self._eof:
            return b""
        if timeout is None:
            item = await self._queue.get()
        else:
            item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if item is _EOF:
            self._eof = True
            return b""
        return item

    async def close(self) -> None:
        """Cancel + await the drain task. Idempotent; never raises. Call in a
        ``finally`` so the background task never outlives the turn."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pragma: no cover - defensive
                pass


# ---------------------------------------------------------------------------
#  codex lane — real, best-effort (codex exec --json JSONL)
# ---------------------------------------------------------------------------

def _codex_reasoning_args(
    reasoning: str | None,
    model: str | None = None,
) -> list[str]:
    """Map a routed reasoning level to codex's reasoning-effort config override.

    codex has NO reasoning CLI flag (VERIFIED `codex --help` / `codex exec
    --help`, codex-cli 0.144.1); the documented lever is the ``-c key=value``
    config override. We emit ``-c model_reasoning_effort="<level>"`` (the value is
    quoted so codex parses it as a TOML string). The stored value is normalized
    via ``app.reasoning`` (so "med"/"on"/"off" aliases resolve), then:
      * OFF / empty / a bare "on" / an unrecognized token → NO override (codex
        uses its model default).
      * a model with a discovered effort ladder accepts only a member of that list;
      * without discovery, ``CODEX_EFFORT_LEVELS`` is the compatibility fallback.
    Returns a (possibly empty) argv fragment, never raises."""
    from app import reasoning as _reasoning

    lvl = _reasoning.normalize_level(reasoning)
    if not lvl or lvl == "_on_":
        return []
    model_value = (model or "").strip()
    supported: list[str] | None = [] if model_value else None
    if model_value:
        try:
            from app import harness as _harness

            option = next(
                (
                    row
                    for row in _harness.harness_model_options("codex")
                    if row.get("value") == model_value
                ),
                None,
            )
            if option is not None:
                supported = (
                    list(option.get("reasoning_levels") or [])
                    if "reasoning_levels" in option
                    else []
                )
        except Exception:
            supported = []
    if (supported is not None and lvl in supported) or (
        supported is None and lvl in CODEX_EFFORT_LEVELS
    ):
        return ["-c", f'model_reasoning_effort="{lvl}"']
    return []


def _codex_sandbox_mode(run_context: str | None = None) -> str:
    """Return the least-privileged Codex sandbox that can satisfy this run."""
    context = _normalise_run_context(run_context)
    return "workspace-write" if context in CODEX_WORKSPACE_WRITE_CONTEXTS else "read-only"


def _build_codex_command(
    prompt: str,
    model: str | None,
    reasoning: str | None = None,
    run_context: str | None = None,
) -> list[str]:
    """Assemble the codex argv with a context-appropriate project sandbox."""
    sandbox_mode = _codex_sandbox_mode(run_context)
    argv = [
        _codex_program(),
        *CODEX_BASE_FLAGS[:3],
        "-s",
        sandbox_mode,
        *CODEX_BASE_FLAGS[3:],
    ]
    if sandbox_mode == "workspace-write":
        # Codex's workspace-write default is offline. Autonomous workers need the
        # project-local Cortex API and their configured provider/API surfaces while
        # remaining filesystem-confined to the selected workspace.
        argv += ["-c", "sandbox_workspace_write.network_access=true"]
    m = (model or "").strip()
    argv += _codex_reasoning_args(reasoning, m)
    if m:
        argv += ["-m", m]
    argv.append(prompt)
    return argv


_GENERIC_CODEX_FAILURE = "codex did not complete the turn."


def _codex_error_message(frame: dict[str, Any]) -> str:
    """Extract a human-readable cause from a codex ``error`` / ``turn.failed``
    frame, unwrapping the nested + stringified shapes codex 0.13x emits.

    Observed shapes (codex-cli 0.136.0):
      * ``{"type":"error","message":"<str, often a JSON blob>"}``
      * ``{"type":"turn.failed","error":{"message":"<str, often a JSON blob>"}}``
    The carried ``message`` is frequently a JSON string like
    ``{"type":"error","status":400,"error":{"message":"..."}}`` — we drill into
    ``error.message`` so the feed shows the actual cause (e.g. the model-rejected
    or usage-limit text) instead of a generic 'did not complete the turn'."""

    def _drill(value: Any, depth: int = 0) -> str | None:
        """Pull the innermost ``message`` string from a value that may be a plain
        string, a dict with ``message``/``error``, or a JSON-encoded string."""
        if depth > 6:
            return None
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            # The string may itself be a JSON blob carrying a deeper message.
            if s[:1] in "{[":
                try:
                    parsed = json.loads(s)
                except ValueError:
                    return s
                inner = _drill(parsed, depth + 1)
                return inner or s
            return s
        if isinstance(value, dict):
            # Prefer a nested error object, then a flat message field.
            for key in ("error", "message", "detail", "reason"):
                if key in value:
                    inner = _drill(value.get(key), depth + 1)
                    if inner:
                        return inner
            return None
        return None

    # `error` (dict or string) takes precedence over a flat `message`.
    cause = _drill(frame.get("error")) or _drill(frame.get("message"))
    return cause or _GENERIC_CODEX_FAILURE


# codex error/turn.failed causes we map to friendly, actionable copy (shared
# heuristics with the claude lane via _classify_error's category words).
def _classify_codex_error(cause: str) -> tuple[str, str]:
    """Map a raw codex failure cause to (canonical_category, friendly_message)."""
    low = (cause or "").lower()
    if any(a in low for a in _AUTH_CATEGORIES) or "not logged in" in low or "401" in low:
        return "authentication_failed", (
            "codex is not logged in — run `codex login` in a terminal "
            "(it uses your ChatGPT subscription, no API key)."
        )
    if any(r in low for r in _RATELIMIT_CATEGORIES) or "usage limit" in low or "429" in low:
        return "rate_limit", (
            "codex hit a rate / usage limit on your ChatGPT subscription — "
            "wait a moment and try again."
        )
    if not cause or cause == _GENERIC_CODEX_FAILURE:
        return "codex_error", _GENERIC_CODEX_FAILURE
    return "codex_error", f"codex did not complete the turn: {cause}"


def _codex_command_text(item: dict[str, Any]) -> str:
    """A SHORT, single-line label for a codex ``command_execution`` item: the
    command itself, bounded. ``command`` is the shell string codex ran (we prefer
    it over ``parsed_cmd``); truncated to the tool-summary cap. Never echoes the
    (possibly large) ``aggregated_output``."""
    cmd = item.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return _summarize_tool_input(cmd.strip())
    # Fall back to the parsed argv list if `command` is absent.
    parsed = item.get("parsed_cmd")
    if parsed is not None:
        return _summarize_tool_input(parsed)
    return ""


def _codex_item_to_event(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map ONE codex ThreadItem (the ``item`` of an ``item.started``/``item.completed``
    frame) to a runner THINKING or TOOL event, or None for an item type we don't
    surface as a span.

    Item ``type`` discriminants (codex-cli 0.136.0 ``--json`` ThreadItem surface):
      * ``reasoning``          → ``thinking`` (the model's reasoning ``text``)
      * ``command_execution``  → ``tool`` (the shell command codex ran)
      * ``mcp_tool_call``      → ``tool`` (``server``/``tool`` name)
      * ``web_search``         → ``tool`` (the search ``query``)
    ``agent_message`` (the reply) is handled by the caller as output, not here.
    Bounded + total: any odd shape yields None or a truncated summary, never the
    raw blob."""
    itype = item.get("type")
    if itype == "reasoning":
        text = item.get("text")
        # Some builds nest reasoning under `summary`/`content`; accept those too.
        if not (isinstance(text, str) and text):
            text = item.get("summary") if isinstance(item.get("summary"), str) else None
        return {"type": "thinking", "text": text} if (isinstance(text, str) and text) else None
    if itype == "command_execution":
        return _tool_use_event("shell", _codex_command_text(item) or None)
    if itype == "mcp_tool_call":
        server = (str(item.get("server")).strip() if item.get("server") else "")
        tool = (str(item.get("tool")).strip() if item.get("tool") else "") or "tool"
        name = f"{server}.{tool}" if server else tool
        return _tool_use_event(name, None)
    if itype == "web_search":
        return _tool_use_event("web_search", item.get("query"))
    return None


def _parse_codex_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    """Map ONE codex JSONL frame to a runner event dict, or None to drop it.

    codex emits: ``thread.started`` (→ session), ``turn.started`` (dropped),
    ``item.started`` for a tool/command item (→ ``tool`` — surfaced at start so the
    operator sees the command/tool the instant it runs), ``item.completed`` with an
    ``agent_message`` item (→ reply text, streamed as a delta — codex sends the
    message as a single completed item, not token deltas) or a ``reasoning`` item
    (→ ``thinking``, the complete reasoning text), and ``turn.completed`` with a
    ``usage`` block (→ result token counts). An ``error`` / ``turn.failed`` frame
    surfaces as a clear error event with the real cause unwrapped from codex's
    nested/stringified error payload.

    Tool vs reasoning timing: TOOL/command items surface on ``item.started`` (the
    command is known immediately; its output streams later) and are NOT re-emitted
    on ``item.completed``; REASONING + the agent_message reply surface on
    ``item.completed`` (we need the finished text). This split means each span is
    emitted exactly once."""
    ftype = frame.get("type")

    if ftype == "thread.started":
        return {"type": "session", "session_id": frame.get("thread_id"), "model": None}

    if ftype == "item.started":
        # Surface TOOL/command spans the instant they begin (reasoning + the reply
        # wait for item.completed, so we don't double-emit them here).
        item = frame.get("item")
        if isinstance(item, dict) and item.get("type") in (
            "command_execution", "mcp_tool_call", "web_search",
        ):
            return _codex_item_to_event(item)
        return None

    if ftype == "item.completed":
        item = frame.get("item")
        if not isinstance(item, dict):
            return None
        itype = item.get("type")
        if itype == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                # Surface as a delta so the feed streams it like any other reply
                # (codex has no partial deltas; this is the whole message at once).
                return {"type": "delta", "text": text}
            return None
        if itype == "reasoning":
            # The complete reasoning text → a thinking span.
            return _codex_item_to_event(item)
        # command_execution / mcp_tool_call / web_search already surfaced at
        # item.started — don't re-emit them on completion.
        return None

    if ftype == "turn.completed":
        usage = frame.get("usage")
        tokens_in = tokens_out = None
        if isinstance(usage, dict):
            def _g(*keys: str) -> int:
                tot = 0
                for k in keys:
                    v = usage.get(k)
                    if isinstance(v, int):
                        tot += v
                return tot
            ti = _g("input_tokens", "cached_input_tokens")
            to = _g("output_tokens", "reasoning_output_tokens")
            tokens_in = ti or None
            tokens_out = to or None
        return {
            "type": "result",
            "text": "",  # the text already streamed via item.completed
            "cost_usd": None,  # codex exec --json carries no per-turn USD cost
            "session_id": None,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }

    if ftype in ("error", "turn.failed"):
        cause = _codex_error_message(frame)
        category, message = _classify_codex_error(cause)
        return {"type": "error", "category": category, "message": message}

    return None


async def _stream_codex(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    reasoning: str | None = None,
    workspace: str | None = None,
    project_key: str | None = None,
    run_context: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Spawn ``codex exec --json`` on the ChatGPT subscription and yield reply
    events (REAL best-effort path).

    Same event contract + robustness as the claude lane: missing binary degrades
    to the graceful 'runner not wired / install codex' message (NOT a crash),
    per-turn timeout kills + surfaces a timeout error, malformed JSONL lines are
    skipped, and a non-zero exit with no parsed reply surfaces a clear error.
    stdin is closed (DEVNULL) so ``codex exec`` never blocks reading piped input.
    Provider keys are stripped from the child env (subscription auth)."""
    argv = _build_codex_command(
        _compose_prompt(prompt, system),
        model,
        reasoning=reasoning,
        run_context=run_context,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,  # don't let `codex exec` wait on stdin
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_apply_project_workspace(_child_env(), project_key, workspace),
            cwd=workspace or None,
            # Raise the StreamReader buffer ceiling above 64KB. A real codex task
            # can emit a SINGLE `--json` event line well over 64KB (a long
            # agent_message), which previously raised asyncio's "Separator is not
            # found, and chunk exceed the limit" and killed the whole run.
            limit=STREAM_BUFFER_LIMIT,
        )
    except FileNotFoundError:
        # codex isn't installed — degrade to the same clear, non-crashing message
        # as the unwired harnesses (chat must never crash on a missing binary).
        async for ev in _stream_graceful(
            "codex", model,
            reason="the codex CLI was not found on PATH "
                   "(install it + run `codex login`)",
        ):
            yield ev
        return
    except OSError as exc:  # pragma: no cover - exec-layer failure
        yield {"type": "error", "category": "spawn_failed", "message": f"Failed to start codex: {exc}"}
        yield {"type": "done"}
        return

    saw_error = False
    saw_text = False
    # Stall-proof stdout drain (see _PipeLineReader) — a busy/blocked loop can't
    # backpressure codex's OS pipe. Closed in `finally`.
    assert proc.stdout is not None
    reader = _PipeLineReader(proc.stdout)
    reader.start()
    turn_timeout = _turn_read_timeout(run_context)
    try:
        while True:
            try:
                line = await reader.readline(turn_timeout)
            except asyncio.TimeoutError:
                _kill(proc)
                yield {
                    "type": "error",
                    "category": "timeout",
                    "message": f"codex did not respond within {int(TURN_TIMEOUT_S)}s — turn aborted.",
                }
                saw_error = True
                break

            if line is _OVERSIZED_LINE:
                # One codex frame overran even the 16MB buffer — skip it (logged)
                # and keep streaming the rest of the turn instead of crashing.
                print(
                    "[harness_runner] codex emitted a stdout line over "
                    f"{STREAM_BUFFER_LIMIT} bytes — skipping that frame.",
                    flush=True,
                )
                continue

            assert isinstance(line, bytes)
            if not line:  # EOF
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue  # non-JSON noise (e.g. a stray log line)
            if not isinstance(frame, dict):
                continue
            event = _parse_codex_frame(frame)
            if event is None:
                continue
            if event["type"] == "delta":
                saw_text = True
            elif event["type"] == "result" and event.get("text"):
                saw_text = True
            elif event["type"] == "error":
                saw_error = True
            yield event

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            _kill(proc)

        stderr_text = ""
        if proc.stderr is not None:
            try:
                stderr_text = (await proc.stderr.read()).decode("utf-8", "replace")
            except (OSError, ValueError):  # pragma: no cover - defensive
                stderr_text = ""

        if not saw_error:
            rc = proc.returncode
            if _looks_like_auth_failure(stderr_text, rc):
                yield {
                    "type": "error",
                    "category": "authentication_failed",
                    "message": "codex is not logged in — run `codex login` in a terminal "
                               "(it uses your ChatGPT subscription, no API key).",
                }
                saw_error = True
            elif rc not in (0, None) and not saw_text:
                tail = " ".join(stderr_text.split())[-300:]
                yield {
                    "type": "error",
                    "category": "nonzero_exit",
                    "message": (
                        f"codex exited with code {rc}" + (f": {tail}" if tail else " (no output).")
                    ),
                }
                saw_error = True
    finally:
        await reader.close()
        if proc.returncode is None:
            _kill(proc)

    yield {"type": "done"}


# ---------------------------------------------------------------------------
#  pi lane — real, best-effort (pi --mode json JSONL)
# ---------------------------------------------------------------------------

def _pi_thinking_level(model: str | None, thinking: str | None) -> str:
    """Return a PI-safe effort for the selected model, or ``""`` to omit it.

    PI advertises its global effort tokens in ``--help`` and marks each model as a
    reasoner in ``--list-models``. The catalog bridge combines those facts into a
    per-model ladder. Explicit ``off`` remains meaningful and is forwarded; unknown
    or unsupported values fall back to PI's model default instead of failing a turn.
    """
    raw = (thinking or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low in {"off", "none", "no", "false", "disabled", "disable"}:
        return "off"

    from app import reasoning as _reasoning

    level = _reasoning.normalize_level(raw)
    if not level or level == "_on_":
        return ""

    supported: list[str] | None = None
    model_value = (model or PI_DEFAULT_MODEL).strip()
    try:
        from app import pi_catalog

        for group in pi_catalog.cached_pi_model_groups():
            for row in group.get("rows", []):
                if row.get("id") == model_value:
                    if "reasoning_levels" in row:
                        supported = list(row.get("reasoning_levels") or [])
                    break
            if supported is not None:
                break
    except Exception:
        supported = None

    if supported is None:
        try:
            from app import harness as _harness

            supported = list(_harness.HARNESS_REASONING.get("pi", []))
        except Exception:
            supported = ["off", "minimal", "low", "medium", "high", "xhigh"]
    return level if level in supported else ""


def _build_pi_command(
    prompt: str,
    model: str | None,
    system: str | None = None,
    thinking: str | None = None,
) -> list[str]:
    """Assemble the pi argv (list, for exec — never a shell string).

    Real path (pi 0.80.3, VERIFIED live 2026-07-10):
        ``pi [--provider openai-codex] --model <model or provider/model> --mode json -p
          --no-session --no-tools --no-context-files
          [--thinking <level>] [--system-prompt <system>] <prompt>``

    Bare model ids (no '/' prefix) keep the historical OpenAI-Codex provider flag.
    Provider-prefixed ids (``fireworks/...``, ``ollama-cloud/...``) are passed
    directly via ``--model`` and do NOT force ``--provider openai-codex``.
    When model is None/blank, the DEFAULT model (PI_DEFAULT_MODEL) is used."""
    argv = [_pi_program()]
    m = (model or PI_DEFAULT_MODEL).strip()
    if not m or "/" not in m:
        argv += ["--provider", PI_PROVIDER]
    argv += [*PI_BASE_FLAGS]
    if m:
        argv += ["--model", m]
    th = _pi_thinking_level(m, thinking)
    if th:
        argv += ["--thinking", th]
    sysmsg = (system or "").strip()
    if sysmsg:
        argv += ["--system-prompt", sysmsg]
    argv.append(prompt)
    return argv


_GENERIC_PI_FAILURE = "pi did not complete the turn."


def _pi_error_message(value: Any, depth: int = 0) -> str | None:
    """Drill the innermost human-readable message out of a pi error payload.

    pi's ``errorMessage`` / ``error`` fields are usually a plain string, but may be
    a dict or a JSON-encoded string carrying a deeper ``message`` (mirrors the
    codex error-unwrapping). Best-effort + total: any odd shape yields None so a
    malformed error frame never crashes the run. We only ever stringify what pi
    already put in the frame — never expand or resolve anything."""
    if depth > 6:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s[:1] in "{[":
            try:
                parsed = json.loads(s)
            except ValueError:
                return s
            inner = _pi_error_message(parsed, depth + 1)
            return inner or s
        return s
    if isinstance(value, dict):
        for key in ("errorMessage", "message", "error", "detail", "reason"):
            if key in value:
                inner = _pi_error_message(value.get(key), depth + 1)
                if inner:
                    return inner
        return None
    return None


def _classify_pi_error(cause: str) -> tuple[str, str]:
    """Map a raw pi failure cause to (canonical_category, friendly_message).

    Shares the auth / rate-limit category words with the claude/codex lanes; pi's
    openai-codex auth lives in ``~/.pi/agent/auth.json`` (refresh via `pi` /login),
    so the not-logged-in copy points there."""
    low = (cause or "").lower()
    if any(a in low for a in _AUTH_CATEGORIES) or "not logged in" in low or "401" in low:
        return "authentication_failed", (
            "pi is not logged in to the OpenAI Codex subscription — run `pi` and "
            "use `/login` (it uses your ChatGPT/Codex subscription, no API key)."
        )
    if any(r in low for r in _RATELIMIT_CATEGORIES) or "usage limit" in low or "429" in low:
        return "rate_limit", (
            "pi hit a rate / usage limit on your ChatGPT/Codex subscription — "
            "wait a moment and try again."
        )
    if not cause or cause == _GENERIC_PI_FAILURE:
        return "pi_error", _GENERIC_PI_FAILURE
    return "pi_error", f"pi did not complete the turn: {cause}"


def _pi_usage_tokens(usage: Any) -> tuple[int | None, int | None, float | None]:
    """Fold a pi ``usage`` block into (tokens_in, tokens_out, cost_usd).

    pi usage shape (VERIFIED live): ``{input, output, cacheRead, cacheWrite,
    totalTokens, cost:{input, output, cacheRead, cacheWrite, total}}``. We fold
    ``input + cacheRead`` into the input total (cacheRead bytes ARE input, billed
    at a cache rate — mirrors the claude/codex cache folding) and surface
    ``cost.total`` as the per-turn USD cost. Tolerant of a missing/partial block
    (→ (None, None, None))."""
    if not isinstance(usage, dict):
        return None, None, None

    def _i(*keys: str) -> int:
        tot = 0
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                tot += int(v)
        return tot

    ti = _i("input", "cacheRead")
    to = _i("output")
    tokens_in = ti or None
    tokens_out = to or None

    cost_usd: float | None = None
    cost = usage.get("cost")
    if isinstance(cost, dict):
        total = cost.get("total")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            cost_usd = float(total)
    return tokens_in, tokens_out, cost_usd


def _parse_pi_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    """Map ONE pi JSONL frame to a runner event dict, or None to drop it.

    pi 0.80.3 ``--mode json`` event stream (shapes VERIFIED against a live turn
    2026-06-02):
      * ``session`` header ``{type:"session", id, ...}`` → ``session`` (the model
        is NOT on this frame — it arrives on the assistant ``message_*`` frames —
        so we surface session_id and leave model None, like the codex thread.started).
      * ``message_update`` with ``assistantMessageEvent.type == "text_delta"``
        (field ``delta``) → ``delta`` (the streamed reply text).
      * ``message_update`` with a reasoning/thinking delta in
        ``assistantMessageEvent`` (``thinking_delta`` / a ``thinking`` field) →
        ``thinking`` (mirrors codex ``reasoning`` → thinking). NOTE: the live spark
        turn emitted encrypted/empty reasoning (``thinking_start``/``thinking_end``
        with empty text), so we only surface NON-empty reasoning text.
      * ``message_update`` with ``assistantMessageEvent.type == "error"`` →
        ``error`` (pi_error, the carried message).
      * ``message_end`` / ``turn_end`` with ``stopReason == "error"`` →
        ``error`` (the ``errorMessage``).
      * ``turn_end`` (success) with ``message.usage`` → ``result`` (empty text —
        the reply already streamed via text_delta — plus token counts + cost).
      * ``agent_end`` → only used as a fallback result if no ``turn_end`` was seen
        (handled in ``_stream_pi``, not here, so we don't double-emit a result).
    Everything else (agent_start, turn_start, message_start, thinking_start/end,
    text_start/end, the user ``message_*`` echoes) is dropped — we never surface
    raw pi JSON to the feed. Defensive: a malformed/unknown frame → None."""
    ftype = frame.get("type")

    if ftype == "session":
        return {"type": "session", "session_id": frame.get("id"), "model": frame.get("model")}

    if ftype == "message_update":
        ame = frame.get("assistantMessageEvent")
        if not isinstance(ame, dict):
            return None
        etype = ame.get("type")
        if etype == "text_delta":
            delta = ame.get("delta")
            if isinstance(delta, str) and delta:
                return {"type": "delta", "text": delta}
            return None
        if etype in ("thinking_delta", "reasoning_delta"):
            # A live reasoning delta — surface as thinking (mirror codex reasoning).
            txt = ame.get("delta")
            if not (isinstance(txt, str) and txt):
                txt = ame.get("thinking") if isinstance(ame.get("thinking"), str) else None
            return {"type": "thinking", "text": txt} if (isinstance(txt, str) and txt) else None
        if etype == "thinking_end":
            # The completed reasoning text (only surfaced when non-empty — the live
            # spark turn carried encrypted/empty reasoning, which we drop).
            txt = ame.get("content")
            return {"type": "thinking", "text": txt} if (isinstance(txt, str) and txt.strip()) else None
        if etype == "error":
            cause = _pi_error_message(ame) or _GENERIC_PI_FAILURE
            category, message = _classify_pi_error(cause)
            return {"type": "error", "category": category, "message": message}
        return None

    if ftype in ("message_end", "turn_end"):
        msg = frame.get("message")
        msg = msg if isinstance(msg, dict) else {}
        # An errored turn → surface the error (don't emit a result for it).
        if msg.get("stopReason") == "error":
            cause = _pi_error_message(msg) or _pi_error_message(frame) or _GENERIC_PI_FAILURE
            category, message = _classify_pi_error(cause)
            return {"type": "error", "category": category, "message": message}
        if ftype == "turn_end":
            # Success terminal frame → final result with usage. The reply text
            # already streamed via text_delta, so text is "".
            tokens_in, tokens_out, cost_usd = _pi_usage_tokens(msg.get("usage"))
            return {
                "type": "result",
                "text": "",
                "cost_usd": cost_usd,
                "session_id": None,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            }
        # message_end (non-error) is not the turn terminal — turn_end carries the
        # authoritative usage; drop it to avoid a duplicate result.
        return None

    if ftype == "tool_execution_start":
        # Surface the tool call the instant it STARTS (real live shape:
        # {type, toolCallId, toolName, args}) — toolName + a bounded arg summary —
        # so the operator SEES read/bash/edit/write live as the agent works
        # (mirrors codex item.started → tool). The matching tool_execution_end
        # (the result) is dropped to avoid a duplicate span.
        return _tool_use_event(frame.get("toolName"), frame.get("args"))

    # agent_start / turn_start / message_start / agent_end / thinking_* / text_* /
    # tool_execution_end and any unknown frame: not surfaced here.
    return None


async def _stream_pi(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    thinking: str | None = None,
    workspace: str | None = None,
    project_key: str | None = None,
    run_context: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Spawn ``pi --mode json`` and yield reply events (REAL best-effort path).

    Mirrors the codex lane: same event contract + robustness. Missing binary
    degrades to the graceful 'install pi / not wired' message (NOT a crash),
    per-turn timeout kills + surfaces a timeout error, malformed JSONL lines are
    skipped, and a non-zero exit with no parsed reply surfaces a clear error.
    stdin is closed (DEVNULL) so pi never blocks reading piped input. The child
    env (``_pi_child_env``) strips metered API keys for bare OpenAI-Codex models
    and preserves provider keys for provider-prefixed models — we never read or log
    auth.json/tokens or the raw stderr beyond a short tail in a nonzero-exit error."""
    argv = _build_pi_command(_compose_prompt(prompt, system), model, thinking=thinking)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,  # don't let pi wait on stdin
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_apply_project_workspace(_pi_child_env(model), project_key, workspace),
            cwd=workspace or None,
            # Raise the StreamReader buffer ceiling above 64KB. A pi `--mode json`
            # event line is huge (each message_update echoes the full partial
            # message + an encrypted reasoning blob), well over 64KB, which would
            # otherwise trip asyncio's "Separator is not found, and chunk exceed
            # the limit" and kill the run.
            limit=STREAM_BUFFER_LIMIT,
        )
    except FileNotFoundError:
        # pi isn't installed — degrade to the same clear, non-crashing message as
        # the unwired harnesses (chat must never crash on a missing binary).
        async for ev in _stream_graceful(
            "pi", model,
            reason="the pi CLI was not found on PATH "
                   "(install pi + log in to the OpenAI Codex subscription)",
        ):
            yield ev
        return
    except OSError as exc:  # pragma: no cover - exec-layer failure
        yield {"type": "error", "category": "spawn_failed", "message": f"Failed to start pi: {exc}"}
        yield {"type": "done"}
        return

    saw_error = False
    saw_text = False
    saw_result = False
    # Drain stdout via a dedicated background reader so a busy/blocked event loop
    # can never backpressure pi's OS pipe (the autonomous-dispatch stall — see
    # _PipeLineReader). Started here, closed in `finally`. The per-line turn cap +
    # oversized handling are unchanged — only the line SOURCE is now stall-proof.
    assert proc.stdout is not None
    reader = _PipeLineReader(proc.stdout)
    reader.start()
    turn_timeout = _turn_read_timeout(run_context)
    try:
        while True:
            try:
                read_timeout = (
                    PI_IDLE_AFTER_TEXT_TIMEOUT_S
                    if saw_text and not saw_result
                    else turn_timeout
                )
                line = await reader.readline(read_timeout)
            except asyncio.TimeoutError:
                _kill(proc)
                if saw_text and not saw_error:
                    saw_result = True
                    yield {
                        "type": "result",
                        "text": "",
                        "cost_usd": None,
                        "session_id": None,
                        "tokens_in": None,
                        "tokens_out": None,
                    }
                else:
                    yield {
                        "type": "error",
                        "category": "timeout",
                        "message": f"pi did not respond within {int(TURN_TIMEOUT_S)}s — turn aborted.",
                    }
                    saw_error = True
                break

            if line is _OVERSIZED_LINE:
                # One pi frame overran even the 16MB buffer — skip it (logged) and
                # keep streaming the rest of the turn instead of crashing.
                print(
                    "[harness_runner] pi emitted a stdout line over "
                    f"{STREAM_BUFFER_LIMIT} bytes — skipping that frame.",
                    flush=True,
                )
                continue

            assert isinstance(line, bytes)
            if not line:  # EOF
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue  # non-JSON noise (e.g. a stray log line)
            if not isinstance(frame, dict):
                continue
            event = _parse_pi_frame(frame)
            if event is None:
                continue
            terminal_event = False
            if event["type"] == "delta":
                saw_text = True
            elif event["type"] == "result":
                saw_result = True
                terminal_event = True
                if event.get("text"):
                    saw_text = True
            elif event["type"] == "error":
                saw_error = True
                terminal_event = True
            yield event
            if terminal_event:
                break

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            _kill(proc)

        stderr_text = ""
        if proc.stderr is not None:
            try:
                stderr_text = (await proc.stderr.read()).decode("utf-8", "replace")
            except (OSError, ValueError):  # pragma: no cover - defensive
                stderr_text = ""

        if not saw_error:
            rc = proc.returncode
            if _looks_like_auth_failure(stderr_text, rc):
                yield {
                    "type": "error",
                    "category": "authentication_failed",
                    "message": "pi is not logged in to the OpenAI Codex subscription — run `pi` "
                               "and use `/login` (it uses your ChatGPT/Codex subscription, no API key).",
                }
                saw_error = True
            elif rc not in (0, None) and not saw_text:
                tail = " ".join(stderr_text.split())[-300:]
                yield {
                    "type": "error",
                    "category": "nonzero_exit",
                    "message": (
                        f"pi exited with code {rc}" + (f": {tail}" if tail else " (no output).")
                    ),
                }
                saw_error = True
    finally:
        await reader.close()
        if proc.returncode is None:
            _kill(proc)

    yield {"type": "done"}


# ---------------------------------------------------------------------------
#  kaidera / kaidera lane — provider API keys configured in Settings
# ---------------------------------------------------------------------------

OWN_HARNESS_DEFAULT_MODEL = os.environ.get(
    "HARNESS_OWN_DEFAULT_MODEL", "kaidera-manifold/ollama-cloud/minimax-m3"
)
OWN_HARNESS_TIMEOUT_S = float(os.environ.get("HARNESS_OWN_TIMEOUT_S", "120"))
OWN_HARNESS_MAX_TOKENS = int(os.environ.get("HARNESS_OWN_MAX_TOKENS", "4096"))

# Kaidera AI Manifold endpoint — the platform's hosted OpenAI-compatible inference gateway.
# Env/config-overridable per deployment; the license customer surface mints the bearer
# key and server-side metering/wallet settlement stays on the platform.
MANIFOLD_BASE_URL = platform_config.manifold_base_url()

_OWN_OPENAI_COMPAT_CHAT: dict[str, tuple[str, str]] = {
    "kaidera-manifold": ("kaidera_manifold_api_key", ""),
    "openai": ("openai_api_key", "https://api.openai.com/v1/chat/completions"),
    "openrouter": ("openrouter_api_key", "https://openrouter.ai/api/v1/chat/completions"),
    "fireworks": ("fireworks_api_key", "https://api.fireworks.ai/inference/v1/chat/completions"),
    "groq": ("groq_api_key", "https://api.groq.com/openai/v1/chat/completions"),
    "siliconflow": ("siliconflow_api_key", "https://api.siliconflow.com/v1/chat/completions"),
    "deepseek": ("deepseek_api_key", "https://api.deepseek.com/v1/chat/completions"),
    "together": ("together_api_key", "https://api.together.xyz/v1/chat/completions"),
    "moonshot": ("moonshot_api_key", "https://api.moonshot.ai/v1/chat/completions"),
    "xai": ("xai_api_key", "https://api.x.ai/v1/chat/completions"),
    "ollama-cloud": ("ollama_cloud_api_key", "https://ollama.com/v1/chat/completions"),
    "dashscope": ("dashscope_api_key", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
    "inception": ("inception_api_key", "https://api.inceptionlabs.ai/v1/chat/completions"),
    "alibaba-cloud": ("alibaba_cloud_api_key", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"),
}


class _OwnHarnessError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


def _chat_url_from_base(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _manifold_base_url(cfg: dict[str, Any]) -> str:
    return platform_config.manifold_base_url(
        str(cfg.get("kaidera_manifold_base_url") or "")
    )


def _manifold_project_id(cfg: dict[str, Any]) -> str:
    """The `X-Project-Id` value — the platform project that scopes + bills the call.
    Manifold's /v1 edge returns 400 missing_project_id without it, so it is required."""
    return str(
        cfg.get("kaidera_manifold_project_id")
        or os.environ.get("KAIDERA_MANIFOLD_PROJECT_ID")
        or ""
    ).strip()


def _own_runtime_config() -> tuple[dict[str, Any], dict[str, dict[str, str]], Any]:
    """Load provider keys/custom providers through the existing Settings store."""
    from app import providers as providers_catalog
    from app import settings as settings_store

    # load_with_secrets(), NOT load(): provider keys live outside the System schema,
    # so a bare load() drops every provider API key and the kaidera call would
    # authenticate with an empty key. _resolve_provider_key reads cfg.get(setting_key).
    cfg = settings_store.load_with_secrets()
    customs = {
        f"custom:{c['id']}": c
        for c in settings_store.load_custom_providers()
        if c.get("id")
    }
    return cfg, customs, providers_catalog._resolve_provider_key


def _own_provider_key(provider: str, cfg: dict[str, Any], customs: dict[str, dict[str, str]], resolver: Any) -> str:
    if provider.startswith("custom:"):
        return (customs.get(provider, {}).get("api_key") or "").strip()
    if provider == "kaidera-manifold":
        try:
            from app import edition
            from app import license as lic_mod
            if not edition.is_dev() and not lic_mod.entitlements().has_advanced("manifold_access"):
                return ""
        except Exception:
            return ""
    if provider == "codex-subscription":
        # PRESENCE sentinel only — the real OAuth bearer is fetched ASYNC in
        # _kaidera_complete (refresh is an async HTTP call; this resolver is sync).
        from . import codex_oauth
        return "codex-oauth" if codex_oauth.is_logged_in() else ""
    if provider == "anthropic":
        return resolver(cfg, "anthropic_api_key")
    meta = _OWN_OPENAI_COMPAT_CHAT.get(provider)
    if not meta:
        return ""
    setting_key, _url = meta
    return resolver(cfg, setting_key)


def _infer_own_provider(model: str) -> str:
    low = (model or "").lower()
    if low.startswith("claude"):
        return "anthropic"
    if low.startswith("accounts/fireworks/"):
        return "fireworks"
    if low.startswith(("gpt", "o1", "o3", "o4", "openai")):
        return "openai"
    return "openrouter"


def _own_target(
    model: str | None,
    cfg: dict[str, Any],
    customs: dict[str, dict[str, str]],
    resolver: Any,
) -> tuple[str, str, str]:
    """Return (provider, native_model, api_key), falling back to OpenRouter when useful."""
    raw = str(model or "").strip() or OWN_HARNESS_DEFAULT_MODEL
    provider = ""
    native = raw

    if raw.startswith("custom:") and "/" in raw:
        provider, native = raw.split("/", 1)
    elif "/" in raw:
        prefix, rest = raw.split("/", 1)
        if (prefix in _OWN_OPENAI_COMPAT_CHAT or prefix == "anthropic"
                or prefix == "codex-subscription" or prefix in customs):
            provider, native = prefix, rest
        else:
            provider, native = "openrouter", raw
    else:
        provider, native = _infer_own_provider(raw), raw

    key = _own_provider_key(provider, cfg, customs, resolver)
    if key:
        return provider, native, key

    # If the selected provider key is missing but OpenRouter is configured, run the
    # original model slug through OpenRouter instead of failing immediately. This
    # preserves older saved values such as "anthropic/claude-..." from before
    # provider source namespacing was added.
    or_key = _own_provider_key("openrouter", cfg, customs, resolver)
    if provider != "openrouter" and or_key:
        return "openrouter", raw, or_key
    return provider, native, key


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                txt = item.get("text") or item.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _openai_compat_payload(provider: str, model: str, prompt: str, system: str | None, thinking: str | None) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    if (system or "").strip():
        messages.append({"role": "system", "content": (system or "").strip()})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if provider == "openai":
        payload["max_completion_tokens"] = OWN_HARNESS_MAX_TOKENS
    else:
        payload["max_tokens"] = OWN_HARNESS_MAX_TOKENS
    # Reasoning/thinking — delegate to the connector-registry standard core
    # (app.reasoning). The core writes the provider's NATIVE param ONLY when the
    # resolved (provider, model) actually reasons AND the level is valid for THAT
    # model (per-model clamp/skip from the registry); otherwise it leaves the body
    # untouched (a correct thinking-off call). This is the live kaidera call path:
    # we never send a param a model rejects (grok-4 400, base kimi-k2 isn't a
    # reasoner, ollama "minimal" 400), so the OLD low/medium/high gate is gone.
    from app import providers as _providers
    from app import reasoning as _reasoning

    _reasoning.apply_reasoning(
        provider,
        model,
        thinking,
        payload,
        available_levels=_providers.cached_reasoning_levels(provider, model),
    )
    return payload


def _parse_openai_compat_result(data: dict[str, Any]) -> tuple[str, int | None, int | None]:
    choices = data.get("choices") if isinstance(data, dict) else None
    text = ""
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message")
            if isinstance(msg, dict):
                text = _message_content_text(msg.get("content"))
            if not text:
                text = _message_content_text(first.get("text"))
    usage = data.get("usage") if isinstance(data, dict) else None
    tokens_in = tokens_out = None
    if isinstance(usage, dict):
        pin = usage.get("prompt_tokens") or usage.get("input_tokens")
        pout = usage.get("completion_tokens") or usage.get("output_tokens")
        tokens_in = int(pin) if isinstance(pin, int) else None
        tokens_out = int(pout) if isinstance(pout, int) else None
    return text, tokens_in, tokens_out


def _parse_anthropic_result(data: dict[str, Any]) -> tuple[str, int | None, int | None]:
    parts: list[str] = []
    for item in data.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            txt = item.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    usage = data.get("usage") if isinstance(data, dict) else None
    tokens_in = tokens_out = None
    if isinstance(usage, dict):
        pin = usage.get("input_tokens")
        pout = usage.get("output_tokens")
        tokens_in = int(pin) if isinstance(pin, int) else None
        tokens_out = int(pout) if isinstance(pout, int) else None
    return "".join(parts), tokens_in, tokens_out


def _codex_responses_payload(model: str, prompt: str, system: str | None) -> dict[str, Any]:
    """LIVE-UNVERIFIED. The ChatGPT backend Responses-API body for a codex-subscription
    turn. Mirrors openai/codex `ResponsesApiRequest` (an `input[]` array, NOT Chat-Completions
    `messages[]`); the exact field set must be confirmed against a live codex token."""
    items: list[dict[str, Any]] = []
    if (system or "").strip():
        items.append({"type": "message", "role": "system",
                      "content": [{"type": "input_text", "text": (system or "").strip()}]})
    items.append({"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": prompt}]})
    return {"model": model, "input": items, "stream": False, "store": False}


def _parse_responses_result(data: dict[str, Any]) -> tuple[str, int | None, int | None]:
    """LIVE-UNVERIFIED. Pull assistant text + token usage from a Responses-API result."""
    parts: list[str] = []
    for item in (data.get("output") if isinstance(data, dict) else None) or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    t = c.get("text")
                    if isinstance(t, str):
                        parts.append(t)
    usage = data.get("usage") if isinstance(data, dict) else None
    tokens_in = tokens_out = None
    if isinstance(usage, dict):
        pin = usage.get("input_tokens")
        pout = usage.get("output_tokens")
        tokens_in = int(pin) if isinstance(pin, int) else None
        tokens_out = int(pout) if isinstance(pout, int) else None
    return "".join(parts), tokens_in, tokens_out


async def _kaidera_complete(
    prompt: str,
    model: str | None,
    system: str | None,
    thinking: str | None,
    workspace: str | None = None,
) -> dict[str, Any]:
    cfg, customs, resolver = _own_runtime_config()
    provider, native_model, api_key = _own_target(model, cfg, customs, resolver)
    if not api_key:
        raise _OwnHarnessError(
            "provider_not_configured",
            f"Kaidera AI harness cannot run {native_model}: no {provider} provider key is configured.",
        )

    timeout = httpx.Timeout(OWN_HARNESS_TIMEOUT_S, connect=10.0)
    headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"}
    payload: dict[str, Any]
    parser: Any

    if provider == "codex-subscription":
        # LIVE-UNVERIFIED codex-subscription lane: the OAuth bearer (NOT a metered key)
        # only works against the ChatGPT backend Responses API. Fetch the real bearer
        # async here (the sync resolver returned a presence sentinel). See
        # docs/2026-06-13-codex-oauth-design.md.
        from . import codex_oauth
        bearer = await codex_oauth.get_codex_oauth_bearer(cfg)
        if not bearer:
            raise _OwnHarnessError(
                "authentication_failed",
                "Codex subscription not logged in — open Settings → Providers and 'Log in with ChatGPT'.",
            )
        headers = {
            "Authorization": f"Bearer {bearer}",
            "chatgpt-account-id": codex_oauth.account_id(),
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "Content-Type": "application/json",
        }
        payload = _codex_responses_payload(native_model, prompt, system)
        url = "https://chatgpt.com/backend-api/codex/responses"
        parser = _parse_responses_result
    elif provider == "anthropic":
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": native_model,
            "max_tokens": OWN_HARNESS_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if (system or "").strip():
            payload["system"] = (system or "").strip()
        # EXTENDED THINKING (the previously-missing Anthropic-direct path): merge
        # the adaptive thinking block + top-level reasoning_effort from the
        # connector core, but ONLY when a real level resolves for this model (an
        # OFF/empty/unknown value adds nothing — the correct quiet default). Opus
        # 4.7+ rejects the legacy budget_tokens, so we never send it.
        from app import providers as _providers
        from app import reasoning as _reasoning

        payload.update(
            _reasoning.anthropic_thinking_fields(
                native_model,
                thinking,
                available_levels=_providers.cached_reasoning_levels(
                    provider, native_model
                ),
            )
        )
        url = "https://api.anthropic.com/v1/messages"
        parser = _parse_anthropic_result
    else:
        if provider.startswith("custom:"):
            custom = customs.get(provider) or {}
            url = _chat_url_from_base(custom.get("base_url", ""))
            if not url:
                raise _OwnHarnessError(
                    "provider_not_configured",
                    f"Kaidera AI custom provider {provider[7:]} has no base URL configured.",
                )
        else:
            meta = _OWN_OPENAI_COMPAT_CHAT.get(provider)
            if not meta:
                raise _OwnHarnessError(
                    "provider_not_supported",
                    f"Kaidera AI harness does not have a chat endpoint wired for provider {provider}.",
                )
            _setting_key, url = meta
            if provider == "kaidera-manifold":
                url = _chat_url_from_base(_manifold_base_url(cfg))
                project_id = _manifold_project_id(cfg)
                if not project_id:
                    raise _OwnHarnessError(
                        "provider_not_configured",
                        "Kaidera AI Manifold requires a project id — set it in Settings -> "
                        "Providers. It is sent as the required X-Project-Id header; the "
                        "/v1 edge returns 400 missing_project_id without it.",
                    )
                # OpenAI-compatible edge, but the org/project scope + server-side wallet
                # metering key off this header (do NOT self-report usage).
                headers["X-Project-Id"] = project_id
        payload = _openai_compat_payload(provider, native_model, prompt, system, thinking)
        parser = _parse_openai_compat_result

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        category = "authentication_failed" if code in (401, 403) else "provider_error"
        raise _OwnHarnessError(category, f"Kaidera AI provider {provider} returned HTTP {code}.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise _OwnHarnessError("provider_error", f"Kaidera AI provider {provider} did not return a usable response.") from exc

    text, tokens_in, tokens_out = parser(data if isinstance(data, dict) else {})
    if not text:
        raise _OwnHarnessError("provider_error", f"Kaidera AI provider {provider} returned an empty response.")
    # B4: surface the model's reasoning/thinking text when the provider returned it
    # in its own field (message.reasoning_content / message.reasoning for the
    # OpenAI-compat lanes; Anthropic 'thinking' content blocks for the direct lane).
    reasoning_text = _extract_reasoning(provider, data if isinstance(data, dict) else {})
    return {
        "provider": provider,
        "model": native_model,
        "text": text,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "reasoning": reasoning_text,
    }


def _extract_reasoning(provider: str, data: dict[str, Any]) -> str:
    """The model's reasoning/thinking text from a single-shot response, read per
    provider (B4). Anthropic returns `thinking` content blocks in its messages
    body; the OpenAI-compat lanes use message.reasoning_content / message.reasoning
    (handled by app.reasoning.extract_reasoning_text). Returns "" when absent."""
    if provider == "anthropic":
        parts: list[str] = []
        for item in data.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "thinking":
                t = item.get("thinking") or item.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t)
        return "".join(parts)
    from app import reasoning as _reasoning

    return _reasoning.extract_reasoning_text(provider, data)


def _agent_base_url(provider: str, customs: dict[str, dict[str, str]], cfg: dict[str, Any]) -> str:
    """The OpenAI-compatible BASE url (no /chat/completions) the Pydantic AI agent
    needs, derived from the same provider table the single-shot lane uses. Anthropic
    needs none (its provider class knows its own endpoint)."""
    if provider.startswith("custom:"):
        return (customs.get(provider, {}).get("base_url") or "").rstrip("/")
    if provider == "kaidera-manifold":
        return _manifold_base_url(cfg)
    meta = _OWN_OPENAI_COMPAT_CHAT.get(provider)
    if not meta:
        return ""
    url = meta[1].rstrip("/")
    suffix = "/chat/completions"
    return url[: -len(suffix)] if url.endswith(suffix) else url


async def _stream_kaidera(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    thinking: str | None = None,
    workspace: str | None = None,
    project_key: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Run the kaidera lane. Resolve the provider/model/key with the existing
    resolver, then fork:

      * ``codex-subscription`` → the single-shot OAuth path (``_kaidera_complete``);
        Pydantic AI can't reach the ChatGPT backend, so it stays chat-only.
      * every other provider → the REAL tool-using agent (``app/kaidera_agent.py``):
        a Pydantic AI agent with bash / read / write / web tools + a tool-execution
        loop, its events translated back into our session/thinking/tool/delta/
        result/done dicts so run_agent + the SSE layer are unchanged.
    """
    chosen = (model or "").strip() or OWN_HARNESS_DEFAULT_MODEL
    cfg, customs, resolver = _own_runtime_config()
    provider, native_model, api_key = _own_target(chosen, cfg, customs, resolver)

    # codex-subscription: single-shot only (Pydantic AI can't reach the ChatGPT backend).
    if provider == "codex-subscription":
        async for ev in _stream_kaidera_singleshot(prompt, chosen, system, thinking, workspace=workspace):
            yield ev
        return

    if not api_key:
        yield {"type": "session", "session_id": None, "model": chosen}
        yield {"type": "error", "category": "provider_not_configured",
               "message": f"Kaidera AI harness cannot run {native_model}: no {provider} provider key is configured."}
        yield {"type": "done"}
        return

    agent_extra_headers: dict[str, str] = {}
    if provider == "kaidera-manifold":
        manifold_project_id = _manifold_project_id(cfg)
        if not manifold_project_id:
            yield {"type": "session", "session_id": None, "model": chosen}
            yield {
                "type": "error",
                "category": "provider_not_configured",
                "message": "Kaidera AI Manifold is disabled because no platform project id is configured.",
            }
            yield {"type": "done"}
            return
        agent_extra_headers["X-Project-Id"] = manifold_project_id

    from app import providers as _providers
    from app import reasoning as _reasoning

    live_levels = _providers.cached_reasoning_levels(provider, native_model)
    reasoning_fields: dict[str, Any] = {}
    if provider == "anthropic":
        reasoning_fields.update(
            _reasoning.anthropic_thinking_fields(
                native_model,
                thinking,
                available_levels=live_levels,
            )
        )
    else:
        _reasoning.apply_reasoning(
            provider,
            native_model,
            thinking,
            reasoning_fields,
            available_levels=live_levels,
        )

    # The REAL tool-using agent — but DEGRADE to a plain single-shot reply if it can't
    # produce output (pydantic-ai absent, a provider that rejects the tools payload, an
    # early error, any crash). A worker must NEVER go silent: tools are a bonus, a reply
    # is mandatory. We hold the session event until the first real content; if the agent
    # errors/crashes before producing anything, we fall through to the no-tools path.
    from . import kaidera_agent
    workspace = workspace or os.environ.get("KAIDERA_AGENT_WORKSPACE") or os.getcwd()
    session_ev: dict[str, Any] = {"type": "session", "session_id": None, "model": native_model}
    produced = False
    reply_produced = False
    done_emitted = False
    try:
        agent_kwargs: dict[str, Any] = {
            "provider": provider,
            "model": native_model,
            "api_key": api_key,
            "base_url": _agent_base_url(provider, customs, cfg),
            "prompt": prompt,
            "system": system,
            "workspace": workspace,
            "max_tokens": OWN_HARNESS_MAX_TOKENS,
            "reasoning_fields": reasoning_fields,
        }
        if agent_extra_headers:
            agent_kwargs["extra_headers"] = agent_extra_headers
        async for ev in kaidera_agent.stream_kaidera_agent(**agent_kwargs):
            t = ev.get("type")
            if t == "session":
                session_ev = ev                 # hold; emit on first real content
                continue
            if t in ("delta", "tool", "thinking", "result"):
                text = str(ev.get("text") or "")
                if t in ("delta", "result") and not text.strip():
                    # An empty final `result` is not a reply. This used to mark
                    # the turn successful and suppress the no-tools fallback,
                    # which surfaced to users as "completed without a text reply".
                    continue
                if not produced:
                    produced = True
                    yield session_ev
                if t in ("delta", "result"):
                    reply_produced = True      # an actual reply (not just thinking/tools)
                yield ev
            elif t == "error":
                if reply_produced:
                    yield ev                   # a trailing error AFTER a real reply is honest
                # else: swallow; fall back to the no-tools path below for a real reply
            elif t == "done":
                if reply_produced:
                    yield ev
                    done_emitted = True
                # else: swallow the premature done; fall back for a real reply
            else:
                yield ev
    except Exception:  # noqa: BLE001 — any agent crash degrades, never propagates as silence
        pass
    if not reply_produced:
        # DEGRADE: the tool-using agent produced no actual reply (it only streamed
        # thinking / called a tool that errored / crashed mid-turn — e.g. a project
        # workspace that isn't mounted in a container). A worker must NEVER go silent,
        # so answer via the plain no-tools chat path, which guarantees a reply (or a
        # clean provider error) instead of "completed without a text reply".
        async for ev in _stream_kaidera_singleshot(prompt, chosen, system, thinking, workspace=workspace):
            if produced and ev.get("type") == "session":
                continue
            yield ev
    elif not done_emitted:
        yield {"type": "done"}


async def _stream_kaidera_singleshot(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    thinking: str | None = None,
    workspace: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """The no-tools chat path: ONE provider call, return the reply. Used for
    codex-subscription (Pydantic AI can't reach the ChatGPT backend) AND as the
    graceful fallback when the tool-using agent can't run — pydantic-ai absent, a
    tools-incompatible provider, an early error. A worker must never go silent:
    tools are a bonus, a reply is mandatory."""
    chosen = (model or "").strip() or OWN_HARNESS_DEFAULT_MODEL
    yield {"type": "session", "session_id": None, "model": chosen}
    try:
        result = await _kaidera_complete(prompt, chosen, system, thinking, workspace=workspace)
    except _OwnHarnessError as exc:
        yield {"type": "error", "category": exc.category, "message": exc.message}
        yield {"type": "done"}
        return
    # B4: surface the model's thinking (when the provider returned it) BEFORE the
    # answer, as a `thinking` event — same shape the tool-using agent path emits,
    # so the feed/SSE layer renders it identically.
    reasoning_text = (result.get("reasoning") or "").strip()
    if reasoning_text:
        yield {"type": "thinking", "text": reasoning_text}
    yield {
        "type": "result", "text": result["text"], "cost_usd": None,
        "session_id": None, "tokens_in": result.get("tokens_in"),
        "tokens_out": result.get("tokens_out"),
    }
    yield {"type": "done"}


# ---------------------------------------------------------------------------
#  graceful lane — unknown harnesses / missing CLIs
# ---------------------------------------------------------------------------

async def _stream_graceful(
    harness: str,
    model: str | None = None,
    reason: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream ONE clear status message + a clean done event for a harness whose
    runner isn't wired (unknown harness) or a missing CLI.

    Yields a ``delta`` (so the feed shows the line like any reply) followed by a
    ``done``. NEVER spawns a process and NEVER crashes — this is the graceful
    fallback the spec requires for unwired lanes. `reason` adds a short cause to
    the message (e.g. a missing binary); otherwise the default 'runner not yet
    wired' copy is used."""
    label = (harness or "unknown").strip() or "unknown"
    m = (model or "").strip()
    model_note = f" · model {m}" if m else ""
    if reason:
        body = f"[harness {label}{model_note} — {reason}]"
    else:
        body = (
            f"[harness {label}{model_note} configured — runner not yet wired. "
            f"claude-code, codex, pi, and kaidera are wired lanes; pick "
            f"one in this agent's config, or wire the {label} runner.]"
        )
    yield {"type": "delta", "text": body}
    yield {"type": "done"}
