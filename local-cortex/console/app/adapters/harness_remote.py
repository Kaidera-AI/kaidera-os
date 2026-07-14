"""HarnessPort adapter — the REMOTE host-service spawn (the container→host wire).

The imperative-shell adapter (`app/adapters/`) that IMPLEMENTS the pure `HarnessPort`
Protocol (`app/domain/harness.py`) over **httpx** — the CONTAINER-side twin of the I1
`LocalHarnessAdapter`. Where the local adapter shells `subprocess.Popen` in-process,
this one crosses the HOST BOUNDARY: the autonomous loop runs in a container, but the
harness CLIs (`claude-code` / `pi`) and their interactive OAuth login live on the
HOST, so the spawn becomes a `POST` to a host-resident harness-service
(`app/harness_service.py`, I2). Arrows point inward (ratified design §3): the domain
port stays pure (no httpx); this adapter is the boundary that talks the wire.

THE WIRE (mirrors `harness_service.py`):
  * `spawn_run`  → `POST {base_url}/spawn`  with `Authorization: Bearer {token}` and a
    JSON body = the `SpawnRequest` fields. A `202` is the async "dispatched" shape —
    `SpawnHandle(accepted=True, exit_code=None)` (the host service spawns the worker
    and returns immediately; the worker reports its terminal state later via the
    run-state store the orchestrator already reads). A `4xx/5xx` →
    `SpawnHandle(accepted=False, error="<status>")`.
  * `cancel_run` → `POST {base_url}/cancel/{run_id}` (bearer) → `200 {"cancelled": bool}`.

FIRE-AND-FORGET + graceful-degrade (the port contract): NEITHER method EVER raises. A
down service / a connect error / a timeout is reported (`accepted=False` / `False`),
never propagated — a broken spawn path must not crash the dispatch loop (the same law
the local adapter's `OSError` handling honours).

PRODUCTION WIRING: `_make_harness_port()` constructs this with NO args when
`HARNESS_SPAWN_MODE=remote`, so `base_url` + `token` resolve from the environment
(`HARNESS_SERVICE_PORT` default 8766 on `host.docker.internal`; `HARNESS_SERVICE_TOKEN`).
`http_client` is injectable for tests (an `httpx.AsyncClient` over a `MockTransport`)
so the wire contract is asserted with no socket + no live host service.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Optional

import httpx

# The container reaches the host via the docker host-gateway alias (I3 wires
# `extra_hosts: ["host.docker.internal:host-gateway"]`); the port defaults to the
# harness-service's 8766. Neither is a per-project literal — both are config-as-data
# env knobs (see docs/sdk/modules/harness.md §6).
_DEFAULT_HOST = os.environ.get("HARNESS_SERVICE_HOST", "host.docker.internal")
_DEFAULT_PORT = os.environ.get("HARNESS_SERVICE_PORT", "8766")


def _default_base_url() -> str:
    """The host harness-service base URL from the environment (the container→host
    target). Resolved at construct time so the env can be set before boot."""
    return f"http://{_DEFAULT_HOST}:{_DEFAULT_PORT}"


class RemoteHarnessAdapter:
    """`HarnessPort` over httpx — POST to the host harness-service (`/spawn`, `/cancel`).

    Constructed with no args in production (binds `HARNESS_SERVICE_*` from the env and
    builds its own pooled `httpx.AsyncClient`); tests inject `base_url` / `token` /
    `http_client`. Satisfies the `HarnessPort` Protocol structurally."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        connect_timeout: float = 5.0,
        request_timeout: float = 15.0,
    ) -> None:
        self._base_url = (base_url or _default_base_url()).rstrip("/")
        # The shared bearer token the host service requires (a loopback/host-gateway
        # service is still authed). Blank ⇒ no Authorization header is sent (the
        # service may have auth disabled; that is the service's call, not ours).
        self._token = (token if token is not None else os.environ.get("HARNESS_SERVICE_TOKEN", "")) or ""
        # Generous-but-bounded timeout: the connect guards a down/unreachable host;
        # the read guards a hung service. A spawn POST returns fast (the service
        # fire-and-forgets the worker), so 15s is ample.
        self._timeout = httpx.Timeout(request_timeout, connect=connect_timeout)
        # Reuse an injected client (tests) or build one. We own the built client and
        # close it in aclose(); an injected client is the caller's to own — but we
        # close it too (the orchestrator constructs ONE adapter for the app lifetime,
        # so closing on adapter teardown is correct in both cases).
        self._client = http_client or httpx.AsyncClient(timeout=self._timeout)

    def _headers(self) -> dict[str, str]:
        """Bearer auth header when a token is configured (omitted when blank)."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def spawn_run(self, request):
        """POST the serialized SpawnRequest to the host service. NEVER raises:
        a 202 → the async 'dispatched' handle (accepted=True, exit_code=None); a
        4xx/5xx → accepted=False + error='<status>'; a connect/timeout error →
        accepted=False + error=str(exc)."""
        from app.domain.harness import SpawnHandle

        try:
            resp = await self._client.post(
                f"{self._base_url}/spawn",
                json=asdict(request),
                headers=self._headers(),
                timeout=self._timeout,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            # The host service is down / unreachable / hung — report, never raise.
            return SpawnHandle(run_id=request.run_id, accepted=False, error=str(exc))
        except httpx.HTTPError as exc:
            # Any other transport-level failure also degrades (belt-and-braces — the
            # spawn path must not crash the dispatch loop).
            return SpawnHandle(run_id=request.run_id, accepted=False, error=str(exc))

        if resp.status_code == 202:
            # The async dispatched shape: the worker was launched on the host; its
            # terminal state arrives later via the run-state store.
            return SpawnHandle(run_id=request.run_id, accepted=True, exit_code=None)
        # Any non-202 → the spawn was rejected; carry the status as the reason.
        return SpawnHandle(
            run_id=request.run_id, accepted=False, error=str(resp.status_code)
        )

    async def spawn_chat(self, request):
        """POST the serialized ChatSpawnRequest to the host service `/chat` (harness-
        service I4 — the interactive-chat host seam). Same wire + degrade contract as
        `spawn_run`: a 202 → the async 'dispatched' handle (accepted=True,
        exit_code=None) — the host chat runner writes the reply to the run-state row the
        route pre-created (the UI reads /runstate/stream); a 4xx/5xx → accepted=False +
        error='<status>'; a connect/timeout/transport error → accepted=False +
        str(exc). NEVER raises (a broken chat seam must not crash the route)."""
        from app.domain.harness import SpawnHandle

        try:
            resp = await self._client.post(
                f"{self._base_url}/chat",
                json=asdict(request),
                headers=self._headers(),
                timeout=self._timeout,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return SpawnHandle(run_id=request.run_id, accepted=False, error=str(exc))
        except httpx.HTTPError as exc:
            return SpawnHandle(run_id=request.run_id, accepted=False, error=str(exc))

        if resp.status_code == 202:
            return SpawnHandle(run_id=request.run_id, accepted=True, exit_code=None)
        return SpawnHandle(
            run_id=request.run_id, accepted=False, error=str(resp.status_code)
        )

    async def upload_attachment(self, attachment_id: str, filename: str, data_b64: str) -> str:
        """Forward ONE chat attachment's bytes to the host `/upload` (chat file-
        attachments, step 6 Inc A — the container→host attachment seam). The container's
        chat upload landed the bytes in ITS sandbox; before the remote chat spawn we POST
        each file's base64 bytes to the HOST (which has the disk the chat runner reads)
        and the host returns the path it wrote.

        Returns the HOST path on a 200 `{host_path}`; degrades to "" on ANY failure (a
        non-200, a connect/timeout/transport error, a missing host_path, a bad body).
        NEVER raises — a down host-upload must degrade the turn to no-attachment (the
        caller drops a "" path), not crash the chat (house graceful-degrade law)."""
        try:
            resp = await self._client.post(
                f"{self._base_url}/upload",
                json={"attachment_id": attachment_id, "filename": filename, "data": data_b64},
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            return ""
        if resp.status_code != 200:
            return ""
        try:
            data = resp.json()
        except ValueError:
            return ""
        host_path = data.get("host_path") if isinstance(data, dict) else None
        return host_path if isinstance(host_path, str) and host_path else ""

    async def cancel_run(self, run_id: str) -> bool:
        """POST /cancel/{run_id} (bearer). Best-effort: a 200 {"cancelled": bool}
        returns that bool; ANY error (non-200, connect, timeout, bad JSON) → False.
        NEVER raises."""
        rid = (run_id or "").strip()
        if not rid:
            return False
        try:
            resp = await self._client.post(
                f"{self._base_url}/cancel/{rid}",
                headers=self._headers(),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            return bool(data.get("cancelled")) if isinstance(data, dict) else False
        except (httpx.HTTPError, ValueError):
            # Transport error or non-JSON body → treat as "cancel not confirmed".
            return False

    async def aclose(self) -> None:
        """Close the underlying httpx client (idempotent-safe). Mirrors
        CortexClient.aclose — call on app shutdown."""
        try:
            await self._client.aclose()
        except Exception:
            pass


__all__ = ["RemoteHarnessAdapter"]
