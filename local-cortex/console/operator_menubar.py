"""macOS menu-bar operator app for Kaidera OS.

The app is intentionally thin: it displays status and calls
``app.native_operator`` for every action. No runtime, install, update, Cortex, or
project logic lives here.
"""

from __future__ import annotations

import sys
from typing import Any

from app import native_operator

REFRESH_INTERVAL_S = 10.0

STATUS_LABELS = {
    "running": "OK",
    "degraded": "DEGRADED",
    "stopped": "STOPPED",
}

ACTION_TITLES = {
    "start": "Start",
    "stop": "Stop",
    "restart": "Restart",
    "run-installer": "Run Install / Repair",
    "preflight": "Preflight",
    "idle-check": "Idle Check",
    "check-update": "Check for Updates",
    "apply-update": "Apply Update",
    "install-login-item": "Install Login Item",
    "uninstall-login-item": "Uninstall Login Item",
    "open": "Open Console",
}

ALWAYS_SHOW_RESULT = {
    "run-installer",
    "preflight",
    "idle-check",
    "check-update",
    "apply-update",
    "install-login-item",
    "uninstall-login-item",
}


def menu_title(snapshot: dict[str, Any]) -> str:
    status = str(snapshot.get("status") or "stopped")
    label = STATUS_LABELS.get(status, status.upper())
    version = snapshot.get("version")
    suffix = f" v{version}" if version else ""
    return f"Kaidera OS {label}{suffix}"


def menu_model(snapshot: dict[str, Any] | None = None) -> list[tuple[str, str | None]]:
    """Return label/action pairs for the visible menu.

    ``None`` actions are separators. Keeping this pure makes the UI contract easy
    to test without importing AppKit.
    """

    snapshot = snapshot or {"status": "stopped"}
    open_label = f"Open Console ({snapshot.get('console_url') or 'http://127.0.0.1:8765'})"
    return [
        (menu_title(snapshot), None),
        (open_label, "open"),
        (None, None),
        ("Start", "start"),
        ("Stop", "stop"),
        ("Restart", "restart"),
        ("Run Install / Repair", "run-installer"),
        (None, None),
        ("Preflight", "preflight"),
        ("Idle Check", "idle-check"),
        ("Check for Updates", "check-update"),
        ("Apply Update", "apply-update"),
        (None, None),
        ("Install Login Item", "install-login-item"),
        ("Uninstall Login Item", "uninstall-login-item"),
        (None, None),
        ("Quit", "quit"),
    ]


def _ok_text(value: bool) -> str:
    return "OK" if value else "FAIL"


def action_result_title(action: str, result: dict[str, Any]) -> str:
    label = ACTION_TITLES.get(action, action)
    return f"{label}: {_ok_text(bool(result.get('ok')))}"


def action_result_detail(action: str, result: dict[str, Any]) -> str:
    """Human-readable result text for native dialogs.

    Kept pure so the behavior is testable without AppKit.
    """

    if action == "preflight":
        checks = result.get("checks") or []
        lines = [f"Install root: {result.get('repo_root') or 'unknown'}"]
        lines.append("")
        for item in checks:
            required = "required" if item.get("required", True) else "optional"
            lines.append(
                f"{_ok_text(bool(item.get('ok')))} {item.get('name')} ({required}): "
                f"{item.get('detail') or ''}"
            )
        guidance = result.get("guidance") or []
        if guidance:
            lines.append("")
            lines.append("Guidance:")
            lines.extend(f"- {item}" for item in guidance)
        lines.append("")
        lines.append(f"Next: {result.get('next') or 'none'}")
        return "\n".join(lines).strip()

    if action == "run-installer":
        if result.get("ok"):
            return (
                "Canonical install.sh started in the background.\n"
                f"PID: {result.get('pid') or 'unknown'}\n"
                f"Log: {result.get('log_path') or 'unknown'}"
            )
        return (
            "Canonical install.sh did not start.\n"
            f"Error: {result.get('error') or 'unknown'}\n"
            f"Log: {result.get('log_path') or 'not created'}"
        )

    if action == "idle-check":
        active = result.get("active_runs") or []
        lines = [
            f"Idle: {result.get('idle')}",
            f"Checked: {result.get('checked')}",
            f"Reason: {result.get('reason') or 'unknown'}",
            f"Projects checked: {result.get('projects_checked')}",
            f"Active runs: {result.get('active_count')}",
        ]
        for run in active[:8]:
            if not isinstance(run, dict):
                continue
            lines.append(
                "- "
                f"{run.get('project') or '?'} / {run.get('agent') or '?'} / "
                f"{run.get('status') or '?'} / {run.get('run_id') or 'unknown run'}"
            )
        if len(active) > 8:
            lines.append(f"- ... {len(active) - 8} more")
        if result.get("error"):
            lines.append(f"Error: {result.get('error')}")
        return "\n".join(lines)

    if action == "check-update":
        payload = result.get("payload") or {}
        return (
            f"Current: {payload.get('current_version') or 'unknown'}\n"
            f"Latest: {payload.get('latest_version') or payload.get('latest_tag') or 'unknown'}\n"
            f"Update available: {payload.get('update_available')}\n"
            f"Source: {payload.get('source') or 'unknown'}\n"
            f"Error: {result.get('error') or payload.get('error') or 'none'}"
        )

    if action == "apply-update":
        payload = result.get("payload") or {}
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        return (
            f"Accepted: {payload.get('accepted')}\n"
            f"Already running: {payload.get('already_running')}\n"
            f"Job: {job.get('job_id') or 'unknown'}\n"
            f"Status: {job.get('status') or 'unknown'}\n"
            f"Log: {job.get('log_path') or 'unknown'}\n"
            f"Error: {result.get('error') or job.get('error') or 'none'}"
        )

    if action in {"start", "stop", "restart"}:
        command_result = result.get("result") or {}
        return (
            f"System: {result.get('system') or 'unknown'}\n"
            f"Action: {result.get('action') or action}\n"
            f"Return code: {command_result.get('return_code')}\n"
            f"Error: {result.get('error') or command_result.get('stderr') or 'none'}"
        )

    if action == "open":
        return f"URL: {result.get('url') or 'unknown'}"

    return f"Result:\n{result}"


def should_show_result(action: str, result: dict[str, Any]) -> bool:
    return action in ALWAYS_SHOW_RESULT or not bool(result.get("ok"))


def _missing_pyobjc() -> int:
    print(
        "Kaidera OS Operator requires the macOS PyObjC build dependencies. "
        "Run: pip install -r requirements-build.txt",
        file=sys.stderr,
    )
    return 2


def run_menubar() -> int:
    try:
        from AppKit import (
            NSApp,
            NSApplication,
            NSApplicationActivationPolicyAccessory,
            NSAlert,
            NSInformationalAlertStyle,
            NSMenu,
            NSMenuItem,
            NSStatusBar,
            NSVariableStatusItemLength,
            NSWarningAlertStyle,
        )
        from Foundation import NSObject, NSTimer
    except Exception:
        return _missing_pyobjc()

    class OperatorDelegate(NSObject):
        status_item = None
        menu = None
        snapshot: dict[str, Any] = {}

        def applicationDidFinishLaunching_(self, _notification):  # noqa: N802 - ObjC selector
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
                NSVariableStatusItemLength
            )
            self.refresh_(None)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                REFRESH_INTERVAL_S, self, "refresh:", None, True
            )

        def refresh_(self, _sender):  # noqa: N802 - ObjC selector
            self.snapshot = native_operator.service_snapshot()
            self._rebuild_menu()

        def _rebuild_menu(self):
            if self.status_item is None:
                return
            self.status_item.button().setTitle_(menu_title(self.snapshot))
            menu = NSMenu.alloc().init()
            for label, action in menu_model(self.snapshot):
                if label is None:
                    menu.addItem_(NSMenuItem.separatorItem())
                    continue
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, None, "")
                if action:
                    selector = f"{action.replace('-', '_')}:"
                    item.setTarget_(self)
                    item.setAction_(selector)
                menu.addItem_(item)
            self.status_item.setMenu_(menu)
            self.menu = menu

        def _show_result(self, action: str, result: dict[str, Any]):
            alert = NSAlert.alloc().init()
            alert.setMessageText_(action_result_title(action, result))
            alert.setInformativeText_(action_result_detail(action, result))
            alert.setAlertStyle_(
                NSInformationalAlertStyle if result.get("ok") else NSWarningAlertStyle
            )
            alert.runModal()

        def _run_action(self, action: str):
            result: dict[str, Any]
            if action == "open":
                result = native_operator.open_console()
            elif action in {"start", "stop", "restart"}:
                result = native_operator.control_service(action)
            elif action == "preflight":
                result = native_operator.preflight()
            elif action == "idle-check":
                result = native_operator.idle_snapshot()
            elif action == "run-installer":
                result = native_operator.start_installer()
            elif action == "check-update":
                result = native_operator.check_update_status()
            elif action == "apply-update":
                result = native_operator.apply_update()
            elif action == "install-login-item":
                result = native_operator.install_launch_agent()
            elif action == "uninstall-login-item":
                result = native_operator.uninstall_launch_agent()
            else:
                result = {"ok": False, "error": f"unknown action: {action}"}
            if should_show_result(action, result):
                self._show_result(action, result)
            self.refresh_(None)

        def open_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("open")

        def start_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("start")

        def stop_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("stop")

        def restart_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("restart")

        def run_installer_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("run-installer")

        def preflight_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("preflight")

        def idle_check_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("idle-check")

        def check_update_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("check-update")

        def apply_update_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("apply-update")

        def install_login_item_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("install-login-item")

        def uninstall_login_item_(self, sender):  # noqa: N802 - ObjC selector
            self._run_action("uninstall-login-item")

        def quit_(self, sender):  # noqa: N802 - ObjC selector
            NSApp.terminate_(sender)

    app = NSApplication.sharedApplication()
    delegate = OperatorDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_menubar())
