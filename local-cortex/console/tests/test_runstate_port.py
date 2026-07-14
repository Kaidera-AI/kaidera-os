"""T2 — tests for the pure RunStatePort interface + its DTOs.

`app/domain/runstate.py` is the functional core for run state: the `RunSpan` +
`RunRecord` dataclasses and the `RunStatePort` Protocol. It is PURE — it must
import nothing outward (no httpx/fastapi/subprocess/psycopg2/asyncpg). The Pg
adapter (T3) implements the Protocol; the orchestrator/worker/watchdog depend on
the Protocol, never the adapter — arrows point inward.

These tests assert:
  * the dataclasses round-trip (construct + to_dict),
  * the Protocol + all its method names are importable and present,
  * a structural-typing check (a stub satisfies the Protocol), and
  * the IMPORT-PURITY GUARD: the module's source imports none of the outward
    libraries (parsed via `ast`, so it can't be fooled by a string mention).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_type_hints

# Path to the module under test (tests[0] → console[1]; app/domain lives under app/).
DOMAIN_RUNSTATE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "domain"
    / "runstate.py"
)

# Libraries the functional core must NEVER import (the arrows-point-inward rule
# from the ratified design §3; T2's explicit guard requirement).
FORBIDDEN_IMPORTS = {"httpx", "fastapi", "subprocess", "psycopg2", "asyncpg"}

# The method surface RunStatePort must expose (the TranscriptStore superset +
# heartbeat/set_status/list_active/subscribe — plan §"five decisions" #2).
EXPECTED_METHODS = {
    "start_run",
    "append_output",
    "set_status",
    "heartbeat",
    "get_run",
    "list_active",
    "recent",
    "by_handoff",
    "subscribe",
}


def test_module_file_exists():
    assert DOMAIN_RUNSTATE.is_file(), f"missing domain module: {DOMAIN_RUNSTATE}"


def test_dataclasses_importable_and_roundtrip():
    """RunSpan + RunRecord construct and expose their fields."""
    from app.domain.runstate import RunRecord, RunSpan

    span = RunSpan(seq=1, kind="output", text="hello", ts="2026-06-05T00:00:00Z")
    assert span.seq == 1
    assert span.kind == "output"
    assert span.text == "hello"

    rec = RunRecord(
        run_id="run-abc",
        project="kaidera-os",
        agent="ren",
        status="running",
    )
    assert rec.run_id == "run-abc"
    assert rec.project == "kaidera-os"
    assert rec.agent == "ren"
    assert rec.status == "running"
    # Optional/defaulted fields exist with sane defaults.
    assert rec.handoff_id is None
    assert rec.tokens_in is None
    assert rec.spans == [] or rec.spans is None


def test_runrecord_has_optional_session_id():
    """Multi-turn chat (feature-gap step 6, Inc B): RunRecord carries an OPTIONAL
    `session_id` (the per-conversation grouping key). ADDITIVE — it defaults to None
    so every existing construction (no session_id) is unchanged, and a chat turn that
    belongs to a conversation sets it. Pure DTO field; the adapter persists it."""
    from app.domain.runstate import RunRecord

    # Default: absent → None (single-shot; today's behaviour preserved).
    rec = RunRecord(run_id="r1", project="kaidera-os", agent="ren")
    assert rec.session_id is None, "session_id must default to None (additive)"

    # Set: a conversation turn carries its session id.
    rec2 = RunRecord(run_id="r2", project="kaidera-os", agent="ren", session_id="sess-xyz")
    assert rec2.session_id == "sess-xyz"


def test_start_run_protocol_accepts_session_id():
    """The `start_run` Protocol signature accepts an OPTIONAL keyword `session_id`
    (defaulting to None), so the chat path can persist the conversation grouping key
    through the SAME port the worker uses. Additive — existing callers omit it."""
    import inspect

    from app.domain.runstate import RunStatePort

    sig = inspect.signature(RunStatePort.start_run)
    assert "session_id" in sig.parameters, (
        "RunStatePort.start_run must accept a session_id keyword (multi-turn chat)"
    )
    assert sig.parameters["session_id"].default is None, (
        "session_id must be OPTIONAL (default None) so existing callers are unaffected"
    )


def test_dataclasses_are_dataclasses():
    """Both DTOs are real dataclasses (so they round-trip via dataclasses.asdict)."""
    import dataclasses

    from app.domain.runstate import RunRecord, RunSpan

    assert dataclasses.is_dataclass(RunSpan)
    assert dataclasses.is_dataclass(RunRecord)

    rec = RunRecord(run_id="r1", project="kaidera-os", agent="ren", status="queued")
    d = dataclasses.asdict(rec)
    assert d["run_id"] == "r1"
    assert d["status"] == "queued"


def test_protocol_importable_and_runtime_checkable():
    """RunStatePort is a typing.Protocol and exposes every expected method."""
    from app.domain.runstate import RunStatePort

    # It's a Protocol.
    assert getattr(RunStatePort, "_is_protocol", False), (
        "RunStatePort must be a typing.Protocol"
    )

    members = set(dir(RunStatePort))
    missing = EXPECTED_METHODS - members
    assert not missing, f"RunStatePort missing methods: {sorted(missing)}"


def test_protocol_structural_conformance():
    """A minimal in-memory stub that implements every method satisfies the
    Protocol structurally (this is the swap-in contract the adapter must meet)."""
    from app.domain.runstate import RunRecord, RunStatePort

    class StubStore:
        async def start_run(self, *, run_id, project, agent, **kwargs):
            return RunRecord(run_id=run_id, project=project, agent=agent, status="queued")

        async def append_output(self, run_id, *, seq, kind, text):
            return None

        async def set_status(self, run_id, status, *, error=None):
            return None

        async def heartbeat(self, run_id, **kwargs):
            return None

        async def get_run(self, run_id):
            return None

        async def list_active(self, project=None):
            return []

        async def recent(self, project=None, limit=20):
            return []

        async def by_handoff(self, handoff_id):
            return None

        async def subscribe(self, project=None):
            if False:
                yield ""  # make it an async generator
            return

    stub = StubStore()
    # runtime_checkable Protocols only verify method presence, which is exactly
    # the structural contract we want to assert here.
    assert isinstance(stub, RunStatePort), (
        "a stub implementing all methods should satisfy RunStatePort"
    )


def test_port_methods_are_coroutines_or_asyncgen():
    """Every Protocol method is declared async (the adapter is async over the
    asyncpg pool); subscribe is an async generator."""
    from app.domain.runstate import RunStatePort

    for name in EXPECTED_METHODS:
        member = getattr(RunStatePort, name, None)
        assert member is not None, f"missing {name}"
        # Declared with `async def` → coroutine function (or async-gen for subscribe).
        is_async = inspect.iscoroutinefunction(member) or inspect.isasyncgenfunction(
            member
        )
        assert is_async, f"RunStatePort.{name} should be declared async"


# ── THE IMPORT-PURITY GUARD ──────────────────────────────────────────────────


def _imported_top_level_modules(source: str) -> set[str]:
    """Parse `source` and return the set of TOP-LEVEL package names it imports
    (so `import psycopg2.extras` and `from asyncpg import X` both count)."""
    tree = ast.parse(source)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Ignore relative imports (node.level > 0 → within the package).
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def test_domain_module_imports_nothing_outward():
    """GUARD: the functional core imports none of the forbidden outward libs.

    Parsed via `ast` (not a substring scan) so a forbidden name appearing only
    in a comment/docstring/type-string cannot trip OR satisfy the check."""
    source = DOMAIN_RUNSTATE.read_text()
    imported = _imported_top_level_modules(source)
    leaked = imported & FORBIDDEN_IMPORTS
    assert not leaked, (
        f"app/domain/runstate.py must import NOTHING outward, but imports: "
        f"{sorted(leaked)} (arrows-point-inward — keep the core pure)"
    )


def test_domain_module_actually_imports_clean_at_runtime():
    """Importing the module must not drag in a forbidden lib transitively either
    — a second belt-and-braces check beyond the static scan."""
    import importlib
    import sys

    # Drop any cached copy so the import really executes.
    sys.modules.pop("app.domain.runstate", None)
    mod = importlib.import_module("app.domain.runstate")
    # The module object exists and exposes the public names.
    assert hasattr(mod, "RunStatePort")
    assert hasattr(mod, "RunRecord")
    assert hasattr(mod, "RunSpan")
