"""AV-1 — purge baked "Cole" persona literals from console code.

`cortex.md` forbids baked worker/agent names in source: the orchestrator label
must come from project roles/config, never a hardcoded persona. Ren already did
the docs/UI half; this pins the CODE half so the literal cannot creep back into
the two files that carried it.

Two guards:

  * BEHAVIOURAL — `AgentsService.orchestrator_label` resolves the label from the
    ROSTER (the orchestrator-role agent's configured display name), and returns
    None when the project has no orchestrator — i.e. there is NO baked "Cole"
    fallback anywhere on the label path.
  * SOURCE — `app/main.py` + `app/orchestrator.py` carry no "Cole" persona
    literal (case-insensitive; the lowercase `cole-plan` tool reference was the
    only other survivor and is gone too).
"""

from __future__ import annotations

from pathlib import Path

from app.agents import service as service_mod
from app.agents.service import AgentsService

# The two files AV-1 purges. Resolved off the service module so the test does not
# import the full FastAPI app just to find a path.
_APP_DIR = Path(service_mod.__file__).resolve().parents[1]
_PURGED_FILES = (_APP_DIR / "main.py", _APP_DIR / "orchestrator.py")


def test_orchestrator_label_is_roster_driven_not_baked():
    """The label is whatever the orchestrator-role agent's display name is — a
    project could call it "Dispatcher", "Kaidera OS Orchestrator", or anything; the
    resolver returns THAT, never a source literal."""
    svc = AgentsService(store=None)
    roster = [
        {"name": "lead-x", "role": "lead", "capabilities": {"display_name": "Lead X"}},
        {
            "name": "orch",
            "role": "orchestrator",
            "capabilities": {"display_name": "Dispatcher"},
        },
    ]
    assert svc.orchestrator_label(roster, "anyproj", {}) == "Dispatcher"


def test_orchestrator_label_none_when_no_orchestrator():
    """No orchestrator-role agent → None (graceful drop of the attribution). There
    is NO baked persona fallback — a missing orchestrator must not surface "Cole"."""
    svc = AgentsService(store=None)
    roster = [
        {"name": "lead-x", "role": "lead", "capabilities": {"display_name": "Lead X"}},
    ]
    label = svc.orchestrator_label(roster, "anyproj", {})
    assert label is None
    assert label != "Cole"


def test_label_path_source_has_no_cole_persona_literal():
    """The two purged files carry no "Cole"/"cole" persona literal (cortex.md:
    no baked worker names in source)."""
    for path in _PURGED_FILES:
        text = path.read_text(encoding="utf-8")
        assert "cole" not in text.lower(), f"persona literal 'cole' leaked back into {path.name}"
