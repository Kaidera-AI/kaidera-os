"""Native operator controller seams for the Mac menu-bar shell.

This module is intentionally UI-free. The E011 macOS app should call these
functions instead of reimplementing lifecycle, health, browser-open, or update
logic. That keeps the native shell thin and the Kaidera OS runtime canonical.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, request

LAUNCHD_LABEL = "ai.kaidera.kaidera-os.console"
DEFAULT_PORT = 8765
OPERATOR_CONFIG_PATH = Path.home() / ".kaidera-os" / "operator.json"
ACTIVE_RUN_STATUSES = {"queued", "running"}
IDLE_GUARD_OVERRIDE_ENV = "KAIDERA_OS_OPERATOR_SKIP_IDLE_GUARD"

RunFn = Callable[[list[str]], subprocess.CompletedProcess]
JsonRequestFn = Callable[[str, str], tuple[int | None, dict[str, Any] | None, str | None]]


@dataclass(frozen=True)
class OperatorConfig:
    """Resolved local install paths for the native operator boundary."""

    repo_root: Path
    console_port: int = DEFAULT_PORT
    launchd_label: str = LAUNCHD_LABEL
    launch_agents_dir_override: Path | None = None

    @property
    def console_base_url(self) -> str:
        return f"http://127.0.0.1:{self.console_port}"

    @property
    def runner_path(self) -> Path:
        return self.repo_root / "run-kaidera-os-console.sh"

    @property
    def update_script(self) -> Path:
        return self.repo_root / "update.sh"

    @property
    def install_script(self) -> Path:
        return self.repo_root / "install.sh"

    @property
    def logs_dir(self) -> Path:
        return self.repo_root / "local-cortex" / "logs"

    @property
    def launch_agents_dir(self) -> Path:
        if self.launch_agents_dir_override is not None:
            return self.launch_agents_dir_override
        return Path.home() / "Library" / "LaunchAgents"

    @property
    def launchd_plist_path(self) -> Path:
        return self.launch_agents_dir / f"{self.launchd_label}.plist"


def _looks_like_repo_root(path: Path) -> bool:
    return (path / "install.sh").exists() and (path / "local-cortex" / "console").exists()


def _read_operator_home(path: Path = OPERATOR_CONFIG_PATH) -> Path | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw = str(data.get("repo_root") or "").strip() if isinstance(data, dict) else ""
    return Path(raw).expanduser() if raw else None


def write_operator_home(repo_root: Path, path: Path = OPERATOR_CONFIG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"repo_root": str(repo_root.expanduser().resolve())}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def resolve_repo_root() -> Path:
    """Resolve the installed Kaidera OS root for packaged native shells."""

    candidates: list[Path] = []
    env_home = os.environ.get("KAIDERA_OS_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser())
    stored = _read_operator_home()
    if stored is not None:
        candidates.append(stored)
    source_root = Path(__file__).resolve().parents[3]
    candidates.append(source_root)
    candidates.append(Path.home() / "Library" / "Application Support" / "Kaidera OS" / "kaidera-os")

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if _looks_like_repo_root(resolved):
            return resolved
    return source_root


def default_config() -> OperatorConfig:
    repo_root = resolve_repo_root()
    port = int(os.environ.get("KAIDERA_CONSOLE_PORT", str(DEFAULT_PORT)))
    return OperatorConfig(repo_root=repo_root, console_port=port)


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _request_json(method: str, url: str) -> tuple[int | None, dict[str, Any] | None, str | None]:
    data = b"" if method.upper() == "POST" else None
    req = request.Request(url, data=data, method=method.upper())
    try:
        with request.urlopen(req, timeout=5) as resp:  # noqa: S310 - localhost operator endpoint
            raw = resp.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return int(resp.status), payload if isinstance(payload, dict) else {"value": payload}, None
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return int(exc.code), payload if isinstance(payload, dict) else {"value": payload}, None
    except Exception as exc:
        return None, None, str(exc)


def _payload_projects(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        projects = payload.get("projects")
        if isinstance(projects, list):
            return [p for p in projects if isinstance(p, dict)]
    return []


def _project_key(row: dict[str, Any]) -> str | None:
    for key in ("project_key", "key", "name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def _active_runs_from_board(project: str, board: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(board, dict):
        return []
    runs = board.get("active")
    if not isinstance(runs, list):
        runs = []
    active: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "").strip().lower()
        if status in ACTIVE_RUN_STATUSES or bool(run.get("running")):
            item = dict(run)
            item.setdefault("project", project)
            active.append(item)
    active_count = board.get("active_count")
    if isinstance(active_count, int) and active_count > len(active):
        active.append(
            {
                "project": project,
                "run_id": None,
                "agent": None,
                "status": "unknown-active",
                "count_gap": active_count - len(active),
            }
        )
    return active


def idle_snapshot(
    config: OperatorConfig | None = None,
    *,
    request_json: JsonRequestFn = _request_json,
) -> dict[str, Any]:
    """Check whether it is safe to restart/update the local console.

    This uses only the console's public JSON surfaces: /healthz, /projects, and
    /runs/{project}. If the console is stopped/unreachable, there is no local
    console process to restart over, so repair/start paths can proceed. If the
    console is reachable but project/run-state reads fail, fail closed.
    """

    config = config or default_config()
    base = config.console_base_url
    health_code, _health_payload, health_err = request_json("GET", f"{base}/healthz")
    if health_code != 200:
        return {
            "ok": True,
            "idle": True,
            "checked": False,
            "reason": "console_unreachable",
            "console_url": base,
            "error": health_err,
            "projects_checked": 0,
            "active_count": 0,
            "active_runs": [],
        }

    projects_code, projects_payload, projects_err = request_json("GET", f"{base}/projects")
    if projects_code != 200:
        return {
            "ok": False,
            "idle": False,
            "checked": False,
            "reason": "projects_unavailable",
            "console_url": base,
            "error": projects_err or f"/projects returned {projects_code}",
            "projects_checked": 0,
            "active_count": None,
            "active_runs": [],
        }

    projects = _payload_projects(projects_payload)
    active_runs: list[dict[str, Any]] = []
    checked = 0
    errors: list[dict[str, Any]] = []
    for row in projects:
        key = _project_key(row)
        if not key:
            continue
        checked += 1
        code, board, err = request_json("GET", f"{base}/runs/{key}")
        if code != 200 or not isinstance(board, dict):
            errors.append({"project": key, "code": code, "error": err or "run board unavailable"})
            continue
        active_runs.extend(_active_runs_from_board(key, board))

    if errors:
        return {
            "ok": False,
            "idle": False,
            "checked": True,
            "reason": "runs_unavailable",
            "console_url": base,
            "projects_checked": checked,
            "active_count": None,
            "active_runs": active_runs,
            "errors": errors,
        }

    return {
        "ok": True,
        "idle": len(active_runs) == 0,
        "checked": True,
        "reason": "idle" if not active_runs else "active_workers",
        "console_url": base,
        "projects_checked": checked,
        "active_count": len(active_runs),
        "active_runs": active_runs,
    }


def _idle_guard_enabled() -> bool:
    return str(os.environ.get(IDLE_GUARD_OVERRIDE_ENV, "")).strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def _blocked_by_active_workers(
    action: str,
    config: OperatorConfig | None = None,
    *,
    request_json: JsonRequestFn = _request_json,
) -> dict[str, Any] | None:
    if not _idle_guard_enabled():
        return None
    snapshot = idle_snapshot(config, request_json=request_json)
    if snapshot.get("idle") is True and snapshot.get("ok") is True:
        return None
    return {
        "ok": False,
        "action": action,
        "error": (
            f"{action} blocked: active or unreadable worker runs detected. "
            f"Wait/cancel them first, or set {IDLE_GUARD_OVERRIDE_ENV}=1 for an explicit override."
        ),
        "idle_check": snapshot,
    }


def _service_domain(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def _completed_ok(result: subprocess.CompletedProcess | None) -> bool:
    return result is not None and int(getattr(result, "returncode", 1)) == 0


def _result_dict(result: subprocess.CompletedProcess | None) -> dict[str, Any]:
    if result is None:
        return {"return_code": None, "stdout": "", "stderr": ""}
    return {
        "return_code": int(getattr(result, "returncode", 0)),
        "stdout": (getattr(result, "stdout", "") or "").strip(),
        "stderr": (getattr(result, "stderr", "") or "").strip(),
    }


def _check(name: str, ok: bool, detail: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "required": required, "detail": detail}


def _preflight_guidance(checks: list[dict[str, Any]]) -> list[str]:
    by_name = {str(item.get("name")): item for item in checks}
    guidance: list[str] = []

    def failed(name: str) -> bool:
        return not bool(by_name.get(name, {}).get("ok"))

    if failed("repo_root") or failed("install_script"):
        guidance.append(
            "Repair the install root: set KAIDERA_OS_HOME or run "
            "`kaidera-os operator set-home /path/to/kaidera-os`."
        )
    if failed("python3"):
        guidance.append("Install Python 3.11+ and re-run Preflight.")
    if failed("docker_cli"):
        guidance.append(
            "Install a Docker-compatible runtime such as Docker Desktop or OrbStack, then re-run Preflight."
        )
    elif failed("docker_daemon"):
        guidance.append(
            "Start Docker Desktop or OrbStack and wait until `docker info` succeeds."
        )
    if failed("runner") and not (failed("repo_root") or failed("install_script")):
        guidance.append("Run Install / Repair to generate the console runner and LaunchAgent.")

    if not guidance:
        guidance.append("Preflight is clean. Use Start/Open Console, or Run Install / Repair for first setup.")
    return guidance


def preflight(
    config: OperatorConfig | None = None,
    *,
    run: RunFn = _run,
) -> dict[str, Any]:
    """Check whether the local install can be controlled by the native shell."""

    config = config or default_config()
    checks: list[dict[str, Any]] = []
    repo_ok = _looks_like_repo_root(config.repo_root)
    checks.append(_check("repo_root", repo_ok, str(config.repo_root)))
    checks.append(_check("install_script", config.install_script.exists(), str(config.install_script)))
    checks.append(_check("runner", config.runner_path.exists(), str(config.runner_path), required=False))

    py = shutil.which("python3")
    checks.append(_check("python3", py is not None, py or "python3 not found"))

    docker = shutil.which("docker")
    checks.append(_check("docker_cli", docker is not None, docker or "docker not found"))
    if docker is not None:
        try:
            docker_info = run([docker, "info"])
            docker_ok = _completed_ok(docker_info)
            docker_detail = "docker daemon reachable" if docker_ok else (_result_dict(docker_info)["stderr"] or "docker info failed")
        except Exception as exc:
            docker_ok = False
            docker_detail = str(exc)
    else:
        docker_ok = False
        docker_detail = "docker not found"
    checks.append(_check("docker_daemon", docker_ok, docker_detail))

    required_ok = all(item["ok"] for item in checks if item["required"])
    guidance = _preflight_guidance(checks)
    return {
        "ok": required_ok,
        "repo_root": str(config.repo_root),
        "checks": checks,
        "guidance": guidance,
        "next": "run-installer" if required_ok else guidance[0],
    }


def start_installer(
    config: OperatorConfig | None = None,
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    request_json: JsonRequestFn = _request_json,
) -> dict[str, Any]:
    """Start the canonical installer in the background and log its output."""

    config = config or default_config()
    blocked = _blocked_by_active_workers("run-installer", config, request_json=request_json)
    if blocked is not None:
        return blocked
    if not config.install_script.exists():
        return {"ok": False, "error": f"install script not found at {config.install_script}"}
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.logs_dir / "operator-install.log"
    try:
        with log_path.open("ab") as log:
            proc = popen(
                ["bash", str(config.install_script)],
                cwd=str(config.repo_root),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env=os.environ.copy(),
            )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "log_path": str(log_path)}
    return {
        "ok": True,
        "pid": getattr(proc, "pid", None),
        "log_path": str(log_path),
        "command": "./install.sh",
    }


def launchd_plist(config: OperatorConfig | None = None) -> str:
    """Render the macOS LaunchAgent that starts the existing console runner."""

    config = config or default_config()
    out_log = config.logs_dir / "kaidera-os-console.launchd.out.log"
    err_log = config.logs_dir / "kaidera-os-console.launchd.err.log"
    values = {
        "label": config.launchd_label,
        "runner": str(config.runner_path),
        "repo": str(config.repo_root),
        "out": str(out_log),
        "err": str(err_log),
    }
    e = {key: html.escape(value, quote=True) for key, value in values.items()}
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{e["label"]}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{e["runner"]}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{e["repo"]}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>StandardOutPath</key>
  <string>{e["out"]}</string>
  <key>StandardErrorPath</key>
  <string>{e["err"]}</string>
</dict>
</plist>
"""


def install_launch_agent(
    config: OperatorConfig | None = None,
    *,
    run: RunFn = _run,
    uid: int | None = None,
) -> dict[str, Any]:
    """Install and start the macOS LaunchAgent for the existing runner."""

    config = config or default_config()
    if platform.system() != "Darwin":
        return {"ok": False, "error": "LaunchAgent install is macOS-only"}
    if not config.runner_path.exists():
        return {
            "ok": False,
            "error": f"console runner not found at {config.runner_path}; run install.sh first",
        }
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    config.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    config.launchd_plist_path.write_text(launchd_plist(config), encoding="utf-8")

    domain = _service_domain(uid)
    bootout = run(["launchctl", "bootout", domain, str(config.launchd_plist_path)])
    bootstrap = run(["launchctl", "bootstrap", domain, str(config.launchd_plist_path)])
    enable = run(["launchctl", "enable", f"{domain}/{config.launchd_label}"])
    kickstart = run(["launchctl", "kickstart", "-k", f"{domain}/{config.launchd_label}"])
    ok = _completed_ok(bootstrap) and _completed_ok(enable) and _completed_ok(kickstart)
    return {
        "ok": ok,
        "label": config.launchd_label,
        "plist": str(config.launchd_plist_path),
        "bootout": _result_dict(bootout),
        "bootstrap": _result_dict(bootstrap),
        "enable": _result_dict(enable),
        "kickstart": _result_dict(kickstart),
    }


def uninstall_launch_agent(
    config: OperatorConfig | None = None,
    *,
    run: RunFn = _run,
    uid: int | None = None,
) -> dict[str, Any]:
    config = config or default_config()
    if platform.system() != "Darwin":
        return {"ok": False, "error": "LaunchAgent uninstall is macOS-only"}
    domain = _service_domain(uid)
    bootout = run(["launchctl", "bootout", domain, str(config.launchd_plist_path)])
    removed = False
    if config.launchd_plist_path.exists():
        config.launchd_plist_path.unlink()
        removed = True
    return {
        "ok": _completed_ok(bootout) or removed,
        "label": config.launchd_label,
        "plist": str(config.launchd_plist_path),
        "removed": removed,
        "bootout": _result_dict(bootout),
    }


def control_service(
    action: str,
    config: OperatorConfig | None = None,
    *,
    run: RunFn = _run,
    request_json: JsonRequestFn = _request_json,
    uid: int | None = None,
) -> dict[str, Any]:
    """Start, stop, or restart the installed Kaidera OS console service."""

    config = config or default_config()
    if action == "restart":
        blocked = _blocked_by_active_workers(action, config, request_json=request_json)
        if blocked is not None:
            return blocked
    system = platform.system()
    if system == "Darwin":
        domain = _service_domain(uid)
        target = f"{domain}/{config.launchd_label}"
        if action == "start":
            if config.launchd_plist_path.exists():
                run(["launchctl", "bootstrap", domain, str(config.launchd_plist_path)])
                run(["launchctl", "enable", target])
            result = run(["launchctl", "kickstart", "-k", target])
        elif action == "restart":
            result = run(["launchctl", "kickstart", "-k", target])
        elif action == "stop":
            # Stop should not unload the LaunchAgent. Unloading is the
            # uninstall-login-item contract; keeping the job loaded lets a later
            # Start call kickstart the same registered service.
            result = run(["launchctl", "kill", "TERM", target])
        else:
            return {"ok": False, "error": f"unknown service action: {action}"}
        return {"ok": _completed_ok(result), "system": system, "action": action, "result": _result_dict(result)}

    if system == "Linux":
        if not shutil.which("systemctl"):
            return {"ok": False, "system": system, "error": "systemctl not found"}
        result = run(["systemctl", action, "kaidera-os-console"])
        if not _completed_ok(result) and shutil.which("sudo"):
            result = run(["sudo", "systemctl", action, "kaidera-os-console"])
        return {"ok": _completed_ok(result), "system": system, "action": action, "result": _result_dict(result)}

    return {"ok": False, "system": system, "error": "service control is only implemented for macOS/Linux"}


def service_snapshot(
    config: OperatorConfig | None = None,
    *,
    request_json: JsonRequestFn = _request_json,
) -> dict[str, Any]:
    """Return the status payload the menu-bar app should poll."""

    config = config or default_config()
    probes: dict[str, dict[str, Any]] = {}

    def probe(name: str, path: str) -> dict[str, Any]:
        code, payload, err = request_json("GET", f"{config.console_base_url}{path}")
        probes[name] = {"ok": code == 200, "code": code, "payload": payload, "error": err}
        return probes[name]

    health_probe = probe("healthz", "/healthz")
    health_payload = health_probe.get("payload") or {}
    health_version = health_payload.get("version")
    if health_version:
        probes["version"] = {
            "ok": True,
            "code": health_probe.get("code"),
            "payload": {"version": health_version, "source": "healthz"},
            "error": None,
        }
    else:
        probe("version", "/console/version")
    probe("cortex_admin", "/cortex/admin-status")

    console_up = probes["healthz"]["ok"] or probes["version"]["ok"]
    live_ok = probes["healthz"]["ok"]
    cortex_payload = probes["cortex_admin"].get("payload") or {}
    cortex_ok = probes["cortex_admin"]["ok"] and cortex_payload.get("status") == "ok"
    if live_ok and cortex_ok:
        status = "running"
    elif console_up:
        status = "degraded"
    else:
        status = "stopped"
    version_payload = probes["version"].get("payload") or {}
    return {
        "status": status,
        "version": health_version or version_payload.get("version"),
        "console_url": config.console_base_url,
        "platform": platform.system(),
        "repo_root": str(config.repo_root),
        "repo_root_found": _looks_like_repo_root(config.repo_root),
        "launchd_label": config.launchd_label,
        "probes": probes,
    }


def open_console(
    config: OperatorConfig | None = None,
    *,
    open_url: Callable[[str], bool] = webbrowser.open,
) -> dict[str, Any]:
    config = config or default_config()
    ok = bool(open_url(config.console_base_url))
    return {"ok": ok, "url": config.console_base_url}


def check_update_status(
    config: OperatorConfig | None = None,
    *,
    request_json: JsonRequestFn = _request_json,
) -> dict[str, Any]:
    config = config or default_config()
    code, payload, err = request_json("GET", f"{config.console_base_url}/console/update-status")
    return {"ok": code == 200, "code": code, "payload": payload, "error": err}


def apply_update(
    config: OperatorConfig | None = None,
    *,
    request_json: JsonRequestFn = _request_json,
) -> dict[str, Any]:
    config = config or default_config()
    blocked = _blocked_by_active_workers("apply-update", config, request_json=request_json)
    if blocked is not None:
        return blocked
    code, payload, err = request_json("POST", f"{config.console_base_url}/console/update/apply")
    return {"ok": code in {200, 202}, "code": code, "payload": payload, "error": err}


def _print_json(data: dict[str, Any]) -> int:
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0 if data.get("ok", True) is not False else 1


def operator_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kaidera-os operator", description="Native operator shell helpers")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in (
        "status",
        "idle-check",
        "open",
        "start",
        "stop",
        "restart",
        "check-update",
        "apply-update",
        "preflight",
        "run-installer",
    ):
        sub.add_parser(name)
    sub.add_parser("install-login-item")
    sub.add_parser("uninstall-login-item")
    set_home = sub.add_parser("set-home")
    set_home.add_argument("path")
    sub.add_parser("home")
    ns = parser.parse_args(argv)

    if ns.cmd == "status":
        return _print_json(service_snapshot())
    if ns.cmd == "idle-check":
        return _print_json(idle_snapshot())
    if ns.cmd == "open":
        return _print_json(open_console())
    if ns.cmd in {"start", "stop", "restart"}:
        return _print_json(control_service(ns.cmd))
    if ns.cmd == "check-update":
        return _print_json(check_update_status())
    if ns.cmd == "apply-update":
        return _print_json(apply_update())
    if ns.cmd == "preflight":
        return _print_json(preflight())
    if ns.cmd == "run-installer":
        return _print_json(start_installer())
    if ns.cmd == "install-login-item":
        return _print_json(install_launch_agent())
    if ns.cmd == "uninstall-login-item":
        return _print_json(uninstall_launch_agent())
    if ns.cmd == "set-home":
        repo_root = Path(ns.path).expanduser().resolve()
        path = write_operator_home(repo_root)
        return _print_json({"ok": True, "config_path": str(path), "repo_root": str(repo_root)})
    if ns.cmd == "home":
        config = default_config()
        return _print_json({
            "ok": _looks_like_repo_root(config.repo_root),
            "config_path": str(OPERATOR_CONFIG_PATH),
            "repo_root": str(config.repo_root),
            "repo_root_found": _looks_like_repo_root(config.repo_root),
        })
    return 2


if __name__ == "__main__":
    raise SystemExit(operator_main(sys.argv[1:]))
