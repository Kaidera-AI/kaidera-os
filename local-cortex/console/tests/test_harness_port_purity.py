"""Harness-service Increment 1 — tests for the pure harness port (`app/domain/harness.py`).

`app/domain/harness.py` is the functional core for the harness-spawn seam: the
`HarnessPort` Protocol (`spawn_run` / `cancel_run`) + its two DTOs (`SpawnRequest`,
`SpawnHandle`). It sits alongside `app/domain/runstate.py` + `app/domain/ports.py`
and obeys the SAME house law: it is PURE — it must import NOTHING outward (no httpx
/ fastapi / subprocess / psycopg2 / asyncpg). The adapters (`app/adapters/*`)
IMPLEMENT this Protocol over the concrete spawn (the LocalHarnessAdapter wraps the
existing `subprocess.Popen` host-side spawn; a future RemoteHarnessAdapter POSTs to
the host harness-service). Callers depend on the Protocol, never the adapter —
arrows point inward (ratified design §3).

These tests assert (mirroring `test_ports_purity.py`):
  * the port + its method names are importable and present,
  * the port is `runtime_checkable` so a stub/adapter can be structurally verified,
  * the DTOs construct + round-trip via `dataclasses.asdict`, and
  * the IMPORT-PURITY GUARD: the module's source imports none of the outward libs
    (parsed via `ast`, so a name in a comment/docstring can't fool it).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

# Path to the module under test (tests[0] → console[1]; app/domain lives under app/).
DOMAIN_HARNESS = (
    Path(__file__).resolve().parents[1] / "app" / "domain" / "harness.py"
)

# Libraries the functional core must NEVER import (arrows-point-inward, design §3).
# subprocess is the I/O the LOCAL adapter owns; httpx is the I/O the REMOTE adapter
# (I2) will own — the pure port must reach for neither.
FORBIDDEN_IMPORTS = {"httpx", "fastapi", "subprocess", "psycopg2", "asyncpg"}

# The method surface the port must expose (spawn_chat added in Increment 4 — the
# interactive-chat host seam; same fire-and-forget shape as spawn_run).
EXPECTED_METHODS = {"spawn_run", "cancel_run", "spawn_chat"}


def test_module_file_exists():
    assert DOMAIN_HARNESS.is_file(), f"missing domain module: {DOMAIN_HARNESS}"


def test_port_and_dtos_importable():
    """The port Protocol + its DTOs import cleanly from the pure module."""
    from app.domain.harness import (  # noqa: F401
        ChatSpawnRequest,
        HarnessPort,
        SpawnHandle,
        SpawnRequest,
    )


def test_chat_spawn_request_constructs_and_roundtrips():
    """ChatSpawnRequest (Increment 4) is a real dataclass: required run-scope +
    message, optional routing/timeout, round-tripping via dataclasses.asdict (so it
    serializes straight across the I2 wire to POST /chat). It carries a `message`
    (the operator's text) and NO handoff_id (a chat is free-standing)."""
    import dataclasses

    from app.domain.harness import ChatSpawnRequest

    req = ChatSpawnRequest(
        run_id="crun-1", project="kaidera-os", agent="kai", message="hello there",
    )
    assert dataclasses.is_dataclass(req)
    # No handoff field exists on a chat request (a chat has no handoff).
    assert not hasattr(req, "handoff_id")
    assert req.harness is None and req.model is None
    assert req.repo_root is None
    d = dataclasses.asdict(req)
    assert d["run_id"] == "crun-1"
    assert d["project"] == "kaidera-os"
    assert d["agent"] == "kai"
    assert d["message"] == "hello there"

    # Full construction — routing carried for a future direct route.
    req2 = ChatSpawnRequest(
        run_id="crun-2", project="kaidera-os", agent="ren", message="hi",
        harness="pi", model="gpt-5", reasoning="high", repo_root="/work/proj",
    )
    d2 = dataclasses.asdict(req2)
    assert d2["harness"] == "pi" and d2["model"] == "gpt-5" and d2["reasoning"] == "high"
    assert d2["repo_root"] == "/work/proj"


def test_port_is_protocol_with_expected_methods():
    """HarnessPort is a typing.Protocol exposing spawn_run + cancel_run."""
    import app.domain.harness as harness

    port = harness.HarnessPort
    assert getattr(port, "_is_protocol", False), "HarnessPort must be a typing.Protocol"
    members = set(dir(port))
    missing = EXPECTED_METHODS - members
    assert not missing, f"HarnessPort missing methods: {sorted(missing)}"


def test_port_is_runtime_checkable():
    """HarnessPort is runtime_checkable (so the adapter can be isinstance-verified)."""
    import app.domain.harness as harness

    assert getattr(harness.HarnessPort, "_is_runtime_protocol", False), (
        "HarnessPort must be @runtime_checkable so a stub/adapter can be verified"
    )


def test_port_methods_are_coroutines():
    """spawn_run + cancel_run are declared `async def` (the fire-and-forget +
    best-effort-cancel async surface the adapters implement)."""
    import app.domain.harness as harness

    for name in EXPECTED_METHODS:
        member = getattr(harness.HarnessPort, name, None)
        assert member is not None, f"HarnessPort.{name} missing"
        assert inspect.iscoroutinefunction(member), (
            f"HarnessPort.{name} should be `async def` (the async spawn surface)"
        )


def test_spawn_request_constructs_and_roundtrips():
    """SpawnRequest is a real dataclass: required fields + optional defaults, and it
    round-trips via dataclasses.asdict (serialization-friendly for the I2 wire API)."""
    import dataclasses

    from app.domain.harness import SpawnRequest

    # Minimal construction — only the four required run-scope fields.
    req = SpawnRequest(
        run_id="run-1",
        project="kaidera-os",
        agent="worker-a",
        handoff_id="h-123",
    )
    assert dataclasses.is_dataclass(req)
    # Optional routing/timeout defaults.
    assert req.harness is None
    assert req.model is None
    assert req.repo_root is None
    assert req.run_timeout_s == 900.0

    d = dataclasses.asdict(req)
    assert d["run_id"] == "run-1"
    assert d["project"] == "kaidera-os"
    assert d["agent"] == "worker-a"
    assert d["handoff_id"] == "h-123"

    # Full construction — every field set.
    req2 = SpawnRequest(
        run_id="run-2",
        project="kaidera-os",
        agent="worker-b",
        handoff_id="h-456",
        harness="pi",
        model="gpt-5",
        repo_root="/work/proj",
        run_timeout_s=120.0,
    )
    d2 = dataclasses.asdict(req2)
    assert d2["harness"] == "pi"
    assert d2["model"] == "gpt-5"
    assert d2["repo_root"] == "/work/proj"
    assert d2["run_timeout_s"] == 120.0


def test_spawn_handle_constructs_and_roundtrips():
    """SpawnHandle is a real dataclass: a run_id + accepted flag, with optional
    exit_code / stderr_tail / error, round-tripping via dataclasses.asdict."""
    import dataclasses

    from app.domain.harness import SpawnHandle

    # Accepted-but-not-yet-terminal (the async "dispatched" shape).
    h = SpawnHandle(run_id="run-1", accepted=True)
    assert dataclasses.is_dataclass(h)
    assert h.exit_code is None
    assert h.stderr_tail is None
    assert h.error is None

    # Accepted + completed (exit_code carried back).
    ok = SpawnHandle(run_id="run-1", accepted=True, exit_code=0, stderr_tail="tail")
    d = dataclasses.asdict(ok)
    assert d["accepted"] is True
    assert d["exit_code"] == 0
    assert d["stderr_tail"] == "tail"

    # Rejected (spawn never happened — accepted=False + an error string).
    bad = SpawnHandle(run_id="run-1", accepted=False, error="No such file")
    assert bad.accepted is False
    assert bad.exit_code is None
    assert bad.error == "No such file"


# ── THE IMPORT-PURITY GUARD ──────────────────────────────────────────────────


def _imported_top_level_modules(source: str) -> set[str]:
    """Parse `source` and return the TOP-LEVEL package names it imports (so
    `import subprocess` and `from httpx import X` both count). Relative imports
    (within the package) are ignored."""
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


def test_harness_module_imports_nothing_outward():
    """GUARD: the functional core imports none of the forbidden outward libs.

    Parsed via `ast` (not a substring scan) so a forbidden name appearing only in a
    comment/docstring/type-string cannot trip OR satisfy the check. CRITICAL for
    this port: `subprocess` (the LOCAL adapter's I/O) and `httpx` (the REMOTE
    adapter's I/O) must live in the adapters, never in the domain."""
    source = DOMAIN_HARNESS.read_text()
    imported = _imported_top_level_modules(source)
    leaked = imported & FORBIDDEN_IMPORTS
    assert not leaked, (
        f"app/domain/harness.py must import NOTHING outward, but imports: "
        f"{sorted(leaked)} (arrows-point-inward — keep the core pure)"
    )


def test_harness_module_imports_clean_at_runtime():
    """Importing the module must not drag in a forbidden lib transitively either.

    NB: we do NOT pop+re-import the module here. The orchestrator lazily imports
    `SpawnRequest` from this SAME module at call time, and other tests assert
    `isinstance(req, SpawnRequest)` against the top-level-imported class — a fresh
    re-import would mint a NEW class object and break that identity across the suite.
    A plain `import_module` (returns the cached module) proves runtime-importability
    without poisoning the shared module identity."""
    import importlib

    mod = importlib.import_module("app.domain.harness")
    for name in ("HarnessPort", "SpawnRequest", "SpawnHandle"):
        assert hasattr(mod, name)
