"""Background online-license refresh loop.

Transport lives in ``app.license_client``. This module owns scheduling and
startup-safe logging so the FastAPI lifespan can start one advisory task without
knowing platform-transport details.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any, Callable

from app import license_client

LoadSettings = Callable[[], dict[str, Any]]
SaveSettings = Callable[[dict[str, Any]], bool]


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except Exception:
        return default


async def heartbeat_forever(
    *,
    load_settings: LoadSettings,
    save_settings: SaveSettings,
    stop: asyncio.Event,
    interval_s: int | None = None,
    initial_delay_s: int | None = None,
    log: Any = None,
) -> None:
    """Refresh online grants in the background, fail-soft forever."""
    interval = interval_s or _int_env(
        "KAIDERA_OS_LICENSE_HEARTBEAT_INTERVAL_SECONDS", 24 * 60 * 60, minimum=60
    )
    initial = initial_delay_s if initial_delay_s is not None else _int_env(
        "KAIDERA_OS_LICENSE_HEARTBEAT_INITIAL_DELAY_SECONDS", 60, minimum=0
    )

    if initial:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=initial)
        if stop.is_set():
            return

    while not stop.is_set():
        try:
            result = await license_client.heartbeat(
                settings=load_settings(),
                save_settings=save_settings,
            )
            if result.ok and log:
                release = (
                    f" (release {result.latest_release.get('version')})"
                    if result.latest_release else ""
                )
                log.info("license heartbeat ok%s", release)
            elif log and result.error not in {
                "no valid license grant to heartbeat",
                "current grant has no license_id",
            }:
                log.warning("license heartbeat soft-failed: %s", result.error)
        except Exception as exc:
            if log:
                log.warning("license heartbeat loop failed softly: %s", exc)

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
