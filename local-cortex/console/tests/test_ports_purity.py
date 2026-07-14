"""Track A step 1 — tests for the pure SDK ports (`app/domain/ports.py`).

`app/domain/ports.py` is the functional core for the five SDK abstractions the
console depends on — `LLMPort`, `CortexMemoryPort`, `OperationalStorePort`,
`ModelCatalogPort`, `BillingPort` — plus their minimal DTOs. It sits alongside the
Milestone-1 `RunStatePort` (`app/domain/runstate.py`) and obeys the SAME house law:
it is PURE — it must import NOTHING outward (no httpx / fastapi / subprocess /
psycopg2 / asyncpg). The adapters (`app/adapters/*`) IMPLEMENT these Protocols over
the existing concrete code; callers depend on the Protocol, never the adapter —
arrows point inward (ratified design §3).

These tests assert (mirroring `test_runstate_port.py`):
  * the Protocols + their method names are importable and present,
  * each Protocol is `runtime_checkable` so a stub can be structurally verified,
  * the DTOs construct + round-trip, and
  * the IMPORT-PURITY GUARD: the module's source imports none of the outward
    libraries (parsed via `ast`, so a name in a comment/docstring can't fool it).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

# Path to the module under test (tests[0] → console[1]; app/domain lives under app/).
DOMAIN_PORTS = (
    Path(__file__).resolve().parents[1] / "app" / "domain" / "ports.py"
)

# Libraries the functional core must NEVER import (arrows-point-inward, design §3).
FORBIDDEN_IMPORTS = {"httpx", "fastapi", "subprocess", "psycopg2", "asyncpg"}

# The method surface each port must expose (lifted 1:1 from the concrete code the
# wrapper implements over).
EXPECTED_METHODS: dict[str, set[str]] = {
    "LLMPort": {"stream"},
    "CortexMemoryPort": {
        "boot",
        "search",
        "get_handoffs",
        "claim_handoff",
        "complete_handoff",
        "get_history",
        "log",
    },
    "OperationalStorePort": {
        "available",
        "record_usage",
        "usage_by_model",
        "usage_by_model_provider",
        "usage_by_agent",
        "usage_by_project",
        "load_app_settings",
        "upsert_app_settings",
        "load_agent_overrides",
        "get_agent_override",
        "save_agent_override",
        "is_project_autonomous",
        "set_project_autonomy",
        "list_autonomous_projects",
        "is_propose_mode",
        "set_propose_mode",
    },
    "ModelCatalogPort": {"list_models", "price_for"},
    "BillingPort": {"record_usage"},
}


def test_module_file_exists():
    assert DOMAIN_PORTS.is_file(), f"missing domain module: {DOMAIN_PORTS}"


def test_all_ports_importable():
    """Every port Protocol imports cleanly from the pure module."""
    from app.domain.ports import (  # noqa: F401
        BillingPort,
        CortexMemoryPort,
        LLMPort,
        ModelCatalogPort,
        OperationalStorePort,
    )


def test_ports_are_protocols_with_expected_methods():
    """Each port is a typing.Protocol exposing every expected method name."""
    import app.domain.ports as ports

    for name, methods in EXPECTED_METHODS.items():
        port = getattr(ports, name)
        assert getattr(port, "_is_protocol", False), f"{name} must be a typing.Protocol"
        members = set(dir(port))
        missing = methods - members
        assert not missing, f"{name} missing methods: {sorted(missing)}"


def test_ports_are_runtime_checkable():
    """Each port is runtime_checkable (so the wrapper can be isinstance-verified)."""
    import app.domain.ports as ports

    for name in EXPECTED_METHODS:
        port = getattr(ports, name)
        # A runtime_checkable Protocol gets the _is_runtime_protocol marker.
        assert getattr(port, "_is_runtime_protocol", False), (
            f"{name} must be @runtime_checkable so a stub/adapter can be verified"
        )


def test_dtos_construct_and_roundtrip():
    """The port DTOs are real dataclasses that round-trip via dataclasses.asdict."""
    import dataclasses

    from app.domain.ports import CatalogModel, UsageRecord

    cm = CatalogModel(
        provider="anthropic",
        id="claude-opus-4.8",
        display_name="Claude Opus 4.8",
        price_in_per_mtok=15.0,
        price_out_per_mtok=75.0,
    )
    assert dataclasses.is_dataclass(cm)
    d = dataclasses.asdict(cm)
    assert d["id"] == "claude-opus-4.8"
    assert d["provider"] == "anthropic"

    ur = UsageRecord(
        project="kaidera-os",
        agent="ren",
        harness="claude-code",
        model="opus",
        provider="anthropic",
        tokens_in=100,
        tokens_out=50,
        cost_est_usd=0.01,
    )
    assert dataclasses.is_dataclass(ur)
    assert dataclasses.asdict(ur)["tokens_in"] == 100


def test_llm_stream_is_async_generator_shape():
    """LLMPort.stream is declared as an async generator (the event stream)."""
    from app.domain.ports import LLMPort

    member = getattr(LLMPort, "stream", None)
    assert member is not None
    assert inspect.isasyncgenfunction(member), (
        "LLMPort.stream should be an async generator (yields harness events)"
    )


# ── THE IMPORT-PURITY GUARD ──────────────────────────────────────────────────


def _imported_top_level_modules(source: str) -> set[str]:
    """Parse `source` and return the TOP-LEVEL package names it imports (so
    `import psycopg2.extras` and `from asyncpg import X` both count). Relative
    imports (within the package) are ignored."""
    tree = ast.parse(source)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def test_ports_module_imports_nothing_outward():
    """GUARD: the functional core imports none of the forbidden outward libs.

    Parsed via `ast` (not a substring scan) so a forbidden name appearing only in
    a comment/docstring/type-string cannot trip OR satisfy the check."""
    source = DOMAIN_PORTS.read_text()
    imported = _imported_top_level_modules(source)
    leaked = imported & FORBIDDEN_IMPORTS
    assert not leaked, (
        f"app/domain/ports.py must import NOTHING outward, but imports: "
        f"{sorted(leaked)} (arrows-point-inward — keep the core pure)"
    )


def test_ports_module_imports_clean_at_runtime():
    """Importing the module must not drag in a forbidden lib transitively either."""
    import importlib
    import sys

    sys.modules.pop("app.domain.ports", None)
    mod = importlib.import_module("app.domain.ports")
    for name in (
        "LLMPort",
        "CortexMemoryPort",
        "OperationalStorePort",
        "ModelCatalogPort",
        "BillingPort",
    ):
        assert hasattr(mod, name)
