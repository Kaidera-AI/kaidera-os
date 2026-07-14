"""Dev-gated RuntimeBackend selection helpers.

This adapter-layer module is the only app code that selects the prototype Herdr
runtime. Routes, orchestrator, harness-service, and run-agent should depend on
the pure runtime contract/selection value, not import ``HerdrCliRuntimeBackend``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Optional

from app.domain.runtime import (
    RUNTIME_BACKEND_HERDR_VISIBLE,
    RuntimeBackend,
    RuntimeBackendSelection,
    select_runtime_backend,
)


RUNTIME_BACKEND_ENV = "KAIDERA_OS_RUNTIME_BACKEND"
HERDR_VISIBLE_GATE_ENV = "KAIDERA_OS_ENABLE_HERDR_VISIBLE"
LEGACY_RUNTIME_BACKEND_ENV = "LOCAL" + "DEV_RUNTIME_BACKEND"
LEGACY_HERDR_VISIBLE_GATE_ENV = "LOCAL" + "DEV_ENABLE_HERDR_VISIBLE"

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def herdr_visible_gate_enabled(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def runtime_backend_selection_from_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    requested: Optional[str] = None,
) -> RuntimeBackendSelection:
    source = os.environ if env is None else env
    runtime_backend = source.get(RUNTIME_BACKEND_ENV)
    if runtime_backend is None:
        runtime_backend = source.get(LEGACY_RUNTIME_BACKEND_ENV)
    herdr_gate = source.get(HERDR_VISIBLE_GATE_ENV)
    if herdr_gate is None:
        herdr_gate = source.get(LEGACY_HERDR_VISIBLE_GATE_ENV)
    return select_runtime_backend(
        requested if requested is not None else runtime_backend,
        herdr_visible_enabled=herdr_visible_gate_enabled(herdr_gate),
    )


def _default_herdr_backend_factory() -> RuntimeBackend:
    from app.adapters.runtime_herdr import HerdrCliRuntimeBackend

    return HerdrCliRuntimeBackend()


def make_runtime_backend(
    *,
    env: Optional[Mapping[str, str]] = None,
    requested: Optional[str] = None,
    direct_backend: Optional[RuntimeBackend] = None,
    herdr_backend_factory: Optional[Callable[[], RuntimeBackend]] = None,
) -> tuple[RuntimeBackendSelection, Optional[RuntimeBackend]]:
    """Return the selected backend object without changing the direct default.

    Until a real ``DirectSubprocessBackend`` lands, callers pass the existing
    direct implementation as ``direct_backend``. For selection-only proof/tests,
    a ``None`` backend with ``selection.backend == "direct"`` is the rollback
    evidence: Herdr is not constructed unless the explicit dev gate is enabled.
    """

    selection = runtime_backend_selection_from_env(env, requested=requested)
    if selection.backend == RUNTIME_BACKEND_HERDR_VISIBLE:
        herdr_backend_factory = herdr_backend_factory or _default_herdr_backend_factory
        return selection, herdr_backend_factory()
    return selection, direct_backend


__all__ = [
    "HERDR_VISIBLE_GATE_ENV",
    "LEGACY_HERDR_VISIBLE_GATE_ENV",
    "LEGACY_RUNTIME_BACKEND_ENV",
    "RUNTIME_BACKEND_ENV",
    "herdr_visible_gate_enabled",
    "make_runtime_backend",
    "runtime_backend_selection_from_env",
]
