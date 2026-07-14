from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.adapters.runtime_herdr import (
    HERDR_BIN_ENV,
    LEGACY_HERDR_BIN_ENV,
    HerdrCliRuntimeBackend,
    as_runtime_metadata,
    resolve_herdr_binary,
)
from app.adapters.runtime_factory import (
    HERDR_VISIBLE_GATE_ENV,
    LEGACY_HERDR_VISIBLE_GATE_ENV,
    LEGACY_RUNTIME_BACKEND_ENV,
    RUNTIME_BACKEND_ENV,
    make_runtime_backend,
    runtime_backend_selection_from_env,
)
from app.domain.runtime import (
    RUNTIME_BACKEND_DIRECT,
    RUNTIME_BACKEND_HERDR_VISIBLE,
    RuntimeStartRequest,
)


@pytest.mark.asyncio
async def test_herdr_backend_start_stream_status_stop_with_fake_runner():
    calls: list[list[str]] = []

    async def runner(args: list[str], timeout_s: float) -> str:
        calls.append(args)
        joined = " ".join(args)
        if "workspace create" in joined:
            return '{"result":{"root_pane":{"pane_id":"w1-1","workspace_id":"w1","tab_id":"w1:1"},"workspace":{"workspace_id":"w1"},"tab":{"tab_id":"w1:1","label":"1"}}}'
        if "pane get" in joined:
            return '{"result":{"pane":{"agent_status":"working"}}}'
        if "pane read" in joined:
            return "x" * 32
        if "session stop" in joined:
            return '{"stopped":true}'
        return "{}"

    backend = HerdrCliRuntimeBackend(
        herdr_bin="herdr",
        session_prefix="e008-test",
        output_max_chars=10,
        runner=runner,
    )
    req = RuntimeStartRequest(
        run_id="run-1",
        project="kaidera-os",
        agent="kai",
        cwd="/tmp/kaidera-os",
        argv=["echo", "HERDR_OK"],
        metadata={"session_name": "e008-test-run-1", "ready_match": "READY_kaidera_os"},
        visible=True,
    )

    run = await backend.start_run(req)

    assert run.accepted is True
    assert run.backend == RUNTIME_BACKEND_HERDR_VISIBLE
    assert run.ref.session_name == "e008-test-run-1"
    assert run.ref.workspace_id == "w1"
    assert run.ref.pane_id == "w1-1"
    assert as_runtime_metadata(run.ref)["runtime"]["pane_id"] == "w1-1"
    assert any(call[:4] == ["herdr", "--session", "e008-test-run-1", "workspace"] for call in calls)
    wait_calls = [call for call in calls if "wait" in call and "output" in call]
    assert wait_calls
    assert wait_calls[0][wait_calls[0].index("--match") + 1] == "READY_kaidera_os"
    assert any(call[:5] == ["herdr", "--session", "e008-test-run-1", "pane", "run"] for call in calls)

    events = [event async for event in backend.stream("run-1")]
    assert len(events) == 1
    assert events[0].text == "x" * 10
    assert events[0].metadata["bounded"] is True

    status = await backend.status("run-1")
    assert status.agent_status == "working"

    reattached = await backend.reattach("run-1")
    assert reattached is not None
    assert reattached.ref.pane_id == "w1-1"

    await backend.stop("run-1")
    assert any(call[:3] == ["herdr", "session", "stop"] for call in calls)
    assert await backend.reattach("run-1") is None


@pytest.mark.asyncio
async def test_herdr_backend_launch_failure_is_value_not_exception():
    async def runner(args: list[str], timeout_s: float) -> str:
        raise RuntimeError("boom")

    backend = HerdrCliRuntimeBackend(herdr_bin="herdr", runner=runner)
    req = RuntimeStartRequest(
        run_id="run-bad",
        project="kaidera-os",
        agent="kai",
        cwd="/tmp/kaidera-os",
        argv=["echo", "nope"],
        metadata={"session_name": "e008-test-bad"},
        visible=True,
    )

    run = await backend.start_run(req)

    assert run.accepted is False
    assert run.status == "error"
    assert "boom" in (run.error or "")
    assert run.ref.session_name == "e008-test-bad"


def test_resolve_herdr_binary_prefers_env_path(tmp_path):
    herdr = tmp_path / "bin" / "herdr"

    assert resolve_herdr_binary({HERDR_BIN_ENV: str(herdr), "PATH": ""}) == str(herdr)


def test_resolve_herdr_binary_accepts_legacy_env_path(tmp_path):
    herdr = tmp_path / "bin" / "herdr"

    assert resolve_herdr_binary({LEGACY_HERDR_BIN_ENV: str(herdr), "PATH": ""}) == str(herdr)


def test_runtime_factory_does_not_import_herdr_adapter_at_module_top():
    source = (
        Path(__file__).resolve().parents[1] / "app" / "adapters" / "runtime_factory.py"
    ).read_text(encoding="utf-8")
    module_preamble = source.split("def _default_herdr_backend_factory", 1)[0]

    assert "runtime_herdr" not in module_preamble


@pytest.mark.asyncio
async def test_herdr_command_timeout_terminates_child_process():
    backend = HerdrCliRuntimeBackend(herdr_bin="herdr", command_timeout_s=0.1)

    with pytest.raises(RuntimeError, match="timed out"):
        await backend._run_command([sys.executable, "-c", "import time; time.sleep(10)"], 0.1)


def test_runtime_factory_defaults_and_rolls_back_to_direct():
    direct_backend = object()

    selection, backend = make_runtime_backend(env={}, direct_backend=direct_backend)
    assert selection.backend == RUNTIME_BACKEND_DIRECT
    assert backend is direct_backend

    selection, backend = make_runtime_backend(
        env={RUNTIME_BACKEND_ENV: RUNTIME_BACKEND_HERDR_VISIBLE},
        direct_backend=direct_backend,
    )
    assert selection.backend == RUNTIME_BACKEND_DIRECT
    assert selection.reason == "herdr-visible-disabled"
    assert backend is direct_backend


def test_runtime_factory_constructs_herdr_only_when_dev_gate_enabled():
    constructed = []

    class FakeHerdrBackend:
        pass

    def factory():
        constructed.append("herdr")
        return FakeHerdrBackend()

    selection = runtime_backend_selection_from_env(
        {
            RUNTIME_BACKEND_ENV: RUNTIME_BACKEND_HERDR_VISIBLE,
            HERDR_VISIBLE_GATE_ENV: "0",
        }
    )
    assert selection.backend == RUNTIME_BACKEND_DIRECT
    assert constructed == []

    selection, backend = make_runtime_backend(
        env={
            RUNTIME_BACKEND_ENV: RUNTIME_BACKEND_HERDR_VISIBLE,
            HERDR_VISIBLE_GATE_ENV: "1",
        },
        herdr_backend_factory=factory,
    )

    assert selection.backend == RUNTIME_BACKEND_HERDR_VISIBLE
    assert isinstance(backend, FakeHerdrBackend)
    assert constructed == ["herdr"]


def test_runtime_factory_uses_kaidera_os_env_names():
    selection = runtime_backend_selection_from_env(
        {
            LEGACY_RUNTIME_BACKEND_ENV: RUNTIME_BACKEND_HERDR_VISIBLE,
            LEGACY_HERDR_VISIBLE_GATE_ENV: "1",
        }
    )

    assert selection.backend == RUNTIME_BACKEND_HERDR_VISIBLE
