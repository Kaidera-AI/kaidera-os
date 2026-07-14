from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "macos" / "prove_operator_lifecycle.py"
    spec = importlib.util.spec_from_file_location("prove_operator_lifecycle", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(payload, returncode: int = 0):
    if isinstance(payload, str):
        stdout = payload
    else:
        stdout = json.dumps(payload)
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_parse_operator_json_returns_dict_payload():
    mod = _load_module()

    payload = mod.parse_operator_json(_result({"status": "running", "version": "0.1.196"}))

    assert payload == {"status": "running", "version": "0.1.196"}


def test_parse_operator_json_captures_invalid_stdout():
    mod = _load_module()

    payload = mod.parse_operator_json(_result("not-json", returncode=1))

    assert "_parse_error" in payload
    assert payload["_returncode"] == 1
    assert payload["_stdout"] == "not-json"


def test_poll_status_retries_until_predicate_matches():
    mod = _load_module()
    seen = iter([
        _result({"status": "stopped"}),
        _result({"status": "running", "version": "0.1.196"}),
    ])
    clock = {"now": 0.0}

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += seconds

    ready, payload, attempts = mod.poll_status(
        lambda: next(seen),
        mod.status_is_ready,
        timeout_seconds=10,
        interval_seconds=1,
        sleep=sleep,
        monotonic=monotonic,
    )

    assert ready is True
    assert attempts == 2
    assert payload["status"] == "running"


def test_poll_status_times_out_with_last_payload():
    mod = _load_module()
    calls = {"count": 0}
    clock = {"now": 0.0}

    def run_status():
        calls["count"] += 1
        return _result({"status": "stopped", "attempt": calls["count"]})

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += seconds

    ready, payload, attempts = mod.poll_status(
        run_status,
        mod.status_is_ready,
        timeout_seconds=1,
        interval_seconds=1,
        sleep=sleep,
        monotonic=monotonic,
    )

    assert ready is False
    assert attempts == 2
    assert payload["attempt"] == 2


def test_build_report_accepts_optional_manual_and_skip_checks(tmp_path):
    mod = _load_module()

    report = mod.build_report(
        root=tmp_path,
        artifact=tmp_path / "operator.dmg",
        version="0.1.196",
        install_dir=tmp_path / "Applications",
        checks=[
            mod.Check("required", "ok", "done"),
            mod.Check("optional_skip", "skip", "not requested", required=False),
            mod.Check("manual_reboot", "manual", "run after reboot", required=False),
        ],
    )

    assert report["ready"] is True


def test_build_report_fails_required_failure(tmp_path):
    mod = _load_module()

    report = mod.build_report(
        root=tmp_path,
        artifact=None,
        version="0.1.196",
        install_dir=None,
        checks=[mod.Check("required", "fail", "broken")],
    )

    assert report["ready"] is False


def test_default_output_uses_release_evidence_path(tmp_path):
    mod = _load_module()

    output = mod.default_output(tmp_path, "0.1.196")

    assert output == tmp_path / "output" / "release" / "kaidera-os-operator-macos" / "evidence" / (
        "operator-lifecycle-v0.1.196.json"
    )
