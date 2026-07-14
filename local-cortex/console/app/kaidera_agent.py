"""The kaidera harness — a real, tool-using agent (app/kaidera_agent.py).

Before this, the kaidera harness was a single chat-completion call (prompt in,
text out, no agency) — which is why a kaidera worker would *say* "I'll install
Playwright" or "I can't browse the web" and do neither: there was no machinery
behind the words. This module turns kaidera into a Pydantic AI ``Agent`` with a
small, safe tool set (bash / read_file / write_file / web_fetch — parity with
``pi --tools read,bash,edit,write``) and a real tool-execution loop.

It TRANSLATES Pydantic AI's streaming events into the console's harness event
dicts (session / thinking / tool / delta / result / error / done), so
``run_agent.py`` and the SSE layer stay harness-agnostic — they never learn
kaidera changed.

Coupling: the provider / native-model / api-key / base-url are resolved by the
caller (``harness_runner`` via ``_own_target``) and passed in, so this module has
no dependency on ``harness_runner`` internals. The codex-subscription OAuth lane
is NOT handled here (Pydantic AI can't reach the ChatGPT backend) — it stays on
the single-shot path in ``harness_runner._kaidera_complete``.

SECURITY MODEL: this agent runs arbitrary shell + fetches the web on behalf of an
autonomous worker — the SAME trust surface as the ``pi`` / ``claude`` CLIs (the
worker acts on its OWN deployment box). ``web_fetch`` is SSRF-guarded (no private /
loopback / link-local / cloud-metadata targets). ``run_bash`` is the deliberately
broad capability the operator enables; a hardened deployment can wrap every command
in a sandbox via ``KAIDERA_AGENT_BASH_WRAPPER`` (e.g. firejail / bubblewrap)
without any code change.

``pydantic_ai`` is imported lazily inside the entrypoint so merely importing this
module never hard-requires the dependency: the console still boots if it's absent,
and a kaidera run then yields a clear error event instead of crashing.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import shlex
import socket
from pathlib import Path
from typing import Any, AsyncGenerator, Callable
from urllib.parse import urlparse

import httpx

def _int_env(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to ``default`` on absence
    or any malformed value (``''`` / ``0`` / non-numeric). Keeps every tuning knob
    operator-overridable without letting a bad value silently break the agent."""
    try:
        v = int((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


# Tool-execution bounds. bash is time + output capped; the file tools are confined to
# the workspace; web_fetch is size + time capped + SSRF-guarded. The char caps are
# deliberately modest — a tool result is fed straight back into the model context, so a
# single huge page/file must not dominate (let alone overflow) the window. All overridable.
_BASH_TIMEOUT_S = 60.0
_BASH_OUTPUT_CAP = _int_env("KAIDERA_AGENT_BASH_CHARS", 16_000)
_FILE_READ_CAP = _int_env("KAIDERA_AGENT_FILE_READ_CHARS", 48_000)
_WEB_FETCH_CAP = _int_env("KAIDERA_AGENT_WEB_FETCH_CHARS", 48_000)
_WEB_FETCH_TIMEOUT_S = 30.0
_WEB_FETCH_MAX_REDIRECTS = 5

# Agent-loop bounds — the two production knobs `pi` has built in and the raw loop lacked:
#  * _CONTEXT_CHAR_BUDGET — agent.iter() accumulates EVERY tool result in the message
#    history; untrimmed, a few large results blow past the model's context window (the
#    "prompt is too long" 400). _fit_context keeps the history under this char budget (a
#    proxy for tokens) — safe for a 128K-token model and up; raise it for a larger window.
#  * _REQUEST_LIMIT — caps model round-trips per run. Pydantic AI defaults to 50, which a
#    real multi-step task exceeds (UsageLimitExceeded); 200 fits real work + still backstops
#    a runaway loop.
_CONTEXT_CHAR_BUDGET = _int_env("KAIDERA_AGENT_CONTEXT_CHARS", 300_000)
_HISTORY_TOOL_RESULT_CAP = _int_env("KAIDERA_AGENT_TOOL_RESULT_CHARS", 12_000)
_REQUEST_LIMIT = _int_env("KAIDERA_AGENT_REQUEST_LIMIT", 200)

# The User-Agent every web_fetch sends. Hoisted to a module constant so both the initial
# request and the per-redirect re-fetch use the same value (and to keep the harness's own
# name out of the inline string the no-project-literals gate scans).
_USER_AGENT = "kaidera-agent/1.0"  # fitness:allow-literal — kaidera is the harness name, not the project

# Appended to every kaidera system prompt so the model KNOWS it has real tools —
# the single most effective counter to the "I can't access the web / I'll install
# X (and then doesn't)" hallucination. The model stops refusing once it is told,
# truthfully, that the capability exists and is its to call.
_TOOL_AWARENESS = (
    "You are running with REAL tools and MUST use them to do the work:\n"
    "- run_bash(command): run a shell command in the project workspace — install "
    "packages, run scripts, use git, curl/wget a URL, inspect the filesystem.\n"
    "- read_file(path) / write_file(path, content): read and write files in the workspace.\n"
    "- web_fetch(url): fetch any public web page or API over HTTP(S) and read its contents.\n"
    "Never claim you cannot browse the web, run commands, or edit files — you can, via "
    "these tools. Prefer DOING (call a tool and use the result) over describing what you "
    "would do. When you say you will do something, call the tool and verify the outcome."
)


def _ssrf_blocked(url: str) -> str | None:
    """SSRF guard: return a reason string if ``url``'s host resolves to a private,
    loopback, link-local, or otherwise non-public address — else ``None``. Resolves
    via ``getaddrinfo`` so a public DNS name pointing at an internal IP (including the
    cloud metadata endpoint 169.254.169.254) is also caught. Critical on a cloud VM:
    without this an agent-controlled URL could read instance credentials or the
    internal Cortex / app-DB services."""
    try:
        host = (urlparse(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return "an unparseable URL"
    if not host:
        return "a URL with no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return f"a host that does not resolve ({host})"
    for info in infos:
        raw = info[4][0].split("%", 1)[0]  # strip any IPv6 zone id
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return f"a non-public address ({host} -> {raw})"
    return None


class KaideraTools:
    """The default kaidera tool set, bound to a workspace directory. Each public
    async method is registered as a Pydantic AI tool — its DOCSTRING becomes the
    description the model sees, and its typed parameters become the tool schema."""

    def __init__(self, workspace: str | None) -> None:
        if workspace:
            cand = Path(workspace)
            # A containerized console may be handed a project repo_root that isn't
            # mounted in this pod; resolve() succeeds on a non-existent path but
            # run_bash(cwd=...) would then raise FileNotFoundError and kill the turn
            # (the "kaidera agent error (FileNotFoundError)" seen when the project  # fitness:allow-literal — kaidera harness name
            # workspace differs from the VM). Fall back to the process cwd (always
            # exists) so the tool agent stays usable when the workspace isn't mounted.
            self.workspace = cand.resolve() if cand.exists() else Path.cwd()
        else:
            self.workspace = Path.cwd()

    def _confined(self, path: str) -> Path:
        """Resolve ``path`` and refuse anything that escapes the workspace."""
        p = Path(path)
        full = (p if p.is_absolute() else self.workspace / p).resolve()
        if full != self.workspace and self.workspace not in full.parents:
            raise ValueError(f"path '{path}' is outside the project workspace")
        return full

    async def run_bash(self, command: str) -> str:
        """Run a shell command in the project workspace and return combined
        stdout+stderr. Use it to install tools, run scripts, use git, curl/wget a
        URL, or inspect the filesystem. Times out after 60 seconds."""
        # A hardened deployment may wrap EVERY command in a sandbox by setting
        # KAIDERA_AGENT_BASH_WRAPPER (e.g. "firejail --quiet --private" or a bubblewrap
        # line); we then exec `<wrapper> bash -c <command>` so the whole shell line runs
        # inside the sandbox. Unset (the default) = a direct shell — the deliberately
        # broad `pi --tools bash`-equivalent capability the operator chose to enable.
        wrapper = os.environ.get("KAIDERA_AGENT_BASH_WRAPPER", "").strip()
        try:
            if wrapper:
                argv = [*shlex.split(wrapper), "bash", "-c", command]
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(self.workspace),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(self.workspace),
                )
        except (FileNotFoundError, OSError) as exc:
            # The workspace vanished mid-run or the wrapper/shell binary is absent:
            # a RECOVERABLE tool error, never a run-killer.
            return f"[bash: {type(exc).__name__}: {exc}]"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_BASH_TIMEOUT_S)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return f"[bash: timed out after {_BASH_TIMEOUT_S:.0f}s]"
        text = (out or b"").decode("utf-8", "replace")
        if len(text) > _BASH_OUTPUT_CAP:
            text = text[:_BASH_OUTPUT_CAP] + f"\n[...truncated; {len(text)} bytes total]"
        return text or f"[bash: exit {proc.returncode}, no output]"

    async def read_file(self, path: str) -> str:
        """Read a UTF-8 text file from the project workspace and return its contents."""
        try:
            full = self._confined(path)
        except ValueError as exc:
            # Out-of-workspace is a RECOVERABLE tool error, not a run-killer: return it
            # so the model can correct (e.g. use run_bash/cat or the right path).
            return f"[read_file: {exc}]"

        def _read() -> str:
            data = full.read_text("utf-8", "replace")
            return data if len(data) <= _FILE_READ_CAP else data[:_FILE_READ_CAP] + "\n[...truncated]"

        try:
            return await asyncio.to_thread(_read)
        except FileNotFoundError:
            return f"[read_file: no such file '{path}']"
        except OSError as exc:
            return f"[read_file: {exc}]"

    async def write_file(self, path: str, content: str) -> str:
        """Create or overwrite a UTF-8 text file in the project workspace. Returns a
        short confirmation with the number of bytes written."""
        try:
            full = self._confined(path)
        except ValueError as exc:
            return f"[write_file: {exc}]"

        def _write() -> int:
            full.parent.mkdir(parents=True, exist_ok=True)
            return full.write_text(content, "utf-8")

        try:
            n = await asyncio.to_thread(_write)
            return f"[write_file: wrote {n} bytes to {path}]"
        except OSError as exc:
            return f"[write_file: {exc}]"

    async def web_fetch(self, url: str) -> str:
        """Fetch a public URL over HTTP(S) and return the response body as text (HTML
        included). Use it to read websites, documentation, or JSON APIs. Times out
        after 30 seconds. Non-public targets (localhost / private / cloud-metadata)
        are refused."""
        if not url.lower().startswith(("http://", "https://")):
            return f"[web_fetch: only http(s) URLs are supported, got '{url}']"
        reason = _ssrf_blocked(url)
        if reason:
            return f"[web_fetch: refused — {reason}]"
        try:
            # follow_redirects=False on purpose: we re-validate EACH hop ourselves so a
            # public URL cannot 30x-redirect into an internal/metadata address.
            async with httpx.AsyncClient(timeout=_WEB_FETCH_TIMEOUT_S, follow_redirects=False) as client:
                resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
                hops = 0
                while resp.is_redirect and resp.next_request is not None and hops < _WEB_FETCH_MAX_REDIRECTS:
                    nxt = str(resp.next_request.url)
                    # Re-validate BOTH the scheme and the host on every hop: a 30x to a
                    # non-http(s) scheme (file:// / gopher:// / …) would otherwise bypass
                    # the initial scheme check and let a redirect read local disk.
                    if not nxt.lower().startswith(("http://", "https://")):
                        return f"[web_fetch: refused redirect to a non-http(s) URL ('{nxt}')]"
                    reason = _ssrf_blocked(nxt)
                    if reason:
                        return f"[web_fetch: refused redirect to {reason}]"
                    # NOTE: the getaddrinfo (in _ssrf_blocked) → httpx.get sequence is a
                    # known TOCTOU (DNS rebinding) residual — a name could resolve to a
                    # public IP at the check and a private one at the fetch. The per-hop
                    # host+scheme re-validation here mitigates the common cases; closing it
                    # fully needs IP-pinning via a custom transport (out of scope here).
                    resp = await client.get(nxt, headers={"User-Agent": _USER_AGENT})
                    hops += 1
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            return f"[web_fetch: {type(exc).__name__}: {exc}]"
        text = resp.text or ""
        if len(text) > _WEB_FETCH_CAP:
            text = text[:_WEB_FETCH_CAP] + f"\n[...truncated; {len(text)} chars total]"
        return text or "[web_fetch: empty body]"


def _build_model(provider: str, model: str, api_key: str, base_url: str) -> Any:
    """Construct the public edition's Manifold-backed Pydantic AI model."""
    if provider != "kaidera-manifold":
        raise ValueError(f"unsupported provider: {provider}")
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    return OpenAIChatModel(model, provider=OpenAIProvider(base_url=base_url, api_key=api_key))


def _apply_reasoning_settings(
    model_settings: dict[str, Any],
    provider: str,
    reasoning_fields: dict[str, Any] | None,
) -> None:
    """Apply Manifold-validated reasoning fields to Pydantic AI settings."""
    if provider != "kaidera-manifold":
        raise ValueError(f"unsupported provider: {provider}")
    fields = dict(reasoning_fields or {})
    if fields:
        model_settings["extra_body"] = fields


def _arg_preview(args: Any) -> str:
    try:
        s = args if isinstance(args, str) else json.dumps(args, default=str)
    except (TypeError, ValueError):
        s = str(args)
    s = " ".join(s.split())
    return s if len(s) <= 160 else s[:160] + "…"


def _text_preview(value: Any) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= 200 else s[:200] + "…"


def _final_output(run: Any) -> str:
    res = getattr(run, "result", None)
    for attr in ("output", "data"):
        val = getattr(res, attr, None)
        if isinstance(val, str):
            return val
    return ""


def _usage_field(usage: Any, *names: str) -> int | None:
    for n in names:
        v = getattr(usage, n, None)
        if isinstance(v, int):
            return v
    return None


# --- Context-window management (the "prompt is too long" fix) -----------------------
# These run as a `ProcessHistory` capability before every model request. They are
# duck-typed (getattr + the `part_kind` discriminator) on purpose: no hard dependency on
# pydantic-ai's message classes, so the module imports without the dep AND the logic is
# trivially unit-testable with simple stand-in objects.

def _part_size(part: Any) -> int:
    """Rough char footprint of one message part — its text/content plus any tool args."""
    total = 0
    for attr in ("content", "args"):
        v = getattr(part, attr, None)
        if isinstance(v, str):
            total += len(v)
        elif v is not None:
            try:
                total += len(json.dumps(v, default=str))
            except (TypeError, ValueError):
                total += len(str(v))
    return total


def _msg_size(msg: Any) -> int:
    return sum(_part_size(p) for p in getattr(msg, "parts", None) or [])


def _is_tool_return(part: Any) -> bool:
    return getattr(part, "part_kind", "") == "tool-return"


def _has_tool_return(msg: Any) -> bool:
    return any(_is_tool_return(p) for p in getattr(msg, "parts", None) or [])


def _compact_old_tool_returns(messages: list[Any]) -> None:
    """Shrink large tool-return contents in every message EXCEPT the most recent, so the
    model keeps a rich view of its latest result while older results stop dominating the
    window on every later request. Mutates in place — message parts are non-frozen
    dataclasses. Idempotent: re-running re-truncates to the same length."""
    for msg in messages[:-1]:
        for part in getattr(msg, "parts", None) or []:
            if _is_tool_return(part):
                c = getattr(part, "content", None)
                if isinstance(c, str) and len(c) > _HISTORY_TOOL_RESULT_CAP:
                    part.content = (c[:_HISTORY_TOOL_RESULT_CAP]
                                    + f"\n[...older tool output truncated to {_HISTORY_TOOL_RESULT_CAP} chars]")


def _fit_context(messages: list[Any]) -> list[Any]:
    """Keep the running message history under a char budget so the tool-execution loop
    can't overflow the model's context window — the fix for the ``prompt is too long`` 400.

    Cheapest-first: (1) compact OLD tool results; (2) if still over budget, keep the first
    message (it carries the system prompt + the original task) plus as many of the MOST
    RECENT messages as fit, dropping the middle. A dropped tool-call would orphan its
    tool-return — which providers reject — so the kept window never starts on a bare
    tool-return."""
    if not messages:
        return messages
    _compact_old_tool_returns(messages)
    if len(messages) <= 2 or sum(_msg_size(m) for m in messages) <= _CONTEXT_CHAR_BUDGET:
        return messages
    head, rest = messages[0], messages[1:]
    running = _msg_size(head)
    kept_rev: list[Any] = []
    for msg in reversed(rest):
        size = _msg_size(msg)
        if kept_rev and running + size > _CONTEXT_CHAR_BUDGET:
            break
        kept_rev.append(msg)
        running += size
    kept = list(reversed(kept_rev))
    while kept and _has_tool_return(kept[0]):  # never lead with an orphaned tool-return
        kept.pop(0)
    return [head, *kept]


async def stream_kaidera_agent(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    prompt: str,
    system: str | None,
    workspace: str | None = None,
    max_tokens: int = 4096,
    extra_headers: dict[str, str] | None = None,
    reasoning_fields: dict[str, Any] | None = None,
    extra_tools: list[Callable[..., Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Run the kaidera agent and yield console harness event dicts.

    Emits: ``session`` → (``thinking`` | ``delta`` | ``tool``)* → ``result`` →
    ``done``, or an ``error`` event then ``done``. ``extra_tools`` lets a caller add
    skill-provided tools (a skill's scripts become callable tools).
    """
    yield {"type": "session", "session_id": None, "model": model}
    try:
        from pydantic_ai import (
            Agent,
            FunctionToolCallEvent,
            FunctionToolResultEvent,
            PartDeltaEvent,
            TextPartDelta,
            ThinkingPartDelta,
        )
        from pydantic_ai.capabilities.process_history import ProcessHistory
        from pydantic_ai.settings import ModelSettings
        from pydantic_ai.usage import UsageLimits
    except ImportError as exc:
        yield {"type": "error", "category": "provider_error",
               "message": f"kaidera agent needs pydantic-ai installed: {exc}"}  # fitness:allow-literal — kaidera harness name
        yield {"type": "done"}
        return

    sys_full = ((system or "").strip() + "\n\n" + _TOOL_AWARENESS).strip()
    tools = KaideraTools(workspace)
    model_settings = ModelSettings(max_tokens=max_tokens)
    if extra_headers:
        model_settings["extra_headers"] = dict(extra_headers)
    _apply_reasoning_settings(model_settings, provider, reasoning_fields)
    agent = Agent(
        _build_model(provider, model, api_key, base_url),
        system_prompt=sys_full,
        model_settings=model_settings,
        tools=[tools.run_bash, tools.read_file, tools.write_file, tools.web_fetch,
               *(extra_tools or [])],
        capabilities=[ProcessHistory(_fit_context)],  # trim history to fit the context window
        retries=2,
    )

    try:
        async with agent.iter(prompt, usage_limits=UsageLimits(request_limit=_REQUEST_LIMIT)) as run:
            async for node in run:
                if Agent.is_model_request_node(node):
                    # The model is producing output — stream its text + thinking deltas.
                    async with node.stream(run.ctx) as request_stream:
                        async for ev in request_stream:
                            if isinstance(ev, PartDeltaEvent):
                                delta = ev.delta
                                if isinstance(delta, TextPartDelta) and delta.content_delta:
                                    yield {"type": "delta", "text": delta.content_delta}
                                elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
                                    yield {"type": "thinking", "text": delta.content_delta}
                elif Agent.is_call_tools_node(node):
                    # The model asked to call tools — surface each call + result as a
                    # `tool` event (run_agent logs these to the live trail + Cortex LTM).
                    async with node.stream(run.ctx) as handle_stream:
                        async for ev in handle_stream:
                            if isinstance(ev, FunctionToolCallEvent):
                                name = ev.part.tool_name
                                yield {"type": "tool", "name": name,
                                       "text": f"{name}({_arg_preview(ev.part.args)})"}
                            elif isinstance(ev, FunctionToolResultEvent):
                                # pydantic-ai ≥1.x exposes the result on `.part`
                                # (`.result` is deprecated, so we don't touch it).
                                part = getattr(ev, "part", None)
                                content = getattr(part, "content", "") if part is not None else ""
                                yield {"type": "tool",
                                       "name": getattr(ev, "tool_name", "") or "",
                                       "text": f"→ {_text_preview(content)}"}
        # `run.usage` became a property in pydantic-ai ≥1.x (older builds exposed a
        # method). Read it tolerantly + quietly — the back-compat shim warns on call.
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            usage = run.usage
            if callable(usage):
                usage = usage()
        yield {
            "type": "result",
            "text": _final_output(run),
            "cost_usd": None,
            "session_id": None,
            "tokens_in": _usage_field(usage, "input_tokens", "request_tokens"),
            "tokens_out": _usage_field(usage, "output_tokens", "response_tokens"),
        }
    except Exception as exc:  # never crash the worker — surface a clean error event
        yield {"type": "error", "category": "provider_error",
               "message": f"kaidera agent error ({type(exc).__name__}): {exc}"}  # fitness:allow-literal — kaidera harness name
    yield {"type": "done"}
