"""DE-FORK equivalence guard — the pure config helpers have exactly ONE owner.

Before this carve the pure helpers `normalize_designation` / `override_store_key`
(`_override_store_key`) / `clean_override` (`_clean_override`) were COPY-PASTED in
three modules:
  * `app.settings` (the legacy facade),
  * `app.settings_module.service` (the carved config module — annotated
    "Lifted 1:1 from settings.py" ~11 times),
  * `app.agents.service` (a third copy of `_override_store_key` + `_normalize_designation`).
Three stores of truth silently diverge. They now ALL delegate to the single owner
`app.domain.designation`. These tests PIN that: each site returns IDENTICAL results
to the owner (and to each other) for a battery of fixed inputs, so a future edit to
one copy that re-introduces a fork makes this test RED.

(`app.domain` is the de-fork owner — not `app.settings_module` — because the
import-linter `modules-are-independent` contract forbids `app.settings` /
`app.agents` from depending on `app.settings_module`, and `app.domain` is the one
inward target every module may import. The names still resolve through
`settings_module.service`, so its public surface is unchanged.)
"""

from __future__ import annotations

import ast
from pathlib import Path

from app import settings as settings_facade
from app.agents import service as agents_service
from app.domain import designation as domain_designation
from app.settings_module import service as settings_service

# ---------------------------------------------------------------------------
#  Fixed input batteries — a couple of representative cases per helper, covering
#  the tolerant edges (None, blanks, casing, whitespace, unknown values, junk).
# ---------------------------------------------------------------------------

_DESIGNATION_INPUTS = [
    "interactive",
    "Autonomous",
    "  INTERACTIVE  ",
    "boss",          # unknown → ""
    "",              # blank → ""
    None,            # None → ""
    123,             # non-str → ""
]

_STORE_KEY_INPUTS = [
    ("kaidera-os", "Ren"),
    (None, "x"),
    ("  P  ", "  A  "),
    ("", ""),
    ("Kaidera OS", "KAI"),
]

_OVERRIDE_INPUTS = [
    {"harness": "claude-code", "model": "opus", "designation": "Interactive", "role": "CPO"},
    {"designation": "nonsense", "role": "  lead  ", "junk": "drop-me"},
    {"harness": "  ", "model": None, "reasoning": "high"},
    {},
    "not-a-dict",   # non-dict → {}
    None,           # None → {}
]


def test_normalize_designation_identical_across_sites():
    """`normalize_designation` is identical at the owner + every delegating site."""
    for raw in _DESIGNATION_INPUTS:
        expected = domain_designation.normalize_designation(raw)
        assert settings_facade.normalize_designation(raw) == expected, raw
        assert settings_service.normalize_designation(raw) == expected, raw
        assert agents_service._normalize_designation(raw) == expected, raw


def test_override_store_key_identical_across_sites():
    """`override_store_key` is identical at the owner + every delegating site
    (the facade + agents expose it as the private `_override_store_key`)."""
    for project, agent in _STORE_KEY_INPUTS:
        expected = domain_designation.override_store_key(project, agent)
        assert settings_facade._override_store_key(project, agent) == expected, (project, agent)
        assert settings_service.override_store_key(project, agent) == expected, (project, agent)
        assert agents_service._override_store_key(project, agent) == expected, (project, agent)


def test_clean_override_identical_across_sites():
    """`clean_override` is identical at the owner + every delegating site."""
    for raw in _OVERRIDE_INPUTS:
        expected = domain_designation.clean_override(raw)
        assert settings_facade._clean_override(raw) == expected, raw
        assert settings_service.clean_override(raw) == expected, raw


def test_value_constants_are_the_same_object_set():
    """The designation values + the override-field tuple are single-sourced from the
    domain — every site exposes the SAME values (no drift possible)."""
    assert settings_facade.DESIGNATION_INTERACTIVE == domain_designation.DESIGNATION_INTERACTIVE
    assert settings_facade.DESIGNATION_AUTONOMOUS == domain_designation.DESIGNATION_AUTONOMOUS
    assert settings_facade.DESIGNATIONS == domain_designation.DESIGNATIONS
    assert settings_facade.AGENT_OVERRIDE_FIELDS == domain_designation.AGENT_OVERRIDE_FIELDS

    assert settings_service.DESIGNATION_INTERACTIVE == domain_designation.DESIGNATION_INTERACTIVE
    assert settings_service.DESIGNATION_AUTONOMOUS == domain_designation.DESIGNATION_AUTONOMOUS
    assert settings_service.DESIGNATIONS == domain_designation.DESIGNATIONS
    assert settings_service.AGENT_OVERRIDE_FIELDS == domain_designation.AGENT_OVERRIDE_FIELDS

    assert agents_service.DESIGNATION_INTERACTIVE == domain_designation.DESIGNATION_INTERACTIVE
    assert agents_service.DESIGNATION_AUTONOMOUS == domain_designation.DESIGNATION_AUTONOMOUS


def test_domain_designation_imports_nothing_outward():
    """PURITY GUARD: `app/domain/designation.py` (the de-fork owner) imports NOTHING
    outward — stdlib only, no reach into `app.*`. Parsed via `ast` (a name in a
    comment/docstring can't fool it), mirroring `test_ports_purity.py`. This is what
    lets the legacy facade + the agents module delegate INWARD to it without breaking
    the import-linter independence contract."""
    src = (
        Path(__file__).resolve().parents[1] / "app" / "domain" / "designation.py"
    ).read_text()
    tree = ast.parse(src)
    top: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top.add(node.module.split(".")[0])
    forbidden = {"httpx", "fastapi", "starlette", "subprocess", "psycopg2", "asyncpg"}
    assert not (top & forbidden), (
        f"domain/designation.py must not import outward I/O libs, got: {sorted(top & forbidden)}"
    )
    # The pure functional core must not reach into the outer app layers either.
    assert "app" not in top, (
        f"domain/designation.py must not import app.* (it is the inward core), got app import"
    )
