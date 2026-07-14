"""RuntimeBackend — pure runtime lifecycle seam for visible/headless agent runs.

This port is deliberately separate from ``HarnessPort``. ``HarnessPort`` is the
worker-SPAWN seam (fire-and-forget host worker launch). ``RuntimeBackend`` is the
runtime LIFECYCLE seam: start, stream, send, status, stop, and reattach. E008 uses
this split so Herdr can replace/avoid app-side visible-terminal responsibilities
without becoming another ad hoc spawn path.

Pure-domain law: this module imports only the standard library. Concrete I/O lives
in adapters (for example, a future DirectSubprocessBackend or HerdrBackend).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Mapping, Optional, Protocol, runtime_checkable


RUNTIME_BACKEND_DIRECT = "direct"
RUNTIME_BACKEND_HERDR_VISIBLE = "herdr-visible"

# Herdr pane output is operational terminal text, not canonical telemetry. Adapters
# that mirror pane reads into run_span must cap each append and keep full history in
# Herdr only unless a later explicit archival decision says otherwise.
DEFAULT_RUNTIME_OUTPUT_MAX_CHARS = 16_384


@dataclass(frozen=True)
class RuntimeBackendSelection:
    """Resolved runtime backend choice for a single run.

    The selector is intentionally pure: callers provide the requested backend and
    whether the Herdr visible-runtime dev gate is enabled. Unknown or disabled
    requests fall back to ``direct`` so rollback is a config-only operation.
    """

    backend: str
    requested: Optional[str] = None
    herdr_visible_enabled: bool = False
    reason: str = "direct-default"


def select_runtime_backend(
    requested: Optional[str] = None,
    *,
    herdr_visible_enabled: bool = False,
) -> RuntimeBackendSelection:
    """Resolve a runtime backend while preserving ``direct`` as the default."""

    normalized = (requested or "").strip().lower()
    if not normalized or normalized == RUNTIME_BACKEND_DIRECT:
        return RuntimeBackendSelection(
            backend=RUNTIME_BACKEND_DIRECT,
            requested=normalized or None,
            herdr_visible_enabled=herdr_visible_enabled,
            reason="direct-default",
        )
    if normalized == RUNTIME_BACKEND_HERDR_VISIBLE:
        if herdr_visible_enabled:
            return RuntimeBackendSelection(
                backend=RUNTIME_BACKEND_HERDR_VISIBLE,
                requested=normalized,
                herdr_visible_enabled=True,
                reason="herdr-visible-dev-gate",
            )
        return RuntimeBackendSelection(
            backend=RUNTIME_BACKEND_DIRECT,
            requested=normalized,
            herdr_visible_enabled=False,
            reason="herdr-visible-disabled",
        )
    return RuntimeBackendSelection(
        backend=RUNTIME_BACKEND_DIRECT,
        requested=normalized,
        herdr_visible_enabled=herdr_visible_enabled,
        reason="unknown-requested-backend",
    )


@dataclass
class RuntimeRef:
    """Re-resolvable operational reference for a backend run.

    Herdr ids are cached live ids, not durable truth. The labels/session fields are
    carried so the adapter can re-resolve workspace/tab/pane before acting.
    ``metadata`` is intentionally small and adapter-owned.
    """

    backend: str
    session_name: Optional[str] = None
    workspace_id: Optional[str] = None
    workspace_label: Optional[str] = None
    tab_id: Optional[str] = None
    tab_label: Optional[str] = None
    pane_id: Optional[str] = None
    pane_label: Optional[str] = None
    protocol: Optional[int] = None
    version: Optional[str] = None
    last_resolved_at: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RuntimeStartRequest:
    """Request to start one runtime-managed run.

    ``argv`` is the concrete command vector the backend will run. ``env`` is a
    small overlay, not a full process environment dump. ``visible`` lets callers
    request terminal-native behavior without knowing the concrete backend.
    """

    run_id: str
    project: str
    agent: str
    cwd: str
    argv: list[str]
    env: Mapping[str, str] = field(default_factory=dict)
    handoff_id: Optional[str] = None
    harness: Optional[str] = None
    model: Optional[str] = None
    visible: bool = False
    run_timeout_s: float = 900.0
    metadata: dict = field(default_factory=dict)


@dataclass
class RuntimeRun:
    """Backend acknowledgement for a started or reattached run."""

    run_id: str
    backend: str
    status: str
    ref: RuntimeRef
    accepted: bool = True
    error: Optional[str] = None


@dataclass
class RuntimeEvent:
    """Bounded event emitted by a runtime backend stream."""

    run_id: str
    seq: int
    kind: str
    text: str = ""
    status: Optional[str] = None
    ts: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RuntimeStatus:
    """Current runtime view for one run.

    This is supplemental operational state. It must never complete a Cortex handoff
    by itself; writers translate it into app-DB run_state updates under policy.
    """

    run_id: str
    backend: str
    status: str
    ref: RuntimeRef
    agent_status: Optional[str] = None
    error: Optional[str] = None
    heartbeat_at: Optional[str] = None


@runtime_checkable
class RuntimeBackend(Protocol):
    """Pure runtime lifecycle contract used by console orchestration.

    Direct subprocess and Herdr implementations both satisfy this shape. Routes and
    orchestrators should depend on this seam, not raw Herdr CLI/socket calls.
    """

    async def start_run(self, request: RuntimeStartRequest) -> RuntimeRun:
        """Start a runtime-managed run. Never raises for launch refusal; return
        ``RuntimeRun(accepted=False, error=...)`` instead."""
        ...

    async def stream(self, run_id: str) -> AsyncIterator[RuntimeEvent]:
        """Yield bounded runtime events for ``run_id`` until terminal or detached.

        Declared as an async generator so callers can use ``async for`` directly:
        ``async for event in backend.stream(run_id): ...``.
        """
        if False:  # pragma: no cover - shape-only; implementations override this
            yield RuntimeEvent(run_id=run_id, seq=0, kind="shape")

    async def send(self, run_id: str, text_or_keys: str) -> None:
        """Send input to an interactive runtime, if supported."""
        ...

    async def status(self, run_id: str) -> RuntimeStatus:
        """Return supplemental runtime status for ``run_id``."""
        ...

    async def stop(self, run_id: str, reason: Optional[str] = None) -> None:
        """Best-effort stop/cancel. Implementations should be idempotent."""
        ...

    async def reattach(self, run_id: str) -> Optional[RuntimeRun]:
        """Re-resolve and return an existing runtime run, or None if gone."""
        ...


__all__ = [
    "DEFAULT_RUNTIME_OUTPUT_MAX_CHARS",
    "RUNTIME_BACKEND_DIRECT",
    "RUNTIME_BACKEND_HERDR_VISIBLE",
    "RuntimeBackend",
    "RuntimeBackendSelection",
    "RuntimeEvent",
    "RuntimeRef",
    "RuntimeRun",
    "RuntimeStartRequest",
    "RuntimeStatus",
    "select_runtime_backend",
]
