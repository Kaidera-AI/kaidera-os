from __future__ import annotations

import asyncio
import json

import pytest

import app.main as main_mod
from app.version import __version__


@pytest.mark.asyncio
async def test_console_version_json_returns_single_source_version():
    """The SPA version badge reads the same version used by FastAPI/Jinja."""
    assert await main_mod.console_version_json() == {"version": __version__}


def test_console_version_route_is_json_and_collision_free():
    """`/console/version` is a literal JSON route outside `/app` static serving."""
    from starlette.responses import HTMLResponse

    route = next(
        r
        for r in main_mod.app.routes
        if getattr(r, "path", None) == "/console/version"
        and "GET" in (getattr(r, "methods", None) or set())
    )
    assert route.response_class is not HTMLResponse

    all_paths = {getattr(r, "path", None) for r in main_mod.app.routes}
    assert "/app" in all_paths or not (main_mod.SPA_DIST_DIR / "index.html").is_file()
    assert "/projects/{project_key}" in all_paths


def test_update_status_soft_errors_when_gh_is_missing(monkeypatch):
    """Update checks are advisory; missing gh must not raise or break the app."""
    monkeypatch.setenv("KAIDERA_REPO", "Kaidera-AI/homebrew-kaidera")
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: None)

    status = main_mod._console_update_status()

    assert status["current_version"] == __version__
    assert status["check_ok"] is False
    assert status["update_available"] is None
    assert "GitHub CLI not installed" in status["error"]


def test_update_status_uses_canonical_repository_by_default(monkeypatch):
    monkeypatch.delenv("KAIDERA_REPO", raising=False)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: None)

    status = main_mod._console_update_status()

    assert status["check_ok"] is False
    assert status["repo"] == "Kaidera-AI/homebrew-kaidera"
    assert "GitHub CLI not installed" in status["error"]


def test_update_status_reports_newer_signed_release(monkeypatch):
    monkeypatch.setenv("KAIDERA_REPO", "Kaidera-AI/homebrew-kaidera")
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: "/usr/bin/gh")

    def run_cmd(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/gh", "release", "view"]
        assert "tagName,publishedAt,url,name,body" in argv
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "tagName": "v999.0.0",
                        "name": "Release v999",
                        "body": "Upgrade notes\n\n- Fix one\n- Add two",
                        "publishedAt": "2026-06-24T00:00:00Z",
                        "url": "https://github.com/Kaidera-AI/homebrew-kaidera/releases/tag/v999.0.0",
                    }
                ),
                "stderr": "",
            },
        )()

    status = main_mod._console_update_status(run_cmd=run_cmd)

    assert status["check_ok"] is True
    assert status["latest_tag"] == "v999.0.0"
    assert status["latest_version"] == "999.0.0"
    assert status["update_available"] is True
    assert status["update_command"] == "./update.sh"
    assert status["admin_required"] is True
    assert status["release_name"] == "Release v999"
    assert "Upgrade notes" in status["release_notes"]
    assert status["impact"]
    assert status["backup_guidance"]
    assert status["rollback_guidance"]
    assert status["post_update_checks"]


def test_update_status_cache_cold_snapshot_schedules_refresh():
    main_mod._clear_update_status_cache_for_tests()

    status, should_refresh = main_mod._update_status_cache_snapshot()

    assert should_refresh is True
    assert status["check_ok"] is False
    assert status["refreshing"] is True
    assert status["cached"] is False
    assert "background" in status["error"]


@pytest.mark.asyncio
async def test_console_update_status_route_does_not_block_on_release_check(monkeypatch):
    main_mod._clear_update_status_cache_for_tests()
    calls: list[str] = []

    async def fake_refresh():
        calls.append("refresh")

    def fail_direct_check(*_args, **_kwargs):
        raise AssertionError("route must not run the blocking release check inline")

    monkeypatch.setattr(main_mod, "_refresh_update_status_cache", fake_refresh)
    monkeypatch.setattr(main_mod, "_console_update_status", fail_direct_check)

    status = await main_mod.console_update_status_json()
    await asyncio.sleep(0)

    assert status["refreshing"] is True
    assert calls == ["refresh"]


@pytest.mark.asyncio
async def test_update_status_refresh_populates_fast_cache(monkeypatch):
    main_mod._clear_update_status_cache_for_tests()
    monkeypatch.setenv("KAIDERA_REPO", "Kaidera-AI/homebrew-kaidera")
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: "/usr/bin/gh")

    def run_cmd(argv, **kwargs):
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps({"tagName": f"v{__version__}", "name": "Current release"}),
                "stderr": "",
            },
        )()

    await main_mod._refresh_update_status_cache(run_cmd=run_cmd)
    status, should_refresh = main_mod._update_status_cache_snapshot()

    assert should_refresh is False
    assert status["cached"] is True
    assert status["check_ok"] is True
    assert status["latest_version"] == __version__
    assert status["update_available"] is False


def test_console_update_status_route_is_json_and_collision_free():
    from starlette.responses import HTMLResponse

    route = next(
        r
        for r in main_mod.app.routes
        if getattr(r, "path", None) == "/console/update-status"
        and "GET" in (getattr(r, "methods", None) or set())
    )
    assert route.response_class is not HTMLResponse


def test_update_apply_job_reports_missing_update_script(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path / "missing-repo")
    monkeypatch.setattr(main_mod, "UPDATE_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(main_mod, "UPDATE_JOB_STATUS_PATH", tmp_path / "logs" / "update-job.json")

    result = main_mod._start_update_apply_job()

    assert result["accepted"] is False
    assert result["job"]["status"] == "failed"
    assert result["job"]["return_code"] == 127
    assert result["job"]["health_checks"] == []
    assert "update script not found" in result["job"]["error"]


def test_update_apply_job_starts_detached_runner(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "update.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    logs = tmp_path / "logs"
    seen: dict[str, object] = {}

    monkeypatch.setattr(main_mod, "REPO_ROOT", repo)
    monkeypatch.setattr(main_mod, "UPDATE_LOG_DIR", logs)
    monkeypatch.setattr(main_mod, "UPDATE_JOB_STATUS_PATH", logs / "update-job.json")

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProc()

    result = main_mod._start_update_apply_job(popen=fake_popen)

    assert result["accepted"] is True
    assert result["job"]["status"] == "running"
    assert result["job"]["pid"] == 4242
    assert result["job"]["command"] == "./update.sh"
    assert result["job"]["health_checks"] == []
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["cwd"] == str(repo)
    runner = logs / f"update-runner-{result['job']['job_id']}.py"
    assert runner.exists()
    runner_text = runner.read_text(encoding="utf-8")
    assert "subprocess.run(" in runner_text
    assert "post_update_health_checks" in runner_text
    assert "/console/version" in runner_text


def test_console_update_apply_route_is_json_and_admin_gated():
    from starlette.responses import HTMLResponse

    route = next(
        r
        for r in main_mod.app.routes
        if getattr(r, "path", None) == "/console/update/apply"
        and "POST" in (getattr(r, "methods", None) or set())
    )
    assert route.response_class is not HTMLResponse
