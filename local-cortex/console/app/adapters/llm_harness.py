"""LLMPort adapter — wraps `harness_runner.stream_chat` (the harness lane).

The imperative-shell adapter (`app/adapters/`) that IMPLEMENTS the pure `LLMPort`
Protocol (`app/domain/ports.py`) over the EXISTING `harness_runner.stream_chat`.
Arrows point inward (ratified design §3): the domain port stays pure; this adapter
is the boundary that talks to the harness subprocess.

THIN + ADDITIVE: it does NOT reimplement any routing/streaming — it delegates 1:1
to `harness_runner.stream_chat`, which already routes by harness (claude-code |
codex | pi | graceful) and yields the harness-agnostic event stream
(session / delta / thinking / tool / result / error / done, always ending in
`done`). The only mapping is positional → the port's keyword-only `stream(...)`
surface; the `reasoning` level is forwarded straight through (it must NOT be
dropped — pi/claude map it to thinking-effort).

The real `stream_chat` is bound by default so production wiring needs no argument;
tests inject a fake to assert delegation without firing a billable harness.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Optional

from app import harness_runner


class HarnessLLMAdapter:
    """`LLMPort` over `harness_runner.stream_chat` (thin pass-through).

    Constructed with no args in production (binds the real `stream_chat`); tests
    pass `stream_chat=<fake>`. Satisfies the `LLMPort` Protocol structurally."""

    def __init__(
        self,
        stream_chat: Optional[Callable[..., AsyncIterator[dict[str, Any]]]] = None,
    ) -> None:
        # Default to the real runner so the app needs no wiring argument.
        self._stream_chat = stream_chat or harness_runner.stream_chat

    async def stream(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        harness: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Delegate to `stream_chat` and re-yield its events 1:1. The per-agent
        routing (model/system/harness/reasoning) is forwarded unchanged."""
        async for event in self._stream_chat(
            prompt,
            model=model,
            system=system,
            harness=harness,
            reasoning=reasoning,
        ):
            yield event


__all__ = ["HarnessLLMAdapter"]
