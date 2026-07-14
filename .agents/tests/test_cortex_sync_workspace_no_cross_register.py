"""Regression tests for cortex-sync-workspace — the FORWARD ingest tool.

Root cause this guards against (cross-project agent-roster contamination):

  cortex-sync-workspace used to emit a raw-SQL
      INSERT INTO agents (name, role, model, project, capabilities)
      ... ON CONFLICT (name, project) DO UPDATE ...
  for EVERY `*_IDENTITY.md` matched by a project's profile_globs. That bypassed
  the Cortex HTTP API caller/scope guard, so when one project's workspace.json
  listed ANOTHER project's identity file under its globs, the agent was silently
  cross-registered into the wrong project's `agents` roster on every sync (the
  `ren@kaidera` / `saul@kaidera` bleed).

The fix: roster registration is now explicit + guarded (cortex-add-agent ->
POST /agents). The sync still writes `agent_profiles` (identity/role/boot data),
which is all cortex-boot / GET /boot identity resolution and
cortex-maintain-agents keep_visible promotion need — but it must NOT emit any
`agents`-table INSERT.

These tests drive ONLY the embedded SQL-generation Python (extracted from the
bash wrapper) against temp config + temp identity files, and assert on the
generated SQL. No DB, no network, no home-dir session scan.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = ROOT / ".agents" / "scripts" / "cortex-sync-workspace"


def _extract_first_pyeof_block() -> str:
    """Pull the first embedded `<<'PYEOF' ... PYEOF` Python block (the SQL planner)."""
    src = SYNC_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", src, re.DOTALL)
    assert m, "could not locate the first PYEOF python block in cortex-sync-workspace"
    return m.group(1)


def _run_planner(config: dict, tmp_path: Path) -> str:
    """Write `config` + run the extracted planner; return the generated SQL text.

    HOME is redirected to an empty dir so the planner's session scan finds nothing
    (it walks ~/.claude/projects and ~/.codex/sessions). Only the SQL file matters.
    """
    block = _extract_first_pyeof_block()
    planner_py = tmp_path / "_planner.py"
    planner_py.write_text(block, encoding="utf-8")

    config_file = tmp_path / "workspace.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")

    sql_file = tmp_path / "out.sql"
    plan_file = tmp_path / "out.plan"
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir(exist_ok=True)

    env = dict(os.environ, HOME=str(empty_home))
    proc = subprocess.run(
        [sys.executable, str(planner_py), str(config_file), str(sql_file), str(plan_file), "0"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"planner failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return sql_file.read_text(encoding="utf-8")


def _write_identity(dir_path: Path, agent_upper: str, role: str = "") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    fm = "---\n"
    fm += f"name: {agent_upper.lower()}\n"
    if role:
        fm += f"role: {role}\n"
    fm += "---\n"
    body = fm + f"# {agent_upper}\n\nIdentity for {agent_upper.lower()}.\n"
    f = dir_path / f"{agent_upper}_IDENTITY.md"
    f.write_text(body, encoding="utf-8")
    return f


def _make_config(project_key: str, root: Path) -> dict:
    return {
        "registry_mode": "partial",
        "projects": [
            {
                "key": project_key,
                "display_name": project_key,
                "default_agent": "kai",
                "profile_globs": ["agents/*_IDENTITY.md"],
                "roots": [{"path": str(root), "kind": "primary"}],
            }
        ],
    }


# ---------------------------------------------------------------------------
# The regression: NO agents-table INSERT is ever emitted.
# ---------------------------------------------------------------------------


def test_identity_file_does_not_emit_agents_insert(tmp_path):
    """An identity file must NOT mint an `agents`-table roster row."""
    proj_root = tmp_path / "proj"
    _write_identity(proj_root / "agents", "KAI", role="pm")

    sql = _run_planner(_make_config("kaidera-os", proj_root), tmp_path)

    assert "INSERT INTO agents " not in sql, (
        "cortex-sync-workspace must not emit a raw-SQL agents-table insert for "
        "identity files (that is the cross-project contamination vector)."
    )
    assert "INSERT INTO agents(" not in sql


def test_identity_file_still_emits_agent_profiles(tmp_path):
    """The identity/role/boot write (agent_profiles) MUST stay — boot depends on it."""
    proj_root = tmp_path / "proj"
    _write_identity(proj_root / "agents", "KAI", role="pm")

    sql = _run_planner(_make_config("kaidera-os", proj_root), tmp_path)

    assert "INSERT INTO agent_profiles" in sql, (
        "agent_profiles write must remain — cortex-boot / GET /boot identity + "
        "role resolution read it directly."
    )
    # The agent name + project must appear in the profile write.
    assert "'kai'" in sql
    assert "'kaidera-os'" in sql


def test_cross_project_identity_file_does_not_cross_register(tmp_path):
    """The exact contamination scenario: project P syncs an identity file that
    belongs to a DIFFERENT agent/project. No agents-table row may be minted for
    P, so the foreign agent can never bleed into P's roster via sync.

    This mirrors Kaidera AI's workspace.json sweeping in a kaidera-os agent's
    identity file (REN_IDENTITY.md) under the `kaidera` project's globs.
    """
    kaidera_root = tmp_path / "kaidera"
    # A kaidera-os agent's identity file physically present under kaidera's tree.
    _write_identity(kaidera_root / "agents", "REN", role="full-stack-senior-developer")

    sql = _run_planner(_make_config("kaidera", kaidera_root), tmp_path)

    # No roster row for ren@kaidera (or anyone) from the sync.
    assert "INSERT INTO agents " not in sql
    # ren's profile is still ingested (identity/role context) — that is fine and
    # does not place ren on kaidera's live roster (the roster reads `agents`).
    assert "INSERT INTO agent_profiles" in sql


def test_no_agents_insert_across_multiple_projects(tmp_path):
    """A multi-project workspace (the Kaidera AI shape) emits zero agents-table inserts."""
    eng_root = tmp_path / "kaidera"
    mkt_root = tmp_path / "marketing"
    _write_identity(eng_root / "agents", "REN", role="full-stack-senior-developer")
    _write_identity(eng_root / "agents", "ALPHA", role="cpo")
    _write_identity(mkt_root / "agents", "SAUL", role="creative-director")

    config = {
        "registry_mode": "partial",
        "projects": [
            {
                "key": "kaidera",
                "display_name": "kaidera",
                "default_agent": "alpha",
                "profile_globs": ["agents/*_IDENTITY.md"],
                "roots": [{"path": str(eng_root), "kind": "primary"}],
            },
            {
                "key": "marketing",
                "display_name": "marketing",
                "default_agent": "saul",
                "profile_globs": ["agents/*_IDENTITY.md"],
                "roots": [{"path": str(mkt_root), "kind": "primary"}],
            },
        ],
    }

    sql = _run_planner(config, tmp_path)

    assert sql.count("INSERT INTO agents ") == 0
    # Every identity still produces a profile write.
    assert sql.count("INSERT INTO agent_profiles") == 3
