"""Chat file-attachments (feature-gap step 6, Inc A) — the pure DTO
(`app/domain/attachments.py`).

`AttachmentMeta` is the value object describing ONE received chat attachment: the
minted id, the run it belongs to, the (sanitized) filename, the byte size, the
content-type, the HOST path the bytes landed at, and a created-at stamp. It sits
alongside the other domain DTOs (`SpawnRequest`/`ChatSpawnRequest` in
`app/domain/harness.py`) and obeys the SAME house law: it is PURE — it must import
NOTHING outward (no httpx / fastapi / subprocess / psycopg2 / asyncpg / pathlib-I/O).

These tests assert (mirroring `test_ports_purity.py`):
  * the DTO is importable, constructs, and carries the documented fields,
  * it round-trips through `dataclasses.asdict` (so a route can serialize it), and
  * the IMPORT-PURITY GUARD: the module's source imports none of the outward
    libraries (parsed via `ast`, so a name in a comment/docstring can't fool it).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

from app.domain.attachments import AttachmentMeta

# Path to the module under test (tests[0] → console[1]; app/domain lives under app/).
DOMAIN_ATTACH = Path(__file__).resolve().parents[1] / "app" / "domain" / "attachments.py"

# Libraries the functional core must NEVER import (arrows-point-inward, design §3).
FORBIDDEN_IMPORTS = {"httpx", "fastapi", "subprocess", "psycopg2", "asyncpg"}


def test_attachment_meta_constructs_with_documented_fields():
    meta = AttachmentMeta(
        attachment_id="att-1",
        run_id="run-1",
        filename="notes.txt",
        size_bytes=42,
        content_type="text/plain",
        host_path="/host/attachments/run-1/notes.txt",
        created_at=1234.5,
    )
    assert meta.attachment_id == "att-1"
    assert meta.run_id == "run-1"
    assert meta.filename == "notes.txt"
    assert meta.size_bytes == 42
    assert meta.content_type == "text/plain"
    assert meta.host_path == "/host/attachments/run-1/notes.txt"
    assert meta.created_at == 1234.5


def test_attachment_meta_round_trips_through_asdict():
    meta = AttachmentMeta(
        attachment_id="att-2",
        run_id="run-2",
        filename="a.json",
        size_bytes=7,
        content_type="application/json",
        host_path="/host/a.json",
        created_at=1.0,
    )
    d = dataclasses.asdict(meta)
    assert d["attachment_id"] == "att-2"
    assert d["filename"] == "a.json"
    # Reconstruct from the dict (proves it's a plain serializable dataclass).
    assert AttachmentMeta(**d) == meta


def test_domain_attachments_is_pure_no_outward_imports():
    """AST-parse the module source and assert it imports none of the forbidden
    outward libraries. A name in a comment/docstring cannot fool this (we walk the
    import nodes only)."""
    src = DOMAIN_ATTACH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    leaked = imported & FORBIDDEN_IMPORTS
    assert not leaked, f"app.domain.attachments must not import {leaked}"


def test_domain_attachments_module_has_no_io_calls_in_source():
    """Belt-and-braces: the pure DTO module defines only the dataclass — there should
    be no `open(`/`Path(` filesystem call in its source body."""
    src = inspect.getsource(__import__("app.domain.attachments", fromlist=["x"]))
    assert "open(" not in src
