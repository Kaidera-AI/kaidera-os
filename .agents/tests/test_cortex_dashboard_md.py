from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


def load_dashboard_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "cortex_dashboard_md.py"
    loader = importlib.machinery.SourceFileLoader("cortex_dashboard_md_under_test", str(module_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_default_project_comes_from_workspace_without_env(tmp_path, monkeypatch):
    config_dir = tmp_path / ".agents" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "workspace.json").write_text(
        """
{
  "program": {"key": "marketing"},
  "projects": [
    {"key": "marketing", "display_name": "Marketing", "roots": [{"path": ".", "kind": "primary"}]}
  ]
}
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("CORTEX_PROJECT", raising=False)
    monkeypatch.setenv("CORTEX_WORKSPACE_ROOT", str(tmp_path))

    dash = load_dashboard_module()

    assert dash.PROJECT == "marketing"


def test_display_cleanup_and_truncation():
    dash = load_dashboard_module()

    assert dash._clean_epic_title("E67_PROMI_RUNTIME") == "Beat Runtime"
    assert dash._clean_display_text("PROMI asked about CPO") == "Beat asked about CPO"
    assert (
        dash._clean_display_text("E84 CLOSED. E75 promoted to active")
        == "E84 marked_for_closeout. E75 opened"
    )
    assert dash._clean_display_text("status=active (promoted 2026-05-10") == "status=opened 2026-05-10"
    assert dash._truncate_status("**RATIFIED 2026-05-08** - ready for CPO review", 24) == "RATIFIED 2026-05-08..."


def test_rag_badge_and_progress_bar():
    dash = load_dashboard_module()

    assert dash.rag_badge({"fresh": True, "stale": False}, 0, 0) == ("🟢", "GREEN")
    assert dash.rag_badge({"fresh": True, "stale": False}, 1, 0) == ("🟡", "AMBER")
    assert dash.rag_badge({"fresh": False, "stale": True}, 0, 0) == ("🔴", "RED")
    assert dash.progress_bar(50, width=10) == "█████░░░░░"
    assert dash.progress_bar(150, width=4) == "████"


def test_schema_mismatch_degrades_gracefully(monkeypatch):
    dash = load_dashboard_module()

    def fake_sql(_sql: str):
        return [["not-a-known-status", "bad-int"], ["pending"], []]

    monkeypatch.setattr(dash, "cortex_sql", fake_sql)

    counts = dash.handoff_counts()
    assert counts["pending"] == 0
    assert counts["stale"] == 0
    assert dash.open_handoffs() == []
    assert dash.recent_decisions() == []
    assert dash.agent_activity() == []


def test_opened_epic_counts_as_active_lane():
    dash = load_dashboard_module()

    assert dash._is_active_epic({"status_raw": "opened 2026-05-10 via handoff"}) is True
    assert dash._is_active_epic({"status_raw": "marked_for_closeout 2026-05-10"}) is False
    assert dash._is_active_epic({"status_raw": "closed 2026-05-09"}) is False


def test_render_all_with_fixture_api_and_filesystem(tmp_path, monkeypatch):
    dash = load_dashboard_module()
    workspace = tmp_path
    dashboards = workspace / ".cortex" / "dashboards"
    epic_dir = workspace / "Program" / "Release_v0.1.0" / "E75_LOCAL_CORTEX_MODERNISATION"
    epic_dir.mkdir(parents=True)
    (epic_dir / "EPIC_SPEC.md").write_text(
        "| **Status** | active - PROMI cleanup by Alpha |\n"
        "| **Owner agent** | Alpha (CPO) |\n",
        encoding="utf-8",
    )

    class FakeViz:
        @staticmethod
        def compute_epic_progress(_epic_dir):
            return {
                "epic_id": "E75",
                "name": "E75_LOCAL_CORTEX_MODERNISATION",
                "implemented": 1,
                "spec_drafted": 2,
                "total": 2,
                "percent_implemented": 50,
                "percent_spec": 100,
                "increments": [
                    {"slug": "01-closeout-review-checklist", "status": "draft", "owner": "Alpha"},
                    {"slug": "02-api-only-memory-redist-closeout", "status": "implemented_with_residuals", "owner": "Ren"},
                ],
            }

        @staticmethod
        def _read_field(text, field):
            if field == "Status":
                return "active - PROMI cleanup by Alpha"
            if field in {"Owner agent", "Owner"}:
                return "Alpha (CPO)"
            return ""

    def fake_sql(sql: str):
        if "GROUP BY status" in sql:
            return [["pending", 1], ["claimed", 1]]
        if "created_at <" in sql or "summary LIKE" in sql:
            return [[0]]
        if "lower(to_role)='cto'" in sql:
            return [["urgent", "CTO decision needed for dashboard", "2026-05-10T01:02:00+00:00"]]
        if "lower(to_role)='cpo'" in sql:
            return [["high", "beat:80dd", "CONSULT for Alpha review"]]
        if "h.claimed_at <" in sql:
            return []
        if "FROM handoffs" in sql:
            return [["abc12345", "pending", "high", "cpo", None, "PROMI handoff for Alpha", "2026-05-10 01:00"]]
        if "GROUP BY agent_name" in sql:
            return [["rex:80dd", 2]]
        if "FROM decisions" in sql:
            return [["2026-05-10 01:01", "alpha:80dd", "PROMI status sent to CPO"]]
        return []

    monkeypatch.setattr(dash, "WORKSPACE", workspace)
    monkeypatch.setattr(dash, "DASHBOARDS", dashboards)
    monkeypatch.setattr(dash, "viz", FakeViz)
    monkeypatch.setattr(dash, "cortex_sql", fake_sql)
    monkeypatch.setattr(dash, "beat_heartbeat", lambda: {"age_seconds": 12, "fresh": True, "stale": False, "last_lines": []})

    result = dash.render_all()
    assert result["files"]["dashboard.md"] == "updated"
    assert result["files"]["00-overview.md"] == "updated"

    dashboard = (dashboards / "dashboard.md").read_text(encoding="utf-8")
    overview = (dashboards / "00-overview.md").read_text(encoding="utf-8")
    active = (dashboards / "01-active-epic.md").read_text(encoding="utf-8")
    queue = (dashboards / "02-handoff-queue.md").read_text(encoding="utf-8")
    recent = (dashboards / "03-recent-activity.md").read_text(encoding="utf-8")

    combined = "\n".join([dashboard, overview, active, queue, recent])
    assert "E75" in overview
    assert "# 🟢 Cortex Dashboard - GREEN" in dashboard
    assert "01-closeout-review-checklist" in dashboard
    assert "Beat Runtime" not in overview
    assert "PROMI" not in combined
    assert "What's coming next" in overview
    assert overview.index("## Status at a glance") < overview.index("## What's coming next") < overview.index("## Active Epics")
    assert "Active consults awaiting review" in overview
    assert "Beat handoff for Alpha" in queue


def test_generic_markdown_provider_renders_without_kaidera_program(tmp_path, monkeypatch):
    dash = load_dashboard_module()
    workspace = tmp_path
    dashboards = workspace / ".cortex" / "dashboards"
    config_dir = workspace / ".agents" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "workspace.json").write_text(
        """
{
  "program": {"key": "marketing"},
  "projects": [
    {
      "key": "marketing",
      "display_name": "Marketing",
      "beat": {
        "orchestrator_agent": "beat",
        "progress_provider": "markdown-file",
        "progress_file": "STATUS.md"
      },
      "roots": [{"path": ".", "kind": "primary"}]
    }
  ]
}
""",
        encoding="utf-8",
    )
    (workspace / "STATUS.md").write_text(
        "| Epic | Progress | Status | Owner |\n"
        "|---|---|---|---|\n"
        "| Launch Workstream | 40% | active | Saul |\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CORTEX_PROJECT", "marketing")
    monkeypatch.setattr(dash, "WORKSPACE", workspace)
    monkeypatch.setattr(dash, "DASHBOARDS", dashboards)
    monkeypatch.setattr(dash, "cortex_sql", lambda _sql: [])
    monkeypatch.setattr(dash, "beat_heartbeat", lambda: {"age_seconds": 20, "fresh": True, "stale": False, "last_lines": []})

    result = dash.render_all()

    assert result["files"]["dashboard.md"] == "updated"
    dashboard = (dashboards / "dashboard.md").read_text(encoding="utf-8")
    overview = (dashboards / "00-overview.md").read_text(encoding="utf-8")
    assert "generic Markdown provider" in overview
    assert "Launch Workstream" in dashboard
    assert ("Program/" + "Kaidera") not in "\n".join([dashboard, overview])


def test_release_provider_uses_current_release_epic_files_not_stale_lane_filter(tmp_path, monkeypatch):
    dash = load_dashboard_module()
    workspace = tmp_path
    program = workspace / "Program" / "Release_v0.1.0"
    dashboards = workspace / ".cortex" / "dashboards"
    config_dir = workspace / ".agents" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "workspace.json").write_text(
        """
{
  "program": {"key": "kaidera-os"},
  "projects": [
    {
      "key": "kaidera-os",
      "beat": {
        "progress_provider": "release-program"
      },
      "roots": [{"path": ".", "kind": "primary"}]
    }
  ]
}
""",
        encoding="utf-8",
    )
    for epic, status in [
        ("E75_LOCAL_CORTEX_MODERNISATION", "opened"),
        ("E91_LOCAL_CORTEX_ARCHITECTURE_SIMPLIFICATION", "SUPERSEDED - folded into E75"),
        ("E87_PLATFORM_CORTEX_UPGRADE", "active"),
    ]:
        epic_dir = program / epic
        epic_dir.mkdir(parents=True)
        (epic_dir / "EPIC_SPEC.md").write_text(f"| **Status** | {status} |\n", encoding="utf-8")
    e59 = program / "E59_BILLING_AND_PAYMENTS_GO_LIVE"
    e59.mkdir(parents=True)
    (e59 / "E59_PLAN.md").write_text(
        "**Status:** Active top-priority epic\n\n"
        "## Immediate First Slice\n\n"
        "1. approve and publish the deployment-first plan\n",
        encoding="utf-8",
    )
    e92 = program / "E92_PLATFORM_CORTEX_HARDENING_FROM_LOCAL_LESSONS"
    e92.mkdir(parents=True)
    (e92 / "PROGRESS.md").write_text(
        "| **Status** | research_track 2026-05-16 |\n\n"
        "## Next actions\n\n"
        "1. Alpha reviews staged catalog\n",
        encoding="utf-8",
    )

    class FakeViz:
        @staticmethod
        def compute_epic_progress(epic_dir):
            if epic_dir.name.startswith("E59"):
                raise FileNotFoundError("legacy plan-only epic")
            return {
                "epic_id": epic_dir.name.split("_", 1)[0],
                "name": epic_dir.name,
                "implemented": 1,
                "spec_drafted": 1,
                "total": 1,
                "percent_implemented": 100,
                "percent_spec": 100,
                "increments": [{"slug": "01-proof", "status": "implemented", "owner": "Kai"}],
            }

        @staticmethod
        def _read_field(text, field):
            return dash._read_field_from_text(text, field)

    monkeypatch.setenv("CORTEX_PROJECT", "kaidera-os")
    monkeypatch.setattr(dash, "WORKSPACE", workspace)
    monkeypatch.setattr(dash, "DASHBOARDS", dashboards)
    monkeypatch.setattr(dash, "viz", FakeViz)
    monkeypatch.setattr(dash, "cortex_sql", lambda _sql: [])
    monkeypatch.setattr(dash, "beat_heartbeat", lambda: {"age_seconds": 20, "fresh": True, "stale": False, "last_lines": []})

    dash.render_all()

    combined = "\n".join(
        [
            (dashboards / "dashboard.md").read_text(encoding="utf-8"),
            (dashboards / "00-overview.md").read_text(encoding="utf-8"),
        ]
    )
    assert "E75" in combined
    assert "E91" in combined
    assert "E87" in combined
    assert "E59" in combined
    assert "E92" in combined
    assert "No active Epics" not in combined


def test_no_provider_fallback_still_writes_useful_dashboards(tmp_path, monkeypatch):
    dash = load_dashboard_module()
    workspace = tmp_path
    dashboards = workspace / ".cortex" / "dashboards"
    config_dir = workspace / ".agents" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "workspace.json").write_text(
        """
{
  "program": {"key": "marketing"},
  "projects": [
    {
      "key": "marketing",
      "display_name": "Marketing",
      "beat": {"progress_provider": "none"},
      "roots": [{"path": ".", "kind": "primary"}]
    }
  ]
}
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("CORTEX_PROJECT", "marketing")
    monkeypatch.setattr(dash, "WORKSPACE", workspace)
    monkeypatch.setattr(dash, "DASHBOARDS", dashboards)
    monkeypatch.setattr(dash, "cortex_sql", lambda _sql: [])
    monkeypatch.setattr(dash, "beat_heartbeat", lambda: {"age_seconds": 20, "fresh": True, "stale": False, "last_lines": []})

    dash.render_all()

    dashboard = (dashboards / "dashboard.md").read_text(encoding="utf-8")
    overview = (dashboards / "00-overview.md").read_text(encoding="utf-8")
    assert "No progress provider configured" in dashboard
    assert "no progress provider configured" in overview


def test_configured_dashboard_dir_resolves_during_import(tmp_path, monkeypatch):
    workspace = tmp_path
    config_dir = workspace / ".agents" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "workspace.json").write_text(
        """
{
  "program": {"key": "demo"},
  "projects": [
    {
      "key": "demo",
      "beat": {
        "dashboard_dir": ".custom-dashboards",
        "progress_provider": "none"
      },
      "roots": [{"path": ".", "kind": "primary"}]
    }
  ]
}
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("CORTEX_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("CORTEX_PROJECT", "demo")
    monkeypatch.delenv("CORTEX_DASHBOARD_DIR", raising=False)

    dash = load_dashboard_module()

    assert dash.DASHBOARDS == workspace / ".custom-dashboards"
