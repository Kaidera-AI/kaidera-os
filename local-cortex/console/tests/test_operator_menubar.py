from __future__ import annotations

import operator_menubar as menu


def test_menu_title_summarizes_status_and_version():
    assert menu.menu_title({"status": "running", "version": "0.1.176"}) == "Kaidera OS OK v0.1.176"
    assert menu.menu_title({"status": "degraded"}) == "Kaidera OS DEGRADED"
    assert menu.menu_title({"status": "stopped"}) == "Kaidera OS STOPPED"


def test_menu_model_contains_only_controller_actions():
    model = menu.menu_model({"status": "running", "version": "0.1.176", "console_url": "http://x"})
    actions = [action for _label, action in model if action]

    assert actions == [
        "open",
        "start",
        "stop",
        "restart",
        "run-installer",
        "preflight",
        "idle-check",
        "check-update",
        "apply-update",
        "install-login-item",
        "uninstall-login-item",
        "quit",
    ]
    assert model[1] == ("Open Console (http://x)", "open")


def test_preflight_result_detail_lists_checks_and_next_step():
    detail = menu.action_result_detail(
        "preflight",
        {
            "ok": False,
            "repo_root": "/tmp/kaidera-os",
            "checks": [
                {"name": "repo_root", "ok": True, "required": True, "detail": "/tmp/kaidera-os"},
                {"name": "docker_daemon", "ok": False, "required": True, "detail": "not running"},
                {"name": "runner", "ok": False, "required": False, "detail": "not installed yet"},
            ],
            "guidance": [
                "Start Docker Desktop or OrbStack and wait until `docker info` succeeds.",
            ],
            "next": "install Docker/Python or repair KAIDERA_OS_HOME/operator home",
        },
    )

    assert "Install root: /tmp/kaidera-os" in detail
    assert "OK repo_root (required): /tmp/kaidera-os" in detail
    assert "FAIL docker_daemon (required): not running" in detail
    assert "FAIL runner (optional): not installed yet" in detail
    assert "Guidance:" in detail
    assert "Start Docker Desktop or OrbStack" in detail
    assert "Next: install Docker/Python" in detail


def test_run_installer_result_detail_points_to_log():
    detail = menu.action_result_detail(
        "run-installer",
        {"ok": True, "pid": 1234, "log_path": "/tmp/operator-install.log"},
    )

    assert "Canonical install.sh started" in detail
    assert "PID: 1234" in detail
    assert "Log: /tmp/operator-install.log" in detail


def test_update_result_details_are_operator_readable():
    check = menu.action_result_detail(
        "check-update",
        {
            "ok": True,
            "payload": {
                "current_version": "0.1.179",
                "latest_version": "0.1.180",
                "update_available": True,
                "source": "github-release",
            },
        },
    )
    apply = menu.action_result_detail(
        "apply-update",
        {
            "ok": True,
            "payload": {
                "accepted": True,
                "already_running": False,
                "job": {"job_id": "abc123", "status": "running", "log_path": "/tmp/update.log"},
            },
        },
    )

    assert "Current: 0.1.179" in check
    assert "Latest: 0.1.180" in check
    assert "Update available: True" in check
    assert "Accepted: True" in apply
    assert "Job: abc123" in apply
    assert "Log: /tmp/update.log" in apply


def test_idle_check_result_details_list_active_runs():
    detail = menu.action_result_detail(
        "idle-check",
        {
            "ok": True,
            "idle": False,
            "checked": True,
            "reason": "active_workers",
            "projects_checked": 1,
            "active_count": 1,
            "active_runs": [
                {
                    "project": "marketing",
                    "agent": "marlow",
                    "status": "running",
                    "run_id": "run-1",
                }
            ],
        },
    )

    assert "Idle: False" in detail
    assert "Reason: active_workers" in detail
    assert "Active runs: 1" in detail
    assert "marketing / marlow / running / run-1" in detail


def test_should_show_result_for_explicit_actions_or_failures():
    assert menu.should_show_result("preflight", {"ok": True}) is True
    assert menu.should_show_result("idle-check", {"ok": True}) is True
    assert menu.should_show_result("start", {"ok": True}) is False
    assert menu.should_show_result("start", {"ok": False}) is True
    assert menu.action_result_title("start", {"ok": False}) == "Start: FAIL"
