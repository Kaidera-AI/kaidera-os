"""Resolve the newest installed copy of a versioned harness CLI."""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from typing import Mapping

_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def executable_candidates(program: str, *, env: Mapping[str, str] | None = None) -> list[str]:
    """Return every executable named ``program`` in effective PATH order."""
    if os.path.sep in program or (os.path.altsep and os.path.altsep in program):
        return [program]
    out: list[str] = []
    for directory in os.get_exec_path(dict(env) if env is not None else None):
        candidate = os.path.join(directory, program)
        if candidate not in out and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            out.append(candidate)
    return out or [program]


@lru_cache(maxsize=64)
def _version_for_file(
    program: str,
    mtime_ns: int | None,
    size: int | None,
    resolved_path: str,
) -> tuple[int, int, int] | None:
    del mtime_ns, size, resolved_path
    try:
        result = subprocess.run(
            [program, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_PATTERN.search("\n".join((result.stdout or "", result.stderr or "")))
    return tuple(int(part) for part in match.groups()) if match else None


def executable_version(program: str) -> tuple[int, int, int] | None:
    """Read a CLI semver, re-probing when its resolved file changes."""
    try:
        stat = os.stat(program)
        signature = (stat.st_mtime_ns, stat.st_size, os.path.realpath(program))
    except OSError:
        signature = (None, None, program)
    return _version_for_file(program, *signature)


def resolve_latest_executable(
    program: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Select the highest-semver installed executable, independent of PATH order."""
    candidates = executable_candidates(program, env=env)
    ranked = [
        (version, -index, candidate)
        for index, candidate in enumerate(candidates)
        if (version := executable_version(candidate)) is not None
    ]
    return max(ranked)[2] if ranked else candidates[0]


__all__ = ["executable_candidates", "executable_version", "resolve_latest_executable"]
