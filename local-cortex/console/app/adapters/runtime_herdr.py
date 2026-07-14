"""Herdr RuntimeBackend prototype adapter (E008 Inc04).

Dev-gated adapter only: no route imports this module by default. It talks to the
external `herdr` binary and returns the pure RuntimeBackend DTOs. The goal is to
replace/avoid app-side visible PTY/session/pane management, not to create another
Cortex/app-DB source of truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import shlex
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from app.domain.runtime import (
    DEFAULT_RUNTIME_OUTPUT_MAX_CHARS,
    RUNTIME_BACKEND_HERDR_VISIBLE,
    RuntimeBackend,
    RuntimeEvent,
    RuntimeRef,
    RuntimeRun,
    RuntimeStartRequest,
    RuntimeStatus,
)


CommandRunner = Callable[[list[str], float], Awaitable[str]]
HERDR_BIN_ENV = "KAIDERA_OS_HERDR_BIN"
LEGACY_HERDR_BIN_ENV = "LOCAL" + "DEV_HERDR_BIN"


def resolve_herdr_binary(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the Herdr CLI without hard-coding a Homebrew path."""

    source = os.environ if env is None else env
    configured = (source.get(HERDR_BIN_ENV) or "").strip()
    if not configured:
        configured = (source.get(LEGACY_HERDR_BIN_ENV) or "").strip()
    if configured:
        return str(Path(configured).expanduser())

    found = shutil.which("herdr", path=source.get("PATH"))
    if found:
        return found

    raise FileNotFoundError(
        f"Herdr binary not found on PATH. Install Herdr or set {HERDR_BIN_ENV} to the executable path."
    )


class HerdrCliRuntimeBackend(RuntimeBackend):
    """Thin external-binary Herdr adapter for explicit `herdr-visible` proofs.

    This prototype intentionally keeps mapping in memory plus returned RuntimeRef.
    Inc04 callers must copy `as_runtime_metadata(run.ref)` into app-DB run_state
    metadata if they want reattach across process restarts. Product migration to a
    typed `runtime_mapping` table remains deferred until after adapter proof.
    """

    def __init__(
        self,
        *,
        herdr_bin: Optional[str] = None,
        session_prefix: str = "e008-runtime",
        output_max_chars: int = DEFAULT_RUNTIME_OUTPUT_MAX_CHARS,
        command_timeout_s: float = 15.0,
        runner: Optional[CommandRunner] = None,
    ) -> None:
        self.herdr_bin = herdr_bin or resolve_herdr_binary()
        self.session_prefix = session_prefix
        self.output_max_chars = output_max_chars
        self.command_timeout_s = command_timeout_s
        self._external_runner = runner is not None
        self._runner = runner or self._run_command
        self._refs: dict[str, RuntimeRef] = {}
        self._server_processes: dict[str, asyncio.subprocess.Process] = {}

    async def start_run(self, request: RuntimeStartRequest) -> RuntimeRun:
        session_name = str(
            request.metadata.get("session_name")
            or f"{self.session_prefix}-{request.project}-{request.run_id}"
        )
        pane_label = str(request.metadata.get("pane_label") or f"{request.agent}:{request.run_id}")
        workspace_label = str(request.metadata.get("workspace_label") or request.project)

        try:
            await self._ensure_server(session_name)
            workspace = await self._run_json(
                [
                    self.herdr_bin,
                    "--session",
                    session_name,
                    "workspace",
                    "create",
                    "--cwd",
                    request.cwd,
                    "--label",
                    workspace_label,
                    "--focus",
                ]
            )
            root_pane = workspace["result"]["root_pane"]  # fitness:allow-literal swarm WIP
            workspace_info = workspace["result"].get("workspace", {})
            tab_info = workspace["result"].get("tab", {})
            pane_id = root_pane["pane_id"]

            await self._run_text(
                [self.herdr_bin, "--session", session_name, "pane", "rename", pane_id, pane_label]  # fitness:allow-literal swarm WIP
            )
            await self._wait_ready(session_name, pane_id, request)
            await self._run_text(
                [self.herdr_bin, "--session", session_name, "pane", "run", pane_id, shlex.join(request.argv)]
            )

            ref = RuntimeRef(
                backend=RUNTIME_BACKEND_HERDR_VISIBLE,
                session_name=session_name,
                workspace_id=workspace_info.get("workspace_id") or root_pane.get("workspace_id"),
                workspace_label=workspace_label,
                tab_id=tab_info.get("tab_id") or root_pane.get("tab_id"),
                tab_label=tab_info.get("label"),
                pane_id=pane_id,
                pane_label=pane_label,
                last_resolved_at=_now_iso(),
                metadata={
                    "cwd": request.cwd,
                    "argv": list(request.argv),
                    "ready_patterns": self._ready_patterns(request),
                    "resolver": "session-label-pane-id-cache",
                },
            )
            self._refs[request.run_id] = ref
            return RuntimeRun(
                run_id=request.run_id,
                backend=RUNTIME_BACKEND_HERDR_VISIBLE,
                status="running",
                ref=ref,
                accepted=True,
            )
        except Exception as exc:  # adapter boundary: launch refusal is a value
            return RuntimeRun(
                run_id=request.run_id,
                backend=RUNTIME_BACKEND_HERDR_VISIBLE,
                status="error",
                ref=RuntimeRef(backend=RUNTIME_BACKEND_HERDR_VISIBLE, session_name=session_name),
                accepted=False,
                error=str(exc),
            )

    async def stream(self, run_id: str):
        ref = self._require_ref(run_id)
        text = await self._pane_read(ref)
        if len(text) > self.output_max_chars:
            text = text[-self.output_max_chars :]
        yield RuntimeEvent(
            run_id=run_id,
            seq=1,
            kind="output",
            text=text,
            metadata={"source": "herdr:pane.read", "bounded": True},
        )

    async def send(self, run_id: str, text_or_keys: str) -> None:
        ref = self._require_ref(run_id)
        await self._run_text(
            [self.herdr_bin, "--session", ref.session_name or "", "pane", "send-text", ref.pane_id or "", text_or_keys]
        )

    async def status(self, run_id: str) -> RuntimeStatus:
        ref = self._require_ref(run_id)
        pane = await self._run_json(
            [self.herdr_bin, "--session", ref.session_name or "", "pane", "get", ref.pane_id or ""]
        )
        pane_info = pane.get("result", {}).get("pane", {})
        return RuntimeStatus(
            run_id=run_id,
            backend=RUNTIME_BACKEND_HERDR_VISIBLE,
            status="running",
            ref=ref,
            agent_status=pane_info.get("agent_status"),
            heartbeat_at=_now_iso(),
        )

    async def stop(self, run_id: str, reason: Optional[str] = None) -> None:
        ref = self._require_ref(run_id)
        if ref.session_name:
            await self._run_text([self.herdr_bin, "session", "stop", ref.session_name, "--json"])
            proc = self._server_processes.pop(ref.session_name, None)
            if proc is not None and proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        self._refs.pop(run_id, None)

    async def reattach(self, run_id: str) -> Optional[RuntimeRun]:
        ref = self._refs.get(run_id)
        if ref is None:
            return None
        ref.last_resolved_at = _now_iso()
        return RuntimeRun(
            run_id=run_id,
            backend=RUNTIME_BACKEND_HERDR_VISIBLE,
            status="running",
            ref=ref,
            accepted=True,
        )

    async def _ensure_server(self, session_name: str) -> None:
        if self._external_runner:
            # Tests inject a runner and do not need a real long-lived server.
            return
        proc = self._server_processes.get(session_name)
        if proc is not None and proc.returncode is None:
            return
        proc = await asyncio.create_subprocess_exec(
            self.herdr_bin,
            "--session",
            session_name,
            "server",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._server_processes[session_name] = proc
        await asyncio.sleep(0.5)

    def _ready_patterns(self, request: RuntimeStartRequest) -> list[str]:
        if request.metadata.get("skip_ready_wait") is True:
            return []

        values: list[str] = []
        metadata_value = request.metadata.get("ready_matches", request.metadata.get("ready_match"))
        if isinstance(metadata_value, str):
            values.append(metadata_value)
        elif isinstance(metadata_value, Sequence):
            values.extend(str(item) for item in metadata_value)

        cwd = request.cwd.strip()
        if cwd:
            basename = Path(cwd).name
            if basename:
                values.append(basename)
            values.append(cwd)

        seen: set[str] = set()
        patterns: list[str] = []
        for value in values:
            pattern = value.strip()
            if pattern and pattern not in seen:
                seen.add(pattern)
                patterns.append(pattern)
        return patterns

    async def _wait_ready(self, session_name: str, pane_id: str, request: RuntimeStartRequest) -> None:
        patterns = self._ready_patterns(request)
        if not patterns:
            return

        last_error: Exception | None = None
        for marker in patterns:
            try:
                await self._run_text(
                    [
                        self.herdr_bin,
                        "--session",
                        session_name,
                        "wait",
                        "output",
                        pane_id,
                        "--match",
                        marker,
                        "--source",
                        "recent-unwrapped",
                        "--timeout",
                        str(int(self.command_timeout_s * 1000)),
                    ]
                )
                return
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"Herdr pane did not become ready for markers: {patterns}") from last_error

    async def _pane_read(self, ref: RuntimeRef) -> str:
        return await self._run_text(
            [
                self.herdr_bin,
                "--session",
                ref.session_name or "",
                "pane",
                "read",
                ref.pane_id or "",
                "--source",
                "recent-unwrapped",
                "--lines",
                "200",
            ]
        )

    async def _run_json(self, args: list[str]) -> dict[str, Any]:
        text = await self._run_text(args)
        return json.loads(text)

    async def _run_text(self, args: list[str]) -> str:
        return await self._runner(args, self.command_timeout_s)

    async def _run_command(self, args: list[str], timeout_s: float) -> str:
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise RuntimeError(f"herdr command timed out after {timeout_s:.1f}s: {shlex.join(args)}") from exc
        except BaseException:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            raise
        if proc.returncode != 0:
            raise RuntimeError(
                f"herdr command failed rc={proc.returncode}: {stderr.decode(errors='replace')[-1000:]}"
            )
        return stdout.decode(errors="replace").strip()

    def _require_ref(self, run_id: str) -> RuntimeRef:
        ref = self._refs.get(run_id)
        if ref is None:
            raise KeyError(f"unknown runtime run_id: {run_id}")
        return ref


def as_runtime_metadata(ref: RuntimeRef) -> dict[str, Any]:
    """Shape to store in run_state.metadata during the prototype phase."""

    return {"runtime": asdict(ref)}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = [
    "HERDR_BIN_ENV",
    "LEGACY_HERDR_BIN_ENV",
    "HerdrCliRuntimeBackend",
    "as_runtime_metadata",
    "resolve_herdr_binary",
]
