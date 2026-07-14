from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import native_operator as op


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


def _idle_request_json(_method, url):
    if url.endswith("/healthz"):
        return 200, {"status": "ok"}, None
    if url.endswith("/projects"):
        return 200, [], None
    raise AssertionError(url)


def test_launchd_plist_uses_existing_runner_and_browser_console(tmp_path):
    config = op.OperatorConfig(repo_root=tmp_path, console_port=9876)

    text = op.launchd_plist(config)

    assert op.LAUNCHD_LABEL in text
    assert str(tmp_path / "run-kaidera-os-console.sh") in text
    assert "<key>RunAtLoad</key>" in text
    assert "<key>KeepAlive</key>" in text
    assert "9876" not in text  # port stays in the existing runner, not duplicated in launchd


def test_service_snapshot_running_when_console_and_cortex_are_ok(tmp_path):
    config = op.OperatorConfig(repo_root=tmp_path, console_port=9876)

    def request_json(method, url):
        assert method == "GET"
        if url.endswith("/console/version"):
            return 200, {"version": "0.1.175"}, None
        if url.endswith("/healthz"):
            return 200, {"status": "ok"}, None
        if url.endswith("/cortex/admin-status"):
            return 200, {"status": "ok"}, None
        raise AssertionError(url)

    snapshot = op.service_snapshot(config, request_json=request_json)

    assert snapshot["status"] == "running"
    assert snapshot["version"] == "0.1.175"
    assert snapshot["console_url"] == "http://127.0.0.1:9876"
    assert snapshot["repo_root"] == str(tmp_path)
    assert snapshot["repo_root_found"] is False


def test_service_snapshot_uses_healthz_version_without_slow_version_probe(tmp_path):
    config = op.OperatorConfig(repo_root=tmp_path, console_port=9876)
    seen: list[str] = []

    def request_json(_method, url):
        seen.append(url)
        if url.endswith("/healthz"):
            return 200, {"status": "ok", "version": "0.1.217"}, None
        if url.endswith("/console/version"):
            raise AssertionError("/console/version should be a fallback, not the primary status probe")
        if url.endswith("/cortex/admin-status"):
            return 200, {"status": "ok"}, None
        raise AssertionError(url)

    snapshot = op.service_snapshot(config, request_json=request_json)

    assert snapshot["status"] == "running"
    assert snapshot["version"] == "0.1.217"
    assert seen == [
        "http://127.0.0.1:9876/healthz",
        "http://127.0.0.1:9876/cortex/admin-status",
    ]


def test_service_snapshot_degraded_when_console_is_up_but_cortex_is_not(tmp_path):
    config = op.OperatorConfig(repo_root=tmp_path, launch_agents_dir_override=tmp_path / "LaunchAgents")

    def request_json(_method, url):
        if url.endswith("/console/version"):
            return 200, {"version": "0.1.175"}, None
        if url.endswith("/healthz"):
            return 200, {"status": "ok"}, None
        if url.endswith("/cortex/admin-status"):
            return 200, {"status": "mismatch"}, None
        raise AssertionError(url)

    assert op.service_snapshot(config, request_json=request_json)["status"] == "degraded"


def test_service_snapshot_stopped_when_console_is_unreachable(tmp_path):
    config = op.OperatorConfig(repo_root=tmp_path)

    def request_json(_method, _url):
        return None, None, "connection refused"

    snapshot = op.service_snapshot(config, request_json=request_json)

    assert snapshot["status"] == "stopped"
    assert snapshot["version"] is None


def test_custom_launchd_label_controls_its_plist_path(tmp_path):
    config = op.OperatorConfig(
        repo_root=tmp_path,
        launchd_label="ai.example.custom-console",
        launch_agents_dir_override=tmp_path / "LaunchAgents",
    )

    assert config.launchd_plist_path == tmp_path / "LaunchAgents" / "ai.example.custom-console.plist"


def test_install_launch_agent_writes_plist_and_bootstraps(monkeypatch, tmp_path):
    monkeypatch.setattr(op.platform, "system", lambda: "Darwin")
    runner = tmp_path / "run-kaidera-os-console.sh"
    runner.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    config = op.OperatorConfig(
        repo_root=tmp_path,
        launch_agents_dir_override=tmp_path / "LaunchAgents",
    )
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        return _cp(0)

    result = op.install_launch_agent(config, run=run, uid=501)

    assert result["ok"] is True
    assert config.launchd_plist_path.exists()
    assert calls[0][:3] == ["launchctl", "bootout", "gui/501"]
    assert calls[1][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert calls[2] == ["launchctl", "enable", f"gui/501/{op.LAUNCHD_LABEL}"]
    assert calls[3] == ["launchctl", "kickstart", "-k", f"gui/501/{op.LAUNCHD_LABEL}"]


def test_install_launch_agent_requires_mac(monkeypatch, tmp_path):
    monkeypatch.setattr(op.platform, "system", lambda: "Linux")

    result = op.install_launch_agent(op.OperatorConfig(repo_root=tmp_path), run=lambda _argv: _cp(0))

    assert result["ok"] is False
    assert "macOS-only" in result["error"]


def test_control_service_restart_uses_launchctl_on_mac(monkeypatch, tmp_path):
    monkeypatch.setattr(op.platform, "system", lambda: "Darwin")
    calls: list[list[str]] = []

    result = op.control_service(
        "restart",
        op.OperatorConfig(repo_root=tmp_path),
        run=lambda argv: calls.append(argv) or _cp(0),
        request_json=_idle_request_json,
        uid=501,
    )

    assert result["ok"] is True
    assert calls == [["launchctl", "kickstart", "-k", f"gui/501/{op.LAUNCHD_LABEL}"]]


def test_control_service_stop_keeps_launch_agent_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(op.platform, "system", lambda: "Darwin")
    calls: list[list[str]] = []

    result = op.control_service(
        "stop",
        op.OperatorConfig(repo_root=tmp_path),
        run=lambda argv: calls.append(argv) or _cp(0),
        uid=501,
    )

    assert result["ok"] is True
    assert calls == [["launchctl", "kill", "TERM", f"gui/501/{op.LAUNCHD_LABEL}"]]


def test_control_service_start_bootstraps_then_kickstarts_on_mac(monkeypatch, tmp_path):
    monkeypatch.setattr(op.platform, "system", lambda: "Darwin")
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    config = op.OperatorConfig(repo_root=tmp_path, launch_agents_dir_override=launch_agents)
    config.launchd_plist_path.write_text("<plist />", encoding="utf-8")
    calls: list[list[str]] = []

    result = op.control_service(
        "start",
        config,
        run=lambda argv: calls.append(argv) or _cp(0),
        uid=501,
    )

    assert result["ok"] is True
    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(config.launchd_plist_path)],
        ["launchctl", "enable", f"gui/501/{op.LAUNCHD_LABEL}"],
        ["launchctl", "kickstart", "-k", f"gui/501/{op.LAUNCHD_LABEL}"],
    ]


def test_check_and_apply_update_call_existing_console_endpoints(tmp_path):
    config = op.OperatorConfig(repo_root=tmp_path)
    seen: list[tuple[str, str]] = []

    def request_json(method, url):
        if url.endswith("/healthz"):
            return 200, {"status": "ok"}, None
        if url.endswith("/projects"):
            return 200, [], None
        seen.append((method, url))
        return 202 if method == "POST" else 200, {"accepted": method == "POST"}, None

    check = op.check_update_status(config, request_json=request_json)
    apply = op.apply_update(config, request_json=request_json)

    assert check["ok"] is True
    assert apply["ok"] is True
    assert seen == [
        ("GET", "http://127.0.0.1:8765/console/update-status"),
        ("POST", "http://127.0.0.1:8765/console/update/apply"),
    ]


def test_idle_snapshot_reports_active_runs(tmp_path):
    config = op.OperatorConfig(repo_root=tmp_path)

    def request_json(_method, url):
        if url.endswith("/healthz"):
            return 200, {"status": "ok"}, None
        if url.endswith("/projects"):
            return 200, [{"project_key": "marketing"}], None
        if url.endswith("/runs/marketing"):
            return 200, {
                "active_count": 1,
                "active": [
                    {
                        "run_id": "run-1",
                        "project": "marketing",
                        "agent": "marlow",
                        "status": "running",
                    }
                ],
            }, None
        raise AssertionError(url)

    snapshot = op.idle_snapshot(config, request_json=request_json)

    assert snapshot["ok"] is True
    assert snapshot["idle"] is False
    assert snapshot["reason"] == "active_workers"
    assert snapshot["active_count"] == 1
    assert snapshot["active_runs"][0]["agent"] == "marlow"


def test_restart_blocks_when_workers_are_active(monkeypatch, tmp_path):
    monkeypatch.setattr(op.platform, "system", lambda: "Darwin")

    def request_json(_method, url):
        if url.endswith("/healthz"):
            return 200, {"status": "ok"}, None
        if url.endswith("/projects"):
            return 200, [{"project_key": "marketing"}], None
        if url.endswith("/runs/marketing"):
            return 200, {
                "active_count": 1,
                "active": [{"run_id": "r1", "agent": "marlow", "status": "queued"}],
            }, None
        raise AssertionError(url)

    calls: list[list[str]] = []
    result = op.control_service(
        "restart",
        op.OperatorConfig(repo_root=tmp_path),
        run=lambda argv: calls.append(argv) or _cp(0),
        request_json=request_json,
        uid=501,
    )

    assert result["ok"] is False
    assert "blocked" in result["error"]
    assert result["idle_check"]["active_count"] == 1
    assert calls == []


def test_restart_idle_guard_can_be_explicitly_overridden(monkeypatch, tmp_path):
    monkeypatch.setattr(op.platform, "system", lambda: "Darwin")
    monkeypatch.setenv(op.IDLE_GUARD_OVERRIDE_ENV, "1")
    calls: list[list[str]] = []

    result = op.control_service(
        "restart",
        op.OperatorConfig(repo_root=tmp_path),
        run=lambda argv: calls.append(argv) or _cp(0),
        request_json=lambda _method, url: (_ for _ in ()).throw(AssertionError(url)),
        uid=501,
    )

    assert result["ok"] is True
    assert calls == [["launchctl", "kickstart", "-k", f"gui/501/{op.LAUNCHD_LABEL}"]]


def test_open_console_delegates_to_default_browser(tmp_path):
    opened: list[str] = []

    result = op.open_console(
        op.OperatorConfig(repo_root=tmp_path, console_port=9876),
        open_url=lambda url: opened.append(url) or True,
    )

    assert result == {"ok": True, "url": "http://127.0.0.1:9876"}
    assert opened == ["http://127.0.0.1:9876"]


def test_preflight_reports_required_dependencies(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "local-cortex" / "console").mkdir(parents=True)
    (repo / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (repo / "run-kaidera-os-console.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(op.shutil, "which", lambda name: f"/bin/{name}" if name in {"python3", "docker"} else None)

    result = op.preflight(
        op.OperatorConfig(repo_root=repo),
        run=lambda argv: _cp(0, stdout="ok"),
    )

    assert result["ok"] is True
    assert {item["name"]: item["ok"] for item in result["checks"]}["docker_daemon"] is True
    assert result["guidance"] == [
        "Preflight is clean. Use Start/Open Console, or Run Install / Repair for first setup."
    ]


def test_preflight_guidance_for_missing_docker_and_runner(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "local-cortex" / "console").mkdir(parents=True)
    (repo / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(op.shutil, "which", lambda name: "/bin/python3" if name == "python3" else None)

    result = op.preflight(op.OperatorConfig(repo_root=repo), run=lambda argv: _cp(1))

    assert result["ok"] is False
    assert any("Docker-compatible runtime" in item for item in result["guidance"])
    assert any("Run Install / Repair" in item for item in result["guidance"])


def test_start_installer_runs_canonical_install_script(tmp_path):
    repo = tmp_path / "repo"
    (repo / "local-cortex" / "logs").mkdir(parents=True)
    install = repo / "install.sh"
    install.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    seen = {}

    class FakeProc:
        pid = 1234

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs["cwd"]
        seen["stdout"] = kwargs["stdout"]
        return FakeProc()

    result = op.start_installer(
        op.OperatorConfig(repo_root=repo),
        popen=fake_popen,
        request_json=_idle_request_json,
    )
    seen["stdout"].close()

    assert result["ok"] is True
    assert result["pid"] == 1234
    assert seen["argv"] == ["bash", str(install)]
    assert seen["cwd"] == str(repo)
    assert result["log_path"].endswith("operator-install.log")


def test_resolve_repo_root_prefers_env_home(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "local-cortex" / "console").mkdir(parents=True)
    (repo / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setenv("KAIDERA_OS_HOME", str(repo))

    assert op.resolve_repo_root() == repo.resolve()


def test_operator_home_round_trip(tmp_path):
    repo = tmp_path / "repo"
    config_path = tmp_path / "operator.json"

    written = op.write_operator_home(repo, config_path)

    assert written == config_path
    assert op._read_operator_home(config_path) == repo.resolve()
